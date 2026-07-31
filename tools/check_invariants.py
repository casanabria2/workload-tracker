#!/usr/bin/env python3
"""Assert the sprint-bindings invariants from docs/plan-sprint-bindings.md §6.

Runs fully offline (no `gh`, no network) and reads the data file **directly**,
without going through ``wt.load()``, so it reports what is actually on disk
rather than what a migration would produce.

    python3 tools/check_invariants.py <data.json> [baseline.json]

Exit status: 0 when every invariant holds, 1 when any FAIL is reported.
Warnings never fail the run — they flag known-historical data (see plan §5).

Invariants:
  1. No task carries ``cross_sprint_parent``. Titles that look like shadow
     naming (``… (Sprint N)``) are warned about, not failed: a real task could
     legitimately be titled that way.
  2. Every ``sprint_issues[].issue`` is a full ``owner/repo#n`` ref. Bare
     numbers would corrupt the cross-repo cases in the live data.
  3. Per task, {binding sprint_ids} ⊇ {sprints with >0 logged minutes}.
     WARNING only — 8 tasks predate the split feature (plan §5); Phase 2's
     reconcile closes the gap.
  4. No two bindings on a task share a ``sprint_id``.
  5. Against the baseline: task/log/minute totals for surviving tasks are
     byte-identical (all relative to the baseline — never hardcoded).
  6. Every shadow recorded in the baseline now exists as a binding, with the
     right issue and hours, on the right parent task.
"""
import json
import math
import re
import sys
from pathlib import Path

ISSUE_RE = re.compile(r"^[\w.-]+/[\w.-]+#\d+$")
SHADOW_TITLE_RE = re.compile(r"\(Sprint\s+\d+\)\s*$", re.IGNORECASE)


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def fail(self, invariant: str, msg: str) -> None:
        self.failures.append(f"[{invariant}] {msg}")

    def warn(self, invariant: str, msg: str) -> None:
        self.warnings.append(f"[{invariant}] {msg}")

    def check(self, ok: bool, invariant: str, msg: str) -> bool:
        if not ok:
            self.fail(invariant, msg)
        return ok


def _mins(task: dict) -> float:
    return sum(l.get("minutes", 0) for l in task.get("logs", []))


def _log_effective_date(log: dict) -> float:
    """Mirror of wt.log_effective_date (kept local so this tool stays standalone)."""
    return log.get("started_at") or log.get("at", 0)


def _round_up_quarter_hours(mins: float) -> float:
    """Mirror of wt.mins_to_quarter_hours."""
    return (math.ceil(mins / 15) * 15) / 60


def _cached_sprints(data: dict) -> list[dict]:
    from datetime import date
    out = []
    for s in data.get("config", {}).get("sprints_cache", []):
        try:
            out.append({
                "id": s["id"],
                "title": s["title"],
                "start_date": date.fromisoformat(s["start_date"]),
                "end_date": date.fromisoformat(s["end_date"]),
            })
        except (KeyError, ValueError):
            continue
    return out


def _sprints_with_time(task: dict, sprints: list[dict]) -> dict:
    """sprint_id -> minutes, for sprints where the task logged > 0 minutes."""
    from datetime import datetime
    totals: dict[str, float] = {}
    for log in task.get("logs", []):
        ts = _log_effective_date(log)
        if not ts:
            continue
        day = datetime.fromtimestamp(ts).date()
        for s in sprints:
            if s["start_date"] <= day < s["end_date"]:
                totals[s["id"]] = totals.get(s["id"], 0) + log.get("minutes", 0)
                break
    return {sid: m for sid, m in totals.items() if m > 0}


def check_data(data: dict, rep: Report) -> None:
    tasks = data.get("tasks", [])
    sprints = _cached_sprints(data)
    sprint_titles = {s["id"]: s["title"] for s in sprints}

    # 1. No shadows.
    shadows = [t for t in tasks if t.get("cross_sprint_parent")]
    rep.check(not shadows, "1/no-shadows",
              f"{len(shadows)} task(s) still carry cross_sprint_parent: "
              + ", ".join(f"{t.get('title')!r} ({t.get('id')})" for t in shadows))
    for t in tasks:
        if SHADOW_TITLE_RE.search(t.get("title", "")):
            rep.warn("1/shadow-naming",
                     f"task {t.get('title')!r} ({t.get('id')}) looks like shadow "
                     "naming — verify it is a real task")

    for t in tasks:
        bindings = t.get("sprint_issues")
        if bindings is None:
            rep.warn("2/no-bindings-key",
                     f"task {t.get('title')!r} ({t.get('id')}) has no sprint_issues key "
                     "(un-migrated?)")
            continue
        if not isinstance(bindings, list):
            rep.fail("2/bindings-shape",
                     f"task {t.get('title')!r} ({t.get('id')}) sprint_issues is "
                     f"{type(bindings).__name__}, expected list")
            continue

        # 2. Full owner/repo#n issue refs.
        for b in bindings:
            issue = b.get("issue")
            if issue is None:
                continue
            rep.check(bool(ISSUE_RE.match(str(issue))), "2/issue-ref",
                      f"task {t.get('title')!r} ({t.get('id')}) binding for "
                      f"{b.get('sprint') or b.get('sprint_id')} has malformed issue "
                      f"{issue!r} — must be owner/repo#n")

        # 4. One binding per sprint.
        seen: dict = {}
        for b in bindings:
            sid = b.get("sprint_id")
            if sid in seen:
                rep.fail("4/duplicate-binding",
                         f"task {t.get('title')!r} ({t.get('id')}) has two bindings for "
                         f"sprint_id {sid!r}")
            seen[sid] = b

        # 3. Bindings cover every sprint with logged time (warning; plan §5).
        if sprints:
            with_time = _sprints_with_time(t, sprints)
            missing = sorted(set(with_time) - set(seen),
                             key=lambda sid: sprint_titles.get(sid, sid))
            if missing:
                detail = ", ".join(
                    f"{sprint_titles.get(sid, sid)}={with_time[sid]:.0f}m" for sid in missing
                )
                rep.warn("3/binding-coverage",
                         f"task {t.get('title')!r} ({t.get('id')}) has logged time in "
                         f"unbound sprint(s): {detail}")


def check_against_baseline(data: dict, base: dict, rep: Report) -> None:
    tasks = data.get("tasks", [])
    by_id = {t["id"]: t for t in tasks if t.get("id")}
    base_tasks = base.get("tasks", {})
    base_shadows = base.get("shadows", {})

    # 5. Totals, relative to the baseline (never hardcoded).
    rep.check(len(tasks) == base["non_shadow_count"], "5/task-count",
              f"task count is {len(tasks)}, baseline non-shadow count is "
              f"{base['non_shadow_count']}")

    total_mins = round(sum(_mins(t) for t in tasks), 6)
    rep.check(total_mins == base["total_minutes_excluding_shadows"], "5/total-minutes",
              f"total minutes {total_mins} != baseline "
              f"{base['total_minutes_excluding_shadows']}")

    total_logs = sum(len(t.get("logs", [])) for t in tasks)
    rep.check(total_logs == base["total_log_count_excluding_shadows"], "5/total-logs",
              f"total log count {total_logs} != baseline "
              f"{base['total_log_count_excluding_shadows']}")

    # Every non-shadow baseline task must survive with identical logs.
    for tid, snap in base_tasks.items():
        if tid in base_shadows:
            rep.check(tid not in by_id, "5/shadow-removed",
                      f"shadow task {snap['title']!r} ({tid}) still present")
            continue
        task = by_id.get(tid)
        if task is None:
            rep.fail("5/task-missing", f"task {snap['title']!r} ({tid}) disappeared")
            continue
        rep.check(round(_mins(task), 6) == snap["minutes"], "5/task-minutes",
                  f"task {snap['title']!r} ({tid}) minutes {round(_mins(task), 6)} != "
                  f"baseline {snap['minutes']}")
        rep.check(len(task.get("logs", [])) == snap["log_count"], "5/task-log-count",
                  f"task {snap['title']!r} ({tid}) log count "
                  f"{len(task.get('logs', []))} != baseline {snap['log_count']}")
        log_ids = sorted(l.get("id", "") for l in task.get("logs", []))
        rep.check(log_ids == snap["log_ids"], "5/task-log-ids",
                  f"task {snap['title']!r} ({tid}) log ids changed")

    # Unexpected new tasks (the migration must not invent any).
    for tid, task in by_id.items():
        if tid not in base_tasks:
            rep.fail("5/task-added",
                     f"task {task.get('title')!r} ({tid}) is not in the baseline")

    # 6. Each baseline shadow now lives as a binding on its parent.
    for sid, snap in base_shadows.items():
        parent = by_id.get(snap["parent"])
        if parent is None:
            rep.warn("6/shadow-parent-missing",
                     f"shadow {snap['title']!r} ({sid}) parent {snap['parent']} not in "
                     "data — orphan shadows are kept, not converted")
            continue
        bindings = parent.get("sprint_issues") or []
        match = next((b for b in bindings if b.get("sprint_id") == snap["sprint_id"]), None)
        if match is None:
            rep.fail("6/shadow-binding-missing",
                     f"parent {parent.get('title')!r} has no binding for "
                     f"{snap['sprint']} (from shadow {snap['title']!r})")
            continue
        rep.check(match.get("issue") == snap["github_issue"], "6/shadow-binding-issue",
                  f"parent {parent.get('title')!r} binding for {snap['sprint']} has issue "
                  f"{match.get('issue')!r}, shadow had {snap['github_issue']!r}")
        expected_hours = _round_up_quarter_hours(snap["marker_minutes"])
        rep.check(match.get("hours_synced") == expected_hours, "6/shadow-binding-hours",
                  f"parent {parent.get('title')!r} binding for {snap['sprint']} has "
                  f"hours_synced {match.get('hours_synced')!r}, expected "
                  f"{expected_hours} (from {snap['marker_minutes']}m)")
        rep.check(match.get("state") == "closed", "6/shadow-binding-state",
                  f"parent {parent.get('title')!r} binding for {snap['sprint']} state is "
                  f"{match.get('state')!r}, expected 'closed'")


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    data_path = Path(sys.argv[1]).expanduser()
    data = json.loads(data_path.read_text())

    rep = Report()
    check_data(data, rep)

    if len(sys.argv) == 3:
        base = json.loads(Path(sys.argv[2]).expanduser().read_text())
        check_against_baseline(data, base, rep)
    else:
        rep.warn("5/no-baseline", "no baseline given — skipped totals comparison")

    tasks = data.get("tasks", [])
    bindings = sum(len(t.get("sprint_issues") or []) for t in tasks)
    print(f"checked {data_path}")
    print(f"  tasks={len(tasks)}  bindings={bindings}  "
          f"minutes={round(sum(_mins(t) for t in tasks), 2)}  "
          f"logs={sum(len(t.get('logs', [])) for t in tasks)}")

    if rep.warnings:
        print(f"\n{len(rep.warnings)} warning(s):")
        for w in rep.warnings:
            print(f"  ! {w}")
    if rep.failures:
        print(f"\n{len(rep.failures)} FAILURE(s):")
        for f in rep.failures:
            print(f"  x {f}")
        return 1
    print("\nAll invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
