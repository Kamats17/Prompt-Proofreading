"""QA gate — validation before any draft can be pushed to Gmail.

Returns {"pass": bool, "blockers": [...], "warnings": [...], "checks": {...}}.

Two classes of check:
  * HARD (always blockers): verbatim quotes, verified evidence, DNC, recontact,
    blocked prospect, grade, sender name/brand in body, template braces,
    compliance footer config.
  * STYLE (blockers for model output, warnings once a human has edited the draft):
    approved subject, word count, banned phrases, dashes, exclamation marks, no
    links in body, ends with a question. You can trim a draft to 50 words or tweak
    the subject without the Approve button locking up.
"""
import json
import re
import sqlite3

from app.settings import (
    DRAFT_MIN_WORDS, DRAFT_MAX_WORDS, FOLLOWUP_MIN_WORDS, FOLLOWUP_MAX_WORDS,
    RECONTACT_WINDOW_DAYS,
)
from app.draft.voice import (
    subject_allowed, allowed_subjects, banned_phrase_hits, em_dash_hits,
    name_in_body_hits, url_hits, mask_quoted_spans, quoted_spans, compliance_blockers,
)
from app.safety.dnc import is_dnc
from app.db.repo import recently_contacted
from app.sources._normalize import domain_of, normalize_email
from app.textnorm import normalize_text

_TEMPLATE_BRACE_RE = re.compile(r"\{[a-z_]+\}")
_SIGNOFF_RE = re.compile(r"\n\s*(thanks|thank you|best|cheers|regards)[,.]?\s*\n", re.IGNORECASE)


def _verified(issues: list[dict]) -> tuple[list[str], set[str]]:
    quotes = [normalize_text(i.get("quote", "")) for i in issues
              if (i.get("confidence") or "").lower() == "verified" and (i.get("quote") or "").strip()]
    facts = {i.get("fact_id") for i in issues
             if (i.get("confidence") or "").lower() == "verified" and i.get("fact_id")}
    return quotes, facts


def _span_is_verbatim(span: str, verified_quotes: list[str]) -> bool:
    """Case-sensitive containment after visual-only normalization. A shorter
    contiguous piece of a verified quote is fine; any edit is not."""
    s = normalize_text(span)
    return bool(s) and any(s in vq for vq in verified_quotes)


def _body_before_signoff(body: str) -> str:
    m = _SIGNOFF_RE.search("\n" + body + "\n")
    return body[:max(0, m.start() - 1)] if m else body


def run_qa(conn: sqlite3.Connection, *,
           company: str,
           prospect_email: str | None,
           prospect_url: str | None,
           subject: str,
           body_text: str,
           audit_issues_json: str,
           final_grade: str | None,
           prospect_id: int | None = None,
           draft_id: int | None = None,
           used_quotes: list[str] | None = None,
           used_fact_ids: list[str] | None = None,
           touch: int = 1,
           human_edited: bool = False,
           blocked_reason: str | None = None) -> dict:
    blockers: list[str] = []
    warnings: list[str] = []
    checks: dict[str, bool] = {}

    def check(name: str, ok: bool, message: str, *, style: bool = False) -> None:
        checks[name] = ok
        if ok:
            return
        if style and human_edited:
            warnings.append(f"WARNING: {message}")
        else:
            blockers.append(f"BLOCKER: {message}")

    masked = mask_quoted_spans(body_text)
    try:
        issues = json.loads(audit_issues_json or "[]")
    except json.JSONDecodeError:
        issues = []
    verified_quotes, verified_facts = _verified(issues)

    # ---- style checks ----
    if touch == 1:
        check("subject_allowed", subject_allowed(subject, company),
              f"subject {subject!r} is not one of the approved subjects", style=True)
    lo, hi = (DRAFT_MIN_WORDS, DRAFT_MAX_WORDS) if touch == 1 else (FOLLOWUP_MIN_WORDS, FOLLOWUP_MAX_WORDS)
    n_words = len(_body_before_signoff(body_text).split())
    check("word_count_ok", lo <= n_words <= hi, f"body is {n_words} words (target {lo}-{hi})", style=True)
    check("no_em_dash", not em_dash_hits(masked), "em/en dash or ' -- ' outside quotes", style=True)
    hits = banned_phrase_hits(masked)
    check("no_banned_phrases", not hits, f"banned phrase(s) outside quotes: {hits}", style=True)
    check("no_exclamation", "!" not in masked, "exclamation mark outside quotes", style=True)
    urls = url_hits(masked)
    check("no_links_in_body", not urls, f"link/URL in body: {urls[:3]}", style=True)
    check("ends_with_question", "?" in _body_before_signoff(body_text).strip()[-160:],
          "the email should end with one yes/no question", style=True)

    # ---- hard checks ----
    spans = quoted_spans(body_text)
    bad_spans = [s for s in spans if not _span_is_verbatim(s, verified_quotes)]
    check("quoted_spans_verbatim", not bad_spans,
          f"text in quotation marks is not copied exactly from the site: {bad_spans}")
    if used_quotes:
        missing = [q for q in used_quotes if not _span_is_verbatim(q, verified_quotes)
                   or normalize_text(q) not in normalize_text(body_text)]
        check("used_quotes_verified", not missing, f"used_quotes not verified/present: {missing}")
    if used_fact_ids:
        unknown = [f for f in used_fact_ids if f not in verified_facts]
        check("used_facts_verified", not unknown, f"used_fact_ids not verified for this page: {unknown}")
    if touch == 1:
        has_evidence = bool(spans and not bad_spans) or bool(used_fact_ids and set(used_fact_ids) <= verified_facts)
        check("has_verified_evidence", has_evidence,
              "body cites no verified quote or fact from the audit")

    check("no_sender_name_in_body", not name_in_body_hits(body_text),
          "sender name/brand in body (the signature adds it)")
    check("no_template_braces", not _TEMPLATE_BRACE_RE.search(body_text),
          "unresolved {placeholder} in body")

    if prospect_email is not None and prospect_email != "":
        check("email_valid", normalize_email(prospect_email) is not None,
              f"recipient address {prospect_email!r} is not a valid single email")
    else:
        warnings.append("WARNING: no recipient email yet (cannot be pushed to Gmail)")

    dnc_reason = is_dnc(conn, company=company, domain=prospect_url, email=prospect_email)
    check("not_on_dnc", dnc_reason is None, f"prospect on DNC list: {dnc_reason}")
    check("prospect_not_blocked", not blocked_reason, f"prospect is blocked: {blocked_reason}")

    if touch == 1:
        recon = recently_contacted(
            conn, email=prospect_email, domain=domain_of(prospect_url or ""),
            days=RECONTACT_WINDOW_DAYS, prospect_id=prospect_id, exclude_draft_id=draft_id,
        )
        check("not_recently_contacted", recon is None, f"{recon} (window {RECONTACT_WINDOW_DAYS}d)")
        check("grade_drafable", final_grade in {"A", "B"}, f"grade must be A or B; got {final_grade!r}")

    for b in compliance_blockers():
        blockers.append(b)
    checks["compliance_config_ok"] = not compliance_blockers()

    return {"pass": not blockers, "blockers": blockers, "warnings": warnings, "checks": checks,
            "allowed_subjects": allowed_subjects(company) if touch == 1 else []}
