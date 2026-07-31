---
name: close-sprint
description: Carlos's end-of-sprint checklist — close the previous sprint's recurrent tasks, recreate them for the new sprint, then reconcile every other task's per-sprint GitHub issues with `wt sync-sprints`. Trigger when the user says "close the sprint", "close Sprint NN", "end of sprint", "sprint just ended", or similar at a sprint boundary.
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

Carlos verifies all hours are logged before asking to close the sprint. Don't
re-audit those.

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
wt sync-sprints --all --dry-run          # ALWAYS first — review the plan
wt sync-sprints --all --create-issues    # then execute, with confirmation
```

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
- **`recurrent` tasks are always skipped** and listed as such — steps 1 and 2
  own them.
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

## Step 4 — Verify

```bash
python3 tools/check_invariants.py ~/.workload_tracker.json
wt sprint                       # tasks grouped by the current sprint's bindings
wt report --sprint "Sprint <previous>"
```

`check_invariants.py` should exit 0. It warns (not fails) about sprints with
logged time and no binding — after a successful reconcile that list should be
empty or only contain tasks you deliberately skipped. Spot-check one
newly-created issue on GitHub (Status=Done, Sprint, Hours) and one
carried-forward issue (Sprint = current, Hours = only this sprint's). Remind
Carlos to press `r` in the TUI to reload.
