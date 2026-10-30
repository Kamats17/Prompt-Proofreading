"""Find local businesses with web search (Claude + WebSearch, on the local CLI login).

Fills the gaps OpenStreetMap leaves in small towns. Nothing returned here is
trusted: every URL is filtered like an OSM result and then fetched by prescreen,
so a wrong or invented address just fails the prescreen and is dropped.
"""
import json
import re
from urllib.parse import urlparse

from app._claude_client import call_claude
from app.safety.aggregator_filter import is_aggregator
from app.settings import MODEL_AUDIT
from app.sources._normalize import normalize_phone, normalize_url
from app.sources.from_osm import _CHAIN_PATH_WORDS, CATEGORIES

SYSTEM = """You find small, independent local businesses and their own websites.
Use web search. Reply with ONLY a JSON array, no prose:
[{"name": "...", "website": "https://...", "city": "...", "phone": "..."}]
Rules:
- website must be the business's OWN domain, copied exactly from a search result.
  Never a directory, review, social or booking page (Yelp, Angi, BBB, Facebook,
  Google, Healthgrades, Zocdoc, Houzz, Thumbtack, YellowPages, Nextdoor...).
- Skip chains, franchises, hospital systems, and anyone without their own site.
- Never guess or construct a URL. If unsure of a website, leave the business out.
- Prefer smaller, older, family-run businesses over polished multi-location ones."""


def _parse(text: str) -> list[dict]:
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        return []
    try:
        rows = json.loads(m.group(0))
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict)]


def discover_web(area: str, state: str, types: list[str], *, per_type: int = 20) -> tuple[list[dict], dict]:
    out, seen = [], set()
    stats = {"web_found": 0, "web_rejected": 0, "web_errors": 0}
    for t in types:
        label = CATEGORIES[t][2]
        user = (f"List up to {per_type} independent {label} businesses in {area}, {state}, "
                f"each with its own website. Search more than once if needed.")
        try:
            rows = _parse(call_claude(system=SYSTEM, user=user, model=MODEL_AUDIT,
                                      tools=("WebSearch",), max_turns=8, timeout_s=420))
        except Exception:
            stats["web_errors"] += 1
            continue
        stats["web_found"] += len(rows)
        for r in rows:
            url = normalize_url(str(r.get("website") or ""))
            name = str(r.get("name") or "").strip()
            path = (urlparse(url).path or "").lower() if url else ""
            if (not url or not name or is_aggregator(url)[0]
                    or any(w in path for w in _CHAIN_PATH_WORDS)):
                stats["web_rejected"] += 1
                continue
            host = (urlparse(url).hostname or "").removeprefix("www.")
            if host in seen:
                continue
            seen.add(host)
            out.append({"company": name, "url": url, "industry": label,
                        "city": (str(r.get("city") or "").strip() or None),
                        "state": state.upper(),
                        "phone": normalize_phone(str(r.get("phone") or ""))})
    return out, stats
