"""Claude audit of one prospect homepage -> structured, evidence-backed findings.

Every finding must cite exactly one kind of evidence:
  * "quote" — copied character-for-character from the page (verified in code by
              the fabrication guard against the cached HTML, case-sensitively), or
  * "fact"  — the id of a machine-checked observation computed in extractor.py
              (phone_not_tappable, pdf_menu, no_https, ...).
The model never scores the lead; grader.py does that deterministically.
"""
import datetime as _dt
import json
import re
from typing import Any

from app._claude_client import call_claude, ClaudeCallError
from app.settings import MODEL_AUDIT, SERVICES, SERVICE_KEYS, BRAND

FINDING_TYPES = (
    "phone_not_tappable", "pdf_menu", "no_https", "no_mobile_layout", "third_party_ordering",
    "no_booking_link", "no_order_link", "typo", "unclear_offer", "stale_content", "weak_cta",
    "missing_hours_or_address", "slow_mobile", "dead_links", "other",
)
OWNERSHIP = ("own_site", "directory_or_platform_page", "franchise", "unclear")


def _services_block() -> str:
    return "\n".join(f"- {s['key']}: {s['name']} - {s['blurb']}" for s in SERVICES)


def build_system_prompt(today: _dt.date | None = None) -> str:
    today = today or _dt.date.today()
    return f"""You audit one small-business homepage for {BRAND['company']}, a one-person web studio run by {BRAND['sender_first_name']}. Today is {today.isoformat()}. Your output feeds a short cold email that points out what a customer experiences on the prospect's own site. A wrong claim is far worse than a missing one.

SERVICES (use the key in `service`):
{_services_block()}

INPUT (JSON)
- business: name, industry, city, state, prior_note (may be stale; never cite it)
- page: final_url, https_ok, title, meta_description, h1s, h2s, body_excerpt (homepage text, may be truncated), ctas (label + href)
- facts: machine-checked observations keyed by id. Only these ids exist.
- extra_pages: short text from same-site contact/about pages, if fetched

You cannot see images, colors, or layout. Never comment on design, photos, or "look and feel".

FINDINGS
Return up to 4 findings a busy owner would care about because a CUSTOMER would notice: can a customer call, order, book, find hours/menu/prices, trust the page, read it on a phone. Skip SEO trivia (alt text, h1 count, meta length) unless nothing else exists, and never make it the primary finding.

Each finding uses exactly ONE evidence kind:
- "quote": copied character-for-character from body_excerpt, ctas[].label, title, meta_description, or extra_pages. 2 to 25 words, one contiguous span, punctuation, capitals and apostrophes exactly as given, no ellipses, no edits.
- "fact": the id of one entry in facts, copied exactly. Do not restate a fact as a quote.

Absence claims ("no online booking", "no ordering", "no contact form", "not mobile friendly", "no https") are allowed ONLY as a fact id. Never infer absence from text; it often lives on other pages or in widgets you can't see.

Typos: flag only an unambiguous error in ordinary English ("a order", "your welcome", "recieve"). Never flag brand names, dish names, non-English words, stylized capitals, regional spellings, or casual tone. Give error_text (the exact wrong words, contained in the quote) and correction.

Dates: call content stale ONLY by citing the fact id "stale_copyright". Current promotions or recent dates mean the owner maintains the site.

For each finding:
- customer_moment: one plain sentence about what a real customer experiences ("On a phone, the number at the top can't be tapped to call."). No claims about lost revenue, percentages, rankings, or studies.
- fix: one sentence, the concrete change {BRAND['sender_first_name']} would make.
- service: one of {", ".join(sorted(SERVICE_KEYS))}
- severity: 3 = blocks calling/ordering/booking or shows a security warning; 2 = confusing or looks unmaintained; 1 = polish.

LEAD CONTEXT
- ownership: "own_site" | "directory_or_platform_page" (auto-generated listing, "claim this business", template shared by many businesses) | "franchise" | "unclear"
- managed_by: platform or agency named in facts/footer, else ""
- activity_signals: short strings showing the business is active (current dates, new items, hiring)
- disqualifiers: e.g. "agency credit in footer", "restaurant platform site", "site is already modern", "no contact route"
- primary_finding_index: the finding the email opens with: highest severity, preferring something the owner can confirm on their own phone in 10 seconds
- personal_detail: one neutral fact from the page, copied exactly (e.g. "Family-owned since 1985"), or ""
- owner_first_name: only if the input explicitly says this person owns or runs the business, else ""
- demo_offer: one sentence describing a specific free thing {BRAND['sender_first_name']} could send (e.g. "a mockup of the homepage header with a tap-to-call button")

OUTPUT: strict JSON only, no prose, no code fences:
{{"ownership":"own_site","managed_by":"","activity_signals":[],"disqualifiers":[],
 "findings":[{{"type":"{'|'.join(FINDING_TYPES)}",
   "evidence_kind":"quote|fact","quote":"","fact_id":"","error_text":"","correction":"",
   "customer_moment":"","fix":"","service":"website","severity":2}}],
 "primary_finding_index":0,"personal_detail":"","owner_first_name":"","demo_offer":""}}
If nothing can be backed by a quote or a fact, return "findings": []."""


# Kept for callers/tests that import the constant.
SYSTEM_PROMPT = build_system_prompt()


def _str(v: Any, limit: int) -> str:
    """Coerce any JSON value to a bounded string (models sometimes emit numbers/null)."""
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        v = json.dumps(v, ensure_ascii=False)
    return str(v).strip()[:limit]


def build_user_payload(*, business: dict, features: dict, facts: dict[str, str],
                       fetch_meta: dict | None = None,
                       extra_texts: list[str] | None = None) -> str:
    fm = fetch_meta or {}
    payload = {
        "business": {
            "name": business.get("name") or "",
            "industry": business.get("industry") or "",
            "city": business.get("city") or "",
            "state": business.get("state") or "",
            "prior_note": business.get("prior_note") or "",
        },
        "page": {
            "final_url": fm.get("final_url") or business.get("url") or "",
            "https_ok": fm.get("https_ok"),
            "title": features.get("title", ""),
            "meta_description": features.get("meta_description", ""),
            "h1s": features.get("h1s", []),
            "h2s": features.get("h2_sample", []),
            "body_excerpt": features.get("body_excerpt", ""),
            "ctas": features.get("cta_links", []),
        },
        "facts": facts,
        "extra_pages": [t[:1500] for t in (extra_texts or [])][:3],
    }
    return json.dumps(payload, ensure_ascii=False, indent=1)


def _strip_json_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```\s*$", "", s)
    return s.strip()


def _extract_json_object(s: str) -> str:
    """Tolerate a chatty preamble by slicing the first top-level {...} block."""
    s = _strip_json_fence(s)
    if s.startswith("{"):
        return s
    start, end = s.find("{"), s.rfind("}")
    return s[start:end + 1] if start != -1 and end > start else s


def parse_audit_response(raw: str) -> dict:
    """Parse + sanitize model output. Returns {...} or {"error": ...}."""
    try:
        data = json.loads(_extract_json_object(raw))
    except json.JSONDecodeError as e:
        return {"error": f"json parse: {e}", "raw": raw[:1000]}
    if not isinstance(data, dict):
        return {"error": "audit response is not a JSON object", "raw": raw[:1000]}
    findings_in = data.get("findings")
    if not isinstance(findings_in, list):
        return {"error": "findings missing or not a list", "raw": raw[:1000]}

    findings = []
    for it in findings_in[:6]:
        if not isinstance(it, dict):
            continue
        ftype = _str(it.get("type"), 40).lower() or "other"
        if ftype not in FINDING_TYPES:
            ftype = "other"
        svc = _str(it.get("service"), 20).lower()
        if svc not in SERVICE_KEYS:
            svc = "website"
        kind = _str(it.get("evidence_kind"), 10).lower()
        try:
            sev = max(1, min(3, int(it.get("severity", 1))))
        except (TypeError, ValueError):
            sev = 1
        findings.append({
            "type": ftype,
            "evidence_kind": kind if kind in ("quote", "fact") else "",
            "quote": _str(it.get("quote"), 400),
            "fact_id": _str(it.get("fact_id"), 80),
            "error_text": _str(it.get("error_text"), 120),
            "correction": _str(it.get("correction"), 120),
            "customer_moment": _str(it.get("customer_moment"), 240),
            "fix": _str(it.get("fix"), 240),
            "service": svc,
            "severity": sev,
        })

    ownership = _str(data.get("ownership"), 40)
    if ownership not in OWNERSHIP:
        ownership = "unclear"
    try:
        primary = int(data.get("primary_finding_index", 0))
    except (TypeError, ValueError):
        primary = 0

    def _list(v) -> list[str]:
        return [_str(x, 120) for x in v][:6] if isinstance(v, list) else []

    return {
        "findings": findings,
        "ownership": ownership,
        "managed_by": _str(data.get("managed_by"), 80),
        "activity_signals": _list(data.get("activity_signals")),
        "disqualifiers": _list(data.get("disqualifiers")),
        "primary_finding_index": primary,
        "personal_detail": _str(data.get("personal_detail"), 160),
        "owner_first_name": _str(data.get("owner_first_name"), 40),
        "demo_offer": _str(data.get("demo_offer"), 240),
        "model_id": MODEL_AUDIT,
    }


def run_audit(*, business: dict, features: dict, facts: dict[str, str],
              fetch_meta: dict | None = None, extra_texts: list[str] | None = None,
              debug_dir: str | None = None) -> dict:
    """Call Claude and parse. Returns parsed dict or {"error", "error_kind"}."""
    system = build_system_prompt()
    user_text = build_user_payload(business=business, features=features, facts=facts,
                                   fetch_meta=fetch_meta, extra_texts=extra_texts)
    try:
        raw = call_claude(system=system, user=user_text, model=MODEL_AUDIT)
    except ClaudeCallError as e:
        return {"error": f"claude call failed: {e}", "error_kind": e.kind}
    except Exception as e:
        return {"error": f"claude call failed: {type(e).__name__}: {str(e)[:200]}",
                "error_kind": "other"}

    if debug_dir:
        from pathlib import Path
        d = Path(debug_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / "audit_prompt.txt").write_text("=== SYSTEM ===\n" + system + "\n\n=== USER ===\n"
                                            + user_text, encoding="utf-8")
        (d / "audit_response.json").write_text(raw, encoding="utf-8")

    return parse_audit_response(raw)
