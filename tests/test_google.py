"""Google sign-in and calendar reading, tested without network or a real account."""

import json
from datetime import datetime, timezone

import keyring
import pytest
from keyring.backend import KeyringBackend

from schedule_optimizer import google_auth
from schedule_optimizer.gcal import fetch_events, list_calendars, to_event_input


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        self.store = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, password):
        self.store[(service, user)] = password

    def delete_password(self, service, user):
        if (service, user) not in self.store:
            raise keyring.errors.PasswordDeleteError()
        del self.store[(service, user)]


@pytest.fixture
def memory_keyring():
    previous = keyring.get_keyring()
    kr = MemoryKeyring()
    keyring.set_keyring(kr)
    yield kr
    keyring.set_keyring(previous)


@pytest.fixture
def client_secret(tmp_path):
    p = tmp_path / "client_secret.json"
    p.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "csecret",
                                           "token_uri": "https://oauth2.googleapis.com/token"}}))
    return p


# ---- sign-in -----------------------------------------------------------------

def test_saved_token_is_used_without_browser(memory_keyring, client_secret, monkeypatch):
    google_auth._save("refresh-123", google_auth.SCOPES)
    monkeypatch.setattr(google_auth.Credentials, "refresh", lambda self, req: None)
    monkeypatch.setattr(google_auth, "_sign_in", lambda p: pytest.fail("browser should not open"))
    creds = google_auth.get_credentials(secret_path=client_secret)
    assert creds.refresh_token == "refresh-123"


def test_revoked_token_falls_back_to_browser(memory_keyring, client_secret, monkeypatch):
    google_auth._save("stale", google_auth.SCOPES)

    def refresh_fails(self, req):
        raise google_auth.RefreshError("invalid_grant")

    monkeypatch.setattr(google_auth.Credentials, "refresh", refresh_fails)
    monkeypatch.setattr(google_auth, "_sign_in", lambda p: "NEW")
    assert google_auth.get_credentials(secret_path=client_secret) == "NEW"
    assert not google_auth.is_signed_in()  # stale token was removed


def test_scope_change_forces_new_sign_in(memory_keyring, client_secret, monkeypatch):
    google_auth._save("old", ["https://www.googleapis.com/auth/calendar"])
    monkeypatch.setattr(google_auth, "_sign_in", lambda p: "NEW")
    assert google_auth.get_credentials(secret_path=client_secret) == "NEW"


def test_non_interactive_raises_when_signed_out(memory_keyring, client_secret):
    with pytest.raises(google_auth.SignInRequired):
        google_auth.get_credentials(interactive=False, secret_path=client_secret)


def test_scopes_never_include_full_calendar_write():
    assert "https://www.googleapis.com/auth/calendar" not in google_auth.SCOPES
    assert "https://www.googleapis.com/auth/calendar.events" not in google_auth.SCOPES


# ---- event conversion --------------------------------------------------------

def ev(**kw):
    base = {"id": "e1", "summary": "Lecture", "status": "confirmed",
            "start": {"dateTime": "2026-11-18T09:00:00-08:00"},
            "end": {"dateTime": "2026-11-18T10:20:00-08:00"}}
    base.update(kw)
    return base


def test_timed_event_converts():
    e = to_event_input(ev())
    assert e.title == "Lecture" and not e.skippable
    assert e.start == datetime(2026, 11, 18, 17, 0, tzinfo=timezone.utc)
    assert (e.end - e.start).total_seconds() == 80 * 60


@pytest.mark.parametrize("item", [
    ev(status="cancelled"),
    ev(start={"date": "2026-11-18"}, end={"date": "2026-11-19"}),
    ev(transparency="transparent"),
    ev(attendees=[{"email": "me@x", "self": True, "responseStatus": "declined"}]),
])
def test_non_blocking_events_are_dropped(item):
    assert to_event_input(item) is None


def test_accepted_invite_is_kept():
    assert to_event_input(ev(attendees=[{"self": True, "responseStatus": "accepted"}])) is not None


# ---- API calls with a fake service -------------------------------------------

class FakeRequest:
    def __init__(self, resp):
        self.resp = resp

    def execute(self):
        return self.resp


class FakeService:
    """Mimics service.calendarList().list(...) and service.events().list(...)."""

    def __init__(self, calendars, events_by_cal):
        self.calendars, self.events_by_cal, self.calls = calendars, events_by_cal, []

    def calendarList(self):
        return self

    def events(self):
        return self

    def list(self, pageToken=None, calendarId=None, **kw):
        self.calls.append({"calendarId": calendarId, "pageToken": pageToken, **kw})
        pages = self.events_by_cal[calendarId] if calendarId else [self.calendars]
        i = int(pageToken or 0)
        resp = {"items": pages[i]}
        if i + 1 < len(pages):
            resp["nextPageToken"] = str(i + 1)
        return FakeRequest(resp)


def test_fetch_events_pages_dedupes_and_expands_recurring():
    shared = ev(id="shared", summary="Group meeting",
                start={"dateTime": "2026-11-18T12:00:00-08:00"}, end={"dateTime": "2026-11-18T13:00:00-08:00"})
    svc = FakeService([], {
        "primary": [[ev(id="a")], [shared]],  # two pages
        "work": [[shared, ev(id="b", start={"date": "2026-11-19"}, end={"date": "2026-11-20"})]],
    })
    start = datetime(2026, 11, 18, tzinfo=timezone.utc)
    out = fetch_events(svc, ["primary", "work"], start, start.replace(day=25))
    assert [e.id for e in out] == ["a", "shared"]
    assert all(c["singleEvents"] is True for c in svc.calls)
    assert svc.calls[0]["timeMin"] == "2026-11-18T00:00:00Z"


def test_list_calendars_puts_primary_first():
    svc = FakeService([{"id": "b", "summary": "Birthdays", "accessRole": "reader"},
                       {"id": "me@x", "summary": "Me", "primary": True, "accessRole": "owner",
                        "timeZone": "America/Los_Angeles"}], {})
    cals = list_calendars(svc)
    assert cals[0].primary and cals[0].time_zone == "America/Los_Angeles"
