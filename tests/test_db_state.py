"""Migration of old DB layouts, duplicate-draft backstop, guarded transitions."""
import sqlite3

import pytest

from app.db.migrate import migrate, connect
from app.db.models import Prospect, Audit, Draft
from app.db.repo import (
    upsert_prospect, insert_audit, insert_draft, transition_draft, get_draft,
    DuplicateDraftError, record_audit_failure, get_prospect,
)

V1_SCHEMA = """
CREATE TABLE prospects (id INTEGER PRIMARY KEY AUTOINCREMENT, company TEXT NOT NULL,
  url TEXT NOT NULL UNIQUE, industry TEXT, state TEXT, email TEXT, phone TEXT,
  source TEXT NOT NULL, node_rating TEXT, node_issues_json TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE audits (id INTEGER PRIMARY KEY AUTOINCREMENT, prospect_id INTEGER NOT NULL,
  screenshot_path TEXT, html_cache_path TEXT, issues_json TEXT NOT NULL, service_angle TEXT,
  demo_angle TEXT, icp_score INTEGER, final_grade TEXT, model_id TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE drafts (id INTEGER PRIMARY KEY AUTOINCREMENT, prospect_id INTEGER NOT NULL,
  audit_id INTEGER NOT NULL, subject TEXT NOT NULL, body_text TEXT NOT NULL, body_html TEXT NOT NULL,
  gmail_draft_id TEXT, gmail_thread_id TEXT, qa_pass INTEGER NOT NULL DEFAULT 0, qa_blockers_json TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','pushed','error')),
  reject_reason TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE dnc_list (id INTEGER PRIMARY KEY AUTOINCREMENT, company TEXT, domain TEXT, email TEXT,
  reason TEXT, added_at TEXT NOT NULL DEFAULT (datetime('now')), added_by TEXT);
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, prospect_id INTEGER, draft_id INTEGER,
  event_type TEXT NOT NULL, payload_json TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')));
INSERT INTO prospects (company, url, source) VALUES ('Old Co', 'https://old.com', 'manual');
INSERT INTO audits (prospect_id, issues_json, final_grade) VALUES (1, '[]', 'A');
INSERT INTO drafts (prospect_id, audit_id, subject, body_text, body_html, status)
  VALUES (1, 1, 'subj', 'body', '<p>body</p>', 'pushed');
INSERT INTO events (prospect_id, draft_id, event_type) VALUES (1, 1, 'approved_pushed');
"""


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "s.db"
    migrate(db)
    c = connect(db)
    yield c
    c.close()


def test_migrates_v1_db_with_existing_drafts(tmp_path):
    db = tmp_path / "v1.db"
    raw = sqlite3.connect(db)
    raw.executescript(V1_SCHEMA)
    raw.close()
    migrate(db)
    migrate(db)  # idempotent
    c = connect(db)
    d = get_draft(c, 1)
    assert d.status == "pushed" and d.subject == "subj" and d.touch == 1
    # Widened CHECK: the statuses added later are now writable.
    assert transition_draft(c, 1, ("pushed",), "sent", sent_at="now")
    assert get_draft(c, 1).status == "sent"
    # Events still reference the rebuilt drafts table.
    assert c.execute("SELECT draft_id FROM events").fetchone()[0] == 1
    assert get_prospect(c, 1).audit_attempts == 0


def test_db_refuses_second_live_draft(conn):
    pid = upsert_prospect(conn, Prospect(company="X", url="https://x.com", source="manual"))
    aid = insert_audit(conn, Audit(prospect_id=pid, issues_json="[]", final_grade="A"))
    insert_draft(conn, Draft(prospect_id=pid, audit_id=aid, subject="s", body_text="b", body_html="b"))
    with pytest.raises(DuplicateDraftError):
        insert_draft(conn, Draft(prospect_id=pid, audit_id=aid, subject="s2", body_text="b", body_html="b"))
    # A rejected draft frees the slot; a follow-up (touch 2) has its own slot.
    insert_draft(conn, Draft(prospect_id=pid, audit_id=aid, touch=2, subject="Re: s",
                             body_text="b", body_html="b"))


def test_transition_guards(conn):
    pid = upsert_prospect(conn, Prospect(company="X", url="https://x.com", source="manual"))
    aid = insert_audit(conn, Audit(prospect_id=pid, issues_json="[]", final_grade="A"))
    did = insert_draft(conn, Draft(prospect_id=pid, audit_id=aid, subject="s", body_text="b", body_html="b"))
    assert transition_draft(conn, did, ("pending",), "approved")
    assert not transition_draft(conn, did, ("pending",), "approved")      # double click
    assert not transition_draft(conn, did, ("pending", "error"), "rejected")  # can't reject in flight
    assert transition_draft(conn, did, ("approved",), "pushed", gmail_draft_id="g1")
    assert transition_draft(conn, did, ("pushed",), "sent", sent_at="now")
    assert get_draft(conn, did).sent_at is not None
    with pytest.raises(ValueError):
        transition_draft(conn, did, ("pushed",), "sent", body_text="x")


def test_audit_failure_tracking(conn):
    pid = upsert_prospect(conn, Prospect(company="X", url="https://x.com", source="manual"))
    record_audit_failure(conn, pid, "timeout")
    record_audit_failure(conn, pid, "timeout")
    p = get_prospect(conn, pid)
    assert p.audit_attempts == 2 and p.last_audit_error == "timeout"
