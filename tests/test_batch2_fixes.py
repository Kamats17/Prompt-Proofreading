"""Fixes from the second live batch: dead domains, junk designer credits, and the
longer word limit for emails that make both offers."""
import json

import pytest

from app.audit import pipeline as audit_pipeline
from app.audit.extractor import extract_features
from app.db.migrate import migrate, connect
from app.db.models import Prospect
from app.db.repo import upsert_prospect, get_prospect
from app.draft.qa_gate import run_qa
from app.draft.voice import pick_subject


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "b.db"
    migrate(db)
    c = connect(db)
    yield c
    c.close()


def test_dead_domain_blocked_once(conn, monkeypatch):
    monkeypatch.setattr(audit_pipeline, "fetch_page", lambda url, **kw: {
        "ok": False, "error": "Page.goto: net::ERR_NAME_NOT_RESOLVED at http://gone.com/"})
    pid = upsert_prospect(conn, Prospect(company="Gone Diner", url="https://gone.com", source="manual"))
    res = audit_pipeline.audit_prospect(conn, pid)
    assert res["blocked"] == "domain_dead"
    assert get_prospect(conn, pid).audit_attempts == 0        # blocked, not a retryable failure


def test_junk_designer_credit_ignored(tmp_path):
    html = tmp_path / "p.html"
    html.write_text("<html><body><p>" + "Great food daily. " * 30 + "</p>"
                    "<footer>Powered by This Site Builder. Website by Acme Web Co</footer></body></html>",
                    encoding="utf-8")
    credits = extract_features(html)["designer_credits"]
    assert "Acme Web Co" in credits and not any(c.lower().startswith("this") for c in credits)


def _body(n_words: int) -> str:
    """A body with exactly n_words words before the sign-off."""
    head = "Hi there,\n\nThe number at the top can't be tapped to call on a phone."
    tail = "\n\nWant me to send a free mockup?"
    pad = n_words - len((head + tail).split())
    return f"{head} {' '.join(['word'] * pad)}.{tail}\n\nThanks,\nChase"


def test_rebuild_emails_get_more_room(conn):
    issues = json.dumps([{"type": "phone_not_tappable", "evidence_kind": "fact",
                          "fact_id": "phone_not_tappable", "confidence": "verified"}])
    kw = dict(company="Joe's Diner", prospect_email="joe@joesdiner.com", prospect_url="https://joesdiner.com",
              subject=pick_subject("Joe's Diner", "phone_not_tappable"), body_text=_body(112),
              audit_issues_json=issues, final_grade="A", used_fact_ids=["phone_not_tappable"])
    plain = run_qa(conn, **kw, audit_facts={"phone_not_tappable": "x"})
    assert not plain["checks"]["word_count_ok"]                       # quick-fix-only email: 105 max
    rebuild = run_qa(conn, **kw, audit_facts={"phone_not_tappable": "x", "stale_copyright": "x"})
    assert rebuild["checks"]["word_count_ok"]                         # both offers: 115 max
