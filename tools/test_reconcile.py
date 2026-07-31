#!/usr/bin/env python3
"""Verification harness for reconcile_task_sprints() (plan §2.3 / Phase 2).

There is no automated test suite in this repo, so this script *is* the test for
Phase 2. It runs fully offline: **every** function that shells out to `gh` is
monkeypatched, and ``wt.subprocess`` itself is replaced with a guard that raises
on any use, so a missed stub fails loudly instead of touching real GitHub.

Usage:

    WT_DATA_FILE=/tmp/ignored \\
    venv/bin/python tools/test_reconcile.py <fixture.json> <migrated.json> \\
                                            <baseline.pristine.json> <scratch-dir>

  fixture.json    pre-migration copy of the data file
  migrated.json   already-migrated copy (Phase 1 applied)
  baseline        Phase-0 baseline for tools/check_invariants.py
  scratch-dir     writable directory for working copies

Exit status 0 when every check passes.
"""
import copy
import json
import os
import shutil
import subprocess as real_subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

FAILURES = []
CHECKS = 0


def check(ok, label, detail=""):
    global CHECKS
    CHECKS += 1
    if ok:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}  {detail}")
        FAILURES.append(f"{label}  {detail}")
    return ok


def section(title):
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- gh stubbing --

GH_FUNCS = [
    "create_github_issue", "add_issue_to_project", "sync_project_status",
    "update_project_hours", "update_project_sprint", "update_project_activity",
    "update_project_type", "add_issue_comment", "close_github_issue",
    "delete_github_issue", "ensure_issue_assigned", "issue_has_comments",
    "update_issue_title", "get_project_info", "get_project_hours",
    "get_all_sprints", "add_to_project_and_update", "add_issue_to_project",
]


class SubprocessGuard:
    """Stand-in for wt.subprocess: any use is a bug in the test setup."""

    def __getattr__(self, name):
        def boom(*a, **k):
            raise AssertionError(
                f"wt.subprocess.{name} called with {a!r} — a gh stub is missing"
            )
        return boom


class Stubs:
    """Monkeypatch every gh-touching entry point in wt.

    mode="strict": any call is a hard failure (used to prove dry-run purity).
    mode="record": calls are recorded and plausible values returned.
    """

    def __init__(self, wt, mode="record", fail_on_create=None, sprints=None):
        self.wt = wt
        self.mode = mode
        self.fail_on_create = fail_on_create or set()
        self.sprints = sprints or []
        self.calls = []
        self._saved = {}
        self._issue_no = 900000

    def __enter__(self):
        wt = self.wt
        self._saved["subprocess"] = wt.subprocess
        wt.subprocess = SubprocessGuard()
        for name in set(GH_FUNCS):
            self._saved[name] = getattr(wt, name)
            setattr(wt, name, self._make(name))
        return self

    def __exit__(self, *exc):
        for name, val in self._saved.items():
            setattr(self.wt, name, val)
        return False

    def _make(self, name):
        def stub(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if self.mode == "strict":
                raise AssertionError(f"GitHub call {name}() during a dry run")
            if name == "create_github_issue":
                task, repo = args[0], args[1]
                title = task.get("title", "")
                for needle in self.fail_on_create:
                    if needle in title:
                        raise Exception(f"simulated gh failure for {title!r}")
                self._issue_no += 1
                return f"{repo}#{self._issue_no}"
            if name == "add_issue_to_project":
                return "PVTI_stub"
            if name == "get_project_info":
                return {
                    "project_id": "PVT_stub",
                    "status_field": {"id": "PVTF_status"},
                    "hours_field": {"id": "PVTF_hours"},
                    "status_options": {"Todo": "a", "In Progress": "b", "Done": "c"},
                    "activity_options": {},
                    "type_options": {},
                }
            if name == "get_all_sprints":
                return copy.deepcopy(self.sprints)
            if name == "get_project_hours":
                return None
            if name == "issue_has_comments":
                return True
            if name == "add_to_project_and_update":
                return {"success": True}
            return True
        return stub

    def names(self):
        return [c[0] for c in self.calls]

    def count(self, name):
        return sum(1 for c in self.calls if c[0] == name)


# ------------------------------------------------------------------- fixtures --

def load_copy(wt, src, dst):
    """Copy *src* to *dst*, point wt at it and wt.load() it.

    wt resolves DATA_FILE once at import time, so the module constant has to be
    rebound (not just the env var). Refuses anything that isn't a fresh copy in
    the scratch dir, so no test can reach the live data file.
    """
    dst = Path(dst)
    assert dst.name.endswith(".json") and dst != Path.home() / ".workload_tracker.json"
    shutil.copyfile(src, dst)
    os.environ["WT_DATA_FILE"] = str(dst)
    wt.DATA_FILE = dst
    return wt.load()


def find(data, title):
    for t in data["tasks"]:
        if t["title"] == title:
            return t
    raise SystemExit(f"task not found: {title!r}")


def sig(data):
    return json.dumps(data, sort_keys=True, default=str)


def brief(res):
    out = []
    for op in res["planned"]:
        bits = [op["op"], str(op.get("sprint"))]
        if op["op"] == "create":
            bits.append(f"issue={op['issue_title']!r}" if op["create_issue"]
                        else f"no-issue({op.get('skipped_github')})")
            bits.append(f"{op['minutes']:.0f}m -> {op['hours']}h")
        elif op["op"] == "repoint":
            bits.append(f"{op.get('from_sprint')} -> {op['sprint']} ({op['issue']})")
        elif op["op"] == "hours":
            bits.append(f"{op['issue']} {op.get('from_hours')} -> {op['hours']}h")
        elif op["op"] == "close":
            bits.append(str(op.get("issue")))
        elif op["op"] == "relabel":
            bits.append(f"{op.get('from_sprint')} -> {op['sprint']}")
        out.append("      " + "  ".join(bits))
    return "\n".join(out) or "      (empty plan)"


# ----------------------------------------------------------------------- tests --

def test_dry_run_purity(wt, migrated, scratch):
    section("1. dry-run purity over every task (strict stubs)")
    data = load_copy(wt, migrated, scratch / "dry.json")
    sprints = wt.get_cached_sprints(data)
    before = sig(data)
    disk_before = (scratch / "dry.json").read_text()

    def no_save(_d):
        raise AssertionError("save_callback called during a dry run")

    planned = 0
    with Stubs(wt, mode="strict", sprints=sprints) as st:
        for task in data["tasks"]:
            res = wt.reconcile_task_sprints(
                task, data, sprints, dry_run=True, save_callback=no_save,
            )
            planned += len(res["planned"])
            assert res["dry_run"] is True
        check(st.calls == [], "zero GitHub calls", f"got {st.names()[:5]}")
    check(sig(data) == before, "in-memory data unchanged")
    check((scratch / "dry.json").read_text() == disk_before, "data file unchanged")
    print(f"       ({planned} ops planned across {len(data['tasks'])} tasks)")


def test_historical(wt, migrated, scratch):
    section("2. historical multi-sprint tasks — plan only, nothing executed")
    data = load_copy(wt, migrated, scratch / "hist.json")
    sprints = wt.get_cached_sprints(data)
    before = sig(data)
    results = {}
    with Stubs(wt, mode="strict", sprints=sprints) as st:
        for title in ("Assist on Banco Galicia",
                      "casanabria - Brokkr support for GrafanaCon"):
            task = find(data, title)
            res = wt.reconcile_task_sprints(task, data, sprints, dry_run=True)
            results[title] = res
            print(f"    {title}  (status={task['status']}, "
                  f"issue={task.get('github_issue')})")
            print(f"      target: "
                  + ", ".join(f"{t['sprint']}={t['minutes']:.0f}m/{t['hours']}h"
                              for t in res["target"]))
            print(brief(res))
        check(st.calls == [], "no GitHub calls while planning")
    check(sig(data) == before, "no mutation while planning")

    bg = results["Assist on Banco Galicia"]
    creates = [o for o in bg["planned"] if o["op"] == "create"]
    repoints = [o for o in bg["planned"] if o["op"] == "repoint"]
    check([o["sprint"] for o in creates] == ["Sprint 95"],
          "Banco Galicia mints exactly one new issue, for Sprint 95",
          f"got {[o['sprint'] for o in creates]}")
    check(len(repoints) == 1 and repoints[0]["issue"] == "grafana/field-eng#5069"
          and repoints[0]["sprint"] == "Sprint 96",
          "Banco Galicia carries #5069 forward to Sprint 96 (Option A)",
          f"got {repoints}")
    check(creates and creates[0]["issue_title"].endswith("(Sprint 95)"),
          "past-sprint issue keeps the ' (Sprint N)' title suffix")

    bk = results["casanabria - Brokkr support for GrafanaCon"]
    bkc = [o["sprint"] for o in bk["planned"] if o["op"] == "create"]
    bkr = [o for o in bk["planned"] if o["op"] == "repoint"]
    check(bkc == ["Sprint 97"], "Brokkr mints one new issue, for Sprint 97",
          f"got {bkc}")
    check(len(bkr) == 1 and bkr[0]["sprint"] == "Sprint 98",
          "Brokkr carries #5263 forward to Sprint 98", f"got {bkr}")

    # Blast radius of a blanket run, for the record (plan §5: opt-in backfill).
    with Stubs(wt, mode="strict", sprints=sprints):
        total_issues = 0
        tasks_affected = 0
        for task in data["tasks"]:
            res = wt.reconcile_task_sprints(task, data, sprints, dry_run=True)
            n = sum(1 for o in res["planned"] if o["op"] == "create" and o["create_issue"])
            total_issues += n
            if n:
                tasks_affected += 1
    print(f"    NOTE: a blanket reconcile of all {len(data['tasks'])} tasks would "
          f"mint {total_issues} issues across {tasks_affected} tasks — NOT executed.")


def test_already_split(wt, migrated, scratch):
    section("3. already-split task (IRON Infusion) must mint nothing")
    data = load_copy(wt, migrated, scratch / "iron.json")
    sprints = wt.get_cached_sprints(data)
    task = find(data, "IRON Infusion")
    print(f"    bindings: " + ", ".join(
        f"{b['sprint']}={b['issue']}({b['state']},{b['hours_synced']})"
        for b in task["sprint_issues"]))
    with Stubs(wt, mode="strict", sprints=sprints) as st:
        res = wt.reconcile_task_sprints(task, data, sprints, dry_run=True)
        check(st.calls == [], "no GitHub calls while planning")
    print(brief(res))
    creates = [o for o in res["planned"] if o["op"] == "create"]
    repoints = [o for o in res["planned"] if o["op"] == "repoint"]
    check(creates == [], "no new bindings/issues planned", f"got {creates}")
    check(repoints == [], "no issue re-pointed", f"got {repoints}")
    check([o for o in res["planned"] if o["op"] == "close"] == [],
          "no issue closed (all five bindings are already closed)")
    check({o["op"] for o in res["planned"]} <= {"hours"},
          "the only remaining op kind is an hours re-sync",
          f"got {[o['op'] for o in res['planned']]}")
    hours = [o for o in res["planned"] if o["op"] == "hours"]
    print(f"    hours ops: {[(o['sprint'], o.get('from_hours'), o['hours']) for o in hours]}")
    check(all(o["hours"] == wt.mins_to_quarter_hours(
                  wt.task_mins_for_sprint(task, o["sprint_id"], sprints))
              for o in hours),
          "planned hours equal the round-up-per-sprint value from the logs")


def test_marker_logs(wt, migrated, scratch):
    section("4. marker-log independence (plan §1.3)")
    # The three 0-minute "Sprint rollover marker" logs in the live data. NOTE:
    # the brief named 'Document current FE platform' as one of them; in this data
    # its Sprint-104 presence is a real 0.13-minute Timer session log, not a
    # marker, so it is exercised separately below.
    for title in ("Implement Sigil instrumentation in /validate-demo-blocks-steps",
                  "CI Check to read Demo Blocks content and verify if the change "
                  "would break the demo block",
                  "Move demo block scripts to the new field-eng-demo-blocks repo"):
        data = load_copy(wt, migrated, scratch / "marker.json")
        sprints = wt.get_cached_sprints(data)
        task = find(data, title)
        markers = [l for l in task["logs"]
                   if l.get("minutes", 0) == 0
                   and "rollover marker" in (l.get("note") or "")]
        with Stubs(wt, mode="strict", sprints=sprints):
            res_with = wt.reconcile_task_sprints(task, data, sprints, dry_run=True)
        short = title[:38]
        print(f"    {title[:60]}  status={task['status']}  markers={len(markers)}")
        print(brief(res_with))
        check(len(markers) == 1, f"{short}…: fixture really has a marker log")

        cur = res_with["current_sprint"]
        existing = {b.get("sprint") for b in task.get("sprint_issues") or []}
        bound_after = existing | {o["sprint"] for o in res_with["planned"]
                                  if o["op"] in ("create", "repoint")}
        bound_after -= {o.get("from_sprint") for o in res_with["planned"]
                        if o["op"] == "repoint"}
        if task["status"] == "done":
            check(cur not in bound_after,
                  f"{short}…: a done task gets no current-sprint binding",
                  f"bound={bound_after}")
        else:
            check(cur in bound_after,
                  f"{short}…: open task ends up bound to the current sprint ({cur})",
                  f"bound={bound_after}")
        check(not any(o["op"] == "create" and o["sprint"] == "Sprint 104"
                      for o in res_with["planned"]),
              f"{short}…: no issue minted for the marker's sprint")

        # Same plan with the marker log deleted.
        task["logs"] = [l for l in task["logs"] if l not in markers]
        with Stubs(wt, mode="strict", sprints=sprints):
            res_without = wt.reconcile_task_sprints(task, data, sprints, dry_run=True)
        check(res_with["planned"] == res_without["planned"],
              f"{short}…: deleting the marker log does not change the plan",
              f"\n      with:    {[o['op'] for o in res_with['planned']]}"
              f"\n      without: {[o['op'] for o in res_without['planned']]}")

    # The task the brief named. Its Sprint-104 bucket is a real 0.13m log, so
    # round-up-per-sprint (deliberately preserved) mints a 0.25h issue for it.
    data = load_copy(wt, migrated, scratch / "marker2.json")
    sprints = wt.get_cached_sprints(data)
    task = find(data, "Document current FE platform")
    with Stubs(wt, mode="strict", sprints=sprints):
        res = wt.reconcile_task_sprints(task, data, sprints, dry_run=True)
    print("    Document current FE platform  status=%s" % task["status"])
    print(brief(res))
    s104_id = next(s["id"] for s in sprints if s["title"] == "Sprint 104")
    s104 = wt.bucket_logs_by_sprint(task, sprints).get(s104_id, [])
    check(len(s104) == 1 and s104[0].get("minutes", 0) > 0
          and "marker" not in (s104[0].get("note") or ""),
          "its Sprint-104 log is a real 0.13m Timer session, not a marker",
          str(s104))
    check(any(o["op"] == "create" and o["sprint"] == "Sprint 104"
              and o["hours"] == 0.25 for o in res["planned"]),
          "round-up-per-sprint (preserved by design) bills that 0.13m as 0.25h")
    check(res["current_sprint"] in {o["sprint"] for o in res["planned"]
                                    if o["op"] == "repoint"},
          "its current issue is carried forward to the current sprint",
          str(res["planned"]))


def test_idempotency(wt, migrated, scratch, baseline):
    section("5. full stubbed reconcile, then idempotency + invariants")
    work = scratch / "full.json"
    data = load_copy(wt, migrated, work)
    sprints = wt.get_cached_sprints(data)
    mins_before = sum(sum(l.get("minutes", 0) for l in t.get("logs", []))
                      for t in data["tasks"])
    logs_before = sum(len(t.get("logs", [])) for t in data["tasks"])
    tasks_before = len(data["tasks"])

    totals = {"created": 0, "repointed": 0, "hours_updated": 0, "closed": 0}
    with Stubs(wt, mode="record", sprints=sprints) as st:
        for task in list(data["tasks"]):
            res = wt.reconcile_task_sprints(task, data, sprints,
                                            save_callback=wt.save)
            if not res["success"]:
                check(False, f"run 1 failed for {task['title']!r}", str(res["errors"]))
            for k in totals:
                totals[k] += len(res[k])
        run1_calls = len(st.calls)
        issues_created = st.count("create_github_issue")
        closed = st.count("close_github_issue")
    print(f"    run 1: {totals}  ({run1_calls} stubbed gh calls, "
          f"{issues_created} issues 'created', {closed} issues 'closed')")
    wt.save(data)

    check(len(data["tasks"]) == tasks_before, "no tasks added or removed")
    check(sum(len(t.get("logs", [])) for t in data["tasks"]) == logs_before,
          "log count unchanged")
    check(abs(sum(sum(l.get("minutes", 0) for l in t.get("logs", []))
                  for t in data["tasks"]) - mins_before) < 1e-9,
          "total minutes unchanged")

    # Second run: empty plans, zero calls, zero mutation.
    before = sig(data)
    with Stubs(wt, mode="strict", sprints=sprints) as st:
        leftover = {}
        for task in list(data["tasks"]):
            res = wt.reconcile_task_sprints(task, data, sprints, dry_run=False,
                                            save_callback=wt.save)
            if res["planned"]:
                leftover[task["title"]] = res["planned"]
        check(st.calls == [], "run 2 makes zero GitHub calls", f"{st.names()[:6]}")
    check(not leftover, "run 2 plans nothing for every task",
          json.dumps({k: [o["op"] for o in v] for k, v in leftover.items()})[:400])
    check(sig(data) == before, "run 2 mutates nothing")

    # Every binding's hours_synced now matches the logs (plan §6 invariant 6).
    bad = []
    for t in data["tasks"]:
        for b in t.get("sprint_issues") or []:
            if not b.get("sprint_id") or not b.get("issue"):
                continue
            m = wt.task_mins_for_sprint(t, b["sprint_id"], sprints)
            if m <= 0:
                continue
            if b.get("hours_synced") != wt.mins_to_quarter_hours(m):
                bad.append((t["title"], b.get("sprint"), b.get("hours_synced"),
                            wt.mins_to_quarter_hours(m)))
    check(not bad, "every binding with time has hours_synced == round-up(logs)",
          str(bad[:4]))

    section("7. tools/check_invariants.py after the stubbed full reconcile")
    proc = real_subprocess.run(
        [sys.executable, str(REPO / "tools" / "check_invariants.py"),
         str(work), str(baseline)],
        capture_output=True, text=True)
    print("\n".join("    " + l for l in proc.stdout.strip().splitlines()))
    if proc.stderr.strip():
        print("    stderr:", proc.stderr.strip()[:500])
    check(proc.returncode == 0, "check_invariants exits 0",
          f"rc={proc.returncode}")
    check("3/binding-coverage" not in proc.stdout,
          "no binding-coverage warnings left (28 before)")
    return work


def test_partial_failure(wt, migrated, scratch):
    section("6. partial failure: one sprint's issue creation raises")
    data = load_copy(wt, migrated, scratch / "partial.json")
    sprints = wt.get_cached_sprints(data)
    by_title = {s["title"]: s for s in sprints}

    def mid(sprint):
        d = sprint["start_date"]
        return datetime(d.year, d.month, d.day, 12, 0).timestamp() + 86400

    task = {
        "id": wt.uid(),
        "title": "Synthetic three-sprint task",
        "description": "",
        "role_id": "other",
        "status": "done",
        "github_repo": "grafana/field-eng",
        "created_at": time.time(),
        "sprint_issues": [],
        "logs": [
            {"id": wt.uid() + "a", "minutes": 60, "note": "s95",
             "at": mid(by_title["Sprint 95"]), "started_at": mid(by_title["Sprint 95"])},
            {"id": wt.uid() + "b", "minutes": 120, "note": "s96",
             "at": mid(by_title["Sprint 96"]), "started_at": mid(by_title["Sprint 96"])},
            {"id": wt.uid() + "c", "minutes": 30, "note": "s97",
             "at": mid(by_title["Sprint 97"]), "started_at": mid(by_title["Sprint 97"])},
        ],
    }
    data["tasks"].append(task)

    with Stubs(wt, mode="record", sprints=sprints,
               fail_on_create={"(Sprint 96)"}) as st:
        res = wt.reconcile_task_sprints(task, data, sprints, save_callback=wt.save)
    print(brief(res))
    print(f"    created={[(c['sprint'], c['issue']) for c in res['created']]}")
    print(f"    errors={[(e.get('sprint'), e['error']) for e in res['errors']]}")
    check(res["success"] is False, "success is False")
    check(len(res["errors"]) == 1 and res["errors"][0]["sprint"] == "Sprint 96",
          "exactly one error, against Sprint 96",
          str([(e.get("sprint"), e["op"], e["error"]) for e in res["errors"]]))
    check(any(s.get("reason", "").startswith("aborted") and s.get("sprint") == "Sprint 96"
              for s in res["skipped"]),
          "the failed sprint's follow-up ops are reported as aborted, not errors")
    got = sorted(c["sprint"] for c in res["created"])
    check(got == ["Sprint 95", "Sprint 97"],
          "Sprint 95 and Sprint 97 still succeeded", str(got))
    bound = {b["sprint"]: b for b in task["sprint_issues"]}
    check(set(bound) == {"Sprint 95", "Sprint 97"},
          "no half-written binding for the failed sprint", str(sorted(bound)))
    check(all(b["issue"] and b["state"] == "closed" and b["hours_synced"]
              for b in bound.values()),
          "surviving bindings are coherent (issue + closed + hours)",
          str(bound))
    on_disk = json.loads((scratch / "partial.json").read_text())
    persisted = next(t for t in on_disk["tasks"] if t["id"] == task["id"])
    check(len(persisted["sprint_issues"]) == 2,
          "the successful bindings were persisted to disk",
          str(persisted["sprint_issues"]))

    # A retry closes the gap and nothing is duplicated.
    with Stubs(wt, mode="record", sprints=sprints) as st:
        res2 = wt.reconcile_task_sprints(task, data, sprints, save_callback=wt.save)
    check(res2["success"] and [c["sprint"] for c in res2["created"]] == ["Sprint 96"],
          "retry creates only the missing Sprint 96 binding",
          str([c["sprint"] for c in res2["created"]]))
    check(len(task["sprint_issues"]) == 3 and
          len({b["sprint_id"] for b in task["sprint_issues"]}) == 3,
          "three distinct bindings, no duplicates")


def test_wrapper(wt, migrated, scratch):
    section("8. split_cross_sprint_task() wrapper keeps its contract")
    data = load_copy(wt, migrated, scratch / "wrap.json")
    sprints = wt.get_cached_sprints(data)
    task = find(data, "Assist on Banco Galicia")
    with Stubs(wt, mode="record", sprints=sprints) as st:
        res = wt.split_cross_sprint_task(task, data, wt.save, sprints)
    check(set(res) == {"success", "sprint_tasks_created", "main_sprint", "error"},
          "return keys unchanged", str(sorted(res)))
    check(res["success"] is True, "success", str(res))
    check(res["main_sprint"] == "Sprint 96", "main_sprint is the most recent sprint",
          str(res["main_sprint"]))
    check([e["sprint"] for e in res["sprint_tasks_created"]] == ["Sprint 95"],
          "one previous sprint reported", str(res["sprint_tasks_created"]))
    check(all("cross_sprint_parent" not in t for t in data["tasks"]),
          "no shadow task objects created")
    print(f"    sprint_tasks_created={res['sprint_tasks_created']}")

    # Single-sprint gate preserved.
    single = find(data, "Update SLO Workshop")
    with Stubs(wt, mode="strict", sprints=sprints):
        res2 = wt.split_cross_sprint_task(single, data, wt.save, sprints)
    check(res2["error"] == "Task only has time in one sprint",
          "single-sprint gate preserved", str(res2))

    # Second call on the same task is a no-op on GitHub.
    with Stubs(wt, mode="strict", sprints=sprints) as st:
        res3 = wt.split_cross_sprint_task(task, data, wt.save, sprints)
        check(st.calls == [], "re-split makes no GitHub calls", str(st.names()))
    check(res3["success"] and all(e.get("skipped") for e in res3["sprint_tasks_created"]),
          "re-split reports every previous sprint as already split",
          str(res3["sprint_tasks_created"]))


def test_close_task(wt, migrated, scratch):
    section("11. close_task() still works end to end (reconcile via the wrapper)")
    data = load_copy(wt, migrated, scratch / "close.json")
    sprints = wt.get_cached_sprints(data)
    task = find(data, "Document current FE platform")
    issue = task["github_issue"]
    with Stubs(wt, mode="record", sprints=sprints) as st:
        res = wt.close_task(task, data, wt.save)
    print(f"    result: success={res['success']} split_performed={res['split_performed']} "
          f"issue_closed={res['issue_closed']} project_updated={res['project_updated']} "
          f"error={res['error']}")
    print(f"    split_result.sprint_tasks_created="
          f"{[(e.get('sprint'), e.get('issue_ref'), e.get('skipped'))
              for e in (res['split_result'] or {}).get('sprint_tasks_created', [])]}")
    print(f"    bindings: {[(b['sprint'], b['issue'], b['state'], b['hours_synced'])
                            for b in task['sprint_issues']]}")
    check(res["success"] is True, "close_task succeeded", str(res))
    check(res["split_performed"] is True, "the cross-sprint step ran")
    check(task["status"] == "done", "task marked done")
    check(all("cross_sprint_parent" not in t for t in data["tasks"]),
          "no shadow task objects created")
    check(len(data["tasks"]) == 80, "no tasks added", str(len(data["tasks"])))
    bound = {b["sprint"]: b for b in task["sprint_issues"]}
    # close_task passes closing=True, so no empty binding is reserved for the
    # current sprint (Sprint 105 has no logs). The task's long-lived issue lands
    # on the newest sprint that actually has time instead of reporting 0h against
    # a sprint it was never worked in.
    check(set(bound) == {"Sprint 97", "Sprint 98", "Sprint 104"},
          "a binding per sprint with time, and no empty current-sprint binding",
          str(sorted(bound)))
    last = "Sprint 104"  # newest sprint with logged time
    check(bound[last]["issue"] == issue,
          "the original issue lands on the newest sprint with time (Option A)",
          str(bound[last]))
    check(st.count("create_github_issue") == 1,
          "only Sprint 98 is minted (Sprint 104 took the carried-forward issue)",
          str(st.count("create_github_issue")))
    check(all(b["state"] == "closed" for b in bound.values()),
          "every binding closed (all their sprints have ended)", str(bound))
    # Hours reported on the main issue must be the sprint-filtered value.
    hour_calls = [c for c in st.calls if c[0] == "update_project_hours"]
    print(f"    update_project_hours calls: "
          f"{[(c[1][0], c[1][1]) for c in hour_calls]}")
    main_hours = [c[1][1] for c in hour_calls if c[1][0] == issue]
    expected = wt.mins_to_quarter_hours(
        wt.task_mins_for_sprint(task, bound[last]["sprint_id"], sprints))
    check(all(h == expected for h in main_hours),
          f"main issue only ever told its own sprint's hours ({expected})",
          str(main_hours))
    check(expected > 0,
          "and that value is non-zero — the whole point of closing=True",
          str(expected))
    check(bound[last]["state"] == "closed"
          and bound[last]["hours_synced"] == expected,
          "the current binding's cached state/hours match what close_task did",
          str(bound[last]))
    # A reconcile straight after must not try to re-close or re-push anything.
    with Stubs(wt, mode="strict", sprints=sprints) as st:
        after = wt.reconcile_task_sprints(task, data, sprints, save_callback=wt.save)
        check(st.calls == [], "reconcile after close_task makes no GitHub calls",
              str(st.names()))
    check(after["planned"] == [], "reconcile after close_task plans nothing",
          str([o["op"] for o in after["planned"]]))


def test_set_sprint_drift(wt, migrated, scratch):
    section("10. sprint_id ahead of the bindings (wt set-sprint) mints no duplicate")
    data = load_copy(wt, migrated, scratch / "drift.json")
    sprints = wt.get_cached_sprints(data)
    task = find(data, "Update brokkr to use github app at run time")
    issue = task["github_issue"]
    current = wt.find_sprint_for_date(sprints, datetime.now().date())
    # Simulate `wt set-sprint <task> "Sprint 105"`: sprint_id jumps forward, the
    # binding stays behind on Sprint 101 with the live issue.
    task["sprint_id"] = current["id"]
    task["sprint"] = current["title"]
    with Stubs(wt, mode="strict", sprints=sprints):
        res = wt.reconcile_task_sprints(task, data, sprints, dry_run=True)
    print(f"    bindings: {[(b['sprint'], b['issue'], b['state']) for b in task['sprint_issues']]}")
    print(brief(res))
    creates = [o for o in res["planned"] if o["op"] == "create"]
    repoints = [o for o in res["planned"] if o["op"] == "repoint"]
    check(not any(o["sprint"] == current["title"] for o in creates),
          "no duplicate issue minted for the current sprint", str(creates))
    check(len(repoints) == 1 and repoints[0]["issue"] == issue
          and repoints[0]["sprint"] == current["title"],
          "the live issue is carried forward instead", str(repoints))
    check(not any(o["op"] == "close" and o["issue"] == issue
                  for o in res["planned"]),
          "the live issue is not closed out from under the open task")
    check([o["sprint"] for o in creates] == ["Sprint 101"],
          "the sprint it vacated (65m of real work) gets its own past-sprint issue",
          str([o["sprint"] for o in creates]))


def test_pre_migration(wt, fixture, scratch):
    section("9. reconcile on a pre-migration fixture (load() migrates first)")
    data = load_copy(wt, fixture, scratch / "pre.json")
    check(len(data["tasks"]) == 80 and not any(t.get("cross_sprint_parent")
                                              for t in data["tasks"]),
          "load() migrated 92 -> 80 tasks, 0 shadows",
          f"{len(data['tasks'])} tasks")
    sprints = wt.get_cached_sprints(data)
    task = find(data, "IRON Infusion")
    with Stubs(wt, mode="strict", sprints=sprints):
        res = wt.reconcile_task_sprints(task, data, sprints, dry_run=True)
    check([o for o in res["planned"] if o["op"] in ("create", "repoint")] == [],
          "IRON Infusion still mints nothing straight off the pristine fixture",
          str(res["planned"]))


def test_hours_withheld_guard(wt, migrated, scratch):
    section("12. hours are withheld when some of a task's time has no issue")
    data = load_copy(wt, migrated, scratch / "hold.json")
    sprints = wt.get_cached_sprints(data)

    # Assist on Banco Galicia: 12h30m in Sprint 95 + 6h30m in Sprint 96, one
    # issue. Narrowing that issue to Sprint 96 alone while Sprint 95 has nowhere
    # to go would delete 12h30m from the project's reporting.
    task = find(data, "Assist on Banco Galicia")
    per = {e["sprint_title"]: e["total_mins"] for e in wt.task_sprints_with_time(task, sprints)}
    print(f"    logs by sprint: { {k: round(v) for k, v in per.items()} }")
    check(len(per) > 1, "the fixture task really does span sprints", str(per))

    with Stubs(wt, mode="strict", sprints=sprints):
        held = wt.reconcile_task_sprints(task, data, sprints, dry_run=True,
                                         create_issues=False)
        freed = wt.reconcile_task_sprints(task, data, sprints, dry_run=True,
                                          create_issues=True)

    held_hours = [o for o in held["planned"] if o["op"] == "hours"]
    withheld = [s for s in held["skipped"] if s.get("withheld_hours")]
    check(held_hours == [], "create_issues=False plans no hours write",
          str(held_hours))
    check(len(withheld) == 1, "and reports exactly one withheld hours write",
          str(withheld))
    check([e["sprint"] for e in held["unbillable"]] == ["Sprint 95"],
          "naming the sprint whose time has no issue",
          str(held["unbillable"]))
    # The withheld value must be the *narrowing* one — that's the whole hazard.
    check(withheld and withheld[0]["hours"] < wt.mins_to_quarter_hours(sum(per.values())),
          "the withheld value is lower than the task's full logged total",
          str(withheld[0]["hours"] if withheld else None))
    lines = wt._reconcile_plan_lines(held)
    check(any(l.startswith("HOLD") for l in lines), "plan output shows a HOLD line",
          "\n".join(lines))
    check(any("Add --create-issues" in l for l in lines),
          "and explains how to clear it", "\n".join(lines))

    # With create_issues=True the deferred sprint gets its own issue in the same
    # plan, so nothing is lost and the guard must step aside.
    freed_hours = [o for o in freed["planned"] if o["op"] == "hours"]
    check(freed["unbillable"] == [], "create_issues=True clears unbillable",
          str(freed["unbillable"]))
    check(len(freed_hours) == 1, "and lets the hours write through",
          str(freed_hours))
    check(not any(s.get("withheld_hours") for s in freed["skipped"]),
          "with nothing withheld", str(freed["skipped"]))

    # A single-sprint task has nothing unbillable, so it is unaffected.
    solo = find(data, "Broken New Relic Dashboard")
    with Stubs(wt, mode="strict", sprints=sprints):
        r = wt.reconcile_task_sprints(solo, data, sprints, dry_run=True,
                                      create_issues=False)
    check(r["unbillable"] == [],
          "a single-sprint task is not affected by the guard", str(r["unbillable"]))
    check(any(o["op"] == "hours" for o in r["planned"]),
          "and still syncs its hours", str(r["planned"]))

    # close_task always mints, so the guard must never withhold on a close.
    closing = find(data, "Assist on Banco Galicia")
    with Stubs(wt, mode="strict", sprints=sprints):
        rc = wt.reconcile_task_sprints(closing, data, sprints, dry_run=True,
                                       closing=True)
    check(rc["unbillable"] == [],
          "a close (closing=True, create_issues default) withholds nothing",
          str(rc["unbillable"]))


def main():
    if len(sys.argv) != 5:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    fixture, migrated, baseline, scratch = (Path(a).expanduser() for a in sys.argv[1:])
    scratch.mkdir(parents=True, exist_ok=True)

    # Point WT_DATA_FILE somewhere harmless before importing wt.
    os.environ.setdefault("WT_DATA_FILE", str(scratch / "unused.json"))
    import wt

    real = Path.home() / ".workload_tracker.json"
    if wt._resolve_data_file() == real or wt.DATA_FILE == real:
        print("REFUSING TO RUN: WT_DATA_FILE resolves to the live data file",
              file=sys.stderr)
        return 2

    test_dry_run_purity(wt, migrated, scratch)
    test_historical(wt, migrated, scratch)
    test_already_split(wt, migrated, scratch)
    test_marker_logs(wt, migrated, scratch)
    test_partial_failure(wt, migrated, scratch)
    test_wrapper(wt, migrated, scratch)
    test_close_task(wt, migrated, scratch)
    test_set_sprint_drift(wt, migrated, scratch)
    test_pre_migration(wt, fixture, scratch)
    test_hours_withheld_guard(wt, migrated, scratch)
    test_idempotency(wt, migrated, scratch, baseline)

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  x {f}")
        return 1
    print("All reconcile checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
