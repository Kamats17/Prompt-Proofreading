"""First-touch draft pipeline for one prospect.

  guards (blocked / existing live draft / email / grade / evidence)
    -> greeting name -> subject from the primary finding
    -> generate -> QA (one automatic regeneration with the failed checks)
    -> insert drafts row (DB unique index is the final duplicate backstop).

Pushes happen separately via the dashboard "Approve" action. This module never
touches Gmail.
"""
import json
import sqlite3

from app.db.models import Draft, Event
from app.db.repo import (
    get_prospect, latest_audit_for_prospect, insert_draft, log_event,
    latest_live_draft_for_prospect, DuplicateDraftError,
)
from app.draft.generator import generate_draft
from app.draft.name_guess import guess_first_name, first_name_from_full
from app.draft.qa_gate import run_qa
from app.audit.grader import rebuild_case
from app.draft.voice import pick_subject, short_name, greeting_name


def resolve_greeting(*, explicit: str | None, contact_name: str | None,
                     owner_first_name: str | None, email: str | None) -> str:
    """explicit arg -> source contact name -> owner named on the site -> email
    local-part (only real first names) -> 'there'."""
    for cand in (explicit, first_name_from_full(contact_name),
                 first_name_from_full(owner_first_name), guess_first_name(email)):
        if cand:
            return cand
    return greeting_name(None)


def draft_for_prospect(conn: sqlite3.Connection, prospect_id: int,
                       *, contact_name: str | None = None,
                       allow_no_email: bool = False) -> dict:
    p = get_prospect(conn, prospect_id)
    if not p:
        return {"ok": False, "error": "prospect not found"}
    if p.blocked_reason:
        return {"ok": False, "skipped": f"prospect blocked ({p.blocked_reason})"}

    existing = latest_live_draft_for_prospect(conn, prospect_id, touch=1)
    if existing:
        return {"ok": False,
                "skipped": f"draft #{existing.id} already exists (status={existing.status}); "
                           "reject it first to re-draft"}
    if not p.email and not allow_no_email:
        log_event(conn, Event(prospect_id=prospect_id, event_type="draft_skipped_no_email"))
        return {"ok": False, "skipped": "no_email — set --allow-no-email or fill email first"}
    audit = latest_audit_for_prospect(conn, prospect_id)
    if not audit:
        return {"ok": False, "error": "no audit yet — run audit first"}
    if audit.final_grade not in {"A", "B"}:
        return {"ok": False, "skipped": f"grade={audit.final_grade} (only A/B drafts)"}

    issues = json.loads(audit.issues_json or "[]")
    verified = [i for i in issues if (i.get("confidence") or "") == "verified"
                and ((i.get("quote") or "").strip() or i.get("fact_id"))]
    if not verified:
        return {"ok": False, "skipped": "audit has no verified evidence to cite"}
    meta = json.loads(audit.meta_json or "{}")

    greeting = resolve_greeting(explicit=contact_name, contact_name=p.contact_name,
                                owner_first_name=meta.get("owner_first_name"), email=p.email)
    primary, second = verified[0], (verified[1] if len(verified) > 1 else None)
    subject = pick_subject(p.company, primary.get("type"), seed=prospect_id)
    facts = meta.get("facts") or {}
    rebuild = rebuild_case(facts)
    short = short_name(p.company)

    def _gen(feedback=None, prior_body=None):
        return generate_draft(
            company=p.company, short=short, industry=p.industry, city=p.city,
            greeting=greeting, subject=subject, primary=primary, second=second,
            personal_detail=meta.get("personal_detail", ""), demo_offer=audit.demo_angle or "",
            rebuild=rebuild, feedback=feedback, prior_body=prior_body)

    def _qa(gen):
        return run_qa(
            conn, company=p.company, prospect_email=p.email, prospect_url=p.url,
            subject=subject, body_text=gen["body_text"],
            audit_issues_json=audit.issues_json or "[]", final_grade=audit.final_grade,
            prospect_id=prospect_id, used_quotes=gen.get("used_quotes"),
            used_fact_ids=gen.get("used_fact_ids"), touch=1, blocked_reason=p.blocked_reason,
            audit_facts=facts)

    gen = _gen()
    if "error" in gen:
        log_event(conn, Event(prospect_id=prospect_id, event_type="draft_gen_error",
                              payload_json=json.dumps(gen)))
        return {"ok": False, "error": gen["error"], "error_kind": gen.get("error_kind")}

    qa = _qa(gen)
    retried = False
    if not qa["pass"]:
        retried = True
        gen2 = _gen(feedback=qa["blockers"], prior_body=gen["body_text"])
        if "error" not in gen2:
            qa2 = _qa(gen2)
            if qa2["pass"] or len(qa2["blockers"]) <= len(qa["blockers"]):
                gen, qa = gen2, qa2

    draft = Draft(
        prospect_id=prospect_id, audit_id=audit.id, touch=1,
        subject=subject, subject_variant=subject, finding_type=primary.get("type"),
        evidence_json=json.dumps({"used_quotes": gen.get("used_quotes") or [], "used_fact_ids": gen.get("used_fact_ids") or []}),
        body_text=gen["body_text"], body_html=gen["body_html"],
        qa_pass=1 if qa["pass"] else 0,
        qa_blockers_json=json.dumps(qa["blockers"] + qa["warnings"]) if (qa["blockers"] or qa["warnings"]) else None,
        status="pending",
    )
    try:
        draft_id = insert_draft(conn, draft)
    except DuplicateDraftError as e:
        return {"ok": False, "skipped": str(e)}
    log_event(conn, Event(
        prospect_id=prospect_id, draft_id=draft_id, event_type="drafted",
        payload_json=json.dumps({"qa_pass": qa["pass"], "blockers": qa["blockers"],
                                 "warnings": qa["warnings"], "retried": retried,
                                 "finding_type": primary.get("type"), "subject": subject,
                                 "rebuild_worthy": rebuild["worthy"],
                                 "used_quotes": gen.get("used_quotes"),
                                 "used_fact_ids": gen.get("used_fact_ids")}),
    ))
    return {"ok": True, "draft_id": draft_id, "qa_pass": qa["pass"], "blockers": qa["blockers"],
            "warnings": qa["warnings"], "subject": subject, "retried": retried}
