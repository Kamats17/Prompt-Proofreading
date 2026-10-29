"""`pp-agent followups` — draft touch 2 / touch 3 in the SAME Gmail thread.

A follow-up is created only when:
  * the previous touch was actually SENT (seen by `pp-agent sync`), not just pushed;
  * nobody replied, opted out, or bounced (any of those moves the thread out of 'sent');
  * enough business days have passed since the FIRST email
    (sending.followup_business_days, default [4, 10]);
  * the prospect isn't blocked or on the DNC list, and touch < sending.max_touches.
Follow-ups go through the same QA gate (touch > 1) and land as pending drafts
for human approval, exactly like first emails.
"""
import datetime as _dt
import json
import sqlite3

from app.db.models import Draft, Event
from app.db.repo import (
    get_prospect, get_audit, insert_draft, log_event, latest_live_draft_for_prospect,
    DuplicateDraftError,
)
from app.audit.grader import rebuild_case
from app.draft.generator import generate_followup
from app.draft.pipeline import resolve_greeting
from app.draft.qa_gate import run_qa
from app.draft.voice import short_name
from app.safety.dnc import is_dnc
from app.settings import FOLLOWUP_BUSINESS_DAYS, MAX_TOUCHES


def business_days_between(start: _dt.date, end: _dt.date) -> int:
    """Weekdays strictly after `start`, up to and including `end`."""
    if end <= start:
        return 0
    days = 0
    d = start
    while d < end:
        d += _dt.timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def _due_touch(touch1_sent: _dt.date, today: _dt.date, next_touch: int) -> bool:
    idx = next_touch - 2
    if idx < 0 or idx >= len(FOLLOWUP_BUSINESS_DAYS):
        return False
    return business_days_between(touch1_sent, today) >= FOLLOWUP_BUSINESS_DAYS[idx]


def find_due_followups(conn: sqlite3.Connection, today: _dt.date | None = None) -> list[dict]:
    """Threads whose latest touch is 'sent' (no reply) and the next touch is due."""
    today = today or _dt.date.today()
    rows = conn.execute(
        """SELECT d.* FROM drafts d
           WHERE d.status = 'sent'
             AND d.touch = (SELECT MAX(touch) FROM drafts x
                            WHERE x.prospect_id = d.prospect_id AND x.status != 'rejected')
           ORDER BY d.id""").fetchall()
    due = []
    for d in rows:
        nxt = d["touch"] + 1
        if nxt > MAX_TOUCHES:
            continue
        first = conn.execute(
            "SELECT sent_at FROM drafts WHERE prospect_id = ? AND touch = 1 AND sent_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 1", (d["prospect_id"],)).fetchone()
        if not first:
            continue
        sent_date = _dt.date.fromisoformat(first["sent_at"][:10])
        if _due_touch(sent_date, today, nxt):
            due.append(dict(d))
    return due


def create_followups(conn: sqlite3.Connection, *, limit: int = 20,
                     today: _dt.date | None = None) -> dict:
    stats = {"due": 0, "created": 0, "qa_blocked": 0, "skipped": 0, "errors": 0}
    for prev in find_due_followups(conn, today)[:limit]:
        stats["due"] += 1
        p = get_prospect(conn, prev["prospect_id"])
        nxt = prev["touch"] + 1
        if p.blocked_reason or is_dnc(conn, company=p.company, domain=p.url, email=p.email):
            stats["skipped"] += 1
            continue
        if latest_live_draft_for_prospect(conn, p.id, touch=nxt):
            stats["skipped"] += 1
            continue
        audit = get_audit(conn, prev["audit_id"])
        issues = [i for i in json.loads(audit.issues_json or "[]") if i.get("confidence") == "verified"]
        meta = json.loads(audit.meta_json or "{}")
        second = issues[1] if len(issues) > 1 else None
        facts = meta.get("facts") or {}
        rebuild = rebuild_case(facts)
        greeting = resolve_greeting(explicit=None, contact_name=p.contact_name,
                                    owner_first_name=meta.get("owner_first_name"), email=p.email)
        subject = prev["subject"] if prev["subject"].lower().startswith("re:") else f"Re: {prev['subject']}"

        def _gen(feedback=None, prior_body=None):
            return generate_followup(touch=nxt, greeting=greeting, short=short_name(p.company),
                                     previous_body=prev["body_text"], second=second,
                                     demo_offer=audit.demo_angle or "", industry=p.industry or "",
                                     rebuild=rebuild, feedback=feedback, prior_body=prior_body)

        def _qa(gen):
            return run_qa(conn, company=p.company, prospect_email=p.email, prospect_url=p.url,
                          subject=subject, body_text=gen["body_text"],
                          audit_issues_json=audit.issues_json or "[]",
                          final_grade=audit.final_grade, prospect_id=p.id,
                          used_quotes=gen.get("used_quotes"), used_fact_ids=gen.get("used_fact_ids"),
                          touch=nxt, blocked_reason=p.blocked_reason, audit_facts=facts)

        gen = _gen()
        if "error" in gen:
            stats["errors"] += 1
            log_event(conn, Event(prospect_id=p.id, event_type="followup_gen_error",
                                  payload_json=json.dumps(gen)))
            if gen.get("error_kind") == "auth":
                break
            continue
        qa = _qa(gen)
        if not qa["pass"]:
            gen2 = _gen(feedback=qa["blockers"], prior_body=gen["body_text"])
            if "error" not in gen2:
                qa2 = _qa(gen2)
                if qa2["pass"] or len(qa2["blockers"]) <= len(qa["blockers"]):
                    gen, qa = gen2, qa2
        draft = Draft(
            prospect_id=p.id, audit_id=audit.id, touch=nxt, parent_draft_id=prev["id"],
            subject=subject, subject_variant=f"followup-{nxt}", finding_type=prev["finding_type"],
            evidence_json=json.dumps({"used_quotes": gen.get("used_quotes") or [], "used_fact_ids": gen.get("used_fact_ids") or []}),
            body_text=gen["body_text"], body_html=gen["body_html"],
            gmail_thread_id=prev["gmail_thread_id"],
            qa_pass=1 if qa["pass"] else 0,
            qa_blockers_json=json.dumps(qa["blockers"] + qa["warnings"]) if (qa["blockers"] or qa["warnings"]) else None,
        )
        try:
            draft_id = insert_draft(conn, draft)
        except DuplicateDraftError:
            stats["skipped"] += 1
            continue
        stats["created"] += 1
        if not qa["pass"]:
            stats["qa_blocked"] += 1
        log_event(conn, Event(prospect_id=p.id, draft_id=draft_id, event_type="followup_drafted",
                              payload_json=json.dumps({"touch": nxt, "qa_pass": qa["pass"],
                                                       "blockers": qa["blockers"]})))
    return stats
