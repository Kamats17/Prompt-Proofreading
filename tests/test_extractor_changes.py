"""Extractor changes: copyright max-year, CTA word boundaries, email harvest, dead-site."""
from app.audit.extractor import extract_features, extract_emails, dead_site_reason


_HTML = """<!doctype html><html><head><title>Bob's Diner</title>
<meta name="viewport" content="width=device-width"></head><body>
<h1>Welcome to Bob's Diner</h1>
<a href="https://facebook.com/bobs">Facebook</a>
<a href="/order">Book a table now</a>
<a href="mailto:owner@bobsdiner.com">Email us</a>
<p>Fresh food since 1985. Come visit our family diner for lunch and dinner every day.
We serve breakfast all day and have a full catering menu for events large and small.</p>
<footer>© 2010-2026 Bob's Diner. All rights reserved. Contact info@bobsdiner.com</footer>
</body></html>"""


def _write(tmp_path, html=_HTML):
    p = tmp_path / "page.html"
    p.write_text(html, encoding="utf-8")
    return p


def test_copyright_range_reads_latest_year(tmp_path):
    f = extract_features(_write(tmp_path))
    assert f["copyright_year"] == 2026


def test_cta_word_boundary_excludes_facebook(tmp_path):
    f = extract_features(_write(tmp_path))
    ctas = [c.lower() for c in f["ctas"]]
    assert "facebook" not in ctas
    assert any("book a table" in c for c in ctas)


def test_email_harvest_prefers_mailto(tmp_path):
    emails = extract_emails(_write(tmp_path))
    assert "owner@bobsdiner.com" in emails
    # mailto address comes first (highest confidence)
    assert emails[0] == "owner@bobsdiner.com"


def test_dead_site_reason_flags_parked(tmp_path):
    parked = "<html><body><h1>This domain is for sale</h1></body></html>"
    f = extract_features(_write(tmp_path, parked))
    reason, permanent = dead_site_reason(f)
    assert reason is not None and permanent


def test_dead_site_reason_none_for_real_page(tmp_path):
    f = extract_features(_write(tmp_path))
    assert dead_site_reason(f) == (None, False)


def test_coming_soon_in_body_of_real_page_is_not_dead(tmp_path):
    html = _HTML.replace("Fresh food since 1985.", "Fresh food since 1985. New patio coming soon!")
    f = extract_features(_write(tmp_path, html))
    assert dead_site_reason(f) == (None, False)


def test_thin_page_is_soft_failure(tmp_path):
    f = extract_features(_write(tmp_path, "<html><body><p>Loading</p></body></html>"))
    reason, permanent = dead_site_reason(f)
    assert reason and not permanent
