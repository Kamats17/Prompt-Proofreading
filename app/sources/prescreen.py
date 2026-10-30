"""Cheap pre-check of a candidate website before it costs a Claude audit.

One plain HTTP fetch (no browser, no Claude): is the site alive, does it show an
email we may use, and does it look old? Only leads that pass reach the audit
queue. The audit re-checks everything with a real browser, so nothing here is
ever quoted in an email; these signals only decide what is worth auditing.
"""
import re
import warnings
from pathlib import Path
from urllib.parse import urlparse

import requests
from urllib3.exceptions import InsecureRequestWarning

from app.audit.extractor import (
    choose_contact_email, dead_site_reason, domain_names_business, extract_emails,
    extract_features, identity_mismatch_reason, today_year,
)
from app.settings import CACHE_DIR as _CACHE_ROOT
from app.sources._normalize import registered_domain

CACHE_DIR = _CACHE_ROOT / "prescreen"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
TIMEOUT_S = 15
MAX_CONTACT_PAGES = 2

_LEGACY_TAG_RE = re.compile(r"<(font|center|marquee|frameset|blink)\b|\bbgcolor\s*=|\.swf\b", re.I)
_TABLE_WIDTH_RE = re.compile(r"<table[^>]*\bwidth\s*=", re.I)
_OLD_JQUERY_RE = re.compile(r"jquery[.-]?1\.\d+(\.\d+)?(\.min)?\.js|jquery/1\.\d", re.I)
# Hosted builders write their own markup (Weebly's editor emits <font> tags and
# ships jQuery 1.8), so old-looking code there says nothing about the owner.
_HOSTED_BUILDERS = {"wix", "squarespace", "weebly", "duda", "godaddy_builder", "shopify",
                    "webflow", "websitego"}
_OLD_GENERATOR_RE = re.compile(
    r"frontpage|dreamweaver|iweb|homestead|joomla!? ?1\.|joomla!? ?2\.|wordpress [1-4]\.", re.I)


def _get(url: str) -> tuple[requests.Response | None, bool, str | None]:
    """(response, cert_error, error). A bad certificate is retried without
    verification: an expired cert is itself the finding, not a dead site."""
    try:
        return requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT_S), False, None
    except requests.exceptions.SSLError:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", InsecureRequestWarning)
                r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT_S, verify=False)
            return r, True, None
        except requests.RequestException as e:
            return None, True, f"{type(e).__name__}"
    except requests.RequestException as e:
        return None, False, f"{type(e).__name__}"


def _save(html: str, url: str, suffix: str = "") -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    host = re.sub(r"[^a-z0-9.-]", "_", (urlparse(url).hostname or "site").lower())
    path = CACHE_DIR / f"{host}{suffix}.html"
    path.write_text(html, encoding="utf-8")
    return path


def staleness(features: dict, html: str, *, final_url: str, cert_error: bool) -> tuple[int, list[str]]:
    """Points for signs the site is old or neglected, with a reason per point."""
    score, why = 0, []
    if not features.get("viewport_present"):
        score += 3
        why.append("no mobile viewport (desktop layout on phones)")
    if cert_error:
        score += 3
        why.append("security certificate error")
    elif final_url.startswith("http://"):
        score += 2
        why.append("no https")
    year = features.get("copyright_year")
    if year and year <= today_year() - 5:
        score += 2
        why.append(f"copyright {year}")
    elif year and year <= today_year() - 3:
        score += 1
        why.append(f"copyright {year}")
    hand_built = not (set(features.get("platforms") or []) & _HOSTED_BUILDERS)
    if hand_built and _LEGACY_TAG_RE.search(html):
        score += 2
        why.append("1990s/2000s markup (font/center/frames/flash)")
    if hand_built and len(_TABLE_WIDTH_RE.findall(html)) >= 3:
        score += 1
        why.append("table-based layout")
    if hand_built and _OLD_JQUERY_RE.search(html):
        score += 1
        why.append("jQuery 1.x")
    if _OLD_GENERATOR_RE.search(features.get("generator") or ""):
        score += 2
        why.append(f"old site builder ({features['generator']})")
    if features.get("phones_in_text") and not features.get("tel_links"):
        score += 1
        why.append("phone number not tappable")
    return score, why


def prescreen(url: str, company: str | None = None) -> dict:
    """Fetch the homepage (plus up to two contact/about pages when no email shows)
    and return {ok, final_url, email, email_source, stale, why, platforms, error}."""
    r, cert_error, err = _get(url)
    if r is None:
        return {"ok": False, "error": err or "fetch failed", "url": url}
    if r.status_code >= 400:
        return {"ok": False, "error": f"http {r.status_code}", "url": url}
    final_url = r.url
    if (registered_domain(final_url) != registered_domain(url)
            and not domain_names_business(final_url, company)):
        return {"ok": False, "error": f"redirects to {registered_domain(final_url)}", "url": url}
    html = r.text or ""
    path = _save(html, final_url)
    features = extract_features(path, base_url=final_url)
    if not features.get("ok"):
        return {"ok": False, "error": features.get("error", "unreadable page"), "url": url}
    dead, _ = dead_site_reason(features)
    not_theirs = dead or identity_mismatch_reason(features, company)
    if not_theirs:
        return {"ok": False, "error": not_theirs, "url": url}

    candidates = extract_emails(path)
    email, source = choose_contact_email(candidates, final_url, company=company)
    if not email:
        for i, page in enumerate((features.get("crawl_candidates") or [])[:MAX_CONTACT_PAGES]):
            pr, _, _ = _get(page)
            if pr is None or pr.status_code >= 400:
                continue
            candidates += [e for e in extract_emails(_save(pr.text or "", final_url, f"-p{i}"))
                           if e not in candidates]
            email, source = choose_contact_email(candidates, final_url, company=company)
            if email:
                break

    stale, why = staleness(features, html, final_url=final_url, cert_error=cert_error)
    return {"ok": True, "url": url, "final_url": final_url, "email": email,
            "email_source": source if email else None, "stale": stale, "why": why,
            "platforms": features.get("platforms") or [],
            "phone": (features.get("phones_in_text") or [None])[0]}
