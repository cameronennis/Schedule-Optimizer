"""Read calendars and events from the Google Calendar API (read-only here).

Events are converted to the solver's EventInput. These don't block time:
cancelled events, all-day events, events marked "Free", and events you declined.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from googleapiclient.discovery import build

from .domain import EventInput


@dataclass(frozen=True)
class CalendarInfo:
    id: str
    name: str
    primary: bool
    access_role: str  # owner, writer, reader, freeBusyReader
    time_zone: str | None


def calendar_service(credentials):
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def _paged(request_fn, **kwargs):
    token = None
    while True:
        resp = request_fn(pageToken=token, **kwargs).execute()
        yield from resp.get("items", [])
        token = resp.get("nextPageToken")
        if not token:
            return


def list_calendars(service) -> list[CalendarInfo]:
    cals = [
        CalendarInfo(
            id=c["id"],
            name=c.get("summaryOverride") or c.get("summary", c["id"]),
            primary=bool(c.get("primary")),
            access_role=c.get("accessRole", "reader"),
            time_zone=c.get("timeZone"),
        )
        for c in _paged(service.calendarList().list, showHidden=False)
    ]
    return sorted(cals, key=lambda c: (not c.primary, c.name.lower()))


def user_time_zone(calendars: list[CalendarInfo]) -> tzinfo:
    """The primary calendar's time zone, else the computer's current offset."""
    for c in calendars:
        if c.primary and c.time_zone:
            try:
                return ZoneInfo(c.time_zone)
            except ZoneInfoNotFoundError:
                break
    return datetime.now().astimezone().tzinfo or timezone.utc


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def to_event_input(item: dict) -> EventInput | None:
    """Convert one API event to EventInput, or None if it shouldn't block time."""
    if item.get("status") == "cancelled":
        return None
    start, end = item.get("start", {}), item.get("end", {})
    if "dateTime" not in start or "dateTime" not in end:
        return None  # all-day event
    if item.get("transparency") == "transparent":
        return None  # marked "Free"
    for a in item.get("attendees", []):
        if a.get("self") and a.get("responseStatus") == "declined":
            return None
    s = datetime.fromisoformat(start["dateTime"])
    e = datetime.fromisoformat(end["dateTime"])
    if e <= s:
        return None
    return EventInput(id=item["id"], title=item.get("summary", "(no title)"), start=s, end=e)


def fetch_events(service, calendar_ids: list[str], start: datetime, end: datetime) -> list[EventInput]:
    """Busy events from the given calendars between start and end, sorted by start.

    singleEvents=True expands recurring series into separate instances, so one
    lecture in a series can be marked skippable on its own later.
    """
    seen: set[str] = set()
    out: list[EventInput] = []
    for cal_id in calendar_ids:
        for item in _paged(
            service.events().list,
            calendarId=cal_id,
            timeMin=_rfc3339(start),
            timeMax=_rfc3339(end),
            singleEvents=True,
            orderBy="startTime",
            maxResults=2500,
        ):
            ev = to_event_input(item)
            if ev and ev.id not in seen:  # the same invite can appear on two calendars
                seen.add(ev.id)
                out.append(ev)
    return sorted(out, key=lambda e: e.start)
