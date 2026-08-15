#!/usr/bin/env python3
"""Verification harness for wt_daemon.py — the HTTP + SSE API (plan Phase 2).

There is no pytest suite in this repo, so this script *is* the test for
``wt_daemon.py``. Like every other harness here it runs fully offline and can
never reach GitHub:

  * ``tools/test_reconcile.py``'s ``Stubs`` monkeypatches every ``gh``-touching
    function in ``wt`` and replaces ``wt.subprocess`` with a guard that raises on
    any attribute access;
  * ``browser_window`` and ``iterm_manager`` are replaced in ``sys.modules`` with
    fakes, so no AppleScript, no Safari, no tmux (the daemon imports both
    *inside* the handler for exactly this reason);
  * the daemon is booted **in this process** on **ephemeral** ports. That is
    deliberate and not a shortcut: a subprocess daemon would not see the ``gh``
    stubs, so any test of ``close`` / ``reconcile`` would have to either skip the
    GitHub paths or let a real ``gh`` run. In-process keeps the socket, the
    routing, the auth, the threading and the SSE framing genuinely under test
    while the ``gh`` boundary stays stubbed. Port **7373 is never bound** (the
    daemon refuses it outright — §13) and 7374/7375 are never bound either;
  * every run happens on a fresh copy in a scratch dir, and the script refuses
    to start if the resolved data file is the live one.

The three checks that matter most, because they are the ones that can *lose the
owner's data* rather than merely annoy:

  1. **§10 — the risk-#9 refusal.** ``wt.load()`` returns ``{}``-defaults for a
     missing, EPERM-under-TCC, zero-byte or corrupt file, which is
     indistinguishable from "no tasks"; a save on top of that clobbers the real
     iCloud-synced file. Every one of those five states is provoked and the
     daemon must answer **503 ``data_unreadable``** *and leave the file byte-
     identical*.
  2. **§11 — the lock timeout.** A daemon must never degrade to an unlocked
     write the way ``save()`` is permitted to. Holding ``data_lock()`` from the
     test thread must turn a mutation into a **503 ``lock_timeout``**.
  3. **§7-§9 — ``check_invariants.py`` after every mutation.** The structural
     invariants (no shadows, full ``owner/repo#n`` refs, one binding per sprint)
     must survive each endpoint.

Usage (same 4-argument form as the other harnesses; the pre-migration fixture is
accepted and unused):

    WT_DATA_FILE=<scratch>/unused.json \\
    venv/bin/python tools/test_daemon.py <fixture.json> <migrated.json> \\
                                         <baseline.json> <scratch-dir>

Set ``WT_FAKE_GH_LOG`` to a logging fake ``gh``'s log file and §14 asserts it
stayed empty.

Exit status 0 when every check passes.
"""
import http.client
import json
import os
import queue
import shutil
import stat
import subprocess as real_subprocess
import sys
import threading
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from test_reconcile import Stubs  # noqa: E402
from test_wt_api import ApiStubs, fresh  # noqa: E402

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


# ============================================================= HTTP client ====

def request(port, method, path, body=None, token=None, headers=None,
            timeout=30):
    """One request/response. Returns ``(status, parsed_body, headers)``."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    hdrs = dict(headers or {})
    if token is not None:
        hdrs["Authorization"] = f"Bearer {token}"
    payload = None
    if body is not None:
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    try:
        conn.request(method, path, payload, hdrs)
        response = conn.getresponse()
        raw = response.read()
        out_headers = dict(response.getheaders())
        status = response.status
    finally:
        conn.close()
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = raw.decode("utf-8", "replace")
    return status, parsed, out_headers


def code_of(body):
    """The machine code out of an ``{"error": {...}}`` envelope, or None."""
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return body["error"].get("code")
    return None


class SSEClient:
    """Reads ``GET /v1/events`` on a background thread into a queue."""

    def __init__(self, port, token, timeout=30):
        self.conn = http.client.HTTPConnection("127.0.0.1", port,
                                               timeout=timeout)
        self.conn.request("GET", "/v1/events",
                          headers={"Authorization": f"Bearer {token}"})
        self.response = self.conn.getresponse()
        self.status = self.response.status
        self.content_type = self.response.getheader("Content-Type") or ""
        self.events: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self):
        fields = {}
        try:
            for raw in self.response:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line == "":
                    if "event" in fields:
                        try:
                            payload = json.loads(fields.get("data") or "{}")
                        except ValueError:
                            payload = {}
                        self.events.put((fields["event"], payload,
                                         fields.get("id")))
                    fields = {}
                    continue
                key, _, value = line.partition(":")
                fields[key.strip()] = value.strip()
        except Exception:  # noqa: BLE001 - the stream just ended
            pass

    def wait(self, event, timeout=15, predicate=None):
        """Next *event* satisfying *predicate*, or None. Consumes as it goes."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                name, payload, _seq = self.events.get(timeout=remaining)
            except queue.Empty:
                return None
            if name == event and (predicate is None or predicate(payload)):
                return payload

    def drain(self):
        out = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                return out

    def close(self):
        """Disconnect, and actually send a FIN.

        ``HTTPConnection.close()`` alone is not enough here, for two reasons:
        ``getresponse()`` has already handed the socket to the response (so
        ``conn.sock`` is None), and the reader thread is blocked inside
        ``response.fp``, which holds an io-reference that defers the real
        ``close(2)``. Without an explicit ``shutdown`` no FIN is ever sent and
        the server has nothing to notice — a quirk of this client, not of the
        daemon (which unsubscribes within two heartbeats of a real disconnect).
        """
        import socket as _socket
        sock = getattr(getattr(self.response, "fp", None), "raw", None)
        sock = getattr(sock, "_sock", None)
        if sock is not None:
            try:
                sock.shutdown(_socket.SHUT_RDWR)
            except OSError:
                pass
        for closer in (self.response.close, self.conn.close):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass
        self._thread.join(timeout=3.0)


# ================================================================== fakes =====

class FakeSafariWindowManager:
    """``browser_window.SafariWindowManager`` without Safari.

    Mutates the same task keys the real one does (``tabs``,
    ``active_window_id``) so the legacy contract's ``active_window_id``
    behaviour is genuinely exercised rather than stubbed away.
    """

    WINDOW_ID = 4242
    calls: list = []

    def __init__(self):
        pass

    def snapshot_task_tabs(self, task):
        FakeSafariWindowManager.calls.append(("snapshot", task.get("id")))
        task["tabs"] = ["https://example.invalid/one",
                        "https://example.invalid/two"]
        return list(task["tabs"])

    def open_task_window(self, task):
        FakeSafariWindowManager.calls.append(("open", task.get("id")))
        if not task.get("tabs"):
            return None
        task["active_window_id"] = self.WINDOW_ID
        return self.WINDOW_ID

    def close_window(self, window_id):
        FakeSafariWindowManager.calls.append(("close", window_id))
        return True

    def window_exists(self, window_id):
        return window_id is not None


class FakeTaskTerminalManager:
    calls: list = []

    def __init__(self, data):
        self.data = data

    def open_terminal(self, task, save_callback):
        FakeTaskTerminalManager.calls.append(("open", task.get("id")))
        task["iterm_session_name"] = f"wt-fake-{task['id']}"
        save_callback(self.data)
        return {"success": True, "session": task["iterm_session_name"]}

    def close_session(self, task):
        FakeTaskTerminalManager.calls.append(("close", task.get("id")))
        return {"success": True}


def install_fake_desktop_modules():
    """Swap ``browser_window`` / ``iterm_manager`` for fakes in ``sys.modules``.

    Both are imported *inside* the functions that use them (``wt._browser_switch``
    and the daemon's handlers), so a ``sys.modules`` swap is enough and no real
    AppleScript can run.
    """
    saved = {}
    for name, attr, value in (
            ("browser_window", "SafariWindowManager", FakeSafariWindowManager),
            ("iterm_manager", "TaskTerminalManager", FakeTaskTerminalManager)):
        saved[name] = sys.modules.get(name)
        module = types.ModuleType(name)
        setattr(module, attr, value)
        sys.modules[name] = module
    return saved


# ============================================================ daemon harness ==

class DaemonHarness:
    """Boot ``wt_daemon`` in-process on ephemeral ports; always tear it down.

    ``port=0`` / ``legacy_port=0`` let the OS pick high ephemeral ports, so a
    test run can never collide with 7373 (the TUI's), 7374 or 7375 (the real
    daemon's). ``__exit__`` shuts both servers down and joins the worker threads
    even when the body raised, so a crashed test cannot strand a listener.
    """

    def __init__(self, wt_daemon, token="test-token-not-a-secret", *,
                 legacy=True, watch_interval=0.15, heartbeat_seconds=1.0,
                 github_sync_on_stop=False, allow_empty=False,
                 presence=False, presence_interval=0.05,
                 idle_stop_ttl=None, resume_grace=None):
        self.wt_daemon = wt_daemon
        self.token = token
        self.legacy = legacy
        # ``presence`` defaults **off** here on purpose. The fixture is a copy of
        # the live file, which has presence_detection_enabled=true and may carry
        # a running timer; a loop left on would auto-stop it out from under an
        # unrelated section the moment the machine went idle.
        self.kwargs = dict(watch_interval=watch_interval,
                           heartbeat_seconds=heartbeat_seconds,
                           github_sync_on_stop=github_sync_on_stop,
                           allow_empty=allow_empty,
                           presence=presence,
                           presence_interval=presence_interval,
                           idle_stop_ttl=(wt_daemon.IDLE_STOP_TTL_SECONDS
                                          if idle_stop_ttl is None
                                          else idle_stop_ttl),
                           resume_grace=(wt_daemon.RESUME_GRACE_SECONDS
                                         if resume_grace is None
                                         else resume_grace))
        self.daemon = None
        self.api = None
        self.legacy_server = None
        self._threads = []

    def __enter__(self):
        self.daemon = self.wt_daemon.Daemon(
            self.token, port=0, legacy_port=0 if self.legacy else None,
            **self.kwargs)
        self.api, self.legacy_server = self.wt_daemon.make_servers(self.daemon)
        self.daemon.start()
        self._threads.append(threading.Thread(
            target=self.api.serve_forever, kwargs={"poll_interval": 0.05},
            name="test-wt-api", daemon=True))
        if self.legacy_server is not None:
            self._threads.append(threading.Thread(
                target=self.legacy_server.serve_forever,
                kwargs={"poll_interval": 0.05}, name="test-wt-legacy",
                daemon=True))
        for thread in self._threads:
            thread.start()
        return self

    def __exit__(self, *exc):
        try:
            self.daemon.join_background(timeout=10.0)
        finally:
            self.daemon.stop()
            for server in (self.api, self.legacy_server):
                if server is None:
                    continue
                try:
                    server.shutdown()
                finally:
                    server.server_close()
            for thread in self._threads:
                thread.join(timeout=3.0)
        return False

    @property
    def port(self):
        return self.daemon.port

    @property
    def legacy_port(self):
        return self.daemon.legacy_port

    def get(self, path, **kw):
        return request(self.port, "GET", path, token=self.token, **kw)

    def post(self, path, body=None, **kw):
        return request(self.port, "POST", path, body=body if body is not None
                       else {}, token=self.token, **kw)

    def patch(self, path, body=None, **kw):
        return request(self.port, "PATCH", path, body=body if body is not None
                       else {}, token=self.token, **kw)

    def delete(self, path, **kw):
        return request(self.port, "DELETE", path, token=self.token, **kw)

    def await_operation(self, record, timeout=30):
        """Poll ``GET /v1/operations/{id}`` until the operation settles."""
        oid = record["operation_id"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, body, _ = self.get(f"/v1/operations/{oid}")
            if status == 200 and body.get("state") != "running":
                return body
            time.sleep(0.05)
        return None


# ========================================================== shared assertions =

def run_invariants(path, baseline=None):
    """``tools/check_invariants.py`` on *path*. Returns ``(ok, detail)``.

    Structural invariants only unless a *baseline* is given — the totals
    comparison would (correctly) fail after a test adds or deletes logs.
    Split from :func:`invariants` so ``test_legacy_contract.py`` can report
    through its own counters instead of this module's.
    """
    args = [sys.executable, str(REPO / "tools" / "check_invariants.py"),
            str(path)]
    if baseline:
        args.append(str(baseline))
    proc = real_subprocess.run(args, capture_output=True, text=True)
    if proc.returncode == 0:
        return True, ""
    # check_invariants prints its failures as "  x [invariant] …" — matching only
    # "FAIL" reported the count line and swallowed every reason.
    return False, "\n      " + "\n      ".join(
        l.strip() for l in proc.stdout.splitlines()
        if l.lstrip().startswith("x [") or "FAILURE" in l)[:800]


def invariants(path, label, baseline=None):
    ok, detail = run_invariants(path, baseline)
    return check(ok, f"check_invariants holds after {label}", detail)


def fresh_idle(wt, src, dst):
    """``fresh``, with any timer the fixture inherited stopped first.

    A data file copied while the owner had a timer running carries it, and then
    ``POST /v1/timer/stop`` legitimately answers 200 instead of the 409 the
    no-timer assertions are about. Dropping the timer *without* committing it
    keeps the fixture's logs untouched.
    """
    data = fresh(wt, src, dst)
    if data.get("active_timer"):
        data["active_timer"] = None
        wt.save(data)
    return data


def digest(path):
    return Path(path).read_bytes()


# =================================================================== tests ====

def test_error_map(wt, wt_api, wt_daemon):
    section("1. error-code registry and the WtError -> HTTP mapping")
    declared = set(wt_api.ERROR_CODES)
    mapped = set(wt_daemon.ERROR_STATUS)
    check(declared == mapped,
          "ERROR_STATUS maps every wt_api code, and no stale ones",
          f"unmapped={sorted(declared - mapped)} stale={sorted(mapped - declared)}")
    check(not (declared & set(wt_daemon.DAEMON_ERROR_CODES)),
          "daemon-only codes do not collide with wt_api codes",
          str(sorted(declared & set(wt_daemon.DAEMON_ERROR_CODES))))
    bad = [(c, s) for c, s in wt_daemon.ERROR_STATUS.items()
           if not 400 <= s <= 599]
    check(not bad, "every mapped status is a 4xx/5xx", str(bad))
    print(f"       ({len(declared)} wt_api codes + "
          f"{len(wt_daemon.DAEMON_ERROR_CODES)} daemon codes)")

    # error_response() must produce the documented envelope for all three
    # exception families, because the Swift client codes against the shape.
    for exc, want_status, want_code in (
            (wt_api.WtError("task_not_found", "nope"), 404, "task_not_found"),
            (wt_api.WtError("invalid_role", "nope"), 400, "invalid_role"),
            (wt_api.WtError("no_active_timer", "nope"), 409, "no_active_timer"),
            (wt_api.WtError("close_failed", "nope"), 502, "close_failed"),
            (wt_api.WtError("no_sprints", "nope"), 503, "no_sprints"),
            (wt_daemon.DaemonError("unauthorized", "nope"), 401, "unauthorized"),
            (wt.DataLockTimeout("busy"), 503, "lock_timeout"),
            (RuntimeError("boom"), 500, "internal_error")):
        status, body = wt_daemon.error_response(exc)
        check(status == want_status and code_of(body) == want_code
              and isinstance(body["error"].get("message"), str),
              f"error_response({type(exc).__name__}/{want_code}) -> "
              f"{want_status} {want_code}", f"got {status} {code_of(body)}")

    # Every wt_api code must round-trip through error_response, so a client can
    # never receive a code that maps to a bare 500.
    unmapped = [c for c in wt_api.ERROR_CODES
                if wt_daemon.error_response(wt_api.WtError(c, "x"))[0] == 500]
    check(not unmapped, "no wt_api code falls through to 500", str(unmapped))


def test_token(wt_daemon, scratch):
    section("2. bearer token file (§5.1)")
    path = scratch / "token" / "daemon.token"
    check(not path.exists(), "starting from no token file")
    token = wt_daemon.load_or_create_token(path)
    check(bool(token) and len(token) >= 32,
          "a token is generated on first run", f"len={len(token)}")
    mode = stat.S_IMODE(path.stat().st_mode)
    check(mode == 0o600, "the token file is mode 0600", oct(mode))
    check(wt_daemon.load_or_create_token(path) == token,
          "a second read returns the same token (no rotation)")

    path.chmod(0o644)
    wt_daemon.load_or_create_token(path)
    check(stat.S_IMODE(path.stat().st_mode) == 0o600,
          "a loosened token file is re-tightened to 0600")


def test_probe(wt, wt_daemon, migrated, scratch):
    section("3. probe_data_file — the risk-#9 detector (§3.1 / §5.3)")
    good = scratch / "probe-good.json"
    shutil.copyfile(migrated, good)
    probe = wt_daemon.probe_data_file(good)
    check(probe["readable"] and probe["reason"] == "ok" and probe["tasks"] > 0,
          "a real data file probes readable/ok", json.dumps(probe, default=str))

    cases = {
        "missing": lambda p: None,
        "empty_file": lambda p: p.write_bytes(b""),
        "unparseable": lambda p: p.write_text("{not json"),
        "no_tasks": lambda p: p.write_text(json.dumps({"tasks": [],
                                                       "roles": []})),
    }
    for reason, prepare in cases.items():
        path = scratch / f"probe-{reason}.json"
        if path.exists():
            path.unlink()
        prepare(path)
        probe = wt_daemon.probe_data_file(path)
        check(not probe["readable"] and probe["reason"] == reason,
              f"probe detects {reason!r}", json.dumps(probe, default=str))

    # The documented second-Mac symptom: the file is there, stat works, the read
    # does not. chmod 000 reproduces it without needing TCC.
    denied = scratch / "probe-denied.json"
    shutil.copyfile(migrated, denied)
    denied.chmod(0o000)
    try:
        probe = wt_daemon.probe_data_file(denied)
        check(not probe["readable"] and probe["reason"] == "permission_denied",
              "probe detects an unreadable file as permission_denied "
              "(the Full Disk Access symptom)", json.dumps(probe, default=str))
    finally:
        denied.chmod(0o600)

    # A JSON array, not an object — load() would silently produce {} for this.
    weird = scratch / "probe-array.json"
    weird.write_text("[1, 2, 3]")
    probe = wt_daemon.probe_data_file(weird)
    check(not probe["readable"] and probe["reason"] == "unparseable",
          "probe rejects a non-object top level",
          json.dumps(probe, default=str))


def test_auth_and_health(wt, wt_daemon, migrated, scratch):
    section("4. auth (§5.1) and /v1/health (§5.3)")
    fresh(wt, migrated, scratch / "auth.json")
    with DaemonHarness(wt_daemon) as h:
        status, body, headers = request(h.port, "GET", "/v1/snapshot")
        check(status == 401 and code_of(body) == "unauthorized",
              "no Authorization header -> 401 unauthorized",
              f"{status} {code_of(body)}")
        check("Bearer" in (headers.get("WWW-Authenticate") or ""),
              "…with a WWW-Authenticate: Bearer challenge",
              str(headers.get("WWW-Authenticate")))

        status, body, _ = request(h.port, "GET", "/v1/snapshot",
                                  token="wrong-token")
        check(status == 401 and code_of(body) == "unauthorized",
              "a wrong token -> 401 unauthorized", f"{status}")

        status, body, _ = request(h.port, "GET", "/v1/snapshot",
                                  headers={"Authorization": h.token})
        check(status == 401,
              "a bare token without the Bearer scheme -> 401", f"{status}")

        status, body, _ = h.get("/v1/snapshot")
        check(status == 200 and isinstance(body.get("tasks"), list),
              "the right token -> 200", f"{status}")

        # Every mutating route is behind the same gate, not just the read ones.
        unauthed = [
            ("POST", "/v1/tasks"), ("POST", "/v1/timer/stop"),
            ("PATCH", "/v1/tasks/x"), ("DELETE", "/v1/tasks/x"),
            ("POST", "/v1/tasks/x/close"), ("POST", "/v1/tasks/x/status"),
            ("GET", "/v1/events"), ("GET", "/v1/health"),
        ]
        bad = [(m, p) for m, p in unauthed
               if request(h.port, m, p, body={} if m != "GET" else None)[0] != 401]
        check(not bad, "every /v1 route rejects an unauthenticated request",
              str(bad))

        status, health, _ = h.get("/v1/health")
        check(status == 200, "GET /v1/health -> 200", str(status))
        for key in ("version", "pid", "port", "data_file", "tui_bridge",
                    "uptime_seconds", "subscribers"):
            check(key in health, f"health carries {key!r}", str(sorted(health)))
        check(health["data_file"]["path"] == str(wt.DATA_FILE)
              and health["data_file"]["readable"],
              "health reports the data file path and readability",
              json.dumps(health["data_file"], default=str))
        check(health["tui_bridge"]["port"] == 7373
              and isinstance(health["tui_bridge"]["running"], bool),
              "health probes :7373 so the client can warn about the TUI",
              json.dumps(health["tui_bridge"]))
        check(h.port not in (7373, 7374, 7375)
              and h.legacy_port not in (7373, 7374, 7375),
              "the harness bound ephemeral ports, not the real ones",
              f"{h.port}/{h.legacy_port}")


def test_snapshot(wt, wt_api, wt_daemon, migrated, scratch):
    section("5. GET /v1/snapshot — the whole UI state in one round trip")
    work = scratch / "snapshot.json"
    data = fresh(wt, migrated, work)
    with ApiStubs(wt, mode="strict", sprints=wt.get_cached_sprints(data)), \
            DaemonHarness(wt_daemon) as h:
        before = digest(work)
        status, snap, _ = h.get("/v1/snapshot")
        check(status == 200, "GET /v1/snapshot -> 200", str(status))
        check(digest(work) == before, "…and it wrote nothing")

        direct = wt_api.snapshot(wt.load())
        check(len(snap["tasks"]) == len(direct["tasks"]),
              "the payload is wt_api.snapshot(), task for task",
              f"{len(snap['tasks'])} vs {len(direct['tasks'])}")
        for key in ("tasks", "roles", "sprints", "current_sprint",
                    "active_timer", "project_options", "config"):
            check(key in snap, f"snapshot carries {key!r}")
        check("data_file" in snap and snap["data_file"]["readable"],
              "…plus the daemon's data_file probe, so an unreadable file is "
              "not rendered as an empty board (risk #9)",
              json.dumps(snap.get("data_file"), default=str))
        sample = snap["tasks"][0]
        for key in ("id", "title", "status", "role_id", "activity",
                    "github_repo", "sprints_with_time", "reportable_mins",
                    "current_issue", "last_logged_at", "logs"):
            check(key in sample, f"a task carries {key!r} (filter facet / UI)",
                  str(sorted(sample))[:200])
        print(f"       ({len(snap['tasks'])} tasks, {len(snap['sprints'])} "
              f"sprints, {len(snap['roles'])} roles)")


def test_task_and_log_endpoints(wt, wt_api, wt_daemon, migrated, baseline,
                                scratch):
    section("6. task + log + timer endpoints, invariants after each mutation")
    work = scratch / "crud.json"
    data = fresh_idle(wt, migrated, work)
    sprints = wt.get_cached_sprints(data)

    with ApiStubs(wt, mode="record", sprints=sprints), \
            DaemonHarness(wt_daemon) as h:
        # -- create -------------------------------------------------------
        status, body, _ = h.post("/v1/tasks",
                                 {"title": "Daemon harness task",
                                  "role": "other", "status": "todo",
                                  "description": "created by tools/test_daemon"})
        check(status == 201 and body["task"]["title"] == "Daemon harness task",
              "POST /v1/tasks -> 201", f"{status} {body}")
        task_id = body["task"]["id"]
        invariants(work, "POST /v1/tasks")

        status, body, _ = h.post("/v1/tasks", {"role": "other"})
        check(status == 400 and code_of(body) == "bad_request",
              "…a create with no title -> 400 bad_request",
              f"{status} {code_of(body)}")
        status, body, _ = h.post("/v1/tasks", {"title": "x", "role": "nope"})
        check(status == 400 and code_of(body) == "invalid_role",
              "…an unknown role -> 400 invalid_role",
              f"{status} {code_of(body)}")

        # -- patch --------------------------------------------------------
        status, body, _ = h.patch(f"/v1/tasks/{task_id}",
                                  {"description": "edited", "title": "Renamed"})
        check(status == 200 and body["task"]["title"] == "Renamed"
              and body["changed"].get("description") == "edited",
              "PATCH /v1/tasks/{id} -> 200 with a changed map",
              f"{status} {body.get('changed')}")
        status, body, _ = h.patch(f"/v1/tasks/{task_id}", {"nonsense": 1})
        check(status == 400 and code_of(body) == "invalid_args",
              "…an unknown field -> 400 invalid_args",
              f"{status} {code_of(body)}")
        invariants(work, "PATCH /v1/tasks/{id}")

        # -- status (non-done: no close workflow) --------------------------
        status, body, _ = h.post(f"/v1/tasks/{task_id}/status",
                                 {"status": "inprogress"})
        check(status == 200 and body["status"] == "inprogress"
              and body["closed"] is False,
              "POST /v1/tasks/{id}/status {inprogress} -> 200",
              f"{status} {body.get('status')}")
        status, body, _ = h.post(f"/v1/tasks/{task_id}/status",
                                 {"status": "banana"})
        check(status == 400 and code_of(body) == "invalid_status",
              "…an unknown status -> 400 invalid_status",
              f"{status} {code_of(body)}")
        invariants(work, "POST /v1/tasks/{id}/status")

        # -- logs ----------------------------------------------------------
        status, body, _ = h.post(f"/v1/tasks/{task_id}/logs",
                                 {"minutes": 90, "note": "harness log",
                                  "started_at": time.time() - 5400,
                                  "ended_at": time.time()})
        check(status == 201 and body["log"]["minutes"] == 90,
              "POST /v1/tasks/{id}/logs -> 201", f"{status} {body}")
        log_id = body["log"]["id"]
        invariants(work, "POST /v1/tasks/{id}/logs")

        status, body, _ = h.post(f"/v1/tasks/{task_id}/logs", {"minutes": -3})
        check(status == 400 and code_of(body) == "invalid_minutes",
              "…negative minutes -> 400 invalid_minutes",
              f"{status} {code_of(body)}")

        status, body, _ = h.patch(f"/v1/tasks/{task_id}/logs/{log_id}",
                                  {"minutes": 60, "note": "edited"})
        check(status == 200 and body["log"]["minutes"] == 60
              and body["old"]["minutes"] == 90,
              "PATCH …/logs/{log_id} -> 200 with the old values",
              f"{status} {body.get('log')}")
        status, body, _ = h.patch(f"/v1/tasks/{task_id}/logs/{log_id}", {})
        check(status == 400 and code_of(body) == "no_changes",
              "…an empty edit -> 400 no_changes", f"{status} {code_of(body)}")
        status, body, _ = h.patch(f"/v1/tasks/{task_id}/logs/nosuchlog",
                                  {"minutes": 1})
        check(status == 404 and code_of(body) == "log_not_found",
              "…an unknown log id -> 404 log_not_found",
              f"{status} {code_of(body)}")
        invariants(work, "PATCH …/logs/{log_id}")

        status, body, _ = h.post(f"/v1/tasks/{task_id}/logs/{log_id}/split",
                                 {"split_at_minutes": 25})
        check(status == 200 and body["first"]["minutes"] == 25
              and body["second"]["minutes"] == 35,
              "POST …/logs/{log_id}/split -> 200, 60 = 25 + 35",
              f"{status} {body}")
        first_id, second_id = body["first"]["id"], body["second"]["id"]
        status, body, _ = h.post(f"/v1/tasks/{task_id}/logs/{first_id}/split",
                                 {"split_at_minutes": 999})
        check(status == 400 and code_of(body) == "invalid_split",
              "…a split beyond the total -> 400 invalid_split",
              f"{status} {code_of(body)}")
        invariants(work, "POST …/logs/{log_id}/split")

        status, body, _ = h.post(f"/v1/tasks/{task_id}/logs/merge",
                                 {"log_id_1": first_id, "log_id_2": second_id})
        check(status == 200 and abs(body["merged"]["minutes"] - 60) < 1e-9,
              "POST …/logs/merge -> 200, 25 + 35 = 60", f"{status} {body}")
        merged_id = body["merged"]["id"]
        status, body, _ = h.post(f"/v1/tasks/{task_id}/logs/merge",
                                 {"log_id_1": merged_id, "log_id_2": merged_id})
        check(status == 400 and code_of(body) == "same_log",
              "…merging a log with itself -> 400 same_log",
              f"{status} {code_of(body)}")
        status, body, _ = h.post(f"/v1/tasks/{task_id}/logs/merge",
                                 {"log_id_1": merged_id})
        check(status == 400 and code_of(body) == "bad_request",
              "…a merge missing log_id_2 -> 400 bad_request",
              f"{status} {code_of(body)}")
        invariants(work, "POST …/logs/merge")

        status, body, _ = h.delete(f"/v1/tasks/{task_id}/logs/{merged_id}")
        check(status == 200 and body["log"]["id"] == merged_id,
              "DELETE …/logs/{log_id} -> 200", f"{status} {body}")
        after = wt.load()
        subject = next(t for t in after["tasks"] if t["id"] == task_id)
        check(not subject.get("logs"),
              "…and the log really is gone from disk",
              str(subject.get("logs")))
        invariants(work, "DELETE …/logs/{log_id}")

        # -- timers ---------------------------------------------------------
        status, body, _ = h.post("/v1/timer/stop")
        check(status == 409 and code_of(body) == "no_active_timer",
              "POST /v1/timer/stop with nothing running -> 409 no_active_timer",
              f"{status} {code_of(body)}")

        FakeSafariWindowManager.calls.clear()
        status, body, _ = h.post("/v1/timer/start", {"task_id": task_id})
        check(status == 200 and body["task_id"] == task_id
              and isinstance(body["started_at"], (int, float)),
              "POST /v1/timer/start -> 200", f"{status} {body}")
        check((wt.load().get("active_timer") or {}).get("task_id") == task_id,
              "…and the timer is on disk")
        # CLAUDE.md: "A start with no saved tabs is a no-op." This task has none.
        check(not [c for c in FakeSafariWindowManager.calls if c[0] == "open"],
              "…and a start on a task with no saved tabs opens no window",
              str(FakeSafariWindowManager.calls))
        invariants(work, "POST /v1/timer/start")

        # Now give it tabs. The v1 endpoint defaults to **browser=false**: a v1
        # client must not rearrange the user's desktop by omission. The Safari
        # window is still a feature — it opens when the client asks for it, and
        # the legacy :7375 start hard-codes True to match tracker.py's bridge —
        # so both halves are asserted here rather than just the new default.
        h.post("/v1/timer/stop")
        h.post(f"/v1/tasks/{task_id}/tabs/save")   # the fake writes two URLs
        FakeSafariWindowManager.calls.clear()
        status, body, _ = h.post("/v1/timer/start", {"task_id": task_id})
        check(status == 200 and not [c for c in FakeSafariWindowManager.calls
                                     if c[0] == "open"],
              "…a start WITH saved tabs still opens no window by default",
              f"{status} {FakeSafariWindowManager.calls}")
        check(next(t for t in wt.load()["tasks"]
                   if t["id"] == task_id).get("active_window_id") is None,
              "…and no active_window_id is recorded")

        # …but the capability is intact when asked for explicitly.
        h.post("/v1/timer/stop")
        FakeSafariWindowManager.calls.clear()
        status, body, _ = h.post("/v1/timer/start",
                                 {"task_id": task_id, "browser": True})
        check(status == 200 and ("open", task_id) in
              FakeSafariWindowManager.calls,
              "…while browser=true opens its Safari window",
              f"{status} {FakeSafariWindowManager.calls}")
        check(next(t for t in wt.load()["tasks"]
                   if t["id"] == task_id).get("active_window_id")
              == FakeSafariWindowManager.WINDOW_ID,
              "…and active_window_id is persisted (the monitor's border)")

        status, body, _ = h.post("/v1/timer/start", {})
        check(status == 400 and code_of(body) == "bad_request",
              "…a start without task_id -> 400 bad_request",
              f"{status} {code_of(body)}")
        status, body, _ = h.post("/v1/timer/start", {"task_id": "nope-nope"})
        check(status == 404 and code_of(body) == "task_not_found",
              "…a start on an unknown task -> 404 task_not_found",
              f"{status} {code_of(body)}")

        time.sleep(0.1)
        status, body, _ = h.post("/v1/timer/stop")
        check(status == 200 and body["task_id"] == task_id,
              "POST /v1/timer/stop -> 200", f"{status} {body}")
        check(wt.load().get("active_timer") is None,
              "…and the timer is cleared on disk")
        invariants(work, "POST /v1/timer/stop")

        # -- delete ----------------------------------------------------------
        status, body, _ = h.delete(f"/v1/tasks/{task_id}")
        check(status == 200 and body["title"] == "Renamed",
              "DELETE /v1/tasks/{id} -> 200", f"{status} {body}")
        check(all(t["id"] != task_id for t in wt.load()["tasks"]),
              "…and the task is gone from disk")
        status, body, _ = h.delete(f"/v1/tasks/{task_id}")
        check(status == 404 and code_of(body) == "task_not_found",
              "…deleting it again -> 404 task_not_found",
              f"{status} {code_of(body)}")
        invariants(work, "DELETE /v1/tasks/{id}", baseline)

    return work


def test_github_and_integrations(wt, wt_api, wt_daemon, migrated, scratch):
    section("7. GitHub, Safari-tabs and iTerm endpoints")
    work = scratch / "github.json"
    data = fresh(wt, migrated, work)
    sprints = wt.get_cached_sprints(data)

    # A task with a repo but no issue, so link/unlink are real transitions.
    subject = next((t for t in data["tasks"]
                    if t.get("github_repo") and not t.get("status") == "done"),
                   None) or data["tasks"][0]
    subject_id = subject["id"]
    repo = subject.get("github_repo") or "grafana/field-eng"
    wt_api.unlink_issue(data, subject_id) if wt.task_current_issue(
        subject, data) else None
    subject["github_repo"] = repo
    wt.save(data)

    with ApiStubs(wt, mode="record", sprints=sprints), \
            DaemonHarness(wt_daemon) as h:
        status, body, _ = h.post(f"/v1/tasks/{subject_id}/github/unlink")
        check(status == 409 and code_of(body) == "not_linked",
              "POST …/github/unlink with nothing linked -> 409 not_linked",
              f"{status} {code_of(body)}")

        status, body, _ = h.post(f"/v1/tasks/{subject_id}/github/link",
                                 {"issue": f"{repo}#4321", "verify": False})
        check(status == 200 and body["issue"] == f"{repo}#4321",
              "POST …/github/link -> 200 (verify=false, so no gh call)",
              f"{status} {body}")
        invariants(work, "POST …/github/link")

        status, body, _ = h.post(f"/v1/tasks/{subject_id}/github/open",
                                 {"open": False})
        check(status == 200
              and body["url"] == f"https://github.com/{repo}/issues/4321",
              "POST …/github/open -> 200 with the issue URL "
              "(open=false: no browser launched)", f"{status} {body}")

        # …and with open=true. The issue opens in **cmux**, not a browser: the
        # daemon finds the workspace whose name begins with the issue number, or
        # creates "<number> - <first 15 chars of title>". `_open_issue_in_cmux`
        # is swapped for a recorder so this asserts the wiring — which arguments
        # reach it, and that its result is reported verbatim — without touching
        # the owner's real cmux.
        import wt_daemon as _wtd
        calls = []

        def fake_open(url, number, title):
            calls.append((url, number, title))
            return {"opened": True, "workspace": "workspace:99",
                    "workspace_created": True}

        real_open = _wtd._open_issue_in_cmux
        _wtd._open_issue_in_cmux = fake_open
        try:
            status, body, _ = h.post(f"/v1/tasks/{subject_id}/github/open",
                                     {"open": True})
            check(status == 200 and body["opened"] is True
                  and body["workspace"] == "workspace:99"
                  and body["workspace_created"] is True,
                  "POST …/github/open reports cmux's workspace and whether it "
                  "was created", f"{status} {body}")
            check(len(calls) == 1
                  and calls[0][0] == f"https://github.com/{repo}/issues/4321"
                  and calls[0][1] == "4321",
                  "…passing the URL and the bare issue number to cmux",
                  str(calls))

            # A cmux failure must surface as opened=false, not a 500 and not a
            # silent diversion to the default browser.
            _wtd._open_issue_in_cmux = lambda *a: {
                "opened": False, "workspace": None, "workspace_created": False,
                "detail": "cmux is not reachable"}
            status, body, _ = h.post(f"/v1/tasks/{subject_id}/github/open",
                                     {"open": True})
            check(status == 200 and body["opened"] is False
                  and "not reachable" in (body.get("detail") or ""),
                  "…and an unreachable cmux reports opened=false with a reason",
                  f"{status} {body}")
        finally:
            _wtd._open_issue_in_cmux = real_open

        # The workspace-matching rules, as pure functions.
        check(_wtd.cmux_workspace_name("316", "Teams integration & automation")
              == "316 - Teams integrati",
              "cmux_workspace_name uses the first 15 title characters",
              _wtd.cmux_workspace_name("316", "Teams integration & automation"))
        check(_wtd.cmux_workspace_name("42", "  Tiny  ") == "42 - Tiny",
              "…trimmed, and no trailing separator when the title is short")
        check(_wtd.cmux_workspace_name("7", "") == "7",
              "…and a title-less task gets just the number")

        rows = [{"ref": "workspace:10", "title": "Teams Integration"},
                {"ref": "workspace:4", "title": "316 - Teams integrat"},
                {"ref": "workspace:9", "title": "3160 - other"},
                {"ref": "workspace:5", "custom_title": "77 renamed"}]
        check(_wtd.cmux_workspace_ref_for_issue("316", rows) == "workspace:4",
              "cmux_workspace_ref_for_issue matches on the leading number")
        check(_wtd.cmux_workspace_ref_for_issue("31", rows) is None,
              "…and issue 31 does NOT adopt the workspace for 316 "
              "(digit boundary, or work files under the wrong issue)")
        check(_wtd.cmux_workspace_ref_for_issue("3160", rows) == "workspace:9",
              "…while 3160 matches its own")
        check(_wtd.cmux_workspace_ref_for_issue("77", rows) == "workspace:5",
              "…and a renamed workspace matches on custom_title")
        check(_wtd.cmux_workspace_ref_for_issue("999", rows) is None,
              "…with no match when nothing begins with the number")

        daemon_src = (REPO / "wt_daemon.py").read_text()
        check("import webbrowser" not in daemon_src,
              "wt_daemon never imports webbrowser (it needs a TCC grant "
              "a launchd agent cannot have)")

        status, body, _ = h.post(f"/v1/tasks/{subject_id}/github/unlink")
        check(status == 200 and body["old_issue"] == f"{repo}#4321",
              "POST …/github/unlink -> 200", f"{status} {body}")
        invariants(work, "POST …/github/unlink")

        status, body, _ = h.post(f"/v1/tasks/{subject_id}/github/link",
                                 {"verify": False})
        check(status == 400 and code_of(body) == "bad_request",
              "…a link with no issue ref -> 400 bad_request",
              f"{status} {code_of(body)}")

        # github/push is a 202 operation (it hits the Project API).
        h.post(f"/v1/tasks/{subject_id}/github/link",
               {"issue": f"{repo}#4321", "verify": False})
        status, record, headers = h.post(f"/v1/tasks/{subject_id}/github/push")
        check(status == 202 and record.get("operation_id"),
              "POST …/github/push -> 202 with an operation_id",
              f"{status} {record}")
        check(headers.get("Location", "").endswith(record["operation_id"]),
              "…and a Location header pointing at the operation",
              str(headers.get("Location")))
        settled = h.await_operation(record)
        check(settled and settled["state"] == "completed",
              "…and the operation completes",
              json.dumps(settled, default=str)[:300] if settled else "timeout")
        invariants(work, "POST …/github/push")

        # -- Safari task windows (never Arc) --------------------------------
        FakeSafariWindowManager.calls.clear()
        status, body, _ = h.post(f"/v1/tasks/{subject_id}/tabs/save")
        check(status == 200 and len(body["tabs"]) == 2,
              "POST …/tabs/save -> 200 with the snapshotted URLs",
              f"{status} {body}")
        status, body, _ = h.post(f"/v1/tasks/{subject_id}/tabs/open")
        check(status == 200
              and body["active_window_id"] == FakeSafariWindowManager.WINDOW_ID,
              "POST …/tabs/open -> 200 and records active_window_id",
              f"{status} {body}")
        status, body, _ = h.post(f"/v1/tasks/{subject_id}/tabs/close")
        check(status == 200 and body["active_window_id"] is None,
              "POST …/tabs/close -> 200 and clears active_window_id",
              f"{status} {body}")
        status, body, _ = h.post(f"/v1/tasks/{subject_id}/tabs/clear")
        check(status == 200 and body["tabs"] == [],
              "POST …/tabs/clear -> 200", f"{status} {body}")
        status, body, _ = h.post(f"/v1/tasks/{subject_id}/tabs/nonsense")
        check(status == 404 and code_of(body) == "not_found",
              "…an unknown tabs action -> 404 not_found",
              f"{status} {code_of(body)}")
        invariants(work, "the tabs endpoints")

        # Arc must not be reachable from the daemon at all. Checked against the
        # AST, not the text: the module documents *why* Arc is unwired, so the
        # word appears legitimately in prose.
        import ast
        tree = ast.parse((REPO / "wt_daemon.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        check("arc_browser" not in imported,
              "wt_daemon.py never imports Arc (deprecated, deliberately "
              "unwired)", str(sorted(imported)))

        # -- iTerm -----------------------------------------------------------
        FakeTaskTerminalManager.calls.clear()
        status, body, _ = h.post(f"/v1/tasks/{subject_id}/iterm",
                                 {"action": "open"})
        check(status == 200 and body["result"]["success"],
              "POST …/iterm {open} -> 200", f"{status} {body}")
        status, body, _ = h.post(f"/v1/tasks/{subject_id}/iterm",
                                 {"action": "close"})
        check(status == 200 and body["action"] == "close",
              "POST …/iterm {close} -> 200", f"{status} {body}")
        status, body, _ = h.post(f"/v1/tasks/{subject_id}/iterm",
                                 {"action": "explode"})
        check(status == 400 and code_of(body) == "bad_request",
              "…an unknown iterm action -> 400 bad_request",
              f"{status} {code_of(body)}")
        invariants(work, "the iterm endpoint")


def test_long_operations(wt, wt_api, wt_daemon, migrated, scratch):
    section("8. close / reconcile — 202 + operation_id + SSE progress (§5.2)")
    work = scratch / "ops.json"
    data = fresh(wt, migrated, work)
    sprints = wt.get_cached_sprints(data)

    # Pick a task with logged time and a repo: it has a real reconcile plan.
    candidates = [t for t in data["tasks"]
                  if t.get("github_repo") and t.get("logs")
                  and t.get("status") != "done"]
    if not candidates:
        raise SystemExit("fixture has no open task with a repo and logs — "
                         "cannot exercise close/reconcile")
    subject = candidates[0]
    subject_id = subject["id"]
    print(f"       subject: {subject['title']!r}")

    with ApiStubs(wt, mode="record", sprints=sprints), \
            DaemonHarness(wt_daemon) as h:
        # -- close/plan is write-free by construction -------------------------
        before = digest(work)
        status, plan, _ = h.post(f"/v1/tasks/{subject_id}/close/plan",
                                 {"offline": True})
        check(status == 200 and "plan" in plan and "plan_lines" in plan,
              "POST …/close/plan -> 200 with a plan", f"{status} {plan}")
        check(digest(work) == before, "…and it wrote nothing (dry run)")
        check(isinstance(plan.get("will_create_issues"), int),
              "…and says how many issues a close would create",
              str(plan.get("will_create_issues")))

        # -- reconcile dry run is synchronous and write-free ------------------
        status, body, _ = h.post(f"/v1/tasks/{subject_id}/reconcile",
                                 {"dry_run": True})
        check(status == 200 and body["dry_run"] is True,
              "POST …/reconcile {dry_run} -> 200 (synchronous)",
              f"{status} {body.get('dry_run')}")
        check(digest(work) == before, "…and it wrote nothing")

        # -- a real reconcile is a 202 operation streaming progress ----------
        sse = SSEClient(h.port, h.token)
        try:
            check(sse.status == 200 and "text/event-stream" in sse.content_type,
                  "GET /v1/events -> 200 text/event-stream",
                  f"{sse.status} {sse.content_type}")
            status, record, headers = h.post(
                f"/v1/tasks/{subject_id}/reconcile", {"create_issues": True})
            check(status == 202 and record["op"] == "reconcile"
                  and record["state"] == "running",
                  "POST …/reconcile -> 202 with a running operation record",
                  f"{status} {record}")
            started = sse.wait("progress", predicate=lambda p:
                               p.get("operation_id") == record["operation_id"]
                               and p.get("state") == "started")
            check(started is not None,
                  "…SSE delivered a progress/started event for it")
            completed = sse.wait("progress", timeout=30, predicate=lambda p:
                                 p.get("operation_id") == record["operation_id"]
                                 and p.get("state") in ("completed",))
            check(completed is not None and "result" in completed,
                  "…and a progress/completed event carrying the result",
                  json.dumps(completed, default=str)[:200] if completed else
                  "timeout")
        finally:
            sse.close()

        settled = h.await_operation(record)
        check(settled and settled["state"] == "completed",
              "GET /v1/operations/{id} reports the finished operation",
              json.dumps(settled, default=str)[:300] if settled else "timeout")
        status, body, _ = h.get("/v1/operations/nosuchoperation")
        check(status == 404 and code_of(body) == "not_found",
              "…and an unknown operation id -> 404 not_found",
              f"{status} {code_of(body)}")
        invariants(work, "a real reconcile")

        # Idempotent: a second reconcile plans nothing.
        status, body, _ = h.post(f"/v1/tasks/{subject_id}/reconcile",
                                 {"dry_run": True})
        check(status == 200 and not body["result"].get("planned"),
              "…and a second dry run plans nothing (idempotent)",
              str(body["result"].get("planned"))[:200])

        # -- close is also a 202 operation, and marks the task done -----------
        sse = SSEClient(h.port, h.token)
        try:
            status, record, headers = h.post(f"/v1/tasks/{subject_id}/close",
                                             {"create_issue": True})
            check(status == 202 and record["op"] == "close",
                  "POST …/close -> 202 with an operation_id",
                  f"{status} {record}")
            changed = sse.wait("changed", timeout=30, predicate=lambda p:
                               p.get("source") == "daemon")
            check(changed is not None,
                  "…SSE delivered a changed/daemon event for the close",
                  json.dumps(changed, default=str) if changed else "timeout")
        finally:
            sse.close()
        settled = h.await_operation(record)
        check(settled and settled["state"] == "completed",
              "…and the close operation completes",
              json.dumps(settled, default=str)[:400] if settled else "timeout")
        after = wt.load()
        closed = next(t for t in after["tasks"] if t["id"] == subject_id)
        check(closed["status"] == "done", "…and the task is done on disk",
              closed["status"])
        invariants(work, "a real close")

        # A close on a task with no repo cannot mint an issue: the workflow
        # fails, the operation reports it, and the task must stay open.
        norepo = next((t for t in after["tasks"]
                       if not t.get("github_repo") and t["status"] != "done"),
                      None)
        if norepo is None:
            print("       (no repo-less open task in the fixture — "
                  "skipping the failed-close check)")
        else:
            status, record, _ = h.post(f"/v1/tasks/{norepo['id']}/close")
            check(status == 202, "POST …/close on a repo-less task -> 202",
                  str(status))
            settled = h.await_operation(record)
            # No repo means no GitHub integration at all, so wt.close_task
            # simply marks it done — that is the documented behaviour, and it
            # is worth pinning either way.
            check(settled is not None and settled["state"] in
                  ("completed", "failed"),
                  "…and the operation settles rather than hanging",
                  json.dumps(settled, default=str)[:200] if settled else
                  "timeout")
        invariants(work, "a repo-less close")


def test_transport_errors(wt, wt_daemon, migrated, scratch):
    section("9. transport-level errors (routing, method, body)")
    fresh_idle(wt, migrated, scratch / "errors.json")
    with DaemonHarness(wt_daemon) as h:
        status, body, _ = h.get("/v1/nope")
        check(status == 404 and code_of(body) == "not_found",
              "an unknown route -> 404 not_found", f"{status} {code_of(body)}")
        status, body, _ = h.get("/")
        check(status == 404 and code_of(body) == "not_found",
              "…including the bare root", f"{status} {code_of(body)}")
        status, body, _ = h.get("/status")
        check(status == 404,
              "…and the legacy paths are NOT served on the v1 port",
              str(status))

        status, body, _ = request(h.port, "GET", "/v1/tasks/whatever",
                                  token=h.token)
        check(status == 405 and code_of(body) == "method_not_allowed",
              "a wrong method on a real route -> 405 method_not_allowed",
              f"{status} {code_of(body)}")
        check(sorted(body["error"]["details"]["allowed"]) == ["DELETE", "PATCH"],
              "…listing the methods that are allowed",
              str(body["error"].get("details")))

        status, body, _ = request(h.port, "POST", "/v1/tasks",
                                  body=b"{not json", token=h.token,
                                  headers={"Content-Type": "application/json"})
        check(status == 400 and code_of(body) == "bad_json",
              "an unparseable body -> 400 bad_json", f"{status} {code_of(body)}")
        status, body, _ = request(h.port, "POST", "/v1/tasks",
                                  body=b'["a", "list"]', token=h.token,
                                  headers={"Content-Type": "application/json"})
        check(status == 400 and code_of(body) == "bad_json",
              "a non-object body -> 400 bad_json", f"{status} {code_of(body)}")

        for body_obj in (None, {}):
            status, parsed, _ = request(h.port, "POST", "/v1/timer/stop",
                                        body=body_obj, token=h.token)
            check(status == 409 and code_of(parsed) == "no_active_timer",
                  f"an empty POST body ({body_obj!r}) is fine, not a 400",
                  f"{status} {code_of(parsed)}")


def test_risk_nine(wt, wt_daemon, migrated, scratch):
    section("10. RISK #9 — refusing to write over an unreadable data file")
    saved_data_file = wt.DATA_FILE

    cases = []
    # (label, reason, how to build the file)
    good = scratch / "r9-good.json"
    shutil.copyfile(migrated, good)

    empty = scratch / "r9-empty.json"
    empty.write_bytes(b"")
    cases.append(("a zero-byte file (iCloud placeholder)", "empty_file", empty))

    corrupt = scratch / "r9-corrupt.json"
    corrupt.write_text('{"tasks": [{"id": "x"')
    cases.append(("a truncated/corrupt file", "unparseable", corrupt))

    notasks = scratch / "r9-notasks.json"
    notasks.write_text(json.dumps(
        {"tasks": [], "config": {},
         "roles": [{"id": "other", "label": "Other", "color": "white"}]}))
    cases.append(("a parseable file with no tasks", "no_tasks", notasks))

    missing = scratch / "r9-missing.json"
    if missing.exists():
        missing.unlink()
    cases.append(("a missing file", "missing", missing))

    denied = scratch / "r9-denied.json"
    shutil.copyfile(migrated, denied)
    denied.chmod(0o000)
    cases.append(("an unreadable file (Full Disk Access symptom)",
                  "permission_denied", denied))

    try:
        wt.DATA_FILE = good
        os.environ["WT_DATA_FILE"] = str(good)
        with DaemonHarness(wt_daemon) as h:
            for label, reason, path in cases:
                wt.DATA_FILE = path
                os.environ["WT_DATA_FILE"] = str(path)

                def snapshot_bytes():
                    """The file's exact state, readable or not."""
                    if not path.exists():
                        return None
                    if reason == "permission_denied":
                        path.chmod(0o600)
                        raw = path.read_bytes()
                        path.chmod(0o000)
                        return raw
                    return path.read_bytes()

                before = snapshot_bytes()

                status, body, _ = h.post("/v1/tasks", {"title": "MUST NOT LAND",
                                                       "role": "other"})
                check(status == 503 and code_of(body) == "data_unreadable",
                      f"POST /v1/tasks over {label} -> 503 data_unreadable",
                      f"{status} {code_of(body)}")
                check(isinstance(body.get("error", {}).get("details"), dict)
                      and body["error"]["details"].get("reason") == reason,
                      f"…with reason={reason!r} so the client can show the "
                      f"right state",
                      json.dumps(body.get("error", {}).get("details"),
                                 default=str))

                # Every mutating route is gated, not just create.
                status, body, _ = h.post("/v1/timer/stop")
                check(status == 503 and code_of(body) == "data_unreadable",
                      "…and POST /v1/timer/stop is gated too",
                      f"{status} {code_of(body)}")

                # Reads must still answer — otherwise the client cannot render
                # the "grant Full Disk Access" state at all — but they must not
                # go through a bare wt.load(), which is a read-modify-WRITE and
                # would materialise a {}-default document over the real file.
                status, snap, _ = h.get("/v1/snapshot")
                check(status == 200 and snap["data_file"]["reason"] == reason,
                      "…while GET /v1/snapshot still answers, flagging why",
                      f"{status} {snap.get('data_file', {}).get('reason')}")
                check(status == 200 and snap["tasks"] == [],
                      "…with an empty task list rather than a lie",
                      str(len(snap.get("tasks", []))))
                status, health, _ = h.get("/v1/health")
                check(status == 200
                      and health["data_file"]["readable"] is False,
                      "…and /v1/health reports data_file.readable = false",
                      json.dumps(health.get("data_file"), default=str))
                # Legacy port too: the monitor polls /status every few seconds,
                # so an unguarded read there would destroy the file unattended.
                status, legacy, _ = request(h.legacy_port, "GET", "/status")
                check(status == 200 and legacy["tasks"] == 0,
                      "…and the unauthenticated legacy /status answers safely",
                      f"{status} {legacy}")

                # THE assertion: after a write attempt AND four reads, the file
                # on disk is exactly what it was.
                after = snapshot_bytes()
                if reason == "missing":
                    check(after is None,
                          "…and the daemon never created the missing file")
                else:
                    check(after == before,
                          "…and the file is byte-identical after every "
                          "refused write and every read",
                          f"{len(before or b'')} -> {len(after or b'')} bytes")
                if reason == "permission_denied":
                    path.chmod(0o600)
                    check(len(json.loads(path.read_text()).get("tasks", []))
                          > 0,
                          "…and the unreadable file still holds all its tasks "
                          "(a bare wt.load() here destroys them)")
                    path.chmod(0o000)

            # --allow-empty is the documented escape hatch for a fresh install.
            wt.DATA_FILE = notasks
            os.environ["WT_DATA_FILE"] = str(notasks)
        with DaemonHarness(wt_daemon, allow_empty=True) as h:
            status, body, _ = h.post("/v1/tasks", {"title": "fresh install",
                                                   "role": "other"})
            check(status == 201,
                  "--allow-empty lets a genuinely fresh install be written to",
                  f"{status} {code_of(body)}")
            check(len(json.loads(notasks.read_text())["tasks"]) == 1,
                  "…and the task really landed")
    finally:
        denied.chmod(0o600)
        wt.DATA_FILE = saved_data_file
        os.environ["WT_DATA_FILE"] = str(saved_data_file)


def test_lock_timeout(wt, wt_daemon, migrated, scratch):
    section("11. lock timeout -> 503 (a daemon never writes unlocked)")
    work = scratch / "lock.json"
    fresh(wt, migrated, work)
    saved_timeout = wt.DATA_LOCK_TIMEOUT_SECONDS
    wt.DATA_LOCK_TIMEOUT_SECONDS = 0.3  # keep the test quick
    try:
        with DaemonHarness(wt_daemon) as h:
            before = digest(work)
            # Held from *this* thread; the handler thread runs in the same
            # process, so it contends on the re-entrancy RLock and times out.
            with wt.data_lock():
                status, body, _ = h.post("/v1/tasks", {"title": "blocked",
                                                       "role": "other"})
                check(status == 503 and code_of(body) == "lock_timeout",
                      "a mutation while the lock is held -> 503 lock_timeout",
                      f"{status} {code_of(body)}")
                status, read_body, _ = h.get("/v1/snapshot")
                check(status == 503 and code_of(read_body) == "lock_timeout",
                      "…and a read is refused the same way rather than "
                      "reading around the lock", f"{status} {code_of(read_body)}")
            check(digest(work) == before,
                  "…and nothing was written while the lock was held")

            # Once released, the same request succeeds — the 503 was contention,
            # not a broken daemon.
            status, body, _ = h.post("/v1/tasks", {"title": "unblocked",
                                                   "role": "other"})
            check(status == 201,
                  "…and the identical request succeeds once the lock is free",
                  f"{status} {code_of(body)}")
    finally:
        wt.DATA_LOCK_TIMEOUT_SECONDS = saved_timeout
    invariants(work, "the lock-timeout test")


def test_sse(wt, wt_daemon, migrated, scratch):
    section("12. SSE: changed (daemon + external), heartbeat, framing")
    work = scratch / "sse.json"
    data = fresh(wt, migrated, work)

    with ApiStubs(wt, mode="record", sprints=wt.get_cached_sprints(data)), \
            DaemonHarness(wt_daemon, heartbeat_seconds=0.5,
                          watch_interval=0.15) as h:
        sse = SSEClient(h.port, h.token)
        try:
            hello = sse.wait("hello", timeout=10)
            check(hello is not None and hello.get("version"),
                  "the stream opens with a hello carrying the daemon version",
                  json.dumps(hello, default=str) if hello else "timeout")

            beat = sse.wait("heartbeat", timeout=10)
            check(beat is not None and "now" in beat,
                  "…and emits heartbeat while idle",
                  json.dumps(beat, default=str) if beat else "timeout")

            # A daemon write -> changed{source: daemon}
            status, body, _ = h.post("/v1/tasks", {"title": "sse subject",
                                                   "role": "other"})
            check(status == 201, "created a task to trigger a change",
                  str(status))
            changed = sse.wait("changed", timeout=10,
                               predicate=lambda p: p.get("source") == "daemon")
            check(changed is not None, "a daemon write emits changed/daemon",
                  json.dumps(changed, default=str) if changed else "timeout")
            check(changed and changed.get("reason") == "task_created"
                  and isinstance(changed.get("mtime"), (int, float)),
                  "…carrying a reason and the new mtime",
                  json.dumps(changed, default=str))

            # An EXTERNAL write (a CLI write, a TUI save, iCloud landing a copy
            # from the other Mac) must also be noticed, by the mtime watcher.
            external = wt.load()
            external["tasks"][0].setdefault("logs", []).append({
                "id": wt.uid(), "minutes": 1.0, "note": "external writer",
                "at": time.time()})
            wt.save(external)
            changed = sse.wait("changed", timeout=10,
                               predicate=lambda p: p.get("source") == "external")
            check(changed is not None,
                  "an external write to the file emits changed/external "
                  "(the 1 Hz mtime watcher, §5.3)",
                  json.dumps(changed, default=str) if changed else "timeout")

            # The watcher must not cry wolf: no further external event when
            # nobody touches the file.
            time.sleep(0.6)
            sse.drain()
            time.sleep(0.6)
            spurious = [e for e in sse.drain()
                        if e[0] == "changed" and e[1].get("source") == "external"]
            check(not spurious,
                  "…and no spurious external changed events when the file is "
                  "untouched", str(spurious))
        finally:
            sse.close()

        # Two subscribers both get the event (fan-out), and unsubscribe is clean.
        first, second = SSEClient(h.port, h.token), SSEClient(h.port, h.token)
        try:
            time.sleep(0.3)
            status, body, _ = h.post("/v1/tasks", {"title": "fan out",
                                                   "role": "other"})
            got = [c.wait("changed", timeout=10,
                          predicate=lambda p: p.get("source") == "daemon")
                   for c in (first, second)]
            check(all(g is not None for g in got),
                  "both open streams receive the same changed event",
                  str(got))
        finally:
            first.close()
            second.close()
        # The handler notices a dead peer on its next write, i.e. within one
        # heartbeat. Poll rather than sleep a guessed amount.
        deadline = time.monotonic() + 10
        subscribers = None
        while time.monotonic() < deadline:
            subscribers = h.get("/v1/health")[1]["subscribers"]
            if subscribers == 0:
                break
            time.sleep(0.2)
        check(subscribers == 0, "…and closed streams are unsubscribed",
              str(subscribers))
    invariants(work, "the SSE test")


def test_lifecycle(wt, wt_daemon, migrated, scratch):
    section("13. lifecycle: attach-don't-double-bind, and never :7373 (§5.5)")
    fresh(wt, migrated, scratch / "lifecycle.json")

    # A free port answers nothing.
    import socket as _socket
    probe = _socket.socket()
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()
    check(not wt_daemon.health_answering(free_port, timeout=1.0),
          "health_answering() is False on a port with nothing on it")

    with DaemonHarness(wt_daemon) as h:
        check(wt_daemon.health_answering(h.port, timeout=2.0),
              "…and True against a running daemon")
        # A wrong token still proves something is listening: attaching is right,
        # double-binding is not.
        status, _body, _ = request(h.port, "GET", "/v1/health",
                                   token="definitely-wrong")
        check(status == 401, "…even though /v1/health itself needs the token",
              str(status))

        # main() must decline rather than raise EADDRINUSE.
        rc = wt_daemon.main(["--port", str(h.port),
                             "--token-file", str(scratch / "lifecycle.token"),
                             "--data-file", str(scratch / "lifecycle.json")])
        check(rc == 0,
              "main() on an already-served port exits 0 instead of binding",
              f"rc={rc}")

    # 7373 belongs to tracker.py. The daemon must refuse it, both ways round.
    for kwargs in ({"port": 7373}, {"port": 0, "legacy_port": 7373}):
        daemon = wt_daemon.Daemon("t", **kwargs)
        try:
            wt_daemon.make_servers(daemon)
            check(False, f"make_servers refuses {kwargs}", "it did not refuse")
        except SystemExit as exc:
            check("7373" in str(exc), f"make_servers refuses {kwargs}",
                  str(exc))
        except Exception as exc:  # noqa: BLE001
            check(False, f"make_servers refuses {kwargs}",
                  f"{type(exc).__name__}: {exc}")

    check(wt_daemon.TUI_BRIDGE_PORT == 7373
          and wt_daemon.DEFAULT_PORT == 7374
          and wt_daemon.DEFAULT_LEGACY_PORT == 7375,
          "the documented port assignment is unchanged",
          f"{wt_daemon.TUI_BRIDGE_PORT}/{wt_daemon.DEFAULT_PORT}/"
          f"{wt_daemon.DEFAULT_LEGACY_PORT}")

    # --print-token is the app's way to read the token without starting a server.
    token_file = scratch / "printed.token"
    rc = wt_daemon.main(["--print-token", "--token-file", str(token_file)])
    check(rc == 0 and token_file.exists()
          and stat.S_IMODE(token_file.stat().st_mode) == 0o600,
          "--print-token mints a 0600 token and exits without binding",
          f"rc={rc}")


class FakeIdle:
    """Swap ``wt_daemon.get_idle_seconds`` for a value the test controls.

    A context manager rather than a bare assignment so a failing check can never
    leave the real ``ioreg``-forking detector replaced for later sections.
    """

    def __init__(self, wt_daemon, seconds=0.0):
        self.wt_daemon = wt_daemon
        self.seconds = seconds
        self._saved = None

    def __enter__(self):
        self._saved = self.wt_daemon.get_idle_seconds
        self.wt_daemon.get_idle_seconds = lambda: self.seconds
        return self

    def __exit__(self, *exc):
        self.wt_daemon.get_idle_seconds = self._saved
        return False


def arm_timer(wt, path, *, elapsed_minutes, enabled=True, timeout=20,
              subtract=True):
    """Point wt at *path* and give it a timer that started *elapsed* ago.

    Returns ``(data, task)``. The task is the first non-done one, so the log
    assertions below have a stable subject.
    """
    data = wt.load()
    task = next(t for t in data["tasks"] if t.get("status") != "done")
    config = data.setdefault("config", {})
    config["presence_detection_enabled"] = enabled
    config["idle_timeout_minutes"] = timeout
    config["subtract_idle_time"] = subtract
    config.pop(wt_daemon_module().PENDING_IDLE_STOP_KEY, None)
    data["active_timer"] = {"task_id": task["id"],
                            "started_at": time.time() - elapsed_minutes * 60}
    wt.save(data)
    return data, task


def wt_daemon_module():
    import wt_daemon
    return wt_daemon


def logs_of(wt, task_id):
    data = wt.load()
    task = next(t for t in data["tasks"] if t["id"] == task_id)
    return list(task.get("logs") or [])


def test_presence(wt, wt_api, wt_daemon, migrated, scratch):
    section("14. presence: the daemon auto-stops an idle timer (and the TUI wins)")
    work = scratch / "presence.json"
    fresh(wt, migrated, work)

    # `strict` is the point of the section: an auto-stop is a *local* operation.
    # Any gh call at all — including the hours sync, which the harness disables —
    # hard-fails here rather than being noticed later by the fake-gh log.
    fixture_sprints = wt.get_cached_sprints(json.loads(Path(migrated).read_text()))
    with ApiStubs(wt, mode="strict", sprints=fixture_sprints) as stubs:

        # -- (i) presence_detection_enabled = false -> never stops -------------
        _data, task = arm_timer(wt, work, elapsed_minutes=60, enabled=False)
        before = len(logs_of(wt, task["id"]))
        with DaemonHarness(wt_daemon) as h, FakeIdle(wt_daemon, 30 * 60):
            reason = h.daemon._presence_pass()
            check(reason == "disabled",
                  "presence_detection_enabled=false -> the pass declines",
                  f"reason={reason}")
        after = wt.load()
        check(after.get("active_timer") is not None
              and len(logs_of(wt, task["id"])) == before,
              "…and the timer is still running, with no log written",
              f"timer={bool(after.get('active_timer'))} "
              f"logs {before}->{len(logs_of(wt, task['id']))}")

        # -- (ii) the TUI is up -> the daemon stands down ----------------------
        # THE correctness gate: tracker.py is already detecting idle, and its
        # save_data() rewrites tasks+active_timer wholesale from memory, so a
        # concurrent daemon stop is silently reverted or double-logged.
        _data, task = arm_timer(wt, work, elapsed_minutes=60)
        before = len(logs_of(wt, task["id"]))
        with DaemonHarness(wt_daemon) as h, FakeIdle(wt_daemon, 30 * 60):
            # Prime the cached :7373 probe rather than replacing the method, so
            # the real tui_bridge_running() is what the pass consults.
            h.daemon._tui_probe = (time.monotonic(), True)
            check(h.daemon.tui_bridge_running() is True,
                  "the :7373 probe reads as 'TUI running'")
            reason = h.daemon._presence_pass()
            check(reason == "tui_running",
                  "…so the presence pass stands down (the TUI is the detector)",
                  f"reason={reason}")
        check(wt.load().get("active_timer") is not None
              and len(logs_of(wt, task["id"])) == before,
              "…and it wrote nothing at all",
              f"logs {before}->{len(logs_of(wt, task['id']))}")

        # -- (iii) the real thing: one log, subtracted, with the TUI's note ----
        _data, task = arm_timer(wt, work, elapsed_minutes=60, timeout=20)
        before = logs_of(wt, task["id"])
        with DaemonHarness(wt_daemon) as h, FakeIdle(wt_daemon, 20 * 60):
            h.daemon._tui_probe = (time.monotonic(), False)
            events = SSEClient(h.port, h.token)
            time.sleep(0.2)
            reason = h.daemon._presence_pass()
            check(reason == "stopped", "20m idle past a 20m threshold -> stopped",
                  f"reason={reason}")

            after = logs_of(wt, task["id"])
            check(len(after) == len(before) + 1,
                  "exactly ONE log entry was written",
                  f"{len(before)} -> {len(after)}")
            entry = after[-1] if after else {}
            check(abs(float(entry.get("minutes") or 0) - 40.0) < 0.5,
                  "…carrying elapsed minus idle (60 - 20 = 40)",
                  str(entry.get("minutes")))
            check(entry.get("note") == "Timer session (auto-stopped, "
                                       "20m idle subtracted)",
                  "…and tracker.py's auto-stop note verbatim",
                  repr(entry.get("note")))
            check(wt.load().get("active_timer") is None,
                  "…and the timer is cleared")

            # -- (iv) the pending record, on both contracts --------------------
            status, body, _ = h.get("/v1/idle-stop")
            record = (body or {}).get("pending_idle_stop") or {}
            check(status == 200 and record.get("task_id") == task["id"]
                  and record.get("log_id") == entry.get("id"),
                  "GET /v1/idle-stop returns the pending record",
                  f"{status} {json.dumps(record, default=str)[:200]}")
            for key in ("id", "task_id", "task_title", "log_id",
                        "logged_minutes", "elapsed_minutes", "idle_minutes",
                        "at", "expires_at"):
                check(key in record, f"…the record carries {key!r}",
                      str(sorted(record)))
            check(abs(record["elapsed_minutes"] - 60) < 0.5
                  and abs(record["idle_minutes"] - 20) < 0.5,
                  "…with the full elapsed and the subtracted idle",
                  f"{record.get('elapsed_minutes')} / "
                  f"{record.get('idle_minutes')}")

            legacy_status, legacy_body, _ = request(
                h.legacy_port, "GET", "/idle-stop")
            check(legacy_status == 200
                  and (legacy_body or {}).get("pending_idle_stop", {})
                  .get("id") == record["id"],
                  "…and the unauthenticated :7375 contract serves the same one "
                  "(that is the port the monitor is pointed at)",
                  f"{legacy_status}")

            seen = events.wait("idle_stop", timeout=5)
            check(seen is not None and seen.get("task_id") == task["id"],
                  "an SSE idle_stop event carried the record",
                  json.dumps(seen, default=str)[:200])

            # /status must NOT have grown a key — the legacy oracle depends on it.
            _s, status_body, _ = request(h.legacy_port, "GET", "/status")
            check(set(status_body) == {"active_timer", "tasks", "time_by_role"},
                  "…while GET /status keeps exactly tracker's top-level keys",
                  str(sorted(status_body)))

            # -- (v) undo is a TRUE undo: entry deleted, timer resumed ---------
            # Not "restore the minutes and start a fresh timer" — that would
            # double-count — and not a timer from `now`, which would split one
            # session in two at an arbitrary idle boundary. The minutes go back
            # into the live timer and land as one entry at the real stop.
            u_status, undo, _ = request(h.legacy_port, "POST", "/idle-stop/undo",
                                        body={"id": record["id"]})
            check(u_status == 200 and undo.get("resumed") is True
                  and undo.get("mode") == "timer_resumed",
                  "POST /idle-stop/undo resumes the timer",
                  f"{u_status} {json.dumps(undo, default=str)[:240]}")
            restored = logs_of(wt, task["id"])
            check(len(restored) == len(before),
                  "…deleting the log entry the auto-stop wrote",
                  f"{len(after)} -> {len(restored)} (pre-stop {len(before)})")
            check(not any(l["id"] == entry["id"] for l in restored),
                  "…that exact entry specifically, not merely one of them")
            resumed = wt.load().get("active_timer") or {}
            check(resumed.get("task_id") == task["id"],
                  "…and active_timer is back on the same task",
                  json.dumps(resumed, default=str))
            check(abs(float(resumed.get("started_at") or 0)
                      - float(record["started_at"])) < 0.01,
                  "…with the ORIGINAL started_at, not `now` — so the session "
                  "stays continuous and the minutes are counted once",
                  f"{resumed.get('started_at')} vs {record['started_at']}")
            check(undo.get("counted_idle_minutes") is not None
                  and "idle" in (undo.get("detail") or ""),
                  "…and the body says out loud that the idle stretch now "
                  "counts as worked time", json.dumps(undo, default=str)[:240])

            # (2) No instant re-fire. The click is HID input in practice, but an
            # API-driven undo gets no such side effect.
            check(h.daemon._presence_pass() == "recently_resumed",
                  "the very next presence pass leaves the resumed timer alone")
            check((wt.load().get("active_timer") or {}).get("task_id")
                  == task["id"],
                  "…so the timer it was just asked to resume is still running")
            # A grace window, not an exemption: once it lapses, a genuinely
            # idle user is caught again.
            h.daemon._resumed_tasks.clear()
            check(h.daemon._presence_pass() == "stopped",
                  "…but once the window lapses it can fire again")

            # Re-arm for the idempotency check below: undo the second stop so
            # the record under test is the one we have been tracking.
            u2_status, undo2, _ = request(h.legacy_port, "POST",
                                          "/idle-stop/undo",
                                          body={"id": record["id"]})
            check(u2_status == 200 and undo2.get("resumed") is False
                  and undo2.get("cleared") is False,
                  "a second undo of the SAME record is a no-op, not a "
                  "second resume", f"{u2_status} {undo2}")
            _s, body_after, _ = request(h.legacy_port, "GET", "/idle-stop")
            pending_now = (body_after or {}).get("pending_idle_stop")
            check(pending_now is not None and pending_now["id"] != record["id"],
                  "…and what is pending is the *new* stop, not the old record",
                  str(pending_now and pending_now.get("id")))
            request(h.legacy_port, "POST", "/idle-stop/ack")

            done = events.wait("idle_stop_resolved", timeout=5)
            check(done is not None and done.get("action") == "undone",
                  "…and an idle_stop_resolved/undone event was published",
                  json.dumps(done, default=str)[:200])
            events.close()
        invariants(work, "the presence auto-stop + undo")

        # -- acknowledge, and a log edited out from under the undo -------------
        _data, task = arm_timer(wt, work, elapsed_minutes=60, timeout=20)
        with DaemonHarness(wt_daemon) as h, FakeIdle(wt_daemon, 20 * 60):
            h.daemon._tui_probe = (time.monotonic(), False)
            h.daemon._presence_pass()
            record = h.get("/v1/idle-stop")[1]["pending_idle_stop"]
            a_status, ack, _ = request(h.legacy_port, "POST", "/idle-stop/ack")
            check(a_status == 200 and ack.get("cleared") is True
                  and ack.get("action") == "acknowledged",
                  "POST /idle-stop/ack clears the record", f"{a_status} {ack}")
            kept = next(l for l in logs_of(wt, task["id"])
                        if l["id"] == record["log_id"])
            check(abs(float(kept["minutes"]) - 40) < 0.5,
                  "…leaving the entry as written, idle removed",
                  str(kept["minutes"]))
            check(request(h.legacy_port, "POST", "/idle-stop/ack")[1]
                  .get("cleared") is False,
                  "…and a second ack is an idempotent no-op")

        # -- undo must never clobber a timer the owner started meanwhile -------
        _data, task = arm_timer(wt, work, elapsed_minutes=60, timeout=20)
        with DaemonHarness(wt_daemon) as h, FakeIdle(wt_daemon, 20 * 60):
            h.daemon._tui_probe = (time.monotonic(), False)
            h.daemon._presence_pass()
            record = h.get("/v1/idle-stop")[1]["pending_idle_stop"]

            # Carlos came back and started working on something else.
            other = next(t for t in wt.load()["tasks"]
                         if t.get("status") != "done" and t["id"] != task["id"])
            status, _b, _ = h.post("/v1/timer/start", {"task_id": other["id"]})
            check(status == 200, "a different task's timer is started",
                  str(status))
            other_started = (wt.load().get("active_timer") or {}).get("started_at")

            _s, undo, _ = request(h.legacy_port, "POST", "/idle-stop/undo")
            check(undo.get("resumed") is False
                  and undo.get("mode") == "minutes_restored",
                  "undo falls back to restoring minutes rather than resuming",
                  json.dumps(undo, default=str)[:240])
            live = wt.load().get("active_timer") or {}
            check(live.get("task_id") == other["id"]
                  and abs(float(live.get("started_at") or 0)
                          - float(other_started)) < 0.01,
                  "…leaving the OTHER timer completely untouched",
                  json.dumps(live, default=str))
            kept = next(l for l in logs_of(wt, task["id"])
                        if l["id"] == record["log_id"])
            check(abs(float(kept["minutes"]) - 60) < 0.5,
                  "…and the auto-stopped task's entry holds the full session",
                  str(kept["minutes"]))
            check("another timer is running" in (undo.get("detail") or ""),
                  "…and says which branch it took, so a panel can be honest",
                  repr(undo.get("detail")))
            # Leave nothing running for the next block.
            request(h.legacy_port, "POST", "/timer/stop")

        _data, task = arm_timer(wt, work, elapsed_minutes=60, timeout=20)
        with DaemonHarness(wt_daemon) as h, FakeIdle(wt_daemon, 20 * 60):
            h.daemon._tui_probe = (time.monotonic(), False)
            h.daemon._presence_pass()
            record = h.get("/v1/idle-stop")[1]["pending_idle_stop"]
            # The owner adjusts the entry by hand before deciding. Their number
            # must win over the undo's.
            status, _b, _ = h.patch(
                f"/v1/tasks/{task['id']}/logs/{record['log_id']}",
                {"minutes": 12.5})
            check(status == 200, "the owner edits the entry to 12.5m", str(status))
            _s, undo, _ = request(h.legacy_port, "POST", "/idle-stop/undo")
            check(undo.get("restored") is False and "edited" in
                  (undo.get("detail") or ""),
                  "undo refuses to clobber a hand-edited entry",
                  json.dumps(undo, default=str))
            edited = next(l for l in logs_of(wt, task["id"])
                          if l["id"] == record["log_id"])
            check(abs(float(edited["minutes"]) - 12.5) < 0.01,
                  "…and the owner's 12.5m stands", str(edited["minutes"]))
            check(undo.get("resumed") is False
                  and wt.load().get("active_timer") is None,
                  "…and it resumes nothing either — the refusal is total",
                  json.dumps(wt.load().get("active_timer"), default=str))

        # -- the record expires rather than lingering -------------------------
        _data, task = arm_timer(wt, work, elapsed_minutes=60, timeout=20)
        with DaemonHarness(wt_daemon, idle_stop_ttl=0.4) as h, \
                FakeIdle(wt_daemon, 20 * 60):
            h.daemon._tui_probe = (time.monotonic(), False)
            h.daemon._presence_pass()
            check(h.get("/v1/idle-stop")[1]["pending_idle_stop"] is not None,
                  "a fresh record is pending")
            time.sleep(0.6)
            check(h.get("/v1/idle-stop")[1]["pending_idle_stop"] is None,
                  "…and reads as absent once its TTL lapses")

        # -- the loop thread actually runs, and stops ------------------------
        fresh_idle(wt, migrated, scratch / "presence-thread.json")
        with DaemonHarness(wt_daemon, presence=True, presence_interval=0.05) as h, \
                FakeIdle(wt_daemon, 0.0):
            time.sleep(0.3)
            thread = h.daemon._presence
            check(thread is not None and thread.is_alive(),
                  "--presence (the default) runs a wt-daemon-presence thread",
                  str(thread))
        check(thread is not None and not thread.is_alive(),
              "…and Daemon.stop() joins it")

        daemon = wt_daemon.Daemon("t", port=0, presence=False)
        daemon.start()
        daemon.stop()
        check(daemon._presence is None,
              "--no-presence starts no thread at all")
        args = wt_daemon.build_parser().parse_args(["--no-presence"])
        check(args.no_presence is True,
              "…and the CLI flag exists, mirroring --no-github-sync-on-stop")

        # -- (vi) not one GitHub call in the whole section --------------------
        check(not stubs.calls,
              "no gh-touching wt function was called anywhere above",
              str(stubs.names()))
    invariants(work, "the presence section")


def test_no_gh_escaped():
    section("15. belt and braces: no real gh invocation")
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


# ==================================================================== main ====

def main():
    if len(sys.argv) != 5:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    _fixture, migrated, baseline, scratch = (
        Path(a).expanduser() for a in sys.argv[1:])
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "token").mkdir(exist_ok=True)

    os.environ["WT_DATA_FILE"] = str(scratch / "unused.json")
    import wt
    import wt_api
    import wt_daemon

    if wt.DATA_FILE == LIVE_FILE:
        print("REFUSING TO RUN: wt.DATA_FILE is the live file", file=sys.stderr)
        return 2
    if not json.loads(Path(migrated).read_text()).get(
            "config", {}).get("sprints_cache"):
        print("REFUSING TO RUN: the fixture has no config.sprints_cache, so "
              "every sprint lookup would need the network", file=sys.stderr)
        return 2

    saved_modules = install_fake_desktop_modules()
    # An *outer* stub blanket over the whole run, on top of the per-section
    # ones. Several sections create tasks only incidentally (to have something
    # to block on, or to prove --allow-empty works), and wt_api.create_task
    # resolves the current sprint through wt.get_all_sprints() — which shells
    # out to `gh`. Without this, those incidental creates reach the network.
    # Nested Stubs save and restore the outer values, so this composes.
    fixture_sprints = wt.get_cached_sprints(
        json.loads(Path(migrated).read_text()))
    try:
        with ApiStubs(wt, mode="record", sprints=fixture_sprints):
            test_error_map(wt, wt_api, wt_daemon)
            test_token(wt_daemon, scratch)
            test_probe(wt, wt_daemon, migrated, scratch)
            test_auth_and_health(wt, wt_daemon, migrated, scratch)
            test_snapshot(wt, wt_api, wt_daemon, migrated, scratch)
            test_task_and_log_endpoints(wt, wt_api, wt_daemon, migrated,
                                        baseline, scratch)
            test_github_and_integrations(wt, wt_api, wt_daemon, migrated,
                                         scratch)
            test_long_operations(wt, wt_api, wt_daemon, migrated, scratch)
            test_transport_errors(wt, wt_daemon, migrated, scratch)
            test_risk_nine(wt, wt_daemon, migrated, scratch)
            test_lock_timeout(wt, wt_daemon, migrated, scratch)
            test_sse(wt, wt_daemon, migrated, scratch)
            test_lifecycle(wt, wt_daemon, migrated, scratch)
            test_presence(wt, wt_api, wt_daemon, migrated, scratch)
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
    print("All wt_daemon checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
