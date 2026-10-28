"""Build a before/after phone mockup for one audited prospect.

  before  = the real iPhone screenshot captured during the audit
  after   = a phone-sized redesign of their header using ONLY their own name,
            headline, phone number, logo and main color, plus the buttons a
            customer of that kind of business needs
  changes = a numbered list tied to the audit's verified facts/findings

Deterministic (no model call), so it can't invent hours, prices, menu items, or
reviews, and it works even when Claude is unavailable.

Output: data/mockups/<prospect_id>/mockup.html, mockup.png, proposed.png
"""
import json
import re
import sqlite3
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.audit.extractor import extract_features
from app.db.models import Event
from app.db.repo import get_prospect, latest_audit_for_prospect, log_event
from app.audit.extractor import _GENERIC_NAME_WORDS
from app.draft.voice import short_name
from app.mockup.brand import extract_brand
from app.settings import BRAND, MOCKUPS_DIR
from app.sources._normalize import domain_of, smart_title

TEMPLATES = Path(__file__).parent / "templates"
_env = Environment(loader=FileSystemLoader(str(TEMPLATES)),
                   autoescape=select_autoescape(["html"]))

_FOOD = ("restaurant", "cafe", "café", "diner", "pizza", "grill", "bakery", "bar", "kitchen", "deli")
_BOOKING = ("salon", "barber", "spa", "dent", "clinic", "massage", "chiro", "nail", "therap", "medical")

# fact id (prefix) -> (title, why). Order = priority on the mockup.
CHANGE_FOR_FACT = [
    ("phone_not_tappable", "One-tap call button at the top of every page",
     "Right now the number has to be copied by hand on a phone."),
    ("desktop_layout_on_phone", "A layout built for phones",
     "Readable text and buttons, no pinching or zooming."),
    ("no_viewport_meta", "A layout built for phones", "Readable text, no pinching or zooming."),
    ("mobile_horizontal_scroll", "Nothing wider than the screen", "No sideways scrolling on a phone."),
    ("small_text_mobile", "Readable text size on phones", ""),
    ("pdf_menu", "Menu as a real page that reads on a phone", "No PDF download to open and zoom."),
    ("no_https", "Secure https", "Browsers stop showing \"Not secure\" next to your name."),
    ("cert_expired", "Renewed security certificate", "No full-page browser warning before your site loads."),
    ("cert_expiring_soon", "Security certificate renewed before it lapses", ""),
    ("heavy_page_mobile", "A much lighter page", "Loads quickly on a cell signal."),
    ("slow_main_content", "Faster first screen", "The main content shows up right away."),
    ("third_party_ordering:", "Order button front and center", "Still works with your current ordering service."),
    ("no_order_link_found", "Order Online button on the first screen", ""),
    ("no_booking_link_found", "Book Now button on the first screen", ""),
    ("dead_links:", "Every button goes somewhere", "Fix the links that currently lead nowhere."),
    ("stale_copyright", "Fresh footer and up-to-date details", ""),
]


def _digits(phone: str) -> str:
    d = re.sub(r"\D", "", phone or "")
    return d[-10:] if len(d) >= 10 else d


def _pretty_phone(phone: str) -> str:
    d = _digits(phone)
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else (phone or "")


def plan_changes(facts: dict, issues: list[dict], has_phone: bool) -> list[dict]:
    """Ordered, de-duplicated list of improvements, each tied to evidence."""
    out: list[dict] = []
    seen_titles: set[str] = set()

    def add(title: str, why: str = "") -> None:
        if title not in seen_titles and len(out) < 6:
            seen_titles.add(title)
            out.append({"title": title, "why": why})

    for prefix, title, why in CHANGE_FOR_FACT:
        if any(fid == prefix or (prefix.endswith(":") and fid.startswith(prefix)) for fid in facts):
            add(title, why)
    for i in issues:
        if i.get("type") == "typo" and i.get("error_text") and i.get("correction"):
            add("Copy cleanup", f"For example \"{i['error_text']}\" becomes \"{i['correction']}\".")
    if has_phone and "One-tap call button at the top of every page" not in seen_titles:
        add("Tap-to-call button kept in the header", "")
    return out


def _buttons(industry: str, phone: str, features: dict) -> list[str]:
    ind = (industry or "").lower()
    buttons: list[str] = []
    if phone:
        buttons.append(f"Call {_pretty_phone(phone)}")
    if any(k in ind for k in _FOOD):
        buttons.append("View Menu")
        if features.get("ordering_providers") or features.get("has_order_link"):
            buttons.append("Order Online")
    elif any(k in ind for k in _BOOKING):
        buttons.append("Book an Appointment")
    else:
        buttons.append("Request a Quote")
    return buttons[:3]


def _headline_and_subhead(features: dict, company: str, meta: dict) -> tuple[str, str]:
    h1 = (features.get("h1_first") or "").strip()
    # Their h1 is often a section heading ("CURRENT HOURS", "Menu"). Use it only
    # when it actually names the business; otherwise the business name leads.
    name_tokens = [t for t in re.findall(r"[a-z0-9]+", company.lower().replace("'", ""))
                   if len(t) >= 3 and t not in _GENERIC_NAME_WORDS]
    h1_low = h1.lower().replace("'", "")
    names_business = any(t in h1_low for t in name_tokens)
    headline = h1 if (3 <= len(h1) <= 70 and names_business) else company
    if headline.isupper():
        headline = smart_title(headline)
    sub = (meta.get("personal_detail") or "").strip()
    if not sub:
        sub = _first_sentences((features.get("meta_description") or "").strip(), 150)
    if sub.lower() == headline.lower():
        sub = ""
    return headline, sub


def _first_sentences(text: str, limit: int) -> str:
    """Whole sentences up to `limit` chars; never a mid-sentence cut."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    out = ""
    for sent in re.split(r"(?<=[.!?])\s+", text):
        if len(out) + len(sent) + 1 > limit:
            break
        out = (out + " " + sent).strip()
    return out


_TIME = r"\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)"
_TIME_RANGE = re.compile(_TIME + r"\s*(?:-|–|to)\s*" + _TIME, re.IGNORECASE)
_HOURS_LEAD = re.compile(r"^(?:our\s+)?(?:current\s+)?(?:hours(?:\s+are)?|from)\s*:?\s*", re.IGNORECASE)
_OPEN_HINT = re.compile(r"\b(open|daily|days a week|every day)\b", re.IGNORECASE)
MAX_HOURS_CHARS = 130
_ADDRESS_RE = re.compile(
    r"\b(\d{2,6}\s+(?:[NSEW]\.?\s+|North\s+|South\s+|East\s+|West\s+)?[A-Z][A-Za-z0-9'. ]{1,40}?\s"
    r"(?:St|Street|Ave|Avenue|Rd|Road|Dr|Drive|Blvd|Boulevard|Hwy|Highway|Ln|Lane|Way|Pkwy|"
    r"Parkway|Ct|Court|Pl|Place|Pike|Route|Rte)\b\.?)")


def find_hours(body_text: str) -> str:
    """The WHOLE sentence holding the first opening-hours range, copied from the
    page. A partial hours line is misleading ("Sunday 8am to 3pm" when they're
    open all week), so if the sentence is too long we show nothing at all."""
    text = re.sub(r"\s+", " ", body_text or "")
    sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+", text) if x.strip()]
    for i, sent in enumerate(sentences):
        ranges = list(_TIME_RANGE.finditer(sent))
        if not ranges:
            continue
        # End at the last time range: pages without punctuation run straight into
        # the next heading ("...2 pm The Best Kept Secret").
        out = _HOURS_LEAD.sub("", sent[:ranges[-1].end()].strip(" .!")).strip()
        prev = sentences[i - 1].strip() if i else ""
        lead = re.search(r"((?:open|daily)\b[^.!?]{0,30}[.!?]?)\s*$", prev, re.IGNORECASE)
        if lead:                                              # "Open 7 days a week!"
            out = f"{lead.group(1).strip()} {out}"
        return out if len(out) <= MAX_HOURS_CHARS else ""
    return ""


def find_hours_and_address(body_text: str) -> tuple[str, str]:
    """Copied verbatim from their page (never composed), or '' when not found."""
    addr = _ADDRESS_RE.search(body_text or "")
    address = re.sub(r"\s+", " ", addr.group(1)).strip(" ,;:!")[:60] if addr else ""
    return find_hours(body_text), address


def mockup_paths(prospect_id: int) -> dict:
    d = Path(MOCKUPS_DIR) / str(prospect_id)
    return {"dir": d, "html": d / "mockup.html", "png": d / "mockup.png", "proposed": d / "proposed.png"}


def build_mockup(conn: sqlite3.Connection, prospect_id: int, *, render: bool = True) -> dict:
    p = get_prospect(conn, prospect_id)
    if not p:
        return {"ok": False, "error": "prospect not found"}
    audit = latest_audit_for_prospect(conn, prospect_id)
    if not audit or not audit.html_cache_path or not Path(audit.html_cache_path).exists():
        return {"ok": False, "error": "no cached audit page — run the audit first"}
    meta = json.loads(audit.meta_json or "{}")
    issues = [i for i in json.loads(audit.issues_json or "[]") if i.get("confidence") == "verified"]
    base_url = meta.get("final_url") or p.url
    features = extract_features(audit.html_cache_path, base_url=base_url)
    brand = extract_brand(audit.html_cache_path, base_url, p.industry)
    phone = (features.get("phones_in_text") or [None])[0] or p.phone or ""
    short = short_name(p.company)
    headline, subhead = _headline_and_subhead(features, p.company, meta)
    before = meta.get("mobile_screenshot_path") or audit.screenshot_path
    before_src = Path(before).resolve().as_uri() if before and Path(before).exists() else None
    city = ", ".join(x for x in (p.city, p.state) if x) if p.city else ""
    hours, address = find_hours_and_address(features.get("body_text", ""))
    info = []
    if hours:
        info.append({"label": "Hours", "value": hours})
    if address:
        info.append({"label": "Find us", "value": address})
    elif city:
        info.append({"label": "Serving", "value": city})

    ctx = {
        "short": short, "company": p.company, "color": brand["color"], "logo_url": brand["logo_url"],
        "phone": _pretty_phone(phone) if phone else "", "phone_digits": _digits(phone),
        "headline": headline, "subhead": subhead,
        "buttons": _buttons(p.industry or "", phone, features),
        "info_lines": info,
        "changes": plan_changes(meta.get("facts") or {}, issues, bool(phone)),
        "before_src": before_src, "city": city, "domain": domain_of(p.url) or p.url,
        "studio": BRAND["company"],
    }
    paths = mockup_paths(prospect_id)
    paths["dir"].mkdir(parents=True, exist_ok=True)
    paths["html"].write_text(_env.get_template("mockup.html").render(**ctx), encoding="utf-8")

    result = {"ok": True, "html_path": str(paths["html"]), "changes": ctx["changes"],
              "brand": brand, "png_path": None, "proposed_png_path": None}
    if render:
        err = render_png(paths["html"], paths["png"], paths["proposed"])
        if err:
            result.update(ok=False, error=f"render failed: {err}")
        else:
            result.update(png_path=str(paths["png"]), proposed_png_path=str(paths["proposed"]))
    log_event(conn, Event(prospect_id=prospect_id, event_type="mockup_built",
                          payload_json=json.dumps({"ok": result["ok"], "changes": len(ctx["changes"]),
                                                   "color_source": brand["color_source"],
                                                   "logo": bool(brand["logo_url"])})))
    return result


def render_png(html_path: Path, png_path: Path, proposed_path: Path) -> str | None:
    """Screenshot the mockup sheet and the proposed phone. Returns an error or None."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "playwright not installed"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1276, "height": 900}, device_scale_factor=2)
                page.goto(Path(html_path).resolve().as_uri(), wait_until="load", timeout=30000)
                try:   # give a remote logo a moment; never fail the mockup over it
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                page.screenshot(path=str(png_path), full_page=True)
                page.locator("#proposed .phone").screenshot(path=str(proposed_path))
            finally:
                browser.close()
    except Exception as e:
        return f"{type(e).__name__}: {str(e)[:200]}"
    return None
