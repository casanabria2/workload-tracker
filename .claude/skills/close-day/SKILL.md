---
name: close-day
description: Carlos's end-of-day sign-off — summarize the tasks he logged time on today (per-task and total), cross-check the day's Google Calendar meetings against logged time, log any missed meetings, and mark the day closed in config.closed_days. Catches up on skipped days automatically. Trigger when the user says "close the day", "close out today", "end of day", "EOD", "signing off", "wrap up the day", or similar.
---

# Close the day (end-of-day workflow)

Runs when Carlos signs off for the day. Also self-heals: if previous weekdays
were never closed (skill wasn't run), it closes those first, oldest to newest.
All read-only analysis happens first; every mutation (logging a meeting,
marking a day closed) is **confirmed with Carlos** before it runs.

All snippets assume the repo venv (`source venv/bin/activate`) or the `wt`
wrapper, and use `wt.load()` / `wt.save()` — the data file is the iCloud-synced
source of truth, so never `save()` until the in-memory change is verified.

## Gotcha — a running TUI can clobber direct file writes

The TUI holds the whole data file in memory and rewrites it on its own saves —
including the **background GitHub hours sync that fires seconds after a bridge
timer stop**. A direct `wt.save()` landing between two TUI saves is silently
lost (observed live: meeting logs written right after `POST /timer/stop` were
wiped by the stop's async hours sync). When the TUI is open (bridge answers on
`curl -s http://localhost:7373/status`):

1. Stop the timer via the bridge (`POST /timer/stop`, empty body — it computes
   the elapsed time itself), never `wt stop`, so the TUI stays in sync.
2. Let the stop's background GitHub sync settle (~10s) before any direct
   `wt.save()`.
3. After every direct write, re-read the file (`wt.load()`) and verify the
   change stuck before building on it; re-apply if it was clobbered (the
   `calendar_event_uid` duplicate guard makes re-applying safe).
4. Tell Carlos to press `r` in the TUI immediately after the last write, before
   he touches the TUI again.

## State: `config.closed_days`

A dict in `data["config"]["closed_days"]` mapping ISO date → **total minutes
logged that day** (the Step 1 grand total: non-shadow tasks only), kept sorted
by key:

```json
{"2026-07-22": 412.24, "2026-07-23": 390.0}
```

A key present = that day was closed via this workflow. The per-day total is
stored so longer-term analysis can read one number per day instead of
re-scanning every task's logs. It's a **snapshot at close time** — later log
edits aren't reflected; recompute from logs (Step 1 snippet) when exactness
matters. The dict lives in the shared data file, so it syncs across Macs.

**Legacy format**: the first version stored a plain list of ISO dates. If a
list is found, migrate in place first: recompute each listed day's total with
the Step 1 snippet and rewrite the key as a dict.

**First run** (key missing): don't invent a backlog. Ask Carlos which date to
start from (default: today), then treat only weekdays from that date onward as
requiring closure.

## Step 0 — Which days need closing?

```python
import wt
from datetime import date, timedelta
data = wt.load()
closed = data.get("config", {}).get("closed_days", {})
if isinstance(closed, list):          # legacy list format — migrate first (see State)
    closed = {d: None for d in closed}
closed = set(closed)                  # ISO date strings (dict keys)
today = date.today()
# Weekdays from the day after the newest closed day through today, not yet closed.
start = date.fromisoformat(max(closed)) + timedelta(days=1) if closed else today
pending = [d for d in (start + timedelta(days=i) for i in range((today - start).days + 1))
           if d.weekday() < 5 and d.isoformat() not in closed]
print([d.isoformat() for d in pending])
```

- Weekends are never *required* — but if a weekend day in the gap has logs,
  mention it to Carlos and offer to include it.
- Process pending days **oldest first**, one full pass (Steps 1–5) per day.
  The GitHub sync check (Step 4) only needs to run once per *task*, on its
  final pass — the Hours field is a per-sprint running total, not per-day.
- If today is the only pending day, this is the normal single-day flow.
- If an **active timer** is running while closing *today*, it must be stopped
  first — running time isn't in `logs[]` yet, so the summary would undercount.
  With the TUI open, stop it via the bridge (`POST /timer/stop`) after
  confirming with Carlos; otherwise `wt stop`. See the clobber gotcha above.

## Step 1 — Day summary (read-only)

Group the day's logs by task. **Skip shadow tasks** (`cross_sprint_parent`
set): the cross-sprint split gives shadows a synthetic copy of the hours, so
including them double-counts. Use `log_effective_date()` (prefers
`started_at`, when the work actually happened, over `at`, when it was logged).

```python
import wt
from datetime import datetime, date, timedelta
data = wt.load()
day = date(2026, 7, 22)                       # the day being closed
lo = datetime.combine(day, datetime.min.time()).timestamp()
hi = (datetime.combine(day, datetime.min.time()) + timedelta(days=1)).timestamp()
total = 0.0
for t in data["tasks"]:
    if t.get("cross_sprint_parent"):
        continue
    todays = [l for l in t.get("logs", []) if lo <= wt.log_effective_date(l) < hi]
    if todays:
        mins = sum(l.get("minutes", 0) for l in todays)
        total += mins
        print(f"{t['title']}: {wt.fmt_mins(mins)}")
        for l in todays:
            print(f"  - {wt.fmt_mins(l.get('minutes', 0))}  {l.get('note', '')}")
print(f"TOTAL: {wt.fmt_mins(total)}")
```

Present this to Carlos as a compact per-task table (task, role, minutes, notes)
with the grand total. A day with zero logs is worth calling out explicitly —
it's either a day off (fine, close it) or a day of unlogged work.

## Step 2 — Calendar cross-check (read-only)

Fetch the day's meetings and split them into logged vs. unlogged. An event is
"logged" when its `uid` appears in `get_imported_calendar_uids(data)` (covers
both event-imported-as-task and event-logged-to-existing-task).

```python
import wt
data = wt.load()
cal_id = data.get("config", {}).get("calendar_id", "primary")
events = wt.get_calendar_events(calendar_id=cal_id, start_date=day, end_date=day)
imported = wt.get_imported_calendar_uids(data)
for ev in events:
    done = ev["uid"] in imported
    target = wt.resolve_event_to_task(data, ev)   # mapped task for this sprint, or None
    print(f"{'✓' if done else '✗'} {ev['title']} ({ev['duration_mins']:.0f}m)"
          + (f" → {target['title']}" if target and not done else ""))
```

Notes:
- `get_calendar_events()` **skips all-day events** — those never need logging.
- The event dict has no attendance status, so a `✗` may just mean Carlos
  declined or skipped the meeting. Never auto-log; list the unlogged events
  (with the mapped target task and a `round_up_to_30(duration_mins)` suggested
  duration) and ask which to log, which to skip.
- If `get_gcal_service()` isn't set up / errors, say so, show the log summary
  anyway, and ask Carlos whether to close the day without the calendar check.

## Step 3 — Log the missed meetings Carlos confirms

For each confirmed event, log to the task from `resolve_event_to_task()` (it
does the sprint-aware routing — don't resolve mappings by hand). If no mapping
resolves, ask Carlos which task to use, then offer to remember the mapping with
`wt calendar map "<event title>" "<task title>"`. Preserve the UID so the event
never re-flags as unlogged:

```python
import wt
data = wt.load()
task = wt.resolve_event_to_task(data, ev)     # or the task Carlos picked
mins = wt.round_up_to_30(ev["duration_mins"]) # Carlos can override per event
task.setdefault("logs", []).append({
    "id": wt.uid(),
    "minutes": float(mins),
    "note": f"Calendar: {ev['title']}",
    "at": ev["end_date"],
    "started_at": ev["start_date"],
    "ended_at": ev["end_date"],
    "calendar_event_uid": ev["uid"],
})
wt.save(data)
```

Run these sequentially (the JSON file is read-modify-written), then re-print
the Step 1 summary so Carlos sees the corrected day total.

## Step 4 — GitHub issue sync check

For every task that got time today (the Step 1 list) and has a linked
`github_issue`, verify the GH Project's Hours field matches our data. The
value GitHub should show is the **sprint-filtered** local total rounded to
quarter hours — exactly what `sync_project_hours()` writes:

```python
import wt
data = wt.load()
sprints = wt.get_cached_sprints(data)
for t in todays_tasks:                       # tasks with logs today, from Step 1
    issue = t.get("github_issue")
    expected = wt.mins_to_quarter_hours(wt.task_logged_mins_for_sprint(t, sprints))
    gh = wt.get_project_hours(issue, data) if issue else None   # network call per issue
    print(t["title"], t.get("github_repo"), t.get("activity"), expected, gh)
```

- **In sync**: `gh == expected` — nothing to do.
- **Mismatch**: confirm with Carlos, then `wt.sync_project_hours(issue, t, data)`
  (re-syncs Status/Activity/Type/Sprint/Hours in one shot; run sequentially,
  it makes several network calls per issue). It marks logs uploaded in the
  local data — `wt.save(data)` after, minding the TUI clobber gotcha.
- **No linked issue**: nothing to verify; flag it if the task *does* have a
  `github_repo` (an issue is expected eventually via the close workflow).
- `gh is None` with an issue linked usually means the issue isn't in the
  configured project — surface it rather than silently passing.

## Step 5 — Mark the day closed

Only after Carlos confirms the day looks right (for *today*, that means he's
actually signing off):

Record the day with its Step 1 grand total (in minutes):

```python
import wt
data = wt.load()
cd = data.setdefault("config", {}).get("closed_days", {})
if isinstance(cd, list):              # legacy list format
    cd = {d: None for d in cd}        # backfill totals via the Step 1 snippet
cd[day.isoformat()] = round(total, 2) # `total` from the (post-Step-3) Step 1 summary
data["config"]["closed_days"] = dict(sorted(cd.items()))
wt.save(data)
```

Idempotent — re-closing an already-closed day just refreshes its stored total.

## Step 6 — Sign-off recap

After the last pending day is closed, give Carlos a one-screen recap:

1. **Per-task table** for the closed day(s), one row per task with time today:
   Task | Repo | Activity | Today | Sprint (local) | GitHub — where *Sprint
   (local)* is `task_logged_mins_for_sprint()` and *GitHub* is the project
   Hours field from Step 4 (with a ✓/✗ sync indicator). Use `—` for tasks
   without a repo/issue.
2. Total time per closed day and meetings logged during this session.
3. Anything left deliberately open (skipped meetings, sync mismatches Carlos
   chose not to fix, a zero-log day he closed anyway).

Remind him to press `r` in the TUI if it's open, so it picks up the new logs.
