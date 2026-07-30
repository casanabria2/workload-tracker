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

Carlos verifies all hours are logged on the previous sprint's **recurrent**
tasks before asking to close the sprint. Don't re-audit those.

**Do still check recurrent copies for misfiled logs.** Recurrent tasks are
still one task object per sprint (unifying them into a single task with one
binding per sprint is a later, separate phase), so a timer left running on the
old `… - Sprint N` copy leaves next-sprint logs on the wrong object. For each
`… - Sprint N` pair, bucket with `wt.bucket_logs_by_sprint()`, move any
next-sprint logs to the next-sprint copy, then `wt.sync_project_hours()` on
**both** issues.

Non-recurrent tasks need no such repair: `wt sync-sprints` recomputes every
sprint's hours from the logs, so moving a log and re-running fixes it.

## Step 1 — Close previous-sprint recurrent tasks

```bash
wt close-recurrent --dry-run   # preview + confirm with Carlos
wt close-recurrent             # closes GH issues, sets Status=Done, syncs hours
```

Only targets `status == "recurrent"` tasks with a linked issue in the sprint
immediately before the current one. Recurrent copies without a linked issue are
skipped silently — check the dry-run lists everything expected.

## Step 2 — Recreate recurrent tasks for the current sprint

Use the `new-sprint-recurrent` skill / `wt new-recurrent` (preview with
`--dry-run` first). Often a no-op if the new sprint's copies were already
created at sprint start — "No recurring tasks … to recreate" is fine.

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
