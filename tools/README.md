# `tools/` — the regression harnesses

There is no pytest suite in this repo. These scripts *are* the tests. They all
run **fully offline**: every `gh`-touching function in `wt` is monkeypatched,
`wt.subprocess` is replaced by a guard that raises on any attribute access, and
(where a module does a function-local `import subprocess`) `sys.modules
["subprocess"]` is swapped for a recording fake. A missed stub fails loudly
instead of reaching GitHub. **Never let one of these run `gh issue create` or
`gh issue close` — those writes are irreversible.**

They also refuse to start if the resolved data file is `~/.workload_tracker.json`.
Work on a copy anyway.

---

## Quick start

```bash
# 0. Scratch dir + a copy of the live data. Never point a harness at the original.
mkdir -p /tmp/wt-test
cp ~/.workload_tracker.json /tmp/wt-test/work.json

# 1. Build the three fixtures the Phase 2/3 harnesses expect.
venv/bin/python tools/make_fixtures.py /tmp/wt-test/work.json /tmp/wt-test/fx
#   -> /tmp/wt-test/fx/pre.json  migrated.json  baseline.json

# 2. Run them. Each takes <pre-migration> <migrated> <baseline> <scratch-dir>.
WT_DATA_FILE=/tmp/wt-test/unused.json venv/bin/python tools/test_reconcile.py \
    /tmp/wt-test/fx/pre.json /tmp/wt-test/fx/migrated.json \
    /tmp/wt-test/fx/baseline.json /tmp/wt-test/s-reconcile

WT_DATA_FILE=/tmp/wt-test/unused.json venv/bin/python tools/test_phase3.py \
    /tmp/wt-test/fx/pre.json /tmp/wt-test/fx/migrated.json \
    /tmp/wt-test/fx/baseline.json /tmp/wt-test/s-phase3

WT_DATA_FILE=/tmp/wt-test/unused.json venv/bin/python tools/test_mcp_phase3.py \
    /tmp/wt-test/fx/pre.json /tmp/wt-test/fx/migrated.json \
    /tmp/wt-test/fx/baseline.json /tmp/wt-test/s-mcp

WT_DATA_FILE=/tmp/wt-test/unused.json venv/bin/python tools/test_tracker_phase3.py \
    /tmp/wt-test/fx/pre.json /tmp/wt-test/fx/migrated.json \
    /tmp/wt-test/fx/baseline.json /tmp/wt-test/s-tracker

WT_DATA_FILE=/tmp/wt-test/unused.json venv/bin/python tools/test_wt_api.py \
    /tmp/wt-test/fx/pre.json /tmp/wt-test/fx/migrated.json \
    /tmp/wt-test/fx/baseline.json /tmp/wt-test/s-wt-api

# 3. Phase 0's concurrency harness takes a different shape.
WT_DATA_FILE=/tmp/wt-test/unused.json venv/bin/python tools/test_atomic_save.py \
    /tmp/wt-test/work.json /tmp/wt-test/s-atomic 8
```

`test_phase3.py` re-runs `test_reconcile.py`, and `test_mcp_phase3.py` re-runs
both, so running the MCP one covers three of the five. `test_tracker_phase3.py`
and `test_wt_api.py` are independent (the former redirects `HOME`).

Belt and braces: put a fake `gh` first on `PATH` that logs and exits non-zero,
run everything, and confirm the log stays empty.

---

## Prerequisites

* The venv (`source venv/bin/activate`, or use `venv/bin/python`). The tracker
  harness needs `textual`; the MCP one needs `mcp`.
* The fixture must contain a populated `config.sprints_cache`. Everything sprint
  related is resolved from that cache, which is what makes the runs offline.
  A fixture without it exits 2 with an explanation.

---

## The fixtures, and why `make_fixtures.py` exists

The four harnesses take `<pre-migration.json> <migrated.json> <baseline.json>`.

* **`migrated.json`** — the data file after `wt.load()`. Every behavioural
  section runs against this.
* **`baseline.json`** — `tools/baseline.py`'s snapshot of the pre-migration
  file: per-task log minutes and log counts, plus the shadow→parent map.
  `tools/check_invariants.py` asserts against it.
* **`pre.json`** — a *pre-migration* snapshot: shadow tasks (`cross_sprint_parent`)
  and per-sprint recurrent clones (`<base> - Sprint N`), which `load()` must fold
  into bindings.

The real pre-migration snapshot was a copy of the live file taken before the
sprint-bindings migration ran in July 2026. **It no longer exists anywhere** —
the live file was migrated in place, the worktree the harnesses were developed in
is gone, and no fixture was ever committed.

Handing an already-migrated file to the `pre` slot does not error. It makes the
migration sections pass *vacuously* ("0 shadows became 0 bindings"), which is
worse than failing. So `tools/make_fixtures.py` reconstructs one by running both
migrations backwards, and verifies the round trip (`pre → load() → same tasks,
same log ids, same minutes`) before writing anything.

**What the synthetic `pre.json` is:** a genuine exercise of the migration code.
The shadows and clones are real objects of the real shape; every log id and
every minute is preserved, so "minutes unchanged" is a real assertion.

**What it is not:** a byte-exact inverse. Known, deliberate differences:

| Not reconstructed | Why | Effect |
|---|---|---|
| a non-carry binding's `state` | `_shadow_binding` always writes `"closed"`, so the original value is unrecoverable | an `open` past-sprint binding comes back `closed` |
| `superseded_issues` | needs two shadows colliding on one sprint in a specific order | one fewer superseded entry than the live file (currently 0 either way) |
| `hours_synced`/`synced_at` on the *carry* binding | a pre-migration task had nowhere to store them | those two fields start null |

No assertion depends on any of that — every count is derived from the fixture at
runtime. If a harness ever needs bit-exactness it needs a real archived snapshot,
not this. Such a snapshot would have to contain: the 12 original shadow tasks
with their real `github_issue` refs and marker-log minutes, the per-sprint
recurrent clones with their own ids and issues, and no `sprint_issues` key
anywhere.

---

## What each harness covers

### `test_reconcile.py` — `reconcile_task_sprints()` (plan Phase 2)

Dry-run purity over every task (zero GitHub calls, zero mutation, byte-identical
file); the Option A carry-forward plan for a cross-sprint task; marker-log
independence; partial failure isolation (one sprint's issue creation raising
must not corrupt the others); the deprecated `split_cross_sprint_task` wrapper's
contract; `close_task()` end to end; `wt set-sprint` drift; the hours-withheld
guard; `get_project_info` memoisation; the Phase 5 recurrent merge; a full
stubbed reconcile followed by an idempotency pass and `check_invariants`.

**Does not cover:** anything the CLI, TUI or MCP layers add on top; real GitHub
behaviour (all stubbed); the sprint list itself (read from the cache).

### `test_phase3.py` — `wt.py` and `_wt` (plan Phase 3)

Every touched CLI command runs end to end; `wt sprint` works from the cache
alone; `--all` mints nothing without `--create-issues`; perpetual recurrent
series reconcile like anything else; the `split-sprint` alias; idempotency;
`set-sprint`; `wt done`'s rendering; all `github_issue` access going through the
accessors; the shadow plumbing being gone but the load-time sweep intact;
`check_invariants` against the baseline; the task-creation paths; and a diff of
`close_task`'s GitHub write sequence against `git show HEAD:wt.py`.

**Does not cover:** argument parsing of untouched commands; output formatting
beyond a few content assertions; the zsh completion file (`_wt`) — it is only
grepped, never executed.

**Caveat on section 12:** it compares the working tree's `wt.py` against
`HEAD:wt.py`. On a branch that does not modify `wt.py` the two are identical and
the section is a no-op that still reports "12/12 identical". Read it as "this
change did not alter close_task's GitHub writes", not as an absolute assertion.

### `test_mcp_phase3.py` — `mcp_server.py` (plan Phase 3)

Tool registry shape and the absence of legacy imports; `mcp_server.load()`
running both migrations; `list_tasks` filters; `get_task`'s bindings section;
`sync_task_sprints` dry run and real run plus idempotency; `set_sprint`;
`set_task_status(..., 'done')` end to end; link/unlink/push/notes/rename/delete;
the read-only sprint tools; `check_invariants`; and it re-runs the other two
harnesses.

**Does not cover:** the MCP protocol layer itself (tools are called as plain
Python functions, not over stdio); the 20-odd tools outside the sprint/issue
surface (Arc, iTerm, tabs, calendar) — of the 43 registered tools only the ones
listed above are exercised. Note `mcp_server.resolve_task` returns `None` on an
ambiguous substring match, so tests address tasks by **id**.

**Exactly which tools it drives** (this is the set macOS-app Phase 1 refactored
onto `wt_api.py`; everything else stayed put precisely because it is not here):
`add_task`, `list_tasks`, `get_task`, `set_task_status`, `sync_task_sprints`,
`set_sprint`, `link_github_issue`, `unlink_github_issue`, `push_task_to_github`,
`get_notes_path`, `rename_task`, `delete_task`, `list_sprints`,
`get_current_sprint_info`, `get_status`, and the retired
`close_previous_recurrent_tasks`.

### `test_tracker_phase3.py` — `tracker.py` (plan Phase 3)

Drives the real Textual app headlessly (`App.run_test()` → `Pilot`) with `HOME`
redirected, `arc_browser`/`iterm_manager`/`browser_window` faked and the HTTP
bridge never started. Covers the board render, the sprint column, role filters,
the edit modal on an out-of-window start sprint, the log modal, the sync-sprints
preview and execution, the close workflow's sprint-filtered hours, the bridge
helper functions, the issue-accessor call sites, timer start/stop, static
assertions on the source, and `load_data()`'s migration of shadows.

**Does not cover:** the bridge's HTTP layer (helpers are called directly, no
socket is opened); Arc/iTerm/Safari integration (faked); visual layout.

Its baseline argument is accepted and ignored — it computes its own before/after
totals. It also accepts the older 3-argument form.

### `test_wt_api.py` — `wt_api.py`, the command layer (macOS-app plan Phase 1)

`snapshot()`'s shape and field completeness — every documented key present, and
each derived field asserted *equal to the `wt` primitive it claims to come
from* (`current_issue` vs `task_current_issue`, `reportable_mins` vs
`task_reportable_mins`, `sprints_with_time` vs `task_sprints_with_time` with the
bulky per-entry `logs` key stripped), plus JSON-serializability, purity, and
zero `gh` calls. Then **every one of the 23 `WtError` codes**, each provoked
through a real call, with `ERROR_CODES` cross-checked against the source in both
directions so a new raise or a dead code is loud. Then the command functions the
MCP server does *not* yet route through (timers, log add/edit/delete/split/merge,
`create_task`/`update_task`/`set_task_*`, `ensure_issue`, `plan_close`, `close`,
`plan_reconcile`/`apply_reconcile`/`reconcile`).

Takes the same four arguments as the others; the pre-migration and baseline
fixtures are accepted and unused. Set `WT_FAKE_GH_LOG` to a logging fake `gh`'s
log file and section 10 asserts it stayed empty.

**Does not cover:** the MCP formatting layer on top (that is
`test_mcp_phase3.py`); the ~28 MCP tools still on their own code path (see the
scope block at the top of `wt_api.py`); real GitHub semantics.

Two deliberate fixture rebuilds, both announced in the output: the live data has
**zero** Type options in `project_options_cache`, so the `unknown_type` path is
unreachable until that facet is seeded on the scratch copy; and the `link_issue`
subject's `github_repo` is cleared first, because almost every real task already
has one and the repo-pinning branch would never be taken.

### `test_atomic_save.py` — `wt.save()` / `data_lock()` (Phase 0)

Atomic-write mechanics, lock semantics, 8 concurrent writers against a looping
reader, and that `tracker.py`/`mcp_server.py` delegate to `wt.save()`. Takes
`<source.json> <scratch-dir> <workers>`. This one is the control: if it fails,
suspect the environment, not the change.

### Helpers, not tests

* `baseline.py <data.json> <out.json>` — Phase-0 snapshot.
* `check_invariants.py <data.json> [baseline.json]` — no shadows survive, every
  binding issue is a full `owner/repo#n`, no two bindings share a sprint, and
  totals match the baseline. Sprints with time and no binding are *warnings*.
* `compare_close_task.py` — one-off close_task differ.
* `make_fixtures.py <source.json> <out-dir>` — see above.

---

## The rule that keeps these from rotting again

**Derive expectations from the fixture at runtime. Never hardcode a task title,
a sprint name, an issue number, a minute total or a log count.**

Every failure repaired in Phase 0.5 had the same root cause: the harnesses were
characterisation tests pinned to one afternoon's copy of the live data file, and
that file keeps moving.

* `wt sync-sprints --all` runs at every sprint boundary, so a task that had an
  interesting reconcile plan when the test was written now has an empty one
  ("Assist on Banco Galicia mints an issue for Sprint 95" → `[]`).
* The current sprint rolls over every two weeks, so `"Current sprint: Sprint 105"`
  is a two-week-lived assertion.
* Logs get added, edited and deleted, so `minutes == 26620.71` and
  `logs == 392` were wrong the next day, and a stray 8-second log an assertion
  depended on was simply removed.

So the pattern throughout is: **pick the subject from the fixture, rebuild the
precondition, derive the expectation from that rebuild.** The shared helpers live
in `test_reconcile.py` and are imported by the others:

| Helper | Purpose |
|---|---|
| `multi_sprint_tasks(wt, data, sprints, ...)` / `pick_multi_sprint(...)` | choose a task whose *logs* span N sprints, deterministically |
| `unreconcile(wt, task, sprints, anchor=...)` | roll a task back to the one-issue-per-task shape reconcile exists to fix; never touches `logs` |
| `sprint_time(wt, task, sprints)` | per-sprint minutes, oldest first |
| `new_sprint_boundary(...)` (in `test_phase3.py`) | rebuild the "a new sprint just started" state on a perpetual recurrent series |

When a rebuilt precondition cannot be found in the fixture, the harnesses raise
`SystemExit` with a reason rather than skipping quietly — a section that cannot
test what its name says must be loud.

Two sections deliberately restore state afterwards: `test_mcp_phase3.py` section
8 undoes its rollback before the file reaches section 13, whose baseline
comparison asserts the *migration's* shadow→binding mapping and would otherwise
fail on freshly-minted stub issue refs.

---

## Known gaps worth knowing before you lean on these

1. **`tracker.py` still refuses to reconcile a recurrent task** (`S` on a
   recurrent row prints "handled by 'wt close-recurrent' / 'wt new-recurrent'"),
   and both of those commands are retired and hard-refuse. `wt.py` and
   `mcp_server.py` were updated by Phase 5; the TUI was not.
   `test_tracker_phase3.py` section 7 *asserts the stale behaviour*, so the
   harness will go red when that is fixed — which is the correct signal, but
   read it as a to-do, not a regression.
2. **Nothing here tests the MCP stdio protocol layer**, only the tool functions.
3. **Real GitHub semantics are never exercised.** Every `gh` path is a stub that
   returns a plausible value; a change in what GitHub actually accepts is
   invisible to all of this.
4. **`config.sprints_cache` is treated as ground truth.** A bug in
   `get_all_sprints()` itself cannot be caught offline.
