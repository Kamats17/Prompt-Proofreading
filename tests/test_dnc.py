import sqlite3
import pytest
from pathlib import Path

from app.db.migrate import migrate, connect
from app.db.models import DncEntry
from app.db.repo import add_dnc
from app.safety.dnc import is_dnc, _normalize_company, _fuzzy_match


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "test.db"
    migrate(db)
    c = connect(db)
    yield c
    c.close()


def test_normalize_strips_legal_suffix():
    assert _normalize_company("Acme Corp.") == "acme"
    assert _normalize_company("Acme, LLC") == "acme"


def test_fuzzy_match_overlap_required():
    # Common generic tokens should NOT cause a match
    assert not _fuzzy_match("acme manufacturing", "smith manufacturing")
    # Real match
    assert _fuzzy_match("acme widgets", "acme widget")


def test_dnc_domain_match(conn):
    add_dnc(conn, DncEntry(domain="example.com", reason="customer"))
    assert is_dnc(conn, domain="https://www.example.com/about") is not None
    assert is_dnc(conn, domain="example.com") is not None
    assert is_dnc(conn, domain="other.com") is None


def test_dnc_email_match(conn):
    add_dnc(conn, DncEntry(email="bob@example.com", reason="opted out"))
    assert is_dnc(conn, email="BOB@example.com") is not None
    assert is_dnc(conn, email="alice@example.com") is None


def test_dnc_company_fuzzy(conn):
    add_dnc(conn, DncEntry(company="Acme Widgets, LLC", reason="dupe"))
    assert is_dnc(conn, company="Acme Widgets") is not None
    assert is_dnc(conn, company="Smith Widgets") is None
