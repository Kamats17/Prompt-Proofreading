"""The reply that carries the mockup back to an interested prospect.

`default_reply_text` gives an editable starting point; `push_mockup_reply`
creates a Gmail DRAFT (never sends) in the prospect's existing thread with the
mockup PNG attached. Same safety checks as outreach: DNC, opt-out/bounce blocks,
valid recipient.
"""
import datetime as _dt
import json
import re
import sqlite3
from pathlib import Path

from app.db.models import Event
from app.db.repo import get_prospect, latest_audit_for_prospect, log_event
from app.audit.grader import rebuild_case
from app.draft.generator import to_html_body
from app.draft.gmail_client import create_gmail_draft
from app.draft.pipeline import resolve_greeting
from app.draft.voice import short_name, possessive
from app.mockup.builder import mockup_paths, plan_changes
from app.safety.dnc import is_dnc
from app.settings import SENDER_FIRST_NAME
from app.sources._normalize import normalize_email

HARD_BLOCKS = {"dnc", "opted_out", "bounced"}


def next_business_days(n: int = 2, today: _dt.date | None = None) -> list[str]:
    d = today or _dt.date.today()
    out = []
    while len(out) < n:
        d += _dt.timedelta(days=1)
        if d.weekday() < 5:
            out.append(d.strftime("%A"))
    return out


def default_reply_text(conn: sqlite3.Connection, prospect_id: int,
                       today: _dt.date | None = None) -> str:
    p = get_prospect(conn, prospect_id)
    audit = latest_audit_for_prospect(conn, prospect_id)
    meta = json.loads(audit.meta_json or "{}") if audit else {}
    issues = json.loads(audit.issues_json or "[]") if audit else []
    greeting = resolve_greeting(explicit=None, contact_name=p.contact_name,
                                owner_first_name=meta.get("owner_first_name"), email=p.email)
    changes = plan_changes(meta.get("facts") or {}, issues, bool(p.phone))[:3]
    bullets = "\n".join(f"- {c['title']}" for c in changes) or "- A phone-first layout for the homepage"
    d1, d2 = next_business_days(2, today)
    bigger = ""
    if rebuild_case(meta.get("facts") or {})["worthy"]:
        bigger = ("If a fresh site makes more sense than patching this one, I can bring a rough "
                  "quote for a full rebuild to the call too.\n\n")
    return (f"Hi {greeting},\n\n"
            f"Here's the mockup I mentioned. On the left is {possessive(short_name(p.company))} homepage on a "
            f"phone today, and on the right is what I'd change:\n\n{bullets}\n\n{bigger}"
            f"Happy to walk you through it in 10 minutes. Would {d1} or {d2} work?\n\n"
            f"Thanks,\n{SENDER_FIRST_NAME}")


def _thread_for(conn: sqlite3.Connection, prospect_id: int):
    return conn.execute(
        "SELECT subject, gmail_thread_id, rfc_message_id FROM drafts WHERE prospect_id = ? "
        "AND gmail_thread_id IS NOT NULL AND status IN ('pushed', 'sent', 'replied') "
        "ORDER BY id DESC LIMIT 1", (prospect_id,)).fetchone()


def push_mockup_reply(conn: sqlite3.Connection, prospect_id: int, body_text: str) -> dict:
    p = get_prospect(conn, prospect_id)
    if not p:
        return {"ok": False, "error": "prospect not found"}
    if p.blocked_reason in HARD_BLOCKS:
        return {"ok": False, "error": f"prospect is blocked ({p.blocked_reason}) — not emailing"}
    dnc = is_dnc(conn, company=p.company, domain=p.url, email=p.email)
    if dnc:
        return {"ok": False, "error": f"on DNC list: {dnc}"}
    to = normalize_email(p.email)
    if not to:
        return {"ok": False, "error": "prospect has no valid email"}
    png = mockup_paths(prospect_id)["png"]
    if not png.exists():
        return {"ok": False, "error": "no mockup yet — generate it first"}
    if not (body_text or "").strip():
        return {"ok": False, "error": "reply text is empty"}

    thread = _thread_for(conn, prospect_id)
    short = short_name(p.company)
    if thread:
        subj = thread["subject"]
        subject = subj if subj.lower().startswith("re:") else f"Re: {subj}"
        thread_id, in_reply_to = thread["gmail_thread_id"], thread["rfc_message_id"]
    else:
        subject, thread_id, in_reply_to = f"Mockup for {short}", None, None
    slug = re.sub(r"[^a-z0-9]+", "-", short.lower()).strip("-") or "site"
    result = create_gmail_draft(
        to_email=to, to_name=p.company, subject=subject, body_text=body_text,
        body_html=to_html_body(body_text, first_touch=False),
        thread_id=thread_id, in_reply_to=in_reply_to,
        attachments=[(f"{slug}-mockup.png", Path(png).read_bytes(), "image/png")],
    )
    log_event(conn, Event(prospect_id=prospect_id,
                          event_type="mockup_reply_pushed" if result.get("success") else "mockup_reply_error",
                          payload_json=json.dumps({k: v for k, v in result.items() if k != "raw"})))
    if not result.get("success"):
        return {"ok": False, "error": result.get("error")}
    return {"ok": True, "gmail_draft_id": result.get("draft_id"), "threaded": bool(thread)}
