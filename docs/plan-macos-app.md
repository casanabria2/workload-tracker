# Plan: native macOS client (Kanban board + time Gantt)

**Status:** proposal, not implemented
**Date:** 2026-08-05
**Goal:** a full native macOS app that replaces `tracker.py` for daily use — a
Kanban board of non-recurrent tasks (To Do / In Progress / Done), a separate
always-visible recurrent shelf, and a zoomable Gantt view of logged time — with a
left sidebar for navigation, faceted filters on both views, and no
reimplementation of the tracker's business logic.

**Decisions already taken:**

| Question | Decision |
|---|---|
| Data layer | Local Python daemon owning the data file; Swift is a pure view layer |
| Write scope | Full client — drag between columns, timer, task/log edits, GitHub close + reconcile |
| Gantt shape | Zoomable: per-log session bars at Day/Week, per-task spans at Sprint/Quarter |
| Filters | Four facets — Activity Type, Sprint, Repository, Role — shared across Board and Timeline (§8) |
| Sprint facet semantics | **"has logged time in that sprint"**, derived from log timestamps — *not* the `sprint`/`sprint_id` field (§8.2) |
| Deployment target | **macOS 26**, unconditional. Both Macs confirmed on 26.x, so Liquid Glass is adopted directly with no `#available` gating |
| Location | **`macos-client/` inside this repo**, not a sibling repo |
| Legacy `:7373` contract | **Served by the daemon**, so the existing menu-bar monitor keeps working with `tracker.py` closed (§5.4) |

---

## 1. What already exists (measured, not assumed)

**Python side** (this repo):

- `wt.py` — 7,076 lines. *All* the real logic: sprint bindings, `reconcile_task_sprints`,
  `close_task`, GitHub issue/project sync, calendar import, sprint cache.
- `tracker.py` — 4,855 lines. Textual TUI + the in-process HTTP bridge on **:7373**
  (`_BridgeHandler`, 9 `_bridge_*` methods). Bridge only exists while the TUI is open.
- `mcp_server.py` — 2,016 lines, 43 tools. **Returns human-readable strings, not
  structured data**, so it is not reusable as a JSON API — but it *is* the existing
  proof of how to wrap `wt.py` into task-oriented commands.
- venv Python is **3.14.6**. `gh` at `/opt/homebrew/bin/gh`.

**Live data shape** (`~/.workload_tracker.json`, 209 KB):

| Metric | Value |
|---|---|
| Tasks | 55 — `done` 37, `recurrent` 7, `inprogress` 6, `todo` 5 |
| Log entries | 416 — **387 carry `started_at`/`ended_at`**, 29 do not |
| Roles | 10 defined, **9 in use** |
| Sprints in `config.sprints_cache` | 72 — full sprint calendar available **offline** |
| Sprints with **logged time** | **11** (Sprint 95 → 105); current sprint is 105 |
| Tasks with **zero logs** | **5** — 3 `todo`, 2 `done` |

**Filter facet cardinality** — this is what makes the filter UI designable rather
than guessed:

| Facet | Distinct values in use | Notes |
|---|---|---|
| Role | 9 of 10 defined | `demokit` 22, `other` 16, `demos` 7, then a long tail of 1–3 |
| Activity Type | **9 in use, but 38 in `project_options_cache`** | maps to the task's `activity` field; 1 task has none |
| Repository | 6 | the task's `github_repo` field |
| Sprint | 11 of 72 cached | derived from logged time (§8.2); Sprint 105 has 13 tasks |

**Time spread across sprints** — measured with `task_sprints_with_time()`:

| Sprints a task has time in | 0 | 1 | 2 | 3 | 5 | 7 | 11 |
|---|---|---|---|---|---|---|---|
| Tasks | 5 | 37 | 5 | 3 | 2 | 2 | 1 |

So a sprint filter is genuinely subtractive rather than a relabelling: 37 tasks
appear under exactly one sprint, but 13 span two or more and one appears under
**eleven**.

Four numbers drive UI design directly: **37 of 55 tasks are done** (the Done column
must be sprint-scoped), **29 logs lack wall-clock timestamps** (the Gantt must
render those honestly rather than fabricating a time of day), **5 tasks have no logs
at all** (so a logged-time sprint filter would hide 3 live To Do cards unless
exempted — §8.2), and **only 13 tasks have time in the current sprint** (so the
default filtered board is ~13 cards rather than 55).

**Swift side** (`~/dev/carlos/workload-macos-monitor`, separate repo):

An existing SwiftUI menu-bar agent that already speaks the :7373 contract —
`Models.swift` (Codable `StatusResponse`/`ActiveTimer`/`TrackerTask`),
`TrackerClient.swift` (async `URLSession` actor), `AppSettings.swift`,
`AppState.swift`. Conventions to inherit verbatim: **SwiftPM executable, no
`.xcodeproj`, zero third-party dependencies, `@MainActor` UI + actor networking,
doc comments on types, CLAUDE.md updated every commit.**

**Existing footguns this plan must respect:**

- `wt.save()` is `DATA_FILE.write_text(json.dumps(...))` — **no lock, no atomic
  replace**. Two writers can tear the file.
- The TUI holds `self._data` in memory and saves wholesale, so it clobbers
  concurrent CLI writes (recorded in memory as `tui-clobbers-cli-writes`).
- `gh project` calls are GraphQL-budgeted (5000 pts/hr); exhaustion surfaces as the
  misleading `unknown owner type`.
- `gh issue create` / `close` are irreversible.

---

## 2. Architecture

```
┌────────────────────────────┐        ┌──────────────────────────────┐
│  WorkloadTracker.app       │        │  workload-macos-monitor      │
│  SwiftUI, sidebar + board  │        │  (existing menu-bar agent)   │
└─────────────┬──────────────┘        └──────────────┬───────────────┘
              │ HTTP + SSE, 127.0.0.1:7374          │ legacy contract
              │ Bearer token                        │ (same daemon, §5.4)
              ▼                                     ▼
      ┌───────────────────────────────────────────────────┐
      │  wt_daemon.py      single writer, flock + SSE     │
      └───────────────┬───────────────────────────────────┘
                      │
              ┌───────▼────────┐        ┌──────────────────┐
              │  wt_api.py     │───────▶│  wt.py           │
              │  dict-returning│        │  (unchanged)     │
              │  command layer │        └────────┬─────────┘
              └───────┬────────┘                 │
                      │                    ┌─────▼──────┐  ┌──────────┐
                      └───────────────────▶│ JSON file  │  │ gh CLI   │
                                           └────────────┘  └──────────┘
```

Three properties this buys:

1. **No logic duplication.** Swift never computes sprint attribution, reportable
   hours, or issue bindings. It renders what the daemon sends.
2. **One writer at a time.** Every mutation is `flock` → `wt.load()` → mutate →
   `wt.save()` → release. No long-lived in-memory cache to go stale (209 KB
   re-reads in under a millisecond), so the TUI-clobber class of bug never applies
   to the app's own writes.
3. **Push, not poll.** An SSE stream carries `changed` events (including data-file
   changes made by the CLI, the TUI, or iCloud sync from the other Mac) and
   `progress` events from long `gh` operations, which `reconcile_task_sprints`
   already supports via its `progress_callback`.

---

## 3. Phase 0 — harden the data-file writes — **DONE**

Implemented on branch `phase0-atomic-save`, commit `003f912` (not pushed).
Verified independently: 37/37 checks pass after, and 8 of 27 fail before.

**There were three write paths, not one** — all doing an unlocked, truncating
`DATA_FILE.write_text(json.dumps(data, indent=2))`:

- `wt.py:179` — `save()`
- `mcp_server.py:99` — a **duplicate** `save()`
- `tracker.py:185` — `save_data()`, plus a migration write-back at `tracker.py:163`

Fixing only `wt.py` would have left the other two bypassing the lock entirely. The
latter two now delegate to `wt.save()`, names and signatures unchanged; a grep for
`DATA_FILE.write_text` across the repo comes back empty.

**Atomic save.** Temp file in the same directory as the **resolved** target,
`flush()` + `os.fsync()`, mode preserved, then `os.replace()`.

> ⚠️ **Resolving the symlink is not optional, and this plan originally got it
> wrong.** The earlier text here said to `os.replace()` onto
> `DATA_FILE.with_suffix(".json.tmp")`'s target directly. But `~/.workload_tracker.json`
> is a symlink chain into iCloud Drive, and **`os.replace` does not follow
> symlinks** — verified: replacing the link path turns the symlink into a regular
> file and leaves the real iCloud file holding stale content. That would have
> silently detached the data from sync on both Macs on the very first save. The
> harness now has a regression check that the file is still a symlink after `save()`.

**Advisory lock.** `data_lock()` takes `fcntl.flock(LOCK_EX)` on a sidecar
`~/.workload_tracker.lock` — never the data file (whose inode is replaced on every
save) and never inside `~/Library/Mobile Documents` (unreliable there). Honours
`WT_DATA_FILE` via a `<copy>.lock` sibling, derived at call time because the
harnesses rebind `wt.DATA_FILE` rather than only setting the env var.

**Contract Phase 2 depends on: `data_lock()` is re-entrant within a process, and
`save()` is always the right call.** There is no `_save_locked()`. A daemon
transaction is simply:

```python
with wt.data_lock():
    data = wt.load()
    ...mutate...
    wt.save(data)        # re-enters, depth-counted, does not deadlock
```

Mechanics: a module-level `threading.RLock` plus a depth counter, with the `flock`
taken on the outermost entry only. The `RLock` is load-bearing — `flock` is
per-open-file-description, so without it two threads in one process would each hold
their own fd and the first to finish would unlock for both.

**Timeout policy** (the plan hadn't specified one): polled `LOCK_NB` against a 5 s
deadline rather than a blocking `LOCK_EX`, because `flock` has no timeout and
`SIGALRM` is unusable from the TUI's bridge and worker threads. On expiry,
`required=True` (the default, for daemon transactions) raises `DataLockTimeout`;
`required=False` (what `save()` uses) logs a warning and proceeds **unlocked**. That
degrades to today's lost-update risk but never to a torn file — the write is atomic
either way — which beats hanging the 1 Hz tick loop or throwing out of `save()` into
callers that have no `try/except` and would drop a time entry.

**Verification.** `tools/test_atomic_save.py`: 8 forked writers each appending a
distinct log to a distinct task, plus two spinning readers, against a
`WT_DATA_FILE` copy with the usual `subprocess` guard. Asserts valid JSON, every
mutation survives, no reader sees a torn or empty document, the symlink is
preserved, and a nested `save()` does not release the outer `flock` early (checked
from a forked child — an in-process check would see the parent's own fd and prove
nothing). `check_invariants.py` holds on the post-concurrency file.

Measured on the pre-change code: **7 of 8 mutations lost, 10 of 134 reads
unparseable.** Both failure modes were real, not theoretical.

### 3.1 Two things Phase 0 deliberately left open

- **`wt.load()` itself does a read-modify-write** (the migration write-back), so its
  load→mutate→save window is still racy. Rare and idempotent, and Phase 2's
  `with data_lock(): load(); …; save()` closes it properly. Not worth expanding
  Phase 0's blast radius.
- **`wt.load()` swallows a parse failure as `{}`** (`except Exception: data = {}`).
  A torn file historically presented as *an empty dataset* — the same symptom as the
  documented TCC/Full-Disk-Access failure on the second Mac. Atomic replace removes
  the torn file as a *cause*, but the masking remains and is a live hazard for the
  daemon: an unreadable file would look like "no tasks" and any subsequent save would
  clobber the real data. **Phase 2 must not build on `load()`'s silence** — see
  risk #9.

---

## 4. Phase 1 — `wt_api.py`, a dict-returning command layer

The gap between `wt.py` (primitives) and a JSON API is command-level assembly and
validation — which `mcp_server.py` already does, but bakes into English strings.
Rather than write that logic a third time, extract it once:

```python
# wt_api.py — no printing, no sys.exit, no argparse. Returns/raises only.
def snapshot(data) -> dict                      # everything the UI renders
def create_task(data, *, title, role, ...) -> dict
def update_task(data, task_id, **fields) -> dict
def set_status(data, task_id, status, *, ...) -> dict
def start_timer(data, task_id) -> dict          # incl. _browser_switch side effects
def stop_timer(data) -> dict
def add_log(data, task_id, minutes, note, started_at=None, ended_at=None) -> dict
def edit_log / delete_log / split_log / merge_logs(...) -> dict
def plan_close(data, task_id) -> dict           # reconcile dry-run preview
def close(data, task_id, *, create_issue, on_progress) -> dict
def reconcile(data, task_id, *, create_issues, dry_run, on_progress) -> dict
class WtError(Exception): code: str; message: str
```

Rules: pure functions over a passed-in `data`; no `load()`/`save()` inside (the
caller owns the transaction); validation errors raise `WtError` with a stable
machine code (`invalid_role`, `unknown_activity`, `no_repo`, …) so Swift can
localize and act on them.

`snapshot()` is the important one, and it must carry every filter facet. Per task:

- identity, `status`, `role_id`, `title`, `description`
- **`activity`, `github_repo`** — the two per-task fields the filter bar needs (§8).
  `type` is carried too, for the editor, but is not a facet.
- **`sprints_with_time`** — `[{sprint_id, sprint_title, total_mins}]`, straight from
  `task_sprints_with_time(task, sprints)`, which already buckets logs by timestamp
  and already drops zero-minute sprints. This is what the Sprint facet filters on
  (§8.2), so sprint attribution stays in Python and Swift never re-derives it from
  log timestamps and sprint date ranges. Strip the `logs` key from each entry — the
  task's full `logs` array is already sent once.
- `logged_mins`, `reportable_mins` — via
  `task_reportable_mins(task, sprints, sprint_id)`. Note the real signature is
  `(task, sprints, sprint_id=None)`, not the `(task, data, sprints)` currently
  documented in CLAUDE.md.
- `start_sprint` / `start_sprint_id`
- `sprint_issues` bindings with per-binding hours and issue ref
- `current_issue` via `task_current_issue(task, data)` — never the raw
  `github_issue` key
- `last_logged_at`, and the full `logs` array

Top level: roles (with colors), `config.sprints_cache`, the current sprint,
`active_timer` with raw epoch `started_at` so Swift ticks elapsed locally, and
**`config.project_options_cache`** — needed by the task editor's Activity/Type
pickers, though *not* by the filter bar (§8.3).

**Then refactor `mcp_server.py` to format `wt_api` results** instead of
reimplementing them. This is the phase's real payoff: one command layer, three
front ends (MCP, daemon, and — later, optionally — the CLI).

**Verification:** `tools/test_wt_api.py` against a `WT_DATA_FILE` copy with the
existing `subprocess`-guard so no `gh` call escapes.

**Harness status (Phase 0.5, `8808f6a` — done).** All four regression harnesses were
broken and are now repaired and passing: `test_reconcile` 126/126, `test_phase3`
132/132, `test_mcp_phase3` 172/172, `test_tracker_phase3` 66/66, with
`test_atomic_save` 37/37 as the control. Every one of the 28+ failures was a **stale
expectation, not a regression** — they were characterisation tests pinned to one
afternoon's copy of the live data (2026-07-31, `minutes=26620.71, logs=392`, Sprint
104 current), and the owner has since logged 26 entries and crossed a sprint
boundary. Expectations are now derived from the fixture at runtime, and
`tools/make_fixtures.py` reconstructs the lost pre-migration snapshot by running both
migrations backwards, verifying the round trip before writing. `tools/README.md`
documents each invocation.

> ⚠️ **But the safety net is thinner than the green numbers suggest, and Phase 1 is
> the phase that finds out:**
>
> - **`test_mcp_phase3.py` exercises ~20 of the 43 registered MCP tools** — the
>   sprint/issue/task surface only. Arc, iTerm, tabs and calendar tools are
>   **untested**. Phase 1 refactors all 43 onto `wt_api.py`, so more than half the
>   blast radius has no regression coverage at all.
> - **`test_phase3.py` §12 diffs the working tree's `wt.py` against
>   `git show HEAD:wt.py`.** Phase 1 touches `mcp_server.py`, so that section becomes
>   a no-op that still prints "12/12 identical" — a green check that proves nothing
>   about the change being made.
> - No MCP stdio protocol layer, no HTTP bridge socket, no real GitHub semantics
>   (all stubbed), and `config.sprints_cache` is treated as ground truth.
>
> **Mitigation for Phase 1:** extend `test_mcp_phase3.py` to cover the untested tool
> families *before* refactoring them, or refactor only the ~20 covered tools in the
> first pass and leave the rest on their current code path. Do not treat "172/172
> passed" as licence to move all 43.

---

## 5. Phase 2 — `wt_daemon.py`

Stdlib only (`http.server.ThreadingHTTPServer`), matching the existing bridge's
approach so there's no new dependency.

### 5.1 Transport and auth

Binds `127.0.0.1:7374` (7373 stays the TUI's). Loopback alone is not auth — any
local process can reach it — so every request requires
`Authorization: Bearer <token>`, where the token is generated on first run into
`~/.workload_tracker_daemon_token` at mode `0600`. The app reads the same file.
CORS is dropped (the existing bridge's `Access-Control-Allow-Origin: *` is
inappropriate for a write-capable API).

### 5.2 Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/v1/snapshot` | whole UI state, one round trip |
| GET | `/v1/events` | SSE: `changed`, `progress`, `error`, `heartbeat` |
| POST | `/v1/tasks` | create |
| PATCH | `/v1/tasks/{id}` | title/description/role/repo/activity/type/sprint/local_folder |
| POST | `/v1/tasks/{id}/status` | `{status}` — the Kanban drop target |
| DELETE | `/v1/tasks/{id}` | |
| POST | `/v1/timer/start` · `/v1/timer/stop` | |
| POST | `/v1/tasks/{id}/logs` + PATCH/DELETE `/logs/{log_id}` | plus `/split`, `/merge` |
| POST | `/v1/tasks/{id}/close/plan` | reconcile dry-run → preview payload |
| POST | `/v1/tasks/{id}/close` | the real close workflow, streams progress |
| POST | `/v1/tasks/{id}/reconcile` | `{create_issues, dry_run}` |
| POST | `/v1/tasks/{id}/github/link` · `unlink` · `open` | |
| POST | `/v1/tasks/{id}/tabs/save` · `open` · `clear` | reuse `browser_window.py` |
| POST | `/v1/tasks/{id}/iterm` | reuse `iterm_manager.py` |
| GET | `/v1/health` | version, data file path, mtime, TUI-detected flag |

Filtering is **not** an endpoint. 55 tasks and 416 logs fit in one snapshot, so
every facet is applied client-side in Swift — instant, no round trip per keystroke,
and no filter logic duplicated across two languages.

### 5.3 Concurrency model

- Mutating handlers run under `data_lock()`: acquire → `wt.load()` → `wt_api.*` →
  `wt.save()` → release → emit SSE `changed`. `data_lock()` is **re-entrant**
  (§3), so the inner `save()` needs no special form. Use the default
  `required=True` so a lock timeout raises `DataLockTimeout` and the request fails
  loudly with a 503 — a daemon must never silently write unlocked the way `save()`
  is allowed to.
- **Never trust a successful `load()` to mean "there is data."** `wt.load()` returns
  `{}`-defaults on a parse failure or an unreadable file (§3.1). Before the first
  write of a session the daemon must assert the file exists, is non-empty, and
  parsed to a non-empty `tasks` list — otherwise refuse to write and surface the
  Full-Disk-Access / unreadable state (risk #9).
- Long operations (`close`, `reconcile`, `github/*`) run on the handler thread —
  `ThreadingHTTPServer` already isolates them — and return `202` with an
  `operation_id`; progress arrives on the SSE stream via `on_progress`. The lock is
  held for the whole operation, which is correct: a close must not interleave with
  another write.
- A watcher thread polls the data file's mtime at 1 Hz and emits `changed` when it
  moves without the daemon having written it. That covers CLI writes, TUI writes,
  and iCloud landing a copy from the other Mac.
- `/v1/health` probes `127.0.0.1:7373`; when the TUI answers, the flag is set and
  the app shows a persistent warning (see §12.1).

### 5.4 Legacy compatibility (decided: yes)

The daemon also serves the exact :7373 contract — `GET /status`, `GET /tasks`,
`POST /timer/start`, `POST /timer/stop` — with byte-compatible payloads, so
`workload-macos-monitor` can be pointed at it and **the menu-bar agent keeps working
with `tracker.py` closed**, which it currently cannot. Its base URL is already
user-configurable, so this needs no monitor code change at all.

Implementation notes:

- Serve them **unauthenticated on a second port** (`--legacy-port 7375`, off by
  default in the tracker repo's own tests). The monitor sends no `Authorization`
  header, and adding one would require changing the monitor — which this is
  explicitly designed to avoid. Loopback-only binding is the security boundary,
  the same one the current bridge relies on.
- Payload parity is a **test**, not a hope: `tools/test_legacy_contract.py` asserts
  the daemon's `/status` and `/tasks` responses have the same key sets and types as
  `tracker.py`'s `_bridge_status()` / `_bridge_list_tasks()`, including the optional
  `active_window_id` and `last_logged_at` the monitor decodes.
- Two behaviours must be reproduced from the bridge, not just the shapes:
  `active_window_id` on the active timer (the monitor draws its Safari border from
  it) and `last_logged_at` per task via `task_last_logged_at()` — **which lives in
  `tracker.py`, not `wt.py`**, so Phase 1 moves it into `wt_api.py` for both callers.
- A legacy **start** must call `_browser_on_task_started` semantics (open the task's
  Safari window) but **not** focus the Arc space, matching the documented bridge
  behaviour exactly. A legacy **stop** goes through the `_commit_active_timer`
  equivalent so it logs an identical `"Timer session"` entry and syncs GitHub hours.
- Port conflict is expected and fine: when `tracker.py` is open it owns 7373 and the
  monitor can talk to either. The daemon never binds 7373.

Once this lands, the monitor's own CLAUDE.md should note the alternative base URL.

### 5.5 Lifecycle

The app launches the daemon as a child `Process`
(`venv/bin/python wt_daemon.py --port 7374`), passing the tracker repo path from
Settings, and terminates it on quit. If `/v1/health` already answers, the app
attaches to the running daemon instead of spawning a second one — so a launchd
`SMAppService` agent for always-on operation is a drop-in later, not a rewrite.

**Verification:** `tools/test_daemon.py` boots the daemon against
`WT_DATA_FILE=/tmp/wt-work.json` with the `subprocess` guard installed, exercises
every endpoint, and runs `tools/check_invariants.py` after each mutation.

---

## 6. Phase 3 — Swift app skeleton

Lives in **`macos-client/` in this repo**. SwiftPM executable, no `.xcodeproj`,
matching the monitor's convention. `.gitignore` gains `macos-client/.build/`.

```
workload-tracker/
├── wt.py  tracker.py  mcp_server.py  wt_api.py  wt_daemon.py
└── macos-client/
    ├── Package.swift                      # SwiftPM, macOS 26, exe + test targets
    ├── Sources/WorkloadClient/
    │   ├── App.swift                      # SwiftUI App scene, Commands, Settings
    │   ├── Models/                        # Codable snapshot mirror + derived types
    │   ├── Client/
    │   │   ├── DaemonClient.swift         # actor, async URLSession
    │   │   ├── EventStream.swift          # SSE via URLSession.bytes(for:)
    │   │   └── DaemonProcess.swift        # spawn/attach/terminate
    │   ├── Store.swift                    # @MainActor @Observable source of truth
    │   ├── Filtering/
    │   │   ├── FilterState.swift          # the 5 facets + text query (§8)
    │   │   ├── FacetCatalog.swift         # in-use values derived from snapshot
    │   │   └── TaskFilter.swift           # pure predicate, unit-tested
    │   ├── Views/Sidebar/ Board/ Timeline/ Overview/ Inspector/ Filter/
    │   └── Design/RolePalette.swift Spacing.swift
    └── Tests/WorkloadClientTests/         # decoding, filter predicate, Gantt geometry
```

`Store` is `@Observable @MainActor`: holds the last snapshot, applies SSE
`changed` by refetching `/v1/snapshot`, and runs a 1 Hz tick so live-timer labels
re-render. Mutations are optimistic with rollback on error — except anything
touching GitHub, which is never optimistic (§12.5).

---

## 7. Phase 4 — the Kanban board

**Layout.** `NavigationSplitView` sidebar + detail. The detail is a `VSplitView`:
Kanban on top, recurrent shelf below (§9) — a real draggable native divider, and
the same top/bottom split the TUI already uses, so muscle memory carries over.

**Columns.** Three `LazyVStack`s in `ScrollView`s: To Do / In Progress / Done.
`recurrent` tasks are excluded entirely. Each column header shows the count and
summed hours **of what the filters currently admit**, with the unfiltered total in
parentheses so nothing feels silently hidden.

**Done column scoping.** With an explicit Sprint facet in place (§8), the Done
column needs no separate scope control — it simply obeys the Sprint filter, which
**defaults to the current sprint** and matches on *logged time in that sprint*
(§8.2). That removes a redundant picker: one concept, one control. It also means the
Done column answers a more useful question than the old field-based scope would —
"what did I finish work on this sprint" rather than "what carries this sprint's
label".

**Card.** Title (2-line truncation), role chip, hours (`reportable_mins` for the
card's sprint context), sprint badge, current-issue badge (`owner/repo#n`, click
opens in browser), activity chip when set, and small SF Symbol affordances for
linked-issue / saved-tabs / iTerm folder. A running task gets an accent border and
a live `m:ss` label.

**Drag and drop.** A `Transferable` `TaskDragPayload` (custom `UTType`
`com.carlossanabria.workloadtracker.task`, carrying the task id) with
`.draggable` on cards and `.dropDestination` per column, plus a drop insertion
indicator and spring-loaded column auto-scroll.

Drop semantics are deliberately not symmetric, because the underlying operations
are not:

| Drop | Behaviour |
|---|---|
| → In Progress | `POST /status {inprogress}`. Optimistic. Mirrors TUI `p`. |
| → To Do (from In Progress) | `POST /status {todo}`. Optimistic. |
| → **Done** | **Never silent.** Opens a confirmation sheet built from `close/plan` (§7.1). |
| → To Do/In Progress **from Done** | **Rejected**, with a "reopening isn't supported" hint. `wt.py` has no reopen path (no `gh issue reopen`), and faking it locally would desync the GitHub Project. Follow-up, not v1. |
| Recurrent card → any column | **Rejected.** Closing a recurrent task ends the series and closes its live issue; CLAUDE.md warns explicitly. Only the shelf's explicit menu action can do it, behind its own confirmation. |

### 7.1 The close sheet

Dropping on Done calls `POST /close/plan` (a `reconcile_task_sprints(dry_run=True)`
— write-free by construction) and renders the plan before anything happens:

```
Close “IRON Infusion”

Sprint 104   6h 15m   →  grafana/field-eng#412   update hours, close issue
Sprint 105   3h 30m   →  (no issue)              CREATE issue, set hours, close
Sprint 106   1h 45m   →  grafana/field-eng#488   update hours  (stays open)

⚠ 1 GitHub issue will be created. This cannot be undone.
                                        [Cancel]  [Close Task]
```

Confirming issues `POST /close`, and the sheet becomes a progress list fed by SSE
`progress` events. On failure the task stays open — matching `close_task`'s
existing contract that a failed reconcile aborts the close.

**Keyboard parity.** Arrow keys move between cards and columns; `⌘←`/`⌘→` move the
selected card between columns; every TUI binding gets a menu command (§11).

---

## 8. Phase 5 — filtering (shared by Board and Timeline)

### 8.1 One filter state, two views

A single `FilterState` lives in `Store` and is **shared across Board and
Timeline**, so switching views keeps context — filter down to
`activity == "Demo Kit Maintenance"` on the board, hit `⌘2`, and the Gantt shows
exactly that work. It persists in `@SceneStorage` across launches.

```swift
struct FilterState: Codable, Equatable {
    var roles:         Set<String> = []     // role_id
    var activityTypes: Set<String> = []     // task.activity
    var repos:         Set<String> = []     // task.github_repo
    var sprints:       Set<String> = []     // sprint_id, matched by LOGGED TIME (§8.2)
    var text:          String      = ""     // title / description / issue ref
}
```

Four facets, matching the four asked for. **Activity Type is one facet** and maps to
the task's `activity` field; `type` is unused across all 55 tasks (and its options
cache is empty), so it is not surfaced as a filter at all.

**Combination rule: OR within a facet, AND across facets** — standard faceted
search. `roles: {demokit, demos}` + `sprints: {105}` means *(demokit OR demos) AND
worked-in-Sprint-105*. An empty facet means "no constraint", not "match nothing".
This is stated explicitly because the alternative (AND everywhere) makes
multi-select useless, and it is the single most likely thing to get wrong.

**Repository** matches the task's `github_repo` field, plainly. A task's issues
*can* technically live in different repos across sprints, but that is a rare
exception and the filter is not designed around it — the accepted consequence is
that such a task matches only its `github_repo`, not the repo of some older
sprint's issue.

`TaskFilter.swift` is a **pure function** `(FilterState, [Task]) -> [Task]`, unit
tested against the real facet distribution — including the task with no Activity,
the task with time in 11 sprints, and the 5 tasks with no logs.

### 8.2 Sprint means "worked in that sprint", not the `sprint` field

This is the one facet whose semantics are not a field lookup, and it matches how the
data model actually thinks: per CLAUDE.md, *"A task is not assigned to a sprint… which
sprint any minute of work belongs to is derived from the log's timestamp."* The
`sprint`/`sprint_id` keys are a legacy mirror of the current binding and explicitly
not to gain new readers.

So the Sprint facet matches **a task that has logged time in the selected sprint**,
using the `sprints_with_time` array the snapshot already carries (§4), which comes
straight from `task_sprints_with_time()` — timestamp-bucketed, zero-minute sprints
already dropped. Swift never re-derives sprint attribution.

Consequences, all measured:

- A task can appear under **many** sprints. 13 tasks span 2+; one spans 11. Selecting
  Sprint 100 shows the 8 tasks worked in it, whatever their current binding says.
- The default selection is **the current sprint** (Sprint 105 → 13 tasks), which is
  what makes the Done column tractable without a separate scope control (§7).
- **The open-work exemption.** ⚠️ *This rule was too narrow as first written, and
  the implementation proved it.* The original text exempted only tasks with **no
  logs at all** (5 tasks, 3 of them live `todo` cards). Built that way, the default
  current-sprint filter hid **5 of the 6 In Progress cards** — open work not yet
  logged against this fortnight. The rationale for the exemption ("a card you just
  created would be invisible") applies at least as strongly to work in flight.

  The rule is therefore: **a task with no time in the selected sprints still matches
  whenever the current sprint is among them and the task is not `done`** — plus a
  `done` task with *no logged time at all*, which has no sprint anywhere and so can
  only sensibly live in the current one. Zero-log tasks are now just the special
  case with no logs.
- **This must not un-scope the Done column**, which is the reason the facet exists:
  a `done` task with time in *other* sprints belongs to those and stays hidden.
  Measured: 31 of the 37 done tasks remain hidden under the default filter, while
  every open task is visible. Default view = 24 of 55: todo 5 + inProgress 6 +
  recurrent 7 + done 6.

### 8.3 Facets are derived from data, and self-hide

`FacetCatalog` computes each facet's options from the snapshot, not from a static
list. A facet offering fewer than 2 distinct values is hidden entirely, so the bar
never shows a control that cannot change the result. Grounded in the measured data:

- **Activity Type** offers the **9 values in use**, not the 38 in
  `project_options_cache`. Offering all 38 would mean 29 options that match nothing.
  (The *editor's* Activity picker still uses the full cache — different control,
  different job.)
- **Sprint** offers the **11 sprints with logged time**, newest-first — not all 72
  cached sprints.
- **Repository** offers the 6 in use. **Role** offers the 9 in use, each with its
  logged-time total.

### 8.4 Native filter UI

Two coordinated surfaces over one state, which is the Finder/Mail idiom rather
than a bespoke filter panel:

1. **`.searchable` with tokens.** The toolbar search field takes free text and
   holds each active facet value as a removable **search token** chip
   (`Activity Type: Demo Kit Maintenance` ×). Tokens are the native macOS mechanism for
   exactly this, they make active filters impossible to miss, and each is
   dismissible with one click or Delete.
2. **A "Filter" toolbar `Menu`** (funnel SF Symbol, badged with the active count)
   containing one submenu per visible facet, each a list of multi-select `Toggle`
   rows with per-value counts. Plus `Clear All Filters` (`⇧⌘K`).

**Sidebar roles stay.** The sidebar's Roles section (§11) writes the *same*
`FilterState.roles` — it is a second view of one state, not a second state. Toggling
a role in the sidebar makes its token appear in the search field, and removing that
token unchecks the sidebar row. Role gets this privileged position because it is
the facet with persistent per-role time totals worth always seeing.

**Empty state.** When filters admit nothing, each view shows a real empty state
naming the offending facets with a one-click "Clear filters" — never a blank pane.

### 8.5 Timeline-specific filter behaviour

The Sprint facet additionally **sets the Gantt's visible x-range**: selecting
Sprint 105 scrolls and zooms the axis to that sprint's cached dates. Selecting
several sprints spans them. This is the one place a filter also drives viewport,
and it is what makes "filter by Sprint" useful on a time axis rather than merely
subtractive.

---

## 9. Phase 6 — the recurrent shelf

7 recurrent tasks, visible on launch, separate from the board — the bottom pane of
the `VSplitView`, collapsible via toolbar toggle and `⌥⌘R`, with its height
persisted in `@SceneStorage`.

A native `Table` (sortable columns, not cards, because these are a stable list you
scan rather than move): Title · Role · This sprint · Total · Current issue ·
Series. Row actions via context menu and the Task menu: Start timer, Log time,
Open issue, Sync sprints, End series (confirmed, and the only route to closing a
recurrent task). Series grouping uses `recurrent_series_for_title()` /
`RECURRENT_SERIES_ALIASES`, never fuzzy title matching.

**Filters apply here too**, with one exception: the **Sprint facet is ignored** for
the shelf. A perpetual task accumulates time in every sprint it runs through, so a
logged-time sprint filter would either match all of them (making the facet pointless
here) or hide the ones with a quiet sprint — and these are precisely the tasks meant
to stay visible on launch. The shelf instead shows a "This sprint" hours column that
*does* respect the selected sprint, so the facet still changes what you read, just
not which rows exist.

---

## 10. Phase 7 — the Gantt view

**Swift Charts**, not hand-rolled drawing: `BarMark(xStart:xEnd:y:)` is exactly a
Gantt bar and brings axes, hit testing, and accessibility descriptors for free.
Zero third-party dependencies, consistent with the monitor's convention.

**Zoom** — segmented control in the toolbar plus `⌘+`/`⌘-`:

- **Day / Week** — one bar per log entry at its real `started_at`→`ended_at`.
- **Sprint / Quarter** — one bar per task, `min(log date)` → `max(log date)`, with
  `RuleMark` sprint boundaries drawn from the 72 offline cached sprints.

**The 29 timestamp-less logs.** They have `at` (when logged) but no wall clock for
the work. They render at the `log_effective_date`, width = `minutes`, with a
hatched fill and a distinct legend entry — "approximate time of day". Inventing a
plausible position would be a lie the chart tells silently; this is the one place
the plan chooses visible imprecision over invisible fabrication.

**Detail.** Rows sectioned by role, colored by role (§11 palette). `RuleMark` for
now. The active timer renders as a growing bar. `chartXSelection` + `.chartOverlay`
gives a hover tooltip (task, note, duration, sprint); clicking a bar selects the
task and syncs the Inspector and Board selection. A summary strip above the chart
totals the **filtered** hours by role for the visible range.

416 logs is far below any performance threshold; no windowing needed.

---

## 11. Phase 8 — native-feel checklist

Concrete, reviewable items rather than "follow the HIG".

**Structure**
- `NavigationSplitView`; sidebar `List(selection:)` with a **Views** section
  (Board `⌘1`, Timeline `⌘2`, Overview `⌘3`) and a **Roles** section of
  checkbox toggles showing each role's logged time — the Calendar.app sidebar
  pattern, so filtering in the sidebar stays idiomatic rather than conflated with
  navigation. Sidebar footer mirrors the TUI's ACTIVE TIMER block with a
  start/stop control.
- Trailing `.inspector` for task detail (logs table, sprint bindings, notes) —
  a panel, not a modal, because it's inspection of a selection.
- Sheets reserved for confirmation and destructive flows. `.alert` for errors.
- `Settings` scene (`⌘,`): General (tracker repo path, daemon port, auto-start),
  Appearance, Advanced (token path, reset).
- `.defaultSize`, `.windowResizability(.contentMinSize)`, `@SceneStorage` for
  selection, zoom level, shelf height, and `FilterState`.

**Menus** — a real menu bar, since every TUI binding needs a discoverable home:
File (New Task `⌘N`, New from Issue `⇧⌘N`), Edit (Undo `⌘Z` through
`UndoManager` for local-only mutations, Find `⌘F` → focus the filter field), View
(view switch, `⌥⌘R` shelf, zoom, `⇧⌘K` clear filters), Task (Start/Stop Timer
`⌘T`, Log Time `⌘L`, Manage Logs `⇧⌘L`, Mark Done `⇧⌘D`, Sync Sprints `⇧⌘S`, Open
Issue `⌘G`, Open iTerm `⌘I`), Window, Help. Menu items act on the selected card via
`FocusedValueKey`/`.focusedValue`. Every menu action is also a card context menu.

**Liquid Glass (macOS 26)** — unconditional, no `#available` gating.
- `.glassEffect()` on floating chrome — toolbar accessories, the filter menu, the
  zoom control, the timer pill. **Not** on cards or chart content: Apple's guidance
  puts glass on controls and navigation layers, and glass behind text-dense cards
  costs legibility for no gain.
- `.backgroundExtensionEffect()` where the board scrolls under the toolbar.
- App icon authored in **Icon Composer** (required for a correct layered macOS 26
  icon).

**Color and type**
- Role colors map to the system palette (`.systemBlue/.systemGreen/.systemYellow/
  .systemRed/.systemTeal/.systemPurple/.systemGray`) rather than raw hex, so Dark
  Mode and Increase Contrast are handled by the OS. Note 3 of the 10 roles are
  `white` in the data file — `RolePalette` assigns stable distinct system colors by
  role index for those, rather than rendering three identical chips. Every
  color-coded element also carries a text label or shape — color is never the only
  channel.
- `Color.accentColor` follows the system accent. No hardcoded backgrounds.
- Semantic text styles only (`.headline`, `.body`, `.caption`); Dynamic Type
  scales. 8/12/16/20 pt spacing grid.
- SF Symbols only, `.symbolRenderingMode(.hierarchical)`.

**Accessibility**
- `.accessibilityLabel`/`Value`/`Hint` on cards; card is a single accessible
  element with a custom action set matching its context menu.
- `.accessibilityChartDescriptor` on the Gantt so VoiceOver can read the timeline.
- Active filters announced when they change, so a VoiceOver user isn't looking at a
  silently-reduced board.
- Full keyboard navigation without the mouse; visible focus rings; `.focusable()`.
- Honour `accessibilityReduceMotion` (cross-fade instead of card spring),
  `colorSchemeContrast`, and `accessibilityDifferentiateWithoutColor`.
- String catalog from day one even though only `en` ships.

**Sandbox and signing.** App Sandbox **off**: the app spawns a Python child,
reads an iCloud-backed path, and shells out to `gh`. Hardened runtime on, signed
with a Developer ID (or ad-hoc for local use). Not App Store bound, so this costs
nothing. `applicationShouldTerminate` stops the daemon and waits for any in-flight
`gh` operation.

---

## 12. Phase 9 — packaging

`macos-client/make-app.sh`: `swift build -c release`, assemble
`WorkloadTracker.app/Contents/{MacOS,Resources}`, write `Info.plist`
(`LSMinimumSystemVersion 26.0`, no `LSUIElement` — this one has a Dock icon),
`codesign --options runtime`. Optional `SMAppService` login-item toggle for the
daemon so the board and the menu-bar monitor both work without any terminal
window open.

**Second-Mac note:** because the data file resolves into
`~/Library/Mobile Documents`, the `.app` itself will need **Full Disk Access** on
the second Mac (the daemon inherits the launching app's TCC context, so granting
Terminal is not enough). Symptom is the documented one: `ls` returns *Operation
not permitted* and the app loads an empty dataset. The app must detect an empty
dataset plus an existing-but-unreadable file and show a specific
"grant Full Disk Access" state — **never** write in that state, or it clobbers the
synced copy.

---

## 13. Risks and mitigations

| # | Risk | Mitigation |
|---|---|---|
| 1 | TUI clobbers the app's writes (known, recorded) | `/v1/health` detects :7373; app shows a persistent, dismissible banner: "tracker.py is running — its next save may overwrite changes made here." Long term: teach the TUI to use the daemon (§14). |
| 2 | Torn data file from concurrent writers | Phase 0 atomic replace + `flock`. |
| 3 | TCC/iCloud on the second Mac | §12 detection state; never write from an empty-load. |
| 4 | GraphQL budget exhaustion (`unknown owner type`) | Daemon never auto-reconciles or auto-fetches project info on load or on snapshot; `get_project_info()` memoisation preserved; `project_info` threaded into `update_project_*`. |
| 5 | Accidental irreversible GitHub writes via drag | Done drops always go through the §7.1 preview sheet; no optimistic UI on GitHub paths; recurrent cards can't be dropped at all. |
| 6 | Hidden-by-filter confusion — a task "missing" because a stale filter excludes it | Active facets always visible as tokens; column headers show filtered *and* total counts; real empty states with one-click clear; `⇧⌘K`. |
| 7 | `wt_api` extraction regresses MCP behaviour | `tools/test_mcp_phase3.py` + `test_reconcile.py` + `test_phase3.py` run before and after, against copies. |
| 8 | Scope: this is a large build | Phases 0–2 are independently useful (they fix `save()` and unlock the monitor without the TUI); Phase 3–4 alone is already a usable board. |
| 9 | **`wt.load()` masks an unreadable file as an empty dataset** — the daemon could see "no tasks" and then clobber real data | §5.3 pre-write assertion: file exists, non-empty, `tasks` non-empty. Same detection feeds the Full-Disk-Access state in §12. This is the failure mode most likely to *destroy* data rather than merely annoy. |
| 10 | Regression harnesses are broken (stale `REPO` paths, stale fixtures) | Phase 0.5 fixes them before Phase 1 refactors 43 MCP tools — see §4. |

**Testing discipline throughout** (from CLAUDE.md, non-negotiable): never point a
harness at the live file — `cp ~/.workload_tracker.json /tmp/wt-work.json` and set
`WT_DATA_FILE`. Keep the `subprocess` guard installed so no test can reach
`gh issue create`/`close`. Quit `tracker.py` before any write-path work.

---

## 13.5 Follow-ups — after the plan is finished

Deferred deliberately. None of these block a phase; all of them are things a
future session would otherwise rediscover.

### 1. Remove the Safari task-window integration entirely

**Owner's decision (2026-08-07): "an old idea that never worked — I would rather
remove all Safari integration at some point."** So this is a removal, not a
deprecation-in-place like Arc. It **subsumes** the `active_window_id` follow-up
that used to be item 1 here: that field exists only to let the monitor draw a
border round the Safari window, and the border is already gone
(`workload-macos-monitor` `f803fb1`).

Barely used in the live data: **1 task has `tabs`, 0 have `active_window_id`.**

The surface, roughly in dependency order:

| Where | What |
|---|---|
| `browser_window.py` | the whole module, 215 lines (`SafariWindowManager`) |
| `wt.py` | `_browser_switch`, its calls in `cmd_start` / `cmd_stop`, `cmd_tabs` (`wt tabs save\|open\|list\|clear\|close`), and the `_wt` completion entries |
| `wt_api.py` | the `browser=` parameter on `start_timer` / `stop_timer` (already defaulting to `False`, `4457ba9`) |
| `wt_daemon.py` | the four `/v1/tasks/{id}/tabs/*` routes, the `browser` flag on the v1 timer endpoints (`0fdf2d7`), and the hard `browser=True` on the two legacy endpoints |
| `tracker.py` | `_browser_on_task_started` / `_browser_on_task_stopped`, the `w` "Save tabs" keybinding, and the bridge's `active_window_id` |
| `mcp_server.py` | `save_task_tabs`, `open_task_window`, `list_task_tabs`, `clear_task_tabs` |
| data model | the per-task `tabs` and `active_window_id` fields |
| docs | the "Task Browser Windows (Safari)" section of `CLAUDE.md`, and this plan |

Two constraints that make this less trivial than the line count suggests:

- **`tracker.py`'s bridge and `wt_daemon.py`'s legacy endpoints must change
  together.** `tools/test_legacy_contract.py` asserts the two produce identical
  payloads, `active_window_id` included; changing one side alone turns the suite
  red for the right reason but the wrong cause.
- **Three harnesses reference the integration** — `test_daemon.py` (21 sites),
  `test_legacy_contract.py` (11), `test_tracker_phase3.py` (2), plus the
  `FakeSafariWindowManager` they share. Deleting the feature means deleting real
  coverage, so do it as its own change with the harnesses updated in the same
  commit, not folded into a phase.

Sequencing note: cheapest **after** Phase 9 packages the app, since that phase
touches the same legacy-contract surface — but it is independent of the phases
and can be done whenever.

### 1b. (subsumed) Retire `active_window_id` from the API surface

Its only consumer was the monitor's Safari border overlay, **removed 2026-08-06**
(`workload-macos-monitor` `f803fb1`). Nothing reads it now.

> ⚠️ **Do not delete the task-level field.** `task["active_window_id"]` is still
> live: `browser_window.py` stores the Safari window id there while a task's
> dedicated tab window is open, and `wt tabs` / the timer start-stop path depend
> on it. Only the **`/status` payload field and its consumers** are dead. A
> future session that greps for the name and deletes every hit will break the
> Safari task-window feature.

What to remove, in order:
- `wt_daemon.py` `legacy_status_payload()` — stop emitting it.
- `tracker.py` `_bridge_status()` — same, so the two stay byte-compatible.
- `tools/test_legacy_contract.py` — drop `"active_window_id": ("number", False)`
  from the expectation table and the assertions around it. **The two must change
  together**: that test asserts daemon/bridge parity, so removing it from one
  side alone turns a green suite red for the right reason but the wrong cause.
- `workload-macos-monitor` `Models.swift` — drop `ActiveTimer.activeWindowID` and
  its `CodingKey`, plus the `DecodingTests` cases covering it.
- `CLAUDE.md` (both repos) — the contract docs describing it.

Kept deliberately until then: the monitor still *decodes* the field, because both
servers still send it and decoding a field you ignore is free, while failing on a
payload the server still emits is not.

### 1c. `reconcile_task_sprints` can report success having written nothing

Found in Phase 6. `sync_project_hours()` swallows a `gh` failure and returns
`False` without raising, so a reconcile whose only operation was an hours update
returns `success: true` while nothing reached GitHub. `hours_synced` is left
unmoved, so a re-run does retry — the data is not corrupted — but **no client can
tell the difference between "synced" and "silently didn't"**, and the app's sync
sheet will say it succeeded.

The same shape as the `load()` bug: a failure that returns a value instead of
raising. Fix in `wt.py`/`wt_api.py` so the outcome carries per-operation success,
and have the daemon surface it; the Swift sheets already decode and report the
flags the daemon *does* send, so most of the client side exists.

### 1d. `task_view()` does not emit `recurrent_series`

The shelf's Series column is built and renders "—" for every row because the
snapshot carries no canonical series name. Phase 6 deliberately did **not**
reimplement `RECURRENT_SERIES_ALIASES` in Swift — that would be a second source
of truth for something the Python side owns — so the column lights up with zero
Swift changes once `wt_api.task_view()` emits it via
`recurrent_series_for_title()`.

Worth knowing before doing it: **2 of the 7 recurrent tasks are not in the alias
table at all** (`1:1 with TomD`, `Alex KC 1:1 calls - casanabria`), so even a
correct implementation resolves five of seven. The aliases need extending, which
is a data question for the owner rather than a code one.

### 1e. Delete the retired recurrent planner, don't just refuse it at the CLI

Surfaced by the **Sprint 106 boundary** (2026-08-10), which is when it first
became reachable. `wt close-recurrent` / `wt new-recurrent` hard-refuse with
`sys.exit(2)`, so the CLI is genuinely blocked — but
`create_current_sprint_recurrent_tasks()` and
`find_recurrent_tasks_to_recreate()` are still importable and still *work*.

Measured at the boundary, called directly on a copy of the live data: the planner
selected **all 7 perpetual series** and ran the full
`create_github_issue → add_issue_to_project → sync_project_status → …` sequence
for each. Mid-sprint it selects nothing, because every series already has a
current-sprint copy — which is why `tools/test_phase3.py`'s "the retired
new-recurrent planner creates nothing for a merged series" passed for weeks and
fails now.

Under the post-Phase-5 model this behaviour is simply wrong: it would mint
per-sprint clones of tasks that are meant to be perpetual. The CLI refusal is a
guard rail in front of live code. **Delete the two functions**, and rewrite that
harness section to assert the *retirement* rather than the incidental "creates
nothing" — an assertion that only holds for 13 days out of every 14 is not an
assertion.

### 1f. Reconcile leaves a stale `hours_synced` on a fresh sprint's binding

Also boundary-only. After a reconcile at the start of Sprint 106,
`tools/test_tracker_phase3.py` finds a binding whose cache and logs disagree:
`{'969e05ef': (6.0, 0.0)}` — 6h recorded as synced for a sprint with no logged
time, because the carry-forward moves the long-lived issue onto the new sprint
without clearing what the *old* sprint had told GitHub.

Self-correcting in practice: the values differ, so the next hours sync writes the
right number. But in the window between, a client reading `hours_synced` — the
app's sync sheet does — will report hours that were never logged in that sprint.
Not present in the live data (no Sprint 106 bindings exist yet), so this is a
latent correctness bug, not current damage.

### 2. Collapse the daemon's `_guarded_load()`

`wt_daemon.Daemon.read()` wraps `wt.load()` in its own probe. That predates the
`wt.py` fix in `9daf5a1`, which moved the same guard into `load()` itself, so
there are now two guards for one hazard. Collapse to the `wt.py` one and keep the
daemon's HTTP mapping (`data_unreadable` → 503). Verify `test_daemon.py`'s
unreadable-file section still fails for the right reason afterwards.

### 3. A committed old-vs-new differential harness

Phase 1 ran a differential of `git show HEAD:mcp_server.py` against the refactored
one — 94 read-only probes, byte-identical results — but it was a throwaway script
and was never committed, so it cannot be re-run. It becomes more valuable, not
less, now that `wt_api` has multiple consumers: it is the cheapest way to prove a
refactor is behaviour-preserving without hand-writing per-tool assertions.

### 4. Teach `tracker.py` to use the daemon

The TUI still holds the whole dataset in memory and saves wholesale, so it can
clobber daemon writes (risk #1). Phase 0's lock cannot fix that. Making the TUI a
daemon client retires the risk entirely, and would let the daemon stop being
"the thing that runs when the TUI isn't".

---

## 14. Explicitly out of scope

- Rewriting or retiring `tracker.py`. It keeps working, unchanged, on :7373. Once
  the app is trusted for daily use, a follow-up can make the TUI a daemon client
  too, which would retire risk #1 entirely.
- Calendar import UI (the TUI's `c` / `AutoLogBatchModal`) — v2.
- Reopening a done task (needs a `gh issue reopen` path in `wt.py` first).
- Saved/named filter presets. `FilterState` is designed to be `Codable` so presets
  are a small additive feature later.
- Retiring the legacy `sprint`/`sprint_id` mirror fields.
- iOS/iPad. The daemon is loopback-only by design.

---

## 15. Sequencing summary

| Phase | Deliverable | Independently useful? |
|---|---|---|
| 0 | Atomic + locked writes, all three paths | ✅ **DONE** — `phase0-atomic-save`, `003f912` |
| 0.5 | Repair the four regression harnesses (§4) | ✅ **DONE** — `phase0.5-repair-harnesses`, `8808f6a` |
| 1 | `wt_api.py`; `mcp_server.py` refactored onto it | Yes — one command layer |
| 2 | `wt_daemon.py` + legacy `:7373` contract on `:7375` | **Yes — the menu-bar monitor works with `tracker.py` closed** |
| 3 | Swift skeleton, sidebar, snapshot rendering | Read-only board |
| 4 | Kanban + drag/drop + close sheet | **First fully usable milestone** |
| 5 | Filtering: 5 facets, tokens, filter menu | Applies to Board now, Timeline on arrival |
| 6 | Recurrent shelf | |
| 7 | Gantt with zoom | |
| 8 | Native polish, menus, a11y, Liquid Glass, icon | |
| 9 | `.app` packaging, signing, login item | Shippable |
