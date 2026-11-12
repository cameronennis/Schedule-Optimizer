"""Google sign-in.

First run: InstalledAppFlow opens the browser to Google's consent page and listens
on a temporary localhost port for the reply. Google returns a short-lived access
token (~1 hour) and a long-lived refresh token. Only the refresh token is saved,
in the OS keychain via keyring (macOS Keychain / Windows Credential Manager).

Later runs: the saved refresh token is traded for a new access token silently.

Scopes are deliberately narrow, so Google itself prevents writes to real calendars:
read the calendar list, read events, and manage only calendars this app creates.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import keyring
import keyring.errors
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .paths import client_secret_path

SCOPES = [
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "https://www.googleapis.com/auth/calendar.app.created",
]
KEYRING_SERVICE = "Schedule Optimizer"
KEYRING_USER = "google-oauth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
SIGN_IN_TIMEOUT_SECONDS = 300


class SignInRequired(Exception):
    """No usable saved sign-in, and interactive sign-in was not allowed."""


def _client_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"Google OAuth client file not found at {path}. "
            "Download it from Google Cloud Console › Credentials (Desktop app)."
        )
    data = json.loads(path.read_text())
    return data.get("installed") or data["web"]


def _load_saved() -> dict | None:
    raw = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    return json.loads(raw) if raw else None


def _save(refresh_token: str, scopes: list[str]) -> None:
    keyring.set_password(
        KEYRING_SERVICE, KEYRING_USER, json.dumps({"refresh_token": refresh_token, "scopes": sorted(scopes)})
    )


def _forget() -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
    except keyring.errors.PasswordDeleteError:
        pass


def _from_saved(saved: dict, secret_path: Path) -> Credentials | None:
    if sorted(saved.get("scopes", [])) != sorted(SCOPES):
        return None  # scopes changed since last sign-in; ask again
    cfg = _client_config(secret_path)
    creds = Credentials(
        token=None,
        refresh_token=saved["refresh_token"],
        token_uri=cfg.get("token_uri", TOKEN_URI),
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        scopes=SCOPES,
    )
    try:
        creds.refresh(Request())
    except RefreshError:
        return None  # revoked or expired (e.g., consent screen left in Testing)
    return creds


def _sign_in(secret_path: Path) -> Credentials:
    # If the user unticks a permission, oauthlib would raise a cryptic "Scope has
    # changed" warning; relax that so we can report the missing scope clearly.
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
    creds = flow.run_local_server(
        port=0,  # any free port
        prompt="consent",  # guarantees a refresh token is returned
        timeout_seconds=SIGN_IN_TIMEOUT_SECONDS,
        authorization_prompt_message="Opening your browser to sign in with Google…\n{url}",
        success_message="Signed in. You can close this tab and return to Schedule Optimizer.",
    )
    granted = creds.granted_scopes or SCOPES
    granted = set(granted.split() if isinstance(granted, str) else granted)
    missing = [s for s in SCOPES if s not in granted]
    if missing:
        raise PermissionError(
            "Google sign-in finished without these permissions (a checkbox was left unticked): "
            + ", ".join(s.rsplit("/", 1)[-1] for s in missing)
        )
    if not creds.refresh_token:
        raise RuntimeError("Google did not return a refresh token; try signing in again.")
    _save(creds.refresh_token, SCOPES)
    return creds


def get_credentials(interactive: bool = True, secret_path: Path | None = None) -> Credentials:
    """Return working credentials, signing in through the browser only if needed."""
    secret_path = secret_path or client_secret_path()
    saved = _load_saved()
    if saved:
        creds = _from_saved(saved, secret_path)
        if creds:
            return creds
        _forget()
    if not interactive:
        raise SignInRequired("Sign in with Google first.")
    return _sign_in(secret_path)


def is_signed_in() -> bool:
    return _load_saved() is not None


def sign_out() -> None:
    """Revoke the refresh token at Google (best effort) and remove it from the keychain."""
    saved = _load_saved()
    if saved:
        try:
            Request()(url=REVOKE_URI, method="POST", body=f"token={saved['refresh_token']}",
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
        except Exception:
            pass  # offline: the local copy is still removed
    _forget()
