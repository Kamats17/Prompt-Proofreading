"""Thin convenience wrapper over repo.log_event for clarity at call sites."""
import json
import sqlite3
from typing import Any
from app.db.models import Event
from app.db.repo import log_event


def log(conn: sqlite3.Connection, event_type: str, *,
        prospect_id: int | None = None,
        draft_id: int | None = None,
        payload: dict[str, Any] | None = None) -> int:
    return log_event(conn, Event(
        prospect_id=prospect_id,
        draft_id=draft_id,
        event_type=event_type,
        payload_json=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
    ))
