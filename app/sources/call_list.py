"""Call list for leads the email agent can't reach: businesses with NO website of
their own (only a Restaurantji/Facebook/directory listing, or nothing), but a
phone number. These are often the best fit for a website build — and in the
master CSV they're ~40% of rows, versus ~10% with an auditable site.

Output is a CSV for MANUAL calls. (Check numbers that may be mobile phones
against the National Do Not Call Registry before calling; never autodial/text.)
"""
import csv
import datetime as _dt
import sqlite3
from pathlib import Path

from app.safety.aggregator_filter import is_aggregator
from app.safety.dnc import is_dnc
from app.sources._normalize import normalize_url, normalize_phone, clean_company, is_us_state
from app.sources.from_master_csv import read_csv_rows

PRIORITY_TYPES = ("dent", "barber", "salon", "spa", "auto", "plumb", "hvac", "roof", "clinic")


def _pitch(type_: str, listing: str) -> str:
    where = f"only listed on {listing}" if listing else "no website"
    t = (type_ or "business").lower()
    if any(k in t for k in ("barber", "salon", "spa", "dent", "clinic")):
        offer = "a simple mobile site with online booking and tap-to-call"
    elif any(k in t for k in ("restaurant", "cafe", "diner", "pizza", "grill", "bakery")):
        offer = "a simple mobile site with your menu, hours, and tap-to-call/online ordering"
    else:
        offer = "a simple mobile site with your services, hours, and tap-to-call"
    return f"{where.capitalize()}; offer {offer}."


_NOTES = {"no_email": "audited site, no email found",
          "domain_dead": "website address no longer exists",
          "deadsite": "website shows a parked/placeholder page",
          "redirect_offsite": "website address forwards to another site"}


def _cached_facts(url: str, industry: str | None) -> dict:
    """Facts from the page already fetched during the audit (local, no Claude).
    No-email leads are parked before facts are stored, so recompute them here."""
    import json
    from app.audit.extractor import compute_facts, extract_features
    from app.audit.fetcher import _cache_dir_for, _result_from_meta
    cache = _cache_dir_for(url)
    meta_path, html = cache / "meta.json", cache / "page.html"
    if not (meta_path.exists() and html.exists()):
        return {}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not meta.get("ok"):
        return {}
    features = extract_features(html, base_url=meta.get("final_url") or url)
    if not features.get("ok", True):
        return {}
    return compute_facts(features, fetch_meta=_result_from_meta(meta, cache, cached=True),
                         industry=industry)


def _no_email_prospects(conn: sqlite3.Connection, seen: set[str]) -> list[dict]:
    """Prospects with a real website the audit found NO email for (contact form or
    phone only). The site's verified problems become the call's talking points."""
    import json
    from app.mockup.builder import plan_changes
    rows = conn.execute(
        """SELECT p.*, (SELECT meta_json FROM audits WHERE prospect_id = p.id
                        ORDER BY id DESC LIMIT 1) AS meta_json
           FROM prospects p WHERE p.blocked_reason IN ('no_email', 'domain_dead', 'deadsite',
                                                        'redirect_offsite')""").fetchall()
    out = []
    for r in rows:
        phone = normalize_phone(r["phone"])
        if not phone or phone in seen or len("".join(ch for ch in phone if ch.isdigit())) != 10:
            continue
        if is_dnc(conn, company=r["company"], domain=r["url"]):
            continue
        seen.add(phone)
        facts = (json.loads(r["meta_json"] or "{}") or {}).get("facts", {}) if r["meta_json"] else {}
        if not facts and r["blocked_reason"] == "no_email":
            facts = _cached_facts(r["url"], r["industry"])
        # Only measured problems become talking points; never a generic guess.
        points = [c["title"] for c in plan_changes(facts, [], False)][:2]
        reason = r["blocked_reason"]
        if reason == "domain_dead":
            pitch = (f"Their website address ({r['url']}) no longer works; customers who search for "
                     "them hit a dead link. Offer a new site.")
        elif reason == "deadsite":
            pitch = (f"Their website address ({r['url']}) shows a parked or placeholder page, not "
                     "their business. Offer a new site.")
        elif reason == "redirect_offsite":
            pitch = (f"Their website address ({r['url']}) forwards visitors to a different site, "
                     "not one of their own. Offer a site of their own.")
        else:
            pitch = ("Has a website but no email listed; " +
                     ("talk about: " + "; ".join(points) + "." if points else "offer a free site review."))
        out.append({"Business": r["company"], "Type": r["industry"] or "", "City": r["city"] or "",
                    "State": r["state"] or "", "Phone": phone, "Listing": r["url"], "Pitch": pitch,
                    "Source note": _NOTES[reason],
                    "Outcome": "", "Follow-up date": ""})
    return out


def build_call_list(conn: sqlite3.Connection, csv_path: str | Path, out_dir: str | Path) -> dict:
    rows = read_csv_rows(csv_path)
    seen: set[str] = set()
    out: list[dict] = []
    skipped = {"has_own_site": 0, "no_phone": 0, "dnc": 0, "duplicate": 0, "non_us": 0}
    for r in rows:
        raw_url = (r.get("Website URL") or "").strip()
        url = normalize_url(raw_url)
        listing = ""
        if url:
            agg, why = is_aggregator(url)
            if not agg:
                skipped["has_own_site"] += 1
                continue
            listing = (why or "").split(":")[-1].strip() or url
        phone = normalize_phone(r.get("Phone"))
        if not phone or len("".join(ch for ch in phone if ch.isdigit())) != 10:
            skipped["no_phone"] += 1
            continue
        if is_us_state(r.get("State")) is False:
            skipped["non_us"] += 1
            continue
        if phone in seen:
            skipped["duplicate"] += 1
            continue
        seen.add(phone)
        company = clean_company(r.get("Business Name"), url)
        if is_dnc(conn, company=company):
            skipped["dnc"] += 1
            continue
        type_ = (r.get("Business Type") or "").strip()
        out.append({
            "Business": company, "Type": type_, "City": (r.get("City") or "").strip(),
            "State": (r.get("State") or "").strip(), "Phone": phone,
            "Listing": url or "", "Pitch": _pitch(type_, listing),
            "Source note": (r.get("Missing Feature") or r.get("Notes") or "").strip()[:120],
            "Outcome": "", "Follow-up date": "",
        })
    out.extend(_no_email_prospects(conn, seen))
    out.sort(key=lambda x: (0 if any(k in x["Type"].lower() for k in PRIORITY_TYPES) else 1,
                            x["State"], x["City"]))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"call_list_{_dt.date.today().isoformat()}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:   # BOM: opens cleanly in Excel
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()) if out else ["Business"])
        w.writeheader()
        w.writerows(out)
    return {"written": len(out), "path": str(path), "skipped": skipped}
