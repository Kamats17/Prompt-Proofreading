"""`pp-agent sync` — reconcile pushed drafts with what actually happened in Gmail.

Uses the READ-ONLY token (gmail.readonly). For each draft we pushed:
  * the draft became a SENT message        -> status 'sent', sent_at, Message-ID
                                              stored (follow-ups reply to it)
  * a bounce (mailer-daemon/postmaster)    -> 'bounced', prospect blocked
  * an out-of-office / auto-reply          -> ignored (logged)
  * an opt-out ("no", "unsubscribe", ...)  -> 'opted_out', email + domain added to
                                              DNC, prospect blocked. This is how the
                                              CAN-SPAM 10-business-day opt-out rule
                                              stops depending on someone's memory.
  * any other reply                        -> 'replied' (a human takes it from here;
                                              no more follow-ups)
"""
import datetime as _dt
import json
import re
import sqlite3
from email.utils import parseaddr

from app.db.models import DncEntry
from app.db.repo import (
    transition_draft, add_dnc, mark_prospect_blocked, stamp_prospect_contacted, get_prospect,
)
from app.events.logger import log
from app.settings import SENDER_EMAIL, GMAIL_USER_ID
from app.safety.dnc import is_dnc
from app.sources._normalize import domain_of

OPT_OUT_RE = re.compile(
    r"\b(unsubscribe|remove me|take me off|stop (?:emailing|contacting|sending)|"
    r"do not (?:email|contact)|don'?t (?:email|contact)|not interested|no,? thanks?|"
    r"no thank you|please stop|opt(?:-| )?out)\b", re.IGNORECASE)
BARE_NO_RE = re.compile(r"^\s*(no|nope|no\.|no!|pass)\s*$", re.IGNORECASE)
AUTO_REPLY_SUBJECT_RE = re.compile(r"(out of (the )?office|automatic reply|auto-?reply|away from)",
                                   re.IGNORECASE)
BOUNCE_FROM_RE = re.compile(r"(mailer-daemon|postmaster|mail delivery)", re.IGNORECASE)
BOUNCE_SUBJECT_RE = re.compile(r"(undeliverable|delivery status notification|failure notice|"
                               r"returned mail|address not found)", re.IGNORECASE)


def _headers(msg: dict) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in (msg.get("payload") or {}).get("headers", [])}


def classify_reply(*, from_addr: str, subject: str, snippet: str, auto_submitted: str = "") -> str:
    """'bounce' | 'auto' | 'opt_out' | 'reply'. Pure function (unit-tested)."""
    if BOUNCE_FROM_RE.search(from_addr) or BOUNCE_SUBJECT_RE.search(subject):
        return "bounce"
    if (auto_submitted and auto_submitted.lower() != "no") or AUTO_REPLY_SUBJECT_RE.search(subject):
        return "auto"
    first_line = (snippet or "").strip().split("\n")[0][:200]
    first_sentence = re.split(r"[.!\n]", first_line, maxsplit=1)[0]
    # Our email asks them to reply "no" to opt out, so a bare "No." counts.
    # "No problem, let's talk" does not.
    if OPT_OUT_RE.search(first_line) or BARE_NO_RE.match(first_sentence):
        return "opt_out"
    return "reply"


def _ts(msg: dict) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, tz=_dt.timezone.utc)


def sync_gmail(conn: sqlite3.Connection, *, service=None) -> dict:
    """Walk every draft in pushed/sent state and reconcile with Gmail threads."""
    if service is None:
        from app.auth.gmail_oauth import build_gmail_service
        service = build_gmail_service(read=True, interactive=False)
    me = {SENDER_EMAIL.lower()}
    try:
        profile = service.users().getProfile(userId=GMAIL_USER_ID).execute()
        me.add((profile.get("emailAddress") or "").lower())
    except Exception:
        pass

    stats = {"checked": 0, "sent": 0, "replied": 0, "opted_out": 0, "bounced": 0, "auto": 0, "errors": 0}
    rows = conn.execute(
        "SELECT * FROM drafts WHERE status IN ('pushed', 'sent') AND gmail_thread_id IS NOT NULL "
        "ORDER BY id").fetchall()
    for d in rows:
        stats["checked"] += 1
        try:
            thread = service.users().threads().get(
                userId=GMAIL_USER_ID, id=d["gmail_thread_id"], format="full").execute()
        except Exception as e:
            stats["errors"] += 1
            log(conn, "sync_error", prospect_id=d["prospect_id"], draft_id=d["id"],
                payload={"error": str(e)[:200]})
            continue
        msgs = sorted(thread.get("messages", []), key=_ts)

        ours = [m for m in msgs if "SENT" in (m.get("labelIds") or [])
                and parseaddr(_headers(m).get("from", ""))[1].lower() in me]
        if d["status"] == "pushed" and ours:
            sent_msg = ours[-1]
            if transition_draft(conn, d["id"], ("pushed",), "sent", sent_at="now",
                                rfc_message_id=_headers(sent_msg).get("message-id")):
                stamp_prospect_contacted(conn, d["prospect_id"])
                stats["sent"] += 1
                log(conn, "sync_sent", prospect_id=d["prospect_id"], draft_id=d["id"])
        if not ours:
            continue
        last_ours = _ts(ours[-1])
        theirs = [m for m in msgs if _ts(m) > last_ours
                  and parseaddr(_headers(m).get("from", ""))[1].lower() not in me]
        for m in theirs:
            h = _headers(m)
            kind = classify_reply(from_addr=h.get("from", ""), subject=h.get("subject", ""),
                                  snippet=m.get("snippet", ""), auto_submitted=h.get("auto-submitted", ""))
            if kind == "auto":
                stats["auto"] += 1
                log(conn, "sync_auto_reply", prospect_id=d["prospect_id"], draft_id=d["id"])
                continue
            p = get_prospect(conn, d["prospect_id"])
            if kind == "bounce":
                transition_draft(conn, d["id"], ("pushed", "sent"), "bounced")
                mark_prospect_blocked(conn, p.id, "bounced")
                stats["bounced"] += 1
                log(conn, "sync_bounced", prospect_id=p.id, draft_id=d["id"],
                    payload={"email": p.email})
            elif kind == "opt_out":
                transition_draft(conn, d["id"], ("pushed", "sent"), "opted_out", replied_at="now")
                if not is_dnc(conn, email=p.email, domain=p.url):
                    add_dnc(conn, DncEntry(company=p.company, domain=domain_of(p.url), email=p.email,
                                           reason="opted out (auto-detected reply)", added_by="sync"))
                mark_prospect_blocked(conn, p.id, "opted_out")
                stats["opted_out"] += 1
                log(conn, "sync_opted_out", prospect_id=p.id, draft_id=d["id"],
                    payload={"snippet": m.get("snippet", "")[:200]})
            else:
                transition_draft(conn, d["id"], ("pushed", "sent"), "replied", replied_at="now")
                stats["replied"] += 1
                log(conn, "sync_replied", prospect_id=p.id, draft_id=d["id"],
                    payload={"snippet": m.get("snippet", "")[:200]})
            break  # first real response decides the thread
    return stats
