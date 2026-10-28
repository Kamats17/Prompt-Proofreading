"""DNC (Do-Not-Contact) engine.

Adapted from PGT_AI_Sales_Agent/email_agent/exclusion_engine.py — same fuzzy
SequenceMatcher algorithm (threshold 0.85), same generic-token guard, but
keyed off the runtime `dnc_list` SQLite table instead of static JSON files.

Three independent checks (any match = blocked):
  1. domain match (exact, after stripping `www.` and protocol)
  2. email match (exact, lowercased)
  3. company name fuzzy match (>= 0.85 with meaningful-token overlap)
"""
import re
import sqlite3
from difflib import SequenceMatcher
from urllib.parse import urlparse

from app.settings import FUZZY_DNC_THRESHOLD
from app.sources._normalize import domain_of

_STRIP_SUFFIXES = re.compile(
    r"\b(llc|inc|corp|corporation|ltd|limited|lp|plc|co\b|company)\b",
    re.IGNORECASE,
)
_STRIP_PUNCT = re.compile(r"[^a-z0-9\s]")

_GENERIC_TOKENS = frozenset({
    "manufacturing", "international", "supply", "services", "service",
    "products", "product", "group", "enterprises", "solutions", "systems",
    "industries", "industry", "global", "materials", "material",
    "equipment", "resources", "associates", "partners", "technologies",
    "technology", "logistics", "transport", "transportation",
    "construction", "management", "distribution", "distributing",
    "restaurant", "restaurants", "salon", "salons", "shop", "shops",
    "store", "stores", "studio", "studios", "garage", "garages",
})


def _normalize_company(name: str) -> str:
    if not name:
        return ""
    s = name.strip().lower()
    s = _STRIP_SUFFIXES.sub("", s)
    s = _STRIP_PUNCT.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _meaningful_tokens(name: str) -> set:
    return {t for t in name.split() if len(t) > 2 and t not in _GENERIC_TOKENS}


def _fuzzy_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    ratio = SequenceMatcher(None, a, b).ratio()
    if ratio < FUZZY_DNC_THRESHOLD:
        return False
    a_tokens = _meaningful_tokens(a)
    b_tokens = _meaningful_tokens(b)
    if a_tokens and b_tokens:
        return bool(a_tokens & b_tokens)
    return ratio >= 0.95


def _domain_of(url_or_domain: str) -> str:
    """Host for any way a domain gets typed: 'acme.com/', 'HTTP://Acme.com:443/x',
    'www.acme.com', or a full URL. Same normalization as prospect URLs."""
    if not url_or_domain:
        return ""
    return domain_of(url_or_domain.strip()) or ""


def is_dnc(conn: sqlite3.Connection, *,
           company: str | None = None,
           domain: str | None = None,
           email: str | None = None) -> str | None:
    """Return reason string if blocked, else None."""
    rows = conn.execute(
        "SELECT id, company, domain, email, reason FROM dnc_list"
    ).fetchall()
    if not rows:
        return None

    target_domain = _domain_of(domain or "")
    target_email = (email or "").strip().lower()
    target_email_domain = target_email.split("@", 1)[-1] if "@" in target_email else ""
    target_company_norm = _normalize_company(company or "")

    def _domain_hit(a: str, b: str) -> bool:
        # Exact or subdomain match: shop.acme.com is blocked by a DNC on acme.com.
        return bool(a) and bool(b) and (a == b or a.endswith("." + b))

    for row in rows:
        dnc_domain = _domain_of(row["domain"] or "")
        dnc_email = (row["email"] or "").strip().lower()
        dnc_company_norm = _normalize_company(row["company"] or "")
        reason = row["reason"] or "no reason"

        if _domain_hit(target_domain, dnc_domain):
            return f"DNC domain match: {target_domain} ~ {dnc_domain} ({reason})"
        if target_email and dnc_email and target_email == dnc_email:
            return f"DNC email match: {dnc_email} ({reason})"
        # A DNC on a domain also blocks an email at that domain (and subdomains).
        if _domain_hit(target_email_domain, dnc_domain):
            return f"DNC email-domain match: {target_email_domain} ~ {dnc_domain} ({reason})"
        if target_company_norm and dnc_company_norm and \
                _fuzzy_match(target_company_norm, dnc_company_norm):
            return f"DNC company match: {row['company']} ({reason})"

    return None
