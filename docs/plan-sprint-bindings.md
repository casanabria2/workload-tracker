# Plan: replace shadow tasks with per-sprint issue bindings

**Status:** proposal, not implemented
**Date:** 2026-07-30 (Sprint 105)
**Goal:** one task object per unit of work, forever. Sprint attribution derived
from log timestamps. GitHub issues tracked as a list of per-sprint bindings
instead of duplicate "shadow" task objects.

---

## 1. What's wrong with the current model

### 1.1 Shadow tasks duplicate data, lossily

`split_cross_sprint_task()` creates one shadow task per previous sprint. The
shadow does **not** carry the real logs — it gets a single synthetic log:

```json
{"id": "…", "minutes": 314.09,
 "note": "Sprint split: 5h 14m from IRON Infusion",
 "at": 1781738865}          // ← the split time, NOT when the work happened
```

The real logs stay on the parent. So the shadow's own log is dated in the
*wrong sprint* (whenever the split ran), and its per-log detail is gone.

Consequence: **every** aggregation over `data["tasks"]` must filter shadows out
or it double-counts. In `wt.py` that's 6 filter sites (`logs_in_date_range`,
`cmd_list`, `cmd_sprint`, `resolve_event_to_task`,
`find_recurrent_tasks_to_close`, `find_recurrent_tasks_to_recreate`) plus 3
skip/idempotency guards; in `tracker.py` 3 filters (board render, cross-sprint
detector, bridge task picker) plus the `TaskModal` field-copy list; in
`mcp_server.py` 1 filter (`list_tasks`). Each one is a place where a future
feature silently double-counts hours.

Current data: **91 tasks, 12 of them shadows** (13%).

### 1.2 `sprint_id` is three fields wearing one hat

`task["sprint_id"]` simultaneously means:

1. **Board grouping** — which sprint column the task shows under (`wt sprint`).
2. **Hours filter** — which sprint's minutes to report to GitHub
   (`task_logged_mins_for_sprint`).
3. **GH Project iteration** — the value written to the Sprint field
   (`update_project_sprint`).

When work continues past a sprint boundary these diverge, and the code resolves
it by **mutating `sprint_id` forward** (`split_cross_sprint_task` step "Update
main task sprint to most recent"). That destroys "when did this task start",
which is information Carlos actually wants.

### 1.3 The marker-log hack

Because rollover is implemented as "mutate `sprint_id` to the most recent sprint
*that has logs*", a task worked only in the closed sprint rolls **backwards**.
The documented workaround (`.claude/skills/close-sprint/SKILL.md` step 3, case 2)
is to append a **0-minute log dated now** to fake presence in the new sprint:

```python
t["logs"].append({"id": wt.uid(), "minutes": 0,
                  "note": "Sprint rollover marker: work continues in Sprint <current>",
                  "at": time.time()})
```

There are **3 such marker logs in the live data** right now (visible as
`Sprint 104=0m` on `Implement Sigil instrumentation…`, `CI Check to read Demo
Blocks…`, `Document current FE platform`). The skill also warns you must *keep*
them or a later `wt done` double-counts. This is a data-model deficiency
papered over with a ritual.

### 1.4 Idempotency is bolted on

The main task keeps all logs, so a re-split re-detects the same previous
sprints. `split_cross_sprint_task` guards with "does a shadow already exist for
this `sprint_id`?" and `cmd_split_sprint` mirrors the same set logic in its
preview. Two copies of the same guard, both load-bearing.

### 1.5 Notes / tabs / terminal state fragment

`notes_path(task_id)` is keyed on task id. A shadow gets a **fresh id** → no
notes, no tabs, no iTerm folder. Per-sprint recurrent copies each get their own
id too. Continuous work therefore scatters its local context across N task
objects.

### 1.6 Recurrent tasks are a second, parallel solution to the same problem

Recurrent work spanning sprints is handled by *manually cloning the task per
sprint* with a ` - Sprint NN` title suffix, plus a whole subsystem:
`close-recurrent`, `new-recurrent`, `SPRINT_SUFFIX_RE`, `strip_sprint_suffix`,
`_same_recurrent_series` (prefix-boundary matching to tolerate title drift), and
calendar mappings keyed on *base names* rather than task ids precisely because
titles carry sprint suffixes. That is a lot of machinery to express "this work
continues into the next sprint" — the same thing the split machinery expresses a
different way.

And it leaks: `Ana 1:1 calls - casanabria - Sprint 104` currently holds 38m of
**Sprint 105** time, and `Ad-hoc Slack Questions - Sprint 104` holds 80m of
Sprint 105 time. Step 0 of the close-sprint skill exists to hand-repair this.

### 1.7 Latent bug to fix while we're in here

`sprint_summary_for_task()` sorts by `x["start_date"]` where that value comes
from `s.get("startDate", "")` — the **camelCase** key, which only
`get_all_sprints()` produces. `get_cached_sprints()` produces `start_date` only.
So calling `sprint_summary_for_task(task, get_cached_sprints(data))` sorts every
entry by `""`, making `summary[-1]` ("most recent sprint") *whatever sprint the
last-encountered log happened to fall in*. All current callers pass
`get_all_sprints()` output, so this is latent rather than live — but it's a
loaded gun aimed at "which sprint stays on the main task".

---

## 2. Target model

### 2.1 Schema

Per task, replacing `sprint` / `sprint_id` / `cross_sprint_parent`:

```json
{
  "id": "20260403085012abcd",
  "title": "IRON Infusion",
  "role_id": "iron infusion",
  "status": "done",
  "github_repo": "grafana/field-eng",
  "activity": "…",
  "type": "…",

  "start_sprint_id": "s98",
  "start_sprint": "Sprint 98",

  "logs": [ /* unchanged — the single source of truth */ ],

  "sprint_issues": [
    {"sprint_id": "s98",  "sprint": "Sprint 98",  "issue": "grafana/field-eng#5826",
     "state": "closed", "hours_synced": 5.25,  "synced_at": 1781738865, "created_at": 1781738860},
    {"sprint_id": "s99",  "sprint": "Sprint 99",  "issue": "grafana/field-eng#5827",
     "state": "closed", "hours_synced": 7.75,  "synced_at": 1781738876, "created_at": 1781738870},
    {"sprint_id": "s100", "sprint": "Sprint 100", "issue": "grafana/field-eng#5828",
     "state": "closed", "hours_synced": 38.25, "synced_at": 1781738886, "created_at": 1781738880},
    {"sprint_id": "s101", "sprint": "Sprint 101", "issue": "grafana/field-eng#5829",
     "state": "closed", "hours_synced": 11.75, "synced_at": 1781738897, "created_at": 1781738890},
    {"sprint_id": "s102", "sprint": "Sprint 102", "issue": "grafana/field-eng#5238",
     "state": "open",   "hours_synced": 3.0,   "synced_at": 1781738900, "created_at": 1712181070}
  ]
}
```

Notes on the shape:

- **One list, not `current_issue` + `previous_issues[]`.** Two fields would need
  dual writes and can drift. "Current" is *derived*: the binding whose
  `sprint_id` is the current sprint, else the binding with the latest sprint
  `start_date`. A `task_current_issue(task)` accessor is the only thing the
  ~148 existing `github_issue` references (49 `wt.py` + 53 `tracker.py` +
  46 `mcp_server.py`) need to route through.
- **`issue` is always a full `owner/repo#n` ref.** Non-negotiable: the live data
  already has a task (`CI Check to read Demo Blocks…`) whose parent issue is in
  `grafana/appenv` while its shadow issues are in
  `grafana/field-eng-demo-kit`. A bare number would corrupt these.
- **`start_sprint*` is derived-then-frozen** — computed from the earliest log
  the first time it's needed, then left alone (so a corrective log edit doesn't
  silently rewrite history). `wt set-sprint` becomes "correct the start sprint",
  a rare manual override.
- **`hours_synced` / `synced_at` are a cache of what GitHub was last told**, so
  a reconcile can skip no-op API calls. Never a source of truth — hours are
  always recomputable from `logs`.
- `state` is the *issue's* open/closed state, independent of the task's
  `status`. `PROJECT_STATUS_MAP` applies to the current binding only; past
  bindings are always `Done`.

### 2.2 Derived accessors (new, in `wt.py`)

```python
task_sprint_bindings(task, sprints) -> list[dict]   # sorted by sprint start_date
task_current_issue(task) -> str | None              # replaces task["github_issue"] reads
task_binding_for_sprint(task, sprint_id) -> dict | None
task_start_sprint(task, sprints) -> dict | None      # frozen, or derived from earliest log
task_mins_for_sprint(task, sprint_id, sprints) -> float   # from logs; replaces task_logged_mins_for_sprint
task_sprints_with_time(task, sprints) -> list[dict]  # replaces sprint_summary_for_task
```

`task_mins_for_sprint` deliberately drops the current
`task_logged_mins_for_sprint` fallback of "no `sprint_id` → return the task
total", which silently over-reports for any task whose sprint can't be resolved.
New behaviour: unresolvable → `0.0` for that sprint, and the minutes land in an
explicit `unassigned` bucket that `wt sprint`/`wt report` can surface.

### 2.3 One reconcile function replaces the split

```python
def reconcile_task_sprints(task, data, sprints, *,
                           create_issues=True, close_past=True,
                           sync_hours=True, dry_run=False,
                           save_callback=None, progress_callback=None) -> dict
```

Algorithm — pure function of `logs` + `sprints` + existing bindings:

1. Bucket logs by sprint (`bucket_logs_by_sprint`). Drop zero-total sprints.
2. Target binding set = {sprints with time} ∪ {current sprint, if the task is
   open}. The second term is what removes the need for marker logs: an open
   task always has a place for new work to land, whether or not any minutes
   exist yet.
3. For each target sprint with no binding, create one. Create its GH issue if
   the task has a `github_repo`.
4. For each binding, compute `hours = mins_to_quarter_hours(mins for that
   sprint)`; push to the Project only if it differs from `hours_synced`.
5. For each binding whose sprint has ended: set Status=Done, close the issue,
   `state = "closed"`. (`close_past=False` for a preview/read-only pass.)
6. Never delete a binding. Never touch `logs`.

Properties this buys for free:

- **Idempotent by construction.** Re-running is a diff against a derived target
  set, so both idempotency guards (§1.4) delete themselves.
- **No marker logs** (step 2).
- **No shadow filtering** — there is nothing to filter (§1.1).
- **Self-healing.** Misfiled logs (§1.6) fix themselves: move the log, re-run
  reconcile, both sprints' hours are recomputed from scratch. Today that's a
  hand-written script in the skill's step 0.

### 2.4 Which issue is "current"? (decision point)

Two directions, both eliminate shadow tasks:

**Option A — carry the original forward (recommended).** The task's original
issue stays the long-lived "current" one; crossing a boundary mints a *new*
issue for the **sprint that just ended**, titled `Task (Sprint N)`, closed with
that sprint's hours. The original's Sprint field moves forward and its Hours is
rewritten to the new sprint's total.

This is **exactly what happens on GitHub today.** The change is purely local:
`sprint_issues[]` replaces shadow task objects. That makes the migration
verifiable in the strongest possible way — *GitHub state should not change at
all*, so any diff is a bug. It also keeps discussion and notes on one
long-lived issue, and matches Carlos's description ("current GH issue = the one
in the current sprint; previous issues created as the task crossed boundaries").

**Option B — roll forward.** The original issue stays pinned to its start
sprint and is closed when that sprint ends; each new sprint mints a fresh
"current" issue. Cleaner in principle (an issue's Sprint and Hours never change
after its sprint closes; closing at sprint end is honest) but it reverses
today's GitHub-side behaviour, fragments discussion across issues, and needs a
backlink chain (`Continues from #X` / `Continued in #Y`) to stay navigable.

**Recommendation: Option A.** Ship the local restructuring with zero GitHub
behaviour change; revisit B later as an independent decision if the moving
Sprint/Hours fields on long-lived issues become annoying.

---

## 3. What this changes for the user

| Today | After |
|---|---|
| `wt split-sprint <task>` + marker-log ritual | `wt sync-sprints [<task>]` — idempotent, no ritual |
| close-sprint skill steps 3 **and** 4 (split, then re-point strays) | one `wt sync-sprints --all` |
| `wt list --shadows` | flag deleted (nothing to hide) |
| `wt set-sprint` = re-point a task forward | = correct a wrong start sprint (rare) |
| `wt sprint` groups by mutable `task["sprint"]` | groups by current sprint's bindings; shows "started Sprint N" for carry-overs |
| `wt report --sprint` needs a shadow filter to be correct | authoritative from logs, no filter |
| 12 duplicate task objects in the board's blind spot | 0 |

TUI: the cross-sprint "use `wt split-sprint`" nag notification goes away —
reconcile is no longer something the user has to be told to run.

---

## 4. Implementation phases

### Phase 0 — Backup and pin the GitHub baseline

1. `cp ~/.workload_tracker.json ~/workload_tracker.backup.$(date +%F).json`
2. Snapshot GH Project state for all 79 linked issues (Sprint, Hours, Status)
   into a JSON file. Under Option A this is the assertion target: after
   migration + a full reconcile, this snapshot must be **unchanged**.
3. Record local invariants: total log minutes per task, count of tasks, count
   of logs.

### Phase 1 — Schema, accessors, migration (no behaviour change)

- Add the accessors from §2.2. Every one falls back to legacy fields, so
  nothing breaks.
- `_migrate_shadows_to_bindings(data)`, run from `load()` behind
  `config.sprint_bindings_migrated` — same pattern as the existing
  `_migrate_role_github_fields` / `config.role_fields_migrated_to_tasks`:
  1. For each shadow (`cross_sprint_parent` set), find the parent. Append
     `{sprint_id, sprint, issue: shadow["github_issue"], state: "closed",
     hours_synced: mins_to_quarter_hours(shadow marker minutes)}` to the
     parent's `sprint_issues`.
  2. Append the parent's own `{sprint_id, github_issue, state: open|closed}`
     binding.
  3. Set `start_sprint_id`/`start_sprint` from the parent's earliest log.
  4. **Delete the shadow task object.** Do *not* merge its marker log into the
     parent — the parent already holds the real logs; merging double-counts.
  5. Log an orphan report if a shadow's parent is missing (currently: none).
- Because the file syncs via iCloud, keep the migration idempotent and also
  **strip re-introduced shadows on sight** (an older `wt.py` on the other Mac
  could recreate one), mirroring how `_migrate_role_github_fields` keeps
  stripping legacy role keys.
- Verify: 91 → 79 tasks, 12 shadows → 0, per-task log minutes identical,
  bindings' `hours_synced` matching the shadows' marker minutes.

### Phase 2 — `reconcile_task_sprints()`

- Implement §2.3. Keep `split_cross_sprint_task()` as a thin deprecated wrapper
  so `tracker.py` and `mcp_server.py` imports keep working through the phase.
- Fix the `startDate`/`start_date` sort bug (§1.7) while rewriting
  `sprint_summary_for_task` into `task_sprints_with_time`.
- Ship `--dry-run` first and diff its plan against the Phase-0 GH snapshot for
  all 14 multi-sprint tasks before allowing any writes.

### Phase 3 — Update consumers

- Delete every `cross_sprint_parent` filter and guard (§1.1) and
  `wt list --shadows` (`wt.py:2503-2524`; it has no `_wt` completion entry).
  Add `sync-sprints` to `_wt`, rename `split-sprint`.
- Route all `github_issue` reads through `task_current_issue()`
  (49 in `wt.py`, 53 in `tracker.py`, 46 in `mcp_server.py` — mostly mechanical).
- Rework `close_task()`: call reconcile instead of the inline split; close
  only the current binding.
- Rework `wt sprint`, `wt report`, `wt done` output, `wt split-sprint` →
  `wt sync-sprints`, `wt set-sprint` semantics, `TaskModal`'s sprint Select
  (retargeted onto `start_sprint`).
  - **Correction, found during implementation:** this section originally claimed
    the `InvalidSelectValueError` workaround becomes unnecessary. It does not —
    it becomes *more* necessary. `start_sprint_id` is frozen at the **earliest**
    log, so it falls outside the Select's "current + previous 4" window far more
    often than the old mutable `sprint_id` did (e.g. a task started in Sprint 95
    with current = Sprint 105). Removing the workaround crashes the edit modal.
    It was kept and retargeted.
- MCP: `sprint_split` → `sync_task_sprints`; `get_task` returns bindings.

### Phase 4 — Retire the rituals

- Rewrite `.claude/skills/close-sprint/SKILL.md`: steps 3 and 4 collapse into
  one `wt sync-sprints --all` (still `--dry-run` + confirm first). Delete the
  marker-log technique and step 0's misfiled-log repair.
- Optionally delete the 3 existing 0-minute marker logs (harmless either way).
- Update `CLAUDE.md`: the whole "Cross-sprint split workflow" / "shadow tasks"
  sections.

### Phase 5 — Collapse recurrent per-sprint copies (**separate decision**)

Once 1–4 land, a recurrent task is just a task that never closes and grows one
binding per sprint. That would let us retire `close-recurrent`, `new-recurrent`,
`SPRINT_SUFFIX_RE`, `strip_sprint_suffix`, `_same_recurrent_series`, and revert
calendar mappings from base-name to task-id keying (§1.6) — a big simplification
that also structurally fixes the leaking-logs problem.

But it's a large blast radius (two CLI commands, two MCP tools, two skills, the
calendar mapping migration, and the TUI's second table), and it changes the
shape of Carlos's per-sprint GitHub issues for recurring work. **Do not bundle
it.** Decide after 1–4 are running clean for a sprint or two.

---

## 5. Risks and mitigations

| Risk | Mitigation |
|---|---|
| **Multi-Mac / iCloud.** An old `wt.py` on the other Mac writes shadows or reads bindings it doesn't understand. | Update both Macs in the same session. Migration is idempotent and strips re-introduced shadows. Phase-0 backup before first migrating run. |
| **Irreversible GH writes** during a buggy reconcile (issues created/closed). | `--dry-run` on every reconcile path, mandatory for `--all`. Compare against the Phase-0 snapshot. Under Option A a correct migration produces **zero** GH diff. |
| **8 historical tasks have unsplit multi-sprint hours** (`Assist on Banco Galicia` 95+96, `casanabria - Brokkr support` 97+98, the `Ad-hoc Slack Questions`/`Time tracking` recurrent copies, …) — they predate the split feature. A blanket `sync-sprints --all` would mint ~10 new issues for closed sprints. | Migration **reports** them and creates bindings only for sprints that already have an issue. Backfilling old sprints is an explicit, separate, opt-in run. |
| **Rounding.** `mins_to_quarter_hours` rounds *up* per sprint, so Σ(per-sprint hours) ≥ round(total). | Unchanged from today by design — the plan preserves the exact arithmetic. Flag it, don't "fix" it silently. |
| **No test suite.** | Add `tools/check_invariants.py` (below) and run it before/after every phase. |
| Bare issue numbers in bindings would break the cross-repo case. | §2.1: always store `owner/repo#n`. Assert it in the invariant checker. |

## 6. Verification

`tools/check_invariants.py`, runnable offline against the live file, asserting:

1. No task has `cross_sprint_parent`; no task's title matches `… (Sprint N)`
   shadow naming.
2. Every `sprint_issues[].issue` matches `^[\w.-]+/[\w.-]+#\d+$`.
3. Per task, `{binding.sprint_id}` ⊇ `{sprints with >0 logged minutes}`.
4. No two bindings on a task share a `sprint_id`.
5. Total log minutes and log count match the Phase-0 snapshot exactly.
6. Every binding's `hours_synced == mins_to_quarter_hours(task_mins_for_sprint(...))`.
7. (Online, optional) each binding's GH Project Hours/Sprint/Status matches the
   Phase-0 snapshot.

Manual smoke pass per phase: `wt list`, `wt sprint`, `wt report --sprint
"Sprint 105"`, `wt logs <multi-sprint task>`, `wt sync-sprints --dry-run
"IRON Infusion"`, TUI launch + `r` reload + board render + edit modal on a
task with an old start sprint.

---

## 6b. Known follow-ups (found while implementing phases 1–4, not fixed)

Deliberately left out of scope so the phases stayed reviewable. None affects the
"GitHub state is unchanged" property.

1. **Three loaders, three migrations.** `wt.load()`, `mcp_server.load()` and
   `tracker.load_data()` are independent; each needed
   `_migrate_shadows_to_bindings` wired in separately. A shared loader would stop
   the next migration from having to be remembered three times.
2. **`get_sprint_date_range_for_task()` still keys on legacy `task["sprint_id"]`**,
   so the TUI calendar modal's default date range for a carried-over task points
   at the pre-rollover sprint. Should consult `current_binding()`.
3. **`cmd_add` / `create_task_from_issue` set only `sprint`/`sprint_id`**, never
   `start_sprint*`, so `wt sprint`'s "started Sprint N" is blank for
   CLI-created tasks until their first reconcile. The TUI sets both.
4. ~~**Closing an open carry-over task can report 0h.**~~ **RESOLVED
   2026-07-31 — now reports the latest sprint with time.** `close_task` passes
   `closing=True` to the reconcile, which suppresses the "reserve the current
   sprint for an open task" target. A task being closed has no future work to
   land, so `latest` becomes the newest sprint that actually has minutes, and the
   task's long-lived issue is carried there and reports *that* sprint's hours.
   Open-task reconcile is unchanged — it still reserves the current sprint, which
   is what makes the marker-log ritual unnecessary (§1.3).

   **This is the first change that is not GitHub-neutral**, so Option A's "a
   correct migration produces a zero GH diff" property (§2.4, §5) applies to
   phases 0–4 only, not to this. Measured against the merged Phase 3 across the
   12 previously-split tasks: 10 byte-identical, 2 differing, and in both the
   only changes are (a) the Sprint field no longer parked on the current sprint,
   (b) one fewer issue minted because the carried-forward issue absorbed the
   newest sprint, and (c) `add_to_project_and_update` reporting 0.25h where it
   used to report 0.0h. `tools/test_phase3.py` asserts exactly that allow-list,
   so any *other* removed write fails the build.
4b. ~~**Narrowing an issue's Hours can delete reported time.**~~ **FIXED
   2026-07-31.** Reconcile makes each issue carry *its own* sprint's hours, which
   is only conservative when every other sprint's hours land on an issue of their
   own. With `create_issues=False` (the `--all` default) a deferred sprint has
   nowhere to go, so narrowing the *other* issues silently dropped the
   difference.

   Found by the first live `--all --dry-run`: reading the current Hours off all
   49 target issues gave 41 no-op writes, 2 corrections upward, and **6 narrowing
   writes totalling ~26h**. `Assist on Banco Galicia` alone (Sprint 95 12h30m +
   Sprint 96 6h30m on one issue at 19.0h) would have been set to 6.5h with Sprint
   95's 12.5h reported nowhere.

   `_reconcile_plan` now computes `plan["unbillable"]` — sprints with logged time
   that end up with no issue, whether deferred by `create_issues=False` or bound
   to an issue-less binding. While it is non-empty, **every** hours write for
   that task is withheld and surfaced as a `HOLD` line plus a `WHY` explaining
   how to clear it. The test is structural, so it costs no network call. Sprints
   getting a freshly-minted issue in the same plan count as billable, so
   `--create-issues` clears the guard instead of tripping it, and `close_task`
   (which always mints) is never affected. Verified live: 49 hours writes → 43,
   exactly the 41 no-ops plus the 2 upward corrections. Covered by
   `tools/test_reconcile.py` group 12.

4c. ~~**`get_project_info()` re-fetched per task.**~~ **FIXED 2026-07-31.**
   Project metadata (project id + field/option ids) costs two GraphQL-backed `gh
   project` calls and changes only when the Project itself is edited, but it was
   re-fetched at every call site — including inside each `update_project_*`
   helper when `project_info` wasn't passed down. A `sync-sprints --all` over ~75
   tasks made **118** metadata calls and exhausted the 5000-point GraphQL budget
   mid-run, failing 17 tasks with `gh`'s misleading `unknown owner type`.

   Now memoised per (owner, project number) with a 300s TTL, so a burst run pays
   once (118 → **2**) while a long-lived TUI still picks up new Activity/Type
   options within minutes. Failures are never cached; `refresh=True` /
   `clear_project_info_cache()` force a re-fetch. Covered by
   `tools/test_reconcile.py` group 13.

   Note the partial failure was clean: per-sprint error isolation meant every
   successful write persisted and nothing was left half-applied, so the retry
   only had to do the remainder.

5. **A stray 8-second timer log mints a whole past-sprint issue** billed at
   0.25h, because `mins_to_quarter_hours` rounds up per sprint (preserved
   deliberately, §5). A minimum-minutes threshold in `_reconcile_plan` would
   avoid it.
6. **`setup_issue_in_project()` sets the project Sprint field from the legacy
   `task["sprint_id"]`** while taking Hours from the current binding. The
   reconcile's `relabel` op keeps them agreeing today, but `wt set-sprint` no
   longer touches `sprint_id`, so they could in principle diverge.
7. **`close_task`'s "no issue" refusal is detected by string match** in
   `mcp_server`; a structured `error_code` would be sturdier.
8. **Legacy `sprint`/`sprint_id`/`github_issue` keys are still written** as a
   mirror. Retiring them needs all three loaders updated at once plus both Macs
   on the new code — a coordinated phase of its own.

## 7. Open questions for Carlos

1. **Option A vs B** (§2.4) — recommend A (zero GitHub-side change). Confirm.
2. **Past-sprint issue titles.** Keep today's ` (Sprint N)` suffix on
   past-sprint issues, or rely on the Project's Sprint field alone?
3. **The 8 historical unsplit tasks** (§5) — leave as-is, or backfill issues for
   their closed sprints?
4. **Phase 5** — appetite for collapsing recurrent per-sprint copies later, or
   keep per-sprint clones indefinitely?
