"""The full-rebuild offer: only when verified facts show a dated site, and the
evidence it cites passes QA even if the audit didn't list it as a finding."""
import json

import pytest

from app.audit.grader import rebuild_case
from app.db.migrate import migrate, connect
from app.db.models import Prospect, Audit
from app.db.repo import upsert_prospect, insert_audit
from app.draft import generator
from app.draft.pipeline import draft_for_prospect
from app.draft.qa_gate import run_qa
from app.draft.voice import pick_subject


def test_rebuild_needs_a_dated_signal():
    # Jethro's case: stale footer + phone + no ordering -> worthy, cites the dated signal.
    r = rebuild_case({"stale_copyright": "footer says 2021", "phone_not_tappable": "x",
                      "no_order_link_found": "x"})
    assert r["worthy"] and [s["fact_id"] for s in r["dated_signals"]] == ["stale_copyright"]
    # Modern site with two small gaps: no rebuild pitch.
    assert not rebuild_case({"phone_not_tappable": "x", "no_order_link_found": "x"})["worthy"]
    # One strong signal is enough on its own.
    assert rebuild_case({"desktop_layout_on_phone": "zoomed out"})["worthy"]
    assert not rebuild_case({})["worthy"]


def test_qa_accepts_code_facts_not_listed_as_findings(tmp_path):
    db = tmp_path / "q.db"
    migrate(db)
    conn = connect(db)
    issues = json.dumps([{"type": "phone_not_tappable", "evidence_kind": "fact",
                          "fact_id": "phone_not_tappable", "confidence": "verified"}])
    body = ("Hi there,\n\nI pulled up Joe's site on my phone and the number can't be tapped to call.\n\n"
            "That's a quick fix. The site also still shows 2021 in the footer, so if you've been "
            "thinking about a fresh site altogether, I build those for local restaurants too.\n\n"
            "Want me to send a free mockup of the header, plus a rough idea of what a full refresh "
            "would look like?\n\nThanks,\nChase")
    kw = dict(company="Joe's Diner", prospect_email="joe@joesdiner.com", prospect_url="https://joesdiner.com",
              subject=pick_subject("Joe's Diner", "phone_not_tappable"), body_text=body,
              audit_issues_json=issues, final_grade="A",
              used_fact_ids=["phone_not_tappable", "stale_copyright"])
    assert not run_qa(conn, **kw)["pass"]                                  # without audit facts
    assert run_qa(conn, **kw, audit_facts={"stale_copyright": "x", "phone_not_tappable": "x"})["pass"]


@pytest.mark.parametrize("facts,worthy", [
    ({"stale_copyright": "The footer still says 2021.", "phone_not_tappable": "x"}, True),
    ({"phone_not_tappable": "x"}, False),
])
def test_draft_payload_carries_rebuild_decision(tmp_path, monkeypatch, facts, worthy):
    db = tmp_path / "d.db"
    migrate(db)
    conn = connect(db)
    pid = upsert_prospect(conn, Prospect(company="Joe's Diner", url="https://joesdiner.com",
                                         source="manual", email="joe@joesdiner.com", industry="restaurant"))
    insert_audit(conn, Audit(prospect_id=pid, final_grade="A", icp_score=70, demo_angle="a header mockup",
                             issues_json=json.dumps([{"type": "phone_not_tappable", "evidence_kind": "fact",
                                                      "fact_id": "phone_not_tappable", "confidence": "verified",
                                                      "severity": 3}]),
                             meta_json=json.dumps({"facts": facts})))
    seen = []
    monkeypatch.setattr(generator, "call_claude", lambda **kw: seen.append(json.loads(kw["user"])) or
                        json.dumps({"error": "stop here"}))
    draft_for_prospect(conn, pid)
    assert seen[0]["rebuild"]["worthy"] is worthy
    if worthy:
        assert seen[0]["rebuild"]["dated_signals"][0]["fact_id"] == "stale_copyright"
