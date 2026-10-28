"""DNC subdomain + email-domain cross-checks (#26) and per-path cache keying (#41)."""
import pytest

from app.db.migrate import migrate, connect
from app.db.models import DncEntry
from app.db.repo import add_dnc
from app.safety.dnc import is_dnc
from app.audit.fetcher import _cache_dir_for


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "dnc.db"
    migrate(db)
    c = connect(db)
    add_dnc(c, DncEntry(domain="acme.com", reason="customer"))
    yield c
    c.close()


def test_subdomain_url_blocked(conn):
    assert is_dnc(conn, domain="https://shop.acme.com") is not None


def test_email_at_dnc_domain_blocked(conn):
    assert is_dnc(conn, email="bob@acme.com") is not None
    assert is_dnc(conn, email="bob@mail.acme.com") is not None


def test_lookalike_not_blocked(conn):
    assert is_dnc(conn, domain="https://notacme.com", email="x@other.com") is None
    # 'fakeacme.com' must NOT be caught by the acme.com rule.
    assert is_dnc(conn, domain="https://fakeacme.com") is None


def test_cache_dir_distinct_per_path():
    a = _cache_dir_for("https://x.com/")
    b = _cache_dir_for("https://x.com/menu")
    assert a != b
    # Same URL is stable.
    assert _cache_dir_for("https://x.com/menu") == b
