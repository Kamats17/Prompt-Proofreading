"""Signature footer never leaks an empty or placeholder postal address."""
from app.draft import voice


def test_no_address_rendered_when_empty(monkeypatch):
    monkeypatch.setattr(voice, "SENDER_POSTAL_ADDRESS", "")
    out = voice.render_signature_html()
    assert "VERIFY" not in out
    assert "reply" in out.lower() and "email again" in out.lower()  # opt-out line present
    assert "solicitation" in out.lower()  # ad identification present


def test_placeholder_address_never_rendered(monkeypatch):
    monkeypatch.setattr(voice, "SENDER_POSTAL_ADDRESS",
                        "[VERIFY LOCALLY - set your business postal address]")
    assert "VERIFY" not in voice.render_signature_html()


def test_real_address_rendered(monkeypatch):
    monkeypatch.setattr(voice, "SENDER_POSTAL_ADDRESS", "PO Box 1, Pittsburgh, PA")
    assert "PO Box 1, Pittsburgh, PA" in voice.render_signature_html()
