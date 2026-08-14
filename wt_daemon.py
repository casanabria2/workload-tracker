#!/usr/bin/env python3
"""wt_daemon — the local HTTP + SSE API (docs/plan-macos-app.md §5, Phase 2).

``wt.py`` holds the primitives, ``wt_api.py`` holds the command layer, and this
module is the *transport*: a stdlib-only ``ThreadingHTTPServer`` that owns the
data file and hands JSON to a client. It adds **no business logic** — every
mutation is a ``wt_api`` call inside one ``wt.data_lock()`` transaction. If you
find yourself computing sprint attribution or reportable hours here, it belongs
in ``wt_api``.

Two servers, deliberately different:

* **``127.0.0.1:7374`` — the v1 API.** Bearer-token authenticated (§5.1), no
  CORS. Everything under ``/v1/``. This is what the Swift client (Phase 3)
  speaks.
* **``127.0.0.1:7375`` — the legacy ``:7373`` contract** (§5.4), opt-in via
  ``--legacy-port``, **unauthenticated**. It reproduces ``tracker.py``'s
  ``_BridgeHandler`` byte for byte so the existing ``workload-macos-monitor``
  menu-bar agent keeps working with the TUI *closed*, without a single line of
  Swift changing (it sends no ``Authorization`` header, and requiring one would
  defeat the point). Loopback binding is the security boundary — the same one
  the TUI's bridge has always relied on. **The daemon never binds 7373**: that
  port stays the TUI's, so both can run at once.

Three invariants worth stating outright, because getting any of them wrong
loses the owner's work history:

1. **Every transaction is ``with wt.data_lock(required=True)``.** The lock is
   re-entrant within a process (Phase 0), so the inner ``wt.save()`` is free and
   needs no special form. ``required=True`` is not the default *policy* of
   ``wt.save()`` — a save is allowed to degrade to an unlocked write rather than
   drop a time entry — but a daemon must never do that silently, so a
   ``DataLockTimeout`` becomes a loud **503 ``lock_timeout``**.
2. **A successful ``wt.load()`` does not mean "there is data."** ``load()``
   swallows a parse failure and an unreadable file as ``{}``-defaults, which is
   indistinguishable from "no tasks" — and the documented second-Mac Full Disk
   Access failure presents exactly that way. A save on top of that clobbers the
   real, iCloud-synced file. So :func:`probe_data_file` runs **before every
   write**, and a failed probe refuses the write with **503 ``data_unreadable``**
   plus a machine-readable ``reason`` (plan risk #9).
3. **Arc is not wired in at all.** It is deprecated and disabled in the live
   config; the legacy stop path deliberately omits the TUI's Arc tab cleanup.
   Safari task windows (``browser_window.py``) *are* wired, because the monitor
   draws its window border from ``active_window_id``.
4. **Only one presence detector runs at a time.** The daemon's ``_presence_loop``
   auto-stops an idle timer — the thing that was missing entirely while the TUI
   was closed, which is most of the day now — but it stands down whenever
   ``tracker.py`` answers on :7373, because the TUI is already doing it and
   ``save_data()`` rewrites ``tasks``/``active_timer`` wholesale from memory. A
   concurrent stop would be silently reverted or double-logged.

Run it::

    venv/bin/python wt_daemon.py                        # :7374, v1 only
    venv/bin/python wt_daemon.py --legacy-port 7375     # + the monitor's contract
    venv/bin/python wt_daemon.py --data-file /tmp/x.json --port 18374

Verified by ``tools/test_daemon.py`` and ``tools/test_legacy_contract.py``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import queue
import re
import secrets
import signal
import socket
import sys
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import wt
import wt_api

try:  # macOS-only, stdlib-only. A module attribute so a test can monkeypatch it.
    from idle_detector import get_idle_seconds
except ImportError:  # pragma: no cover - only off a Mac
    def get_idle_seconds() -> float:
        """No idle detector available: never report the user as away."""
        return 0.0

DAEMON_VERSION = "1.0.0"
API_PREFIX = "/v1"

#: The v1 API port. 7373 is the TUI's and is never bound here.
DEFAULT_PORT = 7374
#: The legacy contract's port. Opt-in: ``--legacy-port`` with no value uses this.
DEFAULT_LEGACY_PORT = 7375
#: Probed (never bound) by ``/v1/health`` so a client can warn that the TUI is
#: running and may clobber the daemon's writes (plan risk #1).
TUI_BRIDGE_PORT = 7373

DEFAULT_TOKEN_FILE = Path.home() / ".workload_tracker_daemon_token"

HEARTBEAT_SECONDS = 15.0
WATCH_INTERVAL_SECONDS = 1.0
TUI_PROBE_CACHE_SECONDS = 2.0
MAX_OPERATIONS_KEPT = 50
MAX_BODY_BYTES = 4 * 1024 * 1024

#: Presence poll interval. Deliberately **not** the TUI's 1 Hz:
#: ``get_idle_seconds()`` forks ``ioreg``, and a 20-second granularity on a
#: 15-to-20-*minute* threshold costs nothing but 180 forks an hour instead of
#: 3600.
PRESENCE_INTERVAL_SECONDS = 20.0
#: The floor the *logged* (post-subtraction) minutes must clear on the auto-stop
#: path, matching ``tracker._auto_stop_timer``'s ``logged_minutes > 0.1``.
IDLE_STOP_MIN_MINUTES = 0.1
#: How long a pending idle-stop stays actionable. Long enough to survive lunch,
#: short enough that yesterday's auto-stop is never offered for undo today.
IDLE_STOP_TTL_SECONDS = 45 * 60
#: Where the pending record lives inside ``data["config"]``. Config is the right
#: home rather than daemon memory: the monitor may be down when the auto-stop
#: happens, and ``tracker.save_data()`` shallow-merges config from disk, so a key
#: the TUI has never heard of survives a TUI save.
PENDING_IDLE_STOP_KEY = "pending_idle_stop"

log = logging.getLogger("wt_daemon")


#: How a URL is handed to the OS. A module attribute so a test can replace it
#: without launching a browser.
OPEN_COMMAND = "/usr/bin/open"

#: The cmux CLI. cmux is the owner's terminal-and-browser; GitHub issues open
#: there rather than in the default browser.
CMUX_COMMAND = "cmux"
CMUX_BUNDLE_ID = "com.cmuxterm.app"
#: Where cmux keeps `automation.socketPassword`. Read at call time, never cached:
#: it is the owner's secret and rotating it should not need a daemon restart.
CMUX_SETTINGS_PATH = Path.home() / ".config" / "cmux" / "cmux.json"
#: How much of the task title goes into a new workspace name.
CMUX_TITLE_CHARS = 15


def cmux_workspace_name(issue_number: str, task_title: str) -> str:
    """`"316 - Teams integrat"` — the name for a freshly created workspace.

    Only the first :data:`CMUX_TITLE_CHARS` characters of the title, so a long
    task does not produce an unreadable workspace tab.
    """
    head = (task_title or "").strip()[:CMUX_TITLE_CHARS].rstrip()
    return f"{issue_number} - {head}" if head else issue_number


def cmux_workspace_ref_for_issue(issue_number: str,
                                 workspaces: list[dict]) -> str | None:
    """The ref of the first workspace whose title begins with *issue_number*.

    The digit-boundary check is the point: a bare ``startswith`` would let issue
    ``31`` adopt the workspace for ``316``, and silently file work under the
    wrong issue. A separator or end-of-string must follow the number.
    """
    for ws in workspaces:
        for key in ("title", "custom_title"):
            title = (ws.get(key) or "").strip()
            if not title.startswith(issue_number):
                continue
            rest = title[len(issue_number):]
            if rest == "" or not rest[0].isdigit():
                return ws.get("ref") or ws.get("id")
    return None


def _cmux_password() -> str | None:
    """`automation.socketPassword` from cmux's JSONC settings, or None.

    cmux refuses outside processes unless ``automation.socketControlMode`` is
    ``"password"`` and this is set — the daemon is a launchd agent, so it is
    always an outside process. The file is JSONC (comments, trailing commas), so
    strip those before parsing rather than reaching for a JSON5 dependency.
    """
    try:
        raw = CMUX_SETTINGS_PATH.read_text()
    except OSError:
        return None
    stripped = re.sub(r"(?m)^\s*//.*$", "", raw)
    match = re.search(r'"socketPassword"\s*:\s*"([^"]+)"', stripped)
    return match.group(1) if match else None


def _cmux(args: list[str], *, timeout: float = 20.0):
    """Run the cmux CLI with socket auth. Returns the CompletedProcess or None."""
    import subprocess
    password = _cmux_password()
    if not password:
        log.warning("no cmux automation.socketPassword in %s", CMUX_SETTINGS_PATH)
        return None
    env = dict(os.environ, CMUX_QUIET="1")
    try:
        return subprocess.run([CMUX_COMMAND, "--password", password, *args],
                              capture_output=True, text=True,
                              timeout=timeout, env=env)
    except FileNotFoundError:
        log.warning("cmux CLI (%s) not found", CMUX_COMMAND)
        return None
    except Exception:  # noqa: BLE001 - opening an issue must never kill the daemon
        log.warning("cmux %s failed", args, exc_info=True)
        return None


def _cmux_workspaces() -> list[dict] | None:
    """Every workspace cmux knows about, or None when cmux can't be reached."""
    proc = _cmux(["--json", "workspace", "list"])
    if proc is None or proc.returncode != 0:
        if proc is not None:
            log.warning("cmux workspace list rc=%s: %s",
                        proc.returncode, (proc.stderr or "").strip()[:200])
        return None
    try:
        return json.loads(proc.stdout).get("workspaces") or []
    except ValueError:
        log.warning("cmux workspace list returned non-JSON: %r", proc.stdout[:200])
        return None


def _cmux_launch_and_wait(timeout: float = 25.0) -> bool:
    """Start cmux if it is not running, and wait for its socket to answer."""
    ok, detail = _launch_via_open(["-b", CMUX_BUNDLE_ID])
    if not ok:
        log.warning("could not launch cmux: %s", detail)
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _cmux_workspaces() is not None:
            return True
        time.sleep(1.0)
    return False


def _open_issue_in_cmux(url: str, issue_number: str, task_title: str) -> dict:
    """Open *url* in the cmux workspace for *issue_number*, creating it if absent.

    Returns the fields the endpoint reports: ``opened``, ``workspace``,
    ``workspace_created`` and, on failure, ``detail``.
    """
    workspaces = _cmux_workspaces()
    if workspaces is None:
        # Chosen fallback: start cmux and retry, rather than quietly diverting
        # the issue to the default browser.
        if not _cmux_launch_and_wait():
            return {"opened": False, "workspace": None, "workspace_created": False,
                    "detail": "cmux is not reachable (socket denied or not running)"}
        workspaces = _cmux_workspaces() or []

    ref = cmux_workspace_ref_for_issue(issue_number, workspaces)
    created = False
    if ref is None:
        name = cmux_workspace_name(issue_number, task_title)
        proc = _cmux(["new-workspace", "--name", name])
        if proc is None or proc.returncode != 0:
            return {"opened": False, "workspace": None, "workspace_created": False,
                    "detail": f"could not create cmux workspace {name!r}"}
        # `new-workspace` answers "OK workspace:11".
        ref = (proc.stdout or "").split()[-1].strip() or None
        created = True
        if ref is None:
            return {"opened": False, "workspace": None, "workspace_created": True,
                    "detail": "cmux created a workspace but returned no ref"}

    proc = _cmux(["open", url, "--workspace", ref])
    if proc is None or proc.returncode != 0:
        detail = (proc.stderr or "").strip()[:200] if proc else "cmux unavailable"
        return {"opened": False, "workspace": ref, "workspace_created": created,
                "detail": f"could not open the issue in {ref}: {detail}"}
    return {"opened": True, "workspace": ref, "workspace_created": created}


def _launch_via_open(args: list[str]) -> tuple[bool, str]:
    """Run ``/usr/bin/open`` with *args*. Returns (ok, detail).

    Launch Services, and **never** ``webbrowser``. On macOS ``webbrowser.get()``
    resolves to ``MacOSXOSAScript``, which drives the target app through
    ``osascript`` — an Apple Event, so it needs an Automation (TCC) grant. This
    daemon is a launchd agent and can never be given one interactively, so
    ``webbrowser.open()`` returned False on every call while no window appeared.
    Launch Services needs no such grant.

    Used to start cmux by bundle id when its socket is not answering.
    """
    import subprocess
    try:
        proc = subprocess.run([OPEN_COMMAND, *args],
                              capture_output=True, text=True, timeout=15)
    except Exception as exc:  # noqa: BLE001 - never take the daemon down
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return False, f"rc={proc.returncode} {(proc.stderr or '').strip()[:200]}"
    return True, ""


# ============================================================ error mapping ===
#
# ``wt_api.ERROR_CODES`` is API surface — the Swift client switches on the code,
# not the message — so the code→status mapping is API surface too. Every one of
# the 23 codes appears here exactly once; :func:`_assert_error_map_complete`
# fails at import if that ever stops being true.

#: Machine codes this module raises that are *not* ``wt_api`` codes. Same
#: contract: stable, greppable, part of the API.
DAEMON_ERROR_CODES = {
    "unauthorized":    401,  # missing or wrong bearer token
    "not_found":       404,  # no such route
    "method_not_allowed": 405,
    "bad_json":        400,  # unparseable / non-object request body
    "bad_request":     400,  # a required field is missing or the wrong type
    "lock_timeout":    503,  # wt.DataLockTimeout — another writer held the lock
    "data_unreadable": 503,  # risk #9: refusing to write over an unreadable file
    "unavailable":     503,  # an optional local integration is not importable
    "internal_error":  500,
}

#: ``wt_api`` code -> HTTP status.
ERROR_STATUS = {
    # 404 — the named thing does not exist.
    "task_not_found":   404,
    "log_not_found":    404,
    "issue_not_found":  404,
    "sprint_not_found": 404,

    # 400 — the request itself is malformed or invalid.
    "invalid_role":     400,
    "invalid_status":   400,
    "invalid_repo":     400,
    "unknown_activity": 400,
    "unknown_type":     400,
    "no_default_repo":  400,
    "no_changes":       400,
    "invalid_minutes":  400,
    "invalid_split":    400,
    "same_log":         400,
    "invalid_args":     400,

    # 409 — the request is well-formed but conflicts with current state. These
    # are the ones a client should *not* retry verbatim: it must disambiguate,
    # set a repo, link an issue, or start a timer first.
    "ambiguous_task":   409,
    "no_repo":          409,
    "not_linked":       409,
    "no_active_timer":  409,

    # 502 — we are the gateway and GitHub (or the workflow over it) failed.
    "close_failed":     502,
    "reconcile_failed": 502,
    "github_failed":    502,

    # 503 — a dependency is unavailable (no sprint source, live or cached).
    "no_sprints":       503,
}


def _assert_error_map_complete():
    """Fail loudly at import if ``wt_api`` grew or lost a code."""
    declared = set(wt_api.ERROR_CODES)
    mapped = set(ERROR_STATUS)
    if declared != mapped:
        raise RuntimeError(
            "wt_daemon.ERROR_STATUS is out of sync with wt_api.ERROR_CODES: "
            f"unmapped={sorted(declared - mapped)} stale={sorted(mapped - declared)}"
        )
    overlap = declared & set(DAEMON_ERROR_CODES)
    if overlap:
        raise RuntimeError(f"daemon codes collide with wt_api codes: {sorted(overlap)}")


_assert_error_map_complete()


class DaemonError(Exception):
    """A transport-level failure carrying a :data:`DAEMON_ERROR_CODES` code."""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.status = DAEMON_ERROR_CODES.get(code, 500)

    def as_dict(self) -> dict:
        body = {"code": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return {"error": body}


def error_response(exc: Exception) -> tuple[int, dict]:
    """``(status, body)`` for any exception a handler can raise.

    The body shape is always ``{"error": {"code", "message", "details"?}}`` —
    including for unexpected exceptions, which become ``internal_error`` rather
    than an HTML traceback page.
    """
    if isinstance(exc, DaemonError):
        return exc.status, exc.as_dict()
    if isinstance(exc, wt_api.WtError):
        return ERROR_STATUS.get(exc.code, 500), exc.as_dict()
    if isinstance(exc, wt.DataLockTimeout):
        return 503, DaemonError("lock_timeout", str(exc)).as_dict()
    log.exception("unhandled error in handler")
    return 500, DaemonError("internal_error",
                            f"{type(exc).__name__}: {exc}").as_dict()


# ============================================================ risk #9 probe ===

def probe_data_file(path: Path | None = None) -> dict:
    """Is the data file *actually* readable, or is ``load()`` masking a failure?

    ``wt.load()`` returns ``{"tasks": [], ...}`` for a missing file, an
    EPERM-under-TCC file, and a corrupt file alike (plan §3.1). Writing on top of
    any of those destroys the synced copy, so this distinguishes them *before*
    the first write of a transaction.

    ``reason`` is one of ``ok`` / ``missing`` / ``permission_denied`` /
    ``empty_file`` / ``unparseable`` / ``no_tasks``. ``permission_denied`` is the
    documented second-Mac Full Disk Access symptom (EPERM, "Operation not
    permitted"); ``empty_file`` is what a dataless iCloud placeholder looks like.
    """
    path = Path(path) if path is not None else Path(wt.DATA_FILE)
    out = {"path": str(path), "readable": False, "reason": None,
           "mtime": None, "size": None, "tasks": None, "detail": None}
    try:
        st = path.stat()
    except FileNotFoundError:
        out["reason"] = "missing"
        return out
    except OSError as exc:
        out["reason"] = "permission_denied"
        out["detail"] = str(exc)
        return out

    out["mtime"] = st.st_mtime
    out["size"] = st.st_size
    if st.st_size == 0:
        out["reason"] = "empty_file"
        return out

    try:
        raw = path.read_text()
    except OSError as exc:
        out["reason"] = "permission_denied"
        out["detail"] = str(exc)
        return out

    try:
        doc = json.loads(raw)
    except ValueError as exc:
        out["reason"] = "unparseable"
        out["detail"] = str(exc)
        return out

    if not isinstance(doc, dict):
        out["reason"] = "unparseable"
        out["detail"] = f"top level is {type(doc).__name__}, not an object"
        return out

    tasks = doc.get("tasks")
    if not isinstance(tasks, list):
        out["reason"] = "no_tasks"
        out["detail"] = "no 'tasks' array"
        return out

    out["tasks"] = len(tasks)
    if not tasks:
        out["reason"] = "no_tasks"
        return out

    out["readable"] = True
    out["reason"] = "ok"
    return out


# =========================================================== idle-stop record ==
#
# When the presence loop stops a timer it leaves a **pending idle-stop** behind:
# everything the menu-bar monitor needs to show its green panel ("we removed 20m
# of idle time from 'Task X'") and to undo that removal on one click.
#
# Undo semantics, decided deliberately:
#
#   * undo puts the subtracted minutes **back on the log entry** (minutes ->
#     the full elapsed session, note back to a plain ``"Timer session"``);
#   * undo does **not** restart the timer — the user was away, and silently
#     resuming a clock they cannot see is how the 5.5-hour bogus entry happened
#     in the first place;
#   * acknowledge (the default) leaves the entry as written, with idle removed.
#
# Both are idempotent, and both degrade to a reported no-op if the entry was
# edited or deleted in the meantime rather than clobbering the owner's edit.


def pending_idle_stop(data: dict, *, now: float | None = None) -> dict | None:
    """The still-actionable pending idle-stop in *data*, or None.

    Resolved and expired records read as absent, so no consumer has to know
    about the TTL. The record is *kept* rather than deleted once resolved: an
    older ``tracker.py`` holding a stale config in memory would otherwise
    resurrect the unresolved copy on its next ``save_data()``.
    """
    record = (data.get("config") or {}).get(PENDING_IDLE_STOP_KEY)
    if not isinstance(record, dict) or not record.get("id"):
        return None
    if record.get("resolved"):
        return None
    now = time.time() if now is None else now
    if float(record.get("expires_at") or 0) <= now:
        return None
    return record


def expired_idle_stop_id(data: dict, *, now: float | None = None) -> str | None:
    """The id of an unresolved-but-expired record, so the loop can retire it."""
    record = (data.get("config") or {}).get(PENDING_IDLE_STOP_KEY)
    if not isinstance(record, dict) or not record.get("id"):
        return None
    if record.get("resolved"):
        return None
    now = time.time() if now is None else now
    if float(record.get("expires_at") or 0) <= now:
        return record["id"]
    return None


def mark_idle_stop_resolved(data: dict, record: dict, action: str) -> dict:
    """Stamp *record* as handled. ``action`` is acknowledged/undone/expired."""
    record["resolved"] = action
    record["resolved_at"] = time.time()
    data.setdefault("config", {})[PENDING_IDLE_STOP_KEY] = record
    return record


def _presence_state(data: dict) -> dict:
    """Everything one presence pass needs, read in a single transaction.

    Defaults match ``tracker._check_presence`` exactly, including
    ``presence_detection_enabled`` defaulting to **False**: an absent key means
    the owner never turned this on, and the daemon must not be the surface that
    starts stopping their timers.
    """
    config = data.get("config") or {}
    active = data.get("active_timer") or {}
    try:
        timeout = float(config.get("idle_timeout_minutes", 15) or 15)
    except (TypeError, ValueError):
        timeout = 15.0
    return {
        "enabled": bool(config.get("presence_detection_enabled", False)),
        "timeout_minutes": timeout,
        "subtract": bool(config.get("subtract_idle_time", True)),
        "task_id": active.get("task_id"),
        "started_at": active.get("started_at"),
        "expired_id": expired_idle_stop_id(data),
    }


def _undo_idle_stop(data: dict, record: dict) -> dict:
    """Put the subtracted idle minutes back. Never clobbers a later edit.

    Three graceful misses, each reported rather than raised: the task is gone,
    the log entry was deleted, or its minutes no longer match what the auto-stop
    wrote (i.e. the owner already adjusted it by hand, and their number wins).
    The fourth case is not a miss: when the idle tail swallowed the whole
    session no entry was written at all, and undo *creates* one.
    """
    full = round(float(record.get("elapsed_minutes") or 0.0), 2)
    written = float(record.get("logged_minutes") or 0.0)
    task = next((t for t in data.get("tasks", [])
                 if t.get("id") == record.get("task_id")), None)
    if task is None:
        return {"restored": False, "detail": "the task no longer exists"}

    logs = task.setdefault("logs", [])
    log_id = record.get("log_id")
    entry = next((l for l in logs if l.get("id") == log_id), None) if log_id \
        else None
    if log_id and entry is None:
        return {"restored": False,
                "detail": "the log entry was deleted since the auto-stop"}
    if entry is not None and \
            abs(float(entry.get("minutes") or 0.0) - written) > 0.01:
        return {"restored": False,
                "detail": "the log entry was edited since the auto-stop"}
    if full <= 0.0:
        return {"restored": False, "detail": "there is nothing to restore"}

    if entry is None:
        entry = {"id": wt_api.uid(),
                 "at": record.get("ended_at") or time.time()}
        for key in ("started_at", "ended_at"):
            if record.get(key) is not None:
                entry[key] = record[key]
        logs.append(entry)
    entry["minutes"] = full
    entry["note"] = "Timer session"
    return {"restored": True, "minutes": full, "log_id": entry["id"],
            "detail": None}


# ================================================================ SSE broker ===

class EventBroker:
    """Fan-out of server-sent events to every open ``/v1/events`` stream.

    Each subscriber gets a bounded queue. A subscriber that stops draining is
    **dropped**, not backpressured: blocking a mutation because some client
    stalled would be strictly worse than that client reconnecting and refetching
    the snapshot (which is what a reconnect does anyway).
    """

    def __init__(self, maxsize: int = 512):
        self._lock = threading.Lock()
        self._subs: set[queue.Queue] = set()
        self._maxsize = maxsize
        self._seq = 0

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._maxsize)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: str, payload: dict) -> int:
        with self._lock:
            self._seq += 1
            frame = (self._seq, event, payload)
            for q in list(self._subs):
                try:
                    q.put_nowait(frame)
                except queue.Full:
                    self._subs.discard(q)
                    log.warning("dropped a stalled SSE subscriber")
            return self._seq

    @property
    def subscribers(self) -> int:
        with self._lock:
            return len(self._subs)


# ==================================================================== daemon ===

class Daemon:
    """Owns the data file, the SSE broker, the watcher and the operation log.

    One instance is shared by both HTTP servers, so a legacy ``POST
    /timer/start`` on :7375 emits the same ``changed`` event a v1 client on :7374
    is listening for.
    """

    def __init__(self, token: str, *, allow_empty: bool = False,
                 port: int = DEFAULT_PORT, legacy_port: int | None = None,
                 heartbeat_seconds: float = HEARTBEAT_SECONDS,
                 watch_interval: float = WATCH_INTERVAL_SECONDS,
                 github_sync_on_stop: bool = True,
                 presence: bool = True,
                 presence_interval: float = PRESENCE_INTERVAL_SECONDS,
                 idle_stop_ttl: float = IDLE_STOP_TTL_SECONDS):
        self.token = token
        self.allow_empty = allow_empty
        self.port = port
        self.legacy_port = legacy_port
        self.heartbeat_seconds = heartbeat_seconds
        self.watch_interval = watch_interval
        self.github_sync_on_stop = github_sync_on_stop
        self.presence = presence
        self.presence_interval = presence_interval
        self.idle_stop_ttl = idle_stop_ttl

        self.broker = EventBroker()
        self.started_at = time.time()

        self._mtime_lock = threading.Lock()
        self._last_mtime = self._current_mtime()
        self._writing = 0

        self._ops_lock = threading.Lock()
        self._ops: OrderedDict[str, dict] = OrderedDict()

        self._workers: set[threading.Thread] = set()
        self._workers_lock = threading.Lock()

        self._tui_probe = (0.0, False)
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None
        self._presence: threading.Thread | None = None
        # Ids resolved during *this* daemon's lifetime. Belt and braces against
        # a running tracker.py resurrecting a pre-resolve config from memory:
        # the record would come back unresolved on disk, but the monitor asks
        # us, and we remember. Bounded by one record per auto-stop.
        self._resolved_idle_stops: set[str] = set()

    # ---------------------------------------------------------- lifecycle ----

    def start(self):
        self._watcher = threading.Thread(target=self._watch_loop,
                                         name="wt-daemon-watch", daemon=True)
        self._watcher.start()
        if self.presence:
            self._presence = threading.Thread(target=self._presence_loop,
                                              name="wt-daemon-presence",
                                              daemon=True)
            self._presence.start()

    def stop(self):
        self._stop.set()
        for thread in (self._watcher, self._presence):
            if thread is not None:
                thread.join(timeout=2.0)

    def join_background(self, timeout: float = 10.0):
        """Wait for spawned worker threads (hours sync, long operations).

        Exists for the harnesses: their ``gh`` stubs are installed for the
        duration of a ``with`` block, and a worker still running when the block
        exits would find the real functions restored under it.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._workers_lock:
                alive = [t for t in self._workers if t.is_alive()]
                self._workers = set(alive)
            if not alive:
                return True
            time.sleep(0.02)
        return False

    def _spawn(self, name: str, fn):
        thread = threading.Thread(target=fn, name=name, daemon=True)
        with self._workers_lock:
            self._workers = {t for t in self._workers if t.is_alive()}
            self._workers.add(thread)
        thread.start()
        return thread

    # -------------------------------------------------------------- events ---

    def publish(self, event: str, payload: dict) -> int:
        return self.broker.publish(event, payload)

    def emit_changed(self, source: str, reason: str, **extra):
        """``changed`` — "refetch the snapshot".

        ``source`` is ``"daemon"`` (we wrote) or ``"external"`` (the watcher saw
        the file move: a CLI write, a TUI save, or iCloud landing a copy from the
        other Mac).
        """
        payload = {"source": source, "reason": reason,
                   "mtime": self._current_mtime_float(), "at": time.time()}
        payload.update(extra)
        return self.publish("changed", payload)

    # ---------------------------------------------------------- file watch ---

    def _current_mtime(self):
        try:
            return Path(wt.DATA_FILE).stat().st_mtime_ns
        except OSError:
            return None

    def _current_mtime_float(self):
        ns = self._current_mtime()
        return None if ns is None else ns / 1e9

    def _watch_loop(self):
        while not self._stop.wait(self.watch_interval):
            try:
                with self._mtime_lock:
                    current = self._current_mtime()
                    if self._writing:
                        # Our own os.replace is in flight (or just landed);
                        # the transaction emits its own `changed`.
                        self._last_mtime = current
                        continue
                    moved = current != self._last_mtime
                    self._last_mtime = current
                if moved:
                    self.emit_changed("external", "file_mtime")
            except Exception:  # noqa: BLE001 - the watcher must never die
                log.exception("watcher iteration failed")

    # ------------------------------------------------------ presence/idle ----

    def _presence_loop(self):
        """Stop a forgotten timer when the user has been away long enough.

        Presence detection used to live **only** in ``tracker.py``'s 1 Hz tick,
        but there are five headless ways to start a timer (``wt start``, both
        daemon ports, the macOS app, MCP) and the daemon is what actually runs
        all day as a LaunchAgent. With the TUI closed — the normal case now — an
        idle timer simply never stopped; that is how a 5.5-hour bogus entry got
        written.
        """
        while not self._stop.wait(self.presence_interval):
            try:
                self._presence_pass()
            except Exception:  # noqa: BLE001 - the loop must never die
                log.exception("presence iteration failed")

    def _presence_pass(self):
        """One poll. Returns the reason it did nothing, for tests and logs.

        **The 7373 gate is the correctness requirement here.** When the TUI is
        up it is already running its own presence check, and
        ``tracker.save_data()`` rewrites ``tasks`` and ``active_timer``
        wholesale from memory — so a concurrent daemon stop is either silently
        reverted or duplicated as a second log entry. Exactly one detector runs
        at a time, and while the TUI is open it is the TUI's.
        """
        if self.tui_bridge_running():
            return "tui_running"

        state = self.read(_presence_state)
        if state["expired_id"]:
            self._retire_pending_idle_stop(state["expired_id"])
        if not state["enabled"]:
            return "disabled"
        if not state["task_id"]:
            return "no_timer"

        idle_seconds = get_idle_seconds()
        if idle_seconds / 60 < state["timeout_minutes"]:
            return "not_idle"

        # Re-probe: an ioreg fork is not instant, and the TUI coming up during
        # it would make this the second detector.
        if self.tui_bridge_running():
            return "tui_running"
        self.auto_stop_idle_timer(idle_seconds, timeout_minutes=state["timeout_minutes"])
        return "stopped"

    def auto_stop_idle_timer(self, idle_seconds: float, *,
                             timeout_minutes: float | None = None) -> dict | None:
        """Commit the running session minus the idle tail; leave an undo record.

        The stop goes through the daemon's existing trio —
        ``wt_api.stop_timer`` inside :meth:`write`, then
        :meth:`sync_hours_async` — exactly as ``_legacy_stop`` and
        ``h_timer_stop`` do, rather than re-implementing the TUI's version.
        Safari task windows are **not** touched (``browser=False``): that
        integration is deprecated and an auto-stop is not the place to reach out
        and rearrange the desktop.
        """
        idle_minutes = idle_seconds / 60

        def run(data):
            config = data.setdefault("config", {})
            active_timer = data.get("active_timer") or {}
            started_at = active_timer.get("started_at")
            if config.get("subtract_idle_time", True):
                note = ("Timer session (auto-stopped, "
                        f"{int(idle_minutes)}m idle subtracted)")
                subtract = idle_minutes
            else:
                note = "Timer session (auto-stopped due to inactivity)"
                subtract = 0.0
            result = wt_api.stop_timer(data, browser=False, note=note,
                                       subtract_minutes=subtract,
                                       min_minutes=IDLE_STOP_MIN_MINUTES)
            entry = result.get("log")
            now = time.time()
            record = {
                "id": wt_api.uid(),
                "task_id": result.get("task_id"),
                "task_title": result.get("title"),
                "log_id": (entry or {}).get("id"),
                # What the entry says now, what the clock actually ran for, and
                # the difference the undo hands back.
                "logged_minutes": round(result.get("logged_minutes") or 0.0, 2),
                "elapsed_minutes": round(result.get("minutes") or 0.0, 2),
                "idle_minutes": round(result.get("subtracted_minutes") or 0.0, 2),
                "idle_timeout_minutes": timeout_minutes,
                "note": note,
                "started_at": started_at,
                "ended_at": now,
                "at": now,
                "expires_at": now + self.idle_stop_ttl,
                "resolved": None,
                "resolved_at": None,
            }
            config[PENDING_IDLE_STOP_KEY] = record
            return record

        try:
            record = self.write(run, reason="timer_auto_stopped")
        except wt_api.WtError as exc:
            # Another writer stopped it between the read and the write.
            if exc.code == "no_active_timer":
                return None
            raise
        log.info("auto-stopped '%s' after %.1fm idle: logged %.2fm of %.2fm",
                 record.get("task_title"), idle_minutes,
                 record.get("logged_minutes"), record.get("elapsed_minutes"))
        self.sync_hours_async(record.get("task_id"))
        self.publish("idle_stop", dict(record))
        return record

    def pending_idle_stop(self) -> dict | None:
        """The record the monitor should be showing, or None."""
        record = self.read(pending_idle_stop)
        if record and record.get("id") in self._resolved_idle_stops:
            return None
        return record

    def _retire_pending_idle_stop(self, record_id: str):
        """Expire a record nobody acted on, so the file does not carry it."""
        def run(data):
            record = (data.get("config") or {}).get(PENDING_IDLE_STOP_KEY)
            if not isinstance(record, dict) or record.get("id") != record_id \
                    or record.get("resolved"):
                return None
            return dict(mark_idle_stop_resolved(data, record, "expired"))

        record = self.write(run, reason="idle_stop_expired")
        if record is not None:
            self._resolved_idle_stops.add(record_id)
            self.publish("idle_stop_resolved",
                         {"id": record_id, "action": "expired",
                          "task_id": record.get("task_id"), "at": time.time()})
        return record

    def resolve_idle_stop(self, action: str,
                          record_id: str | None = None) -> dict:
        """Acknowledge or undo the pending idle-stop. Idempotent either way.

        ``acknowledged`` keeps the entry as written (idle removed);
        ``undone`` restores the subtracted minutes onto the log entry and
        reverts its note. Neither restarts the timer. A record that is already
        resolved, expired, or whose log entry has since been edited or deleted
        answers 200 with ``restored``/``cleared`` false and a ``detail`` — never
        an error, and never a clobbered edit.
        """
        if action not in ("acknowledge", "undo"):
            raise DaemonError("bad_request",
                              "'action' must be acknowledge or undo",
                              action=action)
        resolved_as = "acknowledged" if action == "acknowledge" else "undone"
        miss = {"action": resolved_as, "cleared": False, "restored": False,
                "id": record_id, "detail": "no pending idle-stop"}

        current = self.pending_idle_stop()
        if current is None or (record_id and current.get("id") != record_id):
            return miss

        def run(data):
            record = pending_idle_stop(data)
            if record is None or (record_id and record.get("id") != record_id):
                return dict(miss)
            out = {"action": resolved_as, "cleared": True, "restored": False,
                   "id": record["id"], "task_id": record.get("task_id"),
                   "task_title": record.get("task_title"), "detail": None,
                   "minutes": record.get("logged_minutes")}
            if action == "undo":
                out.update(_undo_idle_stop(data, record))
            mark_idle_stop_resolved(data, record, resolved_as)
            return out

        out = self.write(run, reason=f"idle_stop_{resolved_as}")
        if out.get("cleared"):
            self._resolved_idle_stops.add(out["id"])
            self.publish("idle_stop_resolved",
                         {"id": out["id"], "action": resolved_as,
                          "task_id": out.get("task_id"),
                          "restored": out.get("restored"),
                          "minutes": out.get("minutes"), "at": time.time()})
        return out

    # -------------------------------------------------------- transactions ---

    @contextlib.contextmanager
    def _locked(self):
        """``wt.data_lock(required=True)``, with the timeout as a 503.

        Nothing inside the body can raise ``DataLockTimeout`` of its own — a
        nested ``wt.save()`` re-enters the process-wide ``RLock`` on the same
        thread and never blocks — so catching it around the whole ``with`` is
        unambiguous.
        """
        try:
            with wt.data_lock(required=True):
                yield
        except wt.DataLockTimeout as exc:
            raise DaemonError("lock_timeout", str(exc),
                              timeout_seconds=wt.DATA_LOCK_TIMEOUT_SECONDS) from exc

    #: What a guarded read hands to a caller when the file is unusable. Shaped
    #: like ``wt.load()``'s defaults so every consumer works unchanged.
    _EMPTY = {"tasks": [], "active_timer": None, "roles": [], "config": {}}

    def _guarded_load(self):
        """``(data, probe)`` — ``wt.load()``, but never over an unusable file.

        **``wt.load()`` is itself a read-modify-write.** It runs four migrations
        and ``save()``s when any of them mutated, so on a missing, zero-byte,
        corrupt or EPERM-under-TCC file it materialises a ``{}``-default document
        *over the real one*. Measured on the live-shaped fixture: one
        ``wt.load()`` against a chmod-000 copy turned 210 KB of history into a
        520-byte empty document, mode and all — risk #9 arriving through a plain
        ``GET``, with no write endpoint involved.

        So the probe gates **reads** as well as writes. An unusable file yields
        an in-memory empty document and the probe that explains why; the client
        renders the Full-Disk-Access state from ``data_file`` rather than from
        ``len(tasks)``.
        """
        probe = probe_data_file()
        if probe["readable"] or self.allow_empty:
            return wt.load(), probe
        return json.loads(json.dumps(self._EMPTY)), probe

    def read(self, fn):
        """Run *fn(data)* under the lock, without ever writing. See §risk #9."""
        return self.read_with_probe(fn)[0]

    def read_with_probe(self, fn):
        """:meth:`read`, also returning the probe taken under the same lock."""
        with self._locked():
            data, probe = self._guarded_load()
            return fn(data), probe

    def write(self, fn, *, reason: str, **changed_extra):
        """lock → risk-#9 probe → ``load()`` → *fn(data)* → ``save()`` → ``changed``.

        *fn* may itself persist (``wt_api.close`` and friends take a
        ``save_callback`` because ``wt.close_task`` writes mid-workflow so a late
        ``gh`` failure cannot strand the task). That is safe: ``data_lock`` is
        re-entrant, so the nested ``wt.save()`` re-enters rather than deadlocks.

        ``changed`` is emitted whenever the file's mtime actually moved —
        **including when *fn* raised**, because a half-completed close has
        already persisted some of its work and a client that never heard about
        it would render stale state.
        """
        moved = False
        try:
            with self._locked():
                probe = probe_data_file()
                if not probe["readable"] and not self.allow_empty:
                    raise DaemonError(
                        "data_unreadable",
                        _unreadable_message(probe),
                        **{k: probe[k] for k in ("path", "reason", "size",
                                                 "tasks", "detail")})
                before = self._current_mtime()
                with self._mtime_lock:
                    self._writing += 1
                try:
                    data = wt.load()
                    result = fn(data)
                    wt.save(data)
                finally:
                    after = self._current_mtime()
                    moved = after != before
                    with self._mtime_lock:
                        self._writing -= 1
                        self._last_mtime = after
        finally:
            if moved:
                self.emit_changed("daemon", reason, **changed_extra)
        return result

    def sync_hours_async(self, task_id):
        """Mirror the TUI's ``_sync_task_hours_async`` after a timer stop.

        On a worker thread on purpose: the monitor's ``URLSession`` gives every
        request a 4-second timeout, and a ``gh project`` round trip can exceed
        that. A slow GitHub must not make a *successful* stop look unreachable.
        """
        if not task_id or not self.github_sync_on_stop:
            return None

        def work():
            try:
                def do(data):
                    task = next((t for t in data.get("tasks", [])
                                 if t.get("id") == task_id), None)
                    if task is None:
                        return None
                    ref = wt.task_current_issue(task, data)
                    if not ref:
                        return None
                    return wt.sync_project_hours(ref, task, data, wt.save)

                synced = self.write(do, reason="hours_synced", task_id=task_id)
                if synced is not None:
                    self.publish("progress",
                                 {"operation_id": None, "op": "sync_hours",
                                  "task_id": task_id, "state": "completed",
                                  "message": f"hours synced: {bool(synced)}",
                                  "at": time.time()})
            except Exception as exc:  # noqa: BLE001 - never kill the worker
                status, body = error_response(exc)
                self.publish("error", {"op": "sync_hours", "task_id": task_id,
                                       "status": status, "at": time.time(),
                                       **body})

        return self._spawn(f"wt-hours-{task_id}", work)

    # ---------------------------------------------------------- operations ---

    def operation(self, oid: str) -> dict | None:
        with self._ops_lock:
            op = self._ops.get(oid)
            return dict(op) if op else None

    def start_operation(self, op_name: str, task_id: str | None, fn) -> dict:
        """Kick off a long (``gh``-touching) operation and return its 202 record.

        *fn* is called as ``fn(on_progress)`` on a worker thread. Progress lines
        stream as SSE ``progress`` events; the terminal state is a ``progress``
        event with ``state == "completed"`` carrying ``result``, or an ``error``
        event. ``GET /v1/operations/{id}`` returns the same record for a client
        that reconnected and missed the stream.
        """
        oid = wt_api.uid()
        record = {"operation_id": oid, "op": op_name, "task_id": task_id,
                  "state": "running", "started_at": time.time(),
                  "finished_at": None, "progress": [], "result": None,
                  "error": None}
        with self._ops_lock:
            self._ops[oid] = record
            while len(self._ops) > MAX_OPERATIONS_KEPT:
                self._ops.popitem(last=False)

        def on_progress(message):
            text = str(message)
            with self._ops_lock:
                record["progress"].append(text)
            self.publish("progress", {"operation_id": oid, "op": op_name,
                                      "task_id": task_id, "state": "running",
                                      "message": text, "at": time.time()})

        def runner():
            self.publish("progress", {"operation_id": oid, "op": op_name,
                                      "task_id": task_id, "state": "started",
                                      "message": f"{op_name} started",
                                      "at": time.time()})
            try:
                result = fn(on_progress)
            except Exception as exc:  # noqa: BLE001 - reported, never raised out
                status, body = error_response(exc)
                with self._ops_lock:
                    record["state"] = "failed"
                    record["finished_at"] = time.time()
                    record["error"] = body["error"]
                    record["status"] = status
                self.publish("error", {"operation_id": oid, "op": op_name,
                                       "task_id": task_id, "status": status,
                                       "at": time.time(), **body})
                return
            with self._ops_lock:
                record["state"] = "completed"
                record["finished_at"] = time.time()
                record["result"] = result
            self.publish("progress", {"operation_id": oid, "op": op_name,
                                      "task_id": task_id, "state": "completed",
                                      "message": f"{op_name} completed",
                                      "result": result, "at": time.time()})

        self._spawn(f"wt-op-{op_name}-{oid}", runner)
        return record

    # -------------------------------------------------------------- health ---

    def tui_bridge_running(self) -> bool:
        """Is ``tracker.py`` holding :7373? Cached briefly; never binds it."""
        now = time.monotonic()
        stamp, value = self._tui_probe
        if now - stamp < TUI_PROBE_CACHE_SECONDS:
            return value
        try:
            with socket.create_connection(("127.0.0.1", TUI_BRIDGE_PORT), 0.25):
                value = True
        except OSError:
            value = False
        self._tui_probe = (now, value)
        return value

    def health(self) -> dict:
        probe = probe_data_file()
        return {
            "ok": True,
            "version": DAEMON_VERSION,
            "pid": os.getpid(),
            "port": self.port,
            "legacy_port": self.legacy_port,
            "started_at": self.started_at,
            "uptime_seconds": time.time() - self.started_at,
            "data_file": probe,
            # plan risk #1: the TUI saves wholesale from memory, so it can
            # clobber the daemon's writes. The client shows a banner on this.
            "tui_bridge": {"port": TUI_BRIDGE_PORT,
                           "running": self.tui_bridge_running()},
            # Same risk, other direction: while the TUI is up *it* owns idle
            # detection, so `active` is False even when the loop is running.
            "presence": {"enabled": self.presence,
                         "interval_seconds": self.presence_interval,
                         "active": self.presence and not self.tui_bridge_running()},
            "subscribers": self.broker.subscribers,
            "allow_empty": self.allow_empty,
            "python": sys.version.split()[0],
        }


def _unreadable_message(probe: dict) -> str:
    reason = probe.get("reason")
    base = {
        "missing": "the data file does not exist",
        "permission_denied": (
            "the data file cannot be read — on a second Mac this is the "
            "documented Full Disk Access (TCC) failure; grant it to the app "
            "and relaunch"),
        "empty_file": ("the data file is zero bytes — likely a dataless iCloud "
                       "placeholder; run "
                       "`brctl download ~/WorkloadTracker/.workload_tracker.json`"),
        "unparseable": "the data file is not valid JSON",
        "no_tasks": ("the data file parsed but contains no tasks; refusing to "
                     "write over what may be an unreadable copy "
                     "(pass --allow-empty for a genuinely fresh install)"),
    }.get(reason, f"the data file is unusable ({reason})")
    return f"Refusing to write: {base}."


# ============================================================ HTTP plumbing ===

def _json_bytes(obj) -> bytes:
    # ``default=str`` because reconcile results carry ``datetime.date`` objects
    # (sprint start/end) that would otherwise raise mid-response.
    return json.dumps(obj, default=str).encode("utf-8")


class _BaseHandler(BaseHTTPRequestHandler):
    """Shared response/parsing plumbing. No routing, no auth."""

    protocol_version = "HTTP/1.1"
    server_version = f"wt_daemon/{DAEMON_VERSION}"
    sys_version = ""

    @property
    def daemon(self) -> Daemon:
        return self.server.wt_daemon  # type: ignore[attr-defined]

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        log.debug("%s - %s", self.address_string(), format % args)

    def send_json(self, body, status: int = 200, headers: dict | None = None):
        payload = _json_bytes(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise DaemonError("bad_request", "request body too large",
                              bytes=length)
        raw = self.rfile.read(length)
        if not raw.strip():
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise DaemonError("bad_json", f"invalid JSON body: {exc}") from exc
        if not isinstance(body, dict):
            raise DaemonError("bad_json", "request body must be a JSON object")
        return body

    @property
    def query(self) -> dict:
        return {k: v[0] for k, v in
                parse_qs(urlparse(self.path).query).items()}


def _flag(value, default: bool = False) -> bool:
    """Coerce a JSON/query value to bool. ``"false"``/``"0"`` are False."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# ================================================================= v1 routes ===

ROUTES: list[tuple[str, re.Pattern, str]] = []


def route(method: str, pattern: str):
    def decorate(fn):
        ROUTES.append((method, re.compile(pattern), fn.__name__))
        return fn
    return decorate


class ApiHandler(_BaseHandler):
    """The authenticated ``/v1`` API (plan §5.1, §5.2)."""

    # -------------------------------------------------------------- dispatch --

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str):
        path = urlparse(self.path).path
        try:
            self._require_auth()
            handler_name, params = self._match(method, path)
            result = getattr(self, handler_name)(**params)
        except Exception as exc:  # noqa: BLE001 - every error becomes JSON
            status, body = error_response(exc)
            with contextlib.suppress(OSError):
                self.send_json(body, status,
                               {"WWW-Authenticate": "Bearer"} if status == 401
                               else None)
            return
        if result is None:
            return  # the handler wrote its own response (SSE)
        status, body, headers = result
        with contextlib.suppress(OSError):
            self.send_json(body, status, headers)

    def _match(self, method: str, path: str):
        allowed = set()
        for route_method, pattern, name in ROUTES:
            match = pattern.match(path)
            if not match:
                continue
            if route_method != method:
                allowed.add(route_method)
                continue
            return name, {k: unquote(v) for k, v in match.groupdict().items()}
        if allowed:
            raise DaemonError("method_not_allowed",
                              f"{method} not allowed on {path}",
                              allowed=sorted(allowed))
        raise DaemonError("not_found", f"no route for {method} {path}",
                          path=path)

    def _require_auth(self):
        header = self.headers.get("Authorization") or ""
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not presented:
            raise DaemonError("unauthorized",
                              "Authorization: Bearer <token> is required")
        if not secrets.compare_digest(presented.strip(), self.daemon.token):
            raise DaemonError("unauthorized", "invalid token")

    # ------------------------------------------------------------ read-only --

    @route("GET", rf"^{API_PREFIX}/health$")
    def h_health(self):
        return 200, self.daemon.health(), None

    @route("GET", rf"^{API_PREFIX}/snapshot$")
    def h_snapshot(self):
        snap, probe = self.daemon.read_with_probe(wt_api.snapshot)
        # Additive, and the reason it is here: a snapshot that came from an
        # unreadable file looks exactly like an empty tracker (risk #9). The
        # client renders the Full-Disk-Access state off this, not off len(tasks).
        snap["data_file"] = probe
        return 200, snap, None

    @route("GET", rf"^{API_PREFIX}/operations/(?P<oid>[^/]+)$")
    def h_operation(self, oid):
        record = self.daemon.operation(oid)
        if record is None:
            raise DaemonError("not_found", f"no operation {oid}",
                              operation_id=oid)
        return 200, record, None

    # ------------------------------------------------------------------ SSE --

    @route("GET", rf"^{API_PREFIX}/events$")
    def h_events(self):
        """Server-sent events: ``changed``, ``progress``, ``error``, ``heartbeat``.

        Framed with an ``id:`` so a client can detect a gap, and a ``retry:`` so
        it reconnects on its own. Deliberately *not* replayable
        (``Last-Event-ID`` is ignored): every event means "refetch", so a
        reconnecting client gets back in sync with one ``GET /v1/snapshot``,
        which is cheaper and less error-prone than a server-side backlog.
        """
        subscription = self.daemon.broker.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            # No Content-Length and no chunking: the body runs until the
            # connection closes, which `Connection: close` announces per RFC 7230.
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(b"retry: 2000\n\n")
            self._sse(0, "hello", {"version": DAEMON_VERSION,
                                   "heartbeat_seconds": self.daemon.heartbeat_seconds,
                                   "at": time.time()})
            while True:
                try:
                    seq, event, payload = subscription.get(
                        timeout=self.daemon.heartbeat_seconds)
                except queue.Empty:
                    self._sse(0, "heartbeat", {"now": time.time()})
                    continue
                self._sse(seq, event, payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.daemon.broker.unsubscribe(subscription)
        return None

    def _sse(self, seq: int, event: str, payload: dict):
        frame = (f"id: {seq}\n" if seq else "")
        frame += f"event: {event}\n"
        frame += f"data: {_json_bytes(payload).decode('utf-8')}\n\n"
        self.wfile.write(frame.encode("utf-8"))
        self.wfile.flush()

    # ------------------------------------------------------------- tasks -----

    @route("POST", rf"^{API_PREFIX}/tasks$")
    def h_create_task(self):
        body = self.read_json()
        title = body.get("title")
        if not title or not isinstance(title, str):
            raise DaemonError("bad_request", "'title' is required")
        kwargs = {k: body[k] for k in
                  ("role", "status", "description", "github_issue", "sprint",
                   "github_repo", "activity", "type") if body.get(k) is not None}
        result = self.daemon.write(
            lambda data: wt_api.create_task(data, title=title, **kwargs),
            reason="task_created")
        return 201, {"task": result["task"], "sprint": result.get("sprint"),
                     "role_label": result.get("role_label"),
                     "status_label": result.get("status_label")}, None

    @route("PATCH", rf"^{API_PREFIX}/tasks/(?P<tid>[^/]+)$")
    def h_patch_task(self, tid):
        fields = self.read_json()
        if not fields:
            raise DaemonError("bad_request", "no fields to update")
        result = self.daemon.write(
            lambda data: wt_api.update_task(data, tid, **fields),
            reason="task_updated", task_id=tid)
        return 200, {"task": result["task"], "changed": result["changed"]}, None

    @route("DELETE", rf"^{API_PREFIX}/tasks/(?P<tid>[^/]+)$")
    def h_delete_task(self, tid):
        delete_issue = _flag(self.query.get("delete_issue"), True)
        result = self.daemon.write(
            lambda data: wt_api.delete_task(data, tid, save_callback=wt.save,
                                            delete_issue=delete_issue),
            reason="task_deleted", task_id=tid)
        return 200, result, None

    @route("POST", rf"^{API_PREFIX}/tasks/(?P<tid>[^/]+)/status$")
    def h_set_status(self, tid):
        body = self.read_json()
        status = body.get("status")
        if not status:
            raise DaemonError("bad_request", "'status' is required")
        create_issue = _flag(body.get("create_issue"), False)

        if status == "done":
            # The Kanban Done drop. Long: reconcile + gh close. 202 + SSE.
            return self._close_operation(tid, create_issue, op_name="close")

        result = self.daemon.write(
            lambda data: wt_api.set_status(data, tid, status,
                                           create_issue=create_issue,
                                           save_callback=wt.save),
            reason="status_changed", task_id=tid)
        return 200, result, None

    # ------------------------------------------------------------- timers ----

    @route("POST", rf"^{API_PREFIX}/timer/start$")
    def h_timer_start(self):
        body = self.read_json()
        task_id = body.get("task_id")
        if not task_id:
            raise DaemonError("bad_request", "'task_id' is required")
        # Defaults to **False**: a v1 client starting a timer should not reach
        # out and rearrange the user's desktop. The per-task Safari window is
        # still a feature — the TUI and `wt start` open it, and the legacy
        # :7375 endpoints below hard-code True to stay byte-compatible with
        # tracker.py's bridge — but the app has to ask for it, by sending
        # `{"browser": true}`, rather than get it by omission.
        browser = _flag(body.get("browser"), False)
        result = self.daemon.write(
            lambda data: wt_api.start_timer(data, task_id, browser=browser),
            reason="timer_started", task_id=task_id)
        return 200, {"task_id": result["task"]["id"],
                     "title": result["task"]["title"],
                     "started_at": result["started_at"],
                     "stopped": result["stopped"]}, None

    @route("POST", rf"^{API_PREFIX}/timer/stop$")
    def h_timer_stop(self):
        body = self.read_json()
        # False for the same reason as start, and for a sharper one: with start
        # no longer opening a window, a stop that defaulted to True would
        # snapshot and close a Safari window the *user* opened by hand.
        browser = _flag(body.get("browser"), False)
        result = self.daemon.write(
            lambda data: wt_api.stop_timer(data, browser=browser),
            reason="timer_stopped")
        task = result.get("task")
        self.daemon.sync_hours_async(task["id"] if task else None)
        return 200, {"task_id": result.get("task_id"),
                     "title": result.get("title"),
                     "minutes": result.get("minutes"),
                     "logged": result.get("logged"),
                     "log": result.get("log")}, None

    # ---------------------------------------------------------- idle stop ----

    @route("GET", rf"^{API_PREFIX}/idle-stop$")
    def h_idle_stop(self):
        return 200, {"pending_idle_stop": self.daemon.pending_idle_stop()}, None

    @route("POST", rf"^{API_PREFIX}/idle-stop/(?P<action>ack|undo)$")
    def h_idle_stop_resolve(self, action):
        body = self.read_json()
        return 200, self.daemon.resolve_idle_stop(
            "acknowledge" if action == "ack" else "undo",
            body.get("id")), None

    # --------------------------------------------------------------- logs ----

    @route("POST", rf"^{API_PREFIX}/tasks/(?P<tid>[^/]+)/logs$")
    def h_add_log(self, tid):
        body = self.read_json()
        minutes = body.get("minutes")
        if minutes is None:
            raise DaemonError("bad_request", "'minutes' is required")
        try:
            minutes = float(minutes)
        except (TypeError, ValueError) as exc:
            raise DaemonError("bad_request", "'minutes' must be a number") from exc
        result = self.daemon.write(
            lambda data: wt_api.add_log(
                data, tid, minutes, body.get("note") or "Manual entry",
                started_at=body.get("started_at"), ended_at=body.get("ended_at"),
                calendar_event_uid=body.get("calendar_event_uid")),
            reason="log_added", task_id=tid)
        return 201, {"task_id": result["task"]["id"], "log": result["log"]}, None

    @route("PATCH", rf"^{API_PREFIX}/tasks/(?P<tid>[^/]+)/logs/(?P<lid>[^/]+)$")
    def h_edit_log(self, tid, lid):
        body = self.read_json()
        result = self.daemon.write(
            lambda data: wt_api.edit_log(data, tid, lid,
                                         minutes=body.get("minutes"),
                                         note=body.get("note")),
            reason="log_edited", task_id=tid)
        return 200, {"task_id": result["task"]["id"], "log": result["log"],
                     "old": result["old"]}, None

    @route("DELETE", rf"^{API_PREFIX}/tasks/(?P<tid>[^/]+)/logs/(?P<lid>[^/]+)$")
    def h_delete_log(self, tid, lid):
        result = self.daemon.write(
            lambda data: wt_api.delete_log(data, tid, lid),
            reason="log_deleted", task_id=tid)
        return 200, {"task_id": result["task"]["id"], "log": result["log"]}, None

    @route("POST", rf"^{API_PREFIX}/tasks/(?P<tid>[^/]+)/logs/merge$")
    def h_merge_logs(self, tid):
        body = self.read_json()
        first, second = body.get("log_id_1"), body.get("log_id_2")
        if not first or not second:
            raise DaemonError("bad_request",
                              "'log_id_1' and 'log_id_2' are required")
        result = self.daemon.write(
            lambda data: wt_api.merge_logs(data, tid, first, second),
            reason="logs_merged", task_id=tid)
        return 200, {"task_id": result["task"]["id"], "merged": result["merged"],
                     "sources": result["sources"],
                     "total_mins": result["total_mins"]}, None

    @route("POST", rf"^{API_PREFIX}/tasks/(?P<tid>[^/]+)/logs/(?P<lid>[^/]+)/split$")
    def h_split_log(self, tid, lid):
        body = self.read_json()
        at = body.get("split_at_minutes", body.get("minutes"))
        if at is None:
            raise DaemonError("bad_request", "'split_at_minutes' is required")
        try:
            at = float(at)
        except (TypeError, ValueError) as exc:
            raise DaemonError("bad_request",
                              "'split_at_minutes' must be a number") from exc
        result = self.daemon.write(
            lambda data: wt_api.split_log(data, tid, lid, at),
            reason="log_split", task_id=tid)
        return 200, {"task_id": result["task"]["id"], "first": result["first"],
                     "second": result["second"],
                     "total_mins": result["total_mins"]}, None

    # ------------------------------------------------------ close/reconcile --

    @route("POST", rf"^{API_PREFIX}/tasks/(?P<tid>[^/]+)/close/plan$")
    def h_close_plan(self, tid):
        """The §7.1 close-sheet preview. Write-free by construction."""
        body = self.read_json()
        offline = _flag(body.get("offline"), False)

        def plan(data):
            sprints = wt.get_cached_sprints(data) if offline else None
            return wt_api.plan_close(data, tid, sprints=sprints)

        return 200, self.daemon.read(plan), None

    @route("POST", rf"^{API_PREFIX}/tasks/(?P<tid>[^/]+)/close$")
    def h_close(self, tid):
        body = self.read_json()
        return self._close_operation(tid, _flag(body.get("create_issue"), False))

    def _close_operation(self, tid, create_issue, op_name="close"):
        def run(on_progress):
            def do(data):
                result = wt_api.close(data, tid, create_issue=create_issue,
                                      save_callback=wt.save,
                                      on_progress=on_progress)
                # A failed close is a *failed operation*, not a 200 with a sad
                # payload: reconcile aborts the close on purpose so hours cannot
                # be mis-reported, and the client must not show the card as Done.
                return wt_api.raise_on_failure(result)
            return self.daemon.write(do, reason="task_closed", task_id=tid)

        record = self.daemon.start_operation(op_name, tid, run)
        return 202, record, {"Location": f"{API_PREFIX}/operations/"
                                         f"{record['operation_id']}"}

    @route("POST", rf"^{API_PREFIX}/tasks/(?P<tid>[^/]+)/reconcile$")
    def h_reconcile(self, tid):
        body = self.read_json()
        create_issues = _flag(body.get("create_issues"), True)
        dry_run = _flag(body.get("dry_run"), False)

        if dry_run:
            return 200, self.daemon.read(
                lambda data: wt_api.reconcile(data, tid,
                                              create_issues=create_issues,
                                              dry_run=True)), None

        def run(on_progress):
            return self.daemon.write(
                lambda data: wt_api.reconcile(data, tid,
                                              create_issues=create_issues,
                                              save_callback=wt.save,
                                              on_progress=on_progress),
                reason="task_reconciled", task_id=tid)

        record = self.daemon.start_operation("reconcile", tid, run)
        return 202, record, {"Location": f"{API_PREFIX}/operations/"
                                         f"{record['operation_id']}"}

    # ------------------------------------------------------------- GitHub ----

    @route("POST", rf"^{API_PREFIX}/tasks/(?P<tid>[^/]+)/github/link$")
    def h_gh_link(self, tid):
        body = self.read_json()
        issue = body.get("issue")
        if not issue:
            raise DaemonError("bad_request", "'issue' is required "
                                             "(owner/repo#n, a URL, or a number)")
        verify = _flag(body.get("verify"), True)
        result = self.daemon.write(
            lambda data: wt_api.link_issue(data, tid, issue, verify=verify),
            reason="issue_linked", task_id=tid)
        return 200, {"task_id": result["task"]["id"], "issue": result["issue"],
                     "issue_info": result["issue_info"],
                     "repo_pinned": result["repo_pinned"]}, None

    @route("POST", rf"^{API_PREFIX}/tasks/(?P<tid>[^/]+)/github/unlink$")
    def h_gh_unlink(self, tid):
        result = self.daemon.write(lambda data: wt_api.unlink_issue(data, tid),
                                   reason="issue_unlinked", task_id=tid)
        return 200, {"task_id": result["task"]["id"],
                     "old_issue": result["old_issue"],
                     "remaining": result["remaining"]}, None

    @route("POST", rf"^{API_PREFIX}/tasks/(?P<tid>[^/]+)/github/open$")
    def h_gh_open(self, tid):
        """Open the task's current issue in the default browser.

        Builds the URL locally rather than shelling to ``gh issue view --web``,
        so it costs no API budget and works when ``gh`` is unauthenticated.
        The issue opens in **cmux**, not the default browser: see
        :func:`_open_issue_in_cmux`, which finds or creates the workspace whose
        name begins with the issue number.
        """
        # Drain the request body before doing anything else, or keep-alive
        # desyncs on the next request over the same connection.
        body = self.read_json()

        def resolve(data):
            task = wt_api.require_task(data, tid)
            ref = wt.task_current_issue(task, data)
            if not ref:
                raise wt_api.WtError(
                    "not_linked",
                    f"Task '{task.get('title')}' has no linked GitHub issue",
                    task_id=task.get("id"))
            return task["id"], ref, task.get("title") or ""

        task_id, ref, title = self.daemon.read(resolve)
        url = f"https://github.com/{ref.replace('#', '/issues/')}"
        result = {"opened": False, "workspace": None, "workspace_created": False}
        if _flag(body.get("open"), True):
            number = ref.rsplit("#", 1)[-1]
            result = _open_issue_in_cmux(url, number, title)
        return 200, {"task_id": task_id, "issue": ref, "url": url, **result}, None

    @route("POST", rf"^{API_PREFIX}/tasks/(?P<tid>[^/]+)/github/push$")
    def h_gh_push(self, tid):
        """Push Status/Activity/Type/Sprint/Hours to the current issue. 202."""
        def run(_on_progress):
            return self.daemon.write(
                lambda data: wt_api.push_to_github(data, tid),
                reason="task_pushed", task_id=tid)

        record = self.daemon.start_operation("github_push", tid, run)
        return 202, record, {"Location": f"{API_PREFIX}/operations/"
                                         f"{record['operation_id']}"}

    # ------------------------------------------- local desktop integrations --

    @route("POST", rf"^{API_PREFIX}/tasks/(?P<tid>[^/]+)/tabs/"
                  rf"(?P<action>save|open|clear|close)$")
    def h_tabs(self, tid, action):
        """Safari task-window actions, mirroring ``wt tabs`` (never Arc)."""
        def run(data):
            task = wt_api.require_task(data, tid)
            if action == "clear":
                task["tabs"] = []
                return {"task_id": task["id"], "tabs": [],
                        "active_window_id": task.get("active_window_id")}
            mgr = _safari_manager()
            if action == "save":
                mgr.snapshot_task_tabs(task)
            elif action == "open":
                mgr.open_task_window(task)
            elif action == "close":
                mgr.snapshot_task_tabs(task)
                window_id = task.get("active_window_id")
                if window_id is not None:
                    mgr.close_window(window_id)
                task["active_window_id"] = None
            return {"task_id": task["id"], "tabs": list(task.get("tabs") or []),
                    "active_window_id": task.get("active_window_id")}

        return 200, self.daemon.write(run, reason=f"tabs_{action}",
                                      task_id=tid), None

    @route("POST", rf"^{API_PREFIX}/tasks/(?P<tid>[^/]+)/iterm$")
    def h_iterm(self, tid):
        """Open (or close) the task's iTerm2/tmux session."""
        body = self.read_json()
        action = (body.get("action") or "open").lower()
        if action not in ("open", "close"):
            raise DaemonError("bad_request", "'action' must be open or close",
                              action=action)

        def run(data):
            task = wt_api.require_task(data, tid)
            try:
                from iterm_manager import TaskTerminalManager
            except ImportError as exc:
                raise DaemonError("unavailable",
                                  f"iterm_manager unavailable: {exc}") from exc
            manager = TaskTerminalManager(data)
            if action == "close":
                result = manager.close_session(task)
            else:
                result = manager.open_terminal(task, wt.save)
            return {"task_id": task["id"], "action": action, "result": result}

        return 200, self.daemon.write(run, reason=f"iterm_{action}",
                                      task_id=tid), None

    # ------------------------------------------------------------- helpers ---


def _safari_manager():
    """``SafariWindowManager``, as a coded error when the module is missing.

    Imported inside the function (not at module scope) for the same reason
    ``wt._browser_switch`` does it: the harnesses swap ``sys.modules`` entries,
    and a top-level import would bind the real one before they get the chance.
    """
    try:
        from browser_window import SafariWindowManager
    except ImportError as exc:
        raise DaemonError("unavailable",
                          f"browser_window unavailable: {exc}") from exc
    return SafariWindowManager()


# ============================================================ legacy contract ==
#
# plan §5.4. These four endpoints exist to keep `workload-macos-monitor` working
# with `tracker.py` closed. The monitor decodes:
#
#   GET  /status       -> {"active_timer": {task_id,title,role,started_at,
#                                           active_window_id} | null, ...}
#   GET  /tasks        -> {"tasks": [{id,title,role,status,last_logged_at}]}
#   POST /timer/start  -> {"action","task"}          body {"task_id": ...}
#   POST /timer/stop   -> {"action","task","logged_minutes"}
#
# Three endpoints are *additive* to that contract, for the idle-stop panel. They
# live here rather than only on the authenticated :7374 because this is the port
# the monitor is actually pointed at (`defaults read WorkloadMonitor` ->
# trackerBaseURL = http://127.0.0.1:7375), and the whole premise of §5.4 is that
# the monitor sends no Authorization header:
#
#   GET  /idle-stop      -> {"pending_idle_stop": {...} | null}
#   POST /idle-stop/ack  -> {"action":"acknowledged","cleared",...}  body {"id"?}
#   POST /idle-stop/undo -> {"action":"undone","restored","minutes",...}
#
# They are mirrored at /v1/idle-stop{,/ack,/undo} for a token-bearing client.
#
# `role` and `started_at` are **non-optional** in the monitor's Codable structs,
# so emitting null for either is a decode failure, not a graceful degradation.
# `active_window_id` and `last_logged_at` are optional there but load-bearing
# (the Safari border overlay and the "recently logged" column).
#
# `tools/test_legacy_contract.py` derives the expected key sets and types from
# `tracker.py`'s own `_bridge_status()` / `_bridge_list_tasks()` at runtime
# rather than hardcoding them, so this cannot drift silently.


class LegacyHandler(_BaseHandler):
    """``tracker.py``'s ``_BridgeHandler`` contract, without the TUI.

    Unauthenticated by design (§5.4): the monitor sends no ``Authorization``
    header and the whole point is that it needs no change. Loopback binding is
    the security boundary, exactly as it is for the TUI's own bridge.

    The one behaviour deliberately *not* carried over is Arc: the TUI's
    ``_commit_active_timer`` runs ``_arc_tab_cleanup``, and Arc is deprecated
    and disabled. Safari task windows are carried over in full.
    """

    def do_GET(self):
        path = urlparse(self.path).path.strip("/")
        parts = [p for p in path.split("/") if p] or [""]
        action = parts[0]
        try:
            if action == "status":
                body = self.legacy_status()
            elif action == "tasks":
                body = self.legacy_tasks()
            elif action == "idle-stop" and not parts[1:]:
                # Additive to the contract, and deliberately *not* folded into
                # /status: `tools/test_legacy_contract.py` asserts /status has
                # exactly tracker._bridge_status()'s top-level keys, and that
                # oracle is worth more than saving the monitor one poll.
                body = {"pending_idle_stop": self.daemon.pending_idle_stop()}
            elif action == "timer" and parts[1:2] == ["toggle"]:
                body = self.legacy_toggle()
            elif action == "log" and len(parts) > 1:
                try:
                    minutes = float(parts[1])
                except ValueError:
                    self.send_json({"error": "Invalid minutes"}, 400)
                    return
                body = self.legacy_log(minutes)
            elif action == "filter":
                # Echo only — same as the bridge, which never drove the TUI's
                # filter either (CLAUDE.md, "Known Limitations").
                body = {"action": "filter",
                        "role": parts[1] if len(parts) > 1 else "all",
                        "note": "Use keyboard 1-4 in TUI to filter"}
            elif action == "push":
                body = {"error": "push is not served on the legacy port; use "
                                 "POST /v1/tasks/<id>/github/push",
                        "_status": 501}
            else:
                self.send_json({"error": f"Unknown action: {action}"}, 404)
                return
        except Exception as exc:  # noqa: BLE001 - keep the bridge alive
            self.send_legacy_error(exc)
            return
        self.send_json(body, body.pop("_status", 200))

    def do_POST(self):
        path = urlparse(self.path).path.strip("/")
        parts = [p for p in path.split("/") if p]
        try:
            if parts[:2] == ["timer", "start"]:
                task_id = self.read_json().get("task_id")
                if not task_id:
                    self.send_json({"error": "Missing 'task_id' in body"}, 400)
                    return
                body = self.legacy_start(task_id)
            elif parts[:2] == ["timer", "stop"]:
                body = self.legacy_stop()
            elif parts[:2] in (["idle-stop", "ack"], ["idle-stop", "undo"]):
                body = self.daemon.resolve_idle_stop(
                    "acknowledge" if parts[1] == "ack" else "undo",
                    self.read_json().get("id"))
            else:
                self.send_json({"error": f"Unknown action: {path}"}, 404)
                return
        except Exception as exc:  # noqa: BLE001 - keep the bridge alive
            self.send_legacy_error(exc)
            return
        self.send_json(body, body.pop("_status", 200))

    def send_legacy_error(self, exc: Exception):
        """The bridge's flat ``{"error": "..."}`` shape, with a real status.

        The monitor only branches on the status code (any non-2xx becomes
        ``TrackerError.http``), so the body stays flat rather than adopting the
        v1 ``{"error": {"code", ...}}`` envelope — but the *code* is carried
        alongside for anything else reading this port.
        """
        status, body = error_response(exc)
        detail = body["error"]
        self.send_json({"error": detail["message"], "code": detail["code"]},
                       status)

    # ------------------------------------------------------------- payloads --

    def legacy_status(self) -> dict:
        return self.daemon.read(legacy_status_payload)

    def legacy_tasks(self) -> dict:
        return self.daemon.read(legacy_tasks_payload)

    def legacy_start(self, task_id: str) -> dict:
        return _legacy_start(self.daemon, task_id)

    def legacy_stop(self) -> dict:
        return _legacy_stop(self.daemon)

    def legacy_toggle(self) -> dict:
        """``GET /timer/toggle`` — the Stream Deck's one-button start/stop."""
        target = self.daemon.read(lambda data: (
            bool(data.get("active_timer")),
            next((t["id"] for t in data.get("tasks", [])
                  if t.get("status") == "inprogress"), None)))
        running, first_inprogress = target
        if running:
            return _legacy_stop(self.daemon)
        if not first_inprogress:
            return {"error": "No in-progress tasks found", "_status": 404}
        return _legacy_start(self.daemon, first_inprogress)

    def legacy_log(self, minutes: float) -> dict:
        """``GET /log/<minutes>`` — quick-log to the active or first in-progress task."""
        # Resolve the target before opening a write transaction, so a "nothing
        # to log to" answer does not rewrite the file for no reason.
        target_id = self.daemon.read(lambda data: (
            (data.get("active_timer") or {}).get("task_id")
            or next((t["id"] for t in data.get("tasks", [])
                     if t.get("status") == "inprogress"), None)))
        if not target_id:
            return {"error": "No active task to log to", "_status": 404}

        def run(data):
            result = wt_api.add_log(data, target_id, minutes,
                                    f"Stream Deck ({int(minutes)}m)")
            return {"action": "logged", "minutes": minutes,
                    "task": result["task"]["title"]}

        return self.daemon.write(run, reason="legacy_log", task_id=target_id)


def legacy_status_payload(data: dict) -> dict:
    """Byte-compatible with ``tracker.WorkloadTracker._bridge_status``."""
    tasks = data.get("tasks", [])
    at = data.get("active_timer")

    by_role: dict[str, float] = {}
    for task in tasks:
        rid = task.get("role_id", "other")
        logged = wt.task_logged_mins(task)
        live = ((time.time() - at["started_at"]) / 60
                if at and at.get("task_id") == task["id"] else 0)
        by_role[rid] = by_role.get(rid, 0) + logged + live

    active_timer = None
    if at:
        task = next((t for t in tasks if t["id"] == at.get("task_id")), None)
        if task:
            active_timer = {
                "task_id": task["id"],
                "title": task["title"],
                "role": task.get("role_id"),
                "started_at": at["started_at"],
                "elapsed": wt.fmt_mins((time.time() - at["started_at"]) / 60),
                # The monitor draws its focus-aware Safari border from this.
                "active_window_id": task.get("active_window_id"),
            }

    return {
        "active_timer": active_timer,
        "tasks": len(tasks),
        "time_by_role": {k: wt.fmt_mins(v) for k, v in by_role.items()},
    }


def legacy_tasks_payload(data: dict) -> dict:
    """Byte-compatible with ``tracker.WorkloadTracker._bridge_list_tasks``."""
    return {"tasks": [
        {
            "id": t["id"],
            "title": t["title"],
            "role": t.get("role_id"),
            "status": t.get("status"),
            # Phase 1 moved this out of tracker.py so both callers share it.
            "last_logged_at": wt_api.task_last_logged_at(t),
        }
        for t in data.get("tasks", []) if t.get("status") != "done"
    ]}


def _legacy_start(daemon: Daemon, task_id: str) -> dict:
    """``_bridge_start_timer``: commit the previous timer, then start this one.

    Matches the bridge exactly on the three things that are easy to get wrong:
    an unknown id is a 404 that writes **nothing**; an already-running task is a
    **no-op success** (not a restart, which would discard the elapsed session);
    and the start opens the task's Safari window but does **not** focus the Arc
    space.
    """
    state = daemon.read(lambda data: (
        next((t["title"] for t in data.get("tasks", [])
              if t["id"] == task_id), None),
        (data.get("active_timer") or {}).get("task_id"),
    ))
    title, running_id = state
    if title is None:
        return {"error": f"No task with id '{task_id}'", "_status": 404}
    if running_id == task_id:
        return {"action": "started", "task": title}

    def run(data):
        # wt_api.start_timer commits the previous session and runs
        # wt._browser_switch (snapshot+close the old window, open the new one) —
        # the same pair the bridge gets from _browser_on_task_stopped +
        # _browser_on_task_started. No Arc.
        result = wt_api.start_timer(data, task_id, browser=True)
        return {"action": "started", "task": result["task"]["title"]}

    try:
        out = daemon.write(run, reason="legacy_timer_started", task_id=task_id)
    except wt_api.WtError as exc:
        # The task was deleted between the read and the write. Rare, but the
        # bridge's answer for "no such task" is a 404, not a 500.
        if exc.code == "task_not_found":
            return {"error": f"No task with id '{task_id}'", "_status": 404}
        raise
    if running_id:
        daemon.sync_hours_async(running_id)
    return out


def _legacy_stop(daemon: Daemon) -> dict:
    """``_bridge_stop_timer``: commit, then sync GitHub hours out of band."""
    if not daemon.read(lambda data: bool(data.get("active_timer"))):
        return {"error": "No timer running", "_status": 404}

    def run(data):
        result = wt_api.stop_timer(data, browser=True)
        log_entry = result.get("log")
        return {
            "action": "stopped",
            "task": (result["task"]["title"] if result.get("task") else "?"),
            # The bridge reports round(elapsed, 2) when it logged and 0.0 when
            # the session was too short to record; the log entry already carries
            # exactly that rounded value.
            "logged_minutes": (log_entry or {}).get("minutes", 0.0),
            "_stopped_task_id": (result["task"]["id"]
                                 if result.get("task") else None),
        }

    try:
        out = daemon.write(run, reason="legacy_timer_stopped")
    except wt_api.WtError as exc:
        # Another writer stopped it between the read and the write.
        if exc.code == "no_active_timer":
            return {"error": "No timer running", "_status": 404}
        raise
    stopped_id = out.pop("_stopped_task_id", None)
    if stopped_id:
        daemon.sync_hours_async(stopped_id)
    return out


# =================================================================== servers ===

class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, daemon: Daemon):
        # Not ``self.daemon``: socketserver already gives servers a
        # ``daemon_threads`` attribute and ``.daemon`` is thread vocabulary.
        self.wt_daemon = daemon
        super().__init__(addr, handler)


def make_servers(daemon: Daemon, host: str = "127.0.0.1"):
    """Bind the v1 server and (if configured) the legacy one. Never 7373."""
    if daemon.port == TUI_BRIDGE_PORT or daemon.legacy_port == TUI_BRIDGE_PORT:
        raise SystemExit(f"refusing to bind {TUI_BRIDGE_PORT}: that port belongs "
                         f"to tracker.py's own bridge")
    api = _Server((host, daemon.port), ApiHandler, daemon)
    daemon.port = api.server_address[1]  # resolve an ephemeral :0
    legacy = None
    if daemon.legacy_port is not None:
        try:
            legacy = _Server((host, daemon.legacy_port), LegacyHandler, daemon)
            daemon.legacy_port = legacy.server_address[1]
        except OSError:
            api.server_close()
            raise
    return api, legacy


# ================================================================== lifecycle ==

def load_or_create_token(path: Path = DEFAULT_TOKEN_FILE) -> str:
    """Read the bearer token, minting a ``0600`` one on first run (§5.1).

    Loopback is not authentication — any local process can reach :7374 and this
    API can close GitHub issues — so the token is mandatory. The mode is
    re-asserted on every read, because a token that became world-readable is a
    token that no longer does anything.
    """
    path = Path(path).expanduser()
    if path.exists():
        token = path.read_text().strip()
        if token:
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
            return token
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with 0600 from the outset rather than write-then-chmod, so the
    # secret is never briefly world-readable.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token + "\n")
    return token


def health_answering(port: int, host: str = "127.0.0.1",
                     timeout: float = 1.0) -> bool:
    """Is a daemon already on *port*? (§5.5 — attach, don't double-bind.)

    Any HTTP response counts, **including 401**: a wrong token still proves
    something is listening, and binding a second daemon on top would be the
    actual failure.
    """
    import http.client
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request("GET", f"{API_PREFIX}/health")
        response = conn.getresponse()
        response.read()
        conn.close()
        return True
    except OSError:
        return False
    except Exception:  # noqa: BLE001 - a malformed reply is still a listener
        return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wt_daemon.py",
        description="Local HTTP + SSE API for the workload tracker "
                    "(docs/plan-macos-app.md §5).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"v1 API port (default {DEFAULT_PORT}); "
                             f"0 picks an ephemeral one")
    parser.add_argument("--legacy-port", type=int, nargs="?",
                        const=DEFAULT_LEGACY_PORT, default=None,
                        help=f"also serve tracker.py's unauthenticated :7373 "
                             f"contract on this port (bare flag = "
                             f"{DEFAULT_LEGACY_PORT}; off by default)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default 127.0.0.1; loopback is the "
                             "security boundary — do not change it)")
    parser.add_argument("--token-file", default=str(DEFAULT_TOKEN_FILE),
                        help=f"bearer token file (default {DEFAULT_TOKEN_FILE})")
    parser.add_argument("--data-file", default=None,
                        help="override the data file (same effect as "
                             "WT_DATA_FILE); use a copy, never the live file")
    parser.add_argument("--allow-empty", action="store_true",
                        help="permit writes when the data file has no tasks. "
                             "Only for a genuinely fresh install — it disables "
                             "the risk-#9 guard that stops the daemon writing "
                             "over an unreadable (Full Disk Access) file")
    parser.add_argument("--no-github-sync-on-stop", action="store_true",
                        help="do not push hours to GitHub after a timer stop")
    parser.add_argument("--no-presence", action="store_true",
                        help="do not run the idle/presence loop. The loop is "
                             "already inert while tracker.py holds :7373 (the "
                             "TUI is then the sole detector) and while "
                             "config.presence_detection_enabled is false; this "
                             "turns the thread off outright")
    parser.add_argument("--presence-interval", type=float,
                        default=PRESENCE_INTERVAL_SECONDS,
                        help=f"idle poll interval in seconds "
                             f"(default {PRESENCE_INTERVAL_SECONDS:g}); each "
                             f"poll forks ioreg, so this is not 1 Hz")
    parser.add_argument("--heartbeat-seconds", type=float,
                        default=HEARTBEAT_SECONDS)
    parser.add_argument("--watch-interval", type=float,
                        default=WATCH_INTERVAL_SECONDS,
                        help="data-file mtime poll interval (default 1s)")
    parser.add_argument("--log-level", default="info",
                        choices=["debug", "info", "warning", "error"])
    parser.add_argument("--print-token", action="store_true",
                        help="print the bearer token and exit")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper()),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.data_file:
        target = Path(args.data_file).expanduser()
        os.environ["WT_DATA_FILE"] = str(target)
        wt.DATA_FILE = target  # resolved at import time, so rebind it too

    token = load_or_create_token(Path(args.token_file))
    if args.print_token:
        print(token)
        return 0

    # §5.5: attach, don't double-bind. Exit 0 — from the app's point of view a
    # daemon is running, which is the desired end state.
    if args.port and health_answering(args.port, args.host):
        print(f"wt_daemon already answering on {args.host}:{args.port} — "
              f"not starting a second one", file=sys.stderr)
        return 0

    daemon = Daemon(token, allow_empty=args.allow_empty, port=args.port,
                    legacy_port=args.legacy_port,
                    heartbeat_seconds=args.heartbeat_seconds,
                    watch_interval=args.watch_interval,
                    github_sync_on_stop=not args.no_github_sync_on_stop,
                    presence=not args.no_presence,
                    presence_interval=args.presence_interval)
    try:
        api, legacy = make_servers(daemon, args.host)
    except OSError as exc:
        print(f"could not bind: {exc}", file=sys.stderr)
        return 1

    daemon.start()
    threads = [threading.Thread(target=api.serve_forever, name="wt-api",
                                daemon=True)]
    if legacy is not None:
        threads.append(threading.Thread(target=legacy.serve_forever,
                                        name="wt-legacy", daemon=True))
    for thread in threads:
        thread.start()

    probe = probe_data_file()
    log.info("wt_daemon %s on http://%s:%s (data=%s, %s)", DAEMON_VERSION,
             args.host, daemon.port, probe["path"], probe["reason"])
    if legacy is not None:
        log.info("legacy :7373 contract on http://%s:%s (unauthenticated)",
                 args.host, daemon.legacy_port)
    if not probe["readable"]:
        log.warning("%s", _unreadable_message(probe))

    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError):
            signal.signal(sig, lambda *_: stopping.set())
    try:
        while not stopping.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass

    log.info("shutting down")
    daemon.stop()
    api.shutdown()
    api.server_close()
    if legacy is not None:
        legacy.shutdown()
        legacy.server_close()
    daemon.join_background(timeout=5.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
