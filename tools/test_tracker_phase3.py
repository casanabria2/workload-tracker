#!/usr/bin/env python3
"""Verification harness for Phase 3 of docs/plan-sprint-bindings.md — tracker.py.

There is no automated test suite in this repo, so this script *is* the test for
the TUI half of Phase 3. It runs fully offline and cannot touch anything real:

  * ``HOME`` is redirected to a scratch directory holding a **copy** of the data
    file, so ``Path.home() / ".workload_tracker.json"`` (which tracker.py
    resolves at import time) can never be the live iCloud-synced file. The
    script refuses to start if the resolved path is the real one.
  * every ``gh``-touching function is stubbed in **both** ``wt`` and ``tracker``
    (tracker binds them by value at import), ``wt.subprocess`` is swapped for a
    guard that raises on any attribute access, and the real ``subprocess``
    entry points plus ``os.system`` are replaced with raising stubs — so a
    missed stub fails loudly instead of reaching GitHub.
  * ``arc_browser`` / ``iterm_manager`` / ``browser_window`` are replaced with
    fake modules and ``webbrowser.open`` is recorded, so no AppleScript,
    Safari, Arc, iTerm or Hammerspoon call can escape.
  * the HTTP bridge is never started (port 7373 stays free for the real TUI).

The TUI itself is driven through Textual's official headless driver
(``App.run_test()`` → ``Pilot``).

Usage:

    venv/bin/python tools/test_tracker_phase3.py <pre-migration.json> \\
                                                 <migrated.json> [baseline.json] \\
                                                 <scratch-dir>

The baseline argument is accepted and ignored, purely so this harness takes the
same four arguments as the other three (it does its own before/after totals
rather than comparing against a Phase-0 snapshot).
"""
import asyncio
import copy
import json
import os
import re
import shutil
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

FAILURES = []
CHECKS = 0
LIVE = Path.home() / ".workload_tracker.json"


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


# ─────────────────────────────────────────────────────── environment lockdown ──

def build_home(scratch: Path, src: Path) -> Path:
    """Fake $HOME containing a copy of *src* as ~/.workload_tracker.json."""
    home = scratch / "home"
    if home.exists():
        shutil.rmtree(home)
    (home / ".workload_tracker_notes").mkdir(parents=True)
    dst = home / ".workload_tracker.json"
    shutil.copyfile(src, dst)
    os.environ["HOME"] = str(home)
    os.environ["WT_DATA_FILE"] = str(dst)
    return dst


def fake_module(name, attrs=None):
    mod = types.ModuleType(name)
    for k, v in (attrs or {}).items():
        setattr(mod, k, v)

    def _boom(*a, **k):
        raise AssertionError(f"{name} was actually used: {a!r} {k!r}")

    class _Any:
        def __init__(self, *a, **k):
            raise AssertionError(f"{name}.<class> instantiated — automation leak")

    mod.__getattr__ = lambda item: _Any  # type: ignore[attr-defined]
    sys.modules[name] = mod
    return mod


def lock_down_subprocess():
    """No process may be spawned from anywhere in this run."""
    import subprocess

    def boom(*a, **k):
        raise AssertionError(f"subprocess call escaped the stubs: {a!r}")

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        setattr(subprocess, name, boom)
    os.system = boom  # type: ignore[assignment]


# ───────────────────────────────────────────────────────────── main test body ──

def main():
    if len(sys.argv) == 4:
        fixture, migrated, scratch = (Path(a).expanduser() for a in sys.argv[1:4])
    elif len(sys.argv) == 5:
        # Uniform 4-argument form shared with the other harnesses; the baseline
        # slot is ignored here.
        fixture, migrated, _baseline, scratch = (
            Path(a).expanduser() for a in sys.argv[1:5])
    else:
        print(__doc__)
        return 2
    scratch.mkdir(parents=True, exist_ok=True)

    data_file = build_home(scratch, migrated)
    if data_file.resolve() == LIVE.resolve():
        print("REFUSING TO RUN: resolved data file is the live one")
        return 2
    print(f"data file: {data_file}")
    print(f"HOME:      {os.environ['HOME']}")

    lock_down_subprocess()
    fake_module("arc_browser")
    fake_module("iterm_manager")
    fake_module("browser_window")

    import wt
    from test_reconcile import Stubs, SubprocessGuard, GH_FUNCS  # noqa: F401

    # wt resolves DATA_FILE at import; rebind so nothing can drift to $HOME.
    wt.DATA_FILE = data_file
    assert wt.DATA_FILE != LIVE

    sprints = wt.get_cached_sprints(wt.load())
    if not sprints:
        print("fixture has no config.sprints_cache — cannot run offline")
        return 2
    print(f"offline sprints: {len(sprints)}")

    import tracker
    check(tracker.DATA_FILE == data_file,
          "tracker.DATA_FILE points at the scratch copy", str(tracker.DATA_FILE))

    # ---- stub every GitHub path, in wt *and* in tracker's own namespace -------
    stubs = Stubs(wt, mode="record", sprints=sprints)
    stubs.__enter__()
    tracker_saved = {}
    for name in set(GH_FUNCS):
        if hasattr(wt, name) and hasattr(tracker, name):
            # tracker bound these by value at import time, so it needs the stub too
            tracker_saved[name] = getattr(tracker, name)
            setattr(tracker, name, getattr(wt, name))
    # setup_issue_in_project / sync_project_hours are real wt functions that call
    # only stubbed primitives, so they can stay real — but tracker holds its own
    # reference to the *original* objects, which is fine since those resolve wt
    # globals at call time.
    for name in ("setup_issue_in_project", "sync_project_hours"):
        setattr(tracker, name, getattr(wt, name))
    tracker.get_idle_seconds = lambda: 0.0
    opened_urls = []
    tracker.webbrowser = types.SimpleNamespace(open=lambda u: opened_urls.append(u))
    # Never bind :7373 — the user may have the real TUI running.
    tracker.WorkloadTracker._start_bridge_server = lambda self: None
    check(True, "GitHub, subprocess, browser and automation calls are stubbed")

    asyncio.run(run_tui_checks(tracker, wt, sprints, data_file, stubs, opened_urls))

    static_checks()
    migration_check(tracker, wt, fixture, scratch)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} of {CHECKS} checks FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"{CHECKS}/{CHECKS} checks passed")
    return 0


def select(table, task_id) -> bool:
    """Move a DataTable's cursor onto the row for *task_id*."""
    for idx, key in enumerate(table.rows.keys()):
        if key.value == task_id:
            table.cursor_coordinate = (idx, 0)
            return True
    return False


def find(data, title):
    for t in data["tasks"]:
        if t["title"] == title:
            return t
    raise SystemExit(f"task not found: {title!r}")


async def run_tui_checks(tracker, wt, sprints, data_file, stubs, opened_urls):
    app = tracker.WorkloadTracker()
    data = app._data

    # Record every notification: it is the TUI's only error channel, so a
    # swallowed failure shows up here instead of vanishing.
    notes = []
    real_notify = app.notify

    def notify(msg, **kw):
        notes.append((kw.get("severity", "information"), str(msg)))
        return real_notify(msg, **kw)

    app.notify = notify

    # Pick fixtures out of the real (copied) dataset. They must be tasks the
    # board actually *shows* (not done, not recurrent), or a keypress would act
    # on whatever row the cursor happens to sit on.
    from datetime import datetime
    current = wt.find_sprint_for_date(sprints, datetime.now().date())
    idx = {s["id"]: i for i, s in enumerate(sprints)}
    used = set()

    def pick(pred):
        for t in data["tasks"]:
            if t.get("status") in ("done", "recurrent") or t["id"] in used:
                continue
            if pred(t):
                used.add(t["id"])
                return t
        return None

    def gap(t):
        st = t.get("start_sprint_id")
        if not st or st not in idx or not current:
            return -1
        return idx[current["id"]] - idx[st]

    # Far-out-of-window start sprint → the old InvalidSelectValueError crash.
    old_start = pick(lambda t: gap(t) > 4)
    # A cross-sprint task with something for a reconcile to do.
    multi = pick(lambda t: len(wt.task_sprints_with_time(t, sprints)) > 1)
    # An in-progress cross-sprint task with an issue, for the close workflow. It
    # may be one of the tasks above (the dataset only has a couple), just not the
    # one the reconcile test already brought in sync.
    close_target = next(
        (t for t in data["tasks"]
         if t.get("status") == "inprogress" and wt.task_current_issue(t, data)
         and len(wt.task_sprints_with_time(t, sprints)) > 1
         and t is not multi),
        None)

    async with app.run_test() as pilot:
        await pilot.pause()
        section("1. board render")
        main_tbl = app.query_one("#task-table", tracker.DataTable)
        rec_tbl = app.query_one("#task-table-recurrent", tracker.DataTable)
        expected_main = [t for t in data["tasks"]
                         if t.get("status") not in ("done", "recurrent")]
        expected_rec = [t for t in data["tasks"] if t.get("status") == "recurrent"]
        check(main_tbl.row_count == len(expected_main),
              f"main table has {len(expected_main)} rows", str(main_tbl.row_count))
        check(rec_tbl.row_count == len(expected_rec),
              f"recurrent table has {len(expected_rec)} rows", str(rec_tbl.row_count))
        check(all(t.get("cross_sprint_parent") is None for t in data["tasks"]),
              "no shadow task survives load_data()")

        section("2. sprint column reads the current binding")
        rows = {k.value: main_tbl.get_row(k) for k in main_tbl.rows}
        bad = []
        for t in expected_main:
            cell = rows[t["id"]][3]
            b = wt.current_binding(t, data)
            want = (b or {}).get("sprint") or t.get("sprint", "") or ""
            if not cell.startswith(want):
                bad.append((t["title"], cell, want))
        check(not bad, "every Sprint cell starts with its current binding's sprint",
              str(bad[:3]))
        carry = [rows[t["id"]][3] for t in expected_main if "←" in rows[t["id"]][3]]
        check(True, f"carry-over marker rendered on {len(carry)} row(s)",
              f"e.g. {carry[0] if carry else 'none'}")

        section("3. role filter + show-done keys")
        roles = tracker.get_roles(data)
        await pilot.press("1")
        await pilot.pause()
        want = len([t for t in expected_main if t.get("role_id") == roles[0]["id"]])
        check(main_tbl.row_count == want,
              f"'1' filters to role {roles[0]['id']} ({want} rows)", str(main_tbl.row_count))
        await pilot.press("0")
        await pilot.pause()
        check(main_tbl.row_count == len(expected_main), "'0' restores all roles",
              str(main_tbl.row_count))
        await pilot.press("a")
        await pilot.pause()
        with_done = len([t for t in data["tasks"] if t.get("status") != "recurrent"])
        check(main_tbl.row_count == with_done, f"'a' shows done tasks ({with_done} rows)",
              str(main_tbl.row_count))
        await pilot.press("a")
        await pilot.pause()

        section("4. edit modal on an out-of-window start sprint")
        check(old_start is not None,
              "found a visible task whose start_sprint is >4 sprints back",
              old_start["title"] if old_start else "none")
        check(select(main_tbl, old_start["id"]), f"selected '{old_start['title']}'")
        main_tbl.focus()
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        modal = app.screen
        check(isinstance(modal, tracker.TaskModal),
              "TaskModal mounted without InvalidSelectValueError", type(modal).__name__)
        sel = modal.query_one("#sel-sprint", tracker.Select)
        check(sel.value == old_start["start_sprint_id"],
              f"sprint Select shows the task's start sprint ({old_start.get('start_sprint')})",
              str(sel.value))
        before = copy.deepcopy(old_start)
        modal.query_one("#btn-save", tracker.Button).press()
        await pilot.pause()
        await pilot.pause()
        after = find(app._data, before["title"])
        check(after.get("sprint_issues") == before.get("sprint_issues"),
              "saving the edit modal preserves sprint_issues")
        check(after.get("start_sprint_id") == before.get("start_sprint_id")
              and after.get("start_sprint") == before.get("start_sprint"),
              "saving the edit modal preserves start_sprint*")
        check("cross_sprint_parent" not in after,
              "saving the edit modal does not resurrect cross_sprint_parent")

        section("5. log modal")
        await pilot.press("l")
        await pilot.pause()
        check(isinstance(app.screen, tracker.EditLogsModal),
              "EditLogsModal mounted", type(app.screen).__name__)
        await pilot.press("escape")
        await pilot.pause()

        section("6. sync-sprints preview (dry run) is write-free")
        before_bytes = data_file.read_bytes()
        n_calls = len(stubs.calls)
        check(multi is not None, "found a visible cross-sprint task",
              multi["title"] if multi else "none")
        check(select(main_tbl, multi["id"]), f"selected '{multi['title']}'")
        main_tbl.focus()
        await pilot.pause()
        await pilot.press("S")
        await pilot.pause()
        check(isinstance(app.screen, tracker.SyncSprintsModal),
              f"SyncSprintsModal mounted for '{multi['title']}'", type(app.screen).__name__)
        plan_lines = getattr(app.screen, "_plan_lines", [])
        check(bool(plan_lines), "preview lists a non-empty plan", str(plan_lines[:2]))
        check(len(stubs.calls) == n_calls,
              "the preview made zero GitHub calls",
              str([c[0] for c in stubs.calls[n_calls:]]))
        await pilot.press("escape")
        await pilot.pause()
        check(data_file.read_bytes() == before_bytes,
              "cancelling the preview left the data file byte-identical")

        section("7. sync-sprints skips recurrent tasks")
        rec_task = [t for t in data["tasks"] if t.get("status") == "recurrent"][0]
        select(rec_tbl, rec_task["id"]); rec_tbl.focus()
        rec_tbl.focus()
        await pilot.pause()
        n_calls = len(stubs.calls)
        await pilot.press("S")
        await pilot.pause()
        check(not isinstance(app.screen, tracker.SyncSprintsModal),
              "no reconcile preview for a recurrent task", type(app.screen).__name__)
        check(len(stubs.calls) == n_calls, "and no GitHub call was made")

        section("8. sync-sprints execution reconciles per sprint")
        select(main_tbl, multi["id"])
        main_tbl.focus()
        await pilot.pause()
        before_bindings = {b.get("sprint_id") for b in multi.get("sprint_issues", [])}
        before_dump = copy.deepcopy(multi.get("sprint_issues", []))
        expected_sprints = {e["sprint_id"] for e in wt.task_sprints_with_time(multi, sprints)}
        await pilot.press("S")
        await pilot.pause()
        check(isinstance(app.screen, tracker.SyncSprintsModal),
              "preview re-opened for the execution pass", type(app.screen).__name__)
        app.screen.dismiss(True)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        after_bindings = {b.get("sprint_id") for b in multi.get("sprint_issues", [])}
        check(expected_sprints <= after_bindings,
              "every sprint with logged time now has a binding",
              f"missing {sorted(expected_sprints - after_bindings)}")
        check(len(multi["sprint_issues"]) >= len(before_dump),
              "no binding was dropped",
              f"before={[(b.get('sprint'), b.get('issue')) for b in before_dump]} "
              f"after={[(b.get('sprint'), b.get('issue')) for b in multi['sprint_issues']]}")
        # Option A carries the original issue *forward*, so an existing binding's
        # sprint_id can move (a binding on a sprint with no minutes is re-pointed
        # to the latest sprint). What must never happen is losing an issue ref.
        before_issues = {b.get("issue") for b in before_dump if b.get("issue")}
        after_issues = {b.get("issue") for b in multi["sprint_issues"] if b.get("issue")}
        check(before_issues <= after_issues, "every issue ref survived the reconcile",
              f"lost {sorted(before_issues - after_issues)}")
        moved = before_bindings - after_bindings - {None}
        check(all(wt.task_mins_for_sprint(multi, sid, sprints) == 0 for sid in moved),
              "any re-pointed binding came off a sprint with no logged minutes",
              f"moved {sorted(moved)}")
        hours_by_sprint = {}
        for b in multi["sprint_issues"]:
            sid = b.get("sprint_id")
            if b.get("hours_synced") is not None:
                hours_by_sprint[sid] = b["hours_synced"]
        wrong = {
            sid: (h, wt.mins_to_quarter_hours(wt.task_mins_for_sprint(multi, sid, sprints)))
            for sid, h in hours_by_sprint.items()
            if abs(h - wt.mins_to_quarter_hours(
                wt.task_mins_for_sprint(multi, sid, sprints))) > 1e-9
        }
        check(not wrong, "each binding's hours_synced == that sprint's own hours",
              str(wrong))
        total_hours = wt.mins_to_quarter_hours(wt.task_logged_mins(multi))
        cur = wt.current_binding(multi, data)
        check(cur is None or cur.get("hours_synced") is None
              or len(expected_sprints) == 1
              or abs(cur["hours_synced"] - total_hours) > 1e-9,
              "the current issue was NOT told the whole task total",
              f"{cur and cur.get('hours_synced')} vs total {total_hours}")
        # second run is a no-op
        n_calls = len(stubs.calls)
        await pilot.press("S")
        await pilot.pause()
        check(not isinstance(app.screen, tracker.SyncSprintsModal),
              "a second reconcile finds nothing to do (idempotent)",
              type(app.screen).__name__)
        check(len(stubs.calls) == n_calls, "and issues no GitHub call")

        section("9. close workflow reports sprint-filtered hours")
        check(close_target is not None,
              "found an in-progress cross-sprint task with an issue",
              close_target["title"] if close_target else "none")
        # Re-resolve by id before every assertion: _on_task_saved() *replaces* the
        # task dict when the edit modal is saved, so a reference captured earlier
        # (section 4 edited this same task) points at an orphan.
        target_id = close_target["id"]

        def live():
            return next(t for t in app._data["tasks"] if t["id"] == target_id)

        target = live()
        n_sprints_before = len(wt.task_sprints_with_time(target, sprints))
        check(select(main_tbl, target_id), f"selected '{target['title']}'")
        main_tbl.focus()
        await pilot.pause()
        task_total_hours = wt.mins_to_quarter_hours(wt.task_logged_mins(target))
        n_calls = len(stubs.calls)
        await pilot.press("D")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        check(isinstance(app.screen, tracker.ConfirmCloseTaskModal),
              "ConfirmCloseTaskModal mounted", type(app.screen).__name__)
        shown = app.screen._local_mins
        check(abs(shown - wt.task_reportable_mins(live(), sprints)) < 1e-9,
              "the modal shows the sprint-filtered minutes, not the task total",
              f"{shown} vs reportable {wt.task_reportable_mins(live(), sprints)}")
        app.screen.dismiss(True)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        target = live()
        print("    notifications:")
        for sev, msg in notes[-14:]:
            print(f"      [{sev}] {msg}")
        check(target["status"] == "done", "task is marked done", target["status"])
        # After the reconcile the current binding is the *latest* sprint, so the
        # hours the close pushes are that sprint's own — which is the whole point
        # of Option A. Compute the expectation the way close_task does.
        cur_b = wt.current_binding(target, app._data)
        expect_hours = wt.mins_to_quarter_hours(
            wt.task_mins_for_sprint(target, (cur_b or {}).get("sprint_id"), sprints))
        hours_pushed = [c[1][1] for c in stubs.calls[n_calls:]
                        if c[0] == "add_to_project_and_update"]
        check(hours_pushed and abs(hours_pushed[-1] - expect_hours) < 1e-9,
              f"the current issue was told {expect_hours}h — only "
              f"{(cur_b or {}).get('sprint')}'s own hours",
              str(hours_pushed))
        check(not any(abs(h - task_total_hours) < 1e-9 for h in hours_pushed)
              or abs(expect_hours - task_total_hours) < 1e-9,
              f"the task total ({task_total_hours}h) was never pushed as this "
              f"issue's hours",
              f"{hours_pushed} total={task_total_hours}")
        closed = [c[1][0] for c in stubs.calls[n_calls:] if c[0] == "close_github_issue"]
        check(bool(closed), "the GitHub issue was closed", str(closed))
        bound = {b.get("sprint_id") for b in target.get("sprint_issues", [])}
        want_bound = {e["sprint_id"] for e in wt.task_sprints_with_time(target, sprints)}
        check(want_bound <= bound,
              f"the close reconciled all {n_sprints_before} sprints with time first",
              f"missing {sorted(want_bound - bound)}")
        created = [c for c in stubs.calls[n_calls:] if c[0] == "create_github_issue"]
        check(len(closed) >= 1 + len(created),
              "each newly minted past-sprint issue was closed too",
              f"{len(created)} created, {len(closed)} closed")

        section("10. HTTP bridge helpers")
        listed = app._bridge_list_tasks()["tasks"]
        want = len([t for t in data["tasks"] if t.get("status") != "done"])
        check(len(listed) == want, f"/tasks lists all {want} non-done tasks",
              str(len(listed)))
        status = app._bridge_status()
        check("active_timer" in status and "time_by_role" in status,
              "/status still renders")

        section("11. open-issue action uses the accessor")
        issued = next(t for t in data["tasks"] if wt.task_current_issue(t, data))
        app.show_done = True
        app._populate_table()
        await pilot.pause()
        select(main_tbl, issued["id"]); main_tbl.focus()
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        ref = wt.task_current_issue(issued, data)
        repo, num = ref.rsplit("#", 1)
        check(opened_urls and opened_urls[-1].endswith(f"{repo}/issues/{num}"),
              "'o' opened the current binding's issue URL", str(opened_urls[-1:]))

        # Snapshot for tools/check_invariants.py *before* the timer test appends a
        # log (which would legitimately change the total minutes).
        snapshot = data_file.parent.parent / "after_tui.json"
        shutil.copyfile(data_file, snapshot)
        print(f"    invariants snapshot: {snapshot}")

        section("12. status / sync / update actions use the accessor")
        app.show_done = False
        app._populate_table()
        await pilot.pause()
        todo = next(t for t in app._data["tasks"]
                    if t.get("status") == "todo" and wt.task_current_issue(t, app._data))
        select(main_tbl, todo["id"])
        main_tbl.focus()
        await pilot.pause()
        n_calls = len(stubs.calls)
        await pilot.press("p")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        check(todo["status"] == "inprogress", "'p' moved the task to In Progress",
              todo["status"])
        synced = [c[1][0] for c in stubs.calls[n_calls:] if c[0] == "sync_project_status"]
        check(synced and synced[0] == wt.task_current_issue(todo, app._data),
              "'p' synced status on the current binding's issue", str(synced))
        await pilot.press("g")
        await pilot.pause()
        check(isinstance(app.screen, tracker.SyncIssueModal),
              "'g' on a linked task offers the project sync", type(app.screen).__name__)
        check(app.screen._issue_ref == wt.task_current_issue(todo, app._data),
              "and names the current binding's issue", str(app.screen._issue_ref))
        await pilot.press("escape")
        await pilot.pause()
        n_calls = len(stubs.calls)
        await pilot.press("u")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        check(any(c[0] == "add_issue_to_project" for c in stubs.calls[n_calls:]),
              "'u' pushed the project fields", str([c[0] for c in stubs.calls[n_calls:]]))

        section("13. timer start/stop syncs hours via the accessor")
        # Arc tab cleanup would try to drive Arc through AppleScript on stop; the
        # fake arc_browser module raises rather than silently no-op, so switch the
        # feature off for this copy (the timer path is what's under test).
        app._data.setdefault("config", {})["tab_cleanup_enabled"] = False
        timed = next(t for t in app._data["tasks"]
                     if t.get("status") == "inprogress" and not t.get("tabs")
                     and wt.task_current_issue(t, app._data))
        select(main_tbl, timed["id"])
        main_tbl.focus()
        await pilot.pause()
        n_logs = len(timed.get("logs", []))
        await pilot.press("t")
        await pilot.pause()
        check((app._data.get("active_timer") or {}).get("task_id") == timed["id"],
              "'t' started the timer", str(app._data.get("active_timer")))
        n_calls = len(stubs.calls)
        await pilot.press("t")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        check(app._data.get("active_timer") is None, "'t' again stopped it")
        check(len(timed.get("logs", [])) in (n_logs, n_logs + 1),
              "a session log was appended (or the session was <0.1min)",
              f"{n_logs} -> {len(timed.get('logs', []))}")
        touched = [c for c in stubs.calls[n_calls:]
                   if c[0] in ("update_project_hours", "sync_project_status")]
        check(all(c[1][0] == wt.task_current_issue(timed, app._data) for c in touched),
              "the hours sync targeted the current binding's issue",
              str([(c[0], c[1][0]) for c in touched]))


def static_checks():
    section("14. static assertions on tracker.py")
    src = (REPO / "tracker.py").read_text()
    code_lines = []
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        code_lines.append(line.split("#")[0])
    code = "\n".join(code_lines)
    check("cross_sprint_parent" not in code,
          "no cross_sprint_parent reference survives in code",
          [l for l in code.splitlines() if "cross_sprint_parent" in l][:2])

    # every github_issue mention must be an import, a _create_github_issue flag,
    # a method name, or the preserve-key list — never a task field read.
    offenders = []
    for m in re.finditer(r'.*github_issue.*', code):
        line = m.group(0)
        if re.search(r'(get|create|delete|update)_github_issue|_create_github_issue'
                     r'|"github_issue", "sprint_issues"', line):
            continue
        offenders.append(line.strip())
    check(not offenders, "no direct task['github_issue'] read/write remains",
          str(offenders[:3]))
    # The legacy sprint/sprint_id keys are still *written* (wt.py mirrors them and
    # mcp_server.py + an older wt.py on the other Mac still read them), so a
    # handful of reads remain by design. Pin the count so a new one is visible.
    legacy = [l.strip() for l in code.splitlines()
              if re.search(r'\.get\("sprint_id"\)|\["sprint_id"\]', l)]
    check(len(legacy) == 6,
          "exactly 6 legacy sprint_id sites remain (documented fallbacks + the "
          "new-task legacy mirror)",
          "\n        " + "\n        ".join(legacy))
    check("split_cross_sprint_task" not in code and "sprint_summary_for_task" not in code,
          "the deprecated split shims are no longer imported")


def migration_check(tracker, wt, fixture, scratch):
    section("15. load_data() migrates shadows (pre-migration fixture)")
    home2 = scratch / "home_fixture"
    if home2.exists():
        shutil.rmtree(home2)
    home2.mkdir(parents=True)
    dst = home2 / ".workload_tracker.json"
    shutil.copyfile(fixture, dst)
    raw = json.loads(dst.read_text())
    n_shadow = len([t for t in raw["tasks"] if t.get("cross_sprint_parent")])
    check(n_shadow > 0, f"fixture really has {n_shadow} shadow task(s)")
    saved = tracker.DATA_FILE
    try:
        tracker.DATA_FILE = dst
        loaded = tracker.load_data()
    finally:
        tracker.DATA_FILE = saved
    left = [t for t in loaded["tasks"] if t.get("cross_sprint_parent")]
    check(not left, "load_data() converted every shadow into bindings", str(len(left)))
    # load_data() runs both migrations, so the drop is the shadows *plus* the
    # per-sprint recurrent clones the Phase 5 merge folds into one task per series.
    series = {}
    for t in raw["tasks"]:
        if t.get("cross_sprint_parent"):
            continue
        canon = wt.recurrent_series_for_title(t.get("title", ""))
        if canon:
            series[canon] = series.get(canon, 0) + 1
    n_merged = sum(n - 1 for n in series.values() if n > 1)
    check(len(loaded["tasks"]) == len(raw["tasks"]) - n_shadow - n_merged,
          f"task count dropped by {n_shadow} shadow(s) + {n_merged} merged clone(s)",
          f"{len(raw['tasks'])} -> {len(loaded['tasks'])}")
    on_disk = json.loads(dst.read_text())
    check(not [t for t in on_disk["tasks"] if t.get("cross_sprint_parent")],
          "and persisted the migration to disk")
    before_mins = sum(sum(l.get("minutes", 0) for l in t.get("logs", []))
                      for t in raw["tasks"] if not t.get("cross_sprint_parent"))
    after_mins = sum(sum(l.get("minutes", 0) for l in t.get("logs", []))
                     for t in loaded["tasks"])
    check(abs(before_mins - after_mins) < 1e-9,
          "non-shadow log minutes unchanged by the migration",
          f"{before_mins} vs {after_mins}")


if __name__ == "__main__":
    sys.exit(main())
