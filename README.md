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

## Claude (optional)

Claude is optional and off by default; the app works fully without it. Claude never places blocks. It proposes changes to tasks, event skip costs, preferences and holds (time set aside, such as a break). You approve them, and the solver re-plans. Pick a mode in Preferences › Claude.

**Claude Desktop** (uses your Claude plan). The helper does nothing unless this mode is on: with Claude set to Off, it offers Claude Desktop no tools. Even when on, it works only in chats you activate: **Start a Claude chat** copies `ACTIVATE SCHEDULE OPTIMIZER FOR THIS CHAT (code …)` to paste into a chat; codes last 12 hours and can be ended in Preferences. Releases will install it with a "Connect to Claude" button. During development, add the helper to Claude Desktop's config (Settings › Developer › Edit Config), then fully quit and reopen Claude Desktop. Preferences shows the exact entry for this checkout; it looks like:

```json
{"mcpServers": {"schedule-optimizer": {
  "command": "/Users/you/.local/bin/uv",
  "args": ["run", "--directory", "/path/to/Schedule Optimizer", "schedule-optimizer-mcp"]}}}
```

Tools: `get_plan`, `get_tasks`, `create_task`, `propose_changes`, `apply_changes`, `solve`, `explain_block`. Claude Desktop asks before running the ones that change things. New plans, and breaks, still need **Approve plan** in the app before they're written to the Optimizer calendar.

**API key** (billed per use). Paste a key from the Claude Console in Preferences; it's stored in the OS keychain. A Claude tab appears for chatting, with a monthly spending cap. In this mode Claude can only propose; you click **Apply** in the app.

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
