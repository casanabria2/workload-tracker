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

## State: `config.closed_days`

A sorted, deduped list of ISO dates (`"2026-07-22"`) in
`data["config"]["closed_days"]`. A date in the list = that day was closed via
this workflow. The list lives in the shared data file, so it syncs across Macs.

**First run** (key missing): don't invent a backlog. Ask Carlos which date to
start from (default: today), then treat only weekdays from that date onward as
requiring closure.

## Step 0 — Which days need closing?

```python
import wt
from datetime import date, timedelta
data = wt.load()
closed = set(data.get("config", {}).get("closed_days", []))
today = date.today()
# Weekdays from the day after the newest closed day through today, not yet closed.
start = date.fromisoformat(max(closed)) + timedelta(days=1) if closed else today
pending = [d for d in (start + timedelta(days=i) for i in range((today - start).days + 1))
           if d.weekday() < 5 and d.isoformat() not in closed]
print([d.isoformat() for d in pending])
```

- Weekends are never *required* — but if a weekend day in the gap has logs,
  mention it to Carlos and offer to include it.
- Process pending days **oldest first**, one full pass (Steps 1–4) per day.
- If today is the only pending day, this is the normal single-day flow.
- If an **active timer** is running while closing *today*, remind Carlos to
  stop it first (`t` in the TUI / `wt stop`) — running time isn't in `logs[]`
  yet, so the summary would undercount.

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

## Step 4 — Mark the day closed

Only after Carlos confirms the day looks right (for *today*, that means he's
actually signing off):

```python
import wt
data = wt.load()
closed = set(data.setdefault("config", {}).get("closed_days", []))
closed.add(day.isoformat())
data["config"]["closed_days"] = sorted(closed)
wt.save(data)
```

Idempotent — re-closing an already-closed day is a no-op.

## Step 5 — Sign-off recap

After the last pending day is closed, give Carlos a one-screen recap: total
time per closed day, meetings logged during this session, and anything left
deliberately open (skipped meetings, a zero-log day he chose to close anyway).
Remind him to press `r` in the TUI if it's open, so it picks up the new logs.
