"""Verify audit pipeline skips aggregator URLs and DNC matches before calling Claude."""
import pytest

from app.db.migrate import migrate, connect
from app.db.models import Prospect, DncEntry
from app.db.repo import upsert_prospect, add_dnc
from app.audit.pipeline import audit_prospect


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "audit_skip.db"
    migrate(db)
    c = connect(db)
    yield c
    c.close()


def test_audit_skips_aggregator(conn):
    pid = upsert_prospect(conn, Prospect(
        company="Bob via Yelp", url="https://yelp.com/biz/bob", source="manual",
    ))
    result = audit_prospect(conn, pid)
    assert result["ok"] is False
    assert result.get("blocked") == "aggregator"


def test_audit_skips_dnc(conn):
    pid = upsert_prospect(conn, Prospect(
        company="Bob's Diner", url="https://bobsdiner.com", source="manual",
        email="bob@bobsdiner.com",
    ))
    add_dnc(conn, DncEntry(domain="bobsdiner.com", reason="customer"))
    result = audit_prospect(conn, pid)
    assert result["ok"] is False
    assert result.get("blocked") == "dnc"
