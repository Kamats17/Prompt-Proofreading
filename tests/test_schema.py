"""Smoke test: schema migrates clean, basic CRUD round-trips."""
import json
import pytest

from app.db.migrate import migrate, connect
from app.db.models import Prospect, Audit, Draft
from app.db.repo import (
    upsert_prospect, get_prospect, insert_audit, latest_audit_for_prospect,
    insert_draft, get_draft, update_draft_status,
)


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "schema.db"
    migrate(db)
    c = connect(db)
    yield c
    c.close()


def test_prospect_upsert_dedupes_by_url(conn):
    pid1 = upsert_prospect(conn, Prospect(company="Acme", url="https://acme.com",
                                          source="manual"))
    pid2 = upsert_prospect(conn, Prospect(company="Acme Widgets",
                                          url="https://acme.com",
                                          industry="Manufacturing",
                                          source="manual"))
    assert pid1 == pid2
    p = get_prospect(conn, pid1)
    assert p.company == "Acme Widgets"
    assert p.industry == "Manufacturing"


def test_audit_and_draft_lifecycle(conn):
    pid = upsert_prospect(conn, Prospect(company="X", url="https://x.com", source="manual"))
    aid = insert_audit(conn, Audit(
        prospect_id=pid, issues_json=json.dumps([]),
        icp_score=80, final_grade="A", model_id="claude-sonnet-4-6",
    ))
    a = latest_audit_for_prospect(conn, pid)
    assert a.id == aid

    did = insert_draft(conn, Draft(
        prospect_id=pid, audit_id=aid,
        subject="Quick idea for X",
        body_text="hi", body_html="<p>hi</p>",
        qa_pass=1, status="pending",
    ))
    update_draft_status(conn, did, "pushed",
                       gmail_draft_id="gmail-abc", gmail_thread_id="thr-1")
    d = get_draft(conn, did)
    assert d.status == "pushed"
    assert d.gmail_draft_id == "gmail-abc"
