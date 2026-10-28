"""Make tests independent of the developer's private config.local.json.

The public config.json ships with an empty postal address and
require_postal_address=true, so a fresh clone would otherwise fail every QA test
on the compliance blocker. Tests that care about the address override it again.
"""
import pytest

from app.draft import voice


@pytest.fixture(autouse=True)
def _test_postal_address(monkeypatch):
    monkeypatch.setattr(voice, "SENDER_POSTAL_ADDRESS", "Test Studio, 123 Main St, Anytown, PA 00000")
