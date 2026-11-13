"""Fetch busy events for planning, using the calendars chosen in Preferences."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlmodel import Session

from . import google_auth, store
from .domain import EventInput
from .gcal import CalendarInfo, calendar_service, fetch_events, list_calendars, user_time_zone


class CalendarsNotChosen(Exception):
    pass


def connect(interactive: bool = True):
    return calendar_service(google_auth.get_credentials(interactive=interactive))


def busy_events(
    s: Session,
    now: datetime,
    service=None,
    choose: Callable[[list[CalendarInfo]], list[str]] | None = None,
    force_choose: bool = False,
) -> list[EventInput]:
    """Events from the busy calendars over the planning horizon.

    `choose` is called to pick calendars when none are saved (or force_choose);
    without it, CalendarsNotChosen is raised instead.
    """
    service = service or connect()
    prefs = store.get_prefs(s)
    # The app's own Optimizer calendar never counts as busy (it holds the plan itself).
    calendars = [c for c in list_calendars(service) if c.id != prefs.optimizer_calendar_id]
    if prefs.time_zone is None:
        tz = user_time_zone(calendars)
        if hasattr(tz, "key"):
            store.update_prefs(s, time_zone=tz.key)

    known = {c.id for c in calendars}
    busy = [c for c in prefs.busy_calendars if c in known]
    if force_choose or not busy:
        if choose is None:
            raise CalendarsNotChosen("Choose which calendars block time first.")
        busy = choose(calendars)
        store.update_prefs(s, busy_calendars=busy)
    prefs = store.get_prefs(s)
    return fetch_events(service, busy, now, now + timedelta(days=prefs.horizon_days))


def busy_events_if_connected(s: Session, now: datetime) -> tuple[list[EventInput], str | None]:
    """Busy events when signed in with calendars chosen; otherwise none, plus a note.

    Never opens a browser, so it's safe from the Claude Desktop helper.
    """
    if os.environ.get("SCHEDULE_OPTIMIZER_OFFLINE"):
        return [], "Offline mode (SCHEDULE_OPTIMIZER_OFFLINE is set)."
    if not google_auth.is_signed_in():
        return [], "Not signed in to Google, so calendar events weren't included."
    if not store.get_prefs(s).busy_calendars:
        return [], "No busy calendars chosen in Preferences, so calendar events weren't included."
    try:
        return busy_events(s, now, service=connect(interactive=False)), None
    except Exception as e:  # noqa: BLE001 (offline, revoked token, …): plan without events
        return [], f"Couldn't read Google Calendar ({e}); planned without calendar events."
