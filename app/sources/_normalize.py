"""Shared normalization helpers for all ingestors."""
import re
from urllib.parse import urlparse, urlunparse, unquote

_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                    "fbclid", "gclid", "mc_cid", "mc_eid"}

# Second-level labels used under country TLDs (example.co.uk, example.com.au).
_SECOND_LEVEL = {"co", "com", "net", "org", "gov", "ac", "edu"}

# Country-code TLDs we never prospect (US-only outreach; other countries have
# opt-in consent laws such as Canada's CASL and the EU's ePrivacy/GDPR rules).
NON_US_TLDS = {
    "ca", "uk", "au", "nz", "ie", "de", "fr", "es", "it", "nl", "be", "ch", "at",
    "se", "no", "dk", "fi", "pl", "pt", "gr", "cz", "hu", "ro", "bg", "sk", "si",
    "hr", "lt", "lv", "ee", "lu", "mt", "cy", "is", "in", "jp", "cn", "kr", "br",
    "mx", "ar", "cl", "co.za", "za", "ru", "ua", "tr", "il", "ae", "sg", "hk", "ph",
}

US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV",
    "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN",
    "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC", "PR",
}


def _host_to_ascii(host: str) -> str:
    """Lowercase + IDNA-encode non-ASCII hosts (café.com -> xn--caf-dma.com)."""
    host = host.strip().lower().rstrip(".")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def normalize_url(raw: str) -> str | None:
    """Lowercase host, force https, strip tracking params, canonical trailing slash."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if not raw.lower().startswith(("http://", "https://")):
        raw = "https://" + raw
    try:
        u = urlparse(raw)
    except ValueError:
        return None
    if not u.netloc:
        return None
    netloc = u.netloc
    if "@" in netloc:          # strip credentials (user:pass@host)
        netloc = netloc.rsplit("@", 1)[1]
    host, _, port = netloc.partition(":")
    host = _host_to_ascii(host)
    # Require a host with a dot AND no whitespace.
    if "." not in host or " " in host:
        return None
    if host.startswith("www."):
        host = host[4:]
    netloc = host + (f":{port}" if port and port not in ("80", "443") else "")
    path = u.path or "/"
    query_parts = []
    if u.query:
        for part in u.query.split("&"):
            k = part.split("=", 1)[0].lower()
            if k not in _TRACKING_PARAMS:
                query_parts.append(part)
    # Sort remaining params so ?a=1&b=2 and ?b=2&a=1 dedup to one key.
    query = "&".join(sorted(query_parts))
    # Strip a trailing slash on sub-paths (so /about and /about/ dedup). The root
    # path collapses to empty only when there is no query (keeps /?id=1 readable).
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    if path == "/" and not query:
        path = ""
    return urlunparse(("https", netloc, path, "", query, ""))


def domain_of(url: str) -> str | None:
    """Host without www./port/credentials, lowercase, IDNA-encoded."""
    if not url:
        return None
    url = url.strip()
    try:
        u = urlparse(url if "://" in url else "https://" + url)
    except ValueError:
        return None
    netloc = (u.netloc or "")
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    host = _host_to_ascii(netloc.split(":", 1)[0])
    if host.startswith("www."):
        host = host[4:]
    return host or None


def registered_domain(host_or_url: str | None) -> str:
    """Best-effort registrable domain: shop.acme.com -> acme.com,
    acme.co.uk -> acme.co.uk. Crude (no full Public Suffix List) but enough to
    tell 'same business site' from 'redirected somewhere else'."""
    host = domain_of(host_or_url or "") or ""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def is_non_us_domain(url: str | None) -> bool:
    host = domain_of(url or "") or ""
    if not host:
        return False
    tld = host.rsplit(".", 1)[-1]
    last_two = ".".join(host.split(".")[-2:])
    return tld in NON_US_TLDS or last_two in NON_US_TLDS


def is_us_state(state: str | None) -> bool | None:
    """True/False for a recognisable value, None when unknown/blank."""
    if not state or not state.strip():
        return None
    s = state.strip().upper()
    if s in US_STATE_CODES:
        return True
    return None if len(s) > 2 else False


_EMAIL_OK = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9-]+(\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,}$")


def normalize_email(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = unquote(str(raw)).strip().strip("<>").lower()
    if raw.startswith("mailto:"):
        raw = raw[7:]
    raw = raw.split("?", 1)[0].strip()
    return raw if _EMAIL_OK.fullmatch(raw) else None


def split_emails(raw: str | None) -> list[str]:
    """`mailto:a@x.com,b@y.com` / 'a@x.com; b@y.com' -> validated list."""
    if not raw:
        return []
    raw = unquote(str(raw))
    if raw.lower().startswith("mailto:"):
        raw = raw[7:]
    raw = raw.split("?", 1)[0]
    out: list[str] = []
    for part in re.split(r"[,;\s]+", raw):
        e = normalize_email(part)
        if e and e not in out:
            out.append(e)
    return out


def normalize_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return None
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return digits
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


_SMALL_WORDS = {"and", "of", "the", "at", "in", "on", "for", "a", "an", "&"}
_KEEP_UPPER = {"LLC", "INC", "LLP", "PC", "PLLC", "DDS", "DMD", "MD", "BBQ", "USA",
               "HVAC", "LTD", "CPA", "II", "III", "IV"}


def smart_title(name: str) -> str:
    """Title-case an ALL-CAPS business name without str.title()'s bugs:
    "JOE'S PIZZA LLC" -> "Joe's Pizza LLC" (not "Joe'S Pizza Llc")."""
    words = name.split()
    out = []
    for i, w in enumerate(words):
        bare = re.sub(r"[^A-Za-z]", "", w).upper()
        if bare in _KEEP_UPPER:
            out.append(w.upper())
        elif i > 0 and w.lower() in _SMALL_WORDS:
            out.append(w.lower())
        else:
            out.append(re.sub(r"[A-Za-z]+('[A-Za-z]+)?",
                              lambda m: m.group(0)[:1].upper() + m.group(0)[1:].lower(), w))
    return " ".join(out)


def clean_company(raw: str | None, url: str | None) -> str:
    """Non-empty, sanely-cased company name. Falls back to a domain-derived name so
    a subject line is never empty or shouty ALL-CAPS."""
    name = re.sub(r"\s+", " ", (raw or "")).strip()
    if not name:
        base = (domain_of(url or "") or "").split(".")[0]
        return smart_title(base.replace("-", " ").replace("_", " ")) or (url or "")
    if name.isupper():
        name = smart_title(name)
    return name
