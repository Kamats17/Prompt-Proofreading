"""End-to-end audit pipeline for one prospect.

  guards (blocked / aggregator / directory subdomain / DNC)
    -> fetch (desktop + iPhone render + contact/about pages)
    -> redirect check (final URL must stay on the business's own domain)
    -> extract features + identity check (hijacked / foreign / not-their-site)
    -> dead-site gate (thin pages get one slower retry)
    -> email + owner-name discovery (own domain or free-mail only)
    -> [no email -> park as 'no_email' WITHOUT spending a Claude call]
    -> facts -> Claude audit -> evidence validation -> deterministic score
    -> insert audits row -> log event.

Permanent problems stamp `blocked_reason` so bulk runs skip them. Transient
failures (fetch errors, Claude errors) increment `audit_attempts` instead, and
bulk selection backs off after repeated failures.
"""
import json
import re
import sqlite3
from pathlib import Path

from bs4 import BeautifulSoup

from app.audit.fetcher import fetch_page
from app.audit.extractor import (
    extract_features, compute_facts, extract_emails, choose_contact_email,
    dead_site_reason, domain_names_business, identity_mismatch_reason, page_text_for_quotes,
)
from app.audit.audit_agent import run_audit
from app.audit.grader import combine, score_lead
from app.db.models import Audit, Event
from app.db.repo import (
    get_prospect, insert_audit, log_event, set_prospect_email, mark_prospect_blocked,
    record_audit_failure, clear_audit_failure, set_contact_name_if_empty,
)
from app.draft.name_guess import looks_like_person_name
from app.safety.aggregator_filter import is_aggregator, directory_generated_reason
from app.safety.fabrication_guard import validate_findings
from app.safety.dnc import is_dnc
from app.sources._normalize import registered_domain
from app.textnorm import normalize_text

_OWNER_RE = re.compile(
    r"(?:owner|owned by|founded by|proprietor|chef/owner|owner/chef|owner and chef)"
    r"[\s:,\-]+(?:is\s+)?([A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{1,20})?)")


def _block(conn, pid: int, reason: str, event: str, detail: dict) -> dict:
    mark_prospect_blocked(conn, pid, reason)
    log_event(conn, Event(prospect_id=pid, event_type=event,
                          payload_json=json.dumps({"reason": reason, **detail})))
    return {"ok": False, "blocked": reason, "reason": detail.get("detail", reason)}


def _fail(conn, pid: int, error: str, event: str, detail: dict | None = None,
          error_kind: str = "other") -> dict:
    record_audit_failure(conn, pid, error)
    log_event(conn, Event(prospect_id=pid, event_type=event,
                          payload_json=json.dumps({"error": error, **(detail or {})})))
    return {"ok": False, "error": error, "error_kind": error_kind}


# Problems the code measured itself. The model sometimes leaves one out (it once
# skipped a non-tappable phone number and the lead scored C), so any of these
# facts no kept finding already cites is added as a finding by code.
# fact_id: (type, severity, service, customer_moment, fix)
_FACT_FINDINGS = {
    "cert_expired": ("no_https", 3, "website",
                     "A customer who opens the site gets a full-page browser security warning "
                     "before they see anything.",
                     "Renew the security certificate and set it to renew automatically."),
    "cert_invalid": ("no_https", 3, "website",
                     "A customer who opens the site gets a full-page browser security warning "
                     "before they see anything.",
                     "Install a valid security certificate and set it to renew automatically."),
    "phone_not_tappable": ("phone_not_tappable", 3, "website",
                           "On a phone, a customer can't tap the number to call; they have to "
                           "copy it by hand.",
                           "Make the phone number a tap-to-call link and add a call button near the top."),
    "desktop_layout_on_phone": ("no_mobile_layout", 2, "website",
                                "On a phone, a customer has to pinch and zoom to read the page.",
                                "Rebuild the layout so it fits a phone screen."),
    "pdf_menu": ("pdf_menu", 2, "website",
                 "On a phone, tapping the menu downloads a PDF instead of opening a page.",
                 "Put the menu on a mobile-friendly web page and keep the PDF for printing."),
    "no_https": ("no_https", 2, "website",
                 "The browser marks the site 'Not secure' when a customer opens it.",
                 "Add a security certificate so the site loads over https."),
}


def missing_fact_findings(kept: list[dict], facts: dict[str, str]) -> list[dict]:
    """Findings for measured facts no kept finding covers. Also retypes, in place,
    a kept 'other' finding that cites one of these facts, so the subject line and
    the draft prompt see what it really is."""
    for f in kept:
        if f.get("type") == "other" and f.get("fact_id") in _FACT_FINDINGS:
            f["type"] = _FACT_FINDINGS[f["fact_id"]][0]
    cited = {f.get("fact_id") for f in kept}
    cited_types = {f.get("type") for f in kept}
    out = []
    for fid, (ftype, sev, svc, moment, fix) in _FACT_FINDINGS.items():
        if fid not in facts or fid in cited or ftype in cited_types:
            continue
        cited_types.add(ftype)
        out.append({"type": ftype, "evidence_kind": "fact", "quote": "", "fact_id": fid,
                    "error_text": "", "correction": "", "customer_moment": moment, "fix": fix,
                    "service": svc, "severity": sev, "fact_detail": facts[fid],
                    "confidence": "verified", "why_hurts": moment, "added_by": "code"})
    return out


def _extra_texts(paths: list[str]) -> list[str]:
    out = []
    for p in paths:
        try:
            soup = BeautifulSoup(Path(p).read_text(encoding="utf-8", errors="replace"), "lxml")
            for t in soup(["script", "style", "noscript", "svg"]):
                t.decompose()
            out.append(normalize_text(soup.get_text(" ", strip=True)))
        except OSError:
            continue
    return out


def audit_prospect(conn: sqlite3.Connection, prospect_id: int, *,
                   force_fetch: bool = False, allow_no_email: bool = False) -> dict:
    p = get_prospect(conn, prospect_id)
    if not p:
        return {"ok": False, "error": "prospect not found"}
    if p.blocked_reason and not force_fetch:
        return {"ok": False, "blocked": p.blocked_reason, "reason": "already blocked"}

    agg, agg_reason = is_aggregator(p.url)
    if agg:
        return _block(conn, prospect_id, "aggregator", "audit_skipped_aggregator",
                      {"detail": agg_reason, "url": p.url})
    dir_reason = directory_generated_reason(p.url, p.company)
    if dir_reason:
        return _block(conn, prospect_id, "directory", "audit_skipped_directory",
                      {"detail": dir_reason, "url": p.url})
    dnc = is_dnc(conn, company=p.company, domain=p.url, email=p.email)
    if dnc:
        return _block(conn, prospect_id, "dnc", "audit_skipped_dnc", {"detail": dnc})

    fetched = fetch_page(p.url, force=force_fetch)
    if not fetched.get("ok") and "ERR_NAME_NOT_RESOLVED" in (fetched.get("error") or ""):
        # The domain no longer exists: no point retrying, and the business has
        # lost its website, which makes it a strong call-list prospect.
        return _block(conn, prospect_id, "domain_dead", "audit_skipped_domain_dead",
                      {"detail": f"{p.url} no longer resolves (website address is gone)"})
    if not fetched.get("ok"):
        return _fail(conn, prospect_id, fetched.get("error") or "fetch failed",
                     "audit_fetch_failed", {"fetched": fetched}, "fetch")

    final_url = fetched.get("final_url") or p.url
    agg2, why2 = is_aggregator(final_url)
    if registered_domain(final_url) != registered_domain(p.url) and (
            agg2 or not domain_names_business(final_url, p.company)):
        detail = f"{p.url} redirects to {final_url}" + (f" ({why2})" if agg2 else "")
        return _block(conn, prospect_id, "redirect_offsite", "audit_skipped_redirect",
                      {"detail": detail, "final_url": final_url})
    dnc2 = is_dnc(conn, domain=final_url)
    if dnc2:
        return _block(conn, prospect_id, "dnc", "audit_skipped_dnc", {"detail": dnc2})

    features = extract_features(fetched["html_path"], base_url=final_url)
    if not features.get("ok"):
        return _fail(conn, prospect_id, features.get("error", "extract failed"), "audit_extract_failed")

    mismatch = identity_mismatch_reason(features, p.company)
    if mismatch:
        return _block(conn, prospect_id, "not_their_site", "audit_skipped_identity",
                      {"detail": mismatch, "final_url": final_url})

    dead, permanent = dead_site_reason(features)
    if dead and not permanent:
        # Thin page: often JS-rendered or a bot check. One slower retry.
        fetched = fetch_page(p.url, force=True, wait_until="networkidle")
        if fetched.get("ok"):
            features = extract_features(fetched["html_path"], base_url=final_url)
            dead, permanent = dead_site_reason(features)
    if dead:
        if permanent:
            return _block(conn, prospect_id, "deadsite", "audit_skipped_deadsite",
                          {"detail": dead, "url": p.url})
        return _fail(conn, prospect_id, dead, "audit_thin_page")

    extra_paths = fetched.get("extra_pages") or []
    extra_texts = _extra_texts(extra_paths)

    # Owner name from contact/about pages (only when explicitly labelled).
    if not p.contact_name:
        for text in extra_texts + [features.get("body_text", "")]:
            m = _OWNER_RE.search(text)
            if m and looks_like_person_name(m.group(1)):
                set_contact_name_if_empty(conn, prospect_id, m.group(1))
                p.contact_name = m.group(1)
                break

    # Email discovery (never from pages that forbid address harvesting).
    if not p.email:
        if features.get("harvest_notice"):
            log_event(conn, Event(prospect_id=prospect_id, event_type="email_harvest_skipped",
                                  payload_json=json.dumps({"reason": "site forbids harvesting"})))
        else:
            candidates: list[str] = []
            for path in [fetched["html_path"], *extra_paths]:
                for e in extract_emails(path):
                    if e not in candidates:
                        candidates.append(e)
            exclude = features.get("credit_link_domains") or []
            chosen, why = choose_contact_email(candidates, final_url, exclude, company=p.company)
            log_event(conn, Event(prospect_id=prospect_id, event_type="email_harvest",
                                  payload_json=json.dumps({"chosen": chosen, "why": why,
                                                           "candidates": candidates})))
            if chosen:
                set_prospect_email(conn, prospect_id, chosen, why)
                p.email = chosen

    if not p.email and not allow_no_email:
        # Don't spend a Claude call on a lead we can't reach by email.
        return _block(conn, prospect_id, "no_email", "audit_parked_no_email",
                      {"detail": "no own-domain or free-mail address found; add one manually "
                                 "or use --allow-no-email", "url": p.url})

    facts = compute_facts(features, fetch_meta=fetched, industry=p.industry)
    business = {"name": p.company, "industry": p.industry, "city": p.city, "state": p.state,
                "url": p.url, "prior_note": (json.loads(p.node_issues_json)[0]
                                             if p.node_issues_json else "")}
    debug_dir = str(Path(fetched["html_path"]).parent)
    resp = run_audit(business=business, features=features, facts=facts,
                     fetch_meta=fetched, extra_texts=extra_texts, debug_dir=debug_dir)
    if "error" in resp:
        return _fail(conn, prospect_id, resp["error"], "audit_llm_error",
                     {"error_kind": resp.get("error_kind")}, resp.get("error_kind", "other"))

    quote_source = " ".join([page_text_for_quotes(fetched["html_path"]), *extra_texts])
    sources = {  # first match wins: describes where the email may say the quote came from
        "the page title": features.get("title", ""),
        "the description that shows up in search results": features.get("meta_description", ""),
        "the homepage": features.get("body_text", ""),
        "the contact/about page": " ".join(extra_texts),
    }
    kept, dropped = validate_findings(resp["findings"], quote_source_text=quote_source,
                                      facts=facts, sources=sources)
    kept += missing_fact_findings(kept, facts)

    # Order: the model's primary finding first (if it survived validation), then by severity.
    pf = resp.get("primary_finding_index", 0)
    primary_obj = resp["findings"][pf] if 0 <= pf < len(resp["findings"]) else {}

    def _is_primary(f: dict) -> bool:
        return bool(primary_obj) and f.get("type") == primary_obj.get("type") and (
            f.get("fact_id") == primary_obj.get("fact_id")
            or normalize_text(primary_obj.get("quote", "")) == f.get("quote"))

    kept.sort(key=lambda f: (0 if _is_primary(f) else 1, -int(f.get("severity", 1))))

    score = score_lead(findings=kept, meta=resp, features=features, industry=p.industry,
                       has_email=bool(p.email), has_phone=bool(p.phone or features.get("phones_in_text")),
                       contact_name=p.contact_name)
    final_grade = "Reject" if score["reject_reason"] else combine(p.node_rating, score["icp"])

    meta = {
        "ownership": resp["ownership"], "managed_by": resp["managed_by"],
        "activity_signals": resp["activity_signals"], "disqualifiers": resp["disqualifiers"],
        "personal_detail": resp["personal_detail"], "owner_first_name": resp["owner_first_name"],
        "demo_offer": resp["demo_offer"], "facts": facts, "score": score,
        "dropped_findings": dropped[:8], "final_url": final_url,
        "https_ok": fetched.get("https_ok"), "mobile": fetched.get("mobile") or {},
        "mobile_screenshot_path": fetched.get("mobile_screenshot_path"),
    }
    primary = kept[0] if kept else None
    audit = Audit(
        prospect_id=prospect_id,
        screenshot_path=fetched.get("screenshot_path"),
        html_cache_path=fetched["html_path"],
        issues_json=json.dumps(kept, ensure_ascii=False),
        meta_json=json.dumps(meta, ensure_ascii=False),
        service_angle=(f"{primary['service']}: {primary.get('fix', '')}" if primary else ""),
        demo_angle=resp["demo_offer"],
        icp_score=score["icp"],
        node_rating_snapshot=p.node_rating,
        final_grade=final_grade,
        model_id=resp["model_id"],
    )
    audit_id = insert_audit(conn, audit)
    clear_audit_failure(conn, prospect_id)
    log_event(conn, Event(prospect_id=prospect_id, event_type="audited", payload_json=json.dumps({
        "audit_id": audit_id, "icp_score": score["icp"], "site_need": score["site_need"],
        "buyer_fit": score["buyer_fit"], "final_grade": final_grade,
        "kept_findings": len(kept), "dropped_findings": len(dropped),
        "dropped_detail": dropped[:5], "facts": sorted(facts),
    })))
    return {"ok": True, "audit_id": audit_id, "final_grade": final_grade,
            "icp_score": score["icp"], "issues_kept": len(kept), "issues_dropped": len(dropped)}
