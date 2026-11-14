# Schedule Optimizer

A local desktop app that reads Google Calendar, takes tasks with weights, and uses OR-Tools CP-SAT to suggest what to do and when. The full design is in `docs/PLAN.md` (exported from the "Schedule Optimizer: Build Plan" Claude Doc, https://claude.ai/artifact/YZnXT2URxkNqeAubekyD6J). Read it first.

## How to work with me

- Claude does most of the work. Build whole phases at a time, in the order in `docs/PLAN.md`, with pytest tests, and run them yourself.
- Stop only where I'm needed: Google sign-in, restarting Claude Desktop, pasting TwiceDaily, decisions. Tell me each phase's checkpoint command so I can try it.
- Keep answers brief. Don't answer questions I didn't ask.
- Use American English spellings.

## Decisions already made

- Python 3.12, uv, NiceGUI in native mode (pywebview), SQLite via SQLModel, OR-Tools CP-SAT, google-api-python-client + google-auth-oauthlib, keyring, httpx, APScheduler, pytest, PyInstaller.
- Time unit = 5 minutes (integers). A 7-day horizon is 2,016 units.
- `AddNoOverlap` across all intervals, padded by a 5-minute buffer.
- All weights are in points per hour, so skip costs and priorities are comparable.
- Most rules are soft with large penalties. True infeasibility is diagnosed with assumption literals and `SufficientAssumptionsForInfeasibility()`.
- Re-planning ("what now?") adds a small penalty for moving blocks, so the plan doesn't reshuffle after every progress log.
- The app writes only to its own "Optimizer" Google calendar, never to real ones.
- Claude never places blocks. It changes solver inputs, I approve, CP-SAT re-plans.
- Claude is optional ("Natural Language Input" in Preferences, no mode dropdown): an API key enables the in-app Claude tab (always shown, disabled with a note without a key or connection), and Claude Desktop is set up separately. Without either the app is 100% functional, offline too.
- Completed tasks are deleted 7 days after they're marked done.
- Plan blocks are written to the Optimizer calendar only after I click "Approve plan."
- Preferences (awake window 8:00–23:00, 6 h max focus/day, 5-min buffer, 7-day horizon) have defaults but are editable in a first-run setup step and in Preferences.
- Event title rules (e.g., "Lecture" → skippable, cost 4) have no built-in defaults. I create them by manual entry or through Claude.
- No phone access and no hosted version.
- Google OAuth client file lives at `Secrets/client_secret.json` (capital S, gitignored). Never print or commit it.
- A task's longest block (max_block) is soft: exceeding it costs 0.5 points per extra hour, less than any task's value, so deadlines and avoiding skips win. Blocks of the same task need at least 30 minutes between them (otherwise it's one longer block), as a fixed rule, not a preference.
- Urgency is a small "earlier is better" nudge for deadline tasks, not a multiplier on points per hour, so skip-cost math stays in plain points per hour.
- Platform: macOS first. Keep Windows possible throughout (no macOS-only paths, APIs, or shell assumptions without a cross-platform fallback), but don't implement Windows support yet.

## Claude Desktop mode

- A separate entry point, `mcp_server.py`, built with the MCP Python SDK. It imports the same core modules and uses the same SQLite file (WAL mode on).
- Tools: `get_plan`, `get_tasks`, `create_task`, `propose_changes`, `apply_changes`, `solve`, `explain_block`.
- Fully optional: it works only in chats I activate: "Start a Claude chat" in the app copies "ACTIVATE SCHEDULE OPTIMIZER FOR THIS CHAT (code XXX-XXX)", and every tool call needs an active code (12 h, revocable). Claude Desktop can't start a connector switched off per chat, so this is enforced on our side.
- During development, "Add it for me" in Preferences adds a `uv run` entry to Claude Desktop's config (backing up the original so Undo restores it exactly); manual steps are in a collapsed section. Then fully quit and reopen Claude Desktop.
- For release, build the helper as a standalone binary with PyInstaller and pack it with the mcpb CLI. Ship the `.mcpb` inside the app; a "Connect to Claude" button opens it so Claude Desktop shows its install window.

## Gotchas found so far

- Google OAuth consent screen must be "In production," not "Testing," or refresh tokens expire after 7 days. The unverified-app warning is fine for personal use.
- Publishing to "In production" requires the Branding page: app name, support email, developer email, a home page URL (GitHub repo, with `github.com` as an authorized domain) and a privacy policy URL (`PRIVACY.md` in the repo). Don't upload a logo; that forces verification.
- The Google Calendar API must be enabled separately in the Cloud project, or calls fail with 403 `accessNotConfigured`.
- Use `singleEvents=True` when listing events so recurring lectures come back as separate instances.
- NiceGUI is v4. Always pass `host="127.0.0.1"` to `ui.run` (the default in browser mode listens on the whole network). Async `@ui.refreshable` sections must be awaited; from sync code use `ui_app.load_later`. UI tests use `nicegui.testing.user_simulation(root=...)` with pytest-asyncio.
- The desktop app's preview runner (`preview_start`) hangs on this project; to eyeball the UI, run a script with `.venv/bin/python` in the background and open http://127.0.0.1:8765 in the built-in browser.
- MCP Python SDK is v2: `FastMCP` was renamed `MCPServer`. The helper uses the low-level `mcp.server.lowlevel.Server` with `on_list_tools`/`on_call_tool`, because tool schemas are shared JSON specs in `claude_tools.py`. Types come from `mcp_types` (snake_case fields such as `input_schema`, `is_error`).
- Solver speed: avoid helper variables that can take many values for one plan (they make CP-SAT time out without proving optimality). The re-plan stability overlap uses four linear upper bounds plus a per-span total and a time-order rule; with these, re-plans prove optimal in about 1 s on real data.
- Tests set `SCHEDULE_OPTIMIZER_OFFLINE=1` so nothing touches the real Google account or keychain token.
- Don't build a custom "Sign in with Claude." Anthropic doesn't allow third-party apps to collect Claude.ai credentials or route requests through subscriptions. API-key mode uses a key from the Claude Console stored in keyring.

## Still open

- Which Google calendars count as busy (block time). Show me the list after sign-in and let me pick.
- What TwiceDaily (my Google Apps Script) does today, and its weather source. I'll paste it when we reach the TwiceDaily phase.
