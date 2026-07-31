---
name: new-sprint-recurrent
description: Open the current sprint's GitHub issues for Carlos's perpetual recurring tasks with `wt sync-sprints`. Trigger when the user asks to "create the recurrent tasks for this sprint", "set up the new sprint", "recreate recurring tasks", or similar at the start of a 2-week sprint.
---

# Open the new sprint's recurring issues

> **This changed.** Recurring work is no longer a fresh cloned task per sprint.
> Each series is **one perpetual task** (`status: recurrent`) that never closes
> and grows one GitHub issue per sprint through its `sprint_issues` bindings. So
> there is nothing to *create* — only a new sprint's issue to *open*.
>
> `wt new-recurrent` is retired and hard-refuses. See
> `docs/plan-sprint-bindings.md` Phase 5.

## What to run

```bash
wt sync-sprints --all --dry-run          # review first, always
wt sync-sprints --all --create-issues    # opens the new sprint, closes the old
```

For each perpetual task this plans exactly two things:

- `create <current sprint>` — mints that sprint's issue (what `new-recurrent`
  used to do), and
- `close <previous sprint>` — sets Status=Done and closes the sprint that just
  ended (what `close-recurrent` used to do).

There is deliberately **no `repoint`**: a perpetual series keeps each sprint's
issue permanently. Re-pointing the last sprint's issue onto the new sprint would
strand the hours it carries. If you see a `repoint` line for a recurring task,
something is wrong — stop and check its `status` is still `recurrent`.

## Notes

- `--all` will not mint anything without `--create-issues`; the new sprint shows
  as `SKIP … re-run with --create-issues` until you pass it.
- Idempotent — re-running reports "Nothing to do". Safe if the sprint's issues
  were already opened.
- Run sequentially, never in parallel (the JSON file is read-modify-written).
- Confirm the plan with Carlos before executing; drive the prompt with `--yes`
  once approved.
- Verify after: `python3 tools/check_invariants.py ~/.workload_tracker.json`
  should exit 0, and `wt sprint` should show each series under the new sprint.
