"""Gmail MIME headers stay valid with awkward company names (Change 10)."""
import base64
from email import message_from_bytes
from email.utils import getaddresses

from app.draft.gmail_client import _build_mime


def _parse(raw_b64: str):
    raw = base64.urlsafe_b64decode(raw_b64.encode("ascii"))
    return message_from_bytes(raw)


def test_comma_company_stays_one_recipient():
    raw = _build_mime("owner@smithjones.com", "Smith, Jones & Co",
                      "Quick idea for Smith, Jones & Co", "hi", "<p>hi</p>")
    msg = _parse(raw)
    addrs = getaddresses([msg["To"]])
    assert len(addrs) == 1
    assert addrs[0][1] == "owner@smithjones.com"


def test_newline_injection_stripped():
    raw = _build_mime("owner@x.com", "Evil\r\nBcc: attacker@x.com",
                      "subj\r\nX-Injected: 1", "hi", "<p>hi</p>")
    msg = _parse(raw)
    assert "X-Injected" not in msg
    assert msg["Subject"] == "subj X-Injected: 1"
