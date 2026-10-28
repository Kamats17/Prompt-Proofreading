"""Audit v2: identity/directory gates, email choice, evidence validation, scoring,
response parsing, and a full audit_prospect run with fetch + Claude mocked."""
import json
from pathlib import Path

import pytest

from app.audit import pipeline as audit_pipeline
from app.audit import audit_agent
from app.audit.extractor import (
    extract_features, identity_mismatch_reason, choose_contact_email, compute_facts,
)
from app.audit.grader import score_lead
from app.db.migrate import migrate, connect
from app.db.models import Prospect
from app.db.repo import upsert_prospect, get_prospect, latest_audit_for_prospect
from app.safety.aggregator_filter import is_aggregator, directory_generated_reason
from app.safety.fabrication_guard import validate_findings

PAGE = """<!doctype html><html lang="en"><head><title>Frank's Diner | Springfield</title>
<meta name="viewport" content="width=device-width"></head><body>
<h1>Frank's Diner</h1><p>Family owned since 1985. Call to place a order at (555) 201-7788.</p>
<a href="/menu.pdf">Download our Menu</a>
<a href="mailto:frank@franksdiner.com">Email</a>
<footer>© 2019 Frank's Diner. Website by Acme Web Co</footer>
<p>Breakfast, lunch and dinner seven days a week. Homemade pies daily and a full kids menu.
Join us for our famous pancakes and our seasonal specials all year long in Springfield Illinois.</p>
</body></html>"""


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "a.db"
    migrate(db)
    c = connect(db)
    yield c
    c.close()


def _features(tmp_path, html=PAGE, url="https://franksdiner.com"):
    p = tmp_path / "page.html"
    p.write_text(html, encoding="utf-8")
    return extract_features(p, base_url=url), p


def test_identity_hijacked_domain(tmp_path):
    f, _ = _features(tmp_path, "<html><head><title>Situs Resmi Togel Macau 4D</title></head>"
                               "<body>" + "keluaran macau togel " * 30 + "</body></html>")
    assert "hijacked" in identity_mismatch_reason(f, "Rosa's Cafe")


def test_identity_foreign_language(tmp_path):
    f, _ = _features(tmp_path, "<html lang='id'><body>" + "selamat datang " * 50 + "</body></html>")
    assert "language" in identity_mismatch_reason(f, "Rosa's Cafe")


def test_identity_name_missing_and_present(tmp_path):
    f, _ = _features(tmp_path)
    assert identity_mismatch_reason(f, "Frank's Diner") is None
    assert "does not appear" in identity_mismatch_reason(f, "Golden Dragon Palace")
    assert identity_mismatch_reason(f, "Judi's Diner") is not None  # name absent, not 'spam'


def test_owner_name_like_judi_is_not_spam(tmp_path):
    f, _ = _features(tmp_path, PAGE.replace("Frank's Diner", "Judi's Diner"))
    assert identity_mismatch_reason(f, "Judi's Diner") is None


def test_directory_generated():
    assert is_aggregator("https://joes-corner-diner.poi.place")[0]
    assert directory_generated_reason("https://oak-hill-diner.someotherbiz.com", "Oak Hill Diner")
    assert directory_generated_reason("https://shop.franksdiner.com", "Frank's Diner") is None


def test_choose_contact_email_rules():
    url = "https://franksdiner.com"
    assert choose_contact_email(["hello@acmeweb.co", "frank@franksdiner.com"], url)[0] == "frank@franksdiner.com"
    assert choose_contact_email(["franksdiner@gmail.com"], url)[0] == "franksdiner@gmail.com"
    # Third-party (web agency) address is never chosen.
    assert choose_contact_email(["hello@acmeweb.co"], url)[0] is None
    assert choose_contact_email(["localoria.com@gmail.com"], url)[0] is None


def test_facts_require_mobile_confirmation(tmp_path):
    f, _ = _features(tmp_path)
    assert "phone_not_tappable" not in compute_facts(f, fetch_meta={})  # no phone render
    mobile = {"viewport_width": 390, "tel_links_total": 0, "tap_to_call_first_screen": False}
    facts = compute_facts(f, fetch_meta={"mobile": mobile, "https_ok": False}, industry="restaurant")
    assert {"phone_not_tappable", "pdf_menu", "no_https"} <= set(facts)
    # Wix-style: desktop HTML lacks tel: but the phone render has a tap target.
    mobile_ok = {**mobile, "tel_links_total": 1}
    assert "phone_not_tappable" not in compute_facts(f, fetch_meta={"mobile": mobile_ok})


def test_validate_findings(tmp_path):
    f, page = _features(tmp_path)
    facts = compute_facts(f, fetch_meta={"mobile": {"viewport_width": 390}, "https_ok": True})
    text = f["title"] + " " + f["body_text"]
    findings = [
        {"type": "typo", "evidence_kind": "quote", "quote": "Call to place a order",
         "error_text": "a order", "correction": "an order", "severity": 2},
        {"type": "typo", "evidence_kind": "quote", "quote": "Call to place an order",   # paraphrase
         "error_text": "an order", "correction": "an order", "severity": 2},
        {"type": "typo", "evidence_kind": "quote", "quote": "CALL TO PLACE A ORDER",    # re-cased
         "error_text": "A ORDER", "correction": "AN ORDER", "severity": 2},
        {"type": "pdf_menu", "evidence_kind": "fact", "fact_id": "pdf_menu", "severity": 2},
        {"type": "no_https", "evidence_kind": "fact", "fact_id": "no_https", "severity": 3},  # not a fact here
        {"type": "no_booking_link", "evidence_kind": "quote", "quote": "Family owned since 1985",
         "severity": 2},  # absence claim backed by a quote -> invented
        {"type": "stale_content", "evidence_kind": "fact", "fact_id": "pdf_menu", "severity": 2},
    ]
    kept, dropped = validate_findings(findings, quote_source_text=text, facts=facts)
    assert [k["type"] for k in kept] == ["typo", "pdf_menu"]
    assert all(k["confidence"] == "verified" for k in kept)
    assert len(dropped) == 5


def test_score_lead():
    findings = [{"severity": 3}, {"severity": 2}]
    s = score_lead(findings=findings, meta={"ownership": "own_site", "activity_signals": ["x"]},
                   features={}, industry="Dentist", has_email=True, has_phone=True, contact_name="Mary")
    assert s["site_need"] == 45 and s["buyer_fit"] == 85 and s["icp"] == 61
    agency = score_lead(findings=findings, meta={"ownership": "own_site"},
                        features={"designer_credits": ["Acme Web Co"]}, industry="restaurant",
                        has_email=True, has_phone=True, contact_name=None)
    assert agency["buyer_fit"] == 25
    directory = score_lead(findings=findings, meta={"ownership": "directory_or_platform_page"},
                           features={}, industry="x", has_email=True, has_phone=True, contact_name=None)
    assert directory["icp"] == 0 and directory["reject_reason"]
    empty = score_lead(findings=[], meta={"ownership": "own_site"}, features={}, industry="dentist",
                       has_email=True, has_phone=True, contact_name="A")
    assert empty["icp"] < 50


def test_parse_audit_response_coerces_types():
    raw = "Here is the JSON:\n" + json.dumps({
        "ownership": "own_site", "findings": [
            {"type": "typo", "evidence_kind": "quote", "quote": 2015, "severity": "9",
             "service": "nonsense", "customer_moment": None},
            "not a dict"],
        "primary_finding_index": "x"})
    out = audit_agent.parse_audit_response(raw)
    assert out["findings"][0]["quote"] == "2015"
    assert out["findings"][0]["severity"] == 3
    assert out["findings"][0]["service"] == "website"
    assert out["primary_finding_index"] == 0
    assert "error" in audit_agent.parse_audit_response("no json at all")


def test_audit_prospect_end_to_end(conn, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    html = cache / "page.html"
    html.write_text(PAGE, encoding="utf-8")
    monkeypatch.setattr(audit_pipeline, "fetch_page", lambda url, **kw: {
        "ok": True, "html_path": str(html), "screenshot_path": None, "final_url": url,
        "https_ok": False, "mobile": {"viewport_width": 390, "tel_links_total": 0},
        "extra_pages": [], "mobile_screenshot_path": None})
    model_out = {"ownership": "own_site", "activity_signals": ["homemade pies daily"],
                 "findings": [
                     {"type": "phone_not_tappable", "evidence_kind": "fact", "fact_id": "phone_not_tappable",
                      "customer_moment": "On a phone the number can't be tapped.", "fix": "tap-to-call",
                      "service": "website", "severity": 3},
                     {"type": "typo", "evidence_kind": "quote", "quote": "Call to place a order",
                      "error_text": "a order", "correction": "an order", "customer_moment": "typo",
                      "fix": "fix it", "service": "content", "severity": 2},
                     {"type": "other", "evidence_kind": "quote", "quote": "We guarantee 50% more customers",
                      "severity": 3}],
                 "primary_finding_index": 0, "demo_offer": "a tap-to-call header mockup"}
    calls = []
    monkeypatch.setattr(audit_agent, "call_claude",
                        lambda **kw: calls.append(kw) or json.dumps(model_out))
    pid = upsert_prospect(conn, Prospect(company="Frank's Diner", url="https://franksdiner.com",
                                         source="manual", industry="restaurant", phone="(555) 201-7788"))
    res = audit_pipeline.audit_prospect(conn, pid)
    assert res["ok"], res
    assert res["issues_kept"] == 2 and res["issues_dropped"] == 1   # invented quote dropped
    p = get_prospect(conn, pid)
    assert p.email == "frank@franksdiner.com" and p.email_source == "harvested_own_domain"
    a = latest_audit_for_prospect(conn, pid)
    meta = json.loads(a.meta_json)
    assert "no_https" in meta["facts"] and meta["score"]["site_need"] == 45
    payload = json.loads(calls[0]["user"])
    assert payload["business"]["name"] == "Frank's Diner" and "phone_not_tappable" in payload["facts"]


def test_audit_parks_lead_without_email_before_claude(conn, tmp_path, monkeypatch):
    html = tmp_path / "page.html"
    html.write_text(PAGE.replace('<a href="mailto:frank@franksdiner.com">Email</a>', ""), encoding="utf-8")
    monkeypatch.setattr(audit_pipeline, "fetch_page", lambda url, **kw: {
        "ok": True, "html_path": str(html), "final_url": url, "mobile": {}, "extra_pages": []})
    monkeypatch.setattr(audit_agent, "call_claude", lambda **kw: pytest.fail("Claude must not be called"))
    pid = upsert_prospect(conn, Prospect(company="Frank's Diner", url="https://franksdiner.com", source="manual"))
    res = audit_pipeline.audit_prospect(conn, pid)
    assert res["blocked"] == "no_email"


def test_audit_blocks_offsite_redirect(conn, tmp_path, monkeypatch):
    html = tmp_path / "page.html"
    html.write_text(PAGE, encoding="utf-8")
    monkeypatch.setattr(audit_pipeline, "fetch_page", lambda url, **kw: {
        "ok": True, "html_path": str(html), "final_url": "https://www.facebook.com/franks",
        "mobile": {}, "extra_pages": []})
    pid = upsert_prospect(conn, Prospect(company="Frank's Diner", url="https://franksdiner.com", source="manual"))
    assert audit_pipeline.audit_prospect(conn, pid)["blocked"] == "redirect_offsite"


def test_audit_auth_error_is_reported(conn, tmp_path, monkeypatch):
    from app._claude_client import ClaudeCallError
    html = tmp_path / "page.html"
    html.write_text(PAGE, encoding="utf-8")
    monkeypatch.setattr(audit_pipeline, "fetch_page", lambda url, **kw: {
        "ok": True, "html_path": str(html), "final_url": url, "mobile": {}, "extra_pages": []})

    def boom(**kw):
        raise ClaudeCallError("401 OAuth access token has expired", "auth")
    monkeypatch.setattr(audit_agent, "call_claude", boom)
    pid = upsert_prospect(conn, Prospect(company="Frank's Diner", url="https://franksdiner.com", source="manual"))
    res = audit_pipeline.audit_prospect(conn, pid)
    assert res["error_kind"] == "auth"
    assert get_prospect(conn, pid).audit_attempts == 1
