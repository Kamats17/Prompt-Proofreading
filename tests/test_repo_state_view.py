"""Verify list_prospects_with_state computes derived status correctly."""
import json
import pytest

from app.db.migrate import migrate, connect
from app.db.models import Prospect, Audit, Draft
from app.db.repo import (
    upsert_prospect, insert_audit, insert_draft,
    list_prospects_with_state,
)


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "state.db"
    migrate(db)
    c = connect(db)
    yield c
    c.close()


def test_status_ingested_only(conn):
    upsert_prospect(conn, Prospect(company="A", url="https://a.com", source="manual"))
    rows = list_prospects_with_state(conn)
    assert len(rows) == 1
    assert rows[0]["status"] == "ingested"


def test_status_audited_only(conn):
    pid = upsert_prospect(conn, Prospect(company="B", url="https://b.com", source="manual"))
    insert_audit(conn, Audit(prospect_id=pid, issues_json="[]",
                              icp_score=80, final_grade="A"))
    rows = list_prospects_with_state(conn, grade="A")
    assert rows[0]["status"] == "audited"
    assert rows[0]["final_grade"] == "A"


def test_status_drafted_pending(conn):
    pid = upsert_prospect(conn, Prospect(company="C", url="https://c.com", source="manual"))
    aid = insert_audit(conn, Audit(prospect_id=pid, issues_json="[]",
                                    icp_score=80, final_grade="A"))
    insert_draft(conn, Draft(prospect_id=pid, audit_id=aid,
                              subject="x", body_text="y", body_html="<p>y</p>",
                              qa_pass=1, status="pending"))
    rows = list_prospects_with_state(conn, status="pending")
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"


def test_filter_by_email_presence(conn):
    upsert_prospect(conn, Prospect(company="D", url="https://d.com", source="manual",
                                    email="d@d.com"))
    upsert_prospect(conn, Prospect(company="E", url="https://e.com", source="manual"))
    yes = list_prospects_with_state(conn, has_email="yes")
    no = list_prospects_with_state(conn, has_email="no")
    assert len(yes) == 1 and yes[0]["company"] == "D"
    assert len(no) == 1 and no[0]["company"] == "E"
