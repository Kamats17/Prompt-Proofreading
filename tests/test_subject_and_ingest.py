"""Subjects from findings + short names; URL/email/name normalization at ingest."""
from app.draft.voice import pick_subject, subject_allowed, allowed_subjects, short_name
from app.draft.name_guess import looks_like_person_name, first_name_from_full
from app.sources._normalize import (
    normalize_url, normalize_email, split_emails, smart_title, clean_company,
    registered_domain, is_non_us_domain, is_us_state,
)


def test_short_name():
    assert short_name("Joe's Restaurant & Bar") == "Joe's"
    assert short_name("Riverside Family Restaurant") == "Riverside"
    assert short_name("Frank's Diner") == "Frank's"
    assert short_name("Cafe") == "Cafe"            # never trims to nothing
    assert short_name("Smith Plumbing and Heating Services LLC") == "Smith Plumbing"


def test_subject_names_the_finding():
    s = pick_subject("Frank's Diner", "phone_not_tappable", seed=0)
    assert "Frank's" in s and "phone" in s
    assert "'s's" not in pick_subject("Mama Randazzo's", "pdf_menu")


def test_subject_default_and_allowed():
    s = pick_subject("Acme Dental", "unknown_type", seed=1)
    assert subject_allowed(s, "Acme Dental")
    assert not subject_allowed("Quick idea for Acme Dental", "Acme Dental")
    assert all("Quick idea" not in x for x in allowed_subjects("Acme Dental"))


def test_url_canonicalization():
    assert normalize_url("HTTP://Example.com") == "https://example.com"
    assert normalize_url("Https://www.example.com/About/") == "https://example.com/About"
    assert normalize_url("https://x.com/?b=2&a=1") == normalize_url("https://x.com/?a=1&b=2")
    assert normalize_url("café.com") == "https://xn--caf-dma.com"


def test_registered_domain_and_geo():
    assert registered_domain("https://shop.acme.com/x") == "acme.com"
    assert registered_domain("acme.co.uk") == "acme.co.uk"
    assert is_non_us_domain("https://joes.ca") and not is_non_us_domain("https://joes.com")
    assert is_us_state("pa") is True and is_us_state("ON") is False and is_us_state("") is None


def test_email_validation_and_split():
    assert normalize_email("bob@gmail,com") is None
    assert normalize_email("mailto:%20Info@Biz.com") == "info@biz.com"
    assert split_emails("mailto:a@x.com,b@y.com?subject=hi") == ["a@x.com", "b@y.com"]


def test_smart_title():
    assert smart_title("JOE'S PIZZA LLC") == "Joe's Pizza LLC"
    assert clean_company("", "https://best-bbq-shack.com") == "Best BBQ Shack"
    assert clean_company("Rosa's Cafe", "https://x.com") == "Rosa's Cafe"


def test_contact_name_column_junk_rejected():
    # Real data: 283 of 286 "Contact Name" values in the master CSV are URLs.
    url = "https://www.restaurantji.com/xx/sometown/sample-cafe-/"
    assert not looks_like_person_name(url)
    assert first_name_from_full(url) is None
    for junk in ("Owner", "info@x.com", "555-1234", "Manager", "", None):
        assert first_name_from_full(junk) is None, junk


def test_contact_names():
    assert first_name_from_full("Larry Miller") == "Larry"
    assert first_name_from_full("Kim") == "Kim"
    assert first_name_from_full("José García") == "José"
    assert first_name_from_full("Dr. James Schmitt") == "Dr. Schmitt"
    assert first_name_from_full("Mrs Smith") is None
    assert first_name_from_full("Mr. John Smith") == "John"


def test_short_name_never_ends_on_a_connector():
    from app.draft.voice import short_name
    assert short_name("CORTEX Chiropractic & Clinical Neuroscience") == "CORTEX Chiropractic"
    assert short_name("Buttonwood Motel and Restaurant") == "Buttonwood Motel"
    assert short_name("Smith & Jones") == "Smith & Jones"
