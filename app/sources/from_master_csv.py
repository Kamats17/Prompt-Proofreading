"""Ingest the existing master prospect CSV.

Source: ALL-STATES-MASTER-CLEAN.csv at Proofreading Prospects/.
Columns: Business Name, Business Type, City, State, Phone, Email, Contact Name, Website URL, Missing Feature, Notes
"""
import sqlite3
import json
import csv
from pathlib import Path

from app.db.models import Prospect
from app.db.repo import upsert_prospect
from app.draft.name_guess import looks_like_person_name
from app.safety.aggregator_filter import is_aggregator
from app.sources._normalize import (
    normalize_url, normalize_email, normalize_phone, clean_company,
    is_non_us_domain, is_us_state,
)


def read_csv_rows(csv_path: str | Path) -> list[dict]:
    """Read a CSV exported by Excel or anything else: tolerate a UTF-8 BOM, fall
    back to Windows-1252, and strip whitespace/BOMs from header names."""
    csv_path = Path(csv_path)
    last_err: Exception | None = None
    for enc in ("utf-8-sig", "cp1252"):
        try:
            with open(csv_path, encoding=enc, newline="") as f:
                reader = csv.DictReader(f)
                rows = []
                for row in reader:
                    rows.append({(k or "").strip().lstrip("﻿"): (v or "")
                                 for k, v in row.items() if k is not None})
                return rows
        except UnicodeDecodeError as e:
            last_err = e
    raise last_err  # type: ignore[misc]


def ingest_master_csv(conn: sqlite3.Connection, csv_path: str | Path,
                      *, limit: int | None = None) -> dict:
    """Ingest rows. Returns import stats."""
    stats = {"imported": 0, "skipped_no_url": 0, "skipped_aggregator": 0,
             "skipped_non_us": 0, "contact_names_rejected": 0}
    for row in read_csv_rows(csv_path):
        if limit is not None and stats["imported"] >= limit:
            break
        url = normalize_url(row.get("Website URL", ""))
        if not url:
            stats["skipped_no_url"] += 1
            continue
        if is_aggregator(url)[0]:
            stats["skipped_aggregator"] += 1
            continue
        state = (row.get("State", "") or "").strip() or None
        if is_non_us_domain(url) or is_us_state(state) is False:
            stats["skipped_non_us"] += 1
            continue
        issues = row.get("Missing Feature", "") or row.get("Notes", "")
        raw_contact = (row.get("Contact Name", "") or "").strip()
        contact = raw_contact if looks_like_person_name(raw_contact) else None
        if raw_contact and not contact:
            stats["contact_names_rejected"] += 1
        email_val = normalize_email(row.get("Email", ""))
        p = Prospect(
            company=clean_company(row.get("Business Name", ""), url),
            url=url,
            industry=(row.get("Business Type", "") or "").strip() or None,
            city=(row.get("City", "") or "").strip() or None,
            state=state,
            email=email_val,
            email_source="csv" if email_val else None,
            contact_name=contact,
            phone=normalize_phone(row.get("Phone", "")),
            source="master_csv",
            node_issues_json=json.dumps([issues]) if issues else None,
        )
        upsert_prospect(conn, p)
        stats["imported"] += 1
    return stats
