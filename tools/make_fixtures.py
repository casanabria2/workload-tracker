#!/usr/bin/env python3
"""Build the three fixture files the Phase 2/3 harnesses take as arguments.

    python3 tools/make_fixtures.py <source.json> <out-dir>

Writes into *out-dir*:

    migrated.json   the source after wt.load() (all migrations applied)
    pre.json        a synthetic **pre-migration** snapshot, de-migrated from it
    baseline.json   tools/baseline.py's snapshot of pre.json

Why this exists
---------------
``tools/test_reconcile.py``, ``tools/test_phase3.py``, ``tools/test_mcp_phase3.py``
and ``tools/test_tracker_phase3.py`` all take
``<pre-migration.json> <migrated.json> <baseline.json> <scratch-dir>``. The real
pre-migration snapshot was a copy of the live data file taken before the
sprint-bindings migration ran in July 2026, and it no longer exists anywhere:
the live file has been migrated in place, the worktree the harnesses were
developed in is gone, and no fixture was ever committed.

Handing the harnesses an already-migrated file for the ``pre`` slot does not
error — it makes their migration sections **pass vacuously** ("0 shadows became
0 bindings"), which is worse than failing. So ``pre.json`` is reconstructed by
running the two migrations backwards:

  * every multi-sprint task's non-carry bindings become **shadow task objects**
    (``cross_sprint_parent`` + a "Sprint split" marker log whose minutes encode
    the binding's ``hours_synced``), exactly the shape
    ``_migrate_shadows_to_bindings`` consumes;
  * every merged perpetual recurrent series is exploded back into one
    ``<base> - Sprint N`` **clone per binding**, with that sprint's logs, which
    is the shape ``_migrate_recurrent_series_to_bindings`` consumes;
  * ``sprint_issues``, ``start_sprint*`` and the two ``config`` migration flags
    are removed, so ``load()`` re-runs both one-time passes rather than skipping
    them.

What this is and is not
-----------------------
It **is** a genuine exercise of the migration code: the shadows and clones are
real objects of the real shape, and ``load()`` has to fold them back. Every log
id and every minute is preserved, so "minutes unchanged" is a real assertion.

It is **not** a byte-exact inverse of the historical migration. Known, deliberate
differences, all documented in tools/README.md:

  * a binding's ``state`` is not recoverable from a shadow (the migration always
    writes ``"closed"``), so any non-carry binding that was ``open`` comes back
    ``closed``;
  * ``superseded_issues`` (two issues on one sprint) is not reconstructed, so the
    round trip has one fewer superseded entry than the live file;
  * ``hours_synced``/``synced_at`` on the *carry* binding are dropped, since a
    pre-migration task had nowhere to store them.

None of the harness assertions depend on those; they are all derived from the
fixture at runtime. If a harness ever needs bit-exactness, it needs a real
archived snapshot, not this.
"""
import copy
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SPRINT_NO = re.compile(r"Sprint\s+(\d+)", re.IGNORECASE)


def _marker_log(wt, binding, seq):
    """A shadow's synthetic 'Sprint split' log, encoding the binding's hours.

    ``_shadow_binding`` reads ``hours_synced = mins_to_quarter_hours(sum(minutes))``
    back off exactly this, so the round trip is lossless for any hours value that
    is a multiple of a quarter hour (all of them are).
    """
    hours = binding.get("hours_synced") or 0
    stamp = binding.get("synced_at") or binding.get("created_at") or 0
    return {
        "id": f"demig{seq:04d}",
        "minutes": round(hours * 60, 6),
        "note": f"Sprint split: {hours}h moved to {binding.get('issue')}",
        "at": stamp,
    }


def _shadowify(wt, task, seq):
    """Turn *task*'s non-carry bindings back into shadow task objects."""
    bindings = list(task.get("sprint_issues") or [])
    if len(bindings) < 2:
        return []
    issue = task.get("github_issue")
    carry = next((b for b in bindings if b.get("issue") and b["issue"] == issue), None)
    if carry is None:
        carry = bindings[-1]
    shadows = []
    for b in bindings:
        if b is carry:
            continue
        seq[0] += 1
        shadows.append({
            "id": f"demigrated{seq[0]:06d}",
            "title": f"{task['title']} ({b.get('sprint')})",
            "description": "",
            "role_id": task.get("role_id", "other"),
            "status": "done",
            "created_at": b.get("created_at") or task.get("created_at") or 0,
            "cross_sprint_parent": task["id"],
            "sprint": b.get("sprint"),
            "sprint_id": b.get("sprint_id"),
            "github_issue": b.get("issue"),
            "github_repo": task.get("github_repo"),
            "logs": [_marker_log(wt, b, seq[0])],
        })
    task["github_issue"] = carry.get("issue")
    task["sprint_id"] = carry.get("sprint_id")
    task["sprint"] = carry.get("sprint")
    return shadows


def _clone_recurrent(wt, task, sprints, seq):
    """Explode a merged perpetual series back into one clone per sprint binding.

    The clone that keeps the survivor's own id is the earliest one, so anything
    referencing the task by id (``active_timer``, calendar mappings) still
    resolves, and the Phase 5 merge picks that same object as the survivor.
    """
    bindings = list(task.get("sprint_issues") or [])
    if len(bindings) < 2:
        return []
    base = wt.strip_sprint_suffix(task["title"])
    buckets = wt.bucket_logs_by_sprint(task, sprints)
    order = sorted(bindings, key=lambda b: [s["start_date"] for s in sprints
                                            if s["id"] == b.get("sprint_id")]
                   or [None])
    clones = []
    for i, b in enumerate(order):
        m = SPRINT_NO.search(b.get("sprint") or "")
        title = f"{base} - Sprint {m.group(1)}" if m else f"{base} - {b.get('sprint')}"
        logs = list(buckets.get(b.get("sprint_id")) or [])
        if i == 0:
            # Orphan logs (no resolvable sprint) ride along with the earliest
            # clone so no minute is dropped from the fixture.
            logs = list(buckets.get(None) or []) + logs
        seq[0] += 1
        clone = {
            "id": task["id"] if i == 0 else f"demigrated{seq[0]:06d}",
            "title": title,
            "description": task.get("description", ""),
            "role_id": task.get("role_id", "other"),
            "status": "recurrent" if i == len(order) - 1 else "done",
            "created_at": (task.get("created_at") or 0) + i,
            "sprint": b.get("sprint"),
            "sprint_id": b.get("sprint_id"),
            "github_issue": b.get("issue"),
            "logs": logs,
        }
        for key in ("github_repo", "activity", "type"):
            if task.get(key):
                clone[key] = task[key]
        clones.append(clone)
    return clones


def demigrate(wt, data):
    """Return a synthetic pre-migration copy of an already-migrated *data*."""
    out = copy.deepcopy(data)
    sprints = wt.get_cached_sprints(out)
    seq = [0]
    tasks = []
    for task in out["tasks"]:
        if wt.recurrent_series_for_title(task.get("title", "")):
            clones = _clone_recurrent(wt, task, sprints, seq)
            if clones:
                tasks.extend(clones)
                continue
        tasks.append(task)
        tasks.extend(_shadowify(wt, task, seq))

    for task in tasks:
        task.pop("sprint_issues", None)
        task.pop("start_sprint", None)
        task.pop("start_sprint_id", None)
    out["tasks"] = tasks
    cfg = out.setdefault("config", {})
    cfg.pop("sprint_bindings_migrated", None)
    cfg.pop("recurrent_series_merged", None)
    return out


def main():
    if len(sys.argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    src, outdir = Path(sys.argv[1]).expanduser(), Path(sys.argv[2]).expanduser()
    live = Path.home() / ".workload_tracker.json"
    if src.resolve() == live.resolve():
        print("REFUSING TO RUN: source is the live data file. Copy it first.",
              file=sys.stderr)
        return 2
    outdir.mkdir(parents=True, exist_ok=True)
    migrated = outdir / "migrated.json"
    pre = outdir / "pre.json"
    baseline = outdir / "baseline.json"
    if migrated.resolve() == live.resolve() or pre.resolve() == live.resolve():
        print("REFUSING TO RUN: output would overwrite the live data file",
              file=sys.stderr)
        return 2

    import os
    os.environ["WT_DATA_FILE"] = str(migrated)
    import wt
    wt.DATA_FILE = migrated
    migrated.write_bytes(src.read_bytes())
    data = wt.load()
    wt.save(data)

    pre.write_text(json.dumps(demigrate(wt, data), indent=2))

    # Round-trip check: the synthetic fixture must migrate back to the same task
    # set, log count and minute total. A silent mismatch here would mean the
    # harnesses are testing a fixture that does not correspond to real data.
    check_dst = outdir / "_roundtrip.json"
    check_dst.write_bytes(pre.read_bytes())
    wt.DATA_FILE = check_dst
    os.environ["WT_DATA_FILE"] = str(check_dst)
    back = wt.load()
    wt.DATA_FILE = migrated
    os.environ["WT_DATA_FILE"] = str(migrated)

    def totals(d):
        return (len(d["tasks"]),
                sum(len(t.get("logs", [])) for t in d["tasks"]),
                round(sum(l.get("minutes", 0) for t in d["tasks"]
                          for l in t.get("logs", [])), 6))

    raw_pre = json.loads(pre.read_text())
    n_shadow = sum(1 for t in raw_pre["tasks"] if t.get("cross_sprint_parent"))
    n_clone = sum(1 for t in raw_pre["tasks"]
                  if wt.recurrent_series_for_title(t.get("title", "")))
    print(f"migrated -> {migrated}")
    print(f"  tasks={totals(data)[0]} logs={totals(data)[1]} minutes={totals(data)[2]}")
    print(f"pre      -> {pre}")
    print(f"  tasks={len(raw_pre['tasks'])} shadows={n_shadow} "
          f"recurrent clones={n_clone}")
    print(f"round trip: pre -> load() -> "
          f"tasks={totals(back)[0]} logs={totals(back)[1]} minutes={totals(back)[2]}")
    ok = totals(back) == totals(data)
    if not ok:
        print("ROUND TRIP MISMATCH — the synthetic fixture does not migrate back "
              f"to the source ({totals(back)} vs {totals(data)})", file=sys.stderr)
    check_dst.unlink(missing_ok=True)

    rc = subprocess.run(
        [sys.executable, str(REPO / "tools" / "baseline.py"), str(pre), str(baseline)],
        check=False)
    if rc.returncode != 0:
        return rc.returncode
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
