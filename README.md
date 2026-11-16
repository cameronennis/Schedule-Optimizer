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

## Natural Language Input (optional)

Without an API key or Claude Desktop, the app is 100% functional, offline too. Claude never places blocks: it proposes changes to tasks, event skip costs, preferences and holds (time set aside, such as a break), you approve them, and the solver re-plans. Set it up in Preferences › Natural Language Input.

**API key** (billed per use): paste a key from the Claude Console; it's stored in the OS keychain. Chat in the Claude tab, with a monthly spending cap. Claude can only propose; you click **Apply**.

**Claude Desktop** (uses your Claude plan): click **Add it for me** (it saves a copy of Claude Desktop's config first, so **Undo** restores it), then fully quit and reopen Claude Desktop. Manual steps are under "Set it up manually instead." It works only in chats you activate: **Start a Claude chat** copies `ACTIVATE SCHEDULE OPTIMIZER FOR THIS CHAT (code …)` to paste as a chat's first message; a code works until you end it in Preferences, which lists active chats so you can end them one at a time. Tools: `get_plan`, `get_tasks`, `create_task`, `propose_changes`, `apply_changes`, `solve`, `explain_block`. New plans still need **Approve plan** in the app.

## Command line

```bash
uv run schedule-optimizer task add "Chem midterm" --hours 8 --due "2026-11-19 09:00" --priority 1
uv run schedule-optimizer task add "Reading" --weekly-hours 3 --when evening
uv run schedule-optimizer task list
uv run schedule-optimizer log 1 45            # 45 minutes on task 1, ending now
uv run schedule-optimizer rule add Lecture --skip-for 2   # skippable only for priority 1–2 work
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
