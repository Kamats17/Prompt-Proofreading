"""Fixes from the third live batch: measured facts the model skipped, parked
domains, invalid certificates, and redirecting/parked sites on the call list."""
import csv
import json

import pytest

from app.audit.extractor import compute_facts, dead_site_reason, extract_features
from app.audit.pipeline import missing_fact_findings
from app.db.migrate import migrate, connect
from app.db.models import Prospect
from app.db.repo import upsert_prospect, mark_prospect_blocked
from app.sources.call_list import build_call_list


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "c.db"
    migrate(db)
    c = connect(db)
    yield c
    c.close()


def test_skipped_phone_fact_becomes_finding():
    facts = {"phone_not_tappable": "The phone number 555-1234 appears 1 time ...",
             "stale_copyright": "The footer still says 2022 ..."}
    kept = [{"type": "stale_content", "fact_id": "stale_copyright", "severity": 2}]
    added = missing_fact_findings(kept, facts)
    assert [f["fact_id"] for f in added] == ["phone_not_tappable"]
    assert added[0]["severity"] == 3 and added[0]["fact_detail"] == facts["phone_not_tappable"]


def test_no_duplicate_when_model_already_cited_it():
    facts = {"phone_not_tappable": "x", "cert_expired": "y", "no_https": "z"}
    kept = [{"type": "phone_not_tappable", "fact_id": "phone_not_tappable"}]
    added = missing_fact_findings(kept, facts)
    assert [f["fact_id"] for f in added] == ["cert_expired"]   # one no_https-type finding only


def test_parking_script_page_is_dead(tmp_path):
    html = tmp_path / "p.html"
    html.write_text('<html><head><script src="https://x.com/sk-park.php?dn=a.com"></script></head>'
                    "<body>a.com Privacy Policy</body></html>", encoding="utf-8")
    reason, permanent = dead_site_reason(extract_features(html))
    assert permanent and "parked" in reason


def test_invalid_cert_fact_only_when_confirmed():
    base = {"https_ok": True, "cert_ignored": True}
    assert "cert_invalid" in compute_facts({}, fetch_meta={**base, "cert": {"error": "bad", "expired": False}})
    assert "cert_expired" in compute_facts({}, fetch_meta={**base, "cert": {"error": "bad", "expired": True}})
    # A network error during the TLS check proves nothing about the certificate.
    assert not {"cert_invalid", "cert_expired"} & set(
        compute_facts({}, fetch_meta={**base, "cert": {"error": "TimeoutError: x"}}))


def test_call_list_includes_parked_and_redirecting(conn, tmp_path):
    src = tmp_path / "leads.csv"
    src.write_text("Business Name,Website URL,Phone,State\n", encoding="utf-8")
    for name, url, phone, reason in (("Parked Diner", "https://parked.com", "412-555-0101", "deadsite"),
                                     ("Moved Pizza", "https://moved.com", "412-555-0102", "redirect_offsite")):
        pid = upsert_prospect(conn, Prospect(company=name, url=url, phone=phone, source="manual"))
        mark_prospect_blocked(conn, pid, reason)
    res = build_call_list(conn, src, tmp_path)
    with open(res["path"], encoding="utf-8-sig") as f:
        rows = {r["Business"]: r for r in csv.DictReader(f)}
    assert "placeholder" in rows["Parked Diner"]["Pitch"]
    assert "forwards visitors" in rows["Moved Pizza"]["Pitch"]


def test_other_finding_citing_cert_is_retyped():
    kept = [{"type": "other", "fact_id": "cert_expired", "severity": 3}]
    assert missing_fact_findings(kept, {"cert_expired": "x"}) == []
    assert kept[0]["type"] == "no_https"


def test_fax_number_is_not_the_phone(tmp_path):
    html = tmp_path / "p.html"
    html.write_text("<html><body><p>" + "We care for smiles. " * 30 + "</p>"
                    "<p>Tel: 937-299-8000<br/>Fax: 888-237-1450</p></body></html>", encoding="utf-8")
    assert extract_features(html)["phones_in_text"] == ["937-299-8000"]


def test_unmeasured_no_email_lead_gets_no_guessed_talking_point(conn, tmp_path, monkeypatch):
    import app.audit.fetcher as fetcher
    monkeypatch.setattr(fetcher, "CACHE_DIR", tmp_path / "cache")
    src = tmp_path / "leads.csv"
    src.write_text("Business Name,Website URL,Phone,State\n", encoding="utf-8")
    pid = upsert_prospect(conn, Prospect(company="Quiet Diner", url="https://quiet.com",
                                         phone="412-555-0103", source="manual"))
    mark_prospect_blocked(conn, pid, "no_email")
    with open(build_call_list(conn, src, tmp_path)["path"], encoding="utf-8-sig") as f:
        row = next(r for r in csv.DictReader(f) if r["Business"] == "Quiet Diner")
    assert row["Pitch"].endswith("offer a free site review.")
