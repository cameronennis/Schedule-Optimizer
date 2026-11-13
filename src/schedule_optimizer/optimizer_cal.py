"""Write approved plan blocks, and holds (breaks), to the app's own "Optimizer" Google calendar.

Only this calendar is ever written to; the OAuth scope (calendar.app.created) lets
Google enforce that. Syncing is idempotent: each event carries a key built from its
task and times in extendedProperties.private, so re-syncing the same plan changes
nothing, and a re-plan only deletes and inserts the blocks that actually moved.
Changes go out in batched requests (one HTTP call per 50 changes). The calendar mirrors the
database: re-plan rules (cutting off or merging a block under way) live in store.approve_plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from googleapiclient.errors import HttpError
from sqlmodel import Session

from . import store
from .gcal import _paged, _rfc3339
from .tables import Hold, PlanBlock

CALENDAR_NAME = "Optimizer"
APP_TAG = "schedule-optimizer"
BATCH_SIZE = 50


@dataclass
class SyncResult:
    inserted: int = 0
    deleted: int = 0
    trimmed: int = 0
    kept: int = 0


HOLD_COLOR = "2"  # Google's "Sage", so breaks stand out from work blocks


def block_key(task_id: int, start: datetime, end: datetime) -> str:
    return f"{task_id}|{_rfc3339(start)}|{_rfc3339(end)}"


def hold_key(hold: Hold) -> str:
    return f"hold:{hold.id}|{_rfc3339(hold.start)}|{_rfc3339(hold.end)}"


def ensure_calendar(service, s: Session) -> str:
    """Return the Optimizer calendar's ID, creating it the first time."""
    prefs = store.get_prefs(s)
    if prefs.optimizer_calendar_id:
        try:
            service.calendars().get(calendarId=prefs.optimizer_calendar_id).execute()
            return prefs.optimizer_calendar_id
        except HttpError as e:
            if e.resp.status not in (403, 404):  # deleted by the user, or not ours any more
                raise
    body = {"summary": CALENDAR_NAME, "description": "Work blocks planned by Schedule Optimizer.",
            "timeZone": prefs.time_zone or "UTC"}
    created = service.calendars().insert(body=body).execute()
    store.update_prefs(s, optimizer_calendar_id=created["id"])
    return created["id"]


def _event_body(block: PlanBlock, title: str) -> dict:
    return {
        "summary": title,
        "description": "Planned by Schedule Optimizer.",
        "start": {"dateTime": _rfc3339(block.start)},
        "end": {"dateTime": _rfc3339(block.end)},
        "reminders": {"useDefault": False, "overrides": []},
        "extendedProperties": {"private": {
            "app": APP_TAG, "task_id": str(block.task_id),
            "block_key": block_key(block.task_id, block.start, block.end),
        }},
    }


def _hold_body(hold: Hold) -> dict:
    return {
        "summary": hold.title,
        "description": "Time set aside by Schedule Optimizer.",
        "start": {"dateTime": _rfc3339(hold.start)},
        "end": {"dateTime": _rfc3339(hold.end)},
        "colorId": HOLD_COLOR,
        "reminders": {"useDefault": False, "overrides": []},
        "extendedProperties": {"private": {"app": APP_TAG, "hold_id": str(hold.id), "block_key": hold_key(hold)}},
    }


def _run_batches(service, requests: list) -> list[tuple[object, dict | None, Exception | None]]:
    """Execute (tag, request) pairs in batches; return (tag, response, error) for each."""
    results: list = []
    for i in range(0, len(requests), BATCH_SIZE):
        chunk = requests[i:i + BATCH_SIZE]
        batch = service.new_batch_http_request()
        for n, (tag, req) in enumerate(chunk):
            def cb(request_id, response, exception, tag=tag):
                results.append((tag, response, exception))
            batch.add(req, callback=cb, request_id=str(n))
        batch.execute()
    return results


def sync_approved(service, s: Session, now: datetime | None = None) -> SyncResult:
    """Make the Optimizer calendar mirror the approved blocks and holds from `now` on.

    Calendar events that end after `now` are compared with the database:
    - same key (task and times): kept;
    - the event of a block whose times changed (cut off or merged at a re-plan): patched to match;
    - anything else: deleted.
    Blocks and holds without an event yet are inserted.
    """
    now = now or datetime.now(timezone.utc)
    cal_id = ensure_calendar(service, s)
    # Include blocks that just ended: one cut off at the re-plan may still have a longer event.
    blocks = store.approved_blocks(s, now - timedelta(days=1))
    titles = {t.id: t.title for t in store.list_tasks(s, include_done=True)}
    wanted: dict[str, PlanBlock | Hold] = {block_key(b.task_id, b.start, b.end): b for b in blocks}
    wanted |= {hold_key(h): h for h in store.list_holds(s, now)}
    by_event_id = {b.google_event_id: k for k, b in wanted.items()
                   if isinstance(b, PlanBlock) and b.google_event_id}

    existing = list(_paged(
        service.events().list, calendarId=cal_id, timeMin=_rfc3339(now), singleEvents=True,
        privateExtendedProperty=f"app={APP_TAG}", maxResults=2500,
    ))
    result = SyncResult()
    requests = []
    for ev in existing:
        key = ev.get("extendedProperties", {}).get("private", {}).get("block_key")
        if key in wanted:
            item = wanted.pop(key)
            if isinstance(item, PlanBlock):
                item.google_event_id = ev["id"]
                s.add(item)
            result.kept += 1
        elif ev["id"] in by_event_id and by_event_id[ev["id"]] in wanted:
            block = wanted.pop(by_event_id[ev["id"]])
            body = _event_body(block, titles.get(block.task_id, "Task"))
            requests.append((("patch", ev["id"]), service.events().patch(calendarId=cal_id, eventId=ev["id"], body=body)))
        else:
            requests.append((("delete", ev["id"]), service.events().delete(calendarId=cal_id, eventId=ev["id"])))
    for key, b in wanted.items():
        if b.end <= now:
            continue  # already over, and it never had an event to fix
        body = _hold_body(b) if isinstance(b, Hold) else _event_body(b, titles.get(b.task_id, "Task"))
        requests.append((("insert", key), service.events().insert(calendarId=cal_id, body=body)))

    errors = []
    for (kind, ref), response, error in _run_batches(service, requests):
        if error is not None:
            if kind == "delete" and getattr(getattr(error, "resp", None), "status", None) in (404, 410):
                result.deleted += 1  # already gone
                continue
            errors.append(f"{kind} {ref}: {error}")
        elif kind == "insert":
            if isinstance(wanted[ref], PlanBlock):
                wanted[ref].google_event_id = response["id"]
                s.add(wanted[ref])
            result.inserted += 1
        elif kind == "delete":
            result.deleted += 1
        else:
            result.trimmed += 1
    s.commit()
    if errors:
        raise RuntimeError("Some calendar changes failed:\n" + "\n".join(errors[:5]))
    return result


def approve_and_sync(s: Session, service=None, now: datetime | None = None) -> tuple[int, SyncResult | None]:
    """Approve the proposed plan, then write it (and any holds) to Google if a service is given.

    With no proposed plan, this still syncs, so a new or removed hold reaches the calendar.
    """
    count = store.approve_plan(s)
    if service is None:
        return count, None
    return count, sync_approved(service, s, now)

