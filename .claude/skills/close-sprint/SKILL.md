---
name: close-sprint
description: Carlos's end-of-sprint checklist — close the previous sprint's recurrent tasks, recreate them for the new sprint, split non-recurrent tasks with hours in the closed sprint (marker-log technique), and re-point stray open tasks to the current sprint. Trigger when the user says "close the sprint", "close Sprint NN", "end of sprint", "sprint just ended", or similar at a sprint boundary.
---

# Close a sprint (end-of-sprint workflow)

Run at every sprint boundary (sprints are 2 weeks; e.g. Sprint 103 = Jun 29 →
Jul 13). "Previous sprint" below = the sprint being closed; "current sprint" =
the one that just started. Every mutating step must be **confirmed with Carlos
first** — do all the read-only analysis, present the full plan, then execute.

## Step 0 — Preconditions (Carlos does this manually)

Carlos verifies all hours are logged on the previous sprint's **recurrent**
tasks before asking to close the sprint. Don't re-audit those, but DO check
for misfiled logs: a recurrent task's per-sprint copy sometimes accumulates
logs dated in the *next* sprint (e.g. a timer left running on the old copy).
For each `Time tracking - Sprint N`-style pair, bucket the logs with
`wt.bucket_logs_by_sprint()` and move any next-sprint logs to the next-sprint
copy, then `wt.sync_project_hours()` on **both** issues (it recomputes
sprint-filtered totals from scratch and fixes the GH Hours fields).

## Step 1 — Close previous-sprint recurrent tasks

```bash
wt close-recurrent --dry-run   # preview + confirm with Carlos
wt close-recurrent             # closes GH issues, sets Status=Done, syncs hours
```

Only targets `status == "recurrent"` tasks with a linked issue in the sprint
immediately before the current one. Recurrent copies without a linked issue
are skipped silently — check the dry-run lists everything expected.

## Step 2 — Recreate recurrent tasks for the current sprint

Use the `new-sprint-recurrent` skill / `wt new-recurrent` (preview with
`--dry-run` first). Often a no-op if the new sprint's copies were already
created at sprint start — "No recurring tasks … to recreate" is fine.

## Step 3 — Split non-recurrent tasks with hours in the closed sprint

Goal: every open (non-done, non-recurrent, non-shadow) task with logged time
in the closed sprint gets a **closed shadow task + GH issue carrying that
sprint's hours**, and the main task moves to the current sprint (we're still
working on it), with its issue's Hours showing only current-sprint time.

Find candidates (read-only):

```python
import wt
data = wt.load()
sprints = wt.get_cached_sprints(data)
for t in data["tasks"]:
    if t.get("cross_sprint_parent") or t["status"] in ("recurrent", "done"):
        continue
    buckets = wt.bucket_logs_by_sprint(t, sprints)
    # report tasks whose buckets include the closed sprint's id
```

Two cases:

1. **Task already has logs in the current sprint** → plain split:
   ```bash
   wt split-sprint "<task>"
   ```
2. **Task has logs only in the closed sprint (or earlier)** → the split
   machinery re-points the main task to the most recent sprint *with logs*,
   which would be the closed one. Fix with the **marker-log technique**:
   append a 0-minute log dated now before splitting:
   ```python
   t["logs"].append({
       "id": wt.uid(), "minutes": 0,
       "note": "Sprint rollover marker: work continues in Sprint <current>",
       "at": time.time(),
   })
   wt.save(data)
   ```
   Then `wt split-sprint "<task>"`. The split now creates closed shadows for
   *every* prior sprint with hours (there may be several — e.g. a task with
   Sprint 102 + 103 hours gets two shadows), re-points the main task to the
   current sprint, and sets the main issue's Hours to 0 (correct — the hours
   live on the shadows; new time syncs as it's logged). **Keep the marker
   log**: it also makes a later `wt done` idempotent — without it, closing
   the task before logging new time would re-point it to the old sprint and
   double-count hours against the shadow.

Notes:
- `wt split-sprint` prompts `Proceed? [Y/n]` — drive with `printf 'y\n' |`
  after Carlos has approved the plan.
- Run splits sequentially, never in parallel (the JSON file is
  read-modify-written per invocation).
- Splits are idempotent: already-split sprints are skipped (shadow exists).

## Step 4 — Re-point stray open tasks

Open tasks assigned to the closed sprint with **no logged time** just move
forward (no shadow needed):

```bash
wt set-sprint "<task>" "Sprint <current>"
```

Also surface (but don't touch without asking) open tasks still pointing at
*older* sprints — they're usually paused work Carlos may want to move, split,
or close.

## Step 5 — Verify

Re-run the candidate scan: no open non-shadow task should have unsplit hours
in the closed sprint, and none should still be assigned to it. Spot-check one
shadow issue on GitHub (Status=Done, Sprint, Hours). Remind Carlos to press
`r` in the TUI to reload.
