from app.sources._normalize import normalize_url, normalize_email, normalize_phone, domain_of


def test_normalize_url_adds_https():
    assert normalize_url("example.com") == "https://example.com"
    assert normalize_url("www.example.com") == "https://example.com"
    assert normalize_url("http://Example.com/About") == "https://example.com/About"


def test_normalize_url_strips_tracking():
    assert normalize_url("https://example.com/?utm_source=foo&id=1") == "https://example.com/?id=1"


def test_normalize_url_invalid():
    assert normalize_url("") is None
    assert normalize_url("not a url") is None
    assert normalize_url("No Website") is None
    assert normalize_url("nodot") is None


def test_domain_of():
    assert domain_of("https://www.foo.com/bar") == "foo.com"
    assert domain_of("foo.com") == "foo.com"


def test_normalize_email():
    assert normalize_email(" Bob@Example.COM ") == "bob@example.com"
    assert normalize_email("bad-email") is None
    assert normalize_email("") is None


def test_normalize_phone():
    assert normalize_phone("(412) 555-1234") == "(412) 555-1234"
    assert normalize_phone("4125551234") == "(412) 555-1234"
    assert normalize_phone("1-412-555-1234") == "(412) 555-1234"
    assert normalize_phone("") is None
