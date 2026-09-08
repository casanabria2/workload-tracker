#!/usr/bin/env python3
"""Read-only audit: does every meeting in a sprint have time accounted for?

Answers the sprint-close question "is every calendar meeting represented by
logged time on some task?" — either because the event was imported (the log
carries its `calendar_event_uid`), or because a timer session covers its slot.

    python3 tools/calendar_audit.py                     # the sprint that just ended
    python3 tools/calendar_audit.py --sprint "Sprint 106"
    python3 tools/calendar_audit.py --sprint "Sprint 106" --all-events

Writes nothing, ever. It makes Google Calendar + cached-sprint reads only, so
it is safe to run against the live data file. Verdicts per event:

    LOGGED    the event's uid is on a log entry — imported, exact accounting
    ON-TASK   no uid, but the *mapped* task's logs cover most of the slot
    OTHER     logs cover most of the slot but on a different task (the time is
              counted in the sprint total, only the attribution differs)
    PARTIAL   logs cover some of the slot but under COVERED_THRESHOLD
    GAP       nothing covers the slot; this is unaccounted meeting time

Coverage is measured as the fraction of the event's window filled by merged log
intervals, *not* mere overlap — with a double-booked calendar a 20-minute
brush against a neighbouring meeting's timer would otherwise read as covered.

`GAP` is the actionable list. Note `wt calendar import` only reaches 7 days
back, so at a sprint boundary most of the sprint is out of its range — back-fill
with the programmatic recipe in CLAUDE.md (append to `task["logs"]` with
`calendar_event_uid` set), then re-run `wt sync-sprints "<task>"` to push the
corrected hours onto that sprint's issue. Refuse to add any event whose window
already overlaps an existing log, or you double-count.
"""
import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wt  # noqa: E402

# Events that are never work, so never a GAP. Extend as new blockers appear.
# Keep in sync with the "calendar events to never log" note in Claude's memory.
NOISE_SUBSTRINGS = [
    "lunch", "focus time", "pick up kids", "pickup kids", "therapy",
    "drumming lesson", "morning catch up", "afternoon catch up",
    "out of office", "happy hour", "ooo",
    # Overnight/early open-invite blocks Carlos does not attend (stated at the
    # Sprint 107 close). They sit outside his working hours, so they surfaced
    # as a GAP every sprint.
    "tuesday breakouts", "office hours: fullstack o11y",
]

# All-day markers (>= this many minutes) are context, not meetings.
ALL_DAY_MINS = 600

# Fraction of an event's window that must be covered by logs to count as
# accounted for. Below this (but above zero) an event reports PARTIAL.
COVERED_THRESHOLD = 0.6


def is_noise(title: str) -> bool:
    t = wt.normalize_event_title(title)
    return any(s in t for s in NOISE_SUBSTRINGS)


def previous_sprint(sprints, today):
    """The sprint that just ended (or the one containing yesterday)."""
    past = [s for s in sprints if s["end_date"] <= today]
    if past:
        return max(past, key=lambda s: s["end_date"])
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sprint", help='sprint title, e.g. "Sprint 106" '
                                     "(default: the sprint that just ended)")
    ap.add_argument("--all-events", action="store_true",
                    help="also list events filtered out as noise/all-day")
    ap.add_argument("--calendar", default=None, help="calendar id override")
    args = ap.parse_args()

    data = wt.load()
    sprints = wt.get_cached_sprints(data)
    if not sprints:
        print("no cached sprints — open the TUI or run wt sprint once to populate "
              "config.sprints_cache")
        return 1

    if args.sprint:
        target = next((s for s in sprints if s["title"] == args.sprint), None)
        if not target:
            print(f"unknown sprint {args.sprint!r}; known: "
                  f"{', '.join(s['title'] for s in sprints[-6:])}")
            return 1
    else:
        target = previous_sprint(sprints, dt.date.today())
        if not target:
            print("could not determine the previous sprint; pass --sprint")
            return 1

    start, end = target["start_date"], target["end_date"]   # half-open
    last_day = end - dt.timedelta(days=1)
    print(f"=== {target['title']}: {start} -> {last_day} inclusive ===\n")

    cal_id = args.calendar or data.get("config", {}).get("calendar_id", "primary")
    events = wt.get_calendar_events(start_date=start, end_date=last_day,
                                   calendar_id=cal_id)
    imported = wt.get_imported_calendar_uids(data)

    # ---- index the sprint's logs -------------------------------------------
    by_day = {}
    by_day_task = {}
    total = 0.0
    for task in data["tasks"]:
        for log in task.get("logs", []) or []:
            day = dt.datetime.fromtimestamp(wt.log_effective_date(log)).date()
            if not (start <= day < end):
                continue
            mins = float(log.get("minutes") or 0)
            total += mins
            by_day.setdefault(day, []).append((task, log))
            by_day_task.setdefault(day, {}).setdefault(task["id"], []).append(log)

    ooo_days = {
        dt.datetime.fromtimestamp(e["start_date"]).date()
        for e in events
        if "out of office" in wt.normalize_event_title(e["title"])
    }

    # ---- per-day totals ----------------------------------------------------
    print(f"TOTAL LOGGED: {wt.fmt_mins(total)}\n")
    print("per-day:")
    worked = 0
    zero_weekdays = []
    day = start
    while day < end:
        entries = by_day.get(day, [])
        mins = sum(float(l.get("minutes") or 0) for _, l in entries)
        tags = []
        if day in ooo_days:
            tags.append("OOO")
        if day.weekday() >= 5:
            tags.append("weekend")
        if entries:
            worked += 1
            starts = [l.get("started_at") or l.get("at") for _, l in entries]
            ends = [l.get("ended_at") or l.get("at") for _, l in entries]
            starts = [s for s in starts if s]
            ends = [e for e in ends if e]
            span = (f"{dt.datetime.fromtimestamp(min(starts)):%H:%M}"
                    f"-{dt.datetime.fromtimestamp(max(ends)):%H:%M}") if starts else "-"
            cover = (f"{mins / ((max(ends) - min(starts)) / 60.0) * 100:.0f}%"
                     if starts and max(ends) > min(starts) else "-")
        else:
            span, cover = "-", "-"
            if not tags:
                tags.append("NOTHING LOGGED — PTO or untracked? ask")
                zero_weekdays.append(day)
        print(f"  {day} {day:%a} {wt.fmt_mins(mins):>9} {span:>13} {cover:>6}  "
              f"{' '.join(tags)}")
        day += dt.timedelta(days=1)

    if worked:
        print(f"\n  {worked} day(s) with time; average "
              f"{wt.fmt_mins(total / worked)} per such day")

    # ---- per-event accounting ---------------------------------------------
    def overlapping(s_ts, e_ts, task_id=None):
        hits = []
        for task in data["tasks"]:
            if task_id and task["id"] != task_id:
                continue
            for log in task.get("logs", []) or []:
                ls = log.get("started_at") or log.get("at")
                le = log.get("ended_at") or log.get("at")
                if ls and le and ls < e_ts and le > s_ts:
                    hits.append((task, log))
        return hits

    def covered_fraction(s_ts, e_ts, hits):
        """Share of [s_ts, e_ts) filled by the given logs' merged intervals."""
        window = e_ts - s_ts
        if window <= 0:
            return 0.0
        spans = []
        for _, log in hits:
            ls = max(log.get("started_at") or log.get("at"), s_ts)
            le = min(log.get("ended_at") or log.get("at"), e_ts)
            if le > ls:
                spans.append((ls, le))
        spans.sort()
        merged = []
        for a, b in spans:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        return sum(b - a for a, b in merged) / window

    print("\nper-event accounting (GAP/PARTIAL = unaccounted meeting time):")
    verdicts = {"LOGGED": 0, "ON-TASK": 0, "OTHER": 0, "PARTIAL": 0, "GAP": 0}
    gaps = []
    for ev in sorted(events, key=lambda e: e["start_date"]):
        dur = float(ev.get("duration_mins") or 0)
        day = dt.datetime.fromtimestamp(ev["start_date"]).date()
        skip = dur >= ALL_DAY_MINS or is_noise(ev["title"]) or day in ooo_days
        if skip:
            if args.all_events:
                print(f"  (skipped) {day} {ev['title'][:56]}")
            continue

        mapped = wt.resolve_event_to_task(data, ev)
        all_hits = overlapping(ev["start_date"], ev["end_date"])
        all_cov = covered_fraction(ev["start_date"], ev["end_date"], all_hits)
        task_cov = 0.0
        if mapped:
            task_cov = covered_fraction(
                ev["start_date"], ev["end_date"],
                overlapping(ev["start_date"], ev["end_date"], mapped["id"]))

        if ev["uid"] in imported:
            verdict, shown = "LOGGED", 1.0
        elif task_cov >= COVERED_THRESHOLD:
            verdict, shown = "ON-TASK", task_cov
        elif all_cov >= COVERED_THRESHOLD:
            verdict, shown = "OTHER", all_cov
        elif all_cov > 0:
            verdict, shown = "PARTIAL", all_cov
        else:
            verdict, shown = "GAP", 0.0
        verdicts[verdict] += 1

        s_dt = dt.datetime.fromtimestamp(ev["start_date"])
        print(f"  {verdict:8} {day} {s_dt:%H:%M} [{dur:>4.0f}m] "
              f"{ev['title'][:42]:44} {shown*100:3.0f}% -> "
              f"{(mapped['title'][:30] if mapped else 'no mapping')}")
        if verdict in ("GAP", "PARTIAL"):
            gaps.append((ev, mapped, shown))
        if verdict in ("OTHER", "PARTIAL"):
            for task, log in all_hits[:3]:
                print(f"{'':11}counted on: {task['title'][:40]:42} "
                      f"{log.get('minutes')}m")

    print(f"\nsummary: {verdicts}")
    if gaps:
        unacc = sum(float(e.get("duration_mins") or 0) * (1 - f) for e, _, f in gaps)
        print(f"\n{len(gaps)} meeting(s) with unaccounted time, "
              f"~{unacc:.0f}m uncovered in total:")
        for ev, mapped, frac in gaps:
            s_dt = dt.datetime.fromtimestamp(ev["start_date"])
            cov = f"{frac*100:.0f}% covered" if frac else "uncovered"
            print(f"  {s_dt:%Y-%m-%d %H:%M} [{ev.get('duration_mins', 0):>4.0f}m] "
                  f"{ev['title'][:40]:42} {cov:14} -> "
                  f"{(mapped['title'][:30] if mapped else 'NO MAPPING — pick a task')}")
        print("\nBefore logging any of these: confirm with Carlos, and never add an "
              "event whose\nwindow already overlaps a log (that double-counts). After "
              "logging, re-run\n`wt sync-sprints \"<task>\"` so the sprint's issue "
              "carries the corrected hours.")
    else:
        print("\nevery meeting is accounted for.")

    if zero_weekdays:
        print(f"\nweekday(s) with no logged time at all: "
              f"{', '.join(str(d) for d in zero_weekdays)}")
        print("  ask whether that was PTO (log nothing) or an untracked working day.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
