"""BeautifulSoup extraction. Reads cached HTML, returns features + machine-checked
facts the audit agent needs.

Two kinds of output:
  * features  — text the model reads (title, h1s, body excerpt, CTA labels...)
  * facts     — deterministic observations with stable ids (phone_not_tappable,
                pdf_menu, third_party_ordering:doordash, no_https, ...). Facts are
                computed in code, so an email built on a fact can't be a
                hallucination; the fabrication guard re-checks every fact id.
"""
import datetime as _dt
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.sources._normalize import domain_of, registered_domain, split_emails
from app.textnorm import normalize_text, contains_verbatim

_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)")
_FAX_BEFORE = re.compile(r"\bfax\b", re.I)
_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")

ORDERING_PROVIDERS = {
    "doordash": "doordash.com", "ubereats": "ubereats.com", "grubhub": "grubhub.com",
    "seamless": "seamless.com", "postmates": "postmates.com", "slice": "slicelife.com",
    "chownow": "chownow.com", "toast": "toasttab.com", "menufy": "menufy.com",
    "beyondmenu": "beyondmenu.com", "clover": "clover.com", "square": "square.site",
}
BOOKING_PROVIDERS = {
    "vagaro": "vagaro.com", "booksy": "booksy.com", "square": "squareup.com",
    "opentable": "opentable.com", "resy": "resy.com", "zocdoc": "zocdoc.com",
    "mindbody": "mindbodyonline.com", "schedulicity": "schedulicity.com",
    "styleseat": "styleseat.com", "calendly": "calendly.com", "acuity": "acuityscheduling.com",
    "setmore": "setmore.com", "fresha": "fresha.com", "glossgenius": "glossgenius.com",
    "squire": "getsquire.com", "yelp_reservations": "yelp.com/reservations",
}
PLATFORM_MARKERS = {
    "wix": ("wixstatic.com", "wix.com website builder"),
    "squarespace": ("squarespace.com", "static1.squarespace"),
    "wordpress": ("wp-content", "wp-includes"),
    "godaddy_builder": ("img1.wsimg.com", "godaddy website builder"),
    "weebly": ("weebly.com",),
    "duda": ("dudaone", "multiscreensite.com"),
    "popmenu": ("popmenu.com",),
    "bentobox": ("getbento.com", "bentobox"),
    "owner_com": ("owner.com",),
    "spothopper": ("spothopper",),
    "websitego": ("websitego",),
    "shopify": ("cdn.shopify.com",),
    "webflow": ("webflow.com",),
}
# Capitalized words; a dot is allowed only INSIDE a word (Acme.com), so a credit
# stops at the end of its sentence instead of running into the next one.
_CREDIT_NAME = r"([A-Z][\w&\-]*(?:\.[\w&\-]+)*(?:\s+[A-Z][\w&\-]*(?:\.[\w&\-]+)*){0,3})"
_CREDIT_RE = re.compile(
    r"(?:[Ww]ebsite|[Ss]ite|[Ww]eb)\s*(?:[Dd]esign(?:ed)?|[Bb]uilt|[Dd]eveloped|[Cc]reated|[Mm]ade)?\s*by\s+"
    + _CREDIT_NAME + r"|(?:[Pp]owered|[Dd]esigned|[Hh]osted|[Mm]anaged)\s+by\s+" + _CREDIT_NAME,
)
_CREDIT_STOP = {"toggle", "menu", "sliding", "bar", "home", "back", "top", "privacy", "terms",
                "all", "rights", "reserved", "copyright", "contact", "follow", "skip"}
_CREDIT_JUNK = {"the", "our", "us", "this", "that", "these", "you", "your", "we", "it", "its",
                "wordpress", "love", "passion", "pride", "people", "local", "family"}
_HARVEST_NOTICE = (
    "not be used for unsolicited", "not to be used for unsolicited", "no unsolicited",
    "do not send unsolicited", "will not give, sell", "will not sell or share",
    "may not be harvested", "harvesting of email", "do not harvest",
)
_CTA_RE = re.compile(
    r"\b(book|schedule|order|buy|get|start|contact|call|reserve|free|demo|quote|sign\s*up|"
    r"subscribe|appointment|menu)\b", re.IGNORECASE)
_CRAWL_KEYWORDS = ("contact", "about", "our-story", "story", "team", "staff", "meet",
                   "book", "appointment", "order", "menu", "services")


def _is_menu_anchor(a) -> bool:
    """Dropdown headers in nav menus legitimately use href="#"; they are not
    broken links. Only count '#' anchors that live outside nav/menu structures."""
    if a.get("aria-haspopup") or a.get("aria-expanded") is not None or a.get("data-toggle"):
        return True
    for parent in a.parents:
        if parent.name in ("nav", "header"):
            return True
        cls = " ".join(parent.get("class") or []).lower() if hasattr(parent, "get") else ""
        role = (parent.get("role") or "").lower() if hasattr(parent, "get") else ""
        if role in ("navigation", "menu", "menubar") or any(
                k in cls for k in ("menu", "nav", "dropdown")):
            return True
    return False


def today_year() -> int:
    return _dt.date.today().year


def _visible_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    return normalize_text(soup.get_text(" ", strip=True))


def _abs(href: str, base_url: str | None) -> str:
    href = (href or "").strip()
    if base_url and href and not href.startswith(("mailto:", "tel:", "javascript:", "#")):
        try:
            return urljoin(base_url, href)
        except ValueError:
            return href
    return href


def crawl_candidates_from_links(links: list[dict], base_url: str | None,
                                limit: int = 6) -> list[str]:
    """Same-site HTML pages worth a look for contact info (contact/about first)."""
    if not base_url:
        return []
    own_reg = registered_domain(base_url)
    out: list[str] = []
    for ln in links:
        href = (ln.get("href") or "").split("#")[0]
        if not href.startswith("http") or registered_domain(href) != own_reg:
            continue
        if re.search(r"\.(pdf|jpe?g|png|gif|webp|docx?|zip)(\?|$)", href, re.I):
            continue
        if href.rstrip("/") == base_url.rstrip("/"):
            continue
        key = (urlparse(href).path + " " + (ln.get("label") or "")).lower()
        if any(k in key for k in _CRAWL_KEYWORDS) and href not in out:
            out.append(href)
    out.sort(key=lambda h: (0 if "contact" in h.lower() else
                            1 if "about" in h.lower() or "story" in h.lower() else 2))
    return out[:limit]


def crawl_candidates_from_html(html: str, base_url: str) -> list[str]:
    """Used by the fetcher while the browser is still open."""
    soup = BeautifulSoup(html, "lxml")
    links = [{"label": normalize_text(a.get_text(" ", strip=True)),
              "href": _abs(a.get("href") or "", base_url)} for a in soup.find_all("a")]
    return crawl_candidates_from_links(links, base_url)


def extract_features(html_path: str | Path, *, base_url: str | None = None) -> dict[str, Any]:
    """Extract auditable features and facts from a cached HTML page."""
    html_path = Path(html_path)
    if not html_path.exists():
        return {"ok": False, "error": "html_path missing"}
    html = html_path.read_text(encoding="utf-8", errors="replace")
    html_lower = html.lower()
    soup = BeautifulSoup(html, "lxml")

    html_tag = soup.find("html")
    lang = (html_tag.get("lang") or "").strip().lower() if html_tag else ""
    title = normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    md = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    meta_desc = normalize_text(md.get("content", "")) if md and md.get("content") else ""
    gen = soup.find("meta", attrs={"name": re.compile("^generator$", re.I)})
    generator = normalize_text(gen.get("content", "")) if gen and gen.get("content") else ""

    viewport = soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)})
    viewport_present = viewport is not None and bool(viewport.get("content"))

    h1s = [normalize_text(h.get_text(" ", strip=True)) for h in soup.find_all("h1")]
    h1s = [h for h in h1s if h]
    h2s = [normalize_text(h.get_text(" ", strip=True)) for h in soup.find_all("h2")]
    h2s = [h for h in h2s if h][:8]

    # ---- links (before text extraction decomposes nothing link-related) ----
    links: list[dict] = []
    tel_links = 0
    dead_links = 0
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        label = normalize_text(a.get_text(" ", strip=True)) or normalize_text(a.get("aria-label", ""))
        if href.lower().startswith("tel:"):
            tel_links += 1
        if ((href in ("#", "") or href.lower().startswith("javascript:")) and label
                and _CTA_RE.search(label) and not _is_menu_anchor(a)):
            dead_links += 1
        links.append({"label": label[:80], "href": _abs(href, base_url)[:300]})
    iframe_srcs = [_abs(f.get("src") or "", base_url) for f in soup.find_all("iframe")]
    iframe_domains = sorted({d for d in (domain_of(s) for s in iframe_srcs if s) if d})

    body_text = _visible_text(soup)
    if len(body_text) <= 6500:
        body_excerpt = body_text
    else:
        # First 5000 + last 1500 chars: catches footer copyrights / weak closing CTAs.
        body_excerpt = body_text[:5000] + " [...excerpt break...] " + body_text[-1500:]

    cta_links: list[dict] = []
    for ln in links:
        if ln["label"] and len(ln["label"]) <= 60 and _CTA_RE.search(ln["label"]):
            cta_links.append(ln)
        if len(cta_links) >= 15:
            break
    for b in soup.find_all("button"):
        label = normalize_text(b.get_text(" ", strip=True))
        if label and len(label) <= 60 and _CTA_RE.search(label) and len(cta_links) < 15:
            cta_links.append({"label": label, "href": ""})
    ctas = [c["label"] for c in cta_links]

    forms = []
    for f in soup.find_all("form")[:5]:
        inputs = [inp.get("name") or inp.get("type") or "?"
                  for inp in f.find_all(["input", "textarea", "select"])][:8]
        forms.append({"action": f.get("action") or "", "inputs": inputs})

    imgs = soup.find_all("img")
    img_missing_alt = sum(1 for i in imgs if not (i.get("alt") or "").strip())

    # Every year in the window after each copyright marker; take the most recent.
    copyright_year = None
    years: list[str] = []
    for m in re.finditer(r"(?:©|copyright|&copy;)", body_text, re.IGNORECASE):
        years += _YEAR_RE.findall(body_text[m.end():m.end() + 40])
    if years:
        copyright_year = max(int(y) for y in years)
    ty = today_year()
    mentioned = [int(y) for y in _YEAR_RE.findall(body_text) if int(y) <= ty + 1]
    latest_year_mentioned = max(mentioned) if mentioned else None

    # In page order, skipping fax numbers ("Tel: ... Fax: 888-...") so the phone
    # fact never tells an owner their fax line can't be tapped.
    phones_in_text = list(dict.fromkeys(
        m.group(0).strip() for m in _PHONE_RE.finditer(body_text)
        if not _FAX_BEFORE.search(body_text[max(0, m.start() - 16):m.start()])))[:5]
    phone_locations: list[str] = []
    phone_count = 0
    if phones_in_text:
        want = re.sub(r"\D", "", phones_in_text[0])[-10:]
        phone_count = sum(1 for m in _PHONE_RE.finditer(body_text)
                          if re.sub(r"\D", "", m.group(0))[-10:] == want)
        for label, tags in (("header", ("header", "nav")), ("footer", ("footer",))):
            if any(want in re.sub(r"\D", "", el.get_text(" "))
                   for t in tags for el in soup.find_all(t)):
                phone_locations.append(label)

    hrefs_lower = " ".join(ln["href"].lower() for ln in links) + " " + " ".join(iframe_srcs).lower()
    ordering = sorted({k for k, d in ORDERING_PROVIDERS.items() if d in hrefs_lower})
    booking = sorted({k for k, d in BOOKING_PROVIDERS.items() if d in hrefs_lower})
    pdf_menu = any("menu" in (ln["label"] + " " + ln["href"]).lower()
                   and ln["href"].lower().split("?")[0].endswith(".pdf") for ln in links)
    has_order_link = bool(ordering) or any(re.search(r"\border\b", ln["label"], re.I) for ln in links)
    has_booking_link = bool(booking) or any(
        re.search(r"\b(book|appointment|reserve|reservation|schedule)\b", ln["label"], re.I)
        for ln in links)

    platforms = sorted({name for name, marks in PLATFORM_MARKERS.items()
                        if any(m in html_lower for m in marks)
                        or any(m in generator.lower() for m in marks)})
    credits = []
    for m in _CREDIT_RE.finditer(body_text[-2500:]):
        words = []
        for w in (m.group(1) or m.group(2) or "").split():
            if w.lower().strip(".") in _CREDIT_STOP:
                break
            words.append(w)
        credit = " ".join(words).strip(" .-")
        if len(credit) >= 3 and credit.split()[0].lower() not in _CREDIT_JUNK:
            credits.append(credit[:40])
    own_reg = registered_domain(base_url) if base_url else ""
    credit_link_domains = sorted({
        registered_domain(ln["href"]) for ln in links
        if ln["href"].startswith("http")
        and re.search(r"(design|developed|powered|built|website by|web by|site by)", ln["label"], re.I)
        and registered_domain(ln["href"]) != own_reg
    })

    social = sorted({name for name, d in {
        "facebook": "facebook.com", "instagram": "instagram.com", "tiktok": "tiktok.com",
        "yelp": "yelp.com", "google": "g.page", "x": "twitter.com"}.items() if d in hrefs_lower})

    crawl = crawl_candidates_from_links(links, base_url)

    harvest_notice = any(p in body_text.lower() for p in _HARVEST_NOTICE)

    return {
        "ok": True,
        "lang": lang,
        "title": title,
        "meta_description": meta_desc,
        "generator": generator,
        "viewport_present": viewport_present,
        "h1_first": h1s[0] if h1s else "",
        "h1s": h1s[:5],
        "h1_count": len(h1s),
        "h2_sample": h2s,
        "body_excerpt": body_excerpt,
        "body_text": body_text,
        "body_length": len(body_text),
        "ctas": ctas,
        "cta_links": cta_links,
        "forms": forms,
        "image_count": len(imgs),
        "images_missing_alt": img_missing_alt,
        "copyright_year": copyright_year,
        "latest_year_mentioned": latest_year_mentioned,
        "page_size_kb": round(len(html) / 1024, 1),
        "html_text_lower": body_text.lower(),
        "phones_in_text": phones_in_text,
        "phone_locations": phone_locations,
        "phone_count": phone_count,
        "tel_links": tel_links,
        "dead_links": dead_links,
        "iframe_domains": iframe_domains,
        "park_script": next((m for m in _PARKING_SCRIPTS if m in html_lower), ""),
        "ordering_providers": ordering,
        "booking_providers": booking,
        "pdf_menu": pdf_menu,
        "has_order_link": has_order_link,
        "has_booking_link": has_booking_link,
        "platforms": platforms,
        "designer_credits": credits[:3],
        "credit_link_domains": credit_link_domains,
        "social": social,
        "crawl_candidates": crawl,
        "harvest_notice": harvest_notice,
    }


# ---- facts: deterministic, id-addressable observations -------------------------

def compute_facts(features: dict, *, fetch_meta: dict | None = None,
                  industry: str | None = None) -> dict[str, str]:
    """Return {fact_id: human-readable detail}. Only things code can verify.

    The audit model may cite a fact by id; the fabrication guard accepts the id only
    if it is present in this dict for the same page.
    """
    fm = fetch_meta or {}
    mobile = fm.get("mobile") or {}
    facts: dict[str, str] = {}
    ty = today_year()

    if fm.get("https_ok") is False:
        facts["no_https"] = "The site only loads over plain http, so browsers mark it 'Not secure'."
    # Mobile claims require the real phone render: many builders (Wix, Squarespace)
    # serve different markup to phones, so desktop HTML alone gives false claims.
    mobile_ok = bool(mobile) and not mobile.get("error")
    if mobile_ok and (mobile.get("viewport_width") or 0) > 500:
        facts["desktop_layout_on_phone"] = ("On a phone the site shows the full desktop layout "
                                            "shrunk down, so you have to pinch and zoom to read it.")
    elif not features.get("viewport_present") and not mobile_ok:
        facts["no_viewport_meta"] = "The page has no mobile viewport tag, so phones render it zoomed out."
    if mobile_ok and mobile.get("horizontal_overflow"):
        facts["mobile_horizontal_scroll"] = "On a phone the page is wider than the screen and scrolls sideways."
    if mobile_ok and (mobile.get("small_text_share") or 0) >= 0.3:
        facts["small_text_mobile"] = (f"About {int(mobile['small_text_share'] * 100)}% of the text "
                                      "renders smaller than 12px on a phone.")
    if (mobile_ok and features.get("phones_in_text") and not features.get("tel_links")
            and not mobile.get("tap_to_call_first_screen")
            and not mobile.get("tel_links_total")):
        locs = features.get("phone_locations") or []
        where = ("only in the " + " and the ".join(locs)) if locs else "in the page text"
        times = features.get("phone_count") or 1
        facts["phone_not_tappable"] = (
            f"The phone number {features['phones_in_text'][0]} appears {times} time"
            f"{'s' if times != 1 else ''} on the homepage, {where}, as plain text, so it can't "
            "be tapped to call on a phone.")
    if features.get("pdf_menu"):
        facts["pdf_menu"] = "The menu is a PDF download rather than a page that reads well on a phone."
    for prov in features.get("ordering_providers") or []:
        facts[f"third_party_ordering:{prov}"] = f"Online orders go through {prov} rather than the site itself."
    for prov in features.get("booking_providers") or []:
        facts[f"booking_provider:{prov}"] = f"Bookings go through {prov}."
    ind = (industry or "").lower()
    wants_booking = any(k in ind for k in ("salon", "barber", "dent", "spa", "clinic", "massage",
                                           "chiro", "nail", "medical", "therap"))
    wants_order = any(k in ind for k in ("restaurant", "pizza", "cafe", "diner", "bakery", "grill"))
    if wants_booking and not features.get("has_booking_link") and not features.get("iframe_domains"):
        facts["no_booking_link_found"] = "No online booking link or widget was found on the homepage."
    if wants_order and not features.get("has_order_link") and not features.get("iframe_domains"):
        facts["no_order_link_found"] = "No online ordering link was found on the homepage."
    if (features.get("dead_links") or 0) >= 2:
        facts[f"dead_links:{features['dead_links']}"] = (f"{features['dead_links']} buttons or links "
                                                         "on the homepage go nowhere.")
    cy = features.get("copyright_year")
    ly = features.get("latest_year_mentioned")
    if cy:
        facts[f"copyright_year:{cy}"] = f"The footer copyright reads {cy}."
        if cy <= ty - 2 and (ly is None or ly <= ty - 2):
            facts["stale_copyright"] = (f"The footer still says {cy} and nothing on the page "
                                        f"mentions a more recent year.")
    for p in features.get("platforms") or []:
        facts[f"platform:{p}"] = f"The site is built on {p}."
    for c in features.get("designer_credits") or []:
        facts[f"designer_credit:{c}"] = f"The footer credits {c} for the site."
    # Speed: measured locally on the iPhone render (no Google APIs). Thresholds
    # are deliberately loose because the measurement runs on a fast connection;
    # anything flagged here is slow even in good conditions.
    if mobile_ok:
        mb = (mobile.get("page_bytes") or 0) / 1_000_000
        if mb >= 5:
            facts["heavy_page_mobile"] = (f"The homepage downloads about {mb:.0f} MB on a phone, "
                                          "which is slow on a cellular connection.")
        lcp = mobile.get("lcp_seconds")
        if lcp and lcp >= 4:
            facts["slow_main_content"] = (f"Even on a fast connection, the main content took about "
                                          f"{lcp:.0f} seconds to appear on a phone.")
    cert = fm.get("cert") or {}
    if cert.get("expired"):
        facts["cert_expired"] = ("The site's security certificate has expired, so browsers show "
                                 "a full-page warning before the site loads.")
    elif fm.get("cert_ignored") and "expired" in cert:
        # The browser refused the certificate and the TLS check confirms it.
        facts["cert_invalid"] = ("The site's security certificate isn't valid, so browsers show "
                                 "a full-page warning before the site loads.")
    elif cert.get("days_left") is not None and 0 <= cert["days_left"] <= 14 and fm.get("https_ok"):
        facts["cert_expiring_soon"] = (f"The site's security certificate expires in "
                                       f"{cert['days_left']} days.")
    return facts


# ---- email harvest ---------------------------------------------------------------

FREE_MAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com",
    "me.com", "msn.com", "live.com", "comcast.net", "att.net", "sbcglobal.net",
    "verizon.net", "bellsouth.net", "cox.net", "charter.net", "ymail.com", "protonmail.com",
}
# Starts only at the beginning of a run and caps each part, so a long blob of
# email-like characters (inline base64, minified data) is scanned once, not
# once per character: the unbounded pattern took ~2 minutes on one 800 KB page.
_EMAIL_RE = re.compile(r"(?<![a-zA-Z0-9._%+\-])[a-zA-Z0-9._%+\-]{1,64}"
                       r"@[a-zA-Z0-9.\-]{1,253}\.[a-zA-Z]{2,24}")
_EMAIL_BLOCKLIST_SUBSTR = (
    "example.", "sentry", "wixpress", "godaddy", "yourdomain", "domain.com",
    "email.com", "yoursite", "test@", "user@", "name@", "@2x", "@3x",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".css", ".js",
    "noreply", "no-reply", "donotreply", "privacy@", "abuse@", "webmaster@",
)


def extract_emails(html_path: str | Path) -> list[str]:
    """Candidate emails from a cached page: mailto: links first, then a regex sweep.
    Every address is unquoted, split, and validated."""
    html_path = Path(html_path)
    if not html_path.exists():
        return []
    html = html_path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    found: list[str] = []

    def _add(addr: str) -> None:
        if addr in found or any(b in addr for b in _EMAIL_BLOCKLIST_SUBSTR):
            return
        found.append(addr)

    for a in soup.select('a[href^="mailto:" i], a[href^="MAILTO:"]'):
        for addr in split_emails(a.get("href") or ""):
            _add(addr)
    for m in _EMAIL_RE.findall(html):
        for addr in split_emails(m):
            _add(addr)
    return found[:8]


# Substring match on a squashed label, so only words that can't hide inside a
# business name ("seo" sits inside "houseofpancakes").
_VENDOR_WORDS = ("design", "media", "marketing", "digital", "website", "studio",
                 "hosting", "agency", "creative", "solutions")


def domain_names_business(domain: str, company: str | None) -> bool:
    """True when another domain carries the business's distinctive name: the
    owner's second domain (marylandmallet.com site, management@mdmallet.com) or
    a move (smyrnadiner.net -> smyrnadiner.com). Vendor-looking names never count."""
    reg = registered_domain(domain) or ""
    label = re.sub(r"[^a-z0-9]", "", reg.split(".")[0].lower())
    if not label or not company or any(w in label for w in _VENDOR_WORDS):
        return False
    return any(len(t) >= 4 and t in label for t in _name_tokens(company))


def choose_contact_email(candidates: list[str], prospect_url: str,
                         exclude_domains: list[str] | None = None,
                         company: str | None = None) -> tuple[str | None, str]:
    """Pick the address to email, or (None, reason).

    Only the business's own domain (incl. subdomains), another domain carrying
    the business's name (see domain_names_business), or a common free-mail
    provider is accepted. Anything else on the page is usually a vendor, agency,
    or platform (the footer "website by" credit) — emailing them a critique of
    their own work, or a stranger, is worse than not emailing at all.
    """
    own = registered_domain(prospect_url)
    excluded = {registered_domain(d) for d in (exclude_domains or []) if d}
    own_hits = [e for e in candidates if registered_domain(e.split("@", 1)[1]) == own]
    if own_hits:
        return own_hits[0], "harvested_own_domain"
    named = [e for e in candidates if registered_domain(e.split("@", 1)[1]) not in excluded
             and e.split("@", 1)[1] not in FREE_MAIL_DOMAINS
             and domain_names_business(e.split("@", 1)[1], company)]
    if named:
        return named[0], "harvested_name_domain"
    free = [e for e in candidates if e.split("@", 1)[1] in FREE_MAIL_DOMAINS
            and not re.search(r"\.(com|net|org|io|co)\b", e.split("@", 1)[0])
            and registered_domain(e.split("@", 1)[1]) not in excluded]
    if free:
        return free[0], "harvested_free_mail"
    return None, "no own-domain or free-mail address (third-party addresses ignored)"


# ---- identity / dead-site gates ------------------------------------------------------

_PARKED_MARKERS = (
    "domain is for sale", "buy this domain", "this domain is for sale",
    "domain for sale", "account suspended", "this account has been suspended",
    "default web page", "future home of something", "website is parked",
    "parked free", "sedoparking", "hugedomains", "domain parking", "index of /",
    "site not found", "website coming soon", "site coming soon", "under construction",
)
# Domain-parking ad networks load these scripts on a near-empty page.
_PARKING_SCRIPTS = ("sk-park.php", "parkingcrew.net", "bodis.com/", "sedoparking.com",
                    "parklogic.com", "above.com/marketplace")
# Multi-word or unambiguous markers only — a single common word ("judi", "toto")
# could be an owner's name ("Judi's Diner").
_SPAM_MARKERS = (
    "togel", "slot gacor", "slot online", "judi online", "judi bola", "casino online",
    "bandar togel", "toto macau", "keluaran macau", "situs resmi", "poker online",
    "sbobet", "viagra", "cialis", "online casino", "payday loan", "crypto casino",
    "macau 4d", "4d hari ini", "result terlengkap",
)
_NON_EN_LANGS = ("id", "ms", "zh", "ru", "ko", "ja", "th", "vi", "tr", "ar", "fa", "hi")
_GENERIC_NAME_WORDS = {
    "the", "and", "of", "restaurant", "restaurants", "cafe", "café", "diner", "grill",
    "bar", "family", "kitchen", "pizza", "pizzeria", "bakery", "salon", "barbershop",
    "barber", "shop", "llc", "inc", "co", "company", "dental", "dentist", "dds",
    "services", "service", "group", "house", "place", "eatery", "bistro", "pub", "spa",
}


def dead_site_reason(features: dict) -> tuple[str | None, bool]:
    """(reason, permanent). Parked/placeholder markers only count in the title, an
    h1, or on a short page — so "new patio coming soon!" on a live menu page does not
    kill a good lead. A thin page is a SOFT failure (often JS-rendered or a bot
    check) and is retried later instead of blocked forever."""
    body_len = features.get("body_length", 0) or 0
    head_text = " ".join([features.get("title", "")] + features.get("h1s", [])).lower()
    body_lower = features.get("html_text_lower") or ""
    for marker in _PARKED_MARKERS:
        if marker in head_text or (body_len < 1500 and marker in body_lower):
            return f"parked/placeholder marker: {marker!r}", True
    if features.get("park_script") and body_len < 1500:
        return f"parked domain (parking script {features['park_script']!r})", True
    if body_len < 300:
        return f"thin page ({body_len} chars of visible text)", False
    return None, False


def _name_tokens(company: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", normalize_text(company).lower().replace("'", ""))
    return [w for w in words if len(w) >= 3 and w not in _GENERIC_NAME_WORDS]


def identity_mismatch_reason(features: dict, company: str | None) -> str | None:
    """Return a reason if the page is clearly NOT this business's site: a hijacked
    or expired domain now serving gambling/spam, a foreign-language takeover, or a
    page that never mentions the business. None means 'looks like theirs'."""
    title_l = (features.get("title") or "").lower()
    body_l = features.get("html_text_lower") or ""
    for marker in _SPAM_MARKERS:
        if marker in title_l or body_l.count(marker) >= 3:
            return f"domain appears hijacked (spam/gambling content: {marker!r})"
    lang = (features.get("lang") or "")[:2]
    if lang in _NON_EN_LANGS:
        return f"page language is {lang!r}, not this US business"
    if not company:
        return None
    tokens = _name_tokens(company)
    if not tokens:
        return None
    haystack = " ".join([title_l, " ".join(h.lower() for h in features.get("h1s", [])), body_l])
    squashed = re.sub(r"[^a-z0-9]", "", haystack)
    for t in tokens:
        if t in haystack or t in squashed or (len(t) >= 10 and t[:6] in squashed):
            return None
    return f"business name {company!r} does not appear anywhere on the page"


# ---- quote verification (fabrication guard) --------------------------------------

def page_text_for_quotes(html_path: str | Path) -> str:
    """All text the audit model could quote from: title + meta description + body."""
    html = Path(html_path).read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    md = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    meta = md.get("content", "") if md else ""
    return normalize_text(" ".join([title, meta, _visible_text(soup)]))


def quote_in_html(quote: str, html_path: str | Path) -> bool:
    """Case-sensitive, visual-normalization-only containment check."""
    if not quote or not quote.strip():
        return False
    return contains_verbatim(page_text_for_quotes(html_path), quote)
