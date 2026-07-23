---
name: new-sprint-recurrent
description: Recreate Carlos's recurring per-sprint tasks (+ GitHub issues) at the start of a new sprint using `wt new-recurrent`. Trigger when the user asks to "create the recurrent tasks for this sprint", "set up the new sprint", "recreate recurring tasks", or similar at the start of a 2-week sprint.
---

# Recreate recurrent tasks for a new sprint

Carlos starts every 2-week sprint by recreating the same set of recurring
tasks, each suffixed with the sprint name and linked to a fresh GitHub issue.
The built-in `wt new-recurrent` command does all of it — **never** create
these tasks by hand with `wt add` or raw `gh` calls.

## The expected set (as of Sprint 104)

| Task (base name) | Role | Issue repo |
|---|---|---|
| Ad-hoc Slack Questions - Sprint N | other | grafana/field-eng |
| Ana 1:1 calls - casanabria - Sprint N | other | grafana/field-eng |
| General Demo Kit maintenance - Sprint N | demokit | grafana/field-eng-demo-kit |
| Stand Up Calls - casanabria - Sprint N | other | grafana/field-eng |
| Time tracking - Sprint N | other | grafana/field-eng |

The set is derived automatically from the previous sprint's tasks (sprint-suffix
naming convention or `status == "recurrent"`), so if Carlos adds/retires a
recurring series, the command picks that up — the table above is just a sanity
baseline.

## Steps

1. **Preview first** (also confirms the current sprint resolved correctly):
   ```bash
   ./wt new-recurrent --dry-run
   ```
   Sanity-check the list against the previous sprint's recurrent tasks. If it
   says the current sprint can't be resolved, or the list is empty/unexpected,
   stop and investigate (see Failure modes).
2. **Create**:
   ```bash
   ./wt new-recurrent
   ```
   Each line should end with `(issue owner/repo#N, project updated)`.
3. **Report** the created tasks + issue numbers back to Carlos.

## What the command does per task

Copies title/description/role plus the per-task GitHub fields
(`github_repo`/`activity`/`type`) from the previous sprint's copy, re-suffixes
the title to the current sprint, sets `status="recurrent"` + current
`sprint`/`sprint_id`, creates a GitHub issue via `create_github_issue()` and
sets project fields (Status=In Progress, Activity, Type, Sprint, Hours) via
`setup_issue_in_project()`.

## Safety / failure modes

- **Idempotent**: a series that already has a copy in the current sprint is
  skipped — safe to re-run if it partially failed.
- **Aborts if the current sprint can't be resolved** (needs GitHub reachable
  for `get_all_sprints`). Check `gh auth status` and network.
- `--all-previous` sources every earlier sprint instead of just the previous
  one — only needed after skipping a sprint.
- Source tasks without a `github_repo` get a new task but no issue (noted per result).
- If a task is created but its issue step fails, re-running skips the task
  (already exists); link an issue manually with `wt link <task> owner/repo#N`
  or create one via the `new-task-with-issue` flow guidance.

## Companion command

At sprint end, the previous sprint's recurrent copies are bulk-closed with
`wt close-recurrent` (also supports `--dry-run`). Typical cadence: close the
old sprint's copies, then run this skill for the new sprint.
