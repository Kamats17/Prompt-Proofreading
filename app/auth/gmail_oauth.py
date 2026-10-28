"""Gmail OAuth, two separate tokens.

  compose token (auth/.gmail_token.json)       scope gmail.compose
      Creates drafts. NOTE: Google's gmail.compose scope CAN also send mail; the
      "never send" guarantee is enforced in code (gmail_client exposes only
      drafts.create, and tests/test_no_gmail_send.py fails the build on any send
      call), not by the scope.
  read token    (auth/.gmail_read_token.json)  scope gmail.readonly
      Used only by `pp-agent sync` to see whether drafts were sent, replied to,
      bounced, or opted out. Read-only: cannot send, modify, or delete.

First run of each: `pp-agent gmail-auth` / `pp-agent gmail-auth --read` opens a
browser for consent. The web dashboard never starts an interactive login; it
raises GmailAuthError with the command to run instead.
"""
import json
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from app.settings import (
    GMAIL_CREDENTIALS_PATH, GMAIL_TOKEN_PATH, GMAIL_SCOPE,
    GMAIL_READ_TOKEN_PATH, GMAIL_READ_SCOPE,
)


class GmailAuthError(Exception):
    """Gmail credentials missing/expired and cannot be refreshed non-interactively."""


def _paths(read: bool) -> tuple[Path, list[str], str]:
    if read:
        return Path(GMAIL_READ_TOKEN_PATH), [GMAIL_READ_SCOPE], "pp-agent gmail-auth --read"
    return Path(GMAIL_TOKEN_PATH), [GMAIL_SCOPE], "pp-agent gmail-auth"


def _save_credentials(creds: Credentials, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")


def get_credentials(*, force_login: bool = False, read: bool = False,
                    interactive: bool = True) -> Credentials:
    """Load creds, refresh silently, or (only if interactive) run the consent flow."""
    token_path, scopes, cmd = _paths(read)
    creds: Credentials | None = None
    if token_path.exists() and not force_login:
        try:
            creds = Credentials.from_authorized_user_info(
                json.loads(token_path.read_text(encoding="utf-8")), scopes)
        except (json.JSONDecodeError, ValueError):
            creds = None

    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_credentials(creds, token_path)
            return creds
        except RefreshError as e:
            # Typical when the OAuth app is in Google "Testing" mode: refresh
            # tokens expire after 7 days.
            if not interactive:
                raise GmailAuthError(f"Gmail login expired ({e}). Run `{cmd}` in a terminal.") from e

    if not interactive:
        raise GmailAuthError(f"Gmail is not authorized. Run `{cmd}` in a terminal.")

    cred_path = Path(GMAIL_CREDENTIALS_PATH)
    if not cred_path.exists():
        raise GmailAuthError(
            f"Gmail OAuth client missing at {cred_path}.\n"
            "  1. Google Cloud Console -> APIs & Services -> Credentials\n"
            "  2. Create OAuth client ID -> Application type: Desktop app\n"
            "  3. Download JSON, save as auth/credentials.json")
    flow = InstalledAppFlow.from_client_secrets_file(str(cred_path), scopes)
    creds = flow.run_local_server(port=0)
    _save_credentials(creds, token_path)
    return creds


def build_gmail_service(*, read: bool = False, interactive: bool = False):
    """Authed Gmail service. Non-interactive by default (safe inside the server)."""
    creds = get_credentials(read=read, interactive=interactive)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)
