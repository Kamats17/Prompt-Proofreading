"""Draft dedup guard (Change 8) and recontact suppression (Logic Fix 3)."""
import json
import pytest

from app.db.migrate import migrate, connect
from app.db.models import Prospect, Audit, Draft
from app.db.repo import upsert_prospect, insert_audit, insert_draft, recently_contacted
from app.draft.pipeline import draft_for_prospect
from app.draft.qa_gate import run_qa
from app.draft.voice import pick_subject


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "dedup.db"
    migrate(db)
    c = connect(db)
    yield c
    c.close()


def test_second_draft_blocked_by_dedup_guard(conn):
    pid = upsert_prospect(conn, Prospect(company="X", url="https://x.com",
                                         source="manual", email="mary@x.com"))
    aid = insert_audit(conn, Audit(prospect_id=pid, issues_json="[]",
                                   icp_score=80, final_grade="A"))
    insert_draft(conn, Draft(prospect_id=pid, audit_id=aid, subject="s",
                             body_text="b", body_html="<p>b</p>", status="pending"))
    result = draft_for_prospect(conn, pid)
    assert result["ok"] is False
    assert "already exists" in result.get("skipped", "")


def test_recently_contacted_same_domain(conn):
    # Prospect A on bobsdiner.com was pushed; a NEW prospect on the same domain
    # must be flagged by the recontact check.
    a = upsert_prospect(conn, Prospect(company="Bob A", url="https://bobsdiner.com",
                                       source="manual", email="a@bobsdiner.com"))
    aid = insert_audit(conn, Audit(prospect_id=a, issues_json="[]",
                                   icp_score=80, final_grade="A"))
    insert_draft(conn, Draft(prospect_id=a, audit_id=aid, subject="s",
                             body_text="b", body_html="<p>b</p>", status="pushed"))
    reason = recently_contacted(conn, email=None, domain="bobsdiner.com",
                                exclude_prospect_id=999)
    assert reason is not None


def test_recontact_blocks_in_qa(conn):
    a = upsert_prospect(conn, Prospect(company="Bob A", url="https://bobsdiner.com",
                                       source="manual", email="a@bobsdiner.com"))
    aid = insert_audit(conn, Audit(prospect_id=a, issues_json="[]",
                                   icp_score=80, final_grade="A"))
    insert_draft(conn, Draft(prospect_id=a, audit_id=aid, subject="s",
                             body_text="b", body_html="<p>b</p>", status="pushed"))
    # A different prospect on the same domain.
    b = upsert_prospect(conn, Prospect(company="Bob B", url="https://bobsdiner.com/menu",
                                       source="manual", email="b@bobsdiner.com"))
    issues = json.dumps([{"type": "x", "quote": "old copy here",
                          "confidence": "verified", "why_hurts": "y", "service": "website"}])
    body = ("Hi team,\n\nlong enough body to pass the word count check with a verified "
            "quote \"old copy here\" mentioned and a link to "
            "https://promptproofreading.com/free-review to review it further today please. "
            "one more sentence here to be safe on the count and reach ninety words total "
            "so the check does not fail for length reasons in this recontact test case now.\n\n"
            "Thanks,\nChase")
    result = run_qa(conn, company="Bob B", prospect_email="b@bobsdiner.com",
                    prospect_url="https://bobsdiner.com/menu",
                    subject=pick_subject("Bob B", None), body_text=body,
                    audit_issues_json=issues, final_grade="A", prospect_id=b)
    assert not result["pass"]
    assert any("recently contacted" in x.lower() for x in result["blockers"])
