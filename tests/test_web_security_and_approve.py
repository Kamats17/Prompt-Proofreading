"""Dashboard: CSRF / host checks, approve re-validation, failure rollback, guards."""
import json

import pytest
from starlette.testclient import TestClient

import app.web.routes as routes
import app.web.server as server
from app.db.migrate import migrate, connect
from app.db.models import Prospect, Audit, Draft, DncEntry
from app.db.repo import upsert_prospect, insert_audit, insert_draft, add_dnc, get_draft, get_prospect
from app.draft.voice import pick_subject

ISSUES = json.dumps([{"type": "phone_not_tappable", "evidence_kind": "fact",
                      "fact_id": "phone_not_tappable", "quote": "", "confidence": "verified",
                      "service": "website", "severity": 3}])
BODY = ("Hi Sarah,\n\nI pulled up Bob's site on my phone and the number at the top can't be "
        "tapped to call, so customers have to copy it by hand before they can reach you.\n\n"
        "I'd turn it into a one-tap call button. I do this kind of fix for local restaurants "
        "and it usually takes under a day.\n\nWant me to send a free mockup of the header?\n\n"
        "Thanks,\nChase")


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "w.db"
    migrate(db)
    monkeypatch.setattr(routes, "DB_PATH", db)
    monkeypatch.setattr(server, "DB_PATH", db)
    conn = connect(db)
    pid = upsert_prospect(conn, Prospect(company="Bob's Diner", url="https://bobsdiner.com",
                                         source="manual", email="sarah@bobsdiner.com"))
    aid = insert_audit(conn, Audit(prospect_id=pid, issues_json=ISSUES, final_grade="A", icp_score=80))
    did = insert_draft(conn, Draft(prospect_id=pid, audit_id=aid,
                                   subject=pick_subject("Bob's Diner", "phone_not_tappable"),
                                   body_text=BODY, body_html="<p>x</p>", qa_pass=1,
                                   evidence_json=json.dumps({"used_fact_ids": ["phone_not_tappable"]})))
    client = TestClient(server.create_app())
    yield client, conn, pid, did
    conn.close()


def test_cross_site_post_refused(env):
    client, conn, pid, did = env
    r = client.post(f"/api/draft/{did}/reject", data={"category": "other"},
                    headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403
    assert get_draft(conn, did).status == "pending"
    r = client.post("/api/dnc/add", data={"domain": "x.com"}, headers={"Origin": "null"})
    assert r.status_code == 403


def test_foreign_host_refused(env):
    client, *_ = env
    assert client.get("/", headers={"Host": "attacker.example"}).status_code == 400


def test_same_origin_post_allowed(env):
    client, conn, pid, did = env
    r = client.post(f"/api/draft/{did}/reject", data={"category": "tone"},
                    headers={"Origin": "http://127.0.0.1:8765"}, follow_redirects=False)
    assert r.status_code == 303
    assert get_draft(conn, did).status == "rejected"


def test_approve_rechecks_dnc_added_after_drafting(env, monkeypatch):
    client, conn, pid, did = env
    add_dnc(conn, DncEntry(email="sarah@bobsdiner.com", reason="asked us to stop"))
    monkeypatch.setattr(routes, "create_gmail_draft", lambda **kw: pytest.fail("must not push"))
    r = client.post(f"/api/draft/{did}/approve", follow_redirects=False)
    assert r.status_code == 303 and "DNC" in r.headers["location"].replace("%20", " ")
    assert get_draft(conn, did).status == "pending"


def test_approve_pushes_once_and_stamps_contact(env, monkeypatch):
    client, conn, pid, did = env
    calls = []
    monkeypatch.setattr(routes, "create_gmail_draft",
                        lambda **kw: calls.append(kw) or {"success": True, "draft_id": "g1", "thread_id": "t1"})
    client.post(f"/api/draft/{did}/approve")
    client.post(f"/api/draft/{did}/approve")     # double click
    assert len(calls) == 1 and calls[0]["to_email"] == "sarah@bobsdiner.com"
    d = get_draft(conn, did)
    assert d.status == "pushed" and d.gmail_thread_id == "t1"
    assert get_prospect(conn, pid).last_contacted_at is not None


def test_approve_and_reject_move_to_next_draft(env, monkeypatch):
    client, conn, pid, did = env
    pid2 = upsert_prospect(conn, Prospect(company="Ann's Cafe", url="https://annscafe.com",
                                          source="manual", email="ann@annscafe.com"))
    aid2 = insert_audit(conn, Audit(prospect_id=pid2, issues_json=ISSUES, final_grade="B", icp_score=50))
    did2 = insert_draft(conn, Draft(prospect_id=pid2, audit_id=aid2, subject="x", body_text="y",
                                    body_html="<p>y</p>", qa_pass=1))
    monkeypatch.setattr(routes, "create_gmail_draft",
                        lambda **kw: {"success": True, "draft_id": "g1", "thread_id": "t1"})
    r = client.post(f"/api/draft/{did}/approve", follow_redirects=False)
    assert r.headers["location"].startswith(f"/prospect/{pid2}?")      # next lead, not the one just pushed
    r = client.post(f"/api/draft/{did2}/reject", data={"category": "tone"}, follow_redirects=False)
    assert r.headers["location"].startswith("/queue?") and "empty" in r.headers["location"]
    assert "Queue is empty" in client.get(r.headers["location"]).text


def test_approve_failure_rolls_back_to_pending(env, monkeypatch):
    client, conn, pid, did = env

    def boom(**kw):
        raise RuntimeError("network down")
    monkeypatch.setattr(routes, "create_gmail_draft", boom)
    with pytest.raises(RuntimeError):
        client.post(f"/api/draft/{did}/approve")
    assert get_draft(conn, did).status == "pending"   # not stuck in 'approved'


def test_daily_cap(env, monkeypatch):
    client, conn, pid, did = env
    monkeypatch.setattr(routes, "DAILY_NEW_CAP", 0)
    monkeypatch.setattr(routes, "create_gmail_draft", lambda **kw: pytest.fail("cap ignored"))
    r = client.post(f"/api/draft/{did}/approve", follow_redirects=False)
    assert "cap" in r.headers["location"].lower()


def test_status_guards(env):
    client, conn, pid, did = env
    r = client.post(f"/api/draft/{did}/mark-sent", follow_redirects=False)
    assert get_draft(conn, did).status == "pending"            # can't mark a pending draft sent
    conn.execute("UPDATE drafts SET status='pushed' WHERE id=?", (did,))
    conn.commit()
    client.post(f"/api/draft/{did}/reject", data={"category": "other"})
    assert get_draft(conn, did).status == "pushed"             # can't reject what's in Gmail
    client.post(f"/api/draft/{did}/edit", data={"subject": "x", "body_text": "y"})
    assert get_draft(conn, did).body_text == BODY              # can't edit what's in Gmail


def test_opt_out_button_adds_dnc_and_blocks(env):
    client, conn, pid, did = env
    conn.execute("UPDATE drafts SET status='sent' WHERE id=?", (did,))
    conn.commit()
    client.post(f"/api/draft/{did}/opted-out")
    assert get_draft(conn, did).status == "opted_out"
    assert get_prospect(conn, pid).blocked_reason == "opted_out"
    assert conn.execute("SELECT COUNT(*) FROM dnc_list WHERE domain='bobsdiner.com'").fetchone()[0] == 1


def test_human_edit_relaxes_style_only(env):
    client, conn, pid, did = env
    short = "Hi Sarah,\n\nYour number at the top can't be tapped on a phone.\n\nWant a mockup?\n\nThanks,\nChase"
    client.post(f"/api/draft/{did}/edit", data={"subject": "Bob's phone number", "body_text": short})
    d = get_draft(conn, did)
    assert d.human_edited == 1 and d.qa_pass == 1
    assert "WARNING" in (d.qa_blockers_json or "")
    assert "<p>Hi Sarah,</p>" in d.body_html              # HTML rebuilt from edited text


def test_pages_render(env):
    client, conn, pid, did = env
    for path in ("/", "/queue", "/prospects", "/prospects?status=blocked", "/dnc", f"/prospect/{pid}"):
        assert client.get(path).status_code == 200, path


def test_screenshot_route_serves_only_pngs(env):
    client, conn, pid, did = env
    conn.execute("UPDATE audits SET screenshot_path=? WHERE prospect_id=?", ("C:/Windows/win.ini", pid))
    conn.commit()
    assert client.get("/shot/1/desktop").status_code == 404
    assert client.get("/cache/x/page.html").status_code == 404
