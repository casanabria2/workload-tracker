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

def expected_task_count(wt, fixture):
    """Tasks a fully-migrated *fixture* should end up with.

    Derived, not hardcoded: the shadow migration drops every task carrying
    cross_sprint_parent, and the Phase 5 merge collapses each known recurrent
    series to a single task. Hardcoding a number here just means the harness
    breaks every time a migration legitimately changes the shape.
    """
    import json as _json
    from pathlib import Path as _Path
    raw = _json.loads(_Path(fixture).read_text())
    tasks = raw.get("tasks", [])
    shadows = sum(1 for t in tasks if t.get("cross_sprint_parent"))
    series = {}
    for t in tasks:
        if t.get("cross_sprint_parent"):
            continue
        canon = wt.recurrent_series_for_title(t.get("title", ""))
        if canon:
            series[canon] = series.get(canon, 0) + 1
    merged_away = sum(n - 1 for n in series.values() if n > 1)
    return len(tasks) - shadows - merged_away


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


# ------------------------------------------------------------ scenario builders --
#
# These harnesses run against a *copy of the live data file*, and that file keeps
# moving: `wt sync-sprints --all` reconciles tasks (so the plan a test used to
# assert becomes an empty plan), logs get edited or deleted, and the current
# sprint advances every two weeks. Pinning an assertion to "Assist on Banco
# Galicia mints an issue for Sprint 95" therefore expires the first time the
# owner closes a sprint — which is exactly how this harness rotted (it was
# written when Banco Galicia was un-reconciled and the current sprint was 104).
#
# The fix is not to re-pin to today's values. It is to pick the subject task out
# of whatever the fixture contains, rebuild the *precondition* the behaviour
# needs, and derive the expectation from that rebuild. Nothing below hardcodes a
# task title, sprint name, issue number or minute total.

def sprint_time(wt, task, sprints):
    """``[{sprint_id, sprint, minutes}]`` for sprints with logged time, oldest first."""
    return [
        {"sprint_id": e["sprint_id"], "sprint": e["sprint_title"],
         "minutes": e["total_mins"]}
        for e in wt.task_sprints_with_time(task, sprints)
    ]


def multi_sprint_tasks(wt, data, sprints, *, status=None, min_sprints=2,
                       need_repo=True, need_issue=True):
    """Fixture tasks whose *logs* span at least *min_sprints* sprints.

    Ordered most-spanning first, then by title, so the pick is deterministic for
    a given fixture but never tied to one particular task surviving.
    """
    out = []
    for t in data["tasks"]:
        if status is not None and t.get("status") != status:
            continue
        if t.get("status") == "recurrent":
            continue          # perpetual series: no carry-forward, different rules
        if need_repo and not wt.get_task_repo(t):
            continue
        if need_issue and not t.get("github_issue"):
            continue
        per = sprint_time(wt, t, sprints)
        if len(per) >= min_sprints:
            out.append((t, per))
    out.sort(key=lambda p: (-len(p[1]), p[0].get("title", "")))
    return out


def pick_multi_sprint(wt, data, sprints, **kw):
    """First task from :func:`multi_sprint_tasks`, or SystemExit with a reason.

    A hard exit rather than a skipped check: if the fixture contains no
    cross-sprint task at all, the harness is not testing what its name claims and
    that must be loud.
    """
    cands = multi_sprint_tasks(wt, data, sprints, **kw)
    if not cands:
        raise SystemExit(
            f"fixture has no task matching {kw!r} with logged time in "
            "2+ sprints — cannot exercise the cross-sprint path"
        )
    return cands[0]


def unreconcile(wt, task, sprints, *, anchor="oldest"):
    """Roll *task* back to the one-issue-per-task shape reconcile exists to fix.

    Collapses ``sprint_issues`` down to a single binding carrying the task's own
    ``github_issue``, anchored at the oldest (or newest) sprint that has logged
    time, and points the legacy ``sprint``/``sprint_id`` mirror at it. This is
    the state every cross-sprint task was in before it was first reconciled:
    real work in several sprints, one long-lived issue.

    ``logs`` are never touched, so the derived target state is unchanged — only
    the bookkeeping that reconcile is supposed to reproduce is removed.

    Returns ``{issue, anchor, anchor_id, targets, latest, latest_id}``.
    """
    per = sprint_time(wt, task, sprints)
    assert len(per) >= 2, f"{task.get('title')!r} is not multi-sprint"
    pick = per[0] if anchor == "oldest" else per[-1]
    issue = task.get("github_issue")
    assert issue, f"{task.get('title')!r} has no legacy issue to carry"
    task["sprint_issues"] = [{
        "sprint_id": pick["sprint_id"],
        "sprint": pick["sprint"],
        "issue": issue,
        "state": "closed" if task.get("status") == "done" else "open",
        "hours_synced": None,
        "synced_at": None,
        "created_at": task.get("created_at"),
    }]
    task["sprint_id"] = pick["sprint_id"]
    task["sprint"] = pick["sprint"]
    return {
        "issue": issue,
        "anchor": pick["sprint"], "anchor_id": pick["sprint_id"],
        "targets": per,
        "latest": per[-1]["sprint"], "latest_id": per[-1]["sprint_id"],
    }


def current_sprint(wt, sprints):
    return wt.find_sprint_for_date(sprints, datetime.now().date())


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

    # Two closed cross-sprint tasks, chosen from the fixture. A `done` task has
    # no current-sprint reservation, so its target set is exactly "the sprints it
    # has time in" and the expected plan is fully determined by the logs.
    cands = multi_sprint_tasks(wt, data, sprints, status="done")
    if len(cands) < 2:
        raise SystemExit("fixture has fewer than two closed cross-sprint tasks")
    subjects = cands[:2]

    # Every one of these has long since been reconciled for real (the owner runs
    # `wt sync-sprints --all` at each sprint boundary), so the interesting plan
    # only exists if the pre-reconcile state is rebuilt first.
    rolled = {}
    for task, _per in subjects:
        rolled[task["id"]] = unreconcile(wt, task, sprints, anchor="oldest")

    before = sig(data)
    with Stubs(wt, mode="strict", sprints=sprints) as st:
        for task, _per in subjects:
            info = rolled[task["id"]]
            res = wt.reconcile_task_sprints(task, data, sprints, dry_run=True)
            title = task["title"]
            print(f"    {title}  (status={task['status']}, issue={info['issue']}, "
                  f"rolled back to a single {info['anchor']} binding)")
            print(f"      target: "
                  + ", ".join(f"{t['sprint']}={t['minutes']:.0f}m/{t['hours']}h"
                              for t in res["target"]))
            print(brief(res))

            creates = [o for o in res["planned"] if o["op"] == "create"]
            repoints = [o for o in res["planned"] if o["op"] == "repoint"]
            short = title[:34]

            # Expectation derived from the logs: Option A carries the task's one
            # issue to its most recent sprint and mints an issue for each sprint
            # left behind — including the one the issue vacated.
            want_creates = [t["sprint"] for t in info["targets"][:-1]]
            check([o["sprint"] for o in creates] == want_creates,
                  f"{short}…: mints one issue per sprint left behind "
                  f"({len(want_creates)})",
                  f"got {[o['sprint'] for o in creates]} want {want_creates}")
            check(len(repoints) == 1
                  and repoints[0]["issue"] == info["issue"]
                  and repoints[0]["sprint"] == info["latest"]
                  and repoints[0].get("from_sprint") == info["anchor"],
                  f"{short}…: carries {info['issue']} forward to "
                  f"{info['latest']} (Option A)",
                  f"got {repoints}")
            check(creates and all(
                      o["issue_title"].endswith(f"({o['sprint']})") for o in creates),
                  f"{short}…: past-sprint issues keep the ' (Sprint N)' title suffix",
                  str([o["issue_title"] for o in creates]))
            check(all(o["hours"] == wt.mins_to_quarter_hours(t["minutes"])
                      for o, t in zip(creates, info["targets"][:-1])),
                  f"{short}…: each minted issue carries only its own sprint's hours",
                  str([(o['sprint'], o['hours']) for o in creates]))
        check(st.calls == [], "no GitHub calls while planning")
    check(sig(data) == before, "no mutation while planning")

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


def marker_log_tasks(wt, data):
    """Titles of fixture tasks carrying a 0-minute 'Sprint rollover marker' log.

    Selected from the fixture rather than listed: the markers are leftovers of a
    retired ritual, so their number only ever goes down as the owner tidies logs.
    """
    out = []
    for t in data["tasks"]:
        if any(l.get("minutes", 0) == 0 and "rollover marker" in (l.get("note") or "")
               for l in t.get("logs", []) or []):
            out.append(t["title"])
    return sorted(out)


def test_marker_logs(wt, migrated, scratch):
    section("4. marker-log independence (plan §1.3)")
    # Every 0-minute "Sprint rollover marker" log left in the fixture. The point
    # of the section is that reconcile derives its target set from *minutes*, so
    # a marker changes nothing — whichever tasks still carry one.
    probe = load_copy(wt, migrated, scratch / "marker.json")
    titles = marker_log_tasks(wt, probe)
    check(bool(titles), f"fixture still carries marker logs ({len(titles)} task(s))",
          "no 0-minute rollover markers left — this section is now vacuous")
    for title in titles:
        data = load_copy(wt, migrated, scratch / "marker.json")
        sprints = wt.get_cached_sprints(data)
        task = find(data, title)
        markers = [l for l in task["logs"]
                   if l.get("minutes", 0) == 0
                   and "rollover marker" in (l.get("note") or "")]
        marker_sprints = {
            (wt.find_sprint_for_date(
                sprints, datetime.fromtimestamp(wt.log_effective_date(l)).date()) or {})
            .get("title")
            for l in markers
        } - {None}
        with Stubs(wt, mode="strict", sprints=sprints):
            res_with = wt.reconcile_task_sprints(task, data, sprints, dry_run=True)
        short = title[:38]
        print(f"    {title[:60]}  status={task['status']}  markers={len(markers)}")
        print(brief(res_with))
        check(len(markers) >= 1, f"{short}…: fixture really has a marker log")

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
        # The marker's own sprint, derived from its timestamp — a 0-minute log
        # must never be enough on its own to mint that sprint an issue.
        minted = {o["sprint"] for o in res_with["planned"] if o["op"] == "create"}
        with_time = {e["sprint"] for e in sprint_time(wt, task, sprints)}
        check(not (minted & (marker_sprints - with_time)),
              f"{short}…: no issue minted for a marker-only sprint "
              f"({', '.join(sorted(marker_sprints)) or 'n/a'})",
              f"minted={sorted(minted)} marker_sprints={sorted(marker_sprints)}")

        # Same plan with the marker log deleted.
        task["logs"] = [l for l in task["logs"] if l not in markers]
        with Stubs(wt, mode="strict", sprints=sprints):
            res_without = wt.reconcile_task_sprints(task, data, sprints, dry_run=True)
        check(res_with["planned"] == res_without["planned"],
              f"{short}…: deleting the marker log does not change the plan",
              f"\n      with:    {[o['op'] for o in res_with['planned']]}"
              f"\n      without: {[o['op'] for o in res_without['planned']]}")

    # The counterpart to a marker: a *real* sub-quarter-hour log is not a marker
    # and must be billed, rounded up per sprint. This used to be pinned to a
    # 0.13-minute Timer session that happened to exist on 'Document current FE
    # platform' in Sprint 104; the owner has since deleted that stray 8-second
    # entry, which silently turned the assertion into a permanent failure. It is
    # injected now, so the arithmetic is tested regardless of what the live logs
    # happen to contain.
    data = load_copy(wt, migrated, scratch / "marker2.json")
    sprints = wt.get_cached_sprints(data)
    task, _per = pick_multi_sprint(wt, data, sprints)
    if task.get("status") == "done":
        task["status"] = "inprogress"      # an open task reserves the current sprint
    info = unreconcile(wt, task, sprints, anchor="oldest")
    today = datetime.now().date()
    used = {e["sprint_id"] for e in sprint_time(wt, task, sprints)}
    spare = next((s for s in sorted(sprints, key=lambda s: s["start_date"], reverse=True)
                  if s["id"] not in used and s.get("end_date")
                  and s["end_date"] <= today), None)
    if spare is None:
        raise SystemExit("fixture has no spare ended sprint to inject a tiny log into")
    stamp = datetime.combine(spare["start_date"], datetime.min.time()).timestamp() + 3600
    task["logs"].append({"id": wt.uid(), "minutes": 0.13, "note": "Timer session",
                         "at": stamp, "started_at": stamp, "ended_at": stamp + 8})
    with Stubs(wt, mode="strict", sprints=sprints):
        res = wt.reconcile_task_sprints(task, data, sprints, dry_run=True)
    print(f"    {task['title'][:60]}  status={task['status']}  "
          f"+0.13m real log in {spare['title']}")
    print(brief(res))
    tiny = wt.bucket_logs_by_sprint(task, sprints).get(spare["id"], [])
    check(len(tiny) == 1 and tiny[0].get("minutes", 0) > 0
          and "marker" not in (tiny[0].get("note") or ""),
          f"the injected {spare['title']} log is a real Timer session, not a marker",
          str(tiny))
    check(any(o["op"] == "create" and o["sprint"] == spare["title"]
              and o["hours"] == 0.25 for o in res["planned"]),
          "round-up-per-sprint (preserved by design) bills that 0.13m as 0.25h",
          str([(o["sprint"], o.get("hours")) for o in res["planned"]
               if o["op"] == "create"]))
    check(res["current_sprint"] in {o["sprint"] for o in res["planned"]
                                    if o["op"] == "repoint"},
          "an open task's current issue is carried forward to the current sprint",
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
    # An *open* cross-sprint task, rolled back to the one-issue shape it had
    # before it was ever reconciled — otherwise there is nothing left for the
    # close to do and the section asserts an empty plan. Pinning this to one task
    # title plus the sprint names of the day is what broke it (it expected
    # Sprint 104 to be the newest sprint with time; a stray 8-second log there
    # was later deleted, and the current sprint has since rolled over twice).
    task, _per = pick_multi_sprint(wt, data, sprints)
    if task.get("status") == "done":
        task["status"] = "inprogress"
    info = unreconcile(wt, task, sprints, anchor="oldest")
    issue = info["issue"]
    want_bindings = {t["sprint"] for t in info["targets"]}
    last = info["latest"]
    print(f"    subject: {task['title']!r} (issue {issue}, "
          f"time in {len(info['targets'])} sprints, rolled back to {info['anchor']})")
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
    want = expected_task_count(wt, migrated)
    check(len(data["tasks"]) == want, "no unexpected tasks added or removed",
          f'{len(data["tasks"])} vs expected {want}')
    bound = {b["sprint"]: b for b in task["sprint_issues"]}
    # close_task passes closing=True, so no empty binding is reserved for the
    # current sprint. The task's long-lived issue lands on the newest sprint that
    # actually has time instead of reporting 0h against a sprint it was never
    # worked in — so the binding set is exactly "the sprints with logged time".
    current = current_sprint(wt, sprints)
    check(set(bound) == want_bindings,
          "a binding per sprint with time, and no empty current-sprint binding",
          f"{sorted(bound)} vs want {sorted(want_bindings)}")
    check(current is None or current["title"] not in bound
          or current["title"] in want_bindings,
          "the current sprint is bound only if it has logged time",
          str(sorted(bound)))
    check(bound[last]["issue"] == issue,
          f"the original issue lands on the newest sprint with time ({last}, Option A)",
          str(bound[last]))
    check(st.count("create_github_issue") == len(want_bindings) - 1,
          f"one issue minted per sprint left behind ({len(want_bindings) - 1}); "
          f"{last} took the carried-forward issue",
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
    current = current_sprint(wt, sprints)
    if current is None:
        raise SystemExit("today falls in no cached sprint — cannot run section 10")
    # Subject: an open task whose logged time is entirely in *ended* sprints, so
    # the current sprint is a pure carry-forward target. Rolled back to one
    # binding on its newest worked sprint, which is where a task sits before its
    # first reconcile of a new sprint.
    cands = [(t, per) for t, per in
             multi_sprint_tasks(wt, data, sprints, min_sprints=1)
             if t.get("status") not in ("done", "recurrent")
             and all(e["sprint_id"] != current["id"] for e in per)]
    if not cands:
        raise SystemExit("fixture has no open task worked only in ended sprints")
    task, per = cands[0]
    info = unreconcile(wt, task, sprints, anchor="newest") if len(per) > 1 else None
    if info is None:
        task["sprint_issues"] = [{
            "sprint_id": per[0]["sprint_id"], "sprint": per[0]["sprint"],
            "issue": task["github_issue"], "state": "open",
            "hours_synced": None, "synced_at": None,
            "created_at": task.get("created_at"),
        }]
    issue = task["github_issue"]
    vacated = per[-1]["sprint"]
    # Simulate `wt set-sprint <task> <current>`: sprint_id jumps forward while the
    # binding stays behind on the worked sprint, still holding the live issue.
    task["sprint_id"] = current["id"]
    task["sprint"] = current["title"]
    print(f"    subject: {task['title'][:52]!r}  issue={issue}  "
          f"binding on {vacated}, sprint_id set to {current['title']}")
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
    check([o["sprint"] for o in creates] == [e["sprint"] for e in per],
          f"every sprint it vacated ({vacated} + {len(per) - 1} older) gets its "
          "own past-sprint issue",
          str([o["sprint"] for o in creates]))


def test_pre_migration(wt, fixture, scratch):
    section("9. reconcile on a pre-migration fixture (load() migrates first)")
    data = load_copy(wt, fixture, scratch / "pre.json")
    want = expected_task_count(wt, fixture)
    check(len(data["tasks"]) == want and not any(t.get("cross_sprint_parent")
                                                for t in data["tasks"]),
          f"load() migrated the fixture to {want} tasks with 0 shadows",
          f'{len(data["tasks"])} tasks'
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

    # A closed cross-sprint task rolled back to a single issue on its *newest*
    # sprint — the shape the guard exists for. Narrowing that one issue to its
    # own sprint while the older sprints have nowhere to report would delete the
    # difference from the project's reporting. (Originally pinned to 'Assist on
    # Banco Galicia' / Sprint 95; that task has since been reconciled for real,
    # which made the precondition unreachable and the assertions permanently red.)
    task, _per = pick_multi_sprint(wt, data, sprints, status="done")
    info = unreconcile(wt, task, sprints, anchor="newest")
    per = {e["sprint"]: e["minutes"] for e in info["targets"]}
    deferred = [e["sprint"] for e in info["targets"][:-1]]
    print(f"    subject: {task['title'][:52]!r}  issue={info['issue']}")
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
    check([e["sprint"] for e in held["unbillable"]] == deferred,
          f"naming every sprint whose time has no issue ({', '.join(deferred)})",
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

    # A single-sprint task has nothing unbillable, so it is unaffected. Picked
    # from the fixture; its cached hours are cleared so an hours op is guaranteed
    # to be planned (otherwise the assertion depends on the live sync state).
    solo = next((t for t in data["tasks"]
                 if t.get("status") not in ("recurrent",)
                 and wt.get_task_repo(t)
                 and len(sprint_time(wt, t, sprints)) == 1
                 and any(b.get("issue") for b in t.get("sprint_issues") or [])), None)
    if solo is None:
        raise SystemExit("fixture has no single-sprint task with a linked issue")
    for b in solo["sprint_issues"]:
        b["hours_synced"] = None
    print(f"    single-sprint control: {solo['title'][:52]!r}")
    with Stubs(wt, mode="strict", sprints=sprints):
        r = wt.reconcile_task_sprints(solo, data, sprints, dry_run=True,
                                      create_issues=False)
    check(r["unbillable"] == [],
          "a single-sprint task is not affected by the guard", str(r["unbillable"]))
    check(any(o["op"] == "hours" for o in r["planned"]),
          "and still syncs its hours", str(r["planned"]))

    # close_task always mints, so the guard must never withhold on a close.
    closing = task
    with Stubs(wt, mode="strict", sprints=sprints):
        rc = wt.reconcile_task_sprints(closing, data, sprints, dry_run=True,
                                       closing=True)
    check(rc["unbillable"] == [],
          "a close (closing=True, create_issues default) withholds nothing",
          str(rc["unbillable"]))


def test_project_info_cache(wt, migrated, scratch):
    section("13. project metadata is fetched once per run, not once per task")
    import json as _json
    import types as _types

    class _CP:
        def __init__(self, out):
            self.returncode, self.stdout, self.stderr = 0, out, ""

    FIELDS = _json.dumps({"fields": [
        {"name": "Status", "id": "F1",
         "options": [{"name": "Done", "id": "o1"}, {"name": "In Progress", "id": "o2"}]},
        {"name": "Hours", "id": "F2"},
        {"name": "Activity", "id": "F3", "options": []},
        {"name": "Type", "id": "F4", "options": []},
        {"name": "Sprint", "id": "F5"}]})

    def fake_gh(log):
        def run(argv, *a, **k):
            log.append(" ".join(argv[:3]))
            if argv[:3] == ["gh", "project", "view"]:
                return _CP(_json.dumps({"id": "PVT_x"}))
            if argv[:3] == ["gh", "project", "field-list"]:
                return _CP(FIELDS)
            if argv[:3] == ["gh", "project", "item-add"]:
                return _CP(_json.dumps({"id": "PVTI_x"}))
            return _CP("{}")
        return _types.SimpleNamespace(run=run, PIPE=None)

    real_sub, real_ttl = wt.subprocess, wt.PROJECT_INFO_TTL_SECONDS
    try:
        data = load_copy(wt, migrated, scratch / "pinfo.json")

        # Repeated calls collapse to one fetch (two gh calls).
        log = []
        wt.subprocess = fake_gh(log)
        wt.clear_project_info_cache()
        for _ in range(20):
            wt.get_project_info(data)
        check(len(log) == 2, "20 get_project_info() calls make 2 gh calls", str(len(log)))

        wt.get_project_info(data, refresh=True)
        check(len(log) == 4, "refresh=True forces a re-fetch", str(len(log)))
        wt.clear_project_info_cache()
        wt.get_project_info(data)
        check(len(log) == 6, "clear_project_info_cache() forces a re-fetch", str(len(log)))

        # A cache hit must still return the same metadata.
        a = wt.get_project_info(data)
        b = wt.get_project_info(data)
        check(a == b and a.get("project_id") == "PVT_x",
              "a cache hit returns identical metadata", str(a.get("project_id")))

        # Expiry: a non-positive TTL always re-fetches.
        log.clear()
        wt.PROJECT_INFO_TTL_SECONDS = -1
        wt.clear_project_info_cache()
        for _ in range(3):
            wt.get_project_info(data)
        check(len(log) == 6, "an expired entry re-fetches", str(len(log)))
        wt.PROJECT_INFO_TTL_SECONDS = real_ttl

        # Failures must never be cached, or one blip poisons the whole run.
        attempts = []
        def boom(argv, *a, **k):
            attempts.append(1)
            raise RuntimeError("network down")
        wt.subprocess = _types.SimpleNamespace(run=boom, PIPE=None)
        wt.clear_project_info_cache()
        for _ in range(3):
            try:
                wt.get_project_info(data)
            except Exception:
                pass
        check(len(attempts) == 3, "a failed fetch is not cached", str(len(attempts)))

        # The regression that mattered: a whole-run reconcile must not scale its
        # metadata fetches with the task count. `wt sync-sprints --all` doing so
        # exhausted the 5000-point GraphQL budget mid-run.
        #
        # The precondition is *constructed*, not hoped for: metadata is only
        # fetched by a task that actually has an operation to perform, so on a
        # fully-reconciled fixture (which the live file is, the morning after the
        # sprint ritual) both runs fetch once and the memoisation claim is
        # unobservable — "2 vs 2" passed the cached check and failed the
        # comparison. Rolling several cross-sprint tasks back to the one-issue
        # shape guarantees several metadata users on any fixture.
        sprints = wt.get_cached_sprints(data)
        tasks = [t for t in data["tasks"] if t.get("status") != "recurrent"]
        counts = {}
        n_rolled = 0
        for mode, ttl in (("cached", real_ttl), ("uncached", -1)):
            fresh = load_copy(wt, migrated, scratch / f"pinfo_{mode}.json")
            rolled = 0
            for t, _per in multi_sprint_tasks(wt, fresh, sprints):
                unreconcile(wt, t, sprints)
                rolled += 1
            n_rolled = rolled
            log2 = []
            wt.subprocess = fake_gh(log2)
            wt.PROJECT_INFO_TTL_SECONDS = ttl
            wt.clear_project_info_cache()
            for t in [x for x in fresh["tasks"] if x.get("status") != "recurrent"]:
                try:
                    wt.reconcile_task_sprints(t, fresh, sprints, create_issues=False,
                                              save_callback=lambda _d: None)
                except Exception:
                    pass
            counts[mode] = sum(1 for c in log2
                               if c in ("gh project view", "gh project field-list"))
        wt.PROJECT_INFO_TTL_SECONDS = real_ttl
        if n_rolled < 2:
            raise SystemExit(
                "fixture has fewer than 2 cross-sprint tasks to un-reconcile — "
                "the metadata-memoisation comparison would be vacuous")
        print(f"    {len(tasks)} tasks ({n_rolled} un-reconciled): metadata gh calls "
              f"uncached={counts['uncached']} cached={counts['cached']}")
        check(counts["cached"] == 2,
              "a full-run reconcile fetches project metadata exactly once",
              str(counts["cached"]))
        check(counts["uncached"] > counts["cached"],
              "and that is strictly fewer than the per-task behaviour",
              f"{counts['uncached']} vs {counts['cached']}")
    finally:
        wt.subprocess = real_sub
        wt.PROJECT_INFO_TTL_SECONDS = real_ttl
        wt.clear_project_info_cache()


def test_phase5_merge(wt, fixture, scratch):
    section("14. Phase 5: recurrent clones merge into one task per series")
    import json as _json
    raw = _json.loads(Path(fixture).read_text())

    pre_series = {}
    for t in raw["tasks"]:
        if t.get("cross_sprint_parent"):
            continue
        canon = wt.recurrent_series_for_title(t.get("title", ""))
        if canon:
            pre_series.setdefault(canon, []).append(t)
    pre_issues = {b["issue"] for t in raw["tasks"]
                  for b in (t.get("sprint_issues") or []) if b.get("issue")}
    pre_mins = round(sum(l.get("minutes", 0) for t in raw["tasks"]
                         if not t.get("cross_sprint_parent")
                         for l in t.get("logs", [])), 6)
    pre_log_ids = {l.get("id") for t in raw["tasks"]
                   if not t.get("cross_sprint_parent") for l in t.get("logs", [])}
    print(f"    {len(pre_series)} series, "
          f"{sum(len(v) for v in pre_series.values())} clones pre-merge")

    data = load_copy(wt, fixture, scratch / "phase5.json")

    merged = [t for t in data["tasks"] if wt.recurrent_series_for_title(t["title"])]
    check(len(merged) == len(pre_series),
          f"one task per series survives ({len(pre_series)})", str(len(merged)))
    check(all(" - Sprint " not in t["title"] for t in merged),
          "no survivor keeps a '- Sprint N' suffix",
          str([t["title"] for t in merged if " - Sprint " in t["title"]]))

    # Nothing may be lost: not a log, not an issue reference.
    post_mins = round(sum(l.get("minutes", 0) for t in data["tasks"]
                          for l in t.get("logs", [])), 6)
    post_log_ids = {l.get("id") for t in data["tasks"] for l in t.get("logs", [])}
    post_issues = set()
    for t in data["tasks"]:
        for b in t.get("sprint_issues") or []:
            if b.get("issue"):
                post_issues.add(b["issue"])
            post_issues.update(b.get("superseded_issues") or [])
    check(post_mins == pre_mins, "total minutes unchanged", f"{pre_mins} -> {post_mins}")
    check(post_log_ids == pre_log_ids, "every log id survives",
          f"lost={sorted(pre_log_ids - post_log_ids)[:3]}")
    check(pre_issues <= post_issues,
          "every issue reference survives (as a binding or superseded)",
          f"lost={sorted(pre_issues - post_issues)[:3]}")

    # A clone whose title named one sprint but whose logs fell in another produced
    # two issues for a single sprint. One must be kept as the primary and the other
    # recorded — never silently dropped, or the project double-counts that sprint.
    supers = [(t["title"], b["sprint"], b["issue"], b["superseded_issues"])
              for t in data["tasks"] for b in (t.get("sprint_issues") or [])
              if b.get("superseded_issues")]
    for title, sprint, primary, extra in supers:
        print(f"    superseded on {title[:34]!r} {sprint}: {primary} over {extra}")
    # Whether a real collision exists depends on the fixture's vintage (it needs a
    # clone whose title named one sprint while its logs fell in another), so the
    # behaviour is asserted directly rather than relying on the data.
    synthetic = [{"sprint_id": "s1", "issue": "o/r#1", "hours_synced": 2.0}]
    changed = wt._merge_binding(
        synthetic, {"sprint_id": "s1", "issue": "o/r#2", "hours_synced": 3.75})
    check(changed and len(synthetic) == 1,
          "two issues for one sprint collapse to a single binding", str(synthetic))
    check(synthetic[0]["issue"] == "o/r#2",
          "the better-evidenced issue (more hours) becomes primary", str(synthetic[0]))
    check(synthetic[0].get("superseded_issues") == ["o/r#1"],
          "and the loser is recorded, never dropped", str(synthetic[0]))
    keep_low = [{"sprint_id": "s1", "issue": "o/r#9", "hours_synced": 9.0}]
    wt._merge_binding(keep_low, {"sprint_id": "s1", "issue": "o/r#8", "hours_synced": 1.0})
    check(keep_low[0]["issue"] == "o/r#9"
          and keep_low[0].get("superseded_issues") == ["o/r#8"],
          "the incumbent wins when it has more hours", str(keep_low[0]))

    sprints = wt.get_cached_sprints(data)
    for t in merged:
        ids = [b.get("sprint_id") for b in t.get("sprint_issues") or []]
        check(len(ids) == len(set(ids)), f"one binding per sprint on {t['title'][:32]!r}")
        # The legacy pointer must name the *latest* bound sprint. Pointing it at
        # the survivor's original (earliest) sprint made the carry-forward rule
        # re-point the oldest sprint's issue to the current sprint.
        if ids:
            check(t.get("sprint_id") == ids[-1],
                  f"legacy sprint pointer is the latest binding on {t['title'][:28]!r}",
                  f"{t.get('sprint')} vs {ids[-1]}")

    # Reconcile must plan a superseded-issue cleanup so nothing double-counts.
    # This ran only `if supers:` before, which was False for this fixture — so the
    # op was never exercised and a dropped field in the planner's working copy
    # made it unplannable without any test noticing. Force the state instead.
    forced = next(t for t in data["tasks"]
                  if len([b for b in (t.get("sprint_issues") or []) if b.get("issue")]) >= 2)
    fb = [b for b in forced["sprint_issues"] if b.get("issue")][0]
    fb["superseded_issues"] = ["o/r#4242"]
    with Stubs(wt, mode="strict", sprints=wt.get_cached_sprints(data)):
        fr = wt.reconcile_task_sprints(forced, data, wt.get_cached_sprints(data),
                                       dry_run=True, create_issues=True)
    fops = [o for o in fr["planned"] if o["op"] == "supersede"]
    check(len(fops) == 1, "a superseded issue is always planned for cleanup",
          str([o["op"] for o in fr["planned"]]))
    check(fops and fops[0]["issue"] == "o/r#4242" and fops[0]["hours"] == 0.0,
          "the supersede op names the duplicate and zeroes it", str(fops))
    check(any("SUPER" in l for l in wt._reconcile_plan_lines(fr)),
          "and it is visible in the rendered plan")
    fb.pop("superseded_issues", None)

    if supers:
        owner = next(t for t in data["tasks"]
                     if any(b.get("superseded_issues") for b in t.get("sprint_issues") or []))
        with Stubs(wt, mode="strict", sprints=sprints):
            r = wt.reconcile_task_sprints(owner, data, sprints, dry_run=True,
                                          create_issues=True)
        ops = [o for o in r["planned"] if o["op"] == "supersede"]
        check(ops, "reconcile plans a supersede (zero + close) for the duplicate",
              str([o["op"] for o in r["planned"]]))
        check(all(o["hours"] == 0.0 for o in ops),
              "and zeroes it rather than leaving stale hours", str(ops))

    # Idempotent: a second load changes nothing.
    before = Path(scratch / "phase5.json").read_text()
    wt.load()
    check(Path(scratch / "phase5.json").read_text() == before,
          "a second load() is a no-op")


def pre_migration_fixture_ok(fixture) -> bool:
    """Refuse an already-migrated file in the ``<pre-migration.json>`` slot.

    Without this the migration sections assert "0 shadows became 0 bindings"
    and **pass vacuously**, which is strictly worse than failing: the harness
    reports green while exercising none of the migration code it exists to
    cover. Copying the live data file into this slot is the standing mistake —
    it was migrated in place in July 2026 and has carried no shadows since.
    """
    try:
        raw = json.loads(Path(fixture).read_text())
    except Exception as exc:                      # noqa: BLE001 - report, don't crash
        print(f"REFUSING TO RUN: cannot read {fixture}: {exc}", file=sys.stderr)
        return False
    if any(t.get("cross_sprint_parent") for t in raw.get("tasks", [])):
        return True
    print(f"REFUSING TO RUN: {fixture} carries no cross_sprint_parent tasks, so "
          "it is already migrated.\n"
          "  The first argument must be a PRE-migration snapshot. Rebuild the "
          "fixtures:\n"
          "      python3 tools/make_fixtures.py <source.json> <out-dir>\n"
          "  then pass <out-dir>/pre.json <out-dir>/migrated.json "
          "<out-dir>/baseline.json <scratch>.", file=sys.stderr)
    return False


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

    if not pre_migration_fixture_ok(fixture):
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
    test_project_info_cache(wt, migrated, scratch)
    test_phase5_merge(wt, fixture, scratch)
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
