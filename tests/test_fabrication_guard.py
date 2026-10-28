from pathlib import Path
from app.safety.fabrication_guard import filter_issues, has_verified_quote

FIXTURE_HTML = Path(__file__).parent / "fixtures" / "sample_html.html"


def test_real_quote_passes():
    issues = [{
        "type": "outdated_copyright",
        "quote": "Copyright 2018 Bob's Diner",
        "confidence": "verified",
        "why_hurts": "x",
        "service": "website",
    }]
    kept, dropped = filter_issues(issues, FIXTURE_HTML)
    assert len(kept) == 1 and not dropped
    assert kept[0]["confidence"] == "verified"


def test_fabricated_quote_demoted():
    issues = [{
        "type": "spelling",
        "quote": "this exact text does NOT appear anywhere in the HTML",
        "confidence": "verified",
        "why_hurts": "x",
        "service": "content",
    }]
    kept, dropped = filter_issues(issues, FIXTURE_HTML)
    assert len(kept) == 1 and not dropped
    assert kept[0]["confidence"] == "likely"
    assert kept[0]["demoted_from"] == "verified"


def test_verified_without_quote_dropped():
    issues = [{
        "type": "weak_cta",
        "quote": "",
        "confidence": "verified",
        "why_hurts": "x",
        "service": "website",
    }]
    kept, dropped = filter_issues(issues, FIXTURE_HTML)
    assert not kept and len(dropped) == 1


def test_likely_and_unknown_pass_through():
    issues = [
        {"type": "a", "quote": "", "confidence": "likely", "why_hurts": "x", "service": "website"},
        {"type": "b", "quote": "", "confidence": "unknown", "why_hurts": "x", "service": "website"},
    ]
    kept, _ = filter_issues(issues, FIXTURE_HTML)
    assert len(kept) == 2


def test_has_verified_quote():
    assert has_verified_quote([{"confidence": "verified", "quote": "x"}])
    assert not has_verified_quote([{"confidence": "likely", "quote": "x"}])
    assert not has_verified_quote([{"confidence": "verified", "quote": ""}])
