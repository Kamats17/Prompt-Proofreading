"""First-touch drafting end-to-end with Claude mocked: subject from finding,
greeting fallback, QA retry on a bad first attempt, evidence persisted."""
import json

import pytest

from app.db.migrate import migrate, connect
from app.db.models import Prospect, Audit
from app.db.repo import upsert_prospect, insert_audit, get_draft
from app.draft import generator
from app.draft.pipeline import draft_for_prospect, resolve_greeting

ISSUES = [
    {"type": "phone_not_tappable", "evidence_kind": "fact", "fact_id": "phone_not_tappable",
     "quote": "", "fact_detail": "The phone number is plain text.", "confidence": "verified",
     "customer_moment": "On a phone the number can't be tapped.", "fix": "tap-to-call",
     "service": "website", "severity": 3},
    {"type": "typo", "evidence_kind": "quote", "quote": "Call to place a order",
     "confidence": "verified", "service": "content", "severity": 2},
]
GOOD = {"body_text": ("Hi there,\n\nI pulled up Frank's site on my phone and the number at the top "
                      "can't be tapped to call, so customers have to copy it by hand.\n\n"
                      "I'd turn it into a one-tap button and tidy the line \"Call to place a order\" "
                      "too. I do this kind of fix for local restaurants.\n\n"
                      "Want me to send a free mockup of the header?\n\nThanks,\nChase"),
        "used_quotes": ["Call to place a order"], "used_fact_ids": ["phone_not_tappable"]}


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "d.db"
    migrate(db)
    c = connect(db)
    yield c
    c.close()


def _seed(conn, email="info@franksdiner.com"):
    pid = upsert_prospect(conn, Prospect(company="Frank's Diner", url="https://franksdiner.com",
                                         source="manual", email=email, industry="restaurant"))
    insert_audit(conn, Audit(prospect_id=pid, issues_json=json.dumps(ISSUES), final_grade="A",
                             icp_score=75, demo_angle="a tap-to-call header mockup",
                             meta_json=json.dumps({"owner_first_name": ""})))
    return pid


def test_draft_end_to_end(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(generator, "call_claude", lambda **kw: calls.append(kw) or json.dumps(GOOD))
    pid = _seed(conn)
    res = draft_for_prospect(conn, pid)
    assert res["ok"] and res["qa_pass"], res
    d = get_draft(conn, res["draft_id"])
    assert "Frank's" in d.subject and "phone" in d.subject
    assert d.finding_type == "phone_not_tappable"
    assert json.loads(d.evidence_json)["used_fact_ids"] == ["phone_not_tappable"]
    assert "promptproofreading.com/free-review" not in d.body_html     # no CTA link in first touch
    payload = json.loads(calls[0]["user"])
    assert payload["greeting_name"] == "there"                          # info@ -> no fake name
    assert payload["subject"] == d.subject


def test_draft_retries_once_after_qa_failure(conn, monkeypatch):
    bad = {**GOOD, "body_text": GOOD["body_text"].replace("Want me", "See https://x.com. Want me")}
    outputs = iter([json.dumps(bad), json.dumps(GOOD)])
    seen = []
    monkeypatch.setattr(generator, "call_claude", lambda **kw: seen.append(kw) or next(outputs))
    res = draft_for_prospect(conn, _seed(conn))
    assert res["retried"] and res["qa_pass"]
    assert "previous_attempt_failed_checks" in json.loads(seen[1]["user"])


def test_second_draft_refused(conn, monkeypatch):
    monkeypatch.setattr(generator, "call_claude", lambda **kw: json.dumps(GOOD))
    pid = _seed(conn)
    assert draft_for_prospect(conn, pid)["ok"]
    assert "already exists" in draft_for_prospect(conn, pid)["skipped"]


def test_resolve_greeting_order():
    assert resolve_greeting(explicit=None, contact_name="https://restaurantji.com/x",
                            owner_first_name="", email="info@x.com") == "there"
    assert resolve_greeting(explicit=None, contact_name="Larry Miller", owner_first_name="",
                            email="mary@x.com") == "Larry"
    assert resolve_greeting(explicit=None, contact_name=None, owner_first_name="Dana",
                            email="ckamats@x.com") == "Dana"
