#!/usr/bin/env python3
"""Capture a pre-migration baseline of local time-tracking invariants.

Phase 0 of docs/plan-sprint-bindings.md. Reads a data file WITHOUT going
through wt.load() (so no migration runs) and writes a JSON snapshot that
tools/check_invariants.py asserts against afterwards.

    python3 tools/baseline.py <data.json> <baseline-out.json>

The snapshot is keyed by task id and deliberately records only things the
migration must NOT change: log minutes and log counts. Shadow tasks are
recorded separately with their parent, so the checker can verify each shadow
became a binding on the right task.
"""
import json
import sys
from pathlib import Path


def capture(data: dict) -> dict:
    tasks = data.get("tasks", [])
    per_task = {}
    shadows = {}
    for t in tasks:
        logs = t.get("logs", [])
        per_task[t["id"]] = {
            "title": t.get("title", ""),
            "minutes": round(sum(l.get("minutes", 0) for l in logs), 6),
            "log_count": len(logs),
            "log_ids": sorted(l.get("id", "") for l in logs),
            "github_issue": t.get("github_issue"),
            "sprint_id": t.get("sprint_id"),
            "sprint": t.get("sprint"),
            "status": t.get("status"),
        }
        if t.get("cross_sprint_parent"):
            shadows[t["id"]] = {
                "title": t.get("title", ""),
                "parent": t["cross_sprint_parent"],
                "sprint_id": t.get("sprint_id"),
                "sprint": t.get("sprint"),
                "github_issue": t.get("github_issue"),
                "marker_minutes": round(sum(l.get("minutes", 0) for l in t.get("logs", [])), 6),
            }

    non_shadow = [t for t in tasks if not t.get("cross_sprint_parent")]
    return {
        "task_count": len(tasks),
        "shadow_count": len(shadows),
        "non_shadow_count": len(non_shadow),
        # The number that must never move: total tracked time excluding shadows,
        # since shadow marker logs duplicate their parent's real logs.
        "total_minutes_excluding_shadows": round(
            sum(sum(l.get("minutes", 0) for l in t.get("logs", [])) for t in non_shadow), 6
        ),
        "total_log_count_excluding_shadows": sum(len(t.get("logs", [])) for t in non_shadow),
        "tasks": per_task,
        "shadows": shadows,
    }


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    src, dest = Path(sys.argv[1]).expanduser(), Path(sys.argv[2]).expanduser()
    snap = capture(json.loads(src.read_text()))
    dest.write_text(json.dumps(snap, indent=2, sort_keys=True))
    print(f"baseline -> {dest}")
    print(f"  tasks={snap['task_count']} (shadows={snap['shadow_count']}, "
          f"non-shadow={snap['non_shadow_count']})")
    print(f"  minutes excluding shadows={snap['total_minutes_excluding_shadows']}")
    print(f"  logs excluding shadows={snap['total_log_count_excluding_shadows']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
