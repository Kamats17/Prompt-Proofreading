"""Gmail Drafts API wrapper — the ONLY module that writes to Gmail.

It exposes exactly one write operation: create a draft (optionally as a reply in
an existing thread, for follow-ups). It never sends. The gmail.compose scope
technically permits sending, so the guarantee lives here and in
tests/test_no_gmail_send.py, which fails the build if any send call appears.
"""
import base64
import re
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid
from typing import Optional

from app.settings import GMAIL_USER_ID, SENDER_NAME, SENDER_EMAIL
from app.draft.voice import render_footer_text
from app.sources._normalize import normalize_email


def _clean_header(s: str | None) -> str:
    """Strip CR/LF so a dirty spreadsheet cell can't inject extra headers."""
    return re.sub(r"[\r\n]+", " ", s or "").strip()


Attachment = tuple[str, bytes, str]   # (filename, data, mime type)


def _build_mime(to_email: str, to_name: Optional[str], subject: str, body_text: str,
                body_html: str, *, in_reply_to: Optional[str] = None,
                attachments: Optional[list[Attachment]] = None) -> str:
    """multipart/alternative MIME (wrapped in multipart/mixed when there are
    attachments) -> base64url raw. The plain-text part gets the same compliance
    footer the HTML signature carries."""
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(body_text + render_footer_text(), "plain", "utf-8"))
    alt.attach(MIMEText(body_html, "html", "utf-8"))
    if attachments:
        msg = MIMEMultipart("mixed")
        msg.attach(alt)
        for filename, data, mime in attachments:
            maintype, _, subtype = (mime or "application/octet-stream").partition("/")
            part = MIMEBase(maintype, subtype or "octet-stream")
            part.set_payload(data)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment",
                            filename=_clean_header(filename) or "attachment")
            msg.attach(part)
    else:
        msg = alt
    msg["Subject"] = _clean_header(subject)
    msg["To"] = formataddr((_clean_header(to_name), to_email)) if to_name else to_email
    msg["From"] = formataddr((SENDER_NAME, SENDER_EMAIL))
    msg["Message-ID"] = make_msgid(domain=SENDER_EMAIL.split("@", 1)[-1])
    if in_reply_to:
        ref = _clean_header(in_reply_to)
        msg["In-Reply-To"] = ref
        msg["References"] = ref
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def create_gmail_draft(*, to_email: str, to_name: Optional[str], subject: str,
                       body_text: str, body_html: str,
                       thread_id: Optional[str] = None,
                       in_reply_to: Optional[str] = None,
                       attachments: Optional[list[Attachment]] = None) -> dict:
    """Create a Gmail draft. Never sends. Never raises: returns
    {success, draft_id, thread_id, error, error_kind}."""
    to = normalize_email(to_email)
    if not to:
        return {"success": False, "error": f"invalid recipient {to_email!r}", "error_kind": "recipient"}
    try:
        from app.auth.gmail_oauth import build_gmail_service, GmailAuthError
    except ImportError as e:
        return {"success": False, "error": f"google libs missing: {e}", "error_kind": "setup"}
    try:
        raw = _build_mime(to, to_name, subject, body_text, body_html, in_reply_to=in_reply_to,
                          attachments=attachments)
        service = build_gmail_service(interactive=False)
        message: dict = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id
        result = (service.users().drafts()
                  .create(userId=GMAIL_USER_ID, body={"message": message}).execute())
        return {"success": True, "draft_id": result.get("id"),
                "thread_id": (result.get("message") or {}).get("threadId")}
    except GmailAuthError as e:
        return {"success": False, "error": str(e), "error_kind": "auth"}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:300]}", "error_kind": "other"}
