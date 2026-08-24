---
name: close-sprint
description: Carlos's end-of-sprint checklist — reconcile every task's per-sprint GitHub issues with `wt sync-sprints` (which now handles recurrent series too), then cross-check the sprint's calendar so every meeting has time accounted for in a task. Trigger when the user says "close the sprint", "close Sprint NN", "end of sprint", "sprint just ended", or similar at a sprint boundary.
---

# Close a sprint (end-of-sprint workflow)

Run at every sprint boundary (sprints are 2 weeks; e.g. Sprint 105 = Jul 27 →
Aug 10). "Previous sprint" below = the sprint being closed; "current sprint" =
the one that just started. Every mutating step must be **confirmed with Carlos
first** — do all the read-only analysis, present the full plan, then execute.

> **This workflow changed.** Tasks are no longer "assigned" to one sprint and
> re-pointed forward, and cross-sprint work no longer creates duplicate *shadow
> tasks*. Each task now carries `sprint_issues[]` — one binding per sprint,
> `{sprint_id, sprint, issue, state, hours_synced}` — and its per-sprint hours
> are derived from log timestamps. The old **marker-log technique is gone**;
> do not append 0-minute rollover logs. See `docs/plan-sprint-bindings.md`.

## Step 0 — Preconditions (Carlos does this manually)

Carlos does a pass over his own hours before asking to close the sprint, so
don't second-guess the *durations* he logged. Step 4 is a different check and it
**is** wanted: it looks for meetings on the calendar with no time against them
at all, which is a gap he cannot see from the tracker alone.

No misfiled-log repair is needed any more: every sprint of a recurring series
shares one task, so `wt sync-sprints` attributes each log to its sprint from the
timestamp and recomputes that sprint's hours from scratch.

## Steps 1–2 — (retired)

Recurring work is no longer cloned per sprint, so there is nothing to close and
recreate. `wt close-recurrent` and `wt new-recurrent` **hard-refuse** — a
recurring series is now one perpetual task with a GitHub issue per sprint, and
Step 3 handles it along with everything else. Step 0's misfiled-log repair is
also gone: all of a series' logs live on one task, so a timer that runs past a
sprint boundary is attributed by timestamp automatically.

## Step 3 — Reconcile everything else

This one command replaces the old "split each cross-sprint task, then re-point
the strays" pair of steps.

```bash
wt sync-sprints --all --dry-run                 # ALWAYS first — review the plan
wt sync-sprints --all --create-issues --dry-run # the plan that will actually run
wt sync-sprints --all --create-issues --yes     # then execute, with confirmation
```

Dry-run **twice**. The plain `--all` plan is not the one that executes: it shows
`SKIP` and `HOLD` where `--create-issues` will show `create` lines and real
hours. Present the second plan to Carlos, not the first.

What it does, per non-recurrent task:

- buckets logs by sprint from their timestamps;
- ensures a binding (and GitHub issue) exists for every sprint with time, plus
  the current sprint for any open task — which is why no marker log is needed;
- sets each binding's Hours from that sprint's minutes, only when it differs
  from what was last synced;
- closes any binding whose sprint has ended (Status=Done + `gh issue close`);
- carries the task's long-lived issue forward to its most recent sprint.

Read the dry-run output carefully before approving:

- **`create` lines mint a real GitHub issue.** `--all` deliberately does *not*
  create issues unless `--create-issues` is passed — those sprints show as
  `SKIP … re-run with --create-issues`. A first run over a long history can
  want to mint a couple dozen issues for sprints that predate this workflow.
  If the list contains old sprints Carlos doesn't want issues for, reconcile
  those tasks individually instead of running `--all --create-issues`.
- **`recurrent` tasks are reconciled like everything else** — the old exclusion
  was removed with steps 1 and 2. A perpetual series is one task with a binding
  per sprint, so reconcile is exactly the right operation on it: it closes the
  ended sprint's issue and mints the new sprint's on the same task, which is
  what `close-recurrent` / `new-recurrent` used to do by hand. Expect one
  `create Sprint <current>` line per series — at the 106→107 boundary that was
  7 of the 10 issues created. Do **not** treat those as duplicates.
- **`HOLD` lines mean hours were withheld.** If a task has time in a sprint
  that has no issue, reconcile refuses to narrow its *other* issues, because
  that would delete the unreported time from the project. Adding
  `--create-issues` gives that time an issue and clears the hold. Never work
  around a HOLD by editing Hours by hand.
- Rounding is up-per-sprint (`mins_to_quarter_hours`), unchanged, so a stray
  1-minute log in a sprint bills 0.25h. If a `create` line shows a suspiciously
  tiny sprint, check whether that log is misfiled before approving.

Single task, when you don't want a blanket run:

```bash
wt sync-sprints "<task>" --dry-run
wt sync-sprints "<task>"
```

Notes:
- It prompts `Proceed? [Y/n]`; drive with `printf 'y\n' |` or `--yes` once
  Carlos has approved the plan.
- Run sequentially, never in parallel (the JSON file is read-modify-written).
- Idempotent — a second run reports "Nothing to do". Safe to re-run.
- `wt split-sprint` still works as a deprecated alias.

## Step 4 — Cross-check the calendar

**Every meeting on the calendar must have time accounted for in some task** —
either because the event was imported (a log entry carries its
`calendar_event_uid`) or because a timer session covers its slot. This step
finds the meetings that slipped through.

```bash
python3 tools/calendar_audit.py --sprint "Sprint <previous>"
```

It writes nothing, so it is safe against the live data file. Verdict per event:

| verdict | meaning |
|---|---|
| `LOGGED` | the event's uid is on a log entry — exact accounting |
| `ON-TASK` | the *mapped* task's logs cover most of the slot |
| `OTHER` | logs cover the slot but on a different task — time is in the sprint total, only the attribution differs |
| `PARTIAL` | logs cover some of the slot, under the 60% threshold |
| `GAP` | nothing covers it — unaccounted meeting time |

Coverage is the fraction of the event window filled by merged log intervals,
**not** mere overlap: Carlos double-books heavily, so a 20-minute brush against
a neighbouring meeting's timer would otherwise read as fully covered.

`GAP` and `PARTIAL` are the actionable list. Before proposing anything:

- **A weekday with 0m logged is the strongest signal, and it is ambiguous** —
  it is either PTO or a day he forgot to track. **Ask; never assume.** At the
  106 close, 08-21 looked like a whole missing day and was simply a day off, so
  all five of its meetings were correctly dropped.
- **`OTHER` is usually left alone.** The time is already in the sprint total;
  only the per-task split differs, and "fixing" it means splitting an existing
  log, which shifts hours across several issues. Carlos's call, default no.
- **Never add an event whose window already overlaps a log** — that
  double-counts. At the 106 close, "Enablement Kick Off" looked unlogged but a
  Stand Up Calls timer already covered 93% of it; adding it would have
  duplicated nearly the whole meeting. Guard for this before every write.
- Not every event is work. `calendar_audit.py` filters lunch, focus time,
  school runs, therapy, catch-up blocks and OOO days; extend `NOISE_SUBSTRINGS`
  rather than eyeballing them each sprint. Overnight APAC office hours usually
  were not attended — ask rather than logging them.

**Back-filling an approved gap.** `wt calendar import` is **not** usable here:
it hard-codes a 7-day lookback, so most of a just-ended sprint is out of its
range. Append the log directly instead (recipe in CLAUDE.md), always setting
`calendar_event_uid` so the event can never be re-imported:

```python
task["logs"].append({
    "id": wt.uid(), "minutes": mins, "note": f"Calendar: {ev['title']}",
    "at": ev["end_date"], "started_at": ev["start_date"],
    "ended_at": ev["end_date"], "calendar_event_uid": ev["uid"],
})
```

Then push the corrected hours — the sprint is already closed by this point, and
a closed issue still accepts a project Hours edit:

```bash
wt sync-sprints "<task>"        # dry-run first; expect exactly one hours line
```

## Step 5 — Verify

```bash
python3 tools/check_invariants.py ~/.workload_tracker.json
wt sprint                       # tasks grouped by the current sprint's bindings
wt report --sprint "Sprint <previous>"
```

`check_invariants.py` should exit 0. It warns (not fails) about sprints with
logged time and no binding — after a successful reconcile that list should be
empty or only contain tasks you deliberately skipped. Spot-check one
newly-created issue on GitHub (Status=Done, Sprint, Hours) and one
carried-forward issue (Sprint = current, Hours = only this sprint's).

Two more checks worth running, both cheap:

```bash
wt sync-sprints --all --dry-run   # must now report "Nothing to do"
```

and confirm `check_invariants.py` reports the same `minutes=` and `logs=` as
before the run — reconcile never touches `logs`, so a change there means
something went wrong. `bindings=` should grow by exactly the number of issues
created.

There is no TUI reload step: `tracker.py` is retired and stays closed, so
nothing needs to be told to re-read the file.
