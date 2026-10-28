"""Ingest XLSX files dropped into data/leads_in/ or from Proofreading Prospects/.

Tolerant column mapping: handles variants from different prospecting runs.
"""
import sqlite3
import json
from pathlib import Path
import pandas as pd

from app.db.models import Prospect
from app.db.repo import upsert_prospect
from app.draft.name_guess import looks_like_person_name
from app.safety.aggregator_filter import is_aggregator
from app.sources._normalize import (
    normalize_url, normalize_email, normalize_phone, clean_company,
    is_non_us_domain, is_us_state,
)

# Column-name aliases. Lowercase keys; first match wins.
_COL_ALIASES = {
    "company": ["business name", "company", "name", "company name"],
    "url": ["website url", "url", "website", "site", "homepage"],
    "industry": ["business type", "industry", "category", "type"],
    "city": ["city", "town"],
    "state": ["state", "st", "region"],
    "email": ["email", "email address", "contact email"],
    "contact_name": ["contact name", "contact", "owner", "owner name", "contact person"],
    "phone": ["phone", "phone number", "contact phone", "tel"],
    "node_rating": ["rating", "grade"],
    "issues": ["missing feature", "notes", "issues", "problems", "issue list"],
}


def _normalize_rating(raw: str) -> str | None:
    r = (raw or "").strip().upper()
    if r in {"A", "B", "C"}:
        return r
    if r.startswith("REJECT"):
        return "Reject"
    return None


def _pick(row: dict, key: str) -> str:
    for alias in _COL_ALIASES.get(key, [key]):
        for col, val in row.items():
            if col.strip().lstrip("﻿").lower() == alias and val is not None and str(val).strip():
                return str(val).strip()
    return ""


def _empty_stats() -> dict:
    return {"imported": 0, "skipped_no_url": 0, "skipped_aggregator": 0,
            "skipped_non_us": 0, "contact_names_rejected": 0}


def ingest_xlsx(conn: sqlite3.Connection, xlsx_path: str | Path,
                *, sheet: str | int | None = None,
                limit: int | None = None,
                source: str = "xlsx") -> dict:
    """Ingest one xlsx file (optionally one sheet). Returns import stats.

    Pass `source='node_discovery'` from the node-wrapper so prospects are
    correctly tagged at insert time.
    """
    xlsx_path = Path(xlsx_path)
    stats = _empty_stats()
    if sheet is None:
        try:
            sheets = pd.ExcelFile(xlsx_path).sheet_names
        except Exception as e:
            return {**stats, "error": str(e)}
    else:
        sheets = [sheet]
    for sh in sheets:
        try:
            df = pd.read_excel(xlsx_path, sheet_name=sh, dtype=str)
        except Exception as e:
            stats.setdefault("sheet_errors", []).append(f"{sh}: {e}"[:200])
            continue
        df = df.fillna("")
        for _, raw_row in df.iterrows():
            if limit is not None and stats["imported"] >= limit:
                return stats
            row = {str(k): v for k, v in raw_row.to_dict().items()}
            url = normalize_url(_pick(row, "url"))
            if not url:
                stats["skipped_no_url"] += 1
                continue
            if is_aggregator(url)[0]:
                stats["skipped_aggregator"] += 1
                continue
            state = _pick(row, "state") or None
            if is_non_us_domain(url) or is_us_state(state) is False:
                stats["skipped_non_us"] += 1
                continue
            raw_contact = _pick(row, "contact_name")
            contact = raw_contact if looks_like_person_name(raw_contact) else None
            if raw_contact and not contact:
                stats["contact_names_rejected"] += 1
            issues = _pick(row, "issues")
            email_val = normalize_email(_pick(row, "email"))
            p = Prospect(
                company=clean_company(_pick(row, "company"), url),
                url=url,
                industry=_pick(row, "industry") or None,
                city=_pick(row, "city") or None,
                state=state,
                email=email_val,
                email_source="csv" if email_val else None,
                contact_name=contact,
                phone=normalize_phone(_pick(row, "phone")),
                source=source,
                node_rating=_normalize_rating(_pick(row, "node_rating")),
                node_issues_json=json.dumps([issues]) if issues else None,
            )
            upsert_prospect(conn, p)
            stats["imported"] += 1
    return stats


def ingest_xlsx_dir(conn: sqlite3.Connection, dir_path: str | Path,
                    *, limit_per_file: int | None = None,
                    source: str = "xlsx") -> dict:
    """Ingest every .xlsx in a directory."""
    dir_path = Path(dir_path)
    totals = _empty_stats()
    files = [f for f in sorted(dir_path.glob("*.xlsx")) if not f.name.startswith("~$")]
    for f in files:
        stats = ingest_xlsx(conn, f, limit=limit_per_file, source=source)
        for k in totals:
            totals[k] += stats.get(k, 0)
    return {**totals, "files": len(files)}
