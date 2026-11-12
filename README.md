# Schedule Optimizer

A local desktop app that reads Google Calendar, takes your tasks and their weights, and uses OR-Tools CP-SAT to suggest what to do and when. Design: [docs/PLAN.md](docs/PLAN.md). Privacy: [PRIVACY.md](PRIVACY.md).

## Setup

```bash
uv sync
```

Google sign-in needs an OAuth client file (Desktop app) at `Secrets/client_secret.json`.

## App window

```bash
uv run schedule-optimizer-app             # native window
uv run schedule-optimizer-app --browser   # same UI in a browser tab (http://127.0.0.1:8765)
```

The first launch walks through setup: your day, Google sign-in, and which calendars block time.

## Command line

```bash
uv run schedule-optimizer task add "Chem midterm" --hours 8 --due "2026-11-19 09:00" --priority 5
uv run schedule-optimizer task add "Reading" --weekly-hours 3 --when evening
uv run schedule-optimizer task list
uv run schedule-optimizer log 1 45            # 45 minutes on task 1, ending now
uv run schedule-optimizer rule add Lecture --cost 4
uv run schedule-optimizer events              # upcoming events and how they're treated
uv run schedule-optimizer plan                # solve against your calendar (or --offline)
uv run schedule-optimizer approve             # also writes blocks to your "Optimizer" calendar
uv run schedule-optimizer sync                # rewrite approved blocks to Google
uv run schedule-optimizer prefs show
```

Data lives in one SQLite file in the app data folder (`prefs show` prints its path). The Google sign-in token is in the OS keychain.

## Tests

```bash
uv run pytest
```
