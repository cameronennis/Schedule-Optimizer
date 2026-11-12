"""Fetch busy events for planning, using the calendars chosen in Preferences."""

from __future__ import annotations

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
    calendars = list_calendars(service)
    prefs = store.get_prefs(s)
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
