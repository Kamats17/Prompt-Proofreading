"""Manual ingest: paste URL or list of URLs (newline or comma separated)."""
import sqlite3
from app.db.models import Prospect
from app.db.repo import upsert_prospect
from app.safety.aggregator_filter import is_aggregator
from app.sources._normalize import normalize_url, clean_company


def ingest_manual(conn: sqlite3.Connection, raw_input: str,
                  *, company: str | None = None,
                  industry: str | None = None,
                  state: str | None = None,
                  allow_aggregators: bool = False) -> list[int]:
    """Parse newline/comma-separated URLs, upsert prospects. Returns inserted/updated ids.

    Aggregator URLs (Yelp, RestaurantJi, FB pages, etc.) are skipped unless
    `allow_aggregators=True` is passed (rare manual override).
    """
    parts = [p.strip() for p in raw_input.replace(",", "\n").splitlines() if p.strip()]
    ids: list[int] = []
    for raw in parts:
        url = normalize_url(raw)
        if not url:
            continue
        if not allow_aggregators:
            agg, _reason = is_aggregator(url)
            if agg:
                continue
        derived_company = clean_company(company, url)
        p = Prospect(
            company=derived_company,
            url=url,
            industry=industry,
            state=state,
            source="manual",
        )
        ids.append(upsert_prospect(conn, p))
    return ids
