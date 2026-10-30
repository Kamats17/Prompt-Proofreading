"""Find local businesses with websites from OpenStreetMap (Overpass API, no key).

Only independent businesses come back: anything tagged with a brand (chains,
franchises) or listed on a chain's location page is skipped. OSM is the business
list only; whether the site is old and has an email is decided by prescreen.
"""
import re
import time
from urllib.parse import urlparse

import requests

from app.safety.aggregator_filter import is_aggregator
from app.sources._normalize import normalize_phone, normalize_url

ENDPOINTS = ("https://overpass-api.de/api/interpreter",
             "https://overpass.private.coffee/api/interpreter",
             "https://overpass.kumi.systems/api/interpreter")
UA = "pp-agent/0.1 (local business website research)"

# OSM tag -> industry label. Labels are worded so grader.HIGH_TICKET matches them.
CATEGORIES: dict[str, tuple[str, str, str]] = {
    # name: (key, value regex, industry label)
    "dentist": ("amenity", "^dentist$", "Dentist"),
    "veterinary": ("amenity", "^veterinary$", "Veterinarian"),
    "chiropractor": ("healthcare", "^chiropractor$", "Chiropractor"),
    "physio": ("healthcare", "^physiotherapist$", "Physical Therapy Clinic"),
    "optometrist": ("healthcare", "^optometrist$", "Optometry Clinic"),
    "plumber": ("craft", "^plumber$", "Plumber"),
    "hvac": ("craft", "^hvac$", "HVAC"),
    "electrician": ("craft", "^electrician$", "Electrician"),
    "roofer": ("craft", "^roofer$", "Roofer"),
    "builder": ("craft", "^(builder|carpenter)$", "General Contractor"),
    "painter": ("craft", "^painter$", "Painting Contractor"),
    "landscaper": ("craft", "^gardener$", "Landscaping"),
    "lawyer": ("office", "^lawyer$", "Law Firm"),
    "accountant": ("office", "^(accountant|tax_advisor)$", "Accountant"),
    "insurance": ("office", "^insurance$", "Insurance Agency"),
    "realtor": ("office", "^estate_agent$", "Realtor"),
    "auto": ("shop", "^(car_repair|tyres)$", "Auto Repair"),
}
DEFAULT_TYPES = ("dentist", "chiropractor", "physio", "optometrist", "veterinary",
                 "plumber", "hvac", "electrician", "roofer", "builder", "landscaper",
                 "lawyer", "accountant", "insurance", "auto")

_CHAIN_PATH_WORDS = ("/locations", "/location/", "/store/", "/stores/", "/offices/", "/branch")


def build_query(area: str, state: str, types: list[str]) -> str:
    """Overpass QL for every business of `types` inside the named county or town
    in `state` (two-letter code). `area` matches any admin boundary by name."""
    parts = []
    for t in types:
        key, rx, _ = CATEGORIES[t]
        parts.append(f'nwr["{key}"~"{rx}"](area.a);')
    return (f'[out:json][timeout:180];'
            f'area["ISO3166-2"="US-{state.upper()}"]->.st;'
            f'rel["boundary"="administrative"]["name"="{area}"](area.st);'
            f'map_to_area->.a;'
            f'({"".join(parts)});out tags;')


def fetch_overpass(query: str, *, attempts: int = 6) -> list[dict]:
    """Run a query, rotating endpoints with backoff: the public servers are often
    'at capacity' for a minute, rarely for long."""
    last = ""
    for i in range(attempts):
        ep = ENDPOINTS[i % len(ENDPOINTS)]
        try:
            r = requests.post(ep, data={"data": query}, headers={"User-Agent": UA}, timeout=200)
            if r.status_code == 200:
                return r.json().get("elements", [])
            last = f"{ep}: http {r.status_code}"
        except (requests.RequestException, ValueError) as e:
            last = f"{ep}: {type(e).__name__}"
        time.sleep(min(60, 10 * (i + 1)))
    raise RuntimeError(f"Overpass unavailable after {attempts} tries ({last})")


def _industry(tags: dict, types: list[str]) -> str | None:
    for t in types:
        key, rx, label = CATEGORIES[t]
        if re.search(rx, tags.get(key, "")):
            return label
    return None


def candidates_from_elements(elements: list[dict], types: list[str],
                             default_state: str) -> tuple[list[dict], dict]:
    """Independent businesses with a usable website, one per site."""
    out, seen = [], set()
    skipped = {"no_website": 0, "chain": 0, "aggregator": 0, "duplicate": 0}
    for el in elements:
        tags = el.get("tags") or {}
        name = (tags.get("name") or "").strip()
        url = normalize_url(tags.get("website") or tags.get("contact:website") or tags.get("url") or "")
        if not name or not url:
            skipped["no_website"] += 1
            continue
        path = (urlparse(url).path or "").lower()
        if (tags.get("brand") or tags.get("brand:wikidata") or tags.get("operator:wikidata")
                or any(w in path for w in _CHAIN_PATH_WORDS)):
            skipped["chain"] += 1
            continue
        if is_aggregator(url)[0]:
            skipped["aggregator"] += 1
            continue
        host = (urlparse(url).hostname or "").removeprefix("www.")
        if host in seen:
            skipped["duplicate"] += 1
            continue
        seen.add(host)
        out.append({
            "company": name, "url": url, "industry": _industry(tags, types),
            "city": tags.get("addr:city"),
            "state": (tags.get("addr:state") or default_state).upper()[:2],
            "phone": normalize_phone(tags.get("phone") or tags.get("contact:phone") or ""),
        })
    return out, skipped


def discover(area: str, state: str, types: list[str] | None = None) -> tuple[list[dict], dict]:
    types = list(types or DEFAULT_TYPES)
    unknown = [t for t in types if t not in CATEGORIES]
    if unknown:
        raise ValueError(f"unknown types {unknown}; choose from {sorted(CATEGORIES)}")
    elements = fetch_overpass(build_query(area, state, types))
    cands, skipped = candidates_from_elements(elements, types, state)
    skipped["found"] = len(elements)
    return cands, skipped
