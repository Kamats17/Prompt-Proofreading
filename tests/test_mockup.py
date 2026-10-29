"""Mockup generator: branding, verbatim hours/address, headline choice, rendering,
the reply-with-attachment flow, and the dashboard routes."""
import base64
import json
from email import message_from_bytes

import pytest
from bs4 import BeautifulSoup

import app.mockup.builder as builder
import app.mockup.reply as reply
from app.db.migrate import migrate, connect
from app.db.models import Prospect, Audit, Draft, DncEntry
from app.db.repo import upsert_prospect, insert_audit, insert_draft, add_dnc, mark_prospect_blocked
from app.draft.gmail_client import _build_mime
from app.mockup.brand import find_logo, brand_color
from app.mockup.builder import find_hours, find_hours_and_address, _headline_and_subhead, plan_changes

PAGE = """<!doctype html><html><head><title>Maple Leaf Restaurant</title>
<meta name="description" content="The restaurant that grew out of a family recipe. Offering breakfast ALL DAY, lunch and dinner, Maple Leaf Restaurant is a great family restaurant. Come visit us any time of the year please.">
<style>.hdr{background:#BE5738} .btn{color:#BE5738} body{color:#333333;background:#ffffff}</style></head>
<body><header><img src="/img/instagram_logo.png" alt="instagram"><img src="/img/maple-logo.png" alt="Maple Leaf logo"></header>
<h1>CURRENT HOURS</h1>
<p>Call Us for Delivery! 555-201-4433</p>
<p>Attention!! Our Hours are: Sunday 8am to 3pm, Monday-Wednesday 7am to 3pm, and Thursday-Saturday 7am to 7pm. Thank you!</p>
<p>Find us at 1006 Birch Ct. Springfield IL</p>
<a href="https://www.toasttab.com/mapleleaf">Order Online</a> <a href="/menu.pdf">Menu</a>
</body></html>"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "MOCKUPS_DIR", tmp_path / "mockups")
    db = tmp_path / "m.db"
    migrate(db)
    conn = connect(db)
    html = tmp_path / "page.html"
    html.write_text(PAGE, encoding="utf-8")
    pid = upsert_prospect(conn, Prospect(company="Maple Leaf Restaurant", url="https://mapleleafeats.com",
                                         source="manual", industry="restaurant", city="Springfield", state="IL",
                                         email="info@mapleleafeats.com"))
    facts = {"pdf_menu": "x", "third_party_ordering:toast": "x", "phone_not_tappable": "x"}
    aid = insert_audit(conn, Audit(prospect_id=pid, issues_json=json.dumps([
        {"type": "typo", "confidence": "verified", "quote": "Recieve a free cookie",
         "error_text": "Recieve", "correction": "Receive"}]), final_grade="A",
        html_cache_path=str(html), meta_json=json.dumps({"facts": facts})))
    yield conn, pid, aid, tmp_path
    conn.close()


def test_logo_skips_social_icons():
    soup = BeautifulSoup(PAGE, "lxml")
    assert find_logo(soup, "https://mapleleafeats.com").endswith("/img/maple-logo.png")
    only_social = BeautifulSoup('<header><img src="/instagram_logowhite.png"></header>', "lxml")
    assert find_logo(only_social, "https://x.com") is None


def test_brand_color_from_site_and_fallback():
    soup = BeautifulSoup(PAGE, "lxml")
    color, src = brand_color(PAGE, soup, "restaurant")
    assert src == "site" and color.startswith("#")
    plain = "<html><style>body{color:#000;background:#fff}</style></html>"
    assert brand_color(plain, BeautifulSoup(plain, "lxml"), "dentist")[1] == "industry"


def test_hours_full_sentence_or_nothing():
    assert find_hours(PAGE_TEXT := "Our Hours are: Sunday 8am to 3pm, Monday-Wednesday 7am to 3pm, "
                      "and Thursday-Saturday 7am to 7pm. Thanks") == \
        "Sunday 8am to 3pm, Monday-Wednesday 7am to 3pm, and Thursday-Saturday 7am to 7pm"
    assert find_hours("Breakfast and Lunch Open 7 days a week! from 7 am - 2 pm The Best Kept Secret") == \
        "Open 7 days a week! 7 am - 2 pm"
    assert find_hours("We are open daily. Call us.") == ""
    long = "Hours: " + ", ".join(f"Day{i} 7am to 3pm" for i in range(12)) + "."
    assert find_hours(long) == ""          # too long -> show nothing, never a fragment


def test_address_verbatim():
    _, addr = find_hours_and_address("Find us at 1006 Birch Ct. Springfield IL")
    assert addr == "1006 Birch Ct."


def test_section_heading_is_not_the_headline():
    head, sub = _headline_and_subhead({"h1_first": "CURRENT HOURS", "meta_description": "Short."},
                                      "Maple Leaf Restaurant", {})
    assert head == "Maple Leaf Restaurant"
    head2, _ = _headline_and_subhead({"h1_first": "Welcome to the Maple Leaf"}, "Maple Leaf Restaurant", {})
    assert head2 == "Welcome to the Maple Leaf"


def test_plan_changes_tied_to_evidence():
    ch = plan_changes({"pdf_menu": "x", "third_party_ordering:toast": "x"},
                      [{"type": "typo", "error_text": "Recieve", "correction": "Receive"}], has_phone=True)
    titles = [c["title"] for c in ch]
    assert titles[0].startswith("Menu as a real page")
    assert "Copy cleanup" in titles and any("Tap-to-call" in t for t in titles)
    assert plan_changes({}, [], has_phone=False) == []


def test_build_mockup_html(env):
    conn, pid, aid, tmp = env
    res = builder.build_mockup(conn, pid, render=False)
    assert res["ok"]
    html = open(res["html_path"], encoding="utf-8").read()
    assert "Maple Leaf Restaurant" in html and 'href="tel:5552014433"' in html
    assert "CURRENT HOURS" not in html.split("<h1>")[1].split("</h1>")[0]
    assert "Thursday-Saturday 7am to 7pm" in html and "1006 Birch Ct." in html
    assert "maple-logo.png" in html and "instagram" not in html.split('class="bar"')[1].split("</div>")[0]


def test_mockup_escapes_scraped_text(env):
    conn, pid, aid, tmp = env
    conn.execute("UPDATE prospects SET company=? WHERE id=?", ("Bob's <script>alert(1)</script>", pid))
    conn.commit()
    html = open(builder.build_mockup(conn, pid, render=False)["html_path"], encoding="utf-8").read()
    assert "<script>alert(1)</script>" not in html


def test_build_mockup_renders_png(env):
    pytest.importorskip("playwright")
    conn, pid, aid, tmp = env
    res = builder.build_mockup(conn, pid, render=True)
    if not res["ok"] and "Executable doesn't exist" in (res.get("error") or ""):
        pytest.skip("chromium not installed")
    assert res["ok"], res.get("error")
    for key in ("png_path", "proposed_png_path"):
        assert open(res[key], "rb").read(8) == b"\x89PNG\r\n\x1a\n"


def test_mime_with_attachment():
    raw = _build_mime("a@b.com", "Bob", "Re: hi", "body", "<p>body</p>",
                      attachments=[("maple-leaf-mockup.png", b"\x89PNG fake", "image/png")])
    msg = message_from_bytes(base64.urlsafe_b64decode(raw))
    parts = [p for p in msg.walk() if p.get_filename()]
    assert msg.get_content_type() == "multipart/mixed"
    assert parts[0].get_filename() == "maple-leaf-mockup.png" and parts[0].get_content_type() == "image/png"


def _fake_png(pid):
    paths = builder.mockup_paths(pid)
    paths["dir"].mkdir(parents=True, exist_ok=True)
    paths["png"].write_bytes(b"\x89PNG\r\n\x1a\nfake")


def test_reply_threads_and_attaches(env, monkeypatch):
    conn, pid, aid, tmp = env
    insert_draft(conn, Draft(prospect_id=pid, audit_id=aid, subject="Maple Leaf menu on phones",
                             body_text="b", body_html="b", status="replied",
                             gmail_thread_id="t9", rfc_message_id="<ours@pp>"))
    _fake_png(pid)
    calls = []
    monkeypatch.setattr(reply, "create_gmail_draft",
                        lambda **kw: calls.append(kw) or {"success": True, "draft_id": "g7"})
    text = reply.default_reply_text(conn, pid)
    assert "Menu as a real page" in text and "Would" in text
    res = reply.push_mockup_reply(conn, pid, text)
    assert res["ok"] and res["threaded"]
    kw = calls[0]
    assert kw["subject"] == "Re: Maple Leaf menu on phones" and kw["thread_id"] == "t9"
    assert kw["in_reply_to"] == "<ours@pp>" and kw["attachments"][0][2] == "image/png"


def test_reply_refuses_dnc_optout_and_missing_mockup(env, monkeypatch):
    conn, pid, aid, tmp = env
    monkeypatch.setattr(reply, "create_gmail_draft", lambda **kw: pytest.fail("must not push"))
    assert "no mockup" in reply.push_mockup_reply(conn, pid, "hi")["error"]
    _fake_png(pid)
    mark_prospect_blocked(conn, pid, "opted_out")
    assert "blocked" in reply.push_mockup_reply(conn, pid, "hi")["error"]
    conn.execute("UPDATE prospects SET blocked_reason=NULL WHERE id=?", (pid,))
    conn.commit()
    add_dnc(conn, DncEntry(domain="mapleleafeats.com"))
    assert "DNC" in reply.push_mockup_reply(conn, pid, "hi")["error"]


def test_dashboard_mockup_routes(env, monkeypatch):
    from starlette.testclient import TestClient
    import app.web.routes as routes
    import app.web.server as server
    conn, pid, aid, tmp = env
    db = conn.execute("PRAGMA database_list").fetchone()["file"]
    monkeypatch.setattr(routes, "DB_PATH", db)
    monkeypatch.setattr(server, "DB_PATH", db)
    client = TestClient(server.create_app())
    assert client.get(f"/mockup/{pid}/sheet").status_code == 404
    assert client.get(f"/mockup/{pid}/../../etc").status_code == 404
    _fake_png(pid)
    r = client.get(f"/mockup/{pid}/sheet")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    page = client.get(f"/prospect/{pid}").text
    assert "Put mockup reply in Gmail Drafts" in page
    monkeypatch.setattr(reply, "create_gmail_draft", lambda **kw: {"success": True, "draft_id": "g1"})
    r = client.post(f"/api/prospect/{pid}/mockup/send", data={"body_text": "Hi there"}, follow_redirects=False)
    assert "Gmail%20Drafts" in r.headers["location"]


def test_possessive_names():
    from app.draft.voice import possessive
    assert possessive("Jethro's") == "Jethro's"
    assert possessive("Bob") == "Bob's"
    assert possessive("Collins") == "Collins'"
