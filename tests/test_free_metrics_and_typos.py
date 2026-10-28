"""Local (no Google API) speed/cert facts, and the dictionary-backed typo gate."""
import pytest

from app.audit.extractor import compute_facts
from app.safety.fabrication_guard import typo_rejection_reason, validate_findings

BASE_FEATURES = {"viewport_present": True, "phones_in_text": [], "tel_links": 0}


def _facts(mobile=None, **fm):
    return compute_facts(BASE_FEATURES, fetch_meta={"mobile": mobile or {"viewport_width": 390}, **fm})


def test_heavy_page_and_slow_content():
    f = _facts({"viewport_width": 390, "page_bytes": 6_900_000, "lcp_seconds": 5.2})
    assert "heavy_page_mobile" in f and "7 MB" in f["heavy_page_mobile"]
    assert "slow_main_content" in f
    fast = _facts({"viewport_width": 390, "page_bytes": 1_100_000, "lcp_seconds": 0.4})
    assert "heavy_page_mobile" not in fast and "slow_main_content" not in fast


def test_speed_needs_a_real_phone_render():
    assert "heavy_page_mobile" not in compute_facts(BASE_FEATURES, fetch_meta={"mobile": {"error": "x", "page_bytes": 9e6}})


def test_certificate_facts():
    assert "cert_expired" in _facts(cert={"expired": True, "error": "certificate has expired"})
    assert "cert_expiring_soon" in _facts(cert={"days_left": 5}, https_ok=True)
    assert not {"cert_expired", "cert_expiring_soon"} & set(_facts(cert={"days_left": 73}, https_ok=True))


@pytest.mark.parametrize("err,corr,ok", [
    ("a order", "an order", True), ("your welcome", "you're welcome", True),
    ("should of", "should have", True), ("the the", "the", True),
    ("recieve", "receive", True), ("resturant", "restaurant", True),
    ("Pho", "Phone", False),          # dish name, not a near-miss fix
    ("Tuscon", "Tucson", False),      # proper noun the dictionary doesn't flag
    ("a unique", "an unique", False), ("an hour", "a hour", False),
    ("colour", "color", False), ("centre", "center", False),   # regional spellings
    ("best food", "better food", False),                       # taste, not a typo
])
def test_typo_gate(err, corr, ok):
    assert (typo_rejection_reason(err, corr) is None) is ok


def test_validate_drops_fake_typo():
    text = "Order our famous Pho today. Recieve a free drink."
    findings = [
        {"type": "typo", "evidence_kind": "quote", "quote": "Order our famous Pho today",
         "error_text": "Pho", "correction": "Phone", "severity": 2},
        {"type": "typo", "evidence_kind": "quote", "quote": "Recieve a free drink",
         "error_text": "Recieve", "correction": "Receive", "severity": 2},
        {"type": "slow_mobile", "evidence_kind": "fact", "fact_id": "heavy_page_mobile", "severity": 2},
    ]
    kept, dropped = validate_findings(findings, quote_source_text=text,
                                      facts={"heavy_page_mobile": "about 7 MB"})
    assert [k["error_text"] if k["type"] == "typo" else k["type"] for k in kept] == ["Recieve", "slow_mobile"]
    assert "near-miss" in dropped[0]["reason"]
