"""Brand voice + compliance helpers for the draft generator and QA gate.

Config values live in config.json (via settings); this module gives them names,
regex helpers, subject selection, and the signature/footer renderers.
"""
import html
import re

from app.settings import (
    BANNED_PHRASES, FALLBACK_NAME, SUBJECT_BY_FINDING,
    SENDER_NAME, SENDER_FIRST_NAME, SENDER_TITLE, COMPANY, SENDER_EMAIL, SITE_URL, SERVICES,
    SENDER_POSTAL_ADDRESS, SENDER_IDENTITY_LINE, OPT_OUT_LINE, REQUIRE_POSTAL_ADDRESS,
)


def _compile_banned_pattern(phrase: str) -> re.Pattern:
    """Word-boundary regex for single words, whitespace-tolerant for phrases."""
    p = phrase.strip().lower()
    if " " in p:
        return re.compile(r"\b" + r"\s+".join(re.escape(w) for w in p.split()) + r"\b",
                          re.IGNORECASE)
    return re.compile(r"\b" + re.escape(p) + r"\b", re.IGNORECASE)


_BANNED_PATTERNS = [(_compile_banned_pattern(p), p) for p in BANNED_PHRASES]

EM_DASH_PATTERNS = [
    re.compile(r"—"),         # U+2014
    re.compile(r"–"),         # U+2013 en dash, also forbidden
    re.compile(r"\s--\s"),    # ASCII double-hyphen used as em dash
]

# Sender name / brand must NOT appear in body_text (the signature adds them).
NAME_IN_BODY_PATTERNS = [
    re.compile(re.escape(SENDER_NAME), re.IGNORECASE),
    re.compile(r"PromptProofreading(\.com)?", re.IGNORECASE),
]
URL_PATTERN = re.compile(r"(https?://\S+|www\.\S+|\b[a-z0-9-]+\.(?:com|net|org|io|co|us)\b)",
                         re.IGNORECASE)

# Every way a model (or a human editing) can present text as a quotation:
# "..."  “...”  ‘...’  «...»  „...“  and '...' when used as quote marks (not
# apostrophes: the opening ' must not follow a letter, the closing ' must not
# precede one, so "Bob's" and "it's" are never mistaken for quotes).
_QUOTED_SPAN_RE = re.compile(
    r'"([^"\n]+)"'
    r"|“([^”\n]+)”"
    r"|„([^“”\n]+)[“”]"
    r"|‘([^’\n]+)’"
    r"|«([^»\n]+)»"
    r"|(?<![A-Za-z0-9])'([^'\n]{2,}?)'(?![A-Za-z0-9])"
)


def quoted_spans(body_text: str) -> list[str]:
    return [next(g for g in m if g) for m in _QUOTED_SPAN_RE.findall(body_text or "")]


def mask_quoted_spans(body_text: str) -> str:
    return _QUOTED_SPAN_RE.sub(" [QUOTE] ", body_text or "")


def banned_phrase_hits(body_text: str) -> list[str]:
    return [original for pat, original in _BANNED_PATTERNS if pat.search(body_text or "")]


def em_dash_hits(body_text: str) -> bool:
    return any(p.search(body_text or "") for p in EM_DASH_PATTERNS)


def name_in_body_hits(body_text: str) -> list[str]:
    return [p.pattern for p in NAME_IN_BODY_PATTERNS if p.search(body_text or "")]


def url_hits(body_text: str) -> list[str]:
    return URL_PATTERN.findall(body_text or "")


# ---- names & subjects ------------------------------------------------------------

_SHORT_CUT_WORDS = re.compile(
    r"\b(restaurant|restaurants|family restaurant|bar\s*&\s*grill|&\s*bar|and bar|grill|"
    r"cafe|café|diner|llc|inc|co\.?|company|ltd|pllc|dds|pc)\b.*$", re.IGNORECASE)
_TRAILING_CONNECTORS = {"&", "and", "of", "the", "at", "on", "in", "for", "-", "+", "/"}


def short_name(company: str) -> str:
    """Natural short form for subjects/bodies: 'Joe's Restaurant & Bar' ->
    "Joe's"; 'Riverside Family Restaurant' -> 'Riverside'; max 3 words.
    Falls back to the full name when trimming would leave nothing useful."""
    name = re.sub(r"\s+", " ", (company or "").strip())
    cut = _SHORT_CUT_WORDS.sub("", name).strip(" ,&-")
    cut = re.sub(r"\b(the|family)\s*$", "", cut, flags=re.IGNORECASE).strip(" ,&-")
    cut = re.split(r"\s*(?::|\||\s-\s)\s*", cut)[0].strip(" ,&-")  # 'Hench Law Office: Atherton T'
    words = cut.split()
    if not words or len(cut) < 3:
        words = name.split()
    # Prefer a natural break ('Harry M. Von Sas & Son' -> 'Harry M. Von Sas') and keep
    # a four-word name whole ('North Penn Real Estate'), so a cap never leaves a stub
    # like 'Harry M. Von' or 'North Penn Real'.
    for i, w in enumerate(words):
        if i >= 2 and w.lower() in ("&", "and", "+"):
            words = words[:i]
            break
    if len(words) > 4:
        words = words[:3]
    # Never end on a connector: 'CORTEX Chiropractic &', 'Buttonwood Motel and'.
    while len(words) > 1 and words[-1].lower().strip(",") in _TRAILING_CONNECTORS:
        words.pop()
    return " ".join(words)


def possessive(name: str) -> str:
    """Jethro's -> Jethro's (already possessive); Bob -> Bob's; Collins -> Collins'."""
    n = (name or "").strip()
    if n.lower().endswith(("'s", "’s")):
        return n
    return n + ("'" if n.lower().endswith("s") else "'s")


def subject_templates(finding_type: str | None) -> list[str]:
    return SUBJECT_BY_FINDING.get(finding_type or "", []) or SUBJECT_BY_FINDING["default"]


def pick_subject(company: str, finding_type: str | None, seed: int = 0) -> str:
    """Subject names the primary finding (so the email opens with what the subject
    promises). Deterministic rotation by seed among that finding's templates."""
    templates = subject_templates(finding_type)
    return templates[seed % len(templates)].format(short=short_name(company))


def allowed_subjects(company: str) -> list[str]:
    short = short_name(company)
    out: list[str] = []
    for templates in SUBJECT_BY_FINDING.values():
        for t in templates:
            s = t.format(short=short)
            if s not in out:
                out.append(s)
    return out


def subject_allowed(subject: str, company: str) -> bool:
    return (subject or "").strip() in allowed_subjects(company)


def greeting_name(name: str | None) -> str:
    return (name or "").strip() or FALLBACK_NAME


# ---- signature & compliance footer ------------------------------------------------

def _is_real_value(value: str) -> bool:
    """False for empty config values and unfilled placeholders like "[VERIFY ...]"."""
    v = (value or "").strip()
    return bool(v) and not v.startswith("[")


def footer_lines() -> list[str]:
    """CAN-SPAM elements: identify the message as a solicitation, explain how to
    opt out, and (when configured) the sender's postal address."""
    lines = []
    if _is_real_value(SENDER_POSTAL_ADDRESS):
        lines.append(SENDER_POSTAL_ADDRESS)
    tail = " ".join(x for x in (SENDER_IDENTITY_LINE, OPT_OUT_LINE) if _is_real_value(x))
    if tail:
        lines.append(tail)
    return lines


def compliance_warnings() -> list[str]:
    """Human-readable gaps, shown in status/doctor/dashboard."""
    out = []
    if not _is_real_value(SENDER_POSTAL_ADDRESS):
        out.append("No postal address in config (compliance.sender_postal_address). US CAN-SPAM "
                   "requires a valid physical address (a USPS PO box or a commercial mailbox "
                   "qualifies) in every commercial email.")
    if not _is_real_value(OPT_OUT_LINE):
        out.append("No opt-out line configured (compliance.opt_out_line).")
    if not _is_real_value(SENDER_IDENTITY_LINE):
        out.append("No solicitation identity line configured (compliance.sender_identity_line).")
    return out


def compliance_blockers() -> list[str]:
    """Gaps that block pushing to Gmail. The postal address only blocks when
    compliance.require_postal_address is true (owner's choice)."""
    out = []
    if not _is_real_value(OPT_OUT_LINE):
        out.append("BLOCKER: opt-out line missing from config")
    if not _is_real_value(SENDER_IDENTITY_LINE):
        out.append("BLOCKER: solicitation identity line missing from config")
    if REQUIRE_POSTAL_ADDRESS and not _is_real_value(SENDER_POSTAL_ADDRESS):
        out.append("BLOCKER: postal address required by config but not set")
    return out


def render_signature_html(*, first_touch: bool = True) -> str:
    """HTML signature + footer. First touch carries no website link (one fewer link
    in a cold email is better for deliverability); follow-ups add it."""
    if SENDER_TITLE:
        company = (html.escape(COMPANY) if first_touch
                   else f"<a href=\"{SITE_URL}\">{html.escape(COMPANY)}</a>")
        parts = [f"<p>Thanks,<br>{html.escape(SENDER_NAME)}<br>"
                 f"{html.escape(SENDER_TITLE)}, {company}<br>"]
    else:
        parts = [f"<p>Thanks,<br>{html.escape(SENDER_FIRST_NAME)}<br>"]
        if not first_touch:
            parts.append(f"<a href=\"{SITE_URL}\">{html.escape(SENDER_NAME)} • {html.escape(COMPANY)}</a><br>")
    parts.append(f"<a href=\"mailto:{SENDER_EMAIL}\">{SENDER_EMAIL}</a></p>")
    lines = footer_lines()
    if lines:
        parts.append('<p style="font-size:12px;color:#555;">'
                     + "<br>".join(html.escape(x) for x in lines) + "</p>")
    return "".join(parts)


def render_footer_text() -> str:
    """Plain-text counterpart appended to the text/plain MIME part."""
    lines = footer_lines()
    who = f"{SENDER_NAME}\n{SENDER_TITLE}, {COMPANY}\n" if SENDER_TITLE else ""
    return ("\n\n--\n" + who + SENDER_EMAIL + "\n" + "\n".join(lines)) if lines else ""


def services_summary() -> str:
    return ", ".join(s["name"] for s in SERVICES)
