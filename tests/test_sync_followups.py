"""Gmail sync classification + reconciliation, follow-up timing, call list, Claude errors."""
import datetime as dt
import json

import pytest

from app.db.migrate import migrate, connect
from app.db.models import Prospect, Audit, Draft
from app.db.repo import upsert_prospect, insert_audit, insert_draft, get_draft, get_prospect
from app.draft import gmail_sync, followups
from app.draft.gmail_sync import classify_reply, sync_gmail
from app.draft.followups import business_days_between, find_due_followups
from app._claude_client import _classify


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "s.db"
    migrate(db)
    c = connect(db)
    yield c
    c.close()


@pytest.mark.parametrize("snippet,subject,from_addr,expected", [
    ("No.", "Re: x", "bob@x.com", "opt_out"),
    ("No thanks, we're all set", "Re: x", "bob@x.com", "opt_out"),
    ("Please remove me from your list", "Re: x", "bob@x.com", "opt_out"),
    ("Not interested at this time", "Re: x", "bob@x.com", "opt_out"),
    ("No problem, can you call me Tuesday?", "Re: x", "bob@x.com", "reply"),
    ("Yes please send it over", "Re: x", "bob@x.com", "reply"),
    ("I'm out of the office until Monday", "Automatic reply: x", "bob@x.com", "auto"),
    ("Address not found", "Delivery Status Notification (Failure)", "mailer-daemon@googlemail.com", "bounce"),
])
def test_classify_reply(snippet, subject, from_addr, expected):
    assert classify_reply(from_addr=from_addr, subject=subject, snippet=snippet) == expected


class FakeGmail:
    """Minimal stand-in for the read-only Gmail service."""

    def __init__(self, threads):
        self._threads = threads

    def users(self):
        return self

    def getProfile(self, userId):
        return _Exec({"emailAddress": "ckamats@promptproofreading.com"})

    def threads(self):
        return self

    def get(self, userId, id, format):
        return _Exec(self._threads[id])


class _Exec:
    def __init__(self, v):
        self.v = v

    def execute(self):
        return self.v


def _msg(frm, labels, ts, snippet="", subject="Re: x", msgid="<m@x>"):
    return {"labelIds": labels, "internalDate": str(ts), "snippet": snippet,
            "payload": {"headers": [{"name": "From", "value": frm}, {"name": "Subject", "value": subject},
                                    {"name": "Message-ID", "value": msgid}]}}


def _seed(conn, status="pushed", thread="t1", email="bob@bobsdiner.com"):
    pid = upsert_prospect(conn, Prospect(company="Bob's", url="https://bobsdiner.com",
                                         source="manual", email=email))
    aid = insert_audit(conn, Audit(prospect_id=pid, issues_json="[]", final_grade="A"))
    did = insert_draft(conn, Draft(prospect_id=pid, audit_id=aid, subject="s", body_text="b",
                                   body_html="b", status=status, gmail_thread_id=thread))
    return pid, did


def test_sync_marks_sent_and_detects_opt_out(conn):
    pid, did = _seed(conn)
    me = "Chase <CKamats@promptproofreading.com>"
    svc = FakeGmail({"t1": {"messages": [
        _msg(me, ["SENT"], 1000, msgid="<ours@pp>"),
        _msg("Bob <bob@bobsdiner.com>", ["INBOX"], 2000, snippet="No thanks"),
    ]}})
    stats = sync_gmail(conn, service=svc)
    assert stats["sent"] == 1 and stats["opted_out"] == 1
    d = get_draft(conn, did)
    assert d.status == "opted_out" and d.rfc_message_id == "<ours@pp>"
    assert get_prospect(conn, pid).blocked_reason == "opted_out"
    assert conn.execute("SELECT COUNT(*) FROM dnc_list").fetchone()[0] == 1


def test_sync_ignores_unsent_draft_and_auto_replies(conn):
    pid, did = _seed(conn)
    svc = FakeGmail({"t1": {"messages": [_msg("Chase <ckamats@promptproofreading.com>", ["DRAFT"], 1000)]}})
    assert sync_gmail(conn, service=svc)["sent"] == 0
    assert get_draft(conn, did).status == "pushed"
    svc = FakeGmail({"t1": {"messages": [
        _msg("ckamats@promptproofreading.com", ["SENT"], 1000),
        _msg("bob@bobsdiner.com", ["INBOX"], 2000, subject="Automatic reply: s", snippet="away")]}})
    stats = sync_gmail(conn, service=svc)
    assert stats["auto"] == 1 and get_draft(conn, did).status == "sent"


def test_business_days():
    mon = dt.date(2026, 10, 19)
    assert business_days_between(mon, dt.date(2026, 10, 23)) == 4      # Tue..Fri
    assert business_days_between(mon, dt.date(2026, 10, 26)) == 5      # weekend skipped
    assert business_days_between(mon, mon) == 0


def test_followup_due_only_after_sent_without_reply(conn):
    pid, did = _seed(conn, status="sent")
    conn.execute("UPDATE drafts SET sent_at='2026-10-19 15:00:00' WHERE id=?", (did,))
    conn.commit()
    assert find_due_followups(conn, today=dt.date(2026, 10, 22)) == []          # 3 business days
    due = find_due_followups(conn, today=dt.date(2026, 10, 23))                  # 4 business days
    assert [d["id"] for d in due] == [did]
    conn.execute("UPDATE drafts SET status='replied' WHERE id=?", (did,))
    conn.commit()
    assert find_due_followups(conn, today=dt.date(2026, 11, 30)) == []


def test_create_followup_threads_reply(conn, monkeypatch):
    pid, did = _seed(conn, status="sent")
    conn.execute("UPDATE drafts SET sent_at='2026-10-19 15:00:00', subject=? WHERE id=?",
                 ("Bob's phone number", did))
    conn.commit()
    body = ("Hi Bob,\n\nOne more idea: your menu could open as a page instead of a download, "
            "which is easier on phones.\n\nWant me to include that in the mockup?\n\nThanks,\nChase")
    monkeypatch.setattr(followups, "generate_followup",
                        lambda **kw: {"body_text": body, "body_html": "<p>x</p>",
                                      "used_quotes": [], "used_fact_ids": []})
    stats = followups.create_followups(conn, today=dt.date(2026, 10, 23))
    assert stats["created"] == 1
    fu = conn.execute("SELECT * FROM drafts WHERE touch=2").fetchone()
    assert fu["parent_draft_id"] == did and fu["gmail_thread_id"] == "t1"
    assert fu["subject"].startswith("Re: ") and fu["qa_pass"] == 1
    assert followups.create_followups(conn, today=dt.date(2026, 10, 23))["created"] == 0


def test_claude_error_classification():
    assert _classify("API Error: 401 OAuth access token has expired") == "auth"
    assert _classify("429 rate_limit_error") == "rate_limit"
    assert _classify("something else") == "other"


def test_call_list(conn, tmp_path):
    from app.sources.call_list import build_call_list
    csv_path = tmp_path / "m.csv"
    csv_path.write_text(
        "﻿Business Name,Business Type,City,State,Phone,Email,Contact Name,Website URL,Missing Feature,Notes\n"
        "Kim's Diner,restaurant,Erie,PA,814-555-0101,,,https://www.restaurantji.com/pa/erie/kims/,No Website,\n"
        "Own Site Cafe,restaurant,Erie,PA,814-555-0102,,,https://ownsitecafe.com,,\n"
        "Bob's Barber,barbershop,Erie,PA,814-555-0103,,,No Website,No Website,\n"
        "Dup Barber,barbershop,Erie,PA,814-555-0103,,,,,\n", encoding="utf-8")
    out = build_call_list(conn, csv_path, tmp_path / "exports")
    assert out["written"] == 2
    assert out["skipped"]["has_own_site"] == 1 and out["skipped"]["duplicate"] == 1
    text = open(out["path"], encoding="utf-8-sig").read()
    assert text.index("Bob's Barber") < text.index("Kim's Diner")   # barbers prioritized
