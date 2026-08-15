#!/usr/bin/env python3
"""Verification harness for wt_api.py — the command layer (plan Phase 1).

There is no pytest suite in this repo, so this script *is* the test for
``wt_api.py``. Like every other harness here it runs fully offline:

  * ``tools/test_reconcile.py``'s ``Stubs`` monkeypatches every ``gh``-touching
    function in ``wt`` and replaces ``wt.subprocess`` with a guard that raises on
    any attribute access;
  * ``wt_api`` reaches ``gh`` in exactly one place (``verify_issue``) through a
    *function-local* ``import subprocess``, which resolves via ``sys.modules`` at
    call time, so ``sys.modules["subprocess"]`` is swapped for a recording fake
    around those calls;
  * every run happens on a fresh copy in a scratch dir, and the script refuses to
    start if the resolved data file is the live one.

Two things this harness is specifically for:

  1. **``snapshot()`` shape and field completeness** — it is the whole contract
     between Python and Swift, so every field the plan lists is asserted present
     *and* asserted equal to the ``wt`` primitive it claims to come from. A
     silently-missing key would surface as an empty column in the UI, not an
     error.
  2. **Every ``WtError`` code** — the codes are API surface (Phase 2's daemon maps
     them to HTTP statuses; the Swift client localizes them), so each one is
     provoked through a real call rather than asserted to exist. The harness also
     cross-checks ``ERROR_CODES`` against the codes actually raised in the source,
     in both directions, so a new raise or a dead code is loud.

Usage (same 4-argument form as the other harnesses; the pre-migration and
baseline fixtures are accepted and unused, so the documented quick-start
invocation works unchanged):

    WT_DATA_FILE=<scratch>/unused.json \\
    venv/bin/python tools/test_wt_api.py <fixture.json> <migrated.json> \\
                                         <baseline.json> <scratch-dir>

Exit status 0 when every check passes.
"""
import copy
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from test_reconcile import Stubs, multi_sprint_tasks, sig, unreconcile  # noqa: E402
from test_mcp_phase3 import FakeSubprocess, SwapSubprocess  # noqa: E402

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


class ApiStubs(Stubs):
    """``Stubs`` with ``get_all_sprints`` answered from the fixture.

    ``wt_api``'s planners resolve the sprint list themselves via
    ``wt._sprints_for_cli``, which prefers the live fetch. That is a pure read,
    so strict mode should not treat it as a GitHub *write* attempt — the same
    allowance ``McpStubs`` makes in ``tools/test_mcp_phase3.py``. Everything else
    still hard-fails in strict mode.
    """

    def __enter__(self):
        super().__enter__()
        self.wt.get_all_sprints = lambda d, _s=self.sprints: copy.deepcopy(_s)
        return self


def raises(code, fn, *a, **k):
    """Call *fn* and return (ok, detail) for "raised WtError with *code*"."""
    import wt_api
    try:
        fn(*a, **k)
    except wt_api.WtError as e:
        return e.code == code, f"raised {e.code!r} ({e.message})"
    except Exception as e:  # noqa: BLE001
        return False, f"raised {type(e).__name__}: {e}"
    return False, "did not raise"


def expect(code, fn, *a, **k):
    ok, detail = raises(code, fn, *a, **k)
    return check(ok, f"WtError code {code!r}", detail)


# ------------------------------------------------------------------ fixtures --

def fresh(wt, src, dst):
    """Copy *src* to *dst*, point wt at it, and return the loaded data."""
    dst = Path(dst)
    live = Path.home() / ".workload_tracker.json"
    assert dst.name.endswith(".json") and dst != live, dst
    shutil.copyfile(src, dst)
    os.environ["WT_DATA_FILE"] = str(dst)
    wt.DATA_FILE = dst
    return wt.load()


def sprints_of(wt, data):
    return wt.get_cached_sprints(data)


def project_options(wt, data):
    """``config.project_options_cache``, with an empty facet rebuilt.

    The live data has 38 Activity options and **zero** Type options (the Type
    field is unused — docs/plan-macos-app.md §8.1 measured the same thing), and
    ``_check_project_option`` correctly no-ops when a facet's option list is
    empty. So the ``unknown_type`` path is unreachable from the fixture as-is.
    Rather than skip it quietly, seed the missing facet on the scratch copy and
    say so — the alternative is a validation code with no test.
    """
    opts = dict(wt.get_cached_project_options(data) or {})
    seeded = []
    cache = data.setdefault("config", {}).setdefault(
        "project_options_cache", {})
    for key, filler in (("activity", ["Demo Kit Maintenance", "Other"]),
                        ("type", ["Feature", "Bug"])):
        if not opts.get(key):
            cache[key] = list(filler)
            opts[key] = list(filler)
            seeded.append(key)
    if seeded:
        print(f"       (seeded empty project option facet(s) on the scratch "
              f"copy: {', '.join(seeded)})")
    return opts


# -------------------------------------------------------------------- tests ---

def test_module_shape(wt, wt_api):
    section("1. module shape, error-code registry, task_last_logged_at's new home")
    check(wt_api.wt is wt, "wt_api talks to the same wt module object")

    # Every gh-touching call must go through a `wt.<name>` attribute lookup, not
    # a from-import: the harnesses patch module *attributes*, so a from-import
    # would bind a copy and escape to real GitHub.
    src = (REPO / "wt_api.py").read_text()
    from_wt = re.findall(r'^\s*from wt import .*$', src, re.M)
    check(not from_wt, "wt_api never does `from wt import ...`", str(from_wt))

    # ERROR_CODES is API surface. Cross-check it against the source both ways.
    # Everything after the class definition is "the implementation", so the
    # registry tuple itself (declared above it) can't satisfy its own check.
    declared = set(wt_api.ERROR_CODES)
    body = src.split("class WtError", 1)[1]
    raised = set(re.findall(r'"([a-z_]+)"', body)) & declared
    check(raised <= declared, "every code raised in wt_api.py is in ERROR_CODES",
          str(sorted(raised - declared)))
    check(declared <= raised,
          "every code in ERROR_CODES is actually raised somewhere",
          str(sorted(declared - raised)))
    print(f"       ({len(declared)} codes: {', '.join(sorted(declared))})")

    err = wt_api.WtError("invalid_role", "nope", role="x")
    check(err.code == "invalid_role" and err.message == "nope"
          and err.details == {"role": "x"}, "WtError carries code/message/details")
    check(err.as_dict()["error"]["code"] == "invalid_role",
          "WtError.as_dict() is JSON-shaped", str(err.as_dict()))

    # task_last_logged_at moved out of tracker.py; there must be exactly one copy.
    tracker_src = (REPO / "tracker.py").read_text()
    check("def task_last_logged_at" not in tracker_src,
          "tracker.py no longer defines its own task_last_logged_at")
    check("from wt_api import task_last_logged_at" in tracker_src,
          "tracker.py imports it from wt_api instead")
    check(wt_api.task_last_logged_at({"logs": [{"at": 10}, {"at": 50},
                                               {"at": 20}]}) == 50,
          "task_last_logged_at picks the newest `at`")
    check(wt_api.task_last_logged_at({"logs": [{"at": 10},
                                               {"ended_at": 99}]}) == 99,
          "…falling back to ended_at for a legacy entry with no `at`")
    check(wt_api.task_last_logged_at({"logs": [{"started_at": 7}]}) == 7,
          "…then to started_at")
    check(wt_api.task_last_logged_at({"logs": []}) is None,
          "…and None with no logs")


def test_snapshot(wt, wt_api, migrated, scratch):
    section("2. snapshot() — shape, completeness, and no network")
    data = fresh(wt, migrated, scratch / "snap.json")
    sprints = sprints_of(wt, data)

    # Strict mode: any gh call during a snapshot is a hard failure. The whole
    # point is that a UI can refresh without spending GraphQL budget.
    with Stubs(wt, mode="strict", sprints=sprints):
        fake = FakeSubprocess()
        with SwapSubprocess(fake):
            snap = wt_api.snapshot(data)
        check(fake.calls == [], "no subprocess call during snapshot()",
              str(fake.calls[:2]))

    top = {"generated_at", "tasks", "roles", "sprints", "current_sprint",
           "active_timer", "project_options", "config"}
    check(top <= set(snap), "snapshot has every documented top-level key",
          str(sorted(top - set(snap))))

    try:
        blob = json.dumps(snap)
        ok = True
    except TypeError as e:
        ok, blob = False, str(e)
    check(ok, "snapshot is JSON-serializable (no date/datetime leaks)", str(blob)[:200])
    if ok:
        print(f"       ({len(blob)} bytes for {len(snap['tasks'])} tasks)")

    check(len(snap["tasks"]) == len(data["tasks"]),
          f"one entry per task ({len(data['tasks'])})", str(len(snap["tasks"])))

    want_task_keys = {
        "id", "title", "description", "status", "status_label", "role_id",
        "created_at", "activity", "github_repo", "type", "sprints_with_time",
        "start_sprint", "start_sprint_id", "sprint_issues", "current_issue",
        "logged_mins", "live_mins", "reportable_mins", "last_logged_at", "logs",
        "local_folder",
    }
    missing = [k for t in snap["tasks"] for k in want_task_keys if k not in t]
    check(not missing, "every task carries every documented field",
          str(sorted(set(missing))))

    # The Safari task-window integration is gone: task_view must not resurrect
    # its fields, or a client will render affordances for a feature that no
    # longer has a code path behind it.
    gone = [k for t in snap["tasks"] for k in ("tabs", "active_window_id") if k in t]
    check(not gone, "…and none of the removed Safari fields",
          str(sorted(set(gone))))

    # The raw legacy key must never appear: a task has one issue per sprint and
    # github_issue is a mirror of the current one.
    leaked = [t["id"] for t in snap["tasks"] if "github_issue" in t]
    check(not leaked, "no raw `github_issue` key in a task view", str(leaked[:3]))

    by_id = {t["id"]: t for t in snap["tasks"]}
    bad_issue, bad_hours, bad_report, bad_last, bad_logs = [], [], [], [], []
    for task in data["tasks"]:
        view = by_id[task["id"]]
        if view["current_issue"] != wt.task_current_issue(task, data):
            bad_issue.append(task["id"])
        if abs(view["logged_mins"] - wt.task_logged_mins(task)) > 1e-9:
            bad_hours.append(task["id"])
        if abs(view["reportable_mins"]
               - wt.task_reportable_mins(task, sprints)) > 1e-9:
            bad_report.append(task["id"])
        if view["last_logged_at"] != wt_api.task_last_logged_at(task):
            bad_last.append(task["id"])
        if len(view["logs"]) != len(task.get("logs", [])):
            bad_logs.append(task["id"])
    check(not bad_issue, "current_issue == task_current_issue() for every task",
          str(bad_issue[:3]))
    check(not bad_hours, "logged_mins == task_logged_mins() for every task",
          str(bad_hours[:3]))
    check(not bad_report,
          "reportable_mins == task_reportable_mins(task, sprints) for every task",
          str(bad_report[:3]))
    check(not bad_last, "last_logged_at == task_last_logged_at() for every task",
          str(bad_last[:3]))
    check(not bad_logs, "the full logs array is carried", str(bad_logs[:3]))

    # sprints_with_time: straight from task_sprints_with_time, minus the bulky
    # per-entry logs key (the task's logs are already sent once, above).
    bad_swt, with_logs = [], []
    n_entries = 0
    for task in data["tasks"]:
        want = wt.task_sprints_with_time(task, sprints)
        got = by_id[task["id"]]["sprints_with_time"]
        n_entries += len(got)
        if len(want) != len(got):
            bad_swt.append(task["id"])
            continue
        for w, g in zip(want, got):
            if (w["sprint_id"] != g["sprint_id"]
                    or w["sprint_title"] != g["sprint_title"]
                    or abs(w["total_mins"] - g["total_mins"]) > 1e-9):
                bad_swt.append(task["id"])
            if "logs" in g:
                with_logs.append(task["id"])
    check(not bad_swt, "sprints_with_time matches task_sprints_with_time()",
          str(bad_swt[:3]))
    check(not with_logs, "…with the bulky `logs` key stripped from each entry",
          str(with_logs[:3]))
    multi = sum(1 for t in snap["tasks"] if len(t["sprints_with_time"]) > 1)
    print(f"       ({n_entries} sprint-time entries; {multi} tasks span 2+ sprints)")
    check(multi > 0, "the fixture really has cross-sprint tasks to describe",
          "every task is single-sprint — the facet assertion would be vacuous")

    # Top level.
    check([r["id"] for r in snap["roles"]] == [r["id"] for r in data["roles"]],
          "roles in file order")
    check(all("color" in r for r in snap["roles"]), "roles carry their colour")
    check(snap["sprints"] == data["config"]["sprints_cache"],
          "sprints == the persisted config.sprints_cache",
          f"{len(snap['sprints'])} vs {len(data['config']['sprints_cache'])}")
    cur = wt.find_sprint_for_date(sprints, datetime.now().date())
    check((snap["current_sprint"] or {}).get("id") == (cur or {}).get("id"),
          f"current_sprint resolved offline ({cur and cur['title']})",
          str(snap["current_sprint"]))
    check(snap["project_options"] == (wt.get_cached_project_options(data) or {}),
          "project_options == config.project_options_cache (editor pickers)")
    check(isinstance(snap["project_options"].get("activity"), list),
          "…and it really has an activity option list",
          str(list(snap["project_options"])))

    # active_timer carries a raw epoch so a client ticks locally.
    data["active_timer"] = {"task_id": data["tasks"][0]["id"], "started_at": 1234.5}
    snap2 = wt_api.snapshot(data)
    check(snap2["active_timer"] == {"task_id": data["tasks"][0]["id"],
                                    "started_at": 1234.5},
          "active_timer carries the raw epoch started_at",
          str(snap2["active_timer"]))
    live = next(t for t in snap2["tasks"] if t["id"] == data["tasks"][0]["id"])
    check(live["live_mins"] > 0, "…and the running task reports live_mins",
          str(live["live_mins"]))
    data["active_timer"] = None
    check(wt_api.snapshot(data)["active_timer"] is None, "idle -> active_timer null")

    # Purity: a snapshot must not mutate the data it renders.
    before = sig(data)
    wt_api.snapshot(data)
    check(sig(data) == before, "snapshot() mutates nothing")


def test_resolution_errors(wt, wt_api, migrated, scratch):
    section("3. WtError: task resolution")
    data = fresh(wt, migrated, scratch / "resolve.json")
    task = data["tasks"][0]
    check(wt_api.require_task(data, task["id"]) is task, "resolves by exact id")
    expect("task_not_found", wt_api.require_task, data, "no-such-task-xyz")

    # Ambiguity is a distinct code here, even though every MCP tool collapses it
    # back into "No task found" — the daemon and the app want to tell them apart.
    data["tasks"].append({"id": "amb1", "title": "harness ambiguous alpha",
                          "role_id": "other", "status": "todo", "logs": []})
    data["tasks"].append({"id": "amb2", "title": "harness ambiguous beta",
                          "role_id": "other", "status": "todo", "logs": []})
    expect("ambiguous_task", wt_api.require_task, data, "harness ambiguous")
    check(wt_api.resolve_task(data, "harness ambiguous") is None,
          "resolve_task() still returns None on an ambiguous match "
          "(mcp_server's contract)")
    data["tasks"] = [t for t in data["tasks"] if t["id"] not in ("amb1", "amb2")]


def test_validation_errors(wt, wt_api, migrated, scratch):
    section("4. WtError: field validation")
    data = fresh(wt, migrated, scratch / "validate.json")
    sprints = sprints_of(wt, data)
    tid = data["tasks"][0]["id"]
    n_tasks = len(data["tasks"])

    with ApiStubs(wt, mode="record", sprints=sprints):
        expect("invalid_role", wt_api.create_task, data,
               title="x", role="no-such-role")
        expect("invalid_status", wt_api.create_task, data,
               title="x", status="halfway")
        expect("invalid_repo", wt_api.create_task, data,
               title="x", github_repo="not-a-repo")
        expect("invalid_repo", wt_api.set_task_repo, data, tid, "a/b/c")

        opts = project_options(wt, data)
        check(bool(opts.get("activity")) and bool(opts.get("type")),
              "there are cached project options to validate against",
              str({k: len(v) for k, v in opts.items()}))
        expect("unknown_activity", wt_api.create_task, data,
               title="x", activity="Definitely Not An Activity")
        expect("unknown_type", wt_api.create_task, data,
               title="x", type="Definitely Not A Type")
        expect("unknown_activity", wt_api.set_task_activity, data, tid, "nope")
        expect("unknown_type", wt_api.set_task_type, data, tid, "nope")
        expect("sprint_not_found", wt_api.create_task, data,
               title="x", sprint="Sprint 99999")
        expect("sprint_not_found", wt_api.set_start_sprint, data, tid,
               "Sprint 99999")
        expect("invalid_args", wt_api.update_task, data, tid, nonsense="x")
        expect("invalid_args", wt_api.update_task, data, tid, title="")

    check(len(data["tasks"]) == n_tasks,
          "no half-created task survived a rejected create_task",
          f"{len(data['tasks'])} vs {n_tasks}")

    # no_sprints: an empty cache and a gh that answers with nothing.
    blank = copy.deepcopy(data)
    blank["config"]["sprints_cache"] = []
    with ApiStubs(wt, mode="record", sprints=[]):
        expect("no_sprints", wt_api.set_start_sprint, blank, tid, "Sprint 1")
        expect("no_sprints", wt_api.plan_reconcile, blank, task_id=tid)
        expect("no_sprints", wt_api.reconcile, blank, tid)

    expect("invalid_args", wt_api.plan_reconcile, data)
    expect("invalid_args", wt_api.plan_reconcile, data, task_id=tid,
           all_tasks=True)
    expect("invalid_args", wt_api.close, data, tid, save_callback=None)


def test_log_errors_and_ops(wt, wt_api, migrated, scratch):
    section("5. logs: add / edit / delete / split / merge, and their error codes")
    data = fresh(wt, migrated, scratch / "logs.json")
    task = max(data["tasks"], key=lambda t: len(t.get("logs", [])))
    tid = task["id"]
    n_before = len(task["logs"])
    mins_before = wt.task_logged_mins(task)
    print(f"       subject: {task['title'][:44]!r} ({n_before} logs, "
          f"{mins_before:.1f}m)")

    expect("invalid_minutes", wt_api.add_log, data, tid, 0)
    expect("invalid_minutes", wt_api.add_log, data, tid, -5)
    expect("log_not_found", wt_api.edit_log, data, tid, "zzzz", minutes=1)
    expect("log_not_found", wt_api.delete_log, data, tid, "zzzz")
    expect("log_not_found", wt_api.split_log, data, tid, "zzzz", 5)
    expect("no_changes", wt_api.edit_log, data, tid,
           task["logs"][0]["id"])

    res = wt_api.add_log(data, tid, 45.5, note="harness manual",
                         started_at=1000.0, ended_at=3730.0)
    log = res["log"]
    check(len(task["logs"]) == n_before + 1, "add_log appended one entry")
    check(log["minutes"] == 45.5 and log["note"] == "harness manual"
          and log["started_at"] == 1000.0 and log["ended_at"] == 3730.0
          and log["at"] == 3730.0,
          "…with minutes, note and both timestamps", str(log))

    # The prefix has to be one that *only* this log answers to. `uid()` is
    # yyyymmddHHMMSS + 4 letters, so a fixed [:8] slice is just today's date and
    # collides with every log the owner recorded today — edit_log then silently
    # edited a different entry and three checks downstream failed. Derive the
    # shortest unique prefix instead, and assert it really is a prefix rather
    # than the whole id.
    other_ids = [l["id"] for t in data["tasks"] for l in t.get("logs", [])
                 if l["id"] != log["id"]]
    prefix = next((log["id"][:n] for n in range(4, len(log["id"]))
                   if not any(o.startswith(log["id"][:n]) for o in other_ids)),
                  log["id"])
    check(len(prefix) < len(log["id"]),
          "a strict id prefix is unambiguous in this fixture", prefix)
    wt_api.edit_log(data, tid, prefix, minutes=60.0, note="harness edited")
    check(log["minutes"] == 60.0 and log["note"] == "harness edited",
          "edit_log accepts an id prefix and changes both fields", str(log))

    split = wt_api.split_log(data, tid, log["id"], 25.0)
    first, second = split["first"], split["second"]
    check(abs(first["minutes"] - 25.0) < 1e-9
          and abs(second["minutes"] - 35.0) < 1e-9,
          "split_log divides the minutes", f"{first['minutes']}/{second['minutes']}")
    check(first["ended_at"] == second["started_at"],
          "…and the timestamps meet in the middle",
          f"{first['ended_at']} vs {second['started_at']}")
    check(first["started_at"] == 1000.0 and second["ended_at"] == 3730.0,
          "…while the outer bounds are preserved")
    expect("invalid_split", wt_api.split_log, data, tid, first["id"], 0)
    expect("invalid_split", wt_api.split_log, data, tid, first["id"], 25)

    merged = wt_api.merge_logs(data, tid, first["id"], second["id"])["merged"]
    check(abs(merged["minutes"] - 60.0) < 1e-9, "merge_logs sums the minutes",
          str(merged["minutes"]))
    check(merged["started_at"] == 1000.0 and merged["ended_at"] == 3730.0,
          "…earliest start, latest end", str(merged))
    check(merged["note"].startswith("Merged: "), "…and concatenates the notes",
          merged["note"])
    expect("same_log", wt_api.merge_logs, data, tid, merged["id"], merged["id"])

    wt_api.delete_log(data, tid, merged["id"])
    check(len(task["logs"]) == n_before, "delete_log restores the original count",
          f"{len(task['logs'])} vs {n_before}")
    check(abs(wt.task_logged_mins(task) - mins_before) < 1e-9,
          "…and the task's minutes are back where they started",
          f"{wt.task_logged_mins(task)} vs {mins_before}")


def test_timers(wt, wt_api, migrated, scratch):
    section("6. timers: start / stop / switch")
    data = fresh(wt, migrated, scratch / "timers.json")
    a, b = data["tasks"][0], data["tasks"][1]
    n_a = len(a.get("logs", []))

    # The idle precondition is constructed: a fixture copied while the owner had
    # a timer running carries it, and stop_timer then legitimately succeeds. Drop
    # the timer *without* committing it, so no log is invented on a fixture task.
    data["active_timer"] = None
    expect("no_active_timer", wt_api.stop_timer, data)

    # No browser argument any more: starting or stopping a timer has no desktop
    # side effects since the Safari task-window integration was removed.
    res = wt_api.start_timer(data, a["id"])
    check(data["active_timer"]["task_id"] == a["id"], "start_timer sets the timer")
    check(res["stopped"] is None, "…with nothing stopped when idle")

    data["active_timer"]["started_at"] -= 600  # pretend 10 minutes elapsed
    res2 = wt_api.start_timer(data, b["id"])
    check(data["active_timer"]["task_id"] == b["id"], "switching re-points the timer")
    check(res2["stopped"] and res2["stopped"]["task_id"] == a["id"],
          "…and commits the previous task's session", str(res2["stopped"]))
    check(len(a["logs"]) == n_a + 1, "…as one appended log", str(len(a["logs"])))
    logged = a["logs"][-1]
    check(logged["note"] == "Timer session" and 9.5 < logged["minutes"] < 10.5,
          "…a ~10 minute 'Timer session' entry", str(logged))
    check(logged["started_at"] and logged["ended_at"],
          "…carrying both wall-clock timestamps")

    n_b = len(b.get("logs", []))
    stop = wt_api.stop_timer(data)
    check(data["active_timer"] is None, "stop_timer clears the timer")
    check(stop["logged"] is False and len(b.get("logs", [])) == n_b,
          "a sub-3-second session is discarded, not logged",
          f"logged={stop['logged']} logs={len(b.get('logs', []))}")

    expect("task_not_found", wt_api.start_timer, data, "nope-xyz")


def test_task_commands(wt, wt_api, migrated, scratch):
    section("7. create / update / set_task_* / list_tasks / status_overview")
    data = fresh(wt, migrated, scratch / "tasks.json")
    sprints = sprints_of(wt, data)
    n_before = len(data["tasks"])
    opts = project_options(wt, data)

    with ApiStubs(wt, mode="record", sprints=sprints):
        res = wt_api.create_task(data, title="harness api task", role="other",
                                 github_repo="grafana/field-eng",
                                 activity=opts["activity"][0])
    task = res["task"]
    check(len(data["tasks"]) == n_before + 1 and data["tasks"][0] is task,
          "create_task inserts at the head")
    check(task["github_repo"] == "grafana/field-eng"
          and task["activity"] == opts["activity"][0],
          "…with the per-task GitHub fields set", str(task.get("activity")))
    cur = wt.find_sprint_for_date(sprints, datetime.now().date())
    check(task.get("sprint") == (cur or {}).get("title"),
          "…and the current sprint auto-assigned (legacy mirror)",
          f"{task.get('sprint')} vs {cur and cur['title']}")

    with ApiStubs(wt, mode="record", sprints=sprints):
        none_task = wt_api.create_task(data, title="harness no sprint",
                                       sprint="none")["task"]
    check("sprint" not in none_task and "sprint_id" not in none_task,
          'sprint="none" assigns none', str(none_task.get("sprint")))

    upd = wt_api.update_task(data, task["id"], description="hello",
                             local_folder="/tmp/x")
    check(task["description"] == "hello" and task["local_folder"] == "/tmp/x",
          "update_task patches fields", str(upd["changed"]))
    wt_api.update_task(data, task["id"], local_folder=None)
    check("local_folder" not in task, "…and None clears an optional field")

    wt_api.set_task_activity(data, task["id"], None)
    check("activity" not in task, "set_task_activity(None) clears it")
    wt_api.set_task_type(data, task["id"], opts["type"][0])
    check(task["type"] == opts["type"][0], "set_task_type sets a cached option")
    cleared = wt_api.set_task_repo(data, task["id"], None)
    check(cleared["cleared"] and "github_repo" not in task,
          "set_task_repo(None) clears it")

    # list_tasks filters
    all_rows = wt_api.list_tasks(data, include_done=True)
    n_done = sum(1 for t in data["tasks"] if t.get("status") == "done")
    check(len(all_rows) == len(data["tasks"]), "include_done=True lists everything",
          str(len(all_rows)))
    check(len(wt_api.list_tasks(data)) == len(data["tasks"]) - n_done,
          "done tasks are hidden by default")
    check(len(wt_api.list_tasks(data, status="done")) == n_done,
          'status="done" wins over the default hide')
    check(wt_api.list_tasks(data, role="no-such-role") == [],
          "an unknown role filters everything out")

    # status_overview totals logged minutes *plus* the running timer's elapsed
    # time, so the expectation has to include the live term — a fixture copied
    # while a timer was running otherwise reads as a double-count that is not
    # one. The no-double-count claim itself is asserted on a timer-less copy,
    # where the total must equal the logged sum to the microminute.
    ov = wt_api.status_overview(data)
    check(ov["n_tasks"] == len(data["tasks"]), "status_overview counts every task")
    logged = sum(wt.task_logged_mins(t) for t in data["tasks"])
    live = sum(wt_api.task_live_mins(t, data.get("active_timer"))
               for t in data["tasks"])
    check(abs(ov["total_mins"] - (logged + live)) < 1e-3,
          "…and totals logged + running-timer minutes",
          f"{ov['total_mins']} vs {logged} + {live}")
    idle = copy.deepcopy(data)
    idle["active_timer"] = None
    check(abs(wt_api.status_overview(idle)["total_mins"] - logged) < 1e-6,
          "…counting each task's logs exactly once (no shadow double-count)",
          f"{wt_api.status_overview(idle)['total_mins']} vs {logged}")
    check({r["role_id"] for r in ov["by_role"]}
          == {r["id"] for r in data["roles"]}, "…broken down by every role")

    # rename without touching GitHub
    plain = next(t for t in data["tasks"] if not wt.task_current_issue(t, data))
    rn = wt_api.rename_task(data, plain["id"], "harness renamed")
    check(plain["title"] == "harness renamed" and not rn["issue_updated"],
          "rename_task with no linked issue makes no gh call", str(rn))

    # delete_task names past-sprint issues instead of destroying them
    multi = next(t for t in data["tasks"]
                 if len([b for b in (t.get("sprint_issues") or [])
                         if b.get("issue")]) > 2)
    n = len(data["tasks"])
    with ApiStubs(wt, mode="record", sprints=sprints) as st:
        dl = wt_api.delete_task(data, multi["id"])
    check(len(data["tasks"]) == n - 1, "delete_task removes the task")
    check(st.count("delete_github_issue") == 1,
          "…deleting exactly one issue (the current binding's)",
          str(st.count("delete_github_issue")))
    check(len(dl["other_issues"]) >= 2, "…and naming the past-sprint ones",
          str(dl["other_issues"]))


def test_github_paths(wt, wt_api, migrated, scratch):
    section("8. GitHub: normalize / verify / link / unlink / push / ensure_issue")
    data = fresh(wt, migrated, scratch / "github.json")
    sprints = sprints_of(wt, data)

    check(wt_api.normalize_issue_ref(
        data, "https://github.com/grafana/field-eng/issues/7") ==
        "grafana/field-eng#7", "normalize_issue_ref handles a URL")
    check(wt_api.normalize_issue_ref(data, "grafana/field-eng#7") ==
          "grafana/field-eng#7", "…passes owner/repo#n through")
    data.setdefault("config", {})["github_repo"] = "grafana/field-eng"
    check(wt_api.normalize_issue_ref(data, "42") == "grafana/field-eng#42",
          "…expands a bare number with the default repo")
    del data["config"]["github_repo"]
    expect("no_default_repo", wt_api.normalize_issue_ref, data, "42")

    task = next(t for t in data["tasks"] if not wt.task_current_issue(t, data))
    tid = task["id"]
    expect("not_linked", wt_api.unlink_issue, data, tid)
    expect("not_linked", wt_api.push_to_github, data, tid)

    # issue_not_found: gh answers non-zero.
    with ApiStubs(wt, mode="record", sprints=sprints):
        fake = FakeSubprocess({"gh issue view": (1, "")})
        with SwapSubprocess(fake):
            expect("issue_not_found", wt_api.link_issue, data, tid,
                   "grafana/field-eng#1")
        check(not fake.writes(), "a failed link makes no gh write call",
              str(fake.writes()))
    check(wt.task_current_issue(task, data) is None,
          "…and leaves the task unlinked")

    # Most real tasks already carry a github_repo, so clear it first — otherwise
    # the repo-pinning branch is never taken and the assertion is vacuous.
    task.pop("github_repo", None)
    with ApiStubs(wt, mode="record", sprints=sprints):
        fake = FakeSubprocess({"gh issue view": (0, json.dumps(
            {"number": 4242, "title": "stub issue"}))})
        with SwapSubprocess(fake):
            linked = wt_api.link_issue(data, tid, "grafana/field-eng#4242")
    check(wt.task_current_issue(task, data) == "grafana/field-eng#4242",
          "link_issue binds the ref")
    check(any(b.get("issue") == "grafana/field-eng#4242"
              for b in task.get("sprint_issues") or []),
          "…on a binding, not just the flat key", str(task.get("sprint_issues")))
    check(task.get("github_repo") == "grafana/field-eng" and linked["repo_pinned"],
          "…pinning github_repo from the ref so the close workflow engages",
          f"{task.get('github_repo')} pinned={linked['repo_pinned']}")
    check(linked["issue_info"]["number"] == 4242, "…and returning the issue info")

    un = wt_api.unlink_issue(data, tid)
    check(un["old_issue"] == "grafana/field-eng#4242", "unlink_issue returns the ref")
    check(wt.task_current_issue(task, data) is None, "…and clears it")

    # push: the subject must be a task whose sprint-filtered hours really differ
    # from its total, or "not the total" is not an assertion. They must also be
    # **non-zero**: wt.sync_project_hours only writes Hours when the sprint has
    # minutes in it, so a subject whose current sprint is empty (a perpetual task
    # on the first day of a new sprint, say) sends nothing at all and the
    # "value actually sent" check has no value to compare.
    def sprint_h(t):
        return wt.mins_to_quarter_hours(wt.task_reportable_mins(t, sprints))

    pushable = [t for t in data["tasks"]
                if wt.task_current_issue(t, data)
                and len(wt.task_sprints_with_time(t, sprints)) > 1
                and sprint_h(t) > 0
                and abs(sprint_h(t)
                        - wt.mins_to_quarter_hours(wt.task_logged_mins(t))) > 1e-9]
    if not pushable:
        raise SystemExit("no linked cross-sprint task with non-zero sprint hours "
                         "differing from its total — the push assertion would be "
                         "vacuous")
    ptask = max(pushable, key=lambda t: len(wt.task_sprints_with_time(t, sprints)))
    want_h = wt.mins_to_quarter_hours(wt.task_reportable_mins(ptask, sprints))
    total_h = wt.mins_to_quarter_hours(wt.task_logged_mins(ptask))
    # setup_issue_in_project is not itself a GH_FUNCS stub — it orchestrates
    # several that are — so run it with the subprocess fake installed too.
    with ApiStubs(wt, mode="record", sprints=sprints) as st:
        fake = FakeSubprocess()
        with SwapSubprocess(fake):
            pushed = wt_api.push_to_github(data, ptask["id"])
        check(fake.calls == [], "push made no raw gh subprocess call (all via wt)",
              str(fake.calls[:2]))
    check(abs(pushed["hours"] - want_h) < 1e-9,
          "push_to_github reports the SPRINT-filtered hours, not the total",
          f"{pushed['hours']} vs sprint={want_h} total={total_h}")
    print(f"       pushed {pushed['hours']}h for {ptask['title'][:38]!r} "
          f"(task total would be {total_h}h)")
    sent = [a[1] for n, a, k in st.calls if n == "update_project_hours"]
    check(sent and abs(sent[-1] - want_h) < 1e-9,
          "…and the value actually sent to GitHub matches", str(sent))

    # ensure_issue: no_repo, then a real mint, then a github_failed.
    bare = wt_api.create_task(data, title="harness repo-less", sprint="none")["task"]
    expect("no_repo", wt_api.ensure_issue, data, bare["id"])
    bare["github_repo"] = "grafana/field-eng"
    with ApiStubs(wt, mode="record", sprints=sprints) as st:
        made = wt_api.ensure_issue(data, bare["id"])
    check(made["created"] and made["issue"].startswith("grafana/field-eng#"),
          "ensure_issue mints one when the task has a repo", str(made))
    check(wt.task_current_issue(bare, data) == made["issue"],
          "…and records it as a binding")
    with ApiStubs(wt, mode="record", sprints=sprints):
        again = wt_api.ensure_issue(data, bare["id"])
    check(not again["created"] and again["issue"] == made["issue"],
          "…and is a no-op the second time", str(again))

    bare2 = wt_api.create_task(data, title="harness boom", sprint="none")["task"]
    bare2["github_repo"] = "grafana/field-eng"
    with ApiStubs(wt, mode="record", sprints=sprints,
               fail_on_create={"harness boom"}):
        expect("github_failed", wt_api.ensure_issue, data, bare2["id"])
    check(wt.task_current_issue(bare2, data) is None,
          "…and a failed mint leaves the task unlinked")


def test_close_and_reconcile(wt, wt_api, migrated, scratch):
    section("9. plan_close / close / plan_reconcile / apply_reconcile")
    data = fresh(wt, migrated, scratch / "close.json")
    sprints = sprints_of(wt, data)
    saves = []

    def save(d):
        saves.append(1)
        wt.save(d)

    # Subject picked from the fixture, never pinned by title: an open,
    # repo-having, already-linked task with time in several sprints and at least
    # one of them unbound, so the close really has reconcile work to do.
    def unbound(t):
        bound = {b.get("sprint_id") for b in t.get("sprint_issues") or []}
        return len([e for e in wt.task_sprints_with_time(t, sprints)
                    if e["sprint_id"] not in bound])

    candidates = [t for t in data["tasks"]
                  if t.get("status") == "inprogress" and t.get("github_repo")
                  and wt.task_current_issue(t, data)
                  and len(wt.task_sprints_with_time(t, sprints)) > 1]
    if not candidates:
        raise SystemExit("no open, linked, multi-sprint task in the fixture — "
                         "section 9 cannot test what its name says")
    subject = max(candidates, key=unbound)

    # plan_close is write-free by construction: reconcile plans, then returns.
    before = sig(data)
    with ApiStubs(wt, mode="strict", sprints=sprints):
        plan = wt_api.plan_close(data, subject["id"], sprints)
    check(sig(data) == before, "plan_close mutates nothing")
    check(plan["title"] == subject["title"] and "plan" in plan,
          "plan_close returns the reconcile plan", str(list(plan)))
    check(isinstance(plan["plan_lines"], list),
          "…plus renderable plan lines", str(plan["plan_lines"][:2]))
    check(plan["will_create_issues"] == sum(
        1 for op in plan["plan"]["planned"]
        if op["op"] == "create" and op.get("create_issue")),
        "…and an accurate 'issues that would be created' count",
        str(plan["will_create_issues"]))
    print(f"       subject={subject['title'][:44]!r} "
          f"would create {plan['will_create_issues']} issue(s)")

    # A real close, fully stubbed.
    with ApiStubs(wt, mode="record", sprints=sprints) as st:
        res = wt_api.close(data, subject["id"], create_issue=True,
                           save_callback=save)
    check(res["success"], "close() succeeded", str(res.get("error")))
    check(subject["status"] == "done", "…and the task is done", subject["status"])
    check(res["current_issue"] and res["title"] == subject["title"],
          "…reporting the current issue and title", str(res["current_issue"]))
    check(isinstance(res["outcome_lines"], list) ,
          "…with renderable outcome lines", str(res["outcome_lines"][:2]))
    check(st.count("create_github_issue") == plan["will_create_issues"],
          "close() minted exactly the number plan_close predicted",
          f"{st.count('create_github_issue')} vs {plan['will_create_issues']}")
    check(saves, "…and used the caller's save_callback", str(len(saves)))
    bad = []
    for b in subject["sprint_issues"]:
        want = wt.mins_to_quarter_hours(
            wt.task_mins_for_sprint(subject, b.get("sprint_id"), sprints))
        if b.get("hours_synced") is None or abs(b["hours_synced"] - want) > 1e-9:
            bad.append((b.get("sprint"), b.get("hours_synced"), want))
    check(not bad, "each binding carries its own sprint's hours, not the total",
          str(bad))

    # raise_on_failure maps the two failure kinds to distinct codes.
    ok, detail = raises("close_failed", wt_api.raise_on_failure,
                        {"success": False, "error": "Task must have GitHub issue"})
    check(ok, "raise_on_failure -> close_failed", detail)
    ok, detail = raises("reconcile_failed", wt_api.raise_on_failure,
                        {"success": False,
                         "error": "Sprint reconcile failed: boom"})
    check(ok, "…and -> reconcile_failed for an aborted reconcile", detail)
    check(wt_api.raise_on_failure({"success": True})["success"],
          "…and passes a success through")

    # plan_reconcile: the all_tasks default must not plan any issue creation.
    # A fully-reconciled fixture plans nothing either way, which would make the
    # opt-in half of the comparison pass for the wrong reason, so the work is
    # constructed: roll the cross-sprint tasks back to the one-issue shape
    # (in memory only — the disk copy stays pristine for the "writes nothing"
    # assertions below).
    data2 = fresh(wt, migrated, scratch / "recon.json")
    sprints2 = sprints_of(wt, data2)
    rolled = 0
    for t, _per in multi_sprint_tasks(wt, data2, sprints2):
        unreconcile(wt, t, sprints2)
        rolled += 1
    if not rolled:
        raise SystemExit("fixture has no cross-sprint task to un-reconcile — "
                         "the create_issues opt-in comparison would be vacuous")
    disk = Path(scratch / "recon.json").read_text()
    with ApiStubs(wt, mode="strict", sprints=sprints2):
        blanket = wt_api.plan_reconcile(data2, all_tasks=True)
    check(blanket["create_issues"] is False,
          "plan_reconcile(all_tasks=True) defaults create_issues to False")
    check(blanket["totals"]["create"] == 0,
          "…so it plans zero issue creations", str(blanket["totals"]))
    check(Path(scratch / "recon.json").read_text() == disk,
          "…and writes nothing")
    with ApiStubs(wt, mode="strict", sprints=sprints2):
        opted = wt_api.plan_reconcile(data2, all_tasks=True, create_issues=True)
    check(opted["totals"]["create"] > 0,
          "create_issues=True really would mint issues (so the default matters)",
          str(opted["totals"]))
    print(f"       opt-in would create {opted['totals']['create']} issue(s)")

    # A single-task dry run makes no GitHub calls at all.
    one = next(e["task"] for e in blanket["plans"])
    with ApiStubs(wt, mode="strict", sprints=sprints2):
        dry = wt_api.reconcile(data2, one["id"], dry_run=True)
    check(dry["dry_run"] and isinstance(dry["plan_lines"], list),
          "reconcile(dry_run=True) returns a plan", str(dry["plan_lines"][:1]))
    check(Path(scratch / "recon.json").read_text() == disk,
          "…and still writes nothing")

    # apply_reconcile executes, and a re-plan afterwards is empty (idempotent).
    with ApiStubs(wt, mode="record", sprints=sprints2):
        single = wt_api.plan_reconcile(data2, task_id=one["id"])
        applied = wt_api.apply_reconcile(data2, single, save_callback=wt.save)
    check(applied and all(e["success"] for e in applied),
          "apply_reconcile succeeded", str([e["success"] for e in applied]))
    with ApiStubs(wt, mode="strict", sprints=sprints2):
        again = wt_api.plan_reconcile(data2, task_id=one["id"])
    check(not again["plans"], "…and a second plan is empty (idempotent)",
          str(again["totals"]))


def test_no_gh_escaped():
    """Belt and braces, when the caller put a logging fake ``gh`` first on PATH.

    Set ``WT_FAKE_GH_LOG`` to that fake's log file and this asserts it stayed
    empty. Unset, it prints a note rather than a check that cannot fail.
    """
    section("10. belt and braces: no real gh invocation")
    fake_log = os.environ.get("WT_FAKE_GH_LOG")
    if not fake_log:
        print("       (WT_FAKE_GH_LOG unset — skipping; every gh path above is "
              "already stubbed and guarded)")
        return
    path = Path(fake_log)
    lines = [l for l in path.read_text().splitlines() if l.strip()] \
        if path.exists() else []
    check(not lines, f"the fake gh on PATH logged nothing ({fake_log})",
          "\n      ".join(lines[:5]))


def main():
    if len(sys.argv) != 5:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    _fixture, migrated, _baseline, scratch = (
        Path(a).expanduser() for a in sys.argv[1:])
    scratch.mkdir(parents=True, exist_ok=True)

    os.environ["WT_DATA_FILE"] = str(scratch / "unused.json")
    import wt
    import wt_api

    live = Path.home() / ".workload_tracker.json"
    if wt.DATA_FILE == live:
        print("REFUSING TO RUN: wt.DATA_FILE is the live file", file=sys.stderr)
        return 2

    if not json.loads(Path(migrated).read_text()).get(
            "config", {}).get("sprints_cache"):
        print("REFUSING TO RUN: the fixture has no config.sprints_cache, so "
              "every sprint lookup would need the network", file=sys.stderr)
        return 2

    test_module_shape(wt, wt_api)
    test_snapshot(wt, wt_api, migrated, scratch)
    test_resolution_errors(wt, wt_api, migrated, scratch)
    test_validation_errors(wt, wt_api, migrated, scratch)
    test_log_errors_and_ops(wt, wt_api, migrated, scratch)
    test_timers(wt, wt_api, migrated, scratch)
    test_task_commands(wt, wt_api, migrated, scratch)
    test_github_paths(wt, wt_api, migrated, scratch)
    test_close_and_reconcile(wt, wt_api, migrated, scratch)
    test_no_gh_escaped()

    total = CHECKS
    print(f"\n{total - len(FAILURES)}/{total} checks passed")
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  x {f}")
        return 1
    print("All wt_api checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
