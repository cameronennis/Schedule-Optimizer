# Schedule Optimizer: Build Plan

_Exported from the Claude Doc https://claude.ai/artifact/YZnXT2URxkNqeAubekyD6J on 2026-11-12. The architecture diagram is only in the doc._

·

## Goals and design principles

Build a Python desktop app that reads your Google Calendar, takes your tasks and weights, and uses CP-SAT to place work blocks in 5-minute units, with a "what should I do now?" re-plan.

- **Learn by building.** Each phase ends with something runnable, and every new tool is explained before it is used.
- **Local-first.** Your tasks, weights, and progress live in one database file on your laptop.
- **Calendar stays the source of truth.** The app reads your real calendars but writes only to its own "Optimizer" calendar, so a re-plan never touches your real events.
- **Defaults everywhere.** Every weight has a sensible default, so a new task can be just a title and a time estimate.
- **Claude is optional.** Everything works offline with manual entry; Claude adds natural-language entry, explanations, and in-the-moment adjustments.
- **Claude suggests, the solver decides.** Claude only changes the solver's inputs, you approve each change, and CP-SAT places every block.
- **Core first, TwiceDaily second.** The scheduler is built from scratch, then TwiceDaily's features are ported in.

## How the pieces connect

You can drive the optimizer from the app window or from a Claude Desktop chat; both use the same stored data, and resulting blocks are pushed to the Optimizer calendar. The dashed box is the scheduler; Claude Desktop is a separate app you already have.

## Tool glossary

Nineteen tools, each with one job. The "Layer" column matches the boxes in the architecture diagram above.

| Tool | What it does | Layer | Why this one |
|---|---|---|---|
| Python 3.12 | The language everything is written in | All | OR-Tools and Google's client libraries are first-class in Python |
| uv | Creates the project's virtual environment, installs packages, and locks exact versions | Dev setup | One fast tool replaces pip, venv, and requirements files |
| Git + GitHub | Records every change so you can undo mistakes and see your progress; Releases hosts the downloads | Dev setup | Standard; also backs up the code |
| NiceGUI | Builds the interface in Python. It runs a small local web server and draws pages with Vue under the hood | UI | No JavaScript needed to start |
| FastAPI | The web server inside NiceGUI that routes clicks and page loads to your Python functions | UI | Comes with NiceGUI |
| pywebview | Opens a native window that shows the NiceGUI pages instead of a browser tab | UI | Makes it feel like an app; NiceGUI's `native=True` uses it |
| SQLite | A database stored as a single file | Storage | No server; built into Python. WAL mode lets the app and the Claude helper use it at once |
| SQLModel | Defines database tables as Python classes and reads/writes them | Storage | Built on SQLAlchemy and Pydantic, so data is type-checked |
| google-auth-oauthlib | Runs the Google sign-in flow and returns access and refresh tokens | Auth | Google's official library |
| keyring | Saves the Google refresh token (and an Anthropic API key, if used) in macOS Keychain or Windows Credential Manager | Auth | Keeps secrets out of plain-text files |
| google-api-python-client | Calls the Calendar API to list, create, and delete events | Sync | Google's official library |
| OR-Tools CP-SAT | The constraint solver that decides where each block goes | Optimizer | Has interval variables and no-overlap built in |
| httpx | Makes HTTP requests to outside APIs such as weather | Integrations | Simple, modern, supports async |
| APScheduler | Runs jobs on a timer: calendar sync every few minutes, twice-daily briefs | Background | Replaces Apps Script's time-driven triggers |
| MCP Python SDK | Turns plain Python functions into tools Claude Desktop can call | Claude helper | Official SDK for the Model Context Protocol |
| mcpb CLI | Packs the Claude helper into one `.mcpb` extension file that installs with a click | Claude helper | Claude Desktop's own extension format |
| Anthropic Python SDK | Calls Claude directly from the app window, for users who prefer an API key | Claude, API-key mode | Official SDK with tool use |
| pytest | Runs automated tests, such as "no two blocks overlap" | Dev setup | Standard Python test runner |
| PyInstaller | Bundles Python and every dependency into a double-clickable app, and the Claude helper into a standalone program | Packaging | Mature and works with NiceGUI |

## Data model

Five tables in SQLite: Task, EventMeta, Preferences, ProgressLog, and PlanBlock. Calendar events themselves stay in Google; the app stores only your extra parameters for them.

**Task** — anything you want time for.

| Field | Type | Default | Notes |
|---|---|---|---|
| title | text | required |  |
| priority | 1–5 | 3 | Converted to points per hour (see Optimizer) |
| effort_mode | total or weekly | total | Choose one of the next two fields |
| total_minutes | integer | required if total | Total time expected; remaining = total − logged |
| weekly_minutes | integer | required if weekly | Time intention per week; resets each Monday |
| deadline | datetime | none | Only for total mode |
| earliest_start | datetime | none | E.g., can't start the essay until the prompt is out |
| min_block | minutes | 30 | Shortest acceptable block |
| ideal_block | minutes | 90 | Solver is nudged toward this length |
| max_block | minutes | 120 | Longest acceptable block |
| time_of_day | morning, afternoon, evening, or any | any | Soft preference, not a hard rule |
| status | active, paused, done | active |  |

**EventMeta** — your parameters for a Google Calendar event, keyed by its event ID.

| Field | Type | Default | Notes |
|---|---|---|---|
| google_event_id | text | required | Per instance, so one lecture in a recurring series can differ |
| skippable | yes/no | no | Exams and shifts stay fixed |
| skip_cost | points | 4 | Only used if skippable |

Title rules can set defaults, e.g., any event containing "Lecture" starts as skippable with cost 4.

**Preferences** — one row of global settings: awake window (default 8:00–23:00), max focus per day (default 6 h), buffer between blocks (default 5 min), planning horizon (default 7 days), slot size (5 min), and Claude mode (off, Claude Desktop, or API key; default off).

**ProgressLog** — one row per work session you report: task, start, minutes done. This is what makes "what now?" accurate.

**PlanBlock** — the solver's output: task, start, end, and the ID of the event it wrote to the Optimizer calendar.

## Optimizer model in CP-SAT

CP-SAT picks where each task block goes by maximizing one score, subject to hard rules like "nothing overlaps." Everything is measured in integer time units of 5 minutes, so a 7-day horizon is 2,016 units and events at :20 or :50 land exactly.

**Why integers:** CP-SAT only works with whole numbers. Times, durations, and weights are all scaled to integers before solving.

### Variables

- **Task chunks.** Each task gets up to K candidate chunks, where K = remaining minutes ÷ min_block. Each chunk has a presence flag (used or not), a start, and a length between min_block and max_block. Together these form an *optional interval*.
- **Skippable events.** Each gets an optional interval at its fixed time, whose presence flag means "attend."
- **Fixed events and sleep.** Mandatory intervals the solver can't move.

### Hard constraints

1. **No overlap.** `AddNoOverlap` over every interval, with each padded by the 5-minute buffer.
1. **Deadlines and earliest starts.** A used chunk must start after earliest_start and end before the deadline.
1. **Awake window.** Chunks fit inside awake hours and don't cross midnight.
1. **Daily focus cap.** Total task minutes per day stay under the cap. This is the hardest constraint to write, so it comes last.
1. **Symmetry breaking.** Chunk 2 starts after chunk 1 ends, and used chunks come first. This changes nothing about the answer but makes solving much faster.

### Weekly intentions

A weekly task is treated as a total task whose deadline is Sunday night, with remaining = weekly_minutes − minutes logged this week. When the horizon crosses into next week, the app adds a second instance for that week.

### Objective (maximize)

```
\text{score} = \sum_{\text{tasks}} w_t \cdot \text{scheduled}_t - \sum_{\text{tasks}} u_t \cdot w_t \cdot \text{unmet}_t - \sum_{\text{events}} c_e \cdot \text{skipped}_e + \sum_{\text{chunks}} \left( b \cdot \text{inPreferredTime} - a \cdot |d - d_{\text{ideal}}| - f \right)
```

- w = the task's points per hour, from its priority (priority 5 = 10, priority 1 = 2)
- u = urgency, which grows as the deadline nears relative to the work remaining
- c = the event's skip cost
- b = bonus for a chunk in the preferred time of day
- a = penalty per minute a chunk differs from ideal_block
- f = a fixed cost per chunk, which discourages many small blocks (your "one 2-hour block over four 30-minute blocks" rule)

**Keep every weight in points per hour.** Then a skip cost of 4 means "this lecture is worth 24 minutes of top-priority study." Weights you can reason about are weights you can tune.

### The midterm example

Study has priority 5, 10 hours remaining, and a Friday 9:00 deadline. Wednesday's lecture has skip cost 4. If free time runs short, using the lecture's 80 minutes for study gains about 13 points versus a loss of 4, so the solver skips the lecture. If free time is plentiful, attending costs nothing, so it keeps the lecture.

### Re-planning ("what now?")

Re-solve from the current time using logged progress. Add a small penalty for moving a block away from its previous position, so the plan doesn't reshuffle every time you log 20 minutes. Cap solve time at about 5 seconds; CP-SAT returns its best plan so far.

### When no plan fits

Most rules become soft, with large penalties: deadlines, daily caps, and block sizes. A tight week then returns a plan with visible costs ("3 hours of study unmet") instead of failing. Only true facts stay hard, such as two exams at the same time.

When a solve is still infeasible, each relaxable constraint gets an on/off switch passed to CP-SAT as an assumption. `SufficientAssumptionsForInfeasibility()` then returns the small set of rules that conflict, e.g., "chem deadline + max block 120 + Thursday lab." The app shows that set, and Claude can propose fixes for it.

## Google login, calendar sync, weather

You sign in once in your browser; after that the app refreshes its access silently using a token stored in your OS keychain.

### One-time Google Cloud setup

1. Create a Google Cloud project and enable the Google Calendar API.
1. Configure the OAuth consent screen as External and add the full `calendar` scope, which allows reading your calendars and creating the Optimizer calendar.
1. Set the publishing status to **In production**. In Testing status, refresh tokens expire after 7 days, which would log you out weekly. Unverified production apps show a warning screen at sign-in, which is fine for personal use.
1. Create an OAuth client of type **Desktop app** and download its `client_secret.json` into the project.

### How sign-in works at runtime

1. **First launch:** `InstalledAppFlow` opens your browser to Google's sign-in page and briefly listens on localhost for the reply. Google returns an *access token* (valid about 1 hour) and a *refresh token* (long-lived).
1. `keyring` saves the refresh token in your keychain.
1. **Later launches:** the app loads the refresh token and quietly trades it for a new access token. No browser appears.

### Calendar sync

- **Read:** `events.list` on each calendar you select, with `singleEvents=True` so recurring lectures come back as individual instances you can mark separately.
- **Write:** plan blocks go into a dedicated "Optimizer" calendar. Each block carries its task ID in `extendedProperties.private`, so the app can find its own blocks later.
- **Re-plan:** delete the future blocks on the Optimizer calendar and insert the new ones, in one batched request.
- **Later improvement:** use sync tokens so each refresh fetches only what changed.

### Weather

Weather becomes an optional input to the solver, e.g., a bonus for placing a "run" task in dry hours. The source should match whatever TwiceDaily uses; Open-Meteo is a good default because it needs no API key.

## Claude integration

Claude is optional and never places blocks itself: it changes the solver's inputs, you approve each change, and CP-SAT re-plans.

### Three modes (chosen in Preferences)

| Mode | Where you talk to Claude | Setup | Cost |
|---|---|---|---|
| Off (default) | Nowhere; manual entry only | None | Free; works offline |
| Claude Desktop | Claude Desktop's normal chat window | Install the extension once | Your existing Claude plan |
| API key | Inside the scheduler's own window | Paste a key from the Claude Console once | Billed per use through the Console |

### What Claude can do

- **Natural-language entry.** "6 hours for the chem midterm by Friday, mornings preferred" becomes a Task with defaults filled in.
- **"Why?" explanations.** The solver reports each score term, and Claude turns it into a sentence, e.g., "Skipped Wednesday's lecture: studying then gained 13 points versus a skip cost of 4."
- **In-the-moment adjustments.** "I'm exhausted" leads Claude to suggest a break or reordering, including an activity that is restful and good for you, such as a walk while it's dry.
- **Fixes when no plan fits.** Claude reads the conflicting rules and proposes relaxations: a later deadline, a shorter block, a skipped event.

### Claude Desktop mode

You chat in Claude Desktop as usual. Claude Desktop runs a small helper program on your laptop that reads and writes the scheduler's database, so the scheduler window doesn't need to be open. Claude asks permission before each change.

- **Helper tools:** `get_plan`, `get_tasks`, `create_task`, `propose_changes`, `apply_changes`, `solve`, `explain_block`.
- **Packaging:** PyInstaller builds the helper as a standalone program, since Claude Desktop can't count on Python being installed. The mcpb CLI packs it into one `.mcpb` file.
- **Install for users:** the extension ships inside the scheduler app. A "Connect to Claude" button in Preferences opens it, Claude Desktop shows its install window, and the user clicks Install.
- **While developing:** add the helper to Claude Desktop's config file (Settings › Developer › Edit Config) instead, then fully quit and reopen Claude Desktop.

### API-key mode

The user creates a key in the Claude Console and pastes it into Preferences. The app makes a tiny test call, stores the key in the keychain, and enforces a monthly spending cap the user sets.

- A Claude chat subscription doesn't include API access; Max, Ultra, and Team plans include monthly API credits, claimed by linking a Console organization. [source](https://support.claude.com/en/articles/9876003-i-have-a-paid-claude-subscription-pro-max-team-or-enterprise-plans-why-do-i-have-to-pay-separately-to-use-the-claude-api-and-console)
- A custom "Sign in with Claude" button is not allowed: Anthropic doesn't permit third-party apps to collect Claude.ai credentials or route requests through subscription plans. [source](https://support.claude.com/en/articles/13189465-log-in-to-your-claude-account)

## Phased build plan

Nine phases, each ending in a checkpoint you can run. The solver comes first because it's the heart of the app and needs no accounts or UI to test.

1. **Setup.** Install uv and Git, create the project, and add OR-Tools.
    - Learn: virtual environments, dependency locking, commits.
    - Checkpoint: `uv run python -c "import ortools"` succeeds.
1. **Solver with fake data.** Hard-code 3 tasks and 4 events, solve, and print the plan.
    - Learn: CP-SAT variables, optional intervals, `AddNoOverlap`, objectives, reading solver status.
    - Checkpoint: raise the midterm's priority and watch the lecture get skipped.
1. **Google sign-in and read calendar.**
    - Learn: OAuth, scopes, access vs. refresh tokens, keyring.
    - Checkpoint: print your next 7 days of events, then relaunch without signing in again.
1. **Storage.** Define the five tables in SQLModel and feed the solver from the database.
    - Learn: ORMs, sessions, schema changes, WAL mode.
    - Checkpoint: tasks survive a restart.
1. **Interface.** NiceGUI pages for Now, Tasks, Events, and Preferences, opened in a native window.
    - Learn: how a local server and a UI talk, event handlers, state.
    - Checkpoint: add a task in the window and see it in the plan.
1. **Write back and "what now?"** Write blocks to the Optimizer calendar, log progress, and re-plan with the stability penalty.
    - Learn: idempotent sync, batching API calls.
    - Checkpoint: log 30 minutes and see the plan adjust without a full reshuffle.
1. **Claude integration.** Add soft constraints and conflict reporting, then the Claude helper for Claude Desktop, then API-key mode.
    - Learn: assumptions and infeasibility cores, MCP tools, the Anthropic SDK's tool use.
    - Checkpoint: in Claude Desktop, say "I'm tired" and approve a re-planned afternoon.
1. **Background jobs and TwiceDaily.** Add APScheduler, then port TwiceDaily's features.
    - Learn: scheduled jobs, mapping Apps Script services to Python.
    - Checkpoint: the twice-daily brief appears on schedule.
1. **Packaging.** Bundle the app with PyInstaller, build the helper's `.mcpb` file, and publish both on GitHub Releases.
    - Learn: bundling, OS security prompts for unsigned apps, extension manifests.
    - Checkpoint: a fresh download opens from the dock or Start menu, and "Connect to Claude" installs the extension.

## Merging TwiceDaily

Each TwiceDaily feature gets listed by trigger, inputs, and outputs, then mapped to its Python equivalent. The script itself is still needed for this step.

| Apps Script service | Python equivalent |
|---|---|
| CalendarApp | google-api-python-client (Calendar API) |
| UrlFetchApp | httpx |
| Time-driven triggers | APScheduler |
| PropertiesService | SQLite or keyring |
| GmailApp (if used) | Gmail API via google-api-python-client |

**Trade-off:** Apps Script runs in Google's cloud even when your laptop is closed; the new app runs only while your laptop is on. Anything that must fire on schedule regardless, like a morning email, may be worth keeping as a slim Apps Script.

## Open decisions

- macOS or Windows? This affects packaging and keychain details.
- What does TwiceDaily do today, and which weather source does it use?
- Should the app write blocks to Google Calendar automatically, or only after you approve a plan? Claude's changes are always approved first.
- Is phone access needed? If so, a hosted version becomes worth revisiting.
- Which features must keep running while the laptop is off?
