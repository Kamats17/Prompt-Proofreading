"""Discover -> prescreen -> ingest. Only sites that look old reach the audit queue.

Funnel per area:
  1. OSM lists independent local businesses that have a website (free, no key);
     web search (Claude + WebSearch) fills in what OSM is missing, mostly in
     small towns where few businesses are mapped.
  2. prescreen fetches each site once over plain HTTP (no browser, no Claude):
     alive? email we may use? how many signs of an old site?
  3. Old + email   -> ingested as a normal lead (audited first: it has an email).
     Old, no email -> ingested blocked 'no_email' so it lands on the call list.
     Looks current, dead, or redirected -> not ingested.
Every site checked is remembered in prescreen/seen.json, so re-running an area
or an overlapping one never fetches the same site twice.
"""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from app.db.models import Prospect
from app.db.repo import mark_prospect_blocked, upsert_prospect
from app.sources import from_osm, from_websearch
from app.sources._normalize import clean_company, registered_domain
from app.sources.prescreen import CACHE_DIR, prescreen

SEEN_PATH = CACHE_DIR / "seen.json"
DEFAULT_MIN_STALE = 3


def _load_seen() -> dict:
    try:
        return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_seen(seen: dict) -> None:
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen, indent=0, sort_keys=True), encoding="utf-8")


def _known_domains(conn: sqlite3.Connection) -> set[str]:
    return {registered_domain(r[0]) for r in conn.execute("SELECT url FROM prospects")}


def ingest_screened(conn: sqlite3.Connection, cand: dict, res: dict, *,
                    min_stale: int = DEFAULT_MIN_STALE) -> str:
    """Store one prescreened candidate. Returns the outcome key."""
    if not res.get("ok"):
        return "dead_or_moved"
    if res["stale"] < min_stale:
        return "looks_current"
    phone = cand.get("phone") or res.get("phone")
    if not res.get("email") and not phone:
        return "no_contact"
    p = Prospect(
        company=clean_company(cand["company"], cand["url"]), url=cand["url"],
        industry=cand.get("industry"), city=cand.get("city"), state=cand.get("state"),
        email=res.get("email"), email_source=res.get("email_source"), phone=phone,
        source="osm", node_issues_json=json.dumps(res["why"]),
    )
    pid = upsert_prospect(conn, p)
    if not res.get("email"):
        mark_prospect_blocked(conn, pid, "no_email")
        return "call_list"
    return "lead"


def discover(area: str, state: str, types: list[str] | None,
             sources: tuple[str, ...], log=print) -> tuple[list[dict], dict]:
    """Candidates from every source, one per registered domain (OSM first: its
    name, phone and address come from the map, not from a model)."""
    types = list(types or from_osm.DEFAULT_TYPES)
    stats: dict = {"found": 0}
    cands: list[dict] = []
    if "osm" in sources:
        try:
            c, st = from_osm.discover(area, state, types)
            cands += c
            stats.update(st)
        except RuntimeError as e:
            log(f"  OSM skipped: {e}")
    if "web" in sources:
        c, st = from_websearch.discover_web(area, state, types)
        cands += c
        stats.update(st)
    out, domains = [], set()
    for c in cands:
        d = registered_domain(c["url"])
        if d not in domains:
            domains.add(d)
            out.append(c)
    return out, stats


def prospect_area(conn: sqlite3.Connection, *, area: str, state: str,
                  types: list[str] | None = None, min_stale: int = DEFAULT_MIN_STALE,
                  sources: tuple[str, ...] = ("osm", "web"), workers: int = 8,
                  log=print) -> dict:
    cands, stats = discover(area, state, types, sources, log)
    known, seen = _known_domains(conn), _load_seen()
    fresh = [c for c in cands
             if registered_domain(c["url"]) not in known and registered_domain(c["url"]) not in seen]
    stats.update({"with_site": len(cands), "already_known": len(cands) - len(fresh),
                  "lead": 0, "call_list": 0, "looks_current": 0, "dead_or_moved": 0,
                  "no_contact": 0})
    log(f"{area}, {state}: {len(cands)} independent businesses with a site, "
        f"{len(fresh)} new to check")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(prescreen, c["url"], c["company"]): c for c in fresh}
        for fut in as_completed(futures):
            cand = futures[fut]
            try:
                res = fut.result()
            except Exception as e:  # one odd site never stops the run
                res = {"ok": False, "error": f"{type(e).__name__}"}
            outcome = ingest_screened(conn, cand, res, min_stale=min_stale)
            stats[outcome] += 1
            seen[registered_domain(cand["url"])] = {
                "d": date.today().isoformat(), "o": outcome, "s": res.get("stale")}
            if outcome in ("lead", "call_list"):
                log(f"  + {cand['company']} ({cand.get('industry')}) -> {outcome}: "
                    f"{'; '.join(res['why'])}")
    _save_seen(seen)
    return stats
