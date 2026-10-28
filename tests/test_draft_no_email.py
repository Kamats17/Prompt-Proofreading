"""Verify draft pipeline skips prospects with no email by default."""
import pytest
import json

from app.db.migrate import migrate, connect
from app.db.models import Prospect, Audit
from app.db.repo import upsert_prospect, insert_audit
from app.draft.pipeline import draft_for_prospect


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "draft_noemail.db"
    migrate(db)
    c = connect(db)
    yield c
    c.close()


def test_skips_when_no_email(conn):
    pid = upsert_prospect(conn, Prospect(
        company="X", url="https://x.com", source="manual",
        # email omitted on purpose
    ))
    insert_audit(conn, Audit(
        prospect_id=pid, issues_json=json.dumps([]),
        icp_score=80, final_grade="A",
    ))
    result = draft_for_prospect(conn, pid)
    assert result["ok"] is False
    assert "no_email" in result.get("skipped", "")
