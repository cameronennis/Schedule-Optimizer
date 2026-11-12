"""Write approved plan blocks to the app's own "Optimizer" Google calendar.

Only this calendar is ever written to; the OAuth scope (calendar.app.created) lets
Google enforce that. Syncing is idempotent: each event carries a key built from its
task and times in extendedProperties.private, so re-syncing the same plan changes
nothing, and a re-plan only deletes and inserts the blocks that actually moved.
Changes go out in batched requests (one HTTP call per 50 changes).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from googleapiclient.errors import HttpError
from sqlmodel import Session

from . import store
from .gcal import _paged, _rfc3339
from .tables import PlanBlock

CALENDAR_NAME = "Optimizer"
APP_TAG = "schedule-optimizer"
BATCH_SIZE = 50


@dataclass
class SyncResult:
    inserted: int = 0
    deleted: int = 0
    trimmed: int = 0
    kept: int = 0


def block_key(task_id: int, start: datetime, end: datetime) -> str:
    return f"{task_id}|{_rfc3339(start)}|{_rfc3339(end)}"


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
    """Make the Optimizer calendar match the approved blocks from `now` on."""
    now = now or datetime.now(timezone.utc)
    cal_id = ensure_calendar(service, s)
    blocks = store.approved_blocks(s, now)
    titles = {t.id: t.title for t in store.list_tasks(s, include_done=True)}
    wanted = {block_key(b.task_id, b.start, b.end): b for b in blocks}

    existing = list(_paged(
        service.events().list, calendarId=cal_id, timeMin=_rfc3339(now), singleEvents=True,
        privateExtendedProperty=f"app={APP_TAG}", maxResults=2500,
    ))
    result = SyncResult()
    requests = []
    for ev in existing:
        key = ev.get("extendedProperties", {}).get("private", {}).get("block_key")
        start = datetime.fromisoformat(ev["start"]["dateTime"]) if "dateTime" in ev.get("start", {}) else None
        if key in wanted:
            wanted[key].google_event_id = ev["id"]
            s.add(wanted.pop(key))
            result.kept += 1
        elif start is not None and start < now:
            # Already under way: keep what happened, end it now instead of deleting it.
            body = {"end": {"dateTime": _rfc3339(now)}}
            requests.append((("trim", ev["id"]), service.events().patch(calendarId=cal_id, eventId=ev["id"], body=body)))
        else:
            requests.append((("delete", ev["id"]), service.events().delete(calendarId=cal_id, eventId=ev["id"])))
    for key, b in wanted.items():
        body = _event_body(b, titles.get(b.task_id, "Task"))
        requests.append((("insert", key), service.events().insert(calendarId=cal_id, body=body)))

    errors = []
    for (kind, ref), response, error in _run_batches(service, requests):
        if error is not None:
            if kind == "delete" and getattr(getattr(error, "resp", None), "status", None) in (404, 410):
                result.deleted += 1  # already gone
                continue
            errors.append(f"{kind} {ref}: {error}")
        elif kind == "insert":
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
    """Approve the proposed plan, then write it to Google if a service is given."""
    count = store.approve_plan(s)
    if service is None or count == 0:
        return count, None
    return count, sync_approved(service, s, now)

