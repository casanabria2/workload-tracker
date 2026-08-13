#!/usr/bin/env python3
"""Verification harness for Phase 3 of docs/plan-sprint-bindings.md — mcp_server.py.

There is no automated test suite in this repo, so this script *is* the test for
the MCP half of Phase 3. It runs fully offline:

  * every ``gh``-touching function in ``wt`` is monkeypatched (Phase 2's ``Stubs``
    from ``tools/test_reconcile.py`` is reused verbatim), and ``wt.subprocess``
    is swapped for a guard that raises on any attribute access;
  * ``mcp_server`` re-binds several of those names at import time
    (``get_all_sprints``, ``get_current_sprint``, ``delete_github_issue``,
    ``sync_project_status``), so those module attributes are patched too;
  * several MCP tools shell out via a *function-local* ``import subprocess``,
    which resolves through ``sys.modules`` at call time and therefore cannot be
    intercepted by a module attribute. ``sys.modules["subprocess"]`` is swapped
    for a recording fake for the duration of those calls, so an unstubbed
    ``gh`` invocation raises instead of reaching real GitHub;
  * every run happens on a fresh copy in a scratch dir, and the script refuses
    to start if ``WT_DATA_FILE`` resolves to the live data file.

Usage:

    WT_DATA_FILE=<scratch>/unused.json \\
    venv/bin/python test_mcp_phase3.py <fixture.json> <migrated.json> \\
                                       <baseline.pristine.json> <scratch-dir>

Exit status 0 when every check passes.
"""
import asyncio
import copy
import inspect
import json
import os
import shutil
import subprocess as real_subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from test_reconcile import (  # noqa: E402
    Stubs, SubprocessGuard, sig, multi_sprint_tasks, pick_multi_sprint,
    unreconcile,
)
from test_phase3 import new_sprint_boundary  # noqa: E402

FAILURES = []
_WANT = [0]   # expected post-migration task count, filled in main()
CHECKS = 0

# gh subcommands that mutate GitHub. Any of these reaching the fake subprocess is
# reported so a "read-only" claim can be checked rather than asserted.
WRITE_VERBS = {"create", "close", "edit", "comment", "delete", "item-add", "item-edit"}


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


# ------------------------------------------------------------ subprocess fake --

class _CP:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeSubprocess:
    """Stand-in for the ``subprocess`` *module* (swapped into sys.modules).

    ``run()`` records the argv and answers from *responses* (substring match on
    the joined command). Anything unmatched raises, so a missed stub is loud.
    Every other attribute raises on call.
    """

    def __init__(self, responses=None):
        self.calls = []
        self.responses = list((responses or {}).items())

    def run(self, cmd, *a, **k):
        joined = " ".join(str(x) for x in cmd)
        self.calls.append(list(cmd))
        for needle, (rc, out) in self.responses:
            if needle in joined:
                return _CP(rc, out)
        raise AssertionError(f"unstubbed subprocess.run: {joined}")

    def __getattr__(self, name):
        def boom(*a, **k):
            raise AssertionError(f"subprocess.{name} called — stub missing")
        return boom

    def writes(self):
        out = []
        for cmd in self.calls:
            if len(cmd) > 2 and cmd[0] == "gh" and cmd[2] in WRITE_VERBS:
                out.append(" ".join(str(x) for x in cmd))
        return out


class SwapSubprocess:
    """Temporarily replace sys.modules["subprocess"] with *fake*."""

    def __init__(self, fake):
        self.fake = fake

    def __enter__(self):
        self._saved = sys.modules["subprocess"]
        sys.modules["subprocess"] = self.fake
        return self.fake

    def __exit__(self, *exc):
        sys.modules["subprocess"] = self._saved
        return False


class McpStubs(Stubs):
    """``Stubs`` plus the copies ``mcp_server`` bound at import time.

    ``from wt import X`` creates an independent binding, so patching ``wt.X`` is
    not enough for the four names mcp_server calls directly. ``get_all_sprints``
    stays stubbed-but-allowed in both modes (a pure read the tools do themselves).
    """

    REBOUND = ["get_all_sprints", "get_current_sprint", "delete_github_issue",
               "sync_project_status"]

    def __init__(self, wt, mcp_server, **kw):
        super().__init__(wt, **kw)
        self.mcp = mcp_server
        self._mcp_saved = {}

    def __enter__(self):
        super().__enter__()
        self.wt.get_all_sprints = lambda d, _s=self.sprints: copy.deepcopy(_s)
        for name in self.REBOUND:
            self._mcp_saved[name] = getattr(self.mcp, name)
            setattr(self.mcp, name, getattr(self.wt, name))
        # get_current_sprint is not in GH_FUNCS; route it through the stubbed list.
        self.mcp.get_current_sprint = lambda d: self.wt.find_sprint_for_date(
            copy.deepcopy(self.sprints), __import__("datetime").date.today())
        return self

    def __exit__(self, *exc):
        for name, val in self._mcp_saved.items():
            setattr(self.mcp, name, val)
        return super().__exit__(*exc)


# ------------------------------------------------------------------- fixtures --

def point_at(wt, mcp_server, src, dst):
    """Copy *src* to *dst* and point **both** modules' DATA_FILE at it."""
    dst = Path(dst)
    live = Path.home() / ".workload_tracker.json"
    assert dst.name.endswith(".json") and dst != live, dst
    shutil.copyfile(src, dst)
    os.environ["WT_DATA_FILE"] = str(dst)
    wt.DATA_FILE = dst
    mcp_server.DATA_FILE = dst
    return dst


def sprints_of(data, wt):
    return wt.get_cached_sprints(data)


# ----------------------------------------------------------------------- tests --

def test_import_and_shape(wt, mcp_server):
    section("1. module imports, tool registry, and no legacy imports")
    check(mcp_server.DATA_FILE != Path.home() / ".workload_tracker.json",
          "DATA_FILE honours WT_DATA_FILE", str(mcp_server.DATA_FILE))

    for gone in ("split_cross_sprint_task", "sprint_summary_for_task",
                 "task_logged_mins_for_sprint", "sprint_split"):
        check(not hasattr(mcp_server, gone), f"{gone} no longer bound in mcp_server")
    for need in ("task_current_issue", "set_task_current_issue",
                 "clear_task_current_issue", "current_binding", "task_issue_refs",
                 "task_reportable_mins", "task_sprints_with_time",
                 "reconcile_task_sprints", "close_task",
                 "_migrate_shadows_to_bindings", "sync_task_sprints"):
        check(hasattr(mcp_server, need), f"{need} bound in mcp_server")

    src = (REPO / "mcp_server.py").read_text()
    check("cross_sprint_parent" not in src, "no cross_sprint_parent reference at all")

    # The tools are plain sync functions: FastMCP's @mcp.tool() registers and
    # returns the original function, so the module-level names are callable
    # directly. Proved rather than assumed, since the brief asked.
    for name in ("list_tasks", "get_task", "sync_task_sprints", "set_sprint",
                 "set_task_status", "list_sprints", "get_current_sprint_info",
                 "link_github_issue", "unlink_github_issue",
                 "push_task_to_github", "get_status"):
        fn = getattr(mcp_server, name)
        check(callable(fn) and not inspect.iscoroutinefunction(fn),
              f"{name} is a plain sync function (not async)")

    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {t.name for t in tools}
    check("sync_task_sprints" in names, "sync_task_sprints registered as an MCP tool")
    check("sprint_split" not in names, "sprint_split no longer registered")
    schema = next(t.inputSchema for t in tools if t.name == "sync_task_sprints")
    props = set(schema.get("properties", {}))
    check(props == {"task_query", "all_tasks", "create_issues", "dry_run"},
          "sync_task_sprints schema properties", str(sorted(props)))
    print(f"       ({len(names)} tools registered)")
    return names


def test_load_migrates(wt, mcp_server, fixture, scratch):
    section("2. mcp_server.load() runs the shadow->bindings migration (the fix)")
    dst = point_at(wt, mcp_server, fixture, scratch / "premigration.json")
    raw = json.loads(dst.read_text())
    # Counts are read off the fixture, not hardcoded. The old constants (92 tasks
    # / 12 shadows / 82 bindings / 26620.71 minutes / 392 logs) described one
    # specific July-2026 snapshot that no longer exists; see tools/README.md and
    # tools/make_fixtures.py for how the pre-migration fixture is produced now.
    n_raw = len(raw["tasks"])
    n_shadow = sum(1 for t in raw["tasks"] if t.get("cross_sprint_parent"))
    n_clone = sum(1 for t in raw["tasks"]
                  if wt.recurrent_series_for_title(t.get("title", "")))
    check(n_shadow > 0,
          f"fixture is really pre-migration: {n_raw} tasks, {n_shadow} shadow(s)",
          "0 shadows — this section would pass vacuously; rebuild the fixture "
          "with tools/make_fixtures.py")
    print(f"       ({n_raw} tasks, {n_shadow} shadows, {n_clone} recurrent clones)")
    pre_total = sum(l.get("minutes", 0) for t in raw["tasks"]
                    for l in t.get("logs", []) if not t.get("cross_sprint_parent"))
    pre_logs = sum(len(t.get("logs", [])) for t in raw["tasks"]
                   if not t.get("cross_sprint_parent"))

    data = mcp_server.load()
    check(len(data["tasks"]) == _WANT[0], f"mcp_server.load() returns {_WANT[0]} tasks",
          str(len(data["tasks"])))
    check(not any(t.get("cross_sprint_parent") for t in data["tasks"]),
          "no shadow survives mcp_server.load()")
    n_bind = sum(len(t.get("sprint_issues") or []) for t in data["tasks"])
    # Every shadow and every clone contributes its issue as a binding, and every
    # surviving task keeps at least one. Exact equality is not asserted (a sprint
    # that ends up with two issues collapses to one binding plus a superseded
    # entry), but the count must at least cover the shadows.
    check(n_bind >= n_shadow,
          f"{n_bind} bindings after mcp_server.load() (>= {n_shadow} shadows)",
          str(n_bind))
    check(data.get("config", {}).get("sprint_bindings_migrated") is True,
          "migration flag set")
    check(data.get("config", {}).get("recurrent_series_merged") is True,
          "recurrent-merge flag set")
    # It persisted, and a second load is a no-op.
    on_disk = json.loads(dst.read_text())
    check(len(on_disk["tasks"]) == _WANT[0], "migration was saved to disk",
          str(len(on_disk["tasks"])))
    before = dst.read_text()
    mcp_server.load()
    check(dst.read_text() == before, "second mcp_server.load() is byte-identical")

    total = sum(l.get("minutes", 0) for t in data["tasks"] for l in t.get("logs", []))
    logs = sum(len(t.get("logs", [])) for t in data["tasks"])
    check(abs(total - pre_total) < 1e-6,
          f"total minutes preserved ({pre_total})", f"{total}")
    check(logs == pre_logs, f"log count preserved ({pre_logs})", str(logs))


def test_list_tasks(wt, mcp_server, migrated, scratch):
    section("3. list_tasks — no shadow filter, filters still work")
    point_at(wt, mcp_server, migrated, scratch / "list.json")
    data = mcp_server.load()
    n_all = len(data["tasks"])
    n_done = sum(1 for t in data["tasks"] if t.get("status") == "done")

    out = mcp_server.list_tasks()
    n_default = out.count("\n  ID: ")
    check(n_default == n_all - n_done,
          f"default list = {n_all - n_done} non-done tasks", str(n_default))

    out_all = mcp_server.list_tasks(include_done=True)
    check(out_all.count("\n  ID: ") == n_all,
          f"include_done=True lists all {n_all}", str(out_all.count("\n  ID: ")))

    out_done = mcp_server.list_tasks(status="done")
    check(out_done.count("\n  ID: ") == n_done,
          f'status="done" lists {n_done}', str(out_done.count("\n  ID: ")))

    n_rec = sum(1 for t in data["tasks"] if t.get("status") == "recurrent")
    out_rec = mcp_server.list_tasks(status="recurrent")
    check(out_rec.count("\n  ID: ") == n_rec, f'status="recurrent" lists {n_rec}',
          str(out_rec.count("\n  ID: ")))

    role = data["tasks"][0]["role_id"]
    n_role = sum(1 for t in data["tasks"]
                 if t.get("role_id") == role and t.get("status") != "done")
    out_role = mcp_server.list_tasks(role=role)
    check(out_role.count("\n  ID: ") == n_role, f"role={role!r} filter",
          str(out_role.count("\n  ID: ")))

    check(mcp_server.list_tasks(role="nope") == "No tasks found.",
          "unknown role -> No tasks found.")

    # A previously-shadowed task's parent is visible exactly once, and none of
    # the 12 shadow titles (parent title + " (Sprint N)") appears at all.
    titles = [l for l in out_all.splitlines() if l and not l.startswith("  ID: ")]
    check(titles.count("IRON Infusion") == 1,
          "the ex-shadow parent appears exactly once",
          str(titles.count("IRON Infusion")))
    shadowish = [t for t in titles if t.rstrip().endswith(")") and " (Sprint " in t]
    check(not shadowish, "no shadow-named task in the list", str(shadowish))


def test_get_task(wt, mcp_server, migrated, scratch):
    section("4. get_task — sprint_issues + start_sprint")
    point_at(wt, mcp_server, migrated, scratch / "get.json")
    data = mcp_server.load()
    multi = max(data["tasks"], key=lambda t: len(t.get("sprint_issues") or []))
    out = mcp_server.get_task(multi["id"])
    print("\n".join("       " + l for l in out.splitlines()[:20]))
    check("Start sprint:" in out, "get_task reports Start sprint")
    check("Sprint issues (" in out, "get_task reports the sprint_issues list")
    check("← current" in out, "get_task marks the current binding")
    check("Logged time by sprint:" in out, "get_task reports per-sprint minutes")
    # One check, not one per binding: how many bindings the most-bound task has
    # grows by one every sprint, so a per-binding loop made the harness's own
    # check *count* a function of the day's data.
    refs = [b["issue"] for b in multi["sprint_issues"] if b.get("issue")]
    missing = [r for r in refs if r not in out]
    check(not missing, f"every binding issue is shown ({len(refs)} of them)",
          str(missing))
    cur = wt.task_current_issue(multi, data)
    check(f"GitHub Issue: {cur}" in out, "GitHub Issue line uses task_current_issue",
          cur or "None")
    check(mcp_server.get_task("no-such-task-xyz").startswith("No task found"),
          "get_task misses cleanly")

    # A task with no bindings at all must not crash.
    plain = {"id": "zz", "title": "no bindings here", "role_id": "other",
             "status": "todo", "logs": [], "created_at": 0}
    data["tasks"].append(plain)
    mcp_server.save(data)
    out2 = mcp_server.get_task("no bindings here")
    check("Sprint issues (" not in out2, "task with no bindings renders without the section")
    check("Start sprint: (unknown)" in out2, "unknown start sprint rendered")


def test_sync_dry_run(wt, mcp_server, migrated, scratch):
    section("5. sync_task_sprints(dry_run=True) — zero GitHub calls, zero writes")
    dst = point_at(wt, mcp_server, migrated, scratch / "dry.json")
    data = mcp_server.load()
    sprints = sprints_of(data, wt)
    # Rolled back to its pre-reconcile shape so the dry run has a plan to print.
    # Against the untouched fixture the task is already in sync, so the output is
    # "Nothing to do" and the dry-run banner never appears.
    subject, _per = pick_multi_sprint(wt, data, sprints)
    unreconcile(wt, subject, sprints, anchor="oldest")
    mcp_server.save(data)
    disk_before = dst.read_text()
    # By id: a title like "IRON Infusion" is a substring of several others and
    # mcp_server's resolve_task deliberately returns None on an ambiguous match.
    iron = subject["id"]

    with McpStubs(wt, mcp_server, mode="strict", sprints=sprints) as st:
        fake = FakeSubprocess()
        with SwapSubprocess(fake):
            out = mcp_server.sync_task_sprints(iron, dry_run=True)
        check(st.count("create_github_issue") == 0, "no issue created in a dry run")
        check(st.count("close_github_issue") == 0, "no issue closed in a dry run")
        check(fake.calls == [], "no subprocess call in a dry run", str(fake.calls[:2]))
    check("Dry run — nothing was changed." in out, "dry run says so")
    check(dst.read_text() == disk_before, "data file byte-identical after a dry run")
    print("\n".join("       " + l for l in out.splitlines()[:14]))
    return out


def test_requirement_a(wt, mcp_server, migrated, scratch):
    section("6. requirement (a): all_tasks dry run plans ZERO issue creations")
    dst = point_at(wt, mcp_server, migrated, scratch / "alldry.json")
    data = mcp_server.load()
    sprints = sprints_of(data, wt)

    # Construct the unbound past sprints this section is about. The sprint-start
    # ritual (`sync-sprints --all --create-issues`) binds every past sprint, so a
    # freshly-copied live file plans nothing either way and both halves of the
    # requirement — "reports what it did not bind" and "the opt-in would mint" —
    # pass or fail on the day rather than on the code. `logs` are untouched.
    rolled = 0
    for t, _per in multi_sprint_tasks(wt, data, sprints):
        unreconcile(wt, t, sprints)
        rolled += 1
    if not rolled:
        raise SystemExit("fixture has no cross-sprint task to un-reconcile — "
                         "requirement (a) cannot be exercised")
    mcp_server.save(data)
    data = mcp_server.load()
    disk_before = dst.read_text()
    print(f"       un-reconciled {rolled} cross-sprint task(s)")

    with McpStubs(wt, mcp_server, mode="strict", sprints=sprints):
        fake = FakeSubprocess()
        with SwapSubprocess(fake):
            out = mcp_server.sync_task_sprints(all_tasks=True, dry_run=True)
        check(fake.calls == [], "no subprocess call", str(fake.calls[:2]))
    check(dst.read_text() == disk_before, "data file unchanged")

    # The rendered plan is the contract the caller sees.
    check("Totals: 0 issue(s) to create" in out,
          "all_tasks default plans 0 issue creations",
          [l for l in out.splitlines() if l.startswith("Totals:")])
    check("all_tasks does not create GitHub issues" in out,
          "the opt-in is spelled out in the output")
    unbilled = [l for l in out.splitlines() if "were NOT bound" in l]
    check(bool(unbilled), "unbillable past sprints are reported, not silently bound",
          str(unbilled))
    print("       " + (unbilled[0] if unbilled else "(none)"))

    # And the same assertion at the plan level, not just the rendering.
    with McpStubs(wt, mcp_server, mode="strict", sprints=sprints):
        creates = 0
        for t in data["tasks"]:
            if t.get("status") == "recurrent":
                continue
            res = wt.reconcile_task_sprints(t, data, sprints, create_issues=False,
                                            dry_run=True)
            creates += sum(1 for op in res["planned"]
                           if op["op"] == "create" and op.get("create_issue"))
    check(creates == 0, "plan-level: 0 create-issue ops across every task", str(creates))

    # Contrast: the opt-in really would mint issues, so the default is load-bearing.
    with McpStubs(wt, mcp_server, mode="strict", sprints=sprints):
        fake = FakeSubprocess()
        with SwapSubprocess(fake):
            out2 = mcp_server.sync_task_sprints(all_tasks=True, create_issues=True,
                                                dry_run=True)
    n = next((l for l in out2.splitlines() if l.startswith("Totals:")), "")
    would = int(n.split()[1]) if n else -1
    check(would > 0, "create_issues=True WOULD mint issues (so the default matters)", n)
    print(f"       opt-in would create {would} issue(s)")


def test_recurrent_reconciles(wt, mcp_server, migrated, scratch):
    section("7. Phase 5: MCP reconciles merged recurrent series")
    point_at(wt, mcp_server, migrated, scratch / "rec.json")
    data = mcp_server.load()
    sprints = sprints_of(data, wt)
    recurrent = [t for t in data["tasks"] if t.get("status") == "recurrent"]
    print(f"    {len(recurrent)} perpetual recurrent task(s):")
    for t in recurrent:
        print(f"      - {t['title']}  bindings={len(t.get('sprint_issues') or [])}")

    # Phase 3 asserted these were skipped, because each sprint was its own cloned
    # task. Phase 5 merged them, so they must now be reconciled like anything else.
    check(recurrent, "the merge left perpetual recurrent tasks")
    check(all(" - Sprint " not in t["title"] for t in recurrent),
          "none still carries a '- Sprint N' suffix",
          str([t["title"] for t in recurrent if " - Sprint " in t["title"]]))

    # The live fixture is normally *past* the sprint boundary (the owner's
    # sprint-start ritual already ran `wt sync-sprints --all`), so every series
    # is in sync and the plan is empty. Rebuild the boundary first — otherwise
    # "appears in the plan" is asserting against "Nothing to do".
    cur = wt.find_sprint_for_date(sprints, datetime.now().date())
    rolled = [t for t in recurrent
              if cur and new_sprint_boundary(wt, t, sprints, cur)]
    check(bool(rolled),
          f"{len(rolled)} of {len(recurrent)} series have an earlier sprint to "
          "roll back to",
          "every recurrent series started this sprint — section 7 would be vacuous")
    wt.save(data)
    mcp_server.save(data)

    with McpStubs(wt, mcp_server, mode="strict", sprints=sprints):
        fake = FakeSubprocess()
        with SwapSubprocess(fake):
            out = mcp_server.sync_task_sprints(all_tasks=True, dry_run=True)
    check("recurrent task(s)" not in out or "Skipped 0 recurrent" in out,
          "no recurrent tasks are reported as skipped",
          [l for l in out.splitlines() if "Skipped" in l][:2])
    for t in rolled:
        check(t["title"] in out, f"appears in the plan: {t['title'][:34]}")

    # Single-task form: a perpetual series closes the ended sprint and mints the
    # new one, never carrying an issue forward (that would strand hours).
    target = rolled[0]["title"]
    with McpStubs(wt, mcp_server, mode="strict", sprints=sprints):
        fake = FakeSubprocess()
        with SwapSubprocess(fake):
            one = mcp_server.sync_task_sprints(target, create_issues=True, dry_run=True)
    check("Nothing to do" not in one, f"{target[:30]!r} has work planned", one[:160])
    check("repoint" not in one.lower(), "no carry-forward for a perpetual series",
          [l for l in one.splitlines() if "repoint" in l.lower()][:2])


def test_sync_real_run(wt, mcp_server, migrated, scratch):
    section("8. sync_task_sprints real run (fully stubbed) + idempotency")
    dst = point_at(wt, mcp_server, migrated, scratch / "real.json")
    data = mcp_server.load()
    sprints = sprints_of(data, wt)
    # Subject picked from the fixture and rolled back to its pre-reconcile shape
    # (was pinned to 'Assist on Banco Galicia', which the owner has since
    # reconciled for real — leaving "at least one issue minted" unsatisfiable).
    task, _per = pick_multi_sprint(wt, data, sprints)
    # Keep the real bindings so the rollback can be undone before section 13.
    pristine = copy.deepcopy({k: task.get(k) for k in
                              ("sprint_issues", "github_issue", "sprint_id", "sprint")})
    unreconcile(wt, task, sprints, anchor="oldest")
    # By id, not title: mcp_server.resolve_task returns None on an ambiguous
    # substring match, and several fixture titles are prefixes of others.
    subject_id = task["id"]
    subject = task["title"]
    mcp_server.save(data)
    before_bindings = len(task.get("sprint_issues") or [])
    # Compare log *content* ignoring the uploaded_at marker that
    # mark_logs_uploaded stamps (pre-existing behaviour, not a reconcile change).
    def logsig(t):
        return json.dumps([{k: v for k, v in l.items() if k != "uploaded_at"}
                           for l in t["logs"]], sort_keys=True)
    before_logs = logsig(task)
    print(f"    subject: {subject!r} ({before_bindings} binding before)")

    with McpStubs(wt, mcp_server, mode="record", sprints=sprints) as st:
        fake = FakeSubprocess()
        with SwapSubprocess(fake):
            out = mcp_server.sync_task_sprints(subject_id)
        created = st.count("create_github_issue")
        closed = st.count("close_github_issue")
    print("\n".join("       " + l for l in out.splitlines()[-10:]))
    check("Done." in out, "real run reports Done.", out.splitlines()[-1])
    check(created >= 1, "at least one issue minted (stub)", str(created))
    check(closed >= 1, "at least one issue closed (stub)", str(closed))

    data2 = mcp_server.load()
    t2 = next(t for t in data2["tasks"] if t["title"] == subject)
    check(len(t2["sprint_issues"]) > before_bindings,
          "bindings grew", f"{before_bindings} -> {len(t2['sprint_issues'])}")
    check(logsig(t2) == before_logs,
          "logs untouched by the reconcile (bar the uploaded_at marker)")

    # Idempotency: a second run has nothing to do and touches nothing.
    disk_before = dst.read_text()
    with McpStubs(wt, mcp_server, mode="strict", sprints=sprints) as st2:
        fake = FakeSubprocess()
        with SwapSubprocess(fake):
            again = mcp_server.sync_task_sprints(subject_id)
        check(st2.count("create_github_issue") == 0, "2nd run: no GitHub write")
        check(fake.calls == [], "2nd run: no subprocess call")
    check("Nothing to do" in again, "2nd run reports nothing to do", again[:160])
    check(dst.read_text() == disk_before, "2nd run leaves the file byte-identical")

    # Undo the synthetic rollback before this file becomes section 13's subject.
    # check_invariants compares against the Phase-0 baseline, which records which
    # binding each *shadow* became; a task whose bindings we deliberately deleted
    # and then re-minted with stub issue refs fails that comparison for reasons
    # that have nothing to do with the MCP layer.
    restored = mcp_server.load()
    rt = next(t for t in restored["tasks"] if t["id"] == subject_id)
    rt.update(pristine)
    mcp_server.save(restored)
    return dst


def test_set_sprint(wt, mcp_server, migrated, scratch):
    section("9. set_sprint now corrects start_sprint")
    point_at(wt, mcp_server, migrated, scratch / "setsprint.json")
    data = mcp_server.load()
    sprints = sprints_of(data, wt)
    # Subject and both sprint names derived: the start sprint is whatever the
    # task's earliest log says, and the correction target is the sprint before it
    # (was pinned to 'IRON Infusion' / 'Sprint 98' / 'Sprint 96').
    task, _per = pick_multi_sprint(wt, data, sprints)
    iron = task["id"]
    start = task.get("start_sprint")
    by_start = sorted(sprints, key=lambda s: s["start_date"])
    idx = next((i for i, s in enumerate(by_start) if s["title"] == start), None)
    earlier = by_start[idx - 1]["title"] if idx else by_start[0]["title"]
    old_sprint_id = task.get("sprint_id")
    old_bindings = copy.deepcopy(task.get("sprint_issues"))
    check(bool(start), f"{task['title'][:34]!r} starts out on {start}", str(start))

    with McpStubs(wt, mcp_server, mode="strict", sprints=sprints):
        out = mcp_server.set_sprint(iron, earlier)
    check(f"now starts in {earlier}" in out, "set_sprint says start sprint", out)
    d2 = mcp_server.load()
    t2 = next(t for t in d2["tasks"] if t["id"] == iron)
    check(t2.get("start_sprint") == earlier, f"start_sprint written ({earlier})",
          str(t2.get("start_sprint")))
    check(t2.get("start_sprint_id"), "start_sprint_id written")
    check(t2.get("sprint_id") == old_sprint_id,
          "legacy sprint_id NOT re-pointed", f"{old_sprint_id} -> {t2.get('sprint_id')}")
    check(t2.get("sprint_issues") == old_bindings, "bindings untouched")

    with McpStubs(wt, mcp_server, mode="strict", sprints=sprints_of(d2, wt)):
        out2 = mcp_server.set_sprint(iron, "none")
    check("Cleared the start sprint" in out2, "none clears it", out2)
    d3 = mcp_server.load()
    t3 = next(t for t in d3["tasks"] if t["id"] == iron)
    check("start_sprint" not in t3 and "start_sprint_id" not in t3, "keys removed")

    with McpStubs(wt, mcp_server, mode="strict", sprints=sprints_of(d3, wt)):
        out3 = mcp_server.set_sprint(iron, "Sprint 9999")
    check("No sprint matching" in out3, "unknown sprint rejected", out3)


def test_close_end_to_end(wt, mcp_server, migrated, scratch):
    section("10. set_task_status(..., 'done') end-to-end (stubbed)")
    dst = point_at(wt, mcp_server, migrated, scratch / "close.json")
    data = mcp_server.load()
    sprints = sprints_of(data, wt)

    # (a) an OPEN multi-sprint task: the reconcile must run inside the close, so
    #     each earlier sprint gets its own issue and hours.
    def unbound_count(t):
        bound = {b.get("sprint_id") for b in t.get("sprint_issues") or []}
        return len([e for e in wt.task_sprints_with_time(t, sprints)
                    if e["sprint_id"] not in bound])
    task = max((t for t in data["tasks"]
                if t.get("status") == "inprogress" and t.get("github_repo")
                and len(wt.task_sprints_with_time(t, sprints)) > 1),
               key=unbound_count)
    title, tid = task["title"], task["id"]
    per_sprint = [e["sprint_title"] for e in wt.task_sprints_with_time(task, sprints)]
    bound_before = {b.get("sprint_id") for b in task.get("sprint_issues") or []}
    unbound = [e for e in wt.task_sprints_with_time(task, sprints)
               if e["sprint_id"] not in bound_before]
    print(f"       task={title[:52]!r} sprints={per_sprint} "
          f"bindings={len(bound_before)} unbound={[e['sprint_title'] for e in unbound]}")

    # Plan with closing=True, which is what close_task passes: a task being
    # closed gets no empty current-sprint binding, so its carried-forward issue
    # lands on the newest sprint *with* time and one fewer issue is minted.
    with McpStubs(wt, mcp_server, mode="strict", sprints=sprints):
        plan = wt.reconcile_task_sprints(task, data, sprints, dry_run=True,
                                         closing=True)
    want_mints = sum(1 for op in plan["planned"]
                     if op["op"] == "create" and op.get("create_issue"))
    want_closes = sum(1 for op in plan["planned"] if op["op"] == "close") + 1

    with McpStubs(wt, mcp_server, mode="record", sprints=sprints) as st:
        fake = FakeSubprocess()
        with SwapSubprocess(fake):
            out = mcp_server.set_task_status(tid, "done")
    print("\n".join("       " + l for l in out.splitlines()))
    check(out.startswith(f"Closed '{title}'"), "close reports success", out[:120])
    check(st.count("create_github_issue") == want_mints,
          f"exactly the planned number of issues minted ({want_mints})",
          str(st.count("create_github_issue")))
    check(st.count("close_github_issue") == want_closes,
          f"closes = planned past-sprint closes + 1 current = {want_closes}",
          str(st.count("close_github_issue")))
    check(fake.calls == [], "no raw gh subprocess call (all via wt)", str(fake.calls[:2]))

    d2 = mcp_server.load()
    t2 = next(t for t in d2["tasks"] if t["id"] == tid)
    check(t2["status"] == "done", "task marked done", t2["status"])
    bound_after = {b.get("sprint_id") for b in t2["sprint_issues"]}
    missing = [e["sprint_title"] for e in wt.task_sprints_with_time(t2, sprints)
               if e["sprint_id"] not in bound_after]
    check(not missing, "every sprint with time now has a binding", str(missing))
    check(all(b.get("state") == "closed" for b in t2["sprint_issues"]),
          "every binding closed",
          str([(b.get("sprint"), b.get("state")) for b in t2["sprint_issues"]]))
    # Each binding's cached hours must equal that sprint's own minutes, not the
    # task total — the double-count the plan is about.
    bad = []
    for b in t2["sprint_issues"]:
        want = wt.mins_to_quarter_hours(
            wt.task_mins_for_sprint(t2, b.get("sprint_id"), sprints))
        if b.get("hours_synced") is None or abs(b["hours_synced"] - want) > 1e-9:
            bad.append((b.get("sprint"), b.get("hours_synced"), want))
    check(not bad, "each binding's hours = that sprint's own minutes", str(bad))
    total_h = wt.mins_to_quarter_hours(wt.task_logged_mins(t2))
    check(sum(b["hours_synced"] for b in t2["sprint_issues"]) >= total_h - 1e-9,
          "per-sprint hours sum to >= the rounded total (round-up-per-sprint)",
          f"{sum(b['hours_synced'] for b in t2['sprint_issues'])} vs {total_h}")
    check("Added to project" in out, "project update reported")
    check("Closed issue:" in out, "current issue close reported")

    # (b) create_issue=False on a repo-having task with no issue must refuse.
    #     Whether such a task exists is a property of the day's data (almost
    #     everything the owner tracks gets linked), so when the fixture has none
    #     the subject is *built*: an open task with a repo and its issue
    #     bindings cleared. `logs` are untouched, and it is a scratch copy.
    d3 = mcp_server.load()
    victim = next((t for t in d3["tasks"]
                   if t.get("github_repo") and t.get("status") != "done"
                   and not wt.task_current_issue(t, d3)), None)
    if victim is None:
        victim = next((t for t in d3["tasks"]
                       if t.get("github_repo") and t.get("status") != "done"), None)
        if victim is None:
            raise SystemExit("fixture has no open task with a github_repo — the "
                             "create_issue refusal path cannot be exercised")
        victim["sprint_issues"] = []
        victim.pop("github_issue", None)
        mcp_server.save(d3)
        d3 = mcp_server.load()
        victim = next(t for t in d3["tasks"] if t["id"] == victim["id"])
        assert not wt.task_current_issue(victim, d3)
    print(f"       (no-issue task: {victim['title']!r})")
    with McpStubs(wt, mcp_server, mode="record", sprints=sprints) as st2:
        fake = FakeSubprocess()
        with SwapSubprocess(fake):
            refused = mcp_server.set_task_status(victim["id"], "done")
    check("has no GitHub issue linked" in refused, "refuses without create_issue",
          refused[:80])
    check(st2.count("create_github_issue") == 0, "and mints nothing",
          str(st2.count("create_github_issue")))
    d4 = mcp_server.load()
    v4 = next(t for t in d4["tasks"] if t["id"] == victim["id"])
    check(v4.get("status") != "done", "task left open", v4.get("status"))

    # (c) create_issue=True mints one and closes.
    with McpStubs(wt, mcp_server, mode="record", sprints=sprints) as st3:
        fake = FakeSubprocess()
        with SwapSubprocess(fake):
            ok = mcp_server.set_task_status(victim["id"], "done", create_issue=True)
    check(st3.count("create_github_issue") >= 1, "create_issue=True mints one",
          str(st3.count("create_github_issue")))
    check("Created issue:" in ok, "and reports it", ok[:120])
    d5 = mcp_server.load()
    v5 = next(t for t in d5["tasks"] if t["id"] == victim["id"])
    check(v5.get("status") == "done", "now done", v5.get("status"))
    check(wt.task_current_issue(v5, d5), "issue recorded on a binding",
          str(v5.get("sprint_issues")))

    # (d) a repo-less task closes with no GitHub interaction at all. Every task in
    #     the real data has a repo, so make one via add_task (also exercises the
    #     add_task binding write path).
    with McpStubs(wt, mcp_server, mode="record", sprints=sprints):
        created = mcp_server.add_task("harness repo-less task", role="other")
    check("Created task" in created, "add_task created a repo-less task", created[:80])
    with McpStubs(wt, mcp_server, mode="strict", sprints=sprints):
        fake = FakeSubprocess()
        with SwapSubprocess(fake):
            plain = mcp_server.set_task_status("harness repo-less task", "done")
    check("no repo — no GitHub integration" in plain, "repo-less close is local-only",
          plain[:120])
    check(fake.calls == [], "and makes no subprocess call")

    # (e) a non-done status change still syncs the project via the current issue.
    d7 = mcp_server.load()
    todo = next(t for t in d7["tasks"]
                if t.get("status") == "todo" and wt.task_current_issue(t, d7))
    todo_ref = wt.task_current_issue(todo, d7)
    with McpStubs(wt, mcp_server, mode="record", sprints=sprints) as st5:
        fake = FakeSubprocess()
        with SwapSubprocess(fake):
            moved = mcp_server.set_task_status(todo["id"], "inprogress")
    check("(project synced)" in moved, "status sync used the current binding's issue",
          moved)
    synced = [a for n, a, k in st5.calls if n == "sync_project_status"]
    check(synced and synced[0][0] == todo_ref,
          "…and with the right ref", f"{synced[:1]} want {todo_ref}")

    # (f) add_task with an explicit issue records it on a binding, not just the key.
    with McpStubs(wt, mcp_server, mode="record", sprints=sprints):
        mcp_server.add_task("harness linked task", role="other",
                            github_issue="grafana/field-eng#4321")
    d8 = mcp_server.load()
    lt = next(t for t in d8["tasks"] if t["title"] == "harness linked task")
    check(any(b.get("issue") == "grafana/field-eng#4321"
              for b in lt.get("sprint_issues") or []),
          "add_task(github_issue=...) writes a binding", str(lt.get("sprint_issues")))
    check(wt.task_current_issue(lt, d8) == "grafana/field-eng#4321",
          "…and task_current_issue sees it")
    return dst


def test_link_unlink_push(wt, mcp_server, migrated, scratch):
    section("11. link / unlink / push / notes / rename / delete")
    point_at(wt, mcp_server, migrated, scratch / "link.json")
    data = mcp_server.load()
    sprints = sprints_of(data, wt)
    task = next(t for t in data["tasks"] if t["title"] == "Compliance Week")
    before = wt.task_current_issue(task, data)
    print(f"       (Compliance Week current issue: {before})")

    # link
    with McpStubs(wt, mcp_server, mode="record", sprints=sprints):
        fake = FakeSubprocess({"gh issue view": (0, json.dumps(
            {"number": 4242, "title": "stub issue"}))})
        with SwapSubprocess(fake):
            out = mcp_server.link_github_issue("Compliance Week", "grafana/field-eng#4242")
    check("Linked 'Compliance Week'" in out, "link reports success", out)
    check(not fake.writes(), "link made no gh write call", str(fake.writes()))
    d2 = mcp_server.load()
    t2 = next(t for t in d2["tasks"] if t["title"] == "Compliance Week")
    check(wt.task_current_issue(t2, d2) == "grafana/field-eng#4242",
          "task_current_issue reflects the link", str(wt.task_current_issue(t2, d2)))
    check(any(b.get("issue") == "grafana/field-eng#4242"
              for b in t2.get("sprint_issues") or []),
          "…and it landed on a binding, not just the flat key",
          str(t2.get("sprint_issues")))
    check(t2.get("github_issue") == "grafana/field-eng#4242",
          "legacy mirror still written (other consumers read it)")

    # unlink
    with McpStubs(wt, mcp_server, mode="record", sprints=sprints):
        out2 = mcp_server.unlink_github_issue("Compliance Week")
    check("Unlinked 'Compliance Week' from grafana/field-eng#4242" in out2,
          "unlink reports the removed ref", out2)
    d3 = mcp_server.load()
    t3 = next(t for t in d3["tasks"] if t["title"] == "Compliance Week")
    check(wt.task_current_issue(t3, d3) is None, "no current issue afterwards",
          str(wt.task_current_issue(t3, d3)))
    check("github_issue" not in t3, "legacy key dropped")
    check("not linked to a GitHub issue" in mcp_server.unlink_github_issue("Compliance Week"),
          "second unlink is a clean no-op")

    # unlink on a multi-binding task reports the remaining past-sprint issues
    d4 = mcp_server.load()
    multi = next(t for t in d4["tasks"]
                 if len([b for b in (t.get("sprint_issues") or []) if b.get("issue")]) > 2)
    out3 = mcp_server.unlink_github_issue(multi["id"])
    check("Still bound to" in out3, "remaining past-sprint issues named",
          out3.replace("\n", " | ")[:180])
    print("       " + out3.replace("\n", " | ")[:200])

    # push — hours must be the current sprint's, not the task total
    d5 = mcp_server.load()
    # A task whose sprint-filtered hours differ sharply from its total: IRON
    # Infusion has 66h across 5 sprints, only 3h of which is its current sprint's.
    ptask = next(t for t in d5["tasks"] if t["title"] == "IRON Infusion")
    with McpStubs(wt, mcp_server, mode="record", sprints=sprints) as st:
        fake = FakeSubprocess()
        with SwapSubprocess(fake):
            pout = mcp_server.push_task_to_github(ptask["id"])
    check(pout.startswith("Pushed "), "push reports success", pout)
    reported = float(pout.rsplit(": ", 1)[1].rstrip("h"))
    total_h = wt.mins_to_quarter_hours(wt.task_logged_mins(ptask))
    sprint_h = wt.mins_to_quarter_hours(wt.task_reportable_mins(ptask, sprints))
    check(abs(reported - sprint_h) < 1e-9, "push reports the sprint-filtered hours",
          f"reported={reported} sprint={sprint_h} total={total_h}")
    check(reported != total_h, "…which really differs from the task total",
          f"{reported} vs {total_h}")
    print(f"       pushed {reported}h (task total would be {total_h}h)")
    hours_pushed = [a[1] for n, a, k in st.calls if n == "update_project_hours"]
    check(hours_pushed and abs(hours_pushed[-1] - sprint_h) < 1e-9,
          "the value actually sent to GitHub matches", str(hours_pushed))

    check("has no linked GitHub issue" in mcp_server.push_task_to_github("Compliance Week"),
          "push refuses without an issue")

    # get_notes_path uses the current binding
    d6 = mcp_server.load()
    nt = next(t for t in d6["tasks"] if wt.task_current_issue(t, d6))
    nout = mcp_server.get_notes_path(nt["id"])
    check(wt.task_current_issue(nt, d6) in nout, "get_notes_path names the current issue",
          nout[:120])

    # rename updates the current binding's issue title only
    with McpStubs(wt, mcp_server, mode="record", sprints=sprints):
        fake = FakeSubprocess({"gh issue edit": (0, "")})
        with SwapSubprocess(fake):
            rout = mcp_server.rename_task(nt["id"], "renamed by harness")
    check("Updated GitHub issue" in rout, "rename updated one issue", rout)
    edits = [c for c in fake.calls if "edit" in c]
    check(len(edits) == 1, "exactly one gh issue edit (not one per binding)",
          str(len(edits)))
    want_repo, want_num = wt.task_current_issue(nt, d6).split("#")
    check(want_repo in edits[0] and want_num in edits[0],
          "…and it was the current issue",
          f"{edits[0]} want {want_repo}#{want_num}")

    # delete_task deletes only the current issue, names the rest
    d7 = mcp_server.load()
    dtask = next(t for t in d7["tasks"]
                 if len([b for b in (t.get("sprint_issues") or []) if b.get("issue")]) > 2)
    n_before = len(d7["tasks"])
    with McpStubs(wt, mcp_server, mode="record", sprints=sprints) as st2:
        dout = mcp_server.delete_task(dtask["id"])
    check(st2.count("delete_github_issue") == 1, "exactly one issue deleted",
          str(st2.count("delete_github_issue")))
    check("past-sprint issue(s) left in place" in dout, "the rest are named",
          dout.replace("\n", " | ")[:200])
    check(len(mcp_server.load()["tasks"]) == n_before - 1, "task removed")


def test_read_only_tools(wt, mcp_server, migrated, baseline, scratch):
    section("12. list_sprints / get_current_sprint_info / get_status")
    dst = point_at(wt, mcp_server, migrated, scratch / "read.json")
    data = mcp_server.load()
    sprints = sprints_of(data, wt)
    disk_before = dst.read_text()
    # The current sprint is resolved from the cache, not spelled out: it rolls
    # over every two weeks, so 'Sprint 105' was a two-week-lived assertion.
    cur = wt.find_sprint_for_date(sprints, datetime.now().date())

    with McpStubs(wt, mcp_server, mode="strict", sprints=sprints):
        ls = mcp_server.list_sprints()
        info = mcp_server.get_current_sprint_info()
        status = mcp_server.get_status()
    check(cur is not None and cur["title"] in ls and "← current" in ls,
          f"list_sprints marks the current sprint ({cur and cur['title']})",
          [l for l in ls.splitlines() if "current" in l])
    check(ls.count("\n  Sprint") == len(sprints), f"lists all {len(sprints)} sprints",
          str(ls.count("\n  Sprint")))
    check(cur is not None and f"Current sprint: {cur['title']}" in info,
          "get_current_sprint_info", info[:60])
    check("Duration: 14 days" in info, "duration derived from date objects",
          info.replace("\n", " | "))
    check(dst.read_text() == disk_before, "read-only tools change nothing")

    # get_status total must equal the invariant total (no shadow double-count).
    total_line = status.splitlines()[0]
    check(f"{_WANT[0]} tasks" in total_line,
          f"get_status counts {_WANT[0]} tasks", total_line)
    at = data.get("active_timer")
    mins = sum(mcp_server.task_logged_mins(t) + mcp_server.task_live_mins(t, at)
               for t in data["tasks"])
    check(f"{int(mins // 60)}h" in total_line,
          "get_status total = sum over every task (no shadow double-count)",
          f"{total_line}  (expected {int(mins // 60)}h)")
    logged_only = sum(l.get("minutes", 0)
                      for t in data["tasks"] for l in t.get("logs", []))
    want = json.loads(Path(baseline).read_text())["total_minutes_excluding_shadows"]
    check(abs(logged_only - want) < 1e-6,
          f"…and the logged part is still the baseline total ({want})",
          str(logged_only))
    print(f"       (active timer contributes {mins - logged_only:.1f}m live)")
    print(f"       {total_line}")

    # Offline path: no cache-vs-live key mismatch (the old startDate KeyError).
    with McpStubs(wt, mcp_server, mode="strict", sprints=sprints):
        saved = wt.get_all_sprints
        wt.get_all_sprints = lambda d: []
        mcp_server.get_all_sprints = lambda d: []
        try:
            ls2 = mcp_server.list_sprints()
            info2 = mcp_server.get_current_sprint_info()
        finally:
            wt.get_all_sprints = saved
            mcp_server.get_all_sprints = saved
    check("offline" in ls2 and cur is not None and cur["title"] in ls2,
          "list_sprints works from the cache alone", ls2.splitlines()[0])
    check(cur is not None and f"Current sprint: {cur['title']}" in info2,
          "get_current_sprint_info works from the cache alone", info2[:60])

    # close_previous_recurrent_tasks dry run reads issues via bindings.
    with McpStubs(wt, mcp_server, mode="strict", sprints=sprints):
        cout = mcp_server.close_previous_recurrent_tasks(dry_run=True)
    check("None" not in cout.replace("Nothing", ""), "dry run lists real issue refs",
          cout.replace("\n", " | ")[:220])
    print("       " + cout.replace("\n", " | ")[:220])


def test_invariants(wt, worked, baseline):
    section("13. tools/check_invariants.py on a worked-on copy")
    env = dict(os.environ, WT_DATA_FILE=str(worked))
    proc = real_subprocess.run(
        [sys.executable, str(REPO / "tools" / "check_invariants.py"),
         str(worked), str(baseline)],
        capture_output=True, text=True, env=env)
    head = [l for l in proc.stdout.splitlines() if "minutes=" in l]
    print("\n".join("       " + l for l in head))
    check(proc.returncode == 0, "check_invariants exits 0",
          f"rc={proc.returncode} {proc.stdout[-400:]}")
    # Totals come from the Phase-0 baseline snapshot rather than a constant
    # (26620.71 / 392) frozen on the day this was written — which went stale the
    # next time any time was logged.
    snap = json.loads(Path(baseline).read_text())
    want_mins = snap["total_minutes_excluding_shadows"]
    want_logs = snap["total_log_count_excluding_shadows"]
    check(any(f"minutes={want_mins}" in l for l in head),
          f"minutes unchanged ({want_mins})", str(head))
    check(any(f"logs={want_logs}" in l for l in head),
          f"log count unchanged ({want_logs})", str(head))


def test_other_harnesses(fixture, migrated, baseline, scratch):
    section("14. tools/test_phase3.py and tools/test_reconcile.py still pass")
    for name in ("test_phase3.py", "test_reconcile.py"):
        sub = scratch / name.replace(".py", "")
        sub.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, WT_DATA_FILE=str(sub / "unused.json"))
        proc = real_subprocess.run(
            [sys.executable, str(REPO / "tools" / name),
             str(fixture), str(migrated), str(baseline), str(sub)],
            capture_output=True, text=True, env=env)
        tail = [l for l in proc.stdout.strip().splitlines() if "checks passed" in l]
        print("\n".join("       " + l for l in tail[-2:]))
        check(proc.returncode == 0, f"{name} exits 0",
              f"rc={proc.returncode} {proc.stdout[-500:]}")


def main():
    if len(sys.argv) != 5:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    fixture, migrated, baseline, scratch = (Path(a).expanduser() for a in sys.argv[1:])
    scratch.mkdir(parents=True, exist_ok=True)

    os.environ["WT_DATA_FILE"] = str(scratch / "unused.json")
    import wt
    from test_reconcile import expected_task_count
    _WANT[0] = expected_task_count(wt, fixture)

    import mcp_server

    live = Path.home() / ".workload_tracker.json"
    for mod in (wt, mcp_server):
        if mod.DATA_FILE == live:
            print(f"REFUSING TO RUN: {mod.__name__}.DATA_FILE is the live file",
                  file=sys.stderr)
            return 2

    test_import_and_shape(wt, mcp_server)
    test_load_migrates(wt, mcp_server, fixture, scratch)
    test_list_tasks(wt, mcp_server, migrated, scratch)
    test_get_task(wt, mcp_server, migrated, scratch)
    test_sync_dry_run(wt, mcp_server, migrated, scratch)
    test_requirement_a(wt, mcp_server, migrated, scratch)
    test_recurrent_reconciles(wt, mcp_server, migrated, scratch)
    worked = test_sync_real_run(wt, mcp_server, migrated, scratch)
    test_set_sprint(wt, mcp_server, migrated, scratch)
    test_close_end_to_end(wt, mcp_server, migrated, scratch)
    test_link_unlink_push(wt, mcp_server, migrated, scratch)
    test_read_only_tools(wt, mcp_server, migrated, baseline, scratch)
    test_invariants(wt, worked, baseline)
    test_other_harnesses(fixture, migrated, baseline, scratch)

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  x {f}")
        return 1
    print("All MCP Phase 3 checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
