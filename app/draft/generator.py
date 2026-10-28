"""Email draft generator (first touch + follow-ups).

The model writes ONLY the body. Subject, signature and the legal footer are
added in code. The model must report every quote and fact it used
(`used_quotes`, `used_fact_ids`) so the QA gate can verify each one.
"""
import html
import json
import re
from typing import Optional

from app._claude_client import call_claude, ClaudeCallError
from app.settings import (
    MODEL_DRAFT, SENDER_FIRST_NAME, DRAFT_MIN_WORDS, DRAFT_MAX_WORDS,
    FOLLOWUP_MIN_WORDS, FOLLOWUP_MAX_WORDS, BANNED_PHRASES,
)
from app.draft.voice import render_signature_html


def _banned_block() -> str:
    return ", ".join(f"\"{p}\"" for p in BANNED_PHRASES)


SYSTEM_PROMPT = f"""You write the first cold email from {SENDER_FIRST_NAME}, who runs a one-person web studio. It should read like {SENDER_FIRST_NAME} typed it after looking at the prospect's site on his phone: short, specific, plain, polite. It is not a report.

INPUT (JSON): business_name, short_name, industry, city, greeting_name, subject (already chosen; it names the primary finding), primary_finding, second_finding (optional), personal_detail (optional), demo_offer.
Each finding has evidence_kind:
- "quote": you may put the quote, or a shorter contiguous part of it, in straight double quotes "like this", copied character for character. Never fix its spelling, capitals, or punctuation.
- "fact": describe it in your own words using customer_moment. Never put it in quotation marks.
Use no other quotation marks of any kind, including single quotes for emphasis or paraphrase.

SHAPE: plain text, {DRAFT_MIN_WORDS}-{DRAFT_MAX_WORDS} words before the sign-off, three short paragraphs, no bullets, no links.
Hi {{greeting_name}},

Paragraph 1 (1-2 sentences): open with the primary finding from a customer's point of view, and say {SENDER_FIRST_NAME} looked at the site (for example "I pulled up {{short_name}}'s site on my phone and..."). You may use personal_detail in a few words; no praise.
Paragraph 2 (1-2 sentences): the fix in plain words; optionally the second finding in one clause. Say {SENDER_FIRST_NAME} does this kind of fix for local businesses. No service names, no list of offerings.
Paragraph 3 (one sentence): offer demo_offer free and ask one yes/no question answerable in one word, e.g. "Want me to send it over?" Ask nothing else.

Thanks,
{SENDER_FIRST_NAME}

RULES
- Don't claim impact you can't see: no "costing you customers/leads", no numbers, no rankings, no "studies show". "may make it harder to..." at most once.
- Don't call anything outdated, broken, ugly, old, unprofessional, or bad. Describe what happens.
- No compliments, flattery, urgency, guarantees, apologies for reaching out, "I hope", "just", or "quick idea".
- No em dashes, en dashes, double hyphens, exclamation marks, or emoji.
- Don't write PromptProofreading, {SENDER_FIRST_NAME}'s last name, any URL, email address, phone number, or P.S. The signature and legal footer are added later.
- The email must open with the thing the subject names.
- Banned phrases: {_banned_block()}
If the input includes previous_attempt_failed_checks, fix every listed check and change nothing else.

OUTPUT strict JSON only (no code fences):
{{"body_text":"Hi ...\\n\\n...\\n\\nThanks,\\n{SENDER_FIRST_NAME}","used_quotes":["..."],"used_fact_ids":["..."]}}
If primary_finding is missing, output {{"error":"no_evidence"}}."""


FOLLOWUP_SYSTEM_PROMPT = f"""You write a short follow-up email from {SENDER_FIRST_NAME} (one-person web studio) in the SAME thread as an earlier cold email that got no reply. It is a reply, so no subject line.

INPUT (JSON): touch (2 or 3), greeting_name, short_name, previous_body (what was already sent), second_finding (optional, touch 2 only), demo_offer.

Touch 2: {FOLLOWUP_MIN_WORDS}-{max(FOLLOWUP_MIN_WORDS + 10, FOLLOWUP_MAX_WORDS - 10)} words. Mention one new, concrete thing (the second_finding if given, described in plain words), or restate the free offer more specifically. End with the same kind of one-word yes/no question.
Touch 3: {FOLLOWUP_MIN_WORDS}-40 words. Polite close-out: say you won't keep emailing, and the offer stands if it's ever useful. End with a yes/no question.

Rules: plain text; "Hi {{greeting_name}}," first line; sign off "Thanks,\\n{SENDER_FIRST_NAME}". No links, URLs, phone numbers, em/en dashes, exclamation marks, emoji, guilt, urgency, or invented impact. Never repeat the first email's wording. Don't put anything in quotation marks unless it is copied exactly from second_finding.quote.
Banned phrases: {_banned_block()}
If the input includes previous_attempt_failed_checks, fix every listed check and change nothing else.

OUTPUT strict JSON only: {{"body_text":"...","used_quotes":[],"used_fact_ids":[]}}"""


def _strip_json_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```\s*$", "", s)
    return s.strip()


def _extract_json_object(s: str) -> str:
    s = _strip_json_fence(s)
    if s.startswith("{"):
        return s
    start, end = s.find("{"), s.rfind("}")
    return s[start:end + 1] if start != -1 and end > start else s


_SIGNOFF_LINE_RE = re.compile(
    r"^(thanks|thank you|best|cheers|regards|talk soon|sincerely)[,.]?$", re.IGNORECASE)


def _strip_signoff_paragraph(body_text: str) -> str:
    """Drop the trailing 'Thanks,\\n<First>' paragraph; the HTML signature replaces it."""
    paragraphs = [p for p in body_text.replace("\r\n", "\n").split("\n\n") if p.strip()]
    if not paragraphs:
        return body_text
    lines = [ln.strip() for ln in paragraphs[-1].strip().split("\n") if ln.strip()]
    if 1 <= len(lines) <= 3 and _SIGNOFF_LINE_RE.match(lines[0]) and all(len(ln) <= 40 for ln in lines):
        paragraphs.pop()
    return "\n\n".join(paragraphs)


def to_html_body(body_text: str, *, first_touch: bool = True) -> str:
    """Plain-text body -> HTML paragraphs + formal signature/footer."""
    safe = html.escape(_strip_signoff_paragraph(body_text))
    html_paragraphs = []
    for para in (p.strip() for p in safe.split("\n\n") if p.strip()):
        if "\n- " in para or para.startswith("- "):
            items, preamble = [], []
            for ln in para.split("\n"):
                (items if ln.startswith("- ") else preamble).append(ln)
            if preamble:
                html_paragraphs.append("<p>" + "<br>".join(preamble) + "</p>")
            if items:
                html_paragraphs.append("<ul>" + "".join(f"<li>{i[2:].strip()}</li>" for i in items) + "</ul>")
        else:
            html_paragraphs.append("<p>" + para.replace("\n", "<br>") + "</p>")
    return "\n".join(html_paragraphs) + "\n" + render_signature_html(first_touch=first_touch)


# Backwards-compatible private alias.
_to_html_body = to_html_body


def _call_and_parse(system: str, payload: dict) -> dict:
    try:
        raw = call_claude(system=system, user=json.dumps(payload, ensure_ascii=False, indent=1),
                          model=MODEL_DRAFT)
    except ClaudeCallError as e:
        return {"error": f"claude call failed: {e}", "error_kind": e.kind}
    except Exception as e:
        return {"error": f"claude call failed: {type(e).__name__}: {str(e)[:200]}",
                "error_kind": "other"}
    try:
        data = json.loads(_extract_json_object(raw))
    except json.JSONDecodeError as e:
        return {"error": f"json parse: {e}", "raw": raw[:1000]}
    if not isinstance(data, dict):
        return {"error": "draft response is not a JSON object", "raw": raw[:1000]}
    if data.get("error"):
        return {"error": str(data["error"])[:200]}
    body = str(data.get("body_text") or "").strip()
    if not body:
        return {"error": "empty body_text"}

    def _strs(v) -> list[str]:
        return [str(x).strip() for x in v if str(x).strip()][:6] if isinstance(v, list) else []

    return {"body_text": body, "used_quotes": _strs(data.get("used_quotes")),
            "used_fact_ids": _strs(data.get("used_fact_ids")), "model_id": MODEL_DRAFT}


def _finding_for_prompt(f: Optional[dict]) -> Optional[dict]:
    if not f:
        return None
    return {k: f.get(k, "") for k in ("type", "evidence_kind", "quote", "fact_id", "fact_detail",
                                       "error_text", "correction", "customer_moment", "fix")}


def generate_draft(*, company: str, short: str, industry: Optional[str], city: Optional[str],
                   greeting: str, subject: str, primary: dict, second: Optional[dict],
                   personal_detail: str = "", demo_offer: str = "",
                   feedback: Optional[list[str]] = None,
                   prior_body: Optional[str] = None) -> dict:
    """First-touch draft. Returns {subject, body_text, body_html, used_quotes,
    used_fact_ids} or {error}."""
    if not primary:
        return {"error": "no_evidence"}
    payload = {
        "business_name": company, "short_name": short, "industry": industry or "",
        "city": city or "", "greeting_name": greeting, "subject": subject,
        "primary_finding": _finding_for_prompt(primary),
        "second_finding": _finding_for_prompt(second),
        "personal_detail": personal_detail, "demo_offer": demo_offer,
    }
    if feedback:
        payload["previous_attempt_failed_checks"] = feedback
        payload["previous_body_text"] = prior_body or ""
    res = _call_and_parse(SYSTEM_PROMPT, payload)
    if "error" in res:
        return res
    return {**res, "subject": subject, "body_html": to_html_body(res["body_text"], first_touch=True)}


def generate_followup(*, touch: int, greeting: str, short: str, previous_body: str,
                      second: Optional[dict], demo_offer: str,
                      feedback: Optional[list[str]] = None,
                      prior_body: Optional[str] = None) -> dict:
    payload = {"touch": touch, "greeting_name": greeting, "short_name": short,
               "previous_body": previous_body,
               "second_finding": _finding_for_prompt(second) if touch == 2 else None,
               "demo_offer": demo_offer}
    if feedback:
        payload["previous_attempt_failed_checks"] = feedback
        payload["previous_body_text"] = prior_body or ""
    res = _call_and_parse(FOLLOWUP_SYSTEM_PROMPT, payload)
    if "error" in res:
        return res
    return {**res, "body_html": to_html_body(res["body_text"], first_touch=False)}
