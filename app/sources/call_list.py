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
