#!/usr/bin/env python3
"""Legacy `:7373` contract parity: wt_daemon vs tracker.py (plan §5.4).

The point of Phase 2's legacy port is that ``workload-macos-monitor`` — an
already-shipped SwiftUI menu-bar agent — keeps working with ``tracker.py``
**closed**, without one line of Swift changing. It sends no ``Authorization``
header and decodes fixed ``Codable`` structs, so the daemon's payloads must be
shape-identical to the TUI bridge's, not merely similar.

**This harness derives its expectations from ``tracker.py`` at runtime.** It
calls ``WorkloadTracker._bridge_status`` / ``._bridge_list_tasks`` /
``._bridge_start_timer`` / ``._bridge_stop_timer`` as unbound methods against a
minimal stand-in object holding the same ``_data``, then compares key sets and
value *types* against the daemon's HTTP responses on the same data. Hardcoding
the expected keys is exactly how the four harnesses rotted before Phase 0.5:
a hardcoded contract test passes forever, including after the contract changes.

It also pins the **monitor's own** decoding requirements, read out of
``Models.swift``'s documented shapes, because "same as tracker.py" is necessary
but not sufficient — the monitor declares ``role`` and ``started_at``
non-optional, so a null in either is a decode failure rather than a graceful
degradation.

Offline and side-effect free, like every harness here: the ``gh`` functions are
stubbed, ``wt.subprocess`` is a guard that raises, ``browser_window`` and
``iterm_manager`` are fakes, the daemon is booted on **ephemeral** ports (never
7373/7374/7375), and it refuses to run against the live data file.

Usage (the same 4-argument form as the others; the pre-migration and baseline
fixtures are accepted and unused):

    WT_DATA_FILE=<scratch>/unused.json \\
    venv/bin/python tools/test_legacy_contract.py <fixture.json> <migrated.json> \\
                                                  <baseline.json> <scratch-dir>

Exit status 0 when every check passes.
"""
import copy
import json
import os
import sys
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from test_wt_api import ApiStubs, fresh  # noqa: E402
from test_daemon import (  # noqa: E402
    DaemonHarness, FakeSafariWindowManager, install_fake_desktop_modules,
    request, run_invariants,
)

FAILURES = []
CHECKS = 0
LIVE_FILE = Path.home() / ".workload_tracker.json"


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


def invariants(path, label, baseline=None):
    """``check_invariants.py``, reported through *this* module's counters."""
    ok, detail = run_invariants(path, baseline)
    return check(ok, f"check_invariants holds after {label}", detail)


# ============================================== tracker.py as the oracle =====

class BridgeStandIn:
    """The smallest object ``tracker.py``'s ``_bridge_*`` methods need.

    Those methods only ever touch ``self._data`` plus a handful of UI/side-effect
    helpers, so an instance of this — rather than a live Textual ``App`` — is
    enough to obtain the *authoritative* payload shapes without a terminal, an
    event loop, Safari, or the real :7373 socket. The side-effect helpers are
    recorded so the behavioural assertions (does a start open a Safari window?
    does it focus Arc?) can be made against the real code path.
    """

    def __init__(self, data):
        self._data = data
        self.calls = []

    # -- UI, no-ops -------------------------------------------------------
    def _bridge_refresh_ui(self):
        self.calls.append("refresh_ui")

    def _populate_table(self):
        self.calls.append("populate_table")

    def _refresh_sidebar(self):
        self.calls.append("refresh_sidebar")

    def _refresh_overview(self):
        self.calls.append("refresh_overview")

    def notify(self, *a, **k):
        self.calls.append("notify")

    # -- side effects, recorded -------------------------------------------
    def _sync_task_hours_async(self, task):
        self.calls.append(("sync_hours", task.get("id")))

    def _browser_on_task_started(self, task):
        self.calls.append(("browser_start", task.get("id")))

    def _browser_on_task_stopped(self, task):
        self.calls.append(("browser_stop", task.get("id")))

    def _arc_on_task_started(self, task):
        self.calls.append(("arc_start", task.get("id")))

    def _arc_tab_cleanup(self, task):
        self.calls.append(("arc_cleanup", task.get("id")))

    # -- borrowed wholesale, because it *is* the behaviour under test ------
    def _commit_active_timer(self, note="Timer session"):
        """The real ``_commit_active_timer``, run against this stand-in.

        ``_bridge_stop_timer`` delegates to it, and it is the single source of
        truth for "stop the timer" in the TUI (the t-key stop uses it too), so
        borrowing it rather than faking it is what makes this an oracle.
        """
        import tracker as _tracker
        return _tracker.WorkloadTracker._commit_active_timer(self, note)


def bridge(tracker, name, data, *args):
    """Call ``WorkloadTracker.<name>`` unbound against *data*. Returns
    ``(payload, stand_in)`` so callers can inspect the recorded side effects."""
    stand_in = BridgeStandIn(data)
    method = getattr(tracker.WorkloadTracker, name)
    return method(stand_in, *args), stand_in


def shape(value, _depth=0):
    """A comparable description of a payload's structure.

    Types, not values: the daemon and the TUI compute ``elapsed`` and the
    per-role totals at slightly different instants, so comparing values would
    be flaky while comparing *shape* is exactly the contract the monitor cares
    about. ``None`` is kept distinct from a concrete type so an accidentally
    null ``role`` (which the monitor cannot decode) is caught rather than
    smoothed over. ``int``/``float`` are unified because JSON does not
    distinguish them and neither does Swift's ``TimeInterval``.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        inner = {shape(v, _depth + 1) for v in value}
        return ["list"] + sorted(inner, key=str)
    if isinstance(value, dict):
        return {k: shape(v, _depth + 1) for k, v in sorted(value.items())}
    return type(value).__name__


def relaxed(a, b):
    """Shapes equal, treating ``null`` as compatible with any scalar.

    Used only for the *documented-optional* field ``last_logged_at``: both sides
    legitimately answer null for a task with no logs, and which task the fixture
    happens to put first must not decide whether the harness passes.
    """
    if a == b:
        return True
    if isinstance(a, dict) and isinstance(b, dict):
        return (set(a) == set(b)
                and all(relaxed(a[k], b[k]) for k in a))
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(relaxed(x, y) for x, y in zip(a, b))
    return "null" in (a, b) and isinstance(a, str) and isinstance(b, str)


# ================================= the monitor's own decoding requirements ===
#
# Read out of ~/dev/carlos/workload-macos-monitor/Sources/WorkloadMonitor/
# Models.swift. Optionality here is the Swift declaration, not a guess:
# a non-optional field that arrives null is a decode failure, and the monitor
# turns that into TrackerError.decoding — i.e. a dead menu bar.

MONITOR_REQUIRES = {
    "/status": {
        "active_timer": {  # ActiveTimer, embedded in StatusResponse
            "task_id": ("string", True),
            "title": ("string", True),
            "role": ("string", True),
            "started_at": ("number", True),
            # `active_window_id` used to live here. It went with the Safari
            # task-window integration. The monitor's `ActiveTimer.activeWindowID`
            # is an `Int?`, so its absence decodes as nil rather than failing —
            # which is why the two repos did not need a coordinated release.
        },
    },
    "/tasks": {  # TrackerTask
        "id": ("string", True),
        "title": ("string", True),
        "role": ("string", True),
        "status": ("string", True),
        "last_logged_at": ("number", False),
    },
    "/timer/start": {"action": ("string", True), "task": ("string", True)},
    "/timer/stop": {"action": ("string", True), "task": ("string", True),
                    "logged_minutes": ("number", True)},
}


def assert_monitor_decodes(payload, spec, label):
    """Every declared field present, and non-optional ones never null."""
    missing = [k for k in spec if k not in payload]
    check(not missing, f"{label}: every field the monitor decodes is present",
          str(missing))
    wrong = []
    for key, (want, required) in spec.items():
        if key not in payload:
            continue
        got = shape(payload[key])
        if got == "null":
            if required:
                wrong.append(f"{key}=null but non-optional in Swift")
        elif got != want:
            wrong.append(f"{key}: {got} != {want}")
    check(not wrong, f"{label}: types match the monitor's Codable declarations",
          "; ".join(wrong))


# =================================================================== tests ===

def test_payload_parity(wt, tracker, wt_daemon, migrated, scratch):
    section("1. GET /status and GET /tasks — shape parity with tracker.py")
    work = scratch / "legacy-parity.json"
    data = fresh(wt, migrated, work)

    # Put a timer on a task that also has logs, so `last_logged_at` — the one
    # remaining documented-optional field — is exercised with a real value
    # rather than a null.
    subject = next(t for t in data["tasks"] if t.get("status") != "done")
    subject.setdefault("logs", []).append({
        "id": wt.uid(), "minutes": 12.0, "note": "legacy fixture",
        "at": time.time() - 60})
    data["active_timer"] = {"task_id": subject["id"],
                            "started_at": time.time() - 300}
    wt.save(data)
    print(f"       timer on {subject['title']!r}")

    oracle_status, _ = bridge(tracker, "_bridge_status", copy.deepcopy(wt.load()))
    oracle_tasks, _ = bridge(tracker, "_bridge_list_tasks",
                             copy.deepcopy(wt.load()))

    with DaemonHarness(wt_daemon) as h:
        status, live_status, headers = request(h.legacy_port, "GET", "/status")
        check(status == 200, "GET /status -> 200 on the legacy port", str(status))
        check("application/json" in (headers.get("Content-Type") or ""),
              "…as application/json", str(headers.get("Content-Type")))

        check(set(live_status) == set(oracle_status),
              "…top-level keys identical to tracker._bridge_status()",
              f"daemon={sorted(live_status)} tracker={sorted(oracle_status)}")
        check(shape(live_status) == shape(oracle_status),
              "…and every value's type matches, field for field",
              f"\n      daemon : {json.dumps(shape(live_status), sort_keys=True)}"
              f"\n      tracker: {json.dumps(shape(oracle_status), sort_keys=True)}")
        check(live_status["tasks"] == oracle_status["tasks"],
              "…same task count", f"{live_status['tasks']} vs "
                                  f"{oracle_status['tasks']}")
        check(set(live_status["time_by_role"]) ==
              set(oracle_status["time_by_role"]),
              "…same set of roles in time_by_role",
              str(set(live_status["time_by_role"]) ^
                  set(oracle_status["time_by_role"])))
        check(live_status["active_timer"]["task_id"] ==
              oracle_status["active_timer"]["task_id"]
              and live_status["active_timer"]["title"] ==
              oracle_status["active_timer"]["title"]
              and live_status["active_timer"]["role"] ==
              oracle_status["active_timer"]["role"]
              and live_status["active_timer"]["started_at"] ==
              oracle_status["active_timer"]["started_at"],
              "…and the active timer's identity fields are equal, not just "
              "the same type",
              json.dumps(live_status["active_timer"], default=str))
        check("active_window_id" not in live_status["active_timer"]
              and "active_window_id" not in oracle_status["active_timer"],
              "…and neither side still emits active_window_id, which went with "
              "the Safari integration",
              json.dumps(sorted(live_status["active_timer"])))
        assert_monitor_decodes(live_status["active_timer"],
                               MONITOR_REQUIRES["/status"]["active_timer"],
                               "/status.active_timer")

        status, live_tasks, _ = request(h.legacy_port, "GET", "/tasks")
        check(status == 200, "GET /tasks -> 200 on the legacy port", str(status))
        check(set(live_tasks) == {"tasks"} == set(oracle_tasks),
              "…wrapped in exactly {\"tasks\": [...]}",
              f"{sorted(live_tasks)} vs {sorted(oracle_tasks)}")
        check(len(live_tasks["tasks"]) == len(oracle_tasks["tasks"]),
              "…same number of tasks (done ones excluded on both sides)",
              f"{len(live_tasks['tasks'])} vs {len(oracle_tasks['tasks'])}")
        print(f"       ({len(live_tasks['tasks'])} non-done tasks)")

        daemon_keys = {frozenset(t) for t in live_tasks["tasks"]}
        tracker_keys = {frozenset(t) for t in oracle_tasks["tasks"]}
        check(daemon_keys == tracker_keys,
              "…every task entry carries exactly tracker's key set",
              str([sorted(k) for k in daemon_keys ^ tracker_keys]))

        by_id = {t["id"]: t for t in oracle_tasks["tasks"]}
        mismatched = [t["id"] for t in live_tasks["tasks"]
                      if by_id.get(t["id"]) != t]
        check(not mismatched,
              "…and every task entry is value-for-value identical",
              str(mismatched[:5]))

        typed = [not relaxed(shape(t), shape(by_id[t["id"]]))
                 for t in live_tasks["tasks"] if t["id"] in by_id]
        check(not any(typed), "…types agree on every entry", str(sum(typed)))

        logged = [t for t in live_tasks["tasks"]
                  if t.get("last_logged_at") is not None]
        check(logged,
              "…and at least one task carries a real last_logged_at (the "
              "monitor's 'recently logged' column), not all nulls",
              f"{len(logged)}/{len(live_tasks['tasks'])}")
        for entry in live_tasks["tasks"][:5]:
            assert_monitor_decodes(entry, MONITOR_REQUIRES["/tasks"],
                                   f"/tasks[{entry['id'][:8]}]")

        # Idle is a distinct, decodable state: active_timer null, not absent.
        data = wt.load()
        data["active_timer"] = None
        wt.save(data)
        oracle_idle, _ = bridge(tracker, "_bridge_status",
                                copy.deepcopy(wt.load()))
        status, live_idle, _ = request(h.legacy_port, "GET", "/status")
        check(status == 200 and live_idle["active_timer"] is None
              and "active_timer" in live_idle,
              "…and an idle tracker reports active_timer: null (present, null) "
              "— the monitor's idle state", json.dumps(live_idle)[:160])
        check(shape(live_idle) == shape(oracle_idle),
              "…matching tracker.py's idle shape too",
              f"{shape(live_idle)} vs {shape(oracle_idle)}")
    invariants(work, "the legacy read endpoints")


def test_timer_parity(wt, tracker, wt_daemon, migrated, scratch):
    section("2. POST /timer/start and /timer/stop — shape and behaviour parity")
    work = scratch / "legacy-timer.json"
    data = fresh(wt, migrated, work)
    subject = next(t for t in data["tasks"] if t.get("status") != "done")
    # Deliberately seeded with the removed feature's fields: a task synced from
    # an older wt.py on another Mac can still carry them, and neither side may
    # act on them any more.
    subject["tabs"] = ["https://example.invalid/legacy"]
    subject["active_window_id"] = 9911
    data["active_timer"] = None
    wt.save(data)
    pristine = copy.deepcopy(wt.load())

    # -- the oracle: tracker.py's own bridge methods, on the same data --------
    saved_save = tracker.save_data
    tracker.save_data = lambda d: None          # keep the oracle out of the file
    try:
        oracle_data = copy.deepcopy(pristine)
        oracle_start, start_stand_in = bridge(
            tracker, "_bridge_start_timer", oracle_data, subject["id"])
        oracle_stop, stop_stand_in = bridge(
            tracker, "_bridge_stop_timer", oracle_data)
        oracle_data2 = copy.deepcopy(pristine)
        oracle_missing, _ = bridge(tracker, "_bridge_start_timer",
                                   oracle_data2, "no-such-task-id")
        oracle_no_timer, _ = bridge(tracker, "_bridge_stop_timer", oracle_data2)
    finally:
        tracker.save_data = saved_save

    check(not any(c[0] == "browser_start" for c in start_stand_in.calls
                  if isinstance(c, tuple)),
          "oracle: the TUI bridge start opens no browser window at all",
          str(start_stand_in.calls))
    check(not any(c[0] == "arc_start" for c in start_stand_in.calls
                  if isinstance(c, tuple)),
          "oracle: …and does NOT focus the Arc space",
          str(start_stand_in.calls))

    with ApiStubs(wt, mode="record",
                  sprints=wt.get_cached_sprints(pristine)), \
            DaemonHarness(wt_daemon) as h:
        FakeSafariWindowManager.calls.clear()

        status, live_start, _ = request(
            h.legacy_port, "POST", "/timer/start", body={"task_id": subject["id"]})
        check(status == 200, "POST /timer/start -> 200", str(status))
        check(set(live_start) == set(oracle_start)
              and shape(live_start) == shape(oracle_start),
              "…same keys and types as tracker._bridge_start_timer()",
              f"daemon={live_start} tracker={oracle_start}")
        check(live_start == oracle_start,
              "…and the same values ({action, task})",
              f"{live_start} vs {oracle_start}")
        assert_monitor_decodes(live_start, MONITOR_REQUIRES["/timer/start"],
                               "/timer/start")
        check((wt.load().get("active_timer") or {}).get("task_id")
              == subject["id"], "…and the timer is actually running on disk")
        check(not FakeSafariWindowManager.calls,
              "…and the daemon touched Safari not at all, even though the task "
              "still carries saved tabs (behaviour, not just shape)",
              str(FakeSafariWindowManager.calls))
        check(next(t for t in wt.load()["tasks"] if t["id"] == subject["id"])
              .get("active_window_id") == 9911,
              "…leaving a stale active_window_id exactly as it found it — "
              "removal must not rewrite data it no longer understands")

        # A second start on the already-running task is a no-op success.
        before = Path(work).read_bytes()
        status, again, _ = request(h.legacy_port, "POST", "/timer/start",
                                   body={"task_id": subject["id"]})
        check(status == 200 and again == {"action": "started",
                                          "task": subject["title"]},
              "a start on the already-running task is a no-op success "
              "(matching the bridge — a restart would discard the session)",
              f"{status} {again}")
        check(Path(work).read_bytes() == before,
              "…and it writes nothing at all")

        # Rewind the running timer by five minutes rather than sleeping for
        # them: both commit paths discard a sub-3-second session, so a real
        # sleep would either be slow or test the wrong branch.
        rewound = wt.load()
        rewound["active_timer"]["started_at"] -= 300
        wt.save(rewound)

        status, live_stop, _ = request(h.legacy_port, "POST", "/timer/stop")
        check(status == 200, "POST /timer/stop -> 200", str(status))
        check(set(live_stop) == set(oracle_stop)
              and shape(live_stop) == shape(oracle_stop),
              "…same keys and types as tracker._bridge_stop_timer()",
              f"daemon={live_stop} tracker={oracle_stop}")
        check(live_stop["action"] == "stopped"
              and live_stop["task"] == subject["title"],
              "…and the same action/task values", str(live_stop))
        assert_monitor_decodes(live_stop, MONITOR_REQUIRES["/timer/stop"],
                               "/timer/stop")
        after = wt.load()
        check(after.get("active_timer") is None,
              "…the timer is cleared on disk")
        stopped = next(t for t in after["tasks"] if t["id"] == subject["id"])
        check(stopped.get("active_window_id") == 9911,
              "…the stale window id is still untouched after a full "
              "start/stop cycle", str(stopped.get("active_window_id")))
        check(not FakeSafariWindowManager.calls,
              "…and no Safari call was made on the stop path either",
              str(FakeSafariWindowManager.calls))

        # The commit semantics: an identical "Timer session" entry, as the TUI's
        # t-key stop and _commit_active_timer produce.
        newest = max(stopped["logs"], key=lambda l: l.get("at", 0))
        check(newest["note"] == "Timer session",
              "…and the session logged as note='Timer session', exactly as the "
              "TUI's t-key stop does", newest.get("note"))
        for key in ("id", "minutes", "note", "at", "started_at", "ended_at"):
            check(key in newest, f"…the log entry carries {key!r}",
                  str(sorted(newest)))
        check(abs(newest["minutes"] - 5.0) < 0.2
              and abs(live_stop["logged_minutes"] - newest["minutes"]) < 1e-9,
              "…for the right duration, echoed back as logged_minutes",
              f"log={newest['minutes']} response={live_stop['logged_minutes']}")

        # ---- the one known behavioural delta, pinned so it cannot drift -----
        # tracker._commit_active_timer logs a session only when elapsed > 0.1
        # min (6s); wt_api._commit_timer (which wt.cmd_stop also matches) uses
        # > 0.05 min (3s). So a session between 3 and 6 seconds is logged by the
        # daemon and the CLI, and discarded by the TUI. Asserted rather than
        # hidden: if either threshold moves, this goes red.
        for seconds, want_daemon, want_tracker in ((2, False, False),
                                                   (4, True, False),
                                                   (10, True, True)):
            probe = wt.load()
            probe["active_timer"] = {"task_id": subject["id"],
                                     "started_at": time.time() - seconds}
            wt.save(probe)
            before_n = len(next(t for t in wt.load()["tasks"]
                                if t["id"] == subject["id"])["logs"])
            request(h.legacy_port, "POST", "/timer/stop")
            after_n = len(next(t for t in wt.load()["tasks"]
                               if t["id"] == subject["id"])["logs"])
            daemon_logged = after_n > before_n

            oracle = copy.deepcopy(pristine)
            oracle_task = next(t for t in oracle["tasks"]
                               if t["id"] == subject["id"])
            oracle["active_timer"] = {"task_id": subject["id"],
                                      "started_at": time.time() - seconds}
            oracle_before = len(oracle_task.get("logs", []))
            saved = tracker.save_data
            tracker.save_data = lambda d: None
            try:
                bridge(tracker, "_bridge_stop_timer", oracle)
            finally:
                tracker.save_data = saved
            tracker_logged = len(oracle_task.get("logs", [])) > oracle_before

            check(daemon_logged is want_daemon
                  and tracker_logged is want_tracker,
                  f"a {seconds}s session: daemon logs={want_daemon}, "
                  f"tracker logs={want_tracker} (known 3s-vs-6s threshold delta)",
                  f"daemon={daemon_logged} tracker={tracker_logged}")

        # Error shapes.
        status, live_missing, _ = request(h.legacy_port, "POST", "/timer/start",
                                          body={"task_id": "no-such-task-id"})
        check(status == 404, "an unknown task_id -> 404", str(status))
        check(set(live_missing) >= set(k for k in oracle_missing
                                       if k != "_status"),
              "…with the bridge's flat {\"error\": ...} shape",
              f"{live_missing} vs {oracle_missing}")

        status, live_none, _ = request(h.legacy_port, "POST", "/timer/stop")
        check(status == 404 and live_none.get("error") == "No timer running",
              "stopping with no timer -> 404 'No timer running', verbatim",
              f"{status} {live_none}")
        check(oracle_no_timer.get("error") == live_none.get("error"),
              "…the same message tracker.py returns",
              f"{oracle_no_timer.get('error')} vs {live_none.get('error')}")

        status, _body, _ = request(h.legacy_port, "POST", "/timer/start",
                                   body={})
        check(status == 400, "a start with no task_id -> 400", str(status))
    invariants(work, "the legacy timer endpoints")


def test_unauthenticated_and_isolation(wt, wt_daemon, migrated, scratch):
    section("3. the legacy port is unauthenticated, and separate from v1")
    work = scratch / "legacy-auth.json"
    fresh(wt, migrated, work)
    with DaemonHarness(wt_daemon) as h:
        # THE requirement: the monitor sends no Authorization header, and must
        # not have to start.
        for method, path, body in (("GET", "/status", None),
                                   ("GET", "/tasks", None),
                                   ("POST", "/timer/stop", {})):
            status, _b, _hd = request(h.legacy_port, method, path, body=body)
            check(status in (200, 404),
                  f"{method} {path} needs no Authorization header",
                  str(status))

        # A token must not be *rejected* either: the monitor's base URL is
        # user-configurable and someone may point an authenticated client here.
        status, _b, _hd = request(h.legacy_port, "GET", "/status",
                                  token=h.token)
        check(status == 200, "…and an unnecessary token is ignored, not refused",
              str(status))

        # The two surfaces do not leak into each other.
        status, _b, _hd = request(h.legacy_port, "GET", "/v1/snapshot",
                                  token=h.token)
        check(status == 404, "the v1 API is NOT served on the legacy port",
              str(status))
        status, _b, _hd = request(h.port, "GET", "/status", token=h.token)
        check(status == 404, "…and /status is NOT served on the v1 port",
              str(status))
        check(h.port != h.legacy_port, "they are genuinely different ports",
              f"{h.port} vs {h.legacy_port}")
        check(7373 not in (h.port, h.legacy_port),
              "…and neither of them is 7373 (tracker.py owns that)",
              f"{h.port}/{h.legacy_port}")

        # Unknown legacy routes keep the bridge's flat error shape.
        status, body, _hd = request(h.legacy_port, "GET", "/nonsense")
        check(status == 404 and "error" in body,
              "an unknown legacy action -> 404 {\"error\": ...}",
              f"{status} {body}")

        # opt-out: no --legacy-port means no second listener at all.
    with DaemonHarness(wt_daemon, legacy=False) as h:
        check(h.legacy_port is None and h.legacy_server is None,
              "…and the legacy port is opt-in: off unless asked for",
              str(h.legacy_port))
        status, _b, _hd = request(h.port, "GET", "/v1/health", token=h.token)
        check(status == 200, "…while the v1 API still serves normally",
              str(status))


def test_streamdeck_extras(wt, tracker, wt_daemon, migrated, scratch):
    section("4. the rest of the documented :7373 surface (Stream Deck)")
    work = scratch / "legacy-extras.json"
    data = fresh(wt, migrated, work)
    inprogress = next((t for t in data["tasks"]
                       if t.get("status") == "inprogress"), None)
    if inprogress is None:
        inprogress = data["tasks"][0]
        inprogress["status"] = "inprogress"
    data["active_timer"] = None
    wt.save(data)

    with ApiStubs(wt, mode="record", sprints=wt.get_cached_sprints(data)), \
            DaemonHarness(wt_daemon) as h:
        status, body, _ = request(h.legacy_port, "GET", "/timer/toggle")
        check(status == 200 and body.get("action") == "started",
              "GET /timer/toggle starts the first in-progress task",
              f"{status} {body}")
        status, body, _ = request(h.legacy_port, "GET", "/timer/toggle")
        check(status == 200 and body.get("action") == "stopped"
              and "logged_minutes" in body,
              "…and toggling again stops it, with logged_minutes",
              f"{status} {body}")

        status, body, _ = request(h.legacy_port, "GET", "/log/15")
        check(status == 200 and body.get("action") == "logged"
              and body.get("minutes") == 15,
              "GET /log/<minutes> quick-logs to the in-progress task",
              f"{status} {body}")
        newest = max(next(t for t in wt.load()["tasks"]
                          if t["id"] == inprogress["id"])["logs"],
                     key=lambda l: l.get("at", 0))
        check(newest["note"] == "Stream Deck (15m)",
              "…with the bridge's note format", newest.get("note"))
        status, body, _ = request(h.legacy_port, "GET", "/log/abc")
        check(status == 400 and body.get("error") == "Invalid minutes",
              "…and a non-numeric amount -> 400 'Invalid minutes', verbatim",
              f"{status} {body}")

        status, body, _ = request(h.legacy_port, "GET", "/filter/demokit")
        oracle = {"action": "filter", "role": "demokit",
                  "note": "Use keyboard 1-4 in TUI to filter"}
        check(status == 200 and body == oracle,
              "GET /filter/<role> echoes exactly what the bridge echoes",
              f"{body} vs {oracle}")

        # /push is deliberately NOT served here: it shells out to gh from an
        # unauthenticated port. It must say so rather than 404 into confusion.
        status, body, _ = request(h.legacy_port, "GET", "/push/whatever")
        check(status == 501 and "/v1/tasks" in (body.get("error") or ""),
              "GET /push/<task> -> 501 pointing at the authenticated endpoint",
              f"{status} {body}")
    invariants(work, "the Stream Deck endpoints")


def arc_free(path):
    """``(imports, referenced_names)`` for an Arc audit, ignoring prose.

    A plain substring grep is the wrong tool: ``wt_daemon.py`` *documents* why
    it does not wire Arc in, so the words appear legitimately in comments. This
    walks the AST, so only real imports and real name/attribute references
    count.
    """
    import ast
    tree = ast.parse(Path(path).read_text())
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    return imports, names


def test_source_level_parity(wt_daemon, tracker):
    section("5. source-level guards")
    imports, names = arc_free(REPO / "wt_daemon.py")
    check("arc_browser" not in imports,
          "wt_daemon.py never imports arc_browser (Arc is deprecated)",
          str(sorted(i for i in imports if "arc" in i.lower())))
    check("TaskTabManager" not in names and "_arc_tab_cleanup" not in names,
          "…and never references the TUI's Arc tab cleanup in code "
          "(prose about it is fine)",
          str(sorted(n for n in names if "arc" in n.lower()
                     or "TabManager" in n)))
    check(tracker.BRIDGE_PORT == wt_daemon.TUI_BRIDGE_PORT,
          "the port the daemon probes is the port tracker.py binds",
          f"{tracker.BRIDGE_PORT} vs {wt_daemon.TUI_BRIDGE_PORT}")
    # The two payload builders must be reachable as plain functions, so this
    # harness (and any future one) can diff them without a running server.
    check(callable(wt_daemon.legacy_status_payload)
          and callable(wt_daemon.legacy_tasks_payload),
          "the legacy payload builders are plain functions, diffable offline")


def test_no_gh_escaped():
    section("6. belt and braces: no real gh invocation")
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


# ==================================================================== main ===

def main():
    if len(sys.argv) != 5:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    _fixture, migrated, _baseline, scratch = (
        Path(a).expanduser() for a in sys.argv[1:])
    scratch.mkdir(parents=True, exist_ok=True)

    os.environ["WT_DATA_FILE"] = str(scratch / "unused.json")
    import wt
    import wt_daemon

    if wt.DATA_FILE == LIVE_FILE:
        print("REFUSING TO RUN: wt.DATA_FILE is the live file", file=sys.stderr)
        return 2
    if not json.loads(Path(migrated).read_text()).get(
            "config", {}).get("sprints_cache"):
        print("REFUSING TO RUN: the fixture has no config.sprints_cache",
              file=sys.stderr)
        return 2

    saved_modules = install_fake_desktop_modules()
    # tracker.py must never start its real :7373 bridge from inside a test.
    import tracker
    tracker.WorkloadTracker._start_bridge_server = lambda self: None

    fixture_sprints = wt.get_cached_sprints(
        json.loads(Path(migrated).read_text()))
    try:
        with ApiStubs(wt, mode="record", sprints=fixture_sprints):
            test_payload_parity(wt, tracker, wt_daemon, migrated, scratch)
            test_timer_parity(wt, tracker, wt_daemon, migrated, scratch)
            test_unauthenticated_and_isolation(wt, wt_daemon, migrated, scratch)
            test_streamdeck_extras(wt, tracker, wt_daemon, migrated, scratch)
            test_source_level_parity(wt_daemon, tracker)
        test_no_gh_escaped()
    finally:
        for name, module in saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    total = CHECKS
    print(f"\n{total - len(FAILURES)}/{total} checks passed")
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S):")
        for failure in FAILURES:
            print(f"  x {failure}")
        return 1
    print("All legacy-contract checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
