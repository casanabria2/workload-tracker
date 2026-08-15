#!/usr/bin/env python3
"""
wt — Workload Tracker CLI
Quick command-line interface to manage tasks without launching the full TUI.

Usage:
    wt add "Task title" --role strategic --status inprogress [--sprint NN]
                        [--repo owner/repo] [--activity ACT] [--type TYPE] [--create-issue]
    wt list [--role strategic] [--all]
    wt start <task-id or partial title>
    wt stop
    wt log <task-id or partial title> <minutes> [note]
    wt notes <task-id or partial title>
    wt status
    wt report [<start> <end>] [--sprint NAME] [--last Nd] [--role ROLE] [--json]
                                   — Show logged time in a date range
    wt done <task-id or partial title>
    wt close-recurrent [--all-previous] [--dry-run]
                                   — Close recurrent tasks (with a GitHub issue)
                                     from the previous sprint; --all-previous
                                     closes every earlier sprint
    wt new-recurrent [--all-previous] [--dry-run]
                                   — Recreate the previous sprint's recurring
                                     tasks (open and closed) in the current
                                     sprint, each with its own GitHub issue;
                                     --all-previous sources every earlier sprint
    wt delete <task-id or partial title>
    wt rename <task> <new title>       — Rename a task

    wt logs <task>                              — List all time logs for a task
    wt edit-log <task> <log-id> [--minutes M] [--note N]  — Edit log entry
    wt delete-log <task> <log-id>               — Delete log entry
    wt split-log <task> <log-id> <minutes>      — Split log at minute mark
    wt merge-logs <task> <log-id-1> <log-id-2>  — Merge two log entries

    wt link <task> <github-issue>  — Link task to GitHub issue
    wt unlink <task>               — Unlink task from GitHub issue
    wt push <task>                 — Sync task to linked GitHub issue

    wt add-issue <owner/repo#N|url|N> [--role ROLE] [--folder PATH]
                                   — Create a To Do task from an existing
                                     GitHub issue (links to it)
    wt add-issue [--role ROLE]     — Interactive: pick from your assigned issues

    wt config                    — Show all config
    wt config <key>              — Show config value
    wt config <key> <value>      — Set config value

    wt presence                  — Show presence detection status
    wt presence on               — Enable with default 15-minute timeout
    wt presence off              — Disable presence detection
    wt presence <minutes>        — Set timeout and enable

    wt roles                          — List all roles
    wt roles add <id> <label>         — Add a new role
    wt roles update <id> <label>      — Update role label
    wt roles delete <id>              — Delete a role

    wt sprint                         — Current sprint + active tasks by sprint
    wt set-sprint <task> <sprint|none> — Correct the sprint a task STARTED in
    wt sync-sprints <task>            — Reconcile a task's per-sprint GitHub
                                        issues with its logs (idempotent)
    wt sync-sprints --all [--create-issues] [--dry-run]
                                      — Reconcile every task, recurrent included.
                                        Never mints new issues unless
                                        --create-issues is given; always prints
                                        an itemised plan and asks first.

    wt set-repo <task> [repo]         — Set/clear GitHub repo for a task
    wt set-activity <task> [act]      — Set/clear GitHub Project activity for a task
    wt set-type <task> [type]         — Set/clear GitHub Project type for a task

    wt calendar                  — List events from yesterday & today
    wt calendar <days>           — List events from last N days
    wt calendar import <event> [--task <task>]  — Import event (or log to existing task)
    wt calendar setup            — Show Google Calendar setup instructions
    wt calendar mappings         — List all event-to-task mappings
    wt calendar map <event> <task>   — Map event title to task for quick logging
    wt calendar unmap <event>    — Remove an event-to-task mapping

    wt arc setup                 — Set up Arc browser integration
    wt arc status                — Show Arc integration status
    wt arc sync                  — Sync folders with current roles/tasks

    wt iterm setup               — Enable iTerm2/tmux integration
    wt iterm open <task>         — Open iTerm2 terminal for a task
    wt iterm close <task>        — Close tmux session for a task
    wt iterm set-folder <task> <path> — Set local folder for task
    wt iterm clear-folder <task> — Clear local folder setting
    wt iterm status              — Show iTerm integration status

Notes are stored in ~/.workload_tracker_notes/<task_id>.md
Tasks linked to GitHub issues use the issue for notes instead.
"""

import contextlib
import fcntl
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from datetime import datetime, timedelta

def _resolve_data_file() -> Path:
    """Where the tracker's JSON lives.

    Defaults to ``~/.workload_tracker.json``. ``WT_DATA_FILE`` overrides it so
    migrations and refactors can be exercised against a throwaway copy instead
    of the live, iCloud-synced source of truth. Production runs never set it.
    """
    override = os.environ.get("WT_DATA_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".workload_tracker.json"


DATA_FILE = _resolve_data_file()
NOTES_DIR = Path.home() / ".workload_tracker_notes"

# ── Phase 0 of docs/plan-macos-app.md §3: atomic + mutually exclusive writes ──
#
# Three processes write this file today (CLI, TUI, MCP server) and a fourth is
# coming (wt_daemon.py). Two problems, two mechanisms:
#
#   1. *Torn file* — a half-written JSON document. Fixed by ``_atomic_write_json``
#      (temp file in the same directory, fsync, ``os.replace``), which is what
#      makes every write all-or-nothing. A reader therefore always sees a
#      complete document: either the old one or the new one, never a splice.
#   2. *Lost update* — two writers each load, mutate, and save, and the later
#      save discards the earlier writer's mutation. Fixed by ``data_lock()``,
#      an advisory ``flock`` held across a whole read-modify-write.
#
# The lock lives in a **sidecar** file, never the data file itself: the data
# file is replaced wholesale by ``os.replace``, so a lock held on its inode
# would be silently abandoned on every save. And it is kept out of
# ``~/Library/Mobile Documents`` — advisory locking on an iCloud-synced path is
# unreliable, and iCloud would happily sync a lock file between Macs, which is
# meaningless (the lock is per-machine by design).

# How long to wait for the sidecar lock before giving up. Bounded on purpose:
# the TUI's ``_tick()`` runs once a second and can reach save(), so an unbounded
# ``LOCK_EX`` would let a stuck holder (say a ``wt done`` mid-way through a slow
# ``gh`` round trip) freeze the UI indefinitely. Long enough to outlast any
# normal save (a 209 KB write is sub-millisecond), short enough to never look
# like a hang.
DATA_LOCK_TIMEOUT_SECONDS = 5.0
_DATA_LOCK_POLL_SECONDS = 0.02

# Re-entrancy: ``data_lock()`` is re-entrant *within a process*.
#
# THIS IS THE CONTRACT PHASE 2 DEPENDS ON: a caller may hold ``data_lock()``
# across a whole transaction and call ``save()`` inside it. ``save()`` takes the
# same lock, the nested acquisition is counted rather than re-flocked, and the
# ``flock`` is released only when the outermost block exits. There is no
# separate ``_save_locked()`` to remember — ``save()`` is always the right call.
#
#   with wt.data_lock():          # daemon transaction
#       data = wt.load()
#       ...mutate...
#       wt.save(data)             # re-enters, does not deadlock
#
# ``_DATA_LOCK_MUTEX`` is an RLock so the same thread re-enters freely while a
# *different* thread still blocks (flock is per-open-file, so without this two
# threads in one process would both hold their own fd and neither would be
# excluded... and worse, the first to finish would unlock for both).
_DATA_LOCK_MUTEX = threading.RLock()
_DATA_LOCK_STATE = {"depth": 0, "fh": None}


class DataLockTimeout(RuntimeError):
    """Raised when ``data_lock()`` cannot acquire the sidecar lock in time."""


def _resolve_lock_file(path=None) -> Path:
    """Sidecar advisory-lock path for *path* (default: the current DATA_FILE).

    For the real data file this is ``~/.workload_tracker.lock``. For a
    ``WT_DATA_FILE`` copy it is a deterministic sibling (``<copy>.lock``), so a
    test never contends with the live lock and two concurrent test runs against
    different copies never contend with each other.

    Note the deliberate lack of ``.resolve()``: ``~/.workload_tracker.json`` is
    a symlink chain into iCloud Drive, and the lock must stay in ``$HOME``.
    """
    data = Path(path) if path is not None else Path(DATA_FILE)
    if data == Path.home() / ".workload_tracker.json":
        return Path.home() / ".workload_tracker.lock"
    return data.with_name(data.name + ".lock")


@contextlib.contextmanager
def data_lock(path=None, timeout: float = None, required: bool = True):
    """Hold the exclusive advisory lock guarding the data file.

    Wrap a whole read-modify-write in it so no other process can interleave::

        with data_lock():
            data = load()
            data["tasks"].append(...)
            save(data)

    Re-entrant within a process (see the module notes above), so the nested
    ``save()`` is free.

    *timeout* defaults to ``DATA_LOCK_TIMEOUT_SECONDS``. On expiry:

    - ``required=True`` (the default, and what a daemon transaction wants):
      raise ``DataLockTimeout``. Better to fail loudly than to silently run a
      transaction unprotected.
    - ``required=False`` (what ``save()`` uses): log a warning and proceed
      *without* the lock. This degrades to the pre-Phase-0 behaviour — a lost
      update is possible — but never to a torn file, because the write itself is
      atomic regardless. Chosen so a stuck lock holder can never wedge the TUI's
      1-second tick loop or drop a user's time entry on the floor.
    """
    if timeout is None:
        timeout = DATA_LOCK_TIMEOUT_SECONDS
    deadline = time.monotonic() + max(timeout, 0.0)

    if not _DATA_LOCK_MUTEX.acquire(timeout=max(timeout, 0.001)):
        if required:
            raise DataLockTimeout(
                f"another thread has held the data lock for >{timeout}s"
            )
        logging.warning("data_lock: thread contention timeout — proceeding unlocked")
        yield False
        return
    try:
        nested = _DATA_LOCK_STATE["depth"] > 0
        fh = None
        if not nested:
            lock_path = _resolve_lock_file(path)
            try:
                fh = open(lock_path, "a+")
            except OSError as exc:
                if required:
                    raise DataLockTimeout(f"cannot open lock file {lock_path}: {exc}")
                logging.warning("data_lock: cannot open %s (%s) — proceeding unlocked",
                                lock_path, exc)
                yield False
                return
            # Poll LOCK_NB rather than block on LOCK_EX: flock has no timeout of
            # its own, and SIGALRM is not usable from a non-main thread (the TUI
            # bridge and Textual workers are threads).
            got = False
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    got = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(_DATA_LOCK_POLL_SECONDS)
            if not got:
                fh.close()
                if required:
                    raise DataLockTimeout(
                        f"data lock {lock_path} still held after {timeout}s"
                    )
                logging.warning("data_lock: %s busy after %ss — proceeding unlocked",
                                lock_path, timeout)
                yield False
                return
            _DATA_LOCK_STATE["fh"] = fh
        _DATA_LOCK_STATE["depth"] += 1
        try:
            yield True
        finally:
            _DATA_LOCK_STATE["depth"] -= 1
            if _DATA_LOCK_STATE["depth"] == 0 and _DATA_LOCK_STATE["fh"] is not None:
                held = _DATA_LOCK_STATE["fh"]
                _DATA_LOCK_STATE["fh"] = None
                try:
                    fcntl.flock(held.fileno(), fcntl.LOCK_UN)
                finally:
                    held.close()
    finally:
        _DATA_LOCK_MUTEX.release()


def _atomic_write_json(target: Path, data: dict):
    """Serialize *data* to *target* so a reader can never see a partial file.

    Temp file in the **same directory** as the resolved target (so ``os.replace``
    is a same-filesystem rename and therefore atomic), ``flush`` + ``fsync``
    before the rename so a crash can't leave a rename pointing at unwritten
    bytes, then ``os.replace``.

    ``target`` is resolved through symlinks first, and that matters: the live
    ``~/.workload_tracker.json`` is a symlink chain into iCloud Drive, and
    ``os.replace`` does **not** follow symlinks — replacing the link path
    directly would swap the symlink for a regular file and quietly detach the
    data from iCloud sync on both Macs.

    The payload is ``json.dumps(data, indent=2)`` verbatim, byte for byte as
    before: the file is diffed by hand and synced by iCloud, so reformatting it
    would produce a spurious whole-file change.
    """
    target = Path(target)
    try:
        real = target.resolve()
    except OSError:
        real = target
    payload = json.dumps(data, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(real.parent), prefix=real.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        # mkstemp is 0600; keep whatever mode the file already had instead.
        try:
            os.chmod(tmp, os.stat(real).st_mode & 0o7777)
        except OSError:
            pass
        os.replace(tmp, real)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


DEFAULT_ROLES = [
    {"id": "demokit",   "label": "Managing DemoKit",  "color": "blue"},
    {"id": "demos",     "label": "Demos & Workshops", "color": "green"},
    {"id": "strategic", "label": "Strategic Deals",   "color": "yellow"},
    {"id": "other",     "label": "Other",             "color": "white"},
]

STATUS_LABELS = {"todo": "To Do", "inprogress": "In Progress", "recurrent": "Recurrent", "done": "Done"}
COLORS = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "blue": "\033[34m", "green": "\033[32m", "yellow": "\033[33m",
    "red": "\033[31m", "cyan": "\033[36m",
}

def c(text, *codes):
    return "".join(COLORS.get(code, "") for code in codes) + str(text) + COLORS["reset"]


def uid() -> str:
    import random, string
    return datetime.now().strftime("%Y%m%d%H%M%S") + "".join(random.choices(string.ascii_lowercase, k=4))


class DataFileUnreadable(Exception):
    """The data file exists but could not be read or parsed.

    Raised instead of quietly returning an empty dataset. ``load()`` is a
    read-modify-*write* (the migrations below call ``save()``), so "pretend it
    was empty" meant **persisting** that emptiness over the real file. Since
    Phase 0 the write is an ``os.replace()``, which needs permission on the
    *directory* rather than the file, so even a mode-000 file was replaced —
    210 KB of history became a 520-byte stub wearing the original mode.

    The realistic trigger is the documented second-Mac case: the file is there
    but this process cannot read it (Full Disk Access / TCC), or iCloud left a
    dataless placeholder. That is precisely when the data must be left alone.
    Callers that legitimately tolerate a missing dataset should catch this.
    """

    def __init__(self, path, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(
            f"Cannot read {path}: {reason}. Refusing to continue, because "
            f"treating it as empty would overwrite it. If this is the second "
            f"Mac, grant Full Disk Access to the terminal app; if iCloud left "
            f"a placeholder, run: brctl download {path}"
        )


class RefusingToEmptyDataFile(Exception):
    """A save would have replaced a populated data file with an empty one.

    Defence in depth behind :class:`DataFileUnreadable`: even if some other
    path constructs a task-less document, it must not silently land on top of
    real history. Pass ``allow_empty=True`` to mean it.
    """


def load() -> dict:
    if DATA_FILE.exists():
        # Read and parse before touching anything. A failure here must abort:
        # the migrations below write, so falling back to {} would persist it.
        try:
            raw = DATA_FILE.read_text()
        except OSError as exc:
            raise DataFileUnreadable(DATA_FILE, f"{type(exc).__name__}: {exc}") from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            # Keep the bytes: a truncated or conflicted file is often
            # recoverable by hand, and it is the only evidence of what went
            # wrong. Overwriting it with defaults destroys both.
            raise DataFileUnreadable(DATA_FILE, f"invalid JSON ({exc})") from exc
        if not isinstance(data, dict):
            raise DataFileUnreadable(DATA_FILE, f"expected an object, got {type(data).__name__}")
    else:
        # Genuinely absent — a fresh install. Defaults are correct here, and
        # nothing is at risk of being overwritten.
        data = {}
    # Ensure required keys exist
    data.setdefault("tasks", [])
    data.setdefault("active_timer", None)
    # Initialize roles if missing
    if "roles" not in data:
        data["roles"] = DEFAULT_ROLES.copy()
    # One-time migration of legacy calendar_event_mappings values
    # (task_id -> base name). Idempotent on subsequent runs.
    mutated = _migrate_calendar_mappings(data)
    # Move github_repo/activity/type from roles onto tasks (one-time copy,
    # roles stripped on every load). Idempotent on subsequent runs.
    mutated = _migrate_role_github_fields(data) or mutated
    # Convert cross-sprint shadow tasks into per-sprint issue bindings (one-time),
    # and strip re-introduced shadows on every load. Idempotent.
    mutated = _migrate_shadows_to_bindings(data) or mutated
    # Collapse per-sprint recurrent clones into one task per series (one-time),
    # and absorb any clone an older wt.py re-introduces. Idempotent.
    mutated = _migrate_recurrent_series_to_bindings(data) or mutated
    if mutated:
        save(data)
    return data


def save(data: dict, path=None, *, allow_empty: bool = False):
    """Persist *data*, atomically and under the sidecar lock.

    The single write path for every front end — the CLI, ``tracker.save_data()``
    and ``mcp_server.save()`` all end up here, so there is exactly one place
    that knows how the file is written.

    Safe to call while already holding ``data_lock()`` (it re-enters), which is
    how a daemon wraps a whole read-modify-write. Optional *path* overrides the
    target for callers that keep their own ``DATA_FILE`` binding (mcp_server,
    tracker); it defaults to this module's.

    ``required=False``: a save must not raise just because some other writer is
    slow — the write is atomic either way, so the worst case is the old
    lost-update behaviour rather than a lost time entry or a wedged TUI.

    Refuses to replace a **populated** file with a task-less document and
    raises :class:`RefusingToEmptyDataFile`. That is never a legitimate
    incremental edit, and it is the shape every known data-loss path converges
    on. Pass ``allow_empty=True`` to mean it (deleting the last task, or a
    genuinely fresh install). Creating a new file, or writing over one that is
    already empty, is unaffected.
    """
    target = Path(path) if path is not None else DATA_FILE
    with data_lock(target, required=False):
        if not allow_empty and not data.get("tasks") and target.exists():
            # A target that does not exist yet cannot be destroyed — creating
            # the file on a fresh install is the one legitimate empty write.
            try:
                existing = json.loads(target.read_text())
                had_tasks = len(existing.get("tasks", []))
            except (OSError, ValueError):
                # Unreadable or corrupt: we cannot prove it was empty, so we
                # must not assume it was. Treat it as populated and refuse.
                had_tasks = -1
            if had_tasks != 0:
                raise RefusingToEmptyDataFile(
                    f"Refusing to write 0 tasks over {target}, which currently has "
                    f"{'an unreadable number of' if had_tasks < 0 else had_tasks} tasks. "
                    f"Pass allow_empty=True if this is intended."
                )
        _atomic_write_json(target, data)


def get_roles(data: dict) -> dict:
    """Return dict of role_id -> label"""
    return {r["id"]: r["label"] for r in data.get("roles", [])}


def get_role_ids(data: dict) -> list:
    """Return list of role IDs"""
    return [r["id"] for r in data.get("roles", [])]


def get_imported_calendar_uids(data: dict) -> set:
    """Collect all imported calendar event UIDs from tasks and log entries."""
    uids = set()
    for t in data.get("tasks", []):
        if t.get("calendar_event_uid"):
            uids.add(t["calendar_event_uid"])
        for log in t.get("logs", []):
            if log.get("calendar_event_uid"):
                uids.add(log["calendar_event_uid"])
    return uids


def find_calendar_event_owner(data: dict, event_uid: str):
    """Return ``(task, log)`` for a calendar event UID.

    - If the UID matches a task-level ``calendar_event_uid`` (the whole event
      was imported as its own task), returns ``(task, None)``.
    - If the UID matches a log entry's ``calendar_event_uid`` (the event was
      logged against an existing task), returns ``(task, log)``.
    - Returns ``(None, None)`` if the UID isn't imported anywhere.
    """
    for t in data.get("tasks", []):
        if t.get("calendar_event_uid") == event_uid:
            return (t, None)
        for log in t.get("logs", []):
            if log.get("calendar_event_uid") == event_uid:
                return (t, log)
    return (None, None)


def normalize_event_title(title: str) -> str:
    """Normalize event title for mapping lookup (lowercase + trimmed)."""
    return title.strip().lower()


def get_event_mapping(data: dict, event_title: str) -> str | None:
    """Get the task base name mapped to an event title, or None if not mapped.

    The stored value is the sprint-suffix-stripped task title (e.g.
    ``"Stand Up Calls - casanabria"``), not a task id. Use
    :func:`resolve_event_to_task` to get the actual task dict.
    """
    mappings = data.get("config", {}).get("calendar_event_mappings", {})
    key = normalize_event_title(event_title)
    # Check exact match first
    if key in mappings:
        return mappings[key]
    # Check all stored keys (normalized)
    for stored_title, base_name in mappings.items():
        if normalize_event_title(stored_title) == key:
            return base_name
    return None


def set_event_mapping(data: dict, event_title: str, base_name: str):
    """Create or update an event title -> task base name mapping.

    *base_name* should be the task title with any trailing ``- Sprint XX``
    suffix removed (use :func:`strip_sprint_suffix`). Many events can map
    to the same base name; each event name appears at most once.
    """
    if "config" not in data:
        data["config"] = {}
    if "calendar_event_mappings" not in data["config"]:
        data["config"]["calendar_event_mappings"] = {}
    data["config"]["calendar_event_mappings"][event_title.strip()] = base_name


def remove_event_mapping(data: dict, event_title: str) -> bool:
    """Remove a mapping by event title. Returns True if found and removed."""
    mappings = data.get("config", {}).get("calendar_event_mappings", {})
    key = normalize_event_title(event_title)
    # Find the actual key (may differ in case/whitespace)
    for stored_title in list(mappings.keys()):
        if normalize_event_title(stored_title) == key:
            del mappings[stored_title]
            return True
    return False


def get_event_names_for_base(data: dict, base_name: str) -> list[str]:
    """Return all event titles mapped to *base_name* (case-insensitive).

    Reverse lookup over the calendar event mappings; used by the TUI's
    auto-log batch flow to find which calendar events to surface for a
    highlighted task.
    """
    mappings = data.get("config", {}).get("calendar_event_mappings", {})
    target = (base_name or "").strip().lower()
    return [event_title for event_title, mapped_base in mappings.items()
            if (mapped_base or "").strip().lower() == target]


# Legacy task ids look like 14 digits + 4 lowercase letters (see uid()).
_LEGACY_TASK_ID_RE = re.compile(r"^\d{14}[a-z]{4}$")


def _migrate_calendar_mappings(data: dict) -> bool:
    """One-time migration: convert legacy task_id values to base names.

    Older versions of the tracker stored ``event_title -> task_id`` in
    ``data["config"]["calendar_event_mappings"]``. Now we store
    ``event_title -> base_name`` (the sprint-suffix-stripped task title)
    so a single mapping resolves to whichever per-sprint task copy matches
    the event's date.

    For each entry:
      * If the value is an existing task id -> replace with its base name.
      * If the value looks like a legacy task id (orphan) -> drop the entry.
      * Otherwise leave the value alone (already a base name).

    Returns True if any mutation occurred. Idempotent on repeat runs.
    """
    config = data.get("config", {})
    mappings = config.get("calendar_event_mappings")
    if not mappings:
        return False
    tasks_by_id = {t["id"]: t for t in data.get("tasks", []) if t.get("id")}
    mutated = False
    for event_title in list(mappings.keys()):
        value = mappings[event_title]
        if not isinstance(value, str):
            continue
        task = tasks_by_id.get(value)
        if task is not None:
            base_name = strip_sprint_suffix(task.get("title", ""))
            if base_name and base_name != value:
                mappings[event_title] = base_name
                mutated = True
            continue
        if _LEGACY_TASK_ID_RE.match(value):
            # Orphan id (source task deleted) — drop the mapping.
            del mappings[event_title]
            mutated = True
    return mutated


# GitHub-related fields that used to live on role dicts and now live on tasks.
_ROLE_GITHUB_FIELDS = ("github_repo", "activity", "type")


def _migrate_role_github_fields(data: dict) -> bool:
    """Move github_repo/activity/type from role dicts onto tasks.

    Roles used to carry the GitHub repo and the GitHub Project
    Activity/Type values for all their tasks. These are now per-task
    fields, so:

      * Copy step (one-time, guarded by
        ``config["role_fields_migrated_to_tasks"]``): copy each role's
        fields onto its tasks when the task doesn't already have them.
        The guard means role fields re-introduced later (e.g. by an old
        wt.py on another Mac) are never re-copied onto tasks that are
        intentionally left without a repo/activity.
      * Strip step (every load): remove the three fields from role dicts.

    Returns True if any mutation occurred. Idempotent on repeat runs.
    """
    mutated = False
    config = data.setdefault("config", {})
    roles = data.get("roles", [])
    if not config.get("role_fields_migrated_to_tasks"):
        role_by_id = {r["id"]: r for r in roles}
        for task in data.get("tasks", []):
            role = role_by_id.get(task.get("role_id", "other"))
            if not role:
                continue
            for key in _ROLE_GITHUB_FIELDS:
                if role.get(key) and key not in task:
                    task[key] = role[key]
        config["role_fields_migrated_to_tasks"] = True
        mutated = True
    for role in roles:
        for key in _ROLE_GITHUB_FIELDS:
            if key in role:
                del role[key]
                mutated = True
    return mutated


def _merge_binding(bindings: list[dict], new: dict) -> bool:
    """Append *new* to *bindings* unless its sprint_id is already bound.

    One binding per sprint (plan §6 invariant 4). On collision the entry that
    carries an ``issue`` wins; when *both* carry a distinct issue the one with the
    larger ``hours_synced`` wins and the loser is recorded in the winner's
    ``superseded_issues`` — **never dropped**.

    That case is real: merging the recurrent clones found two issues for one
    sprint (``#5615`` at 2.0h and ``#5719`` at 3.75h, both Sprint 101 of the
    Ad-hoc Slack series, because one clone's title said Sprint 100 while its logs
    fell in 101). Silently discarding one would leave it open on the project,
    still reporting its old hours, while the surviving binding reports the merged
    total — i.e. double-counting. A superseded issue must be zeroed and closed;
    reconcile plans that as a ``supersede`` op.

    Returns True if *bindings* was modified.
    """
    sprint_id = new.get("sprint_id")
    existing = None
    if sprint_id:
        existing = next((b for b in bindings if b.get("sprint_id") == sprint_id), None)
    if existing is None:
        bindings.append(new)
        return True

    def _carry(winner: dict, loser: dict) -> None:
        """Move the loser's issue (and anything it superseded) onto the winner."""
        extra = list(winner.get("superseded_issues") or [])
        for ref in list(loser.get("superseded_issues") or []) + [loser.get("issue")]:
            if ref and ref != winner.get("issue") and ref not in extra:
                extra.append(ref)
        if extra:
            winner["superseded_issues"] = extra

    if new.get("issue") and not existing.get("issue"):
        _carry(new, existing)
        bindings[bindings.index(existing)] = new
        return True
    if not new.get("issue"):
        # Incumbent keeps the sprint; still preserve anything new superseded.
        before = list(existing.get("superseded_issues") or [])
        _carry(existing, new)
        return list(existing.get("superseded_issues") or []) != before
    if new["issue"] == existing.get("issue"):
        return False

    # Both bound, to different issues: keep the better-evidenced one.
    def rank(b):
        h = b.get("hours_synced")
        return (h if isinstance(h, (int, float)) else -1.0)

    if rank(new) > rank(existing):
        _carry(new, existing)
        bindings[bindings.index(existing)] = new
    else:
        _carry(existing, new)
    return True


def _dedupe_bindings(task: dict) -> bool:
    """Collapse duplicate sprint_ids in a task's bindings. True if changed."""
    bindings = task.get("sprint_issues")
    if not isinstance(bindings, list) or len(bindings) < 2:
        return False
    kept: list[dict] = []
    for b in bindings:
        _merge_binding(kept, b)
    if len(kept) == len(bindings):
        return False
    task["sprint_issues"] = kept
    return True


def _sort_task_bindings(task: dict, sprints: list[dict]) -> bool:
    """Store a task's bindings in chronological (sprint start_date) order.

    Keeping the persisted list sorted means "the last binding" is always the
    most recent sprint, so ``task_current_issue(task)`` picks the right issue
    even when it is called without *data* (no sprint cache to sort by). Bindings
    whose sprint can't be resolved keep sorting first, so they never become the
    accidental "current" one. Returns True if the order changed.
    """
    bindings = task.get("sprint_issues")
    if not isinstance(bindings, list) or len(bindings) < 2:
        return False
    start_by_id = {s["id"]: s.get("start_date") for s in (sprints or [])}
    ordered = sorted(
        bindings,
        key=lambda b: _sprint_start_sort_key(start_by_id.get(b.get("sprint_id"))),
    )
    if ordered == bindings:
        return False
    task["sprint_issues"] = ordered
    return True


def _ensure_bindings(task: dict) -> list[dict]:
    """Return the task's ``sprint_issues`` list, seeding it from legacy fields."""
    bindings = task.get("sprint_issues")
    if not isinstance(bindings, list):
        legacy = _legacy_binding_for_task(task)
        bindings = [legacy] if legacy else []
        task["sprint_issues"] = bindings
    return bindings


def _shadow_binding(shadow: dict) -> dict:
    """Build the parent binding that replaces a shadow task object.

    The shadow's hours are preserved as ``hours_synced`` (what GitHub was told)
    and its newest log timestamp as ``synced_at``. Its synthetic "Sprint split"
    marker log is deliberately dropped: the parent already holds the real logs,
    so merging would double-count (plan §4 Phase 1, step 4).
    """
    logs = shadow.get("logs", []) or []
    marker_mins = sum(l.get("minutes", 0) for l in logs)
    stamps = [l.get("at") for l in logs if l.get("at")]
    return {
        "sprint_id": shadow.get("sprint_id"),
        "sprint": shadow.get("sprint"),
        "issue": shadow.get("github_issue"),
        "state": "closed",
        "hours_synced": mins_to_quarter_hours(marker_mins) if marker_mins else None,
        "synced_at": max(stamps) if stamps else None,
        "created_at": shadow.get("created_at"),
    }


def _migrate_shadows_to_bindings(data: dict) -> bool:
    """Replace cross-sprint shadow tasks with per-sprint issue bindings.

    See docs/plan-sprint-bindings.md §2.1/§4. Two parts:

      * One-time conversion (guarded by ``config["sprint_bindings_migrated"]``):
        give every task a ``sprint_issues`` list seeded from its legacy
        ``sprint_id``/``github_issue``, and freeze ``start_sprint_id`` /
        ``start_sprint`` from its earliest log (offline, via the sprints cache;
        left unset when the sprint can't be resolved).
      * Shadow sweep (**every load**): any task carrying ``cross_sprint_parent``
        is converted into a binding on its parent and deleted, mirroring how
        ``_migrate_role_github_fields`` keeps stripping legacy role keys — an
        older wt.py on another Mac can re-introduce shadows via iCloud.

    Legacy ``sprint``/``sprint_id``/``github_issue`` keys are kept (Phase 1 is
    additive plus shadow removal; Phase 3 stops reading them). ``logs`` and the
    per-task ``github_repo``/``activity``/``type`` are never touched.

    Orphan shadows (parent id not in the data) are left alone and reported via
    ``logging.warning`` rather than silently deleted.

    Returns True if any mutation occurred. Idempotent on repeat runs.
    """
    mutated = False
    config = data.setdefault("config", {})
    tasks = data.get("tasks", [])
    first_run = not config.get("sprint_bindings_migrated")

    # 1. Seed each real task's bindings from its legacy fields (one-time).
    if first_run:
        for task in tasks:
            if task.get("cross_sprint_parent"):
                continue  # shadows are removed below, not migrated
            if task.get("sprint_issues") is None:
                _ensure_bindings(task)
                mutated = True

    # 2/4. Convert shadows into parent bindings and drop the shadow objects.
    by_id = {t["id"]: t for t in tasks if t.get("id")}
    survivors = []
    orphans = []
    for task in tasks:
        parent_id = task.get("cross_sprint_parent")
        if not parent_id:
            survivors.append(task)
            continue
        parent = by_id.get(parent_id)
        if parent is None:
            # 5. Orphan: keep it, report it. Never destroy data we can't re-home.
            orphans.append(task)
            survivors.append(task)
            continue
        if _merge_binding(_ensure_bindings(parent), _shadow_binding(task)):
            mutated = True
        mutated = True  # the shadow object itself goes away
    if len(survivors) != len(tasks):
        data["tasks"] = survivors
        tasks = survivors

    if orphans:
        logging.warning(
            "sprint bindings migration: %d shadow task(s) kept — parent missing: %s",
            len(orphans),
            ", ".join(f"{t.get('title', '?')} (parent {t.get('cross_sprint_parent')})"
                      for t in orphans),
        )

    # Sprint dates come from the persisted cache — this migration never touches
    # the network (load() must stay offline).
    sprints = get_cached_sprints(data)

    # 3. Freeze the start sprint from the earliest log (one-time, offline).
    if first_run:
        if sprints:
            for task in tasks:
                if task.get("cross_sprint_parent") or task.get("start_sprint_id"):
                    continue
                start = task_start_sprint(task, sprints)
                if start:
                    task["start_sprint_id"] = start["id"]
                    task["start_sprint"] = start["title"]
                    mutated = True
        config["sprint_bindings_migrated"] = True
        mutated = True

    # 9. One binding per sprint, always — and keep the list chronological so
    #    bindings[-1] is the most recent sprint.
    for task in tasks:
        if _dedupe_bindings(task):
            mutated = True
        if _sort_task_bindings(task, sprints):
            mutated = True

    return mutated



# Recurring work used to be modelled as one cloned task per sprint, titled
# "<base> - Sprint N". Phase 5 collapses each series into a single perpetual task
# that grows one binding per sprint, exactly like any other cross-sprint task.
#
# Grouping cannot be fully automatic: the live data drifted three ways for one
# series ("Ad-hoc Slack Questions", "Ad-hoc Slack Questions - casanabria",
# "Ad-hoc Slack Question casanabria" — note the singular and the missing dash),
# and the prefix-boundary matching the retired recreate planner used bridged only
# two of them. So the drift is resolved
# by an explicit alias table: normalised stripped title -> canonical title.
# Anything not listed here is left alone.
RECURRENT_SERIES_ALIASES = {
    "stand up calls - casanabria":        "Stand Up Calls - casanabria",
    "ana 1:1 calls - casanabria":         "Ana 1:1 calls - casanabria",
    "general demo kit maintenance":       "General Demo Kit maintenance",
    "time tracking":                      "Time tracking",
    # One logical series, three spellings.
    "ad-hoc slack questions":             "Ad-hoc Slack Questions - casanabria",
    "ad-hoc slack questions - casanabria": "Ad-hoc Slack Questions - casanabria",
    "ad-hoc slack question casanabria":   "Ad-hoc Slack Questions - casanabria",
}


def recurrent_series_for_title(title: str) -> str | None:
    """Canonical series title for *title*, or None if it isn't a known series.

    Matches on the sprint-suffix-stripped title, case- and whitespace-insensitive,
    so both "Ad-hoc Slack Questions - Sprint 103" and the already-merged
    "Ad-hoc Slack Questions - casanabria" resolve to the same series.
    """
    base = " ".join(strip_sprint_suffix(title or "").split()).lower()
    return RECURRENT_SERIES_ALIASES.get(base)


def _migrate_recurrent_series_to_bindings(data: dict) -> bool:
    """Collapse per-sprint recurrent clones into one task per series (Phase 5).

    Recurring work was cloned per sprint with a ``- Sprint N`` title suffix, so a
    single activity was scattered across up to ten task objects — each with its
    own id, its own issue, and its own slice of the logs. That is the same
    problem the shadow tasks had, solved a second way; with per-sprint bindings
    the clones are redundant.

    For each series in :data:`RECURRENT_SERIES_ALIASES`:

      * the **earliest-created** member survives and is retitled to the canonical
        name (the sprint suffix is what made the clones necessary);
      * every member's ``logs`` move onto it, deduped by log id;
      * every member's ``sprint_issues`` merge into it, one binding per sprint,
        preferring the entry that actually has an issue;
      * ``status`` becomes ``recurrent`` if any member was still recurrent;
      * ``start_sprint*`` is re-frozen from the merged earliest log;
      * ``calendar_event_mappings`` values and ``active_timer.task_id`` are
        re-pointed at the survivor so neither dangles;
      * absorbed members are deleted.

    **No GitHub call and no log edit**: every clone's issue survives as a binding,
    so the project keeps exactly the issues it had, each still carrying its own
    sprint's hours.

    Guarded by ``config["recurrent_series_merged"]`` for the one-time pass, but
    the absorb sweep runs on **every load** so a ``- Sprint N`` clone created by
    an older wt.py on another Mac is folded in rather than resurrecting the split
    model — the same defence ``_migrate_shadows_to_bindings`` uses for shadows.

    Returns True if anything changed. Idempotent.
    """
    tasks = data.get("tasks", [])
    if not tasks:
        return False

    config = data.setdefault("config", {})
    mutated = False

    groups: dict[str, list[dict]] = {}
    for task in tasks:
        canon = recurrent_series_for_title(task.get("title", ""))
        if canon:
            groups.setdefault(canon, []).append(task)

    absorbed_ids: set[str] = set()
    for canon, members in groups.items():
        if len(members) < 2:
            # Already a single task for this series: just make sure its title is
            # canonical so future clones group onto it.
            only = members[0]
            if only.get("title") != canon:
                only["title"] = canon
                mutated = True
            continue

        members.sort(key=lambda t: (t.get("created_at") or 0, t.get("id") or ""))
        survivor, absorbed = members[0], members[1:]

        if survivor.get("title") != canon:
            survivor["title"] = canon
            mutated = True

        bindings = _ensure_bindings(survivor)
        seen_logs = {l.get("id") for l in survivor.get("logs", []) if l.get("id")}
        for other in absorbed:
            for log in other.get("logs", []) or []:
                if log.get("id") and log["id"] in seen_logs:
                    continue
                seen_logs.add(log.get("id"))
                survivor.setdefault("logs", []).append(log)
            for binding in other.get("sprint_issues") or []:
                _merge_binding(bindings, dict(binding))
            if other.get("status") == "recurrent":
                survivor["status"] = "recurrent"
            # Inherit GitHub config only where the survivor lacks it; the live
            # data has these identical across a series, so this is a no-op there.
            for key in _ROLE_GITHUB_FIELDS:
                if not survivor.get(key) and other.get(key):
                    survivor[key] = other[key]
            absorbed_ids.add(other.get("id"))
            mutated = True

        survivor["logs"].sort(key=lambda l: log_effective_date(l) or 0)
        # The clone titles encoded the sprint; the merged task derives it from
        # logs instead, so drop the now-misleading legacy pointer's title form.
        survivor.pop("cross_sprint_parent", None)

    if not absorbed_ids:
        # Nothing to fold in. Still record that the one-time pass has run.
        if not config.get("recurrent_series_merged"):
            config["recurrent_series_merged"] = True
            mutated = True
        return mutated

    # Re-point anything that referenced an absorbed clone by id.
    at = data.get("active_timer")
    if at and at.get("task_id") in absorbed_ids:
        for canon, members in groups.items():
            if any(m.get("id") == at["task_id"] for m in members):
                keep = min(members, key=lambda t: (t.get("created_at") or 0, t.get("id") or ""))
                logging.warning(
                    "active timer moved from absorbed clone %s to %r",
                    at["task_id"], keep.get("title"))
                at["task_id"] = keep["id"]
                mutated = True
                break

    # Calendar mappings store the sprint-stripped base name; re-point any that
    # named a drifted spelling at the canonical title.
    mappings = config.get("calendar_event_mappings")
    if isinstance(mappings, dict):
        for event, base in list(mappings.items()):
            canon = recurrent_series_for_title(base or "")
            if canon and base != canon:
                mappings[event] = canon
                mutated = True

    data["tasks"] = [t for t in tasks if t.get("id") not in absorbed_ids]

    sprints = get_cached_sprints(data)
    for task in data["tasks"]:
        if recurrent_series_for_title(task.get("title", "")) is None:
            continue
        if _dedupe_bindings(task):
            mutated = True
        if _sort_task_bindings(task, sprints):
            mutated = True
        # The survivor is the *earliest* clone, so its legacy sprint/sprint_id
        # still name the sprint the series began in. Leaving them there makes the
        # carry-forward rule (which trusts task["sprint_id"]) treat the oldest
        # sprint's issue as the live one and re-point it to the current sprint —
        # vacating the sprint whose hours it actually carries. Point them at the
        # most recent binding instead, which is what "current" means.
        bindings = task.get("sprint_issues") or []
        if bindings:
            latest = bindings[-1]
            if latest.get("sprint_id") and task.get("sprint_id") != latest["sprint_id"]:
                task["sprint_id"] = latest["sprint_id"]
                task["sprint"] = latest.get("sprint")
                mutated = True
        # Re-freeze the start sprint: the merged log set reaches further back
        # than any single clone's did.
        if sprints and task.get("logs"):
            first = min((log_effective_date(l) or 0) for l in task["logs"])
            if first:
                start = find_sprint_for_date(
                    sprints, datetime.fromtimestamp(first).date())
                if start and task.get("start_sprint_id") != start["id"]:
                    task["start_sprint_id"] = start["id"]
                    task["start_sprint"] = start["title"]
                    mutated = True

    config["recurrent_series_merged"] = True
    return True


def resolve_task_by_id(data: dict, task_id: str) -> dict | None:
    """Find a task by its exact ID."""
    return next((t for t in data.get("tasks", []) if t["id"] == task_id), None)


# Matches a trailing " - Sprint <number>" suffix (case-insensitive, whitespace-tolerant).
SPRINT_SUFFIX_RE = re.compile(r"\s*-\s*Sprint\s+\d+\s*$", re.IGNORECASE)


def strip_sprint_suffix(title: str) -> str:
    """Return *title* with any trailing ' - Sprint XX' suffix removed.

    Used by the calendar mapping resolver so a single mapping like
    'Carlos / Ana weekly sync' → 'Ana 1:1 calls - casanabria - Sprint 100'
    can dispatch to the per-sprint task whose dates cover the calendar event.
    """
    return SPRINT_SUFFIX_RE.sub("", title or "").strip()


def resolve_event_to_task(data: dict, event: dict) -> dict | None:
    """Find which task a calendar event should be logged to via its mapping.

    The mapping value is a *base name* (sprint-suffix stripped). This function:
      1. Looks up the base name for ``event["title"]``.
      2. Collects all tasks whose ``strip_sprint_suffix(title)``
         matches that base name (case-insensitive).
      3. If the event's ``start_date`` resolves to a sprint, returns the
         candidate whose ``sprint_id`` matches.
      4. Otherwise prefers non-done tasks, then the most recent sprint
         start_date, then ``created_at``.

    Returns ``None`` if no mapping exists or no candidate tasks remain.
    """
    base_name = get_event_mapping(data, event.get("title", ""))
    if not base_name:
        return None
    base_lower = base_name.strip().lower()
    if not base_lower:
        return None

    candidates = [
        t for t in data.get("tasks", [])
        if strip_sprint_suffix(t.get("title", "")).lower() == base_lower
    ]
    if not candidates:
        return None

    # Try sprint-aware match first.
    start_ts = event.get("start_date")
    if start_ts:
        try:
            event_date = datetime.fromtimestamp(start_ts).date()
        except (TypeError, ValueError, OSError):
            event_date = None
        if event_date:
            sprints = get_cached_sprints(data)
            if not sprints:
                sprints = get_all_sprints(data) or []
            target_sprint = find_sprint_for_date(sprints, event_date)
            if target_sprint:
                for t in candidates:
                    if t.get("sprint_id") == target_sprint["id"]:
                        return t

    # Fallback: prefer non-done, then most recent sprint, then created_at.
    sprints = get_cached_sprints(data) or []
    sprint_starts = {s["id"]: s.get("start_date") for s in sprints}

    def _sort_key(t):
        is_done = t.get("status") == "done"
        sprint_start = sprint_starts.get(t.get("sprint_id"))
        # Sort newer-first using negative ordinal; fall back to created_at.
        if sprint_start is not None:
            try:
                sprint_score = -sprint_start.toordinal()
            except AttributeError:
                sprint_score = 0
        else:
            sprint_score = 0
        created = -(t.get("created_at") or 0)
        return (is_done, sprint_score, created)

    return sorted(candidates, key=_sort_key)[0]


def fmt_mins(mins: float) -> str:
    if not mins:
        return "0m"
    h = int(mins // 60)
    m = int(mins % 60)
    return f"{h}h {m}m" if h else f"{m}m"


def task_logged_mins(task: dict) -> float:
    return sum(l.get("minutes", 0) for l in task.get("logs", []))


def task_logged_mins_for_sprint(task: dict, sprints: list) -> float:
    """**Deprecated** — use :func:`task_mins_for_sprint` / :func:`task_reportable_mins`.

    Sum log minutes that fall within the task's assigned sprint.

    If the task has no sprint_id or sprint not found, returns total logged minutes.
    Used when reporting hours to GitHub: a task split across sprints keeps all
    logs locally but should only report its assigned sprint's hours to GH.

    Kept only because ``mcp_server.py`` imports it. No ``wt.py`` code path calls
    it any more: the "sprint unknown → report the task total" fallback silently
    over-reports, so callers now go through ``task_reportable_mins()``, which
    resolves the sprint from the task's *bindings* first and only falls back to
    the total when no sprint can be resolved at all.
    """
    sprint_id = task.get("sprint_id")
    if not sprint_id or not sprints:
        return task_logged_mins(task)

    sprint = next((s for s in sprints if s["id"] == sprint_id), None)
    if not sprint or not sprint.get("start_date"):
        return task_logged_mins(task)

    from datetime import datetime
    total = 0.0
    for log in task.get("logs", []):
        ts = log.get("started_at") or log.get("at", 0)
        if not ts:
            # No timestamp: attribute to task's sprint
            total += log.get("minutes", 0)
            continue
        log_date = datetime.fromtimestamp(ts).date()
        if sprint["start_date"] <= log_date < sprint["end_date"]:
            total += log.get("minutes", 0)
    return total


def task_uploaded_mins(task: dict) -> float:
    """Sum of minutes from logs that have been uploaded to GitHub."""
    return sum(l.get("minutes", 0) for l in task.get("logs", []) if l.get("uploaded_at"))


def task_pending_upload_mins(task: dict) -> float:
    """Sum of minutes from logs that haven't been uploaded to GitHub."""
    return sum(l.get("minutes", 0) for l in task.get("logs", []) if not l.get("uploaded_at"))


def round_to_quarter_hours(mins: float) -> float:
    """Round minutes up to nearest 15 minutes (0.25 hours).

    Examples:
        1 min -> 15 min (0.25 hours)
        15 min -> 15 min (0.25 hours)
        16 min -> 30 min (0.5 hours)
        45 min -> 45 min (0.75 hours)
        46 min -> 60 min (1 hour)
    """
    import math
    quarters = math.ceil(mins / 15)
    return quarters * 15


def mins_to_quarter_hours(mins: float) -> float:
    """Convert minutes to hours, rounded up to nearest 0.25."""
    rounded_mins = round_to_quarter_hours(mins)
    return rounded_mins / 60


def round_up_to_30(mins: float) -> int:
    """Round minutes up to the next multiple of 30.

    Examples:
        25 -> 30
        30 -> 30
        31 -> 60
        59 -> 60
        60 -> 60
        61 -> 90
    """
    import math
    if mins is None or mins <= 0:
        return 0
    return int(math.ceil(mins / 30) * 30)


def mark_logs_uploaded(task: dict, up_to_time: float = None) -> int:
    """Mark all unuploaded logs as uploaded. Returns count of logs marked."""
    import time as _time
    up_to_time = up_to_time or _time.time()
    count = 0
    for log in task.get("logs", []):
        if not log.get("uploaded_at"):
            log["uploaded_at"] = up_to_time
            count += 1
    return count


def task_live_mins(task: dict, at) -> float:
    if at and at.get("task_id") == task["id"]:
        return (time.time() - at["started_at"]) / 60
    return 0.0


def resolve_task(data: dict, query: str):
    tasks = data.get("tasks", [])
    # Exact ID match
    match = next((t for t in tasks if t["id"] == query), None)
    if match:
        return match

    q = query.lower()

    # Exact title match (case-insensitive)
    exact_matches = [t for t in tasks if t["title"].lower() == q]
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        # Prefer non-done tasks
        active = [t for t in exact_matches if t.get("status") != "done"]
        if len(active) == 1:
            return active[0]

    # Partial title match (case-insensitive)
    matches = [t for t in tasks if q in t["title"].lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # Prefer non-done tasks
        active = [t for t in matches if t.get("status") != "done"]
        if len(active) == 1:
            return active[0]
        # Still ambiguous - show options (prefer showing active tasks first)
        display = active if active else matches
        print(c("Ambiguous match. Did you mean:", "yellow"))
        for t in display:
            status = " [done]" if t.get("status") == "done" else ""
            print(f"  {t['id']}  {t['title']}{status}")
        sys.exit(1)
    print(c(f"No task matching '{query}'", "red"))
    sys.exit(1)


def resolve_role(data: dict, raw: str) -> str:
    r = raw.lower()
    role_ids = get_role_ids(data)
    if r in role_ids:
        return r
    # Partial match
    matches = [rid for rid in role_ids if rid.startswith(r)]
    if len(matches) == 1:
        return matches[0]
    print(c(f"Unknown role '{raw}'. Use: {', '.join(role_ids)}", "red"))
    sys.exit(1)


def notes_path(task_id: str) -> Path:
    return NOTES_DIR / f"{task_id}.md"


def has_notes(task_id: str) -> bool:
    p = notes_path(task_id)
    return p.exists() and p.stat().st_size > 0


def normalize_issue_ref(issue_ref: str, data: dict, task: dict = None) -> str:
    """Normalize issue reference, using default repo for bare numbers.

    Handles:
      - "262" -> "owner/repo#262" (uses task's repo, then config github_repo)
      - "#262" -> "owner/repo#262" (uses task's repo, then config github_repo)
      - "owner/repo#262" -> "owner/repo#262"
      - "https://github.com/owner/repo/issues/262" -> "owner/repo#262"
    """
    import re

    # Handle full GitHub URL
    url_match = re.match(r'https?://github\.com/([^/]+/[^/]+)/issues/(\d+)', issue_ref)
    if url_match:
        return f"{url_match.group(1)}#{url_match.group(2)}"

    # Handle bare number or #number
    bare_match = re.match(r'^#?(\d+)$', issue_ref)
    if bare_match:
        # Try task's repo first, then global config
        repo = None
        if task:
            repo = get_task_repo(task)
        if not repo:
            repo = data.get("config", {}).get("github_repo")
        if not repo:
            print(c("Issue number requires a default repo.", "red"))
            print("Set with: wt config github-repo owner/repo")
            print("Or use full reference: owner/repo#123")
            sys.exit(1)
        return f"{repo}#{bare_match.group(1)}"

    # Already in owner/repo#number format
    return issue_ref


def gh_issue_args(issue_ref: str) -> list[str]:
    """Convert owner/repo#123 format to gh command args: ["-R", "owner/repo", "123"]."""
    import re
    match = re.match(r'^([^#]+)#(\d+)$', issue_ref)
    if match:
        return ["-R", match.group(1), match.group(2)]
    # Fallback (URL or other format) - let gh handle it
    return [issue_ref]


# ── GitHub Project Integration ───────────────────────────

def get_task_repo(task: dict) -> str | None:
    """Get the GitHub repo for a task. Returns None if not set."""
    return task.get("github_repo") or None


def get_task_activity(task: dict) -> str | None:
    """Get the GitHub Project activity for a task. Returns None if not set."""
    return task.get("activity") or None


def get_task_type(task: dict) -> str | None:
    """Get the GitHub Project type for a task. Returns None if not set."""
    return task.get("type") or None


def create_github_issue(task: dict, repo: str) -> str:
    """Create a GitHub issue for a task in the specified repo.
    Includes local notes in issue body.
    Returns the issue reference (owner/repo#number).
    """
    import re

    # Read local notes if they exist
    npath = notes_path(task["id"])
    body = ""
    if npath.exists():
        body = npath.read_text()

    # Create issue via gh CLI (assign to current user)
    cmd = ["gh", "issue", "create", "-R", repo, "--title", task["title"], "--assignee", "@me"]
    if body:
        cmd.extend(["--body", body])
    else:
        cmd.extend(["--body", f"Task created from workload tracker: {task['title']}"])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"Failed to create issue: {result.stderr}")

    # Parse issue URL from output, convert to reference
    # gh outputs: https://github.com/owner/repo/issues/123
    url = result.stdout.strip()
    url_match = re.match(r'https?://github\.com/([^/]+/[^/]+)/issues/(\d+)', url)
    if url_match:
        return f"{url_match.group(1)}#{url_match.group(2)}"
    else:
        raise Exception(f"Could not parse issue URL: {url}")


# Map workload tracker status to GitHub project status
PROJECT_STATUS_MAP = {
    "todo": "Todo",
    "inprogress": "In Progress",
    "recurrent": "In Progress",
    "done": "Done",
}


# Project field metadata (project id + field/option ids) costs two GraphQL-backed
# `gh project` calls to fetch and changes only when the GitHub Project itself is
# edited. It used to be re-fetched per call site — `wt sync-sprints --all` over 67
# tasks burned ~134 of them and exhausted the 5000-point GraphQL budget mid-run,
# which surfaced as `gh`'s unhelpful "unknown owner type". Memoised per
# (owner, project number) with a short TTL: a burst run pays for it once, while a
# long-lived process (the TUI) still picks up new Activity/Type options within a
# few minutes rather than needing a restart.
PROJECT_INFO_TTL_SECONDS = 300
_PROJECT_INFO_CACHE: dict = {}


def clear_project_info_cache() -> None:
    """Drop the memoised project metadata. For tests and explicit refreshes."""
    _PROJECT_INFO_CACHE.clear()


def get_project_info(data: dict, refresh: bool = False) -> dict:
    """Get project ID and field information.

    Returns dict with project_id, status_field, hours_field, status_options, etc.
    Raises Exception if project not configured or fields missing.

    Memoised for ``PROJECT_INFO_TTL_SECONDS``; pass ``refresh=True`` to force a
    re-fetch. Failures are never cached, so a transient error doesn't stick.
    """
    config = data.get("config", {})
    key = (config.get("github_project_owner", "grafana"),
           config.get("github_project_number"))
    if not refresh:
        hit = _PROJECT_INFO_CACHE.get(key)
        if hit and (time.time() - hit[0]) < PROJECT_INFO_TTL_SECONDS:
            # Keep the local Activity/Type options cache warm on hits too, so
            # behaviour matches an uncached fetch exactly.
            save_project_options_cache(data, hit[1])
            return hit[1]
    info = _fetch_project_info(data)
    _PROJECT_INFO_CACHE[key] = (time.time(), info)
    return info


def _fetch_project_info(data: dict) -> dict:
    """Uncached fetch behind :func:`get_project_info` — two `gh project` calls."""
    config = data.get("config", {})
    owner = config.get("github_project_owner", "grafana")
    project_num = config.get("github_project_number")

    if not project_num:
        raise Exception("github_project_number not configured")

    # Get project info (need full project ID for item-edit)
    project_result = subprocess.run([
        "gh", "project", "view", str(project_num),
        "--owner", owner, "--format", "json"
    ], capture_output=True, text=True)

    if project_result.returncode != 0:
        raise Exception(f"Failed to get project info: {project_result.stderr}")

    project_data = json.loads(project_result.stdout)
    project_id = project_data.get("id")

    # Get field IDs
    fields_result = subprocess.run([
        "gh", "project", "field-list", str(project_num),
        "--owner", owner, "--format", "json"
    ], capture_output=True, text=True)

    if fields_result.returncode != 0:
        raise Exception(f"Failed to get project fields: {fields_result.stderr}")

    fields_data = json.loads(fields_result.stdout)
    fields = {f["name"]: f for f in fields_data.get("fields", [])}

    status_field = fields.get("Status", {})
    hours_field = fields.get("Hours", {})
    activity_field = fields.get("Activity", {})
    type_field = fields.get("Type", {})
    sprint_field = fields.get("Sprint", {})

    if not status_field.get("id"):
        raise Exception("Project missing 'Status' field")

    # Build status options map
    status_options = {}
    for opt in status_field.get("options", []):
        status_options[opt.get("name")] = opt.get("id")

    # Build activity options map
    activity_options = {}
    for opt in activity_field.get("options", []):
        activity_options[opt.get("name")] = opt.get("id")

    # Build type options map
    type_options = {}
    for opt in type_field.get("options", []):
        type_options[opt.get("name")] = opt.get("id")

    info = {
        "owner": owner,
        "project_num": project_num,
        "project_id": project_id,
        "status_field": status_field,
        "hours_field": hours_field,
        "activity_field": activity_field,
        "type_field": type_field,
        "sprint_field": sprint_field,
        "status_options": status_options,
        "activity_options": activity_options,
        "type_options": type_options,
    }
    # Refresh the in-memory options cache; persists on the caller's next
    # save(data) (every flow that fetches project info saves shortly after).
    save_project_options_cache(data, info)
    return info


def get_all_sprints(data: dict) -> list[dict]:
    """Get all sprint iterations from the GitHub project.

    Returns list of dicts sorted by startDate ascending:
        [{id, title, startDate, duration, field_id, start_date, end_date}, ...]
    where start_date/end_date are datetime.date objects.
    Returns [] if project not configured or query fails.
    """
    from datetime import datetime, timedelta

    config = data.get("config", {})
    owner = config.get("github_project_owner", "grafana")
    project_num = config.get("github_project_number")

    if not project_num:
        return []

    query = f'''query {{
        organization(login: "{owner}") {{
            projectV2(number: {project_num}) {{
                field(name: "Sprint") {{
                    ... on ProjectV2IterationField {{
                        id
                        name
                        configuration {{
                            iterations {{
                                id
                                title
                                startDate
                                duration
                            }}
                            completedIterations {{
                                id
                                title
                                startDate
                                duration
                            }}
                        }}
                    }}
                }}
            }}
        }}
    }}'''

    result = subprocess.run([
        "gh", "api", "graphql", "-f", f"query={query}"
    ], capture_output=True, text=True)

    if result.returncode != 0:
        return []

    try:
        response = json.loads(result.stdout)
        field = response.get("data", {}).get("organization", {}).get("projectV2", {}).get("field", {})
        config_data = field.get("configuration", {})
        iterations = config_data.get("iterations", []) + config_data.get("completedIterations", [])
        field_id = field.get("id")

        sprints = []
        for iteration in iterations:
            start_date = datetime.strptime(iteration["startDate"], "%Y-%m-%d").date()
            end_date = start_date + timedelta(days=iteration["duration"])
            sprints.append({
                "id": iteration["id"],
                "title": iteration["title"],
                "startDate": iteration["startDate"],
                "duration": iteration["duration"],
                "field_id": field_id,
                "start_date": start_date,
                "end_date": end_date,
            })

        sprints.sort(key=lambda s: s["startDate"])
        return sprints
    except Exception:
        return []


def get_current_sprint(data: dict) -> dict | None:
    """Get the current sprint iteration based on today's date."""
    from datetime import datetime
    today = datetime.now().date()
    return find_sprint_for_date(get_all_sprints(data), today)


def find_sprint_for_date(sprints: list[dict], dt) -> dict | None:
    """Find which sprint a date falls in.

    Args:
        sprints: List from get_all_sprints()
        dt: datetime.date object

    Returns matching sprint dict or None if date falls outside all sprints.
    """
    if not dt:
        return None
    for s in sprints:
        if s["start_date"] <= dt < s["end_date"]:
            return s
    return None


def save_sprints_cache(data: dict, sprints: list[dict]) -> None:
    """Persist the sprint list to data['config']['sprints_cache'].

    Stores only the fields needed for offline lookup (id, title, start_date,
    end_date, field_id). Dates are ISO strings so the JSON file stays portable.
    The caller is responsible for invoking save(data) after this.
    """
    if not sprints:
        return
    cfg = data.setdefault("config", {})
    cfg["sprints_cache"] = [
        {
            "id": s["id"],
            "title": s["title"],
            "start_date": s["start_date"].isoformat(),
            "end_date": s["end_date"].isoformat(),
            "field_id": s.get("field_id"),
        }
        for s in sprints
    ]


def get_cached_sprints(data: dict) -> list[dict]:
    """Read the persisted sprint list from config and parse dates back to date objects.

    Returns [] if the cache is missing or unreadable. Cached sprints carry the
    same start_date/end_date date-object contract as get_all_sprints().
    """
    from datetime import date
    raw = data.get("config", {}).get("sprints_cache", [])
    out = []
    for s in raw:
        try:
            out.append({
                "id": s["id"],
                "title": s["title"],
                "start_date": date.fromisoformat(s["start_date"]),
                "end_date": date.fromisoformat(s["end_date"]),
                "field_id": s.get("field_id"),
            })
        except (KeyError, ValueError):
            continue
    return out


def save_project_options_cache(data: dict, project_info: dict) -> None:
    """Persist the project's Activity/Type option names to config.

    Stores data['config']['project_options_cache'] = {"activity": [...],
    "type": [...]} preserving GitHub's option order, so the TUI Selects and
    CLI validation work without a network call. The caller is responsible
    for invoking save(data) after this (same contract as save_sprints_cache).
    """
    cfg = data.setdefault("config", {})
    cfg["project_options_cache"] = {
        "activity": list(project_info.get("activity_options", {}).keys()),
        "type": list(project_info.get("type_options", {}).keys()),
    }


def get_cached_project_options(data: dict) -> dict:
    """Read the persisted Activity/Type option lists from config.

    Returns {} when the cache is missing; otherwise a dict like
    {"activity": [...], "type": [...]}.
    """
    return data.get("config", {}).get("project_options_cache", {})


def get_sprint_date_range_for_task(task: dict | None, data: dict):
    """Resolve (start_date, end_date) for a task's sprint context.

    Lookup order:
      1. The task's sprint_id, resolved against the persisted sprints cache.
      2. The current sprint based on today's date, resolved against the cache.
      3. The same lookups against a live get_all_sprints() call (network).

    Returns a (sprint_dict, start_date, end_date) tuple or None if no sprint
    information is available. The returned dates are datetime.date objects.
    The returned end_date is the *inclusive* last day of the sprint (i.e.
    sprint["end_date"] - 1 day), since the stored sprint end_date follows the
    half-open `[start, end)` convention used by find_sprint_for_date.
    """
    from datetime import datetime

    def _pick(sprints):
        if not sprints:
            return None
        if task and task.get("sprint_id"):
            for s in sprints:
                if s["id"] == task["sprint_id"]:
                    return s
        today = datetime.now().date()
        return find_sprint_for_date(sprints, today)

    sprint = _pick(get_cached_sprints(data))
    if sprint is None:
        sprint = _pick(get_all_sprints(data))
    if sprint is None:
        return None
    return sprint, sprint["start_date"], sprint["end_date"] - timedelta(days=1)


def log_effective_date(log: dict) -> float:
    """Return the best timestamp for determining which sprint a log belongs to.

    Uses started_at (when work happened) if available, falls back to at (when logged).
    """
    return log.get("started_at") or log.get("at", 0)


def bucket_logs_by_sprint(task: dict, sprints: list[dict]) -> dict:
    """Group task logs by sprint based on their effective date.

    Returns dict mapping sprint_id -> list of logs.
    Logs outside any sprint are mapped to None key.
    """
    from datetime import datetime
    buckets = {}
    for log in task.get("logs", []):
        ts = log_effective_date(log)
        dt = datetime.fromtimestamp(ts).date() if ts else None
        sprint = find_sprint_for_date(sprints, dt) if dt else None
        key = sprint["id"] if sprint else None
        buckets.setdefault(key, []).append(log)
    return buckets


def _sprint_start_sort_key(start_date):
    """Sort key for a sprint's start_date that tolerates None/unknown sprints.

    Unresolvable sprints sort first (they can never be "the most recent").
    """
    from datetime import date
    return start_date if start_date is not None else date.min


def _sprint_time_entries(task: dict, sprints: list[dict], include_zero: bool = False) -> list[dict]:
    """Per-sprint breakdown of a task's logged time, sorted by sprint start date.

    Shared implementation behind ``sprint_summary_for_task`` (legacy, keeps
    zero-minute sprints) and ``task_sprints_with_time`` (drops them).

    ``start_date`` in each entry is the sprint's ``start_date`` **date object**,
    which both ``get_all_sprints()`` and ``get_cached_sprints()`` provide — the
    old code read the camelCase ``startDate`` that only the former produces, so
    passing cached sprints sorted every entry by ``""`` (see plan §1.7).
    """
    sprints = sprints or []
    buckets = bucket_logs_by_sprint(task, sprints)
    sprint_map = {s["id"]: s for s in sprints}
    result = []
    for sprint_id, logs in buckets.items():
        if sprint_id is None:
            continue
        s = sprint_map.get(sprint_id, {})
        total_mins = sum(l.get("minutes", 0) for l in logs)
        if not include_zero and total_mins <= 0:
            continue
        result.append({
            "sprint_id": sprint_id,
            "sprint_title": s.get("title", "Unknown"),
            "field_id": s.get("field_id"),
            "start_date": s.get("start_date"),
            "logs": logs,
            "total_mins": total_mins,
        })
    result.sort(key=lambda x: _sprint_start_sort_key(x["start_date"]))
    return result


def sprint_summary_for_task(task: dict, sprints: list[dict]) -> list[dict]:
    """Get per-sprint breakdown of logged time for a task.

    Returns list of dicts sorted by sprint start date:
        [{sprint_id, sprint_title, field_id, start_date, logs, total_mins}, ...]
    Only includes sprints that have logged time (excludes None bucket).

    Legacy shim: delegates to ``_sprint_time_entries`` with ``include_zero=True``
    so its current callers (the split machinery, which relies on zero-minute
    "sprint rollover marker" logs creating a bucket) keep their behaviour while
    picking up the start_date sort fix. New code should use
    ``task_sprints_with_time``.
    """
    return _sprint_time_entries(task, sprints, include_zero=True)


def task_sprints_with_time(task: dict, sprints: list[dict]) -> list[dict]:
    """Sprints in which this task has logged time, oldest first.

    Replacement for ``sprint_summary_for_task``: same entry shape
    ``{sprint_id, sprint_title, field_id, start_date, logs, total_mins}`` but
    sorted by the real ``start_date`` date object and excluding sprints whose
    total is zero (e.g. the marker-log hack of plan §1.3).
    """
    return _sprint_time_entries(task, sprints, include_zero=False)


# --- Per-sprint issue bindings (plan §2.1/§2.2) --------------------------------
#
# A task carries ``sprint_issues``: one binding per sprint the work was billed
# to, replacing the "shadow task" duplicates. Every accessor below falls back to
# the legacy ``sprint``/``sprint_id``/``github_issue`` fields, so they are safe
# to call on un-migrated data.


def _legacy_binding_for_task(task: dict) -> dict | None:
    """Synthesize a binding from a task's legacy sprint/issue fields.

    Returns None when the task has neither a ``sprint_id`` nor a
    ``github_issue`` (nothing to bind).
    """
    sprint_id = task.get("sprint_id")
    issue = task.get("github_issue")
    if not sprint_id and not issue:
        return None
    return {
        "sprint_id": sprint_id,
        "sprint": task.get("sprint"),
        "issue": issue,
        "state": "closed" if task.get("status") == "done" else "open",
        "hours_synced": None,
        "synced_at": None,
        "created_at": task.get("created_at"),
    }


def task_sprint_bindings(task: dict, sprints: list[dict] = None) -> list[dict]:
    """Return the task's per-sprint issue bindings. Never None.

    Sorted by the binding's sprint ``start_date`` when *sprints* is supplied,
    otherwise left in insertion order. When the task has no ``sprint_issues``
    key at all (un-migrated data) a single binding is synthesized from the
    legacy fields — synthesized, not persisted. An explicitly empty list is
    respected as "this task has no bindings".
    """
    bindings = task.get("sprint_issues")
    if bindings is None:
        legacy = _legacy_binding_for_task(task)
        bindings = [legacy] if legacy else []
    elif not isinstance(bindings, list):
        return []
    if not sprints or not bindings:
        return list(bindings)
    start_by_id = {s["id"]: s.get("start_date") for s in sprints}
    return sorted(
        bindings,
        key=lambda b: _sprint_start_sort_key(start_by_id.get(b.get("sprint_id"))),
    )


def task_binding_for_sprint(task: dict, sprint_id: str) -> dict | None:
    """The task's binding for *sprint_id*, or None."""
    if not sprint_id:
        return None
    for b in task_sprint_bindings(task):
        if b.get("sprint_id") == sprint_id:
            return b
    return None


def task_current_issue(task: dict, data: dict = None) -> str | None:
    """The issue ref that "is" this task's current GitHub issue.

    Resolution order:
      1. The binding for the current sprint — resolved **offline** from
         ``get_cached_sprints(data)`` + today's date when *data* is given.
      2. The binding with the latest sprint ``start_date`` (needs the cache).
      3. The last binding in insertion order.
      4. The legacy ``task["github_issue"]``.

    Never makes a network call, so it is safe on every hot path.
    """
    bindings = task_sprint_bindings(task)
    candidates = []
    if bindings:
        sprints = get_cached_sprints(data) if data is not None else []
        if sprints:
            current = find_sprint_for_date(sprints, datetime.now().date())
            if current:
                b = next((x for x in bindings if x.get("sprint_id") == current["id"]), None)
                if b is not None:
                    candidates.append(b)
            candidates.append(task_sprint_bindings(task, sprints)[-1])
        candidates.append(bindings[-1])
    for b in candidates:
        if b.get("issue"):
            return b["issue"]
    return task.get("github_issue")


def current_binding(task: dict, data: dict = None) -> dict | None:
    """The binding object :func:`task_current_issue` reads from, or None.

    Same resolution order as ``task_current_issue`` (current-sprint binding →
    latest by sprint ``start_date`` → last in insertion order) but returns the
    live dict so a writer can update it in place. Offline; never a network call.
    Returns None when the task has no ``sprint_issues`` entries at all.
    """
    bindings = task.get("sprint_issues")
    if not isinstance(bindings, list) or not bindings:
        return None
    sprints = get_cached_sprints(data) if data is not None else []
    if sprints:
        current = find_sprint_for_date(sprints, datetime.now().date())
        if current:
            b = next((x for x in bindings if x.get("sprint_id") == current["id"]), None)
            if b is not None:
                return b
        # sorted() returns the same dict objects, so this is still a live binding.
        return task_sprint_bindings(task, sprints)[-1]
    return bindings[-1]


def task_issue_refs(task: dict) -> list[str]:
    """Every distinct issue ref the task is bound to, oldest binding first.

    Includes the legacy ``github_issue`` if it isn't already on a binding, so
    this is safe on un-migrated data.
    """
    out: list[str] = []
    for b in task_sprint_bindings(task):
        ref = b.get("issue")
        if ref and ref not in out:
            out.append(ref)
    legacy = task.get("github_issue")
    if legacy and legacy not in out:
        out.append(legacy)
    return out


def set_task_current_issue(task: dict, issue_ref: str, data: dict = None) -> dict:
    """Point the task's current binding at *issue_ref*. Returns that binding.

    Target binding, in order: the one for the task's own ``sprint_id``, else the
    one :func:`current_binding` resolves (only if it is unclaimed or already
    holds *issue_ref*), else a freshly appended binding for the task's sprint.

    The legacy ``task["github_issue"]`` key is written **as well**. That is
    deliberate for Phase 3: ``tracker.py`` and ``mcp_server.py`` (and an older
    wt.py on the other Mac reading the iCloud-synced file) still read the flat
    key directly, so dropping the mirror here would break them. Phase 3 stops
    *reading* it in wt.py; a later phase can stop writing it once every consumer
    goes through ``task_current_issue()``.
    """
    bindings = _ensure_bindings(task)
    own = task.get("sprint_id")
    binding = _find_binding(bindings, sprint_id=own) if own else None
    if binding is None:
        candidate = current_binding(task, data)
        if candidate is not None and candidate.get("issue") in (None, issue_ref):
            binding = candidate
    if binding is None:
        binding = {
            "sprint_id": own,
            "sprint": task.get("sprint"),
            "issue": None,
            "state": "closed" if task.get("status") == "done" else "open",
            "hours_synced": None,
            "synced_at": None,
            "created_at": time.time(),
        }
        bindings.append(binding)
    binding["issue"] = issue_ref
    task["github_issue"] = issue_ref  # legacy mirror — see docstring
    return binding


def clear_task_current_issue(task: dict, data: dict = None) -> str | None:
    """Unlink the task's current issue. Returns the ref that was removed.

    Clears it from whichever binding(s) hold it and drops the legacy
    ``github_issue`` key. The binding itself is kept (bindings are never
    deleted — plan §2.3 step 6), it just no longer names an issue.
    """
    old = task_current_issue(task, data)
    if old:
        for b in task.get("sprint_issues") or []:
            if b.get("issue") == old:
                b["issue"] = None
    task.pop("github_issue", None)
    return old


def task_start_sprint(task: dict, sprints: list[dict]) -> dict | None:
    """The sprint this task started in.

    Uses the frozen ``start_sprint_id`` when set, otherwise derives it from the
    task's earliest log (``log_effective_date``). Derive-only: this never writes
    the field — freezing happens in ``_migrate_shadows_to_bindings``.
    """
    sprints = sprints or []
    frozen = task.get("start_sprint_id")
    if frozen:
        return next((s for s in sprints if s["id"] == frozen), None)
    stamps = [ts for ts in (log_effective_date(l) for l in task.get("logs", [])) if ts]
    if not stamps:
        return None
    return find_sprint_for_date(sprints, datetime.fromtimestamp(min(stamps)).date())


def task_mins_for_sprint(task: dict, sprint_id: str, sprints: list[dict]) -> float:
    """Minutes logged by *task* inside *sprint_id*'s half-open date range.

    Unlike the legacy ``task_logged_mins_for_sprint`` there is **no** "sprint
    unknown → return the task total" fallback (which silently over-reports):
    an unresolvable sprint, or a task whose logs carry no usable timestamp,
    contributes 0.0 to that sprint.
    """
    if not sprint_id or not sprints:
        return 0.0
    sprint = next((s for s in sprints if s.get("id") == sprint_id), None)
    if not sprint or not sprint.get("start_date") or not sprint.get("end_date"):
        return 0.0
    total = 0.0
    for log in task.get("logs", []):
        ts = log_effective_date(log)
        if not ts:
            continue
        log_date = datetime.fromtimestamp(ts).date()
        if sprint["start_date"] <= log_date < sprint["end_date"]:
            total += log.get("minutes", 0)
    return total


def task_reportable_mins(task: dict, sprints: list[dict], sprint_id: str = None) -> float:
    """Minutes to report to GitHub for the task's current sprint context.

    Replacement for ``task_logged_mins_for_sprint`` on every GitHub-hours path.
    Resolution order for the sprint:

      1. *sprint_id* when given (e.g. the binding being synced).
      2. The sprint of the task's current binding (:func:`current_binding`).
      3. The task's legacy ``sprint_id``.

    Once a sprint is resolved the value is ``task_mins_for_sprint`` — no
    "unknown sprint → whole task total" fallback, which is what over-reported
    for cross-sprint tasks. The total is only used when **no** sprint can be
    resolved at all (no sprint list, or none of the above resolves), because in
    that case reporting 0.0 would silently *under*-report and is strictly worse
    than today's behaviour.
    """
    sprints = sprints or []
    if not sprints:
        return task_logged_mins(task)
    known = {s.get("id") for s in sprints}
    candidates = [sprint_id]
    binding = current_binding(task)
    if binding is not None:
        candidates.append(binding.get("sprint_id"))
    candidates.append(task.get("sprint_id"))
    for sid in candidates:
        if sid and sid in known:
            return task_mins_for_sprint(task, sid, sprints)
    return task_logged_mins(task)


def logs_in_date_range(
    data: dict,
    start_date,
    end_date,
    role_id: str | None = None,
) -> list[tuple[dict, dict]]:
    """Return [(task, log), ...] for every log whose effective date falls in
    the inclusive range ``[start_date, end_date]``.

    Sorted by effective timestamp ascending. Done tasks are included.

    Every task in ``data["tasks"]`` is now a distinct unit of work, so there is
    nothing to filter out: the old ``cross_sprint_parent`` shadow-skip is gone
    because ``load()`` guarantees shadows do not exist (they are converted into
    ``sprint_issues`` bindings by ``_migrate_shadows_to_bindings``, which also
    strips any an older wt.py re-introduces via iCloud).

    Args:
        data: The full data dict from ``load()``.
        start_date: ``datetime.date`` (inclusive lower bound).
        end_date:   ``datetime.date`` (inclusive upper bound).
        role_id: Optional role filter; when set, only logs whose owning
            task has this ``role_id`` are returned.
    """
    out: list[tuple[dict, dict]] = []
    for task in data.get("tasks", []):
        if role_id is not None and task.get("role_id") != role_id:
            continue
        for log in task.get("logs", []):
            ts = log_effective_date(log)
            if not ts:
                continue
            d = datetime.fromtimestamp(ts).date()
            if start_date <= d <= end_date:
                out.append((task, log))
    out.sort(key=lambda pair: log_effective_date(pair[1]))
    return out


def _parse_last_arg(s: str) -> int:
    """Parse a ``--last`` value. Accepts ``"7"`` or ``"7d"``; rejects others.

    Returns the integer day count. Raises ``ValueError`` with a clear message
    when the input is not recognised.
    """
    raw = (s or "").strip().lower()
    if not raw:
        raise ValueError("--last requires a value, e.g. '7' or '7d'")
    if raw.endswith("d"):
        raw = raw[:-1]
    if not raw.isdigit():
        raise ValueError(f"--last expected an integer (optionally suffixed 'd'), got '{s}'")
    days = int(raw)
    if days <= 0:
        raise ValueError(f"--last must be a positive integer, got '{s}'")
    return days


def build_time_report(
    data: dict,
    start_date,
    end_date,
    sprint: dict | None = None,
    role_id: str | None = None,
) -> dict:
    """Build the structured time-report payload (the JSON shape).

    Pure function: doesn't print or load data. Used by both the CLI ``--json``
    path and the MCP ``report_time_range`` tool so they emit byte-identical
    documents.
    """
    roles = get_roles(data)
    pairs = logs_in_date_range(data, start_date, end_date, role_id=role_id)

    total_minutes = 0.0
    task_ids: set[str] = set()
    by_role_minutes: dict[str, float] = {}
    log_entries = []

    for task, log in pairs:
        mins = float(log.get("minutes", 0) or 0)
        total_minutes += mins
        task_ids.add(task.get("id", ""))
        rid = task.get("role_id", "other")
        by_role_minutes[rid] = by_role_minutes.get(rid, 0.0) + mins

        ts = log_effective_date(log)
        eff_date = datetime.fromtimestamp(ts).date().isoformat() if ts else None

        log_entries.append({
            "log_id": log.get("id"),
            "task_id": task.get("id"),
            "task_title": task.get("title", ""),
            "role_id": rid,
            "role_label": roles.get(rid, rid),
            "minutes": mins,
            "note": log.get("note", ""),
            "at": log.get("at"),
            "started_at": log.get("started_at"),
            "ended_at": log.get("ended_at"),
            "effective_date": eff_date,
            "calendar_event_uid": log.get("calendar_event_uid"),
            "uploaded": bool(log.get("uploaded_at")),
        })

    # Sort role breakdown by minutes desc, then by label for stable output.
    by_role = sorted(
        (
            {
                "role_id": rid,
                "role_label": roles.get(rid, rid),
                "minutes": mins,
            }
            for rid, mins in by_role_minutes.items()
        ),
        key=lambda r: (-r["minutes"], r["role_label"]),
    )

    filters = {"role": role_id} if role_id else None

    return {
        "range": {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "sprint": sprint.get("title") if sprint else None,
        },
        "filters": filters,
        "totals": {
            "total_minutes": total_minutes,
            "task_count": len(task_ids),
            "log_count": len(log_entries),
            "by_role": by_role,
        },
        "logs": log_entries,
    }


def format_time_report(payload: dict, use_color: bool = True) -> str:
    """Render a :func:`build_time_report` payload as human-readable text.

    When ``use_color`` is False, ANSI sequences are stripped so the output is
    safe for piping or for the MCP tool's plain-text mode.
    """
    def _c(text, *codes):
        return c(text, *codes) if use_color else str(text)

    rng = payload["range"]
    totals = payload["totals"]
    filters = payload.get("filters") or {}

    sprint_label = f"  ({rng['sprint']})" if rng.get("sprint") else ""
    role_label = ""
    if filters.get("role"):
        role_label = f"  [role: {filters['role']}]"

    lines = []
    lines.append("")
    lines.append(_c(
        f"  Time report: {rng['start_date']} → {rng['end_date']}{sprint_label}{role_label}",
        "bold",
    ))
    lines.append(_c(
        f"  Total: {fmt_mins(totals['total_minutes'])} across "
        f"{totals['task_count']} tasks, {totals['log_count']} log entries",
        "dim",
    ))

    if not payload["logs"]:
        lines.append("")
        lines.append(_c("  No logs in range.", "yellow"))
        lines.append("")
        return "\n".join(lines)

    # Role breakdown — mirrors cmd_status bar layout.
    total = totals["total_minutes"] or 0
    lines.append("")
    lines.append(_c("  by role:", "bold"))
    for r in totals["by_role"]:
        mins = r["minutes"]
        pct = round(mins / total * 100) if total else 0
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        lines.append(f"    {r['role_label']:<25} {bar} {pct:>3}%  {fmt_mins(mins)}")

    lines.append("")
    lines.append(_c("  Logs (sorted by start time):", "bold"))
    for entry in payload["logs"]:
        eff = entry.get("effective_date") or "—"
        started = entry.get("started_at")
        ended = entry.get("ended_at")
        if started and ended:
            time_range = f"{datetime.fromtimestamp(started).strftime('%H:%M')}-{datetime.fromtimestamp(ended).strftime('%H:%M')}"
        else:
            time_range = "—"
        dur = fmt_mins(entry["minutes"])
        role_lbl = entry.get("role_label") or entry.get("role_id") or "?"
        title = (entry.get("task_title") or "")[:35]
        note = (entry.get("note") or "")[:40]
        lines.append(
            f"    {eff}  {time_range:>11}  [{dur:>7}]  {role_lbl:<22}  {title:<37}  {note}"
        )

    lines.append("")
    return "\n".join(lines)


def _match_sprint(sprints: list[dict], query: str) -> dict | None:
    """Fuzzy match a sprint by title. Returns the sprint dict or None."""
    q = query.lower().strip()
    # Exact match first
    for s in sprints:
        if s["title"].lower() == q:
            return s
    # Partial match
    matches = [s for s in sprints if q in s["title"].lower()]
    if len(matches) == 1:
        return matches[0]
    return None


def _find_binding(bindings: list[dict], issue: str = None, sprint_id: str = None) -> dict | None:
    """Locate a binding by issue ref first, then by sprint_id. None if absent.

    Issue-ref lookup wins because a reconcile can re-point a binding's sprint,
    but an issue ref, once minted, never moves to another binding.
    """
    if issue:
        for b in bindings:
            if b.get("issue") == issue:
                return b
    if sprint_id:
        for b in bindings:
            if b.get("sprint_id") == sprint_id:
                return b
    return None


def _reconcile_plan(task: dict, data: dict, sprints: list[dict], *,
                    create_issues: bool = True, close_past: bool = True,
                    sync_hours: bool = True, closing: bool = False) -> dict:
    """Pure planner behind :func:`reconcile_task_sprints` (plan §2.3).

    Reads ``task``/``data``/``sprints`` and returns the ordered list of
    operations that would bring the task's ``sprint_issues`` bindings in line
    with what its logs say. **Mutates nothing and makes no GitHub call**, which
    is what makes ``dry_run=True`` structurally airtight: the caller either
    executes this plan or returns it untouched.

    Returned dict:
      ``error``              — fatal planning problem (no sprints), else None
      ``current_sprint`` / ``current_sprint_id``
      ``target``             — [{sprint_id, sprint, minutes, hours}], oldest first
      ``ops``                — ordered ops, each a self-describing dict with an
                               ``op`` of create/repoint/hours/close
      ``skipped``            — no-ops worth reporting, each with a ``reason``
      ``unassigned_minutes`` — minutes whose log timestamps fall in no sprint
      ``seed_legacy``        — the task's legacy ``github_issue`` is not yet
                               represented by a binding and must be seeded
    """
    plan = {
        "error": None,
        "current_sprint": None,
        "current_sprint_id": None,
        "target": [],
        "ops": [],
        "skipped": [],
        "unassigned_minutes": 0.0,
        "unbillable": [],
        "seed_legacy": False,
    }

    sprints = sprints or []
    if not sprints:
        plan["error"] = "No sprints found"
        return plan

    by_id = {s["id"]: s for s in sprints if s.get("id")}
    today = datetime.now().date()
    repo = get_task_repo(task)
    has_project = bool(data.get("config", {}).get("github_project_number"))

    def sort_key(sprint_id):
        s = by_id.get(sprint_id)
        return _sprint_start_sort_key(s.get("start_date") if s else None)

    def ended(sprint_id):
        s = by_id.get(sprint_id)
        return bool(s and s.get("end_date") and s["end_date"] <= today)

    # 1. Bucket logs by sprint; sprints with a zero total are not targets. Logs
    #    that land outside every sprint are surfaced, never silently attributed.
    plan["unassigned_minutes"] = sum(
        l.get("minutes", 0) for l in bucket_logs_by_sprint(task, sprints).get(None, [])
    )
    targets = {e["sprint_id"]: e["total_mins"] for e in task_sprints_with_time(task, sprints)}

    # 2. …plus the current sprint while the task is still open. This is what
    #    makes the marker-log ritual of plan §1.3 unnecessary: an open task
    #    always has a binding for new work to land on, even at 0 minutes.
    #
    #    ``closing=True`` suppresses that: a task being closed has no future work
    #    to land, so reserving an empty current-sprint binding would carry its
    #    long-lived issue onto a sprint it was never worked in and report 0h
    #    there. With the reservation gone, ``latest`` below is the newest sprint
    #    that actually *has* time, which is what the close reports against.
    current = find_sprint_for_date(sprints, today)
    if current:
        plan["current_sprint"] = current["title"]
        plan["current_sprint_id"] = current["id"]
        if task.get("status") != "done" and not closing:
            targets.setdefault(current["id"], 0.0)

    target_ids = sorted((sid for sid in targets if sid in by_id), key=sort_key)
    plan["target"] = [
        {
            "sprint_id": sid,
            "sprint": by_id[sid]["title"],
            "minutes": targets[sid],
            "hours": mins_to_quarter_hours(targets[sid]) if targets[sid] > 0 else 0.0,
        }
        for sid in target_ids
    ]

    # Working copy of the existing bindings — planning never touches the real ones.
    persisted = task.get("sprint_issues")
    if isinstance(persisted, list):
        # NB: carry superseded_issues through. The planner works on a copy so it
        # never mutates the real bindings, but omitting a field here silently
        # disables anything that depends on it — that is exactly how the
        # supersede op became unplannable after Phase 5 introduced it.
        work = [
            {"sprint_id": b.get("sprint_id"), "issue": b.get("issue"),
             "state": b.get("state"), "hours_synced": b.get("hours_synced"),
             "superseded_issues": list(b.get("superseded_issues") or [])}
            for b in persisted
        ]
        legacy_issue = task.get("github_issue")
        if legacy_issue and not any(w["issue"] == legacy_issue for w in work):
            # e.g. close_task() just minted the task's first issue and set only
            # the legacy field. Adopt it rather than minting a second one.
            seed = _legacy_binding_for_task(task)
            work.append({"sprint_id": seed["sprint_id"], "issue": seed["issue"],
                         "state": seed["state"], "hours_synced": None})
            plan["seed_legacy"] = True
    else:
        work = []
        seed = _legacy_binding_for_task(task)
        if seed:
            work.append({"sprint_id": seed["sprint_id"], "issue": seed["issue"],
                         "state": seed["state"], "hours_synced": None})
            plan["seed_legacy"] = True

    bound = {w["sprint_id"] for w in work if w["sprint_id"] in by_id}

    # Option A (plan §2.4): the task's *original* issue is the long-lived
    # "current" one and moves forward across sprint boundaries; brand-new issues
    # are minted for the sprints left behind. The carry-forward binding is the
    # one holding that issue: the binding for the task's own sprint_id, else an
    # unanchored binding (sprint unknown — a pre-sprint-tracking task).
    #
    # A ``recurrent`` series is the exception and gets **no** carry-forward. It
    # never ends, so there is no single long-lived issue: every sprint is its own
    # reporting unit, keeps its own issue permanently, and that issue closes when
    # the sprint does. Carrying forward would move the sprint-just-ended's issue
    # onto the new sprint and strand the hours it actually carries — e.g. Sprint
    # 104's #6207 (2h 54m) re-pointed to Sprint 105, leaving 104 unreported. This
    # is precisely the one-issue-per-sprint shape `wt new-recurrent` used to
    # produce by hand.
    recurring = task.get("status") == "recurrent"
    own = None if recurring else task.get("sprint_id")
    carry = None
    if not recurring:
        if own:
            carry = next((w for w in work if w["sprint_id"] == own and w["issue"]), None)
        if carry is None:
            carry = next((w for w in work
                          if w["issue"] and w["sprint_id"] not in by_id), None)
        if carry is None:
            # `wt set-sprint` moves sprint_id without touching bindings, so the two
            # can disagree. Fall back to the newest still-open issue — same rule as
            # task_current_issue(). Without this we would mint a *second* issue for
            # a sprint the task's live issue should simply have moved to.
            still_open = sorted(
                (w for w in work
                 if w["issue"] and w["state"] != "closed" and w["sprint_id"] in by_id),
                key=lambda w: by_id[w["sprint_id"]]["start_date"],
            )
            carry = still_open[-1] if still_open else None
    main_issue = carry["issue"] if carry else task.get("github_issue")

    latest = target_ids[-1] if target_ids else None
    if latest and latest not in bound and carry is not None:
        carry_start = (by_id[carry["sprint_id"]]["start_date"]
                       if carry["sprint_id"] in by_id else None)
        # Never move an issue backwards in time.
        if carry_start is None or carry_start < by_id[latest]["start_date"]:
            plan["ops"].append({
                "op": "repoint",
                "sprint_id": latest,
                "sprint": by_id[latest]["title"],
                "from_sprint_id": carry["sprint_id"],
                "from_sprint": (by_id[carry["sprint_id"]]["title"]
                                if carry["sprint_id"] in by_id else None),
                "issue": carry["issue"],
                "minutes": targets[latest],
                "hours": mins_to_quarter_hours(targets[latest]) if targets[latest] > 0 else 0.0,
                "reason": "carry the task's current issue forward (Option A)",
            })
            bound.discard(carry["sprint_id"])
            bound.add(latest)
            carry["sprint_id"] = latest

    # 3. Target sprints with no binding get one (plus a GH issue when possible).
    has_issue_anywhere = any(w["issue"] for w in work)
    missing = [sid for sid in target_ids if sid not in bound]
    created_plan = []
    for sid in missing:
        sprint_title = by_id[sid]["title"]
        minutes = targets[sid]
        hours = mins_to_quarter_hours(minutes) if minutes > 0 else 0.0
        will_close = bool(close_past and ended(sid))
        if repo and not create_issues:
            # Don't bind the sprint at all: an issue-less binding would count as
            # "already bound" on the next pass, so a later run with
            # create_issues=True could never mint the issue this sprint needs.
            # Report it instead, so `wt sync-sprints --all` (which defaults to
            # create_issues=False) can tell the user exactly what it skipped.
            plan["skipped"].append({
                "sprint": sprint_title, "sprint_id": sid, "issue": None,
                "minutes": minutes, "hours": hours,
                "needs_issue": True, "repo": repo,
                "reason": "would need a new issue (create_issues=False)",
            })
            continue
        # A past-sprint issue keeps today's " (Sprint N)" title suffix. Only a
        # task that has no issue at all gets a plain-titled one, and only for
        # its most recent sprint — matching create_github_issue()'s use in
        # close_task()/wt add.
        plain = (not has_issue_anywhere) and sid == latest
        op = {
            "op": "create",
            "sprint_id": sid,
            "sprint": sprint_title,
            "minutes": minutes,
            "hours": hours,
            "issue": None,
            "create_issue": bool(repo and create_issues),
            "issue_title": task.get("title") if plain else f"{task.get('title')} ({sprint_title})",
            "repo": repo,
            "will_close": will_close,
            "main_issue": main_issue,
            "reason": ("sprint has logged time" if minutes > 0
                       else "open task needs a binding for the current sprint"),
        }
        if not repo:
            op["skipped_github"] = "task has no github_repo"
        elif not create_issues:
            op["skipped_github"] = "create_issues=False"
        plan["ops"].append(op)
        created_plan.append(op)

    # Post-plan binding set: existing (possibly re-pointed) plus the new ones.
    final = [
        {"sprint_id": w["sprint_id"], "issue": w["issue"], "state": w["state"],
         "hours_synced": w["hours_synced"],
         "superseded_issues": list(w.get("superseded_issues") or []),
         "new": False}
        for w in work
    ]
    for op in created_plan:
        # A created binding gets its hours pushed as part of creation, so it
        # never needs a separate hours op.
        final.append({"sprint_id": op["sprint_id"], "issue": None, "state": "open",
                      "hours_synced": op["hours"], "new": True})

    for sid in target_ids:
        if sid in bound and sid != latest:
            entry = next((f for f in final if f["sprint_id"] == sid), None)
            plan["skipped"].append({
                "sprint": by_id[sid]["title"], "sprint_id": sid,
                "minutes": targets[sid], "issue": entry and entry.get("issue"),
                "reason": "already bound",
            })

    # 4a. Safety guard: never narrow an issue's Hours while some of this task's
    # logged time has nowhere to be reported.
    #
    # Reconcile's job is to make each issue carry *its own* sprint's hours. That
    # is only conservative when every other sprint's hours land on an issue of
    # their own. If a sprint with time ends up with no issue — because
    # create_issues=False deferred it, or because its binding was never linked —
    # then narrowing the *other* issues silently deletes the difference from the
    # project's reporting. Observed on real data: `Assist on Banco Galicia`
    # (Sprint 95 12.5h + Sprint 96 6.5h) would have gone from 19.0h on one issue
    # to 6.5h, with Sprint 95's 12.5h reported nowhere.
    #
    # The test is structural, so it needs no network call: if any sprint of this
    # task has minutes but no issue to put them on, withhold *all* of the task's
    # hours writes and say why. Passing --create-issues binds those sprints and
    # the guard clears itself.
    # Sprints getting a freshly-minted issue *in this same plan* are billable:
    # the create op carries their hours, so nothing is lost.
    will_mint = {op["sprint_id"] for op in plan["ops"]
                 if op["op"] == "create" and op.get("create_issue")}
    unbillable = []
    for sid, mins in targets.items():
        if mins <= 0 or sid not in by_id or sid in will_mint:
            continue
        entry = next((f for f in final if f["sprint_id"] == sid), None)
        if entry is None or not entry.get("issue"):
            unbillable.append({"sprint": by_id[sid]["title"], "sprint_id": sid,
                               "minutes": mins,
                               "hours": mins_to_quarter_hours(mins)})
    unbillable.sort(key=lambda e: sort_key(e["sprint_id"]))
    plan["unbillable"] = unbillable

    # 4. Hours: push only when the value differs from what we last told GitHub.
    for f in final:
        sid = f["sprint_id"]
        label = by_id[sid]["title"] if sid in by_id else (sid or "unknown sprint")
        if sid not in by_id:
            plan["skipped"].append({
                "sprint": None, "sprint_id": sid, "issue": f["issue"],
                "reason": "binding's sprint is not in the sprint list",
            })
            continue
        if f["new"]:
            continue
        minutes = task_mins_for_sprint(task, sid, sprints)
        hours = mins_to_quarter_hours(minutes) if minutes > 0 else 0.0
        common = {"sprint": label, "sprint_id": sid, "issue": f["issue"],
                  "minutes": minutes, "hours": hours,
                  "from_hours": f["hours_synced"]}
        if not f["issue"]:
            plan["skipped"].append({**common, "reason": "binding has no issue"})
        elif unbillable:
            where = ", ".join(f"{e['sprint']} {fmt_mins(e['minutes'])}"
                              for e in unbillable)
            plan["skipped"].append({
                **common, "withheld_hours": True,
                "reason": ("hours withheld — unreported time in " + where +
                           "; re-run with --create-issues so it lands on its "
                           "own issue"),
            })
        elif not sync_hours:
            plan["skipped"].append({**common, "reason": "sync_hours=False"})
        elif not has_project:
            plan["skipped"].append({**common, "reason": "no github project configured"})
        elif hours <= 0:
            # Matches today's `if sprint_mins > 0` guard everywhere else: we
            # never push a 0 to the project's Hours field.
            plan["skipped"].append({**common, "reason": "no logged minutes"})
        elif hours == f["hours_synced"]:
            plan["skipped"].append({**common, "reason": "hours already synced"})
        else:
            plan["ops"].append({
                "op": "hours", **common,
                "reason": ("hours_synced unknown" if f["hours_synced"] is None
                           else f"hours changed {f['hours_synced']} -> {hours}"),
            })

    # 4c. Superseded issues: a sprint that ended up with two issues (see
    # _merge_binding) has one primary carrying the sprint's full hours. The others
    # must be zeroed and closed or the project double-counts that sprint.
    for f in final:
        for ref in list(f.get("superseded_issues") or []):
            if not has_project:
                plan["skipped"].append({
                    "sprint": by_id.get(f["sprint_id"], {}).get("title"),
                    "sprint_id": f["sprint_id"], "issue": ref,
                    "reason": "superseded, but no github project configured",
                })
                continue
            plan["ops"].append({
                "op": "supersede",
                "sprint_id": f["sprint_id"],
                "sprint": by_id.get(f["sprint_id"], {}).get("title"),
                "issue": ref,
                "primary": f.get("issue"),
                "hours": 0.0,
                "reason": ("its sprint's hours now live on the primary issue; "
                           "zero and close so the project doesn't double-count"),
            })

    # 5. Bindings whose sprint has ended are final: Status=Done + close.
    for f in final:
        sid = f["sprint_id"]
        if sid not in by_id or not ended(sid):
            continue
        if f["state"] == "closed":
            continue
        if not close_past:
            plan["skipped"].append({
                "sprint": by_id[sid]["title"], "sprint_id": sid, "issue": f["issue"],
                "reason": "close_past=False",
            })
            continue
        plan["ops"].append({
            "op": "close",
            "sprint_id": sid,
            "sprint": by_id[sid]["title"],
            "issue": f["issue"],
            "reason": "sprint has ended",
        })

    # Local-only: keep the legacy sprint/sprint_id pointing at whichever sprint
    # the task's carried-forward "current" issue ends up bound to. wt.py no longer
    # reads them for hours (task_reportable_mins goes through the bindings), but
    # the TUI board, mcp_server.py and an older wt.py on the other Mac still do,
    # so they stay in sync. Listed as an op so an empty plan really means
    # "nothing to do".
    anchor = carry["sprint_id"] if (carry and carry["sprint_id"] in by_id) else latest
    if anchor and task.get("sprint_id") != anchor:
        own_start = by_id[own]["start_date"] if own in by_id else None
        if own_start is not None and own_start > by_id[anchor]["start_date"]:
            # Never walk the pointer backwards in time.
            plan["skipped"].append({
                "sprint": by_id[anchor]["title"], "sprint_id": anchor,
                "reason": "would move the task's sprint pointer backwards",
            })
        else:
            plan["ops"].append({
                "op": "relabel",
                "sprint_id": anchor,
                "sprint": by_id[anchor]["title"],
                "from_sprint_id": own,
                "from_sprint": task.get("sprint"),
                "reason": "legacy sprint/sprint_id follows the current issue's binding",
            })

    return plan


def reconcile_task_sprints(task: dict, data: dict, sprints: list[dict], *,
                           create_issues: bool = True, close_past: bool = True,
                           sync_hours: bool = True, dry_run: bool = False,
                           closing: bool = False,
                           save_callback=None, progress_callback=None) -> dict:
    """Bring a task's per-sprint issue bindings in line with its logs.

    Implements plan §2.3 as a diff between the target state derived from
    ``logs`` + ``sprints`` and the task's existing ``sprint_issues``:

      1. Bucket logs by sprint; sprints with 0 minutes are not targets.
      2. Target set = {sprints with time} ∪ {current sprint, if the task is
         open and ``closing`` is False}. The second term replaces the marker-log
         ritual (plan §1.3); ``closing=True`` drops it so a task being closed
         reports against the newest sprint that actually has time instead of an
         empty current sprint.
      3. Target sprints with no binding get one, plus a GitHub issue when the
         task has a ``github_repo``. With ``create_issues=False`` a sprint that
         *would* need a new issue is reported in ``skipped`` (flagged
         ``needs_issue``) and left unbound, rather than getting an issue-less
         binding that a later ``create_issues=True`` run would mistake for
         "already handled".
      4. Every binding's hours are recomputed from the logs and pushed only
         when they differ from the cached ``hours_synced``.
      5. Bindings whose sprint has ended get Status=Done + a closed issue.
      6. Bindings are never deleted and ``logs`` is never touched.

    Which issue is "current" follows Option A of plan §2.4: the task's original
    issue is carried forward to its most recent sprint and newly-minted issues
    are for the sprints left behind (titled ``Task (Sprint N)``), which is
    exactly what ``split_cross_sprint_task`` did on GitHub.

    Idempotent: a second run plans nothing and calls nothing.

    ``dry_run=True`` performs **zero** writes — no GitHub call, no mutation of
    *task*/*data*, no ``save_callback`` — and returns the same result shape
    describing what would happen. The plan is computed by the pure
    :func:`_reconcile_plan` and only then executed, so this is structural
    rather than a per-call-site check.

    Args:
        sprints: sprint list (``get_all_sprints`` or ``get_cached_sprints``).
        create_issues: mint GitHub issues for new bindings.
        close_past: close issues whose sprint has ended.
        sync_hours: push recomputed hours to the project.
        dry_run: plan only.
        closing: the task is being closed, so don't reserve an empty
            current-sprint binding for future work. Set by :func:`close_task`.
        save_callback: called after each created binding and once at the end.
        progress_callback: ``f(msg)`` progress updates.

    Returns:
        {success, error, dry_run, task, task_id, current_sprint, target,
         planned, created, repointed, hours_updated, closed, relabeled,
         skipped, errors, unassigned_minutes, bindings}

        ``success`` is False if any per-sprint operation errored; the remaining
        sprints are still processed and their results persisted.
    """
    def progress(msg):
        if progress_callback:
            progress_callback(msg)

    plan = _reconcile_plan(task, data, sprints, create_issues=create_issues,
                           close_past=close_past, sync_hours=sync_hours,
                           closing=closing)

    result = {
        "success": plan["error"] is None,
        "error": plan["error"],
        "dry_run": bool(dry_run),
        "task": task.get("title"),
        "task_id": task.get("id"),
        "current_sprint": plan["current_sprint"],
        "target": plan["target"],
        "planned": plan["ops"],
        "created": [],
        "repointed": [],
        "hours_updated": [],
        "closed": [],
        "relabeled": None,
        "skipped": list(plan["skipped"]),
        "errors": [],
        "unassigned_minutes": plan["unassigned_minutes"],
        "unbillable": plan["unbillable"],
        "superseded": [],
        "bindings": [],
    }

    if plan["error"] or dry_run or not plan["ops"]:
        result["bindings"] = [dict(b) for b in task_sprint_bindings(task, sprints)]
        return result

    # ---- execute -------------------------------------------------------------
    by_id = {s["id"]: s for s in (sprints or []) if s.get("id")}
    has_project = bool(data.get("config", {}).get("github_project_number"))
    needs_project = any(
        op["op"] in ("hours", "repoint", "close") or op.get("create_issue")
        for op in plan["ops"]
    )
    pi = None
    if has_project and needs_project:
        progress("Fetching project info...")
        try:
            pi = get_project_info(data)
        except Exception:
            pi = None

    bindings = task.get("sprint_issues")
    if not isinstance(bindings, list):
        bindings = []
        task["sprint_issues"] = bindings
    if plan["seed_legacy"]:
        seed = _legacy_binding_for_task(task)
        if seed:
            _merge_binding(bindings, seed)

    activity = get_task_activity(task)
    type_val = get_task_type(task)
    did_work = False
    failed_sprints = set()

    for op in plan["ops"]:
        kind = op["op"]
        sprint = by_id.get(op["sprint_id"])
        label = op.get("sprint") or op["sprint_id"]
        if op["sprint_id"] in failed_sprints:
            # An earlier op for this sprint failed (e.g. issue creation), so the
            # follow-ups have nothing to act on. One error per sprint, not three.
            result["skipped"].append({
                **op, "reason": "aborted: an earlier operation for this sprint failed",
            })
            continue
        try:
            if kind == "create":
                binding = {
                    "sprint_id": op["sprint_id"],
                    "sprint": op["sprint"],
                    "issue": None,
                    "state": "open",
                    "hours_synced": None,
                    "synced_at": None,
                    "created_at": time.time(),
                }
                if op["create_issue"]:
                    progress(f"  {label}: Creating issue...")
                    issue_ref = create_github_issue(
                        {"id": uid(), "title": op["issue_title"]}, op["repo"]
                    )
                    binding["issue"] = issue_ref
                    if pi:
                        progress(f"  {label}: Adding to project...")
                        item_id = add_issue_to_project(issue_ref, data)
                        progress(f"  {label}: Setting fields...")
                        if not op["will_close"]:
                            # A close op below sets Status=Done; don't push twice.
                            sync_project_status(issue_ref, task.get("status", "todo"),
                                                data, project_info=pi, item_id=item_id)
                        if op["hours"] > 0 and sync_hours:
                            if update_project_hours(issue_ref, op["hours"], data,
                                                    project_info=pi, item_id=item_id):
                                binding["hours_synced"] = op["hours"]
                                binding["synced_at"] = time.time()
                        if sprint and sprint.get("field_id"):
                            update_project_sprint(issue_ref, op["sprint_id"],
                                                  sprint["field_id"], data,
                                                  project_info=pi, item_id=item_id)
                        if activity:
                            update_project_activity(issue_ref, activity, data,
                                                    project_info=pi, item_id=item_id)
                        if type_val:
                            update_project_type(issue_ref, type_val, data,
                                                project_info=pi, item_id=item_id)
                    if op.get("main_issue"):
                        progress(f"  {label}: Adding comment...")
                        add_issue_comment(
                            binding["issue"],
                            f"Sprint split from {op['main_issue']}. "
                            "See that issue for full details and notes.",
                        )
                _merge_binding(bindings, binding)
                if binding["issue"] and not task.get("github_issue"):
                    task["github_issue"] = binding["issue"]
                did_work = True
                result["created"].append({
                    "sprint": op["sprint"], "sprint_id": op["sprint_id"],
                    "issue": binding["issue"], "minutes": op["minutes"],
                    "hours": op["hours"],
                    "skipped_github": op.get("skipped_github"),
                })
                if save_callback:
                    save_callback(data)

            elif kind == "repoint":
                binding = _find_binding(bindings, issue=op["issue"],
                                        sprint_id=op["from_sprint_id"])
                if binding is None:
                    raise Exception("binding to carry forward has disappeared")
                binding["sprint_id"] = op["sprint_id"]
                binding["sprint"] = op["sprint"]
                if binding.get("issue") and pi and sprint and sprint.get("field_id"):
                    progress(f"  {label}: Moving issue {binding['issue']} forward...")
                    item_id = add_issue_to_project(binding["issue"], data)
                    update_project_sprint(binding["issue"], op["sprint_id"],
                                          sprint["field_id"], data,
                                          project_info=pi, item_id=item_id)
                did_work = True
                result["repointed"].append({
                    "sprint": op["sprint"], "sprint_id": op["sprint_id"],
                    "from_sprint": op.get("from_sprint"), "issue": op["issue"],
                })

            elif kind == "hours":
                binding = _find_binding(bindings, issue=op["issue"],
                                        sprint_id=op["sprint_id"])
                if binding is None:
                    raise Exception("binding to sync hours for has disappeared")
                progress(f"  {label}: Setting hours to {op['hours']}...")
                item_id = add_issue_to_project(op["issue"], data)
                if not update_project_hours(op["issue"], op["hours"], data,
                                            project_info=pi, item_id=item_id):
                    raise Exception(f"failed to set hours to {op['hours']}")
                binding["hours_synced"] = op["hours"]
                binding["synced_at"] = time.time()
                did_work = True
                result["hours_updated"].append({
                    "sprint": op["sprint"], "sprint_id": op["sprint_id"],
                    "issue": op["issue"], "minutes": op["minutes"],
                    "hours": op["hours"], "from_hours": op.get("from_hours"),
                })

            elif kind == "close":
                binding = _find_binding(bindings, issue=op["issue"],
                                        sprint_id=op["sprint_id"])
                if binding is None:
                    raise Exception("binding to close has disappeared")
                issue_ref = binding.get("issue")
                if issue_ref:
                    if pi:
                        progress(f"  {label}: Setting Status=Done...")
                        item_id = add_issue_to_project(issue_ref, data)
                        sync_project_status(issue_ref, "done", data,
                                            project_info=pi, item_id=item_id)
                    progress(f"  {label}: Closing issue...")
                    if not close_github_issue(issue_ref):
                        raise Exception(f"failed to close issue {issue_ref}")
                binding["state"] = "closed"
                did_work = True
                result["closed"].append({
                    "sprint": op["sprint"], "sprint_id": op["sprint_id"],
                    "issue": issue_ref,
                })

            elif kind == "supersede":
                # A second issue for a sprint whose hours now live on the primary
                # binding. Zero it and close it, or the project double-counts.
                binding = _find_binding(bindings, sprint_id=op["sprint_id"])
                if binding is None:
                    raise Exception("binding for the superseded issue has disappeared")
                progress(f"  {label}: Zeroing superseded {op['issue']}...")
                item_id = add_issue_to_project(op["issue"], data)
                if not update_project_hours(op["issue"], 0.0, data,
                                            project_info=pi, item_id=item_id):
                    raise Exception(f"failed to zero superseded issue {op['issue']}")
                if pi:
                    sync_project_status(op["issue"], "done", data,
                                        project_info=pi, item_id=item_id)
                progress(f"  {label}: Closing superseded {op['issue']}...")
                if not close_github_issue(op["issue"]):
                    raise Exception(f"failed to close superseded issue {op['issue']}")
                binding["superseded_issues"] = [
                    r for r in (binding.get("superseded_issues") or [])
                    if r != op["issue"]
                ]
                if not binding["superseded_issues"]:
                    binding.pop("superseded_issues", None)
                did_work = True
                result.setdefault("superseded", []).append({
                    "sprint": op["sprint"], "sprint_id": op["sprint_id"],
                    "issue": op["issue"], "primary": op.get("primary"),
                })

            elif kind == "relabel":
                task["sprint_id"] = op["sprint_id"]
                task["sprint"] = op["sprint"]
                did_work = True
                result["relabeled"] = {
                    "sprint": op["sprint"], "sprint_id": op["sprint_id"],
                    "from_sprint": op.get("from_sprint"),
                }
        except Exception as e:
            result["errors"].append({**op, "error": str(e)})
            result["success"] = False
            failed_sprints.add(op["sprint_id"])

    if did_work:
        _dedupe_bindings(task)
        _sort_task_bindings(task, sprints)
        mark_logs_uploaded(task)
        if save_callback:
            save_callback(data)

    result["bindings"] = [dict(b) for b in task_sprint_bindings(task, sprints)]
    return result


def split_cross_sprint_task(task: dict, data: dict, save_callback,
                            all_sprints: list[dict] = None,
                            progress_callback=None) -> dict:
    """**Deprecated** — thin wrapper over :func:`reconcile_task_sprints`.

    Kept so ``tracker.py`` and ``mcp_server.py`` keep importing cleanly while
    they are migrated to call reconcile directly. ``wt.py`` no longer calls it:
    ``close_task`` reconciles directly and the CLI entry point is now
    ``wt sync-sprints`` (:func:`cmd_sync_sprints`). The return-dict keys
    ``success`` / ``sprint_tasks_created`` / ``main_sprint`` / ``error`` and the
    "only has time in one sprint" gate are preserved.

    What changed underneath: previous sprints are recorded as ``sprint_issues``
    bindings on the task instead of duplicate "shadow" task objects. The GitHub
    side is unchanged — one issue per sprint, past ones titled
    ``Task (Sprint N)``, closed, carrying that sprint's hours.

    Phase-3 gate change: the "one sprint" test now uses
    :func:`task_sprints_with_time` (sprints with **>0** minutes) instead of
    ``sprint_summary_for_task`` (which counted zero-minute sprints). A task whose
    only second sprint is a 0-minute "rollover marker" log therefore now reports
    "only has time in one sprint" rather than performing a no-op split.
    """
    def progress(msg):
        if progress_callback:
            progress_callback(msg)

    result = {
        "success": False,
        "sprint_tasks_created": [],
        "main_sprint": None,
        "error": None,
    }

    if all_sprints is None:
        progress("Fetching sprints...")
        all_sprints = get_all_sprints(data)
    if not all_sprints:
        result["error"] = "No sprints found"
        return result

    if len(task_sprints_with_time(task, all_sprints)) <= 1:
        result["error"] = "Task only has time in one sprint"
        return result

    rec = reconcile_task_sprints(task, data, all_sprints,
                                 save_callback=save_callback,
                                 progress_callback=progress_callback)

    result["success"] = bool(rec.get("success"))
    result["error"] = rec.get("error")
    result["main_sprint"] = task.get("sprint") or (
        rec["target"][-1]["sprint"] if rec.get("target") else None
    )

    for entry in rec.get("created", []):
        result["sprint_tasks_created"].append({
            "sprint": entry["sprint"],
            "total_mins": entry["minutes"],
            "issue_ref": entry.get("issue"),
        })
    for entry in rec.get("skipped", []):
        if entry.get("reason") == "already bound":
            result["sprint_tasks_created"].append({
                "sprint": entry["sprint"],
                "total_mins": entry.get("minutes", 0),
                "issue_ref": None,
                "skipped": "shadow already exists",
            })
    for entry in rec.get("errors", []):
        result["sprint_tasks_created"].append({
            "sprint": entry.get("sprint"),
            "total_mins": entry.get("minutes", 0),
            "issue_ref": entry.get("issue"),
            "error": entry["error"],
        })
    if not result["success"] and not result["error"]:
        result["error"] = "; ".join(
            f"{e.get('sprint')}: {e['error']}" for e in rec.get("errors", [])
        ) or "reconcile failed"

    return result


def update_project_sprint(issue_ref: str, sprint_id: str, sprint_field_id: str, data: dict,
                          project_info: dict = None, item_id: str = None) -> bool:
    """Update Sprint field for an issue in the project.

    Returns True on success, False if project not configured or field missing.
    """
    config = data.get("config", {})
    if not config.get("github_project_number"):
        return False

    try:
        if not project_info:
            project_info = get_project_info(data)
        if not item_id:
            item_id = add_issue_to_project(issue_ref, data)

        result = subprocess.run([
            "gh", "project", "item-edit",
            "--project-id", project_info["project_id"],
            "--id", item_id,
            "--field-id", sprint_field_id,
            "--iteration-id", sprint_id
        ], capture_output=True, text=True)

        return result.returncode == 0
    except Exception:
        return False


def add_issue_to_project(issue_ref: str, data: dict) -> str:
    """Add issue to project and return item ID. Idempotent - returns existing item if already added."""
    config = data.get("config", {})
    owner = config.get("github_project_owner", "grafana")
    project_num = config.get("github_project_number")

    if not project_num:
        raise Exception("github_project_number not configured")

    issue_url = f"https://github.com/{issue_ref.replace('#', '/issues/')}"

    result = subprocess.run([
        "gh", "project", "item-add", str(project_num),
        "--owner", owner, "--url", issue_url, "--format", "json"
    ], capture_output=True, text=True)

    if result.returncode != 0:
        raise Exception(f"Failed to add to project: {result.stderr}")

    item_data = json.loads(result.stdout)
    item_id = item_data.get("id")

    if not item_id:
        raise Exception("No item ID returned from project")

    return item_id


def sync_project_status(issue_ref: str, status: str, data: dict,
                        project_info: dict = None, item_id: str = None) -> bool:
    """Sync task status to GitHub project. Adds issue to project if not already there.

    Args:
        issue_ref: GitHub issue reference (owner/repo#number)
        status: Workload tracker status (todo, inprogress, done)
        data: Full data dict with config
        project_info: Optional cached project info (avoids redundant API call)
        item_id: Optional cached item ID (avoids redundant API call)

    Returns True on success, False if project not configured.
    """
    config = data.get("config", {})
    if not config.get("github_project_number"):
        return False  # No project configured, skip silently

    project_status = PROJECT_STATUS_MAP.get(status)
    if not project_status:
        return False  # Unknown status

    try:
        if not project_info:
            project_info = get_project_info(data)
        if not item_id:
            item_id = add_issue_to_project(issue_ref, data)

        option_id = project_info["status_options"].get(project_status)
        if not option_id:
            return False  # Status option not found in project

        result = subprocess.run([
            "gh", "project", "item-edit",
            "--project-id", project_info["project_id"],
            "--id", item_id,
            "--field-id", project_info["status_field"]["id"],
            "--single-select-option-id", option_id
        ], capture_output=True, text=True)

        return result.returncode == 0
    except Exception:
        return False


def update_project_activity(issue_ref: str, activity: str, data: dict,
                            project_info: dict = None, item_id: str = None) -> bool:
    """Update Activity field for an issue in the project.

    Returns True on success, False if project not configured or field/option missing.
    """
    config = data.get("config", {})
    if not config.get("github_project_number"):
        return False

    try:
        if not project_info:
            project_info = get_project_info(data)
        if not item_id:
            item_id = add_issue_to_project(issue_ref, data)

        activity_field = project_info.get("activity_field", {})
        if not activity_field.get("id"):
            return False  # No Activity field

        option_id = project_info["activity_options"].get(activity)
        if not option_id:
            return False  # Activity option not found

        result = subprocess.run([
            "gh", "project", "item-edit",
            "--project-id", project_info["project_id"],
            "--id", item_id,
            "--field-id", activity_field["id"],
            "--single-select-option-id", option_id
        ], capture_output=True, text=True)

        return result.returncode == 0
    except Exception:
        return False


def update_project_type(issue_ref: str, type_val: str, data: dict,
                        project_info: dict = None, item_id: str = None) -> bool:
    """Update Type field for an issue in the project.

    Returns True on success, False if project not configured or field/option missing.
    """
    config = data.get("config", {})
    if not config.get("github_project_number"):
        return False

    try:
        if not project_info:
            project_info = get_project_info(data)
        if not item_id:
            item_id = add_issue_to_project(issue_ref, data)

        type_field = project_info.get("type_field", {})
        if not type_field.get("id"):
            return False  # No Type field

        option_id = project_info["type_options"].get(type_val)
        if not option_id:
            return False  # Type option not found

        result = subprocess.run([
            "gh", "project", "item-edit",
            "--project-id", project_info["project_id"],
            "--id", item_id,
            "--field-id", type_field["id"],
            "--single-select-option-id", option_id
        ], capture_output=True, text=True)

        return result.returncode == 0
    except Exception:
        return False


def update_project_hours(issue_ref: str, hours: int, data: dict,
                         project_info: dict = None, item_id: str = None) -> bool:
    """Update Hours field for an issue in the project.

    Returns True on success, False if project not configured or field missing.
    """
    config = data.get("config", {})
    if not config.get("github_project_number"):
        return False

    try:
        if not project_info:
            project_info = get_project_info(data)
        if not item_id:
            item_id = add_issue_to_project(issue_ref, data)

        hours_field = project_info.get("hours_field", {})
        if not hours_field.get("id"):
            return False  # No Hours field

        result = subprocess.run([
            "gh", "project", "item-edit",
            "--project-id", project_info["project_id"],
            "--id", item_id,
            "--field-id", hours_field["id"],
            "--number", str(hours)
        ], capture_output=True, text=True)

        return result.returncode == 0
    except Exception:
        return False


def add_to_project_and_update(issue_ref: str, hours: int, data: dict) -> dict:
    """Add issue to GitHub project and set Status=Done, add hours.

    Returns dict with item_id and success status.
    """
    # Sync status to Done
    sync_project_status(issue_ref, "done", data)

    # Update hours
    update_project_hours(issue_ref, hours, data)

    return {"success": True}


def get_project_hours(issue_ref: str, data: dict) -> float | None:
    """Get the current Hours value for an issue in the project.

    Returns the hours value or None if not found/not in project.
    """
    config = data.get("config", {})
    owner = config.get("github_project_owner", "grafana")
    project_num = config.get("github_project_number")

    if not project_num:
        return None

    # Convert to int for comparison (config may store as string)
    try:
        project_num_int = int(project_num)
    except (ValueError, TypeError):
        return None

    try:
        # Get issue's project items
        issue_url = f"https://github.com/{issue_ref.replace('#', '/issues/')}"
        query = f'''query {{
            resource(url: "{issue_url}") {{
                ... on Issue {{
                    projectItems(first: 10) {{
                        nodes {{
                            project {{ number }}
                            fieldValueByName(name: "Hours") {{
                                ... on ProjectV2ItemFieldNumberValue {{
                                    number
                                }}
                            }}
                        }}
                    }}
                }}
            }}
        }}'''
        result = subprocess.run([
            "gh", "api", "graphql", "-f", f"query={query}"
        ], capture_output=True, text=True)

        if result.returncode != 0:
            return None

        response = json.loads(result.stdout)
        items = response.get("data", {}).get("resource", {}).get("projectItems", {}).get("nodes", [])

        for item in items:
            if item.get("project", {}).get("number") == project_num_int:
                field_value = item.get("fieldValueByName")
                if field_value:
                    return field_value.get("number", 0)
                return 0

        return None  # Issue not in project
    except Exception:
        return None


def setup_issue_in_project(issue_ref: str, task: dict, data: dict) -> dict:
    """Add issue to project and set up all fields (Status, Activity, Type, Sprint, Hours).

    Args:
        issue_ref: GitHub issue reference (owner/repo#number)
        task: Task dict with status, logs, and optional activity/type
        data: Full data dict with config

    Returns dict with success status and any errors.
    """
    result = {"success": False, "errors": []}

    config = data.get("config", {})
    if not config.get("github_project_number"):
        result["errors"].append("Project not configured")
        return result

    try:
        # Pre-fetch (cache) project info and item id
        pi = get_project_info(data)
        item_id = add_issue_to_project(issue_ref, data)
        all_sprints = get_all_sprints(data)

        # Sync status
        status = task.get("status", "todo")
        if not sync_project_status(issue_ref, status, data, project_info=pi, item_id=item_id):
            result["errors"].append("Failed to set status")

        # Set activity from the task
        activity = get_task_activity(task)
        if activity:
            if not update_project_activity(issue_ref, activity, data, project_info=pi, item_id=item_id):
                result["errors"].append(f"Failed to set activity: {activity}")

        # Set type from the task
        type_val = get_task_type(task)
        if type_val:
            if not update_project_type(issue_ref, type_val, data, project_info=pi, item_id=item_id):
                result["errors"].append(f"Failed to set type: {type_val}")

        # Set sprint: use task's stored sprint, fall back to current sprint
        sprint_id = task.get("sprint_id")
        if sprint_id:
            field_id = all_sprints[0]["field_id"] if all_sprints else None
            if field_id and not update_project_sprint(issue_ref, sprint_id, field_id, data, project_info=pi, item_id=item_id):
                result["errors"].append(f"Failed to set sprint: {task.get('sprint', '?')}")
        else:
            current_sprint = get_current_sprint(data)
            if current_sprint:
                if not update_project_sprint(issue_ref, current_sprint["id"], current_sprint["field_id"], data, project_info=pi, item_id=item_id):
                    result["errors"].append(f"Failed to set sprint: {current_sprint['title']}")

        # Set hours (filtered by the task's current sprint, rounded to 0.25h)
        sprint_mins = task_reportable_mins(task, all_sprints)
        if sprint_mins > 0:
            hours = mins_to_quarter_hours(sprint_mins)
            if not update_project_hours(issue_ref, hours, data, project_info=pi, item_id=item_id):
                result["errors"].append("Failed to set hours")
            else:
                # Mark logs as uploaded
                mark_logs_uploaded(task)

        result["success"] = len(result["errors"]) == 0
        return result

    except Exception as e:
        result["errors"].append(str(e))
        return result


def sync_project_hours(issue_ref: str, task: dict, data: dict, save_callback=None) -> bool:
    """Sync task to GitHub project - updates Hours, Status, Activity, Type, and Sprint.

    Calculates total logged time, rounds to nearest 0.25 hours, and updates project.
    Also syncs Status, Activity, Type, and Sprint fields.
    Marks logs as uploaded after successful sync.

    Returns True on success.
    """
    if not issue_ref:
        return False

    config = data.get("config", {})
    if not config.get("github_project_number"):
        return False

    success = True

    # Sync status
    status = task.get("status", "todo")
    if not sync_project_status(issue_ref, status, data):
        success = False

    # Sync activity from the task
    activity = get_task_activity(task)
    if activity:
        if not update_project_activity(issue_ref, activity, data):
            success = False

    # Sync type from the task
    type_val = get_task_type(task)
    if type_val:
        if not update_project_type(issue_ref, type_val, data):
            success = False

    # Sync sprint: use task's stored sprint
    all_sprints = get_all_sprints(data)
    sprint_id = task.get("sprint_id")
    if sprint_id:
        field_id = all_sprints[0]["field_id"] if all_sprints else None
        if field_id:
            update_project_sprint(issue_ref, sprint_id, field_id, data)

    # Sync hours (filtered by the task's current sprint)
    sprint_mins = task_reportable_mins(task, all_sprints)
    if sprint_mins > 0:
        hours = mins_to_quarter_hours(sprint_mins)
        if update_project_hours(issue_ref, hours, data):
            mark_logs_uploaded(task)
            if save_callback:
                save_callback(data)
        else:
            success = False

    return success


def close_github_issue(issue_ref: str) -> bool:
    """Close a GitHub issue. Returns True on success."""
    result = subprocess.run(
        ["gh", "issue", "close", *gh_issue_args(issue_ref)],
        capture_output=True, text=True
    )
    return result.returncode == 0


def delete_github_issue(issue_ref: str) -> bool:
    """Permanently delete a GitHub issue. Returns True on success.
    WARNING: This is irreversible and removes all comments and history.
    """
    result = subprocess.run(
        ["gh", "issue", "delete", *gh_issue_args(issue_ref), "--yes"],
        capture_output=True, text=True
    )
    return result.returncode == 0


def ensure_issue_assigned(issue_ref: str) -> bool:
    """Ensure the current user is assigned to a GitHub issue.
    Adds @me as assignee if not already assigned. Returns True on success.
    """
    # gh issue edit --add-assignee is idempotent - won't duplicate if already assigned
    result = subprocess.run(
        ["gh", "issue", "edit", *gh_issue_args(issue_ref), "--add-assignee", "@me"],
        capture_output=True, text=True
    )
    return result.returncode == 0


def issue_has_comments(issue_ref: str) -> bool:
    """Check if a GitHub issue has any comments."""
    result = subprocess.run(
        ["gh", "issue", "view", *gh_issue_args(issue_ref), "--json", "comments"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return True  # Assume has comments on error to avoid blocking
    data = json.loads(result.stdout)
    return len(data.get("comments", [])) > 0


def add_issue_comment(issue_ref: str, comment: str) -> bool:
    """Add a comment to a GitHub issue. Returns True on success."""
    result = subprocess.run(
        ["gh", "issue", "comment", *gh_issue_args(issue_ref), "--body", comment],
        capture_output=True, text=True
    )
    return result.returncode == 0


def update_issue_title(issue_ref: str, new_title: str) -> bool:
    """Update the title of a GitHub issue. Returns True on success."""
    result = subprocess.run(
        ["gh", "issue", "edit", *gh_issue_args(issue_ref), "--title", new_title],
        capture_output=True, text=True
    )
    return result.returncode == 0


def _legacy_split_result(rec: dict, task: dict) -> dict:
    """Render a reconcile result in the shape ``split_cross_sprint_task`` returned.

    ``close_task()["split_result"]`` used to be a split result dict; renderers
    (``wt done``, the TUI, MCP) read ``sprint_tasks_created`` and ``main_sprint``
    off it. Phase 3 puts the full reconcile result there instead, but keeps those
    two keys populated so nothing downstream breaks mid-refactor.
    """
    created = [
        {"sprint": e["sprint"], "total_mins": e.get("minutes", 0),
         "issue_ref": e.get("issue")}
        for e in rec.get("created", [])
    ]
    for e in rec.get("skipped", []):
        if e.get("reason") == "already bound":
            created.append({"sprint": e.get("sprint"), "total_mins": e.get("minutes", 0),
                            "issue_ref": e.get("issue"), "skipped": "already bound"})
    for e in rec.get("errors", []):
        created.append({"sprint": e.get("sprint"), "total_mins": e.get("minutes", 0),
                        "issue_ref": e.get("issue"), "error": e["error"]})
    out = dict(rec)
    out["sprint_tasks_created"] = created
    out["main_sprint"] = task.get("sprint") or (
        rec["target"][-1]["sprint"] if rec.get("target") else None
    )
    return out


def close_task(task: dict, data: dict, save_callback, prompt_callback=None, comment_callback=None) -> dict:
    """
    Full task closing workflow.

    Args:
        task: The task dict to close
        data: The full data dict
        save_callback: Function to call to save data
        prompt_callback: Optional function(msg) -> bool to prompt user for confirmation
        comment_callback: Optional function(msg) -> str|None to get closing comment from user

    Returns:
        Dict with results: {success, issue_created, issue_closed, project_updated,
        skipped_github, comment_added, split_performed, split_result,
        reconcile_result, error}

    Phase 3: step 2.5 now calls :func:`reconcile_task_sprints` directly instead of
    going through the deprecated ``split_cross_sprint_task`` wrapper. Two
    consequences worth knowing:

      * The old gate was ``len(sprint_summary_for_task(task, sprints)) > 1``,
        which counted **zero-minute** sprints — so a task carrying one of the
        0-minute "rollover marker" logs (plan §1.3) looked multi-sprint and got
        split, while an identical task without the marker did not. Reconcile is
        idempotent and plans nothing when there is nothing to do, so it is now
        called unconditionally and the gate is gone. ``split_performed`` reports
        whether the reconcile actually created or re-pointed a binding.
      * Hours reported to the current issue come from
        :func:`task_reportable_mins`, i.e. the *current binding's* sprint rather
        than the task's mutable ``sprint_id``.
      * The reconcile is passed ``closing=True``, so it does **not** reserve an
        empty binding for the current sprint. A task being closed has no future
        work to land, so the old behaviour carried its long-lived issue onto a
        sprint it was never worked in and reported 0h there. Now the issue lands
        on — and reports against — the newest sprint that actually has time.

    ``recurrent`` tasks are reconciled like anything else. Phase 5 merged the old
    per-sprint clones into one perpetual task with a binding per sprint, so the
    reconcile below runs for them too — see the ``recurring`` branch in
    ``reconcile_task_sprints``, which withholds the carry-forward so each sprint
    keeps its own issue and its own hours. ``close-recurrent`` / ``new-recurrent``
    are retired and hard-refuse.
    """
    result = {
        "success": False,
        "issue_created": False,
        "issue_closed": False,
        "project_updated": False,
        "skipped_github": False,
        "comment_added": False,
        "split_performed": False,
        "split_result": None,
        "reconcile_result": None,
        "error": None
    }

    # 1. Check if the task has a GitHub repo
    repo = get_task_repo(task)

    if not repo:
        # No GitHub integration for this task - just close
        task["status"] = "done"
        save_callback(data)
        result["success"] = True
        result["skipped_github"] = True
        return result

    # 2. Ensure GitHub issue exists
    issue_ref = task_current_issue(task, data)
    if not issue_ref:
        if prompt_callback:
            create = prompt_callback(
                f"Task '{task['title']}' has no GitHub issue. Create one in {repo}?"
            )
            if not create:
                result["error"] = "Task must have GitHub issue to close (task has a repo configured)"
                return result

        try:
            issue_ref = create_github_issue(task, repo)
            set_task_current_issue(task, issue_ref, data)
            result["issue_created"] = True
            save_callback(data)
        except Exception as e:
            result["error"] = f"Failed to create issue: {e}"
            return result

    # 2.5. Reconcile the task's per-sprint bindings against its logs, so each
    # past sprint's hours land on their own issue and the current issue only ever
    # carries its own sprint's hours (Option A, plan §2.4). Idempotent, so this
    # is unconditional — see the docstring for what replaced the old gate.
    all_sprints = get_all_sprints(data)
    # Phase 5: recurrent tasks are no longer per-sprint clones, so they reconcile
    # like anything else. Closing one ends the recurrence; its per-sprint issues
    # were already created and closed as each sprint ended.
    if all_sprints:
        rec = reconcile_task_sprints(task, data, all_sprints, closing=True,
                                     save_callback=save_callback)
        result["reconcile_result"] = rec
        result["split_result"] = _legacy_split_result(rec, task)
        result["split_performed"] = bool(rec.get("created") or rec.get("repointed"))
        if not rec.get("success"):
            detail = rec.get("error") or "; ".join(
                f"{e.get('sprint')}: {e['error']}" for e in rec.get("errors", [])
            ) or "unknown error"
            result["error"] = f"Sprint reconcile failed: {detail}"
            return result
        # A create op can adopt the legacy field; re-read so we close the right one.
        issue_ref = task_current_issue(task, data) or issue_ref

    binding = _find_binding(task.get("sprint_issues") or [], issue=issue_ref)

    # 3. Add to project and update fields
    config = data.get("config", {})
    if config.get("github_project_number"):
        try:
            # Report only the current binding's sprint hours. The task keeps all
            # logs locally (source of truth) while each past sprint's hours live
            # on its own binding's issue; reporting the total would double-count.
            sprint_id = (binding or {}).get("sprint_id") or task.get("sprint_id")
            sprint_mins = task_reportable_mins(task, all_sprints, sprint_id)
            hours = mins_to_quarter_hours(sprint_mins)
            add_to_project_and_update(issue_ref, hours, data)
            result["project_updated"] = True

            # Record what GitHub was told on the matching binding, so a later
            # reconcile doesn't re-push an identical value.
            if binding is not None:
                binding["hours_synced"] = hours
                binding["synced_at"] = time.time()

            # Set activity if the task has one
            activity = get_task_activity(task)
            if activity:
                update_project_activity(issue_ref, activity, data)

            # Set the Sprint field from the binding we are closing
            if sprint_id:
                field_id = all_sprints[0]["field_id"] if all_sprints else None
                if field_id:
                    update_project_sprint(issue_ref, sprint_id, field_id, data)

            # Set type if the task has one
            type_val = get_task_type(task)
            if type_val:
                update_project_type(issue_ref, type_val, data)
        except Exception as e:
            # Project update is non-fatal - still mark task as done
            result["error"] = f"Project update failed: {e}"

    # 4. Check for comments and prompt for closing comment if none
    if comment_callback and not issue_has_comments(issue_ref):
        comment = comment_callback(
            f"Issue {issue_ref} has no comments. Add a closing comment?"
        )
        if comment:
            if add_issue_comment(issue_ref, comment):
                result["comment_added"] = True

    # 5. Close the GitHub issue — only the current binding's. Past-sprint
    #    bindings were already closed by the reconcile above.
    if close_github_issue(issue_ref):
        result["issue_closed"] = True
        # Keep the binding's cached state honest, or a reconcile run after this
        # sprint ends would try to close an already-closed issue.
        if binding is not None:
            binding["state"] = "closed"

    # 6. Mark as done
    task["status"] = "done"
    mark_logs_uploaded(task)
    save_callback(data)
    result["success"] = True
    return result


# ── Commands ──────────────────────────────────────────────

def cmd_add(args):
    if not args:
        print("Usage: wt add <title> [--role ROLE] [--status STATUS] [--desc DESC] [--sprint SPRINT] [--repo OWNER/REPO] [--activity ACT] [--type TYPE] [--create-issue]")
        sys.exit(1)

    data = load()
    roles = get_roles(data)

    # Parse title and flags
    title_parts = []
    role_id = "other"
    status = "todo"
    desc = ""
    sprint_override = None
    github_repo = None
    activity = None
    type_val = None
    create_issue = False
    i = 0
    while i < len(args):
        if args[i] == "--role" and i + 1 < len(args):
            role_id = resolve_role(data, args[i+1]); i += 2
        elif args[i] == "--status" and i + 1 < len(args):
            status = args[i+1]; i += 2
        elif args[i] == "--desc" and i + 1 < len(args):
            desc = args[i+1]; i += 2
        elif args[i] == "--sprint" and i + 1 < len(args):
            sprint_override = args[i+1]; i += 2
        elif args[i] == "--repo" and i + 1 < len(args):
            github_repo = args[i+1]; i += 2
        elif args[i] == "--activity" and i + 1 < len(args):
            activity = args[i+1]; i += 2
        elif args[i] == "--type" and i + 1 < len(args):
            type_val = args[i+1]; i += 2
        elif args[i] == "--create-issue":
            create_issue = True; i += 1
        else:
            title_parts.append(args[i]); i += 1
    title = " ".join(title_parts)
    if not title:
        print(c("Title is required.", "red")); sys.exit(1)

    # Validate flags BEFORE touching the data file
    if github_repo and ("/" not in github_repo or github_repo.count("/") != 1):
        print(c("Error: --repo must be in owner/repo format", "red"))
        sys.exit(1)
    cached_options = get_cached_project_options(data)
    if activity and cached_options.get("activity") and activity not in cached_options["activity"]:
        print(c(f"Unknown activity: {activity}", "red"))
        print(c("  Available: " + ", ".join(cached_options["activity"]), "dim"))
        sys.exit(1)
    if type_val and cached_options.get("type") and type_val not in cached_options["type"]:
        print(c(f"Unknown type: {type_val}", "red"))
        print(c("  Available: " + ", ".join(cached_options["type"]), "dim"))
        sys.exit(1)
    if create_issue and not github_repo:
        print(c("--create-issue requires --repo owner/repo.", "red"))
        sys.exit(1)

    task = {
        "id": uid(), "title": title, "description": desc,
        "role_id": role_id, "status": status,
        "logs": [], "created_at": time.time()
    }
    if github_repo:
        task["github_repo"] = github_repo
    if activity:
        task["activity"] = activity
    if type_val:
        task["type"] = type_val

    # Auto-assign sprint (or use override)
    if sprint_override and sprint_override.lower() == "none":
        pass  # Skip sprint assignment
    elif sprint_override:
        all_sprints = get_all_sprints(data)
        match = _match_sprint(all_sprints, sprint_override)
        if match:
            task["sprint"] = match["title"]
            task["sprint_id"] = match["id"]
        else:
            print(c(f"Sprint '{sprint_override}' not found.", "red")); sys.exit(1)
    else:
        current_sprint = get_current_sprint(data)
        if current_sprint:
            task["sprint"] = current_sprint["title"]
            task["sprint_id"] = current_sprint["id"]

    data["tasks"].insert(0, task)
    save(data)
    sprint_info = f"  [{task.get('sprint', 'no sprint')}]" if task.get("sprint") else ""
    print(c(f"✓ Added: {title}", "green") + f"  [{roles.get(role_id, role_id)}]  [{STATUS_LABELS.get(status, status)}]{sprint_info}")
    print(c(f"  id: {task['id']}", "dim"))

    # Arc integration: create task folder via UI scripting
    if data.get("config", {}).get("arc_space_id"):
        try:
            from arc_browser import TaskTabManager
            manager = TaskTabManager(data)
            result = manager.on_task_created(task, save)
            if result.get("folder_created"):
                print(c("  [Arc folder created]", "dim"))
            elif result.get("error"):
                print(c(f"  [Arc: {result['error']}]", "dim"))
        except ImportError:
            pass

    # Optional: create the linked GitHub issue and set up the project entry
    if create_issue:
        repo = get_task_repo(task)
        # repo is guaranteed by the pre-flight check above, but be defensive
        if not repo:
            print(c("  [skip github] task has no github_repo", "dim"))
            return
        try:
            issue_ref = create_github_issue(task, repo)
        except Exception as e:
            print(c(f"  Failed to create issue: {e}", "red"))
            sys.exit(1)
        set_task_current_issue(task, issue_ref, data)
        save(data)
        print(c(f"  ✓ Created issue: {issue_ref}", "green"))

        # Add to GitHub project (Status, Activity, Type, Sprint, Hours) if configured
        if data.get("config", {}).get("github_project_number"):
            res = setup_issue_in_project(issue_ref, task, data)
            if res.get("success"):
                print(c("  ✓ Added to project (Status/Activity/Type/Sprint/Hours)", "green"))
            else:
                for err in res.get("errors") or ["unknown error"]:
                    print(c(f"  ! project setup: {err}", "yellow"))


def cmd_list(args):
    data = load()
    tasks = data.get("tasks", [])
    at = data.get("active_timer")
    roles = get_roles(data)
    role_ids = get_role_ids(data)

    filter_role = None
    show_done = False
    i = 0
    while i < len(args):
        if args[i] == "--role" and i + 1 < len(args):
            filter_role = resolve_role(data, args[i+1]); i += 2
        elif args[i] in ("--all", "-a"):
            show_done = True; i += 1
        else:
            i += 1

    if filter_role:
        tasks = [t for t in tasks if t.get("role_id") == filter_role]

    # Hide done tasks by default
    if not show_done:
        tasks = [t for t in tasks if t.get("status") != "done"]

    # (Phase 3: the `--shadows` flag and its cross_sprint_parent filter are gone —
    # every task object is a real unit of work now.)

    if not tasks:
        print(c("No tasks.", "dim")); return

    # Group by role
    by_role = {}
    for task in tasks:
        by_role.setdefault(task.get("role_id", "other"), []).append(task)

    for role_id in role_ids:
        role_tasks = by_role.get(role_id, [])
        if not role_tasks:
            continue
        print(c(f"\n  {roles.get(role_id, role_id)}", "bold", "cyan"))
        for t in role_tasks:
            running = at and at.get("task_id") == t["id"]
            logged = task_logged_mins(t) + task_live_mins(t, at)
            status = STATUS_LABELS.get(t.get("status", "todo"), "")
            dot = c("▶ ", "green") if running else "  "
            # Notes indicator: # for GitHub issue, + for local notes
            if task_current_issue(t, data):
                notes_icon = c("#", "cyan")
            elif has_notes(t["id"]):
                notes_icon = c("+", "dim")
            else:
                notes_icon = " "
            time_str = c(fmt_mins(logged), "dim")
            status_str = c(f"[{status}]", "dim")
            sprint_str = c(f"[{t['sprint']}]", "dim") if t.get("sprint") else ""
            print(f"  {dot}{t['title'][:50]:<52} {notes_icon} {time_str:<10} {status_str} {sprint_str}")
            print(c(f"      id: {t['id']}", "dim"))
    print()


def cmd_start(args):
    if not args:
        print("Usage: wt start <task-id or title>"); sys.exit(1)
    data = load()
    task = resolve_task(data, " ".join(args))
    at = data.get("active_timer")

    # Stop current timer
    prev = None
    if at:
        prev = next((t for t in data["tasks"] if t["id"] == at["task_id"]), None)
        if prev:
            started_at = at["started_at"]
            ended_at = time.time()
            elapsed = (ended_at - started_at) / 60
            if elapsed > 0.05:
                prev.setdefault("logs", []).append({
                    "id": uid(), "minutes": round(elapsed, 2),
                    "note": "Timer session", "at": ended_at,
                    "started_at": started_at, "ended_at": ended_at
                })
        print(c(f"⏹  Stopped: {prev['title'] if prev else '?'}", "yellow"))

    data["active_timer"] = {"task_id": task["id"], "started_at": time.time()}
    save(data)
    print(c(f"▶  Started: {task['title']}", "green"))

    # Arc integration: focus the Workload Tracker space
    if data.get("config", {}).get("arc_space_id"):
        try:
            from arc_browser import TaskTabManager
            manager = TaskTabManager(data)
            result = manager.on_task_started(task)
            if result.get("focused"):
                print(c("  [Arc: Focused Workload Tracker space]", "dim"))
        except ImportError:
            pass


def cmd_stop(args):
    data = load()
    at = data.get("active_timer")
    if not at:
        print(c("No active timer.", "dim")); return
    task = next((t for t in data["tasks"] if t["id"] == at["task_id"]), None)
    started_at = at["started_at"]
    ended_at = time.time()
    elapsed = (ended_at - started_at) / 60
    if task and elapsed > 0.05:
        task.setdefault("logs", []).append({
            "id": uid(), "minutes": round(elapsed, 2),
            "note": "Timer session", "at": ended_at,
            "started_at": started_at, "ended_at": ended_at
        })
    data["active_timer"] = None
    save(data)
    print(c(f"⏹  Stopped: {task['title'] if task else '?'}  ({fmt_mins(elapsed)})", "yellow"))

    # Arc integration: tab cleanup
    if task and data.get("config", {}).get("tab_cleanup_enabled"):
        try:
            from arc_browser import TaskTabManager
            manager = TaskTabManager(data)
            result = manager.on_task_stopped(task, prompt_callback=_cli_tab_cleanup_prompt)
            if result.get("tabs_closed"):
                print(c(f"  [Arc: Closed {result['tabs_closed']} unrelated tabs]", "dim"))
            elif result.get("unrelated_tabs"):
                print(c(f"  [Arc: Found {len(result['unrelated_tabs'])} potentially unrelated tabs]", "dim"))
        except ImportError:
            pass


def cmd_log(args):
    if len(args) < 2:
        print("Usage: wt log <task-id or title> <minutes> [note]"); sys.exit(1)
    data = load()
    # Last numeric arg is minutes; everything before is task query
    try:
        mins = float(args[-1])
        query_parts = args[:-1]
        note = "Manual entry"
    except ValueError:
        # Maybe: wt log <task> <mins> <note>
        if len(args) < 3:
            print("Usage: wt log <task-id or title> <minutes> [note]"); sys.exit(1)
        try:
            mins = float(args[-2])
            note = args[-1]
            query_parts = args[:-2]
        except ValueError:
            print(c("Could not parse minutes.", "red")); sys.exit(1)

    task = resolve_task(data, " ".join(query_parts))
    task.setdefault("logs", []).append({
        "id": uid(), "minutes": mins, "note": note, "at": time.time()
    })
    save(data)
    print(c(f"✓ Logged {fmt_mins(mins)} to '{task['title']}'  ({note})", "green"))


def cmd_logs(args):
    """List all time logs for a task."""
    if not args:
        print("Usage: wt logs <task-id or title>"); sys.exit(1)
    data = load()
    task = resolve_task(data, " ".join(args))
    logs = task.get("logs", [])

    if not logs:
        print(c(f"No time logs for '{task['title']}'", "dim"))
        return

    total_mins = sum(l.get("minutes", 0) for l in logs)
    print(c(f"\n  Time logs for: {task['title']}", "bold"))
    print(c(f"  Total: {fmt_mins(total_mins)}\n", "dim"))

    for log in logs:
        log_id = log.get("id", "?")[:11]
        mins = log.get("minutes", 0)
        note = log.get("note", "—")[:30]
        at = log.get("at", 0)
        started = log.get("started_at")
        ended = log.get("ended_at")

        # Format time range if available
        if started and ended:
            start_str = datetime.fromtimestamp(started).strftime("%H:%M")
            end_str = datetime.fromtimestamp(ended).strftime("%H:%M")
            time_range = f"[{start_str}-{end_str}]"
        else:
            time_range = ""

        at_str = datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M") if at else ""

        print(f"  {log_id}...  {fmt_mins(mins):>7}  {note:<30}  {time_range:>13}  {at_str}")
    print()


def cmd_edit_log(args):
    """Edit a log entry's minutes or note."""
    if len(args) < 2:
        print("Usage: wt edit-log <task> <log-id> [--minutes M] [--note N]")
        print("  Example: wt edit-log 'My task' 20260403085 --minutes 45")
        sys.exit(1)

    data = load()

    # Parse arguments - find log-id and flags
    task_parts = []
    log_id_prefix = None
    new_minutes = None
    new_note = None

    i = 0
    while i < len(args):
        if args[i] == "--minutes" and i + 1 < len(args):
            try:
                new_minutes = float(args[i + 1])
            except ValueError:
                print(c("Error: minutes must be a number", "red")); sys.exit(1)
            i += 2
        elif args[i] == "--note" and i + 1 < len(args):
            new_note = args[i + 1]
            i += 2
        elif log_id_prefix is None and len(args[i]) >= 8 and args[i][:8].isdigit():
            # Looks like a log ID (starts with timestamp)
            log_id_prefix = args[i]
            i += 1
        else:
            task_parts.append(args[i])
            i += 1

    if not task_parts:
        print(c("Error: task identifier required", "red")); sys.exit(1)
    if not log_id_prefix:
        print(c("Error: log ID required", "red")); sys.exit(1)
    if new_minutes is None and new_note is None:
        print(c("Error: specify --minutes and/or --note", "red")); sys.exit(1)

    task = resolve_task(data, " ".join(task_parts))
    logs = task.get("logs", [])

    # Find log by ID prefix
    log = next((l for l in logs if l.get("id", "").startswith(log_id_prefix)), None)
    if not log:
        print(c(f"No log found with ID starting with '{log_id_prefix}'", "red"))
        sys.exit(1)

    # Apply changes
    old_mins = log.get("minutes", 0)
    old_note = log.get("note", "")

    if new_minutes is not None:
        log["minutes"] = new_minutes
    if new_note is not None:
        log["note"] = new_note

    save(data)

    if new_minutes is not None and new_note is not None:
        print(c(f"✓ Updated log: {fmt_mins(old_mins)} → {fmt_mins(new_minutes)}, note → '{new_note}'", "green"))
    elif new_minutes is not None:
        print(c(f"✓ Updated log: {fmt_mins(old_mins)} → {fmt_mins(new_minutes)}", "green"))
    else:
        print(c(f"✓ Updated log note: '{old_note}' → '{new_note}'", "green"))


def cmd_delete_log(args):
    """Delete a log entry."""
    if len(args) < 2:
        print("Usage: wt delete-log <task> <log-id>")
        sys.exit(1)

    data = load()

    # Last arg is log ID, rest is task query
    log_id_prefix = args[-1]
    task = resolve_task(data, " ".join(args[:-1]))
    logs = task.get("logs", [])

    # Find log by ID prefix
    log = next((l for l in logs if l.get("id", "").startswith(log_id_prefix)), None)
    if not log:
        print(c(f"No log found with ID starting with '{log_id_prefix}'", "red"))
        sys.exit(1)

    # Confirm deletion
    mins = log.get("minutes", 0)
    note = log.get("note", "—")
    print(f"Delete log entry: {fmt_mins(mins)} — {note}")
    try:
        response = input("Confirm delete? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    if response not in ("y", "yes"):
        print("Cancelled.")
        sys.exit(0)

    task["logs"] = [l for l in logs if l.get("id") != log.get("id")]
    save(data)
    print(c(f"✓ Deleted log entry ({fmt_mins(mins)})", "yellow"))


def cmd_split_log(args):
    """Split a log entry at a specified minute mark."""
    if len(args) < 3:
        print("Usage: wt split-log <task> <log-id> <minutes>")
        print("  Example: wt split-log 'My task' 20260403085 25")
        print("  Splits a 60min log at 25min into two entries: 25min + 35min")
        sys.exit(1)

    data = load()

    # Parse: last arg is split point, second-to-last is log ID, rest is task
    try:
        split_at = float(args[-1])
    except ValueError:
        print(c("Error: split point must be a number", "red")); sys.exit(1)

    log_id_prefix = args[-2]
    task = resolve_task(data, " ".join(args[:-2]))
    logs = task.get("logs", [])

    # Find log by ID prefix
    log_idx = next((i for i, l in enumerate(logs) if l.get("id", "").startswith(log_id_prefix)), None)
    if log_idx is None:
        print(c(f"No log found with ID starting with '{log_id_prefix}'", "red"))
        sys.exit(1)

    log = logs[log_idx]
    total_mins = log.get("minutes", 0)

    if split_at <= 0 or split_at >= total_mins:
        print(c(f"Error: split point must be between 0 and {total_mins}", "red"))
        sys.exit(1)

    # Calculate split
    first_mins = split_at
    second_mins = total_mins - split_at
    note = log.get("note", "")
    started = log.get("started_at")
    ended = log.get("ended_at")

    # Calculate proportional timestamps if available
    if started and ended:
        duration = ended - started
        ratio = first_mins / total_mins
        mid_time = started + (duration * ratio)

        first_log = {
            "id": uid(), "minutes": round(first_mins, 2),
            "note": f"{note} (1/2)", "at": mid_time,
            "started_at": started, "ended_at": mid_time
        }
        second_log = {
            "id": uid(), "minutes": round(second_mins, 2),
            "note": f"{note} (2/2)", "at": ended,
            "started_at": mid_time, "ended_at": ended
        }
    else:
        at = log.get("at", time.time())
        first_log = {
            "id": uid(), "minutes": round(first_mins, 2),
            "note": f"{note} (1/2)", "at": at
        }
        second_log = {
            "id": uid(), "minutes": round(second_mins, 2),
            "note": f"{note} (2/2)", "at": at
        }

    # Replace original with two new entries
    logs[log_idx:log_idx+1] = [first_log, second_log]
    save(data)

    print(c(f"✓ Split {fmt_mins(total_mins)} into {fmt_mins(first_mins)} + {fmt_mins(second_mins)}", "green"))


def cmd_merge_logs(args):
    """Merge two log entries into one."""
    if len(args) < 3:
        print("Usage: wt merge-logs <task> <log-id-1> <log-id-2>")
        sys.exit(1)

    data = load()

    # Parse: last two args are log IDs, rest is task
    log_id_1 = args[-2]
    log_id_2 = args[-1]
    task = resolve_task(data, " ".join(args[:-2]))
    logs = task.get("logs", [])

    # Find logs by ID prefix
    log1 = next((l for l in logs if l.get("id", "").startswith(log_id_1)), None)
    log2 = next((l for l in logs if l.get("id", "").startswith(log_id_2)), None)

    if not log1:
        print(c(f"No log found with ID starting with '{log_id_1}'", "red")); sys.exit(1)
    if not log2:
        print(c(f"No log found with ID starting with '{log_id_2}'", "red")); sys.exit(1)
    if log1.get("id") == log2.get("id"):
        print(c("Error: cannot merge a log with itself", "red")); sys.exit(1)

    # Combine
    combined_mins = log1.get("minutes", 0) + log2.get("minutes", 0)
    note1 = log1.get("note", "")
    note2 = log2.get("note", "")
    combined_note = f"Merged: {note1} + {note2}"

    # Use earliest start and latest end
    started1 = log1.get("started_at")
    started2 = log2.get("started_at")
    ended1 = log1.get("ended_at")
    ended2 = log2.get("ended_at")

    merged_log = {
        "id": uid(),
        "minutes": round(combined_mins, 2),
        "note": combined_note,
        "at": max(log1.get("at", 0), log2.get("at", 0))
    }

    # Add timestamps if both logs have them
    if started1 and started2:
        merged_log["started_at"] = min(started1, started2)
    if ended1 and ended2:
        merged_log["ended_at"] = max(ended1, ended2)

    # Remove old logs and add merged
    task["logs"] = [l for l in logs if l.get("id") not in (log1.get("id"), log2.get("id"))]
    task["logs"].append(merged_log)

    # Sort by 'at' timestamp
    task["logs"].sort(key=lambda x: x.get("at", 0))

    save(data)
    print(c(f"✓ Merged {fmt_mins(log1.get('minutes', 0))} + {fmt_mins(log2.get('minutes', 0))} = {fmt_mins(combined_mins)}", "green"))


def cmd_done(args):
    if not args:
        print("Usage: wt done <task-id or title>"); sys.exit(1)
    data = load()
    task = resolve_task(data, " ".join(args))

    def prompt_cb(msg):
        try:
            response = input(f"{msg} [Y/n]: ").strip().lower()
            return response != 'n'
        except (EOFError, KeyboardInterrupt):
            print()
            return False

    def comment_cb(msg):
        try:
            print(f"{msg}")
            comment = input("Comment (or Enter to skip): ").strip()
            return comment if comment else None
        except (EOFError, KeyboardInterrupt):
            print()
            return None

    result = close_task(task, data, save, prompt_callback=prompt_cb, comment_callback=comment_cb)

    if result["success"]:
        print(c(f"✓ Closed: {task['title']}", "green"))
        if result["skipped_github"]:
            print(c(f"  (No GitHub integration — task has no repo)", "dim"))
        else:
            issue_ref = task_current_issue(task, data)
            rec = result.get("reconcile_result")
            if rec:
                for line in _reconcile_outcome_lines(rec):
                    print(c(f"  {line}", "dim"))
            if result["issue_created"]:
                print(c(f"  Created issue: {issue_ref}", "dim"))
            if result.get("comment_added"):
                print(c(f"  Added closing comment", "dim"))
            if result["issue_closed"]:
                print(c(f"  Closed issue: {issue_ref}", "dim"))
            if result["project_updated"]:
                # Report the sprint-filtered hours actually synced: the task keeps
                # every log locally, but each past sprint's hours live on its own
                # binding's issue, so only the current binding's total goes here.
                binding = _find_binding(task.get("sprint_issues") or [], issue=issue_ref)
                sprints = get_all_sprints(data)
                synced = mins_to_quarter_hours(task_reportable_mins(
                    task, sprints, (binding or {}).get("sprint_id")))
                sprint_label = (binding or {}).get("sprint") or task.get("sprint") or "?"
                print(c(f"  Updated project (Status: Done, Sprint: {sprint_label}, "
                        f"Hours: {synced})", "dim"))
            elif result.get("error"):
                print(c(f"  Warning: {result['error']}", "yellow"))
    else:
        print(c(f"Failed to close: {result.get('error')}", "red"))
        sys.exit(1)

    # Arc integration: archive tabs and delete folder
    if task.get("arc_folder_id"):
        try:
            from arc_browser import TaskTabManager, prompt_arc_restart
            manager = TaskTabManager(data)
            arc_result = manager.on_task_completed(task, save)
            if arc_result.get("tabs_archived"):
                print(c(f"  [Arc: Archived {arc_result['tabs_archived']} tabs]", "dim"))
            if arc_result.get("folder_deleted"):
                print(c("  [Arc: Folder removed]", "dim"))
                if arc_result.get("restart_required"):
                    print(c("  Restart Arc to apply changes.", "yellow"))
        except ImportError:
            pass


def _recurrent_command_retired(name: str, replacement: str) -> None:
    """Refuse a retired per-sprint-clone command and point at the replacement.

    Phase 5 merged each recurring series into one perpetual task with a binding
    per sprint, so there are no longer per-sprint clones to close or recreate.
    These commands were dangerous rather than merely obsolete: the close side
    selected on ``status == "recurrent"`` plus a prior-sprint ``sprint_id``,
    which the merged task matches — running it would have set the whole series
    to ``done`` and closed its live issue, ending the recurrence; the recreate
    side would have minted a per-sprint clone of a task that is meant to be
    perpetual (measured at the Sprint 106 boundary: all 7 series selected).

    The planners themselves are gone — the refusal is no longer a guard rail in
    front of live code, it is all that remains. These two entry points survive
    only so the commands explain what replaced them instead of printing
    "unknown command".
    """
    print(c(f"\n  wt {name} has been retired.", "yellow"))
    print("  Recurring work is now one perpetual task with a GitHub issue per")
    print("  sprint, so there are no per-sprint copies to close or recreate.")
    print(c(f"\n  Use: {replacement}", "bold"))
    print(c("  It closes the sprint that just ended and opens the new one.\n", "dim"))
    sys.exit(2)


def cmd_close_recurrent(args):
    _recurrent_command_retired("close-recurrent", "wt sync-sprints --all            (then --create-issues to mint the new sprint's issues)")


def cmd_new_recurrent(args):
    _recurrent_command_retired("new-recurrent", "wt sync-sprints --all --create-issues")


def cmd_delete(args):
    if not args:
        print("Usage: wt delete <task-id or title>"); sys.exit(1)
    data = load()
    task = resolve_task(data, " ".join(args))
    issue_ref = task_current_issue(task, data)
    # Past-sprint bindings are separate, already-closed issues; deleting them is
    # not what `wt delete` ever did, so name them instead of silently orphaning.
    other_issues = [r for r in task_issue_refs(task) if r != issue_ref]
    data["tasks"] = [t for t in data["tasks"] if t["id"] != task["id"]]
    if (data.get("active_timer") or {}).get("task_id") == task["id"]:
        data["active_timer"] = None
    save(data)
    print(c(f"✓ Deleted: {task['title']}", "yellow"))
    if issue_ref:
        if delete_github_issue(issue_ref):
            print(c(f"  Deleted GitHub issue: {issue_ref}", "dim"))
        else:
            print(c(f"  Warning: Failed to delete GitHub issue {issue_ref} (may need admin permissions)", "yellow"))
    if other_issues:
        print(c(f"  Note: {len(other_issues)} past-sprint issue(s) left in place: "
                + ", ".join(other_issues), "yellow"))


def cmd_rename(args):
    if len(args) < 2:
        print("Usage: wt rename <task-id or title> <new title>")
        print("  Example: wt rename 'old name' 'new name'")
        sys.exit(1)
    data = load()
    # First arg is task identifier, rest is new title
    task_query = args[0]
    new_title = " ".join(args[1:])
    task = resolve_task(data, task_query)
    old_title = task["title"]
    task["title"] = new_title
    save(data)
    print(c(f"✓ Renamed: {old_title}", "dim"))
    print(c(f"       → {new_title}", "green"))

    # Update the current binding's GitHub issue title if present. Past-sprint
    # issues keep their " (Sprint N)" titles — renaming those is not this
    # command's job (and would need the suffix re-applied per binding).
    issue_ref = task_current_issue(task, data)
    if issue_ref:
        if update_issue_title(issue_ref, new_title):
            print(c(f"  Updated GitHub issue: {issue_ref}", "dim"))
        else:
            print(c(f"  Warning: Failed to update GitHub issue title", "yellow"))


def cmd_status(args):
    data = load()
    tasks = data.get("tasks", [])
    at = data.get("active_timer")
    roles = get_roles(data)
    role_ids = get_role_ids(data)

    total = sum(task_logged_mins(t) + task_live_mins(t, at) for t in tasks)
    print(c(f"\n  Workload Tracker — {len(tasks)} tasks — {fmt_mins(total)} total\n", "bold"))

    by_role = {}
    for task in tasks:
        rid = task.get("role_id", "other")
        by_role.setdefault(rid, 0)
        by_role[rid] += task_logged_mins(task) + task_live_mins(task, at)

    for role_id in role_ids:
        mins = by_role.get(role_id, 0)
        pct = round(mins / total * 100) if total else 0
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        print(f"  {roles.get(role_id, role_id):<25} {bar} {pct:>3}%  {fmt_mins(mins)}")

    if at:
        task = next((t for t in tasks if t["id"] == at["task_id"]), None)
        elapsed = (time.time() - at["started_at"]) / 60
        print(c(f"\n  ▶ Timer running: {task['title'] if task else '?'}  ({fmt_mins(elapsed)})", "green"))
    print()


def cmd_notes(args):
    if not args:
        print("Usage: wt notes <task-id or title>"); sys.exit(1)
    data = load()
    task = resolve_task(data, " ".join(args))

    # Check if task is linked to a GitHub issue
    gh_ref = task_current_issue(task, data)
    if gh_ref:
        print(c(f"Opening GitHub issue: {gh_ref}", "cyan"))
        subprocess.run(["gh", "issue", "view", *gh_issue_args(gh_ref), "--web"])
        return

    # Local notes behavior
    NOTES_DIR.mkdir(exist_ok=True)
    npath = notes_path(task["id"])

    # Create file with header if it doesn't exist
    if not npath.exists():
        npath.write_text(f"# {task['title']}\n\n")

    editor = os.environ.get("EDITOR", "vim")
    print(c(f"Opening notes for: {task['title']}", "cyan"))
    print(c(f"  {npath}", "dim"))
    subprocess.run([editor, str(npath)])


def cmd_roles(args):
    """Manage roles: list, add, update, delete"""
    data = load()

    if not args:
        # List roles
        print(c("\n  Roles:\n", "bold"))
        for r in data.get("roles", []):
            task_count = len([t for t in data["tasks"] if t.get("role_id") == r["id"]])
            count_str = f"({task_count} tasks)" if task_count else ""
            print(f"  {r['id']:<15} {r['label']:<35} {count_str}")
        print()
        return

    subcmd = args[0].lower()

    if subcmd == "add":
        if len(args) < 3:
            print("Usage: wt roles add <id> <label>"); sys.exit(1)
        role_id = args[1].lower()
        label = " ".join(args[2:])

        if any(r["id"] == role_id for r in data["roles"]):
            print(c(f"Role '{role_id}' already exists.", "red")); sys.exit(1)

        data["roles"].append({"id": role_id, "label": label, "color": "white"})
        save(data)
        print(c(f"✓ Added role: {role_id} ({label})", "green"))

    elif subcmd == "update":
        if len(args) < 3:
            print("Usage: wt roles update <id> <new-label>"); sys.exit(1)
        role_id = args[1].lower()
        new_label = " ".join(args[2:])

        role = next((r for r in data["roles"] if r["id"] == role_id), None)
        if not role:
            print(c(f"Role '{role_id}' not found.", "red")); sys.exit(1)

        role["label"] = new_label
        save(data)
        print(c(f"✓ Updated role: {role_id} → {new_label}", "green"))

    elif subcmd == "delete" or subcmd == "del" or subcmd == "rm":
        if len(args) < 2:
            print("Usage: wt roles delete <id>"); sys.exit(1)
        role_id = args[1].lower()

        role = next((r for r in data["roles"] if r["id"] == role_id), None)
        if not role:
            print(c(f"Role '{role_id}' not found.", "red")); sys.exit(1)

        task_count = len([t for t in data["tasks"] if t.get("role_id") == role_id])
        if task_count > 0:
            print(c(f"Cannot delete role '{role_id}': {task_count} tasks use it.", "red"))
            print(c("  Reassign or delete those tasks first.", "dim"))
            sys.exit(1)

        data["roles"] = [r for r in data["roles"] if r["id"] != role_id]
        save(data)
        print(c(f"✓ Deleted role: {role_id}", "yellow"))

    else:
        print(c(f"Unknown roles subcommand: {subcmd}", "red"))
        print("Usage: wt roles [add|update|delete] ...")
        print(c("  (Per-task GitHub settings moved to: wt set-repo/set-activity/set-type <task> ...)", "dim"))
        sys.exit(1)


def cmd_push(args):
    """Sync logged time and project fields to the linked GitHub issue."""
    if not args:
        print("Usage: wt push <task-id or title>")
        sys.exit(1)
    data = load()
    query = " ".join(args)
    task = resolve_task(data, query)
    if not task:
        print(c(f"No task matching '{query}'", "red"))
        sys.exit(1)
    issue_ref = task_current_issue(task, data)
    if not issue_ref:
        print(c(f"Task '{task['title']}' has no linked GitHub issue", "red"))
        sys.exit(1)
    result = setup_issue_in_project(issue_ref, task, data)
    save(data)  # persist mark_logs_uploaded side-effect
    if result["success"]:
        hours = mins_to_quarter_hours(task_reportable_mins(task, get_all_sprints(data)))
        print(c(f"Pushed '{task['title']}' to {issue_ref}: {hours}h", "green"))
    else:
        print(c(f"Push completed with errors: {', '.join(result['errors'])}", "yellow"))
        sys.exit(1)


def cmd_link(args):
    """Link a task to a GitHub issue."""
    if len(args) < 2:
        print("Usage: wt link <task-id or title> <github-issue>")
        print("  Examples:")
        print("    wt link 'Fix bug' 123              (uses default repo)")
        print("    wt link 'Fix bug' owner/repo#123")
        print("    wt link 'Fix bug' https://github.com/owner/repo/issues/123")
        sys.exit(1)

    data = load()
    # Resolve task first so we can use its repo for bare issue numbers
    query = " ".join(args[:-1])
    task = resolve_task(data, query)
    # Issue ref is the last argument - use task's repo if available
    issue_ref = normalize_issue_ref(args[-1], data, task)

    # Validate the issue exists
    result = subprocess.run(
        ["gh", "issue", "view", *gh_issue_args(issue_ref), "--json", "number,title"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(c(f"Could not find GitHub issue: {issue_ref}", "red"))
        print(c("  Make sure the issue exists and you have access.", "dim"))
        sys.exit(1)

    issue_info = json.loads(result.stdout)

    # Warn if task has existing local notes
    if has_notes(task["id"]):
        npath = notes_path(task["id"])
        print(c(f"Warning: Task has local notes at {npath}", "yellow"))
        print(c("  Local notes will be ignored when GitHub issue is linked.", "dim"))

    # Ensure current user is assigned to the issue
    ensure_issue_assigned(issue_ref)

    # Store the issue reference (normalized form) on the current binding
    set_task_current_issue(task, issue_ref, data)
    # Pin the task's repo from the issue ref (so the close workflow engages)
    if not task.get("github_repo") and "#" in issue_ref:
        task["github_repo"] = issue_ref.split("#", 1)[0]
    save(data)
    print(c(f"Linked '{task['title']}' to GitHub issue #{issue_info['number']}: {issue_info['title']}", "green"))


def cmd_unlink(args):
    """Unlink a task from its GitHub issue."""
    if not args:
        print("Usage: wt unlink <task-id or title>"); sys.exit(1)

    data = load()
    task = resolve_task(data, " ".join(args))

    if not task_current_issue(task, data):
        print(c(f"Task '{task['title']}' is not linked to a GitHub issue.", "yellow"))
        sys.exit(0)

    old_issue = clear_task_current_issue(task, data)
    save(data)
    print(c(f"Unlinked '{task['title']}' from {old_issue}", "green"))
    remaining = task_issue_refs(task)
    if remaining:
        print(c(f"  Still bound to {len(remaining)} past-sprint issue(s): "
                + ", ".join(remaining), "dim"))


def cmd_config(args):
    """View or set configuration values."""
    data = load()
    config = data.setdefault("config", {})

    # Keys that should be converted to specific types
    BOOL_KEYS = {"presence_detection_enabled", "subtract_idle_time", "tab_cleanup_enabled"}
    INT_KEYS = {"idle_timeout_minutes"}
    FLOAT_KEYS = {"tab_confidence_threshold"}

    if not args:
        # Show all config
        if not config:
            print(c("No config set.", "dim"))
            return
        print(c("\n  Configuration:\n", "bold"))
        for key, value in config.items():
            print(f"  {key}: {value}")
        print()
        return

    key = args[0]
    # Normalize key (allow github-repo or github_repo)
    key_normalized = key.replace("-", "_")

    if len(args) == 1:
        # Show specific value
        value = config.get(key_normalized)
        if value is None:
            print(c(f"Config '{key}' is not set.", "dim"))
        else:
            print(value)
        return

    # Set value with type conversion
    raw_value = args[1]

    if key_normalized in BOOL_KEYS:
        value = raw_value.lower() in ("true", "1", "yes", "on")
    elif key_normalized in INT_KEYS:
        try:
            value = int(raw_value)
        except ValueError:
            print(c(f"Error: {key} must be an integer.", "red"))
            sys.exit(1)
    elif key_normalized in FLOAT_KEYS:
        try:
            value = float(raw_value)
        except ValueError:
            print(c(f"Error: {key} must be a number.", "red"))
            sys.exit(1)
    else:
        value = raw_value

    config[key_normalized] = value
    save(data)
    print(c(f"✓ Set {key}: {value}", "green"))


def cmd_presence(args):
    """Manage presence detection (auto-stop timer on idle)."""
    data = load()
    config = data.setdefault("config", {})

    if not args:
        # Show status
        enabled = config.get("presence_detection_enabled", False)
        timeout = config.get("idle_timeout_minutes", 15)
        subtract = config.get("subtract_idle_time", True)

        print(c("\n  Presence Detection\n", "bold"))
        print(f"  Enabled:       {'Yes' if enabled else 'No'}")
        print(f"  Timeout:       {timeout} minutes")
        print(f"  Subtract idle: {'Yes' if subtract else 'No'}")
        print()

        if not enabled:
            print(c("  Enable with: wt presence on", "dim"))
        print()
        return

    arg = args[0].lower()

    if arg == "on":
        config["presence_detection_enabled"] = True
        timeout = config.get("idle_timeout_minutes", 15)
        save(data)
        print(c(f"✓ Presence detection enabled ({timeout}m timeout)", "green"))

    elif arg == "off":
        config["presence_detection_enabled"] = False
        save(data)
        print(c("✓ Presence detection disabled", "yellow"))

    elif arg.isdigit():
        minutes = int(arg)
        if minutes < 1:
            print(c("Error: Timeout must be at least 1 minute.", "red"))
            sys.exit(1)
        config["presence_detection_enabled"] = True
        config["idle_timeout_minutes"] = minutes
        save(data)
        print(c(f"✓ Presence detection enabled with {minutes}m timeout", "green"))

    else:
        print(c(f"Unknown argument: {arg}", "red"))
        print("Usage: wt presence [on|off|<minutes>]")
        sys.exit(1)


def create_task_from_issue(
    data: dict,
    issue_ref: str,
    role_id: str = "other",
    local_folder: str | None = None,
    status: str = "todo",
    assign_sprint: bool = True,
) -> dict:
    """Create a task linked to an *existing* GitHub issue.

    Fetches the issue title via `gh`, builds a task with the given role/status,
    optionally records a validated `local_folder`, and assigns the current
    sprint. The task is inserted into ``data["tasks"]`` but **not** persisted —
    the caller is responsible for calling ``save(data)``.

    Returns a result dict ``{task, error, existed}``:
      - ``error``  — non-None message if the issue/folder is invalid (no task created)
      - ``existed`` — True if a task is already linked to this issue (``task`` is it)
    """
    issue_ref = normalize_issue_ref(issue_ref, data)

    # Validate the local folder up front so we never create a half-set task.
    folder_str = None
    if local_folder:
        folder = Path(local_folder).expanduser()
        if not folder.exists():
            return {"task": None, "error": f"Folder does not exist: {local_folder}", "existed": False}
        if not folder.is_dir():
            return {"task": None, "error": f"Path is not a directory: {local_folder}", "existed": False}
        folder_str = str(folder.resolve())

    # Fetch issue details (also validates existence/access).
    result = subprocess.run(
        ["gh", "issue", "view", *gh_issue_args(issue_ref), "--json", "number,title,state,url"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return {"task": None, "error": f"Could not find GitHub issue: {issue_ref}", "existed": False}
    issue_info = json.loads(result.stdout)

    # Don't create a duplicate if a task is already linked to this issue — check
    # every binding, not just the legacy field, so a past-sprint issue matches too.
    for t in data["tasks"]:
        if issue_ref in task_issue_refs(t):
            return {"task": t, "error": None, "existed": True}

    task = {
        "id": uid(),
        "title": issue_info["title"],
        "description": "",
        "role_id": role_id,
        "status": status,
        "logs": [],
        "created_at": time.time(),
        "github_issue": issue_ref,
    }
    # The issue ref pins the task's repo
    if "#" in issue_ref:
        task["github_repo"] = issue_ref.split("#", 1)[0]
    if folder_str:
        task["local_folder"] = folder_str

    # Best-effort current-sprint assignment (network call; don't fail the task).
    if assign_sprint:
        try:
            current_sprint = get_current_sprint(data)
            if current_sprint:
                task["sprint"] = current_sprint["title"]
                task["sprint_id"] = current_sprint["id"]
        except Exception:
            pass

    # Record the link as a binding too (set after the sprint so the binding lands
    # on the right sprint). Rewrites the same legacy key it was seeded with.
    set_task_current_issue(task, issue_ref, data)

    data["tasks"].insert(0, task)
    return {"task": task, "error": None, "existed": False}


def cmd_add_issue(args):
    """Create a task from an existing GitHub issue (status: To Do)."""
    data = load()
    roles = get_roles(data)
    role_ids = get_role_ids(data)

    # Parse flags
    role_id = None
    local_folder = None
    remaining_args = []
    i = 0
    while i < len(args):
        if args[i] == "--role" and i + 1 < len(args):
            role_id = resolve_role(data, args[i + 1])
            i += 2
        elif args[i] == "--folder" and i + 1 < len(args):
            local_folder = args[i + 1]
            i += 2
        else:
            remaining_args.append(args[i])
            i += 1

    if remaining_args:
        # Direct mode: create from URL/ref (normalize handles bare numbers)
        issue_ref = remaining_args[0]
    else:
        # Interactive mode: list assigned issues
        repo = data.get("config", {}).get("github_repo")
        if not repo:
            print(c("No default repo configured.", "red"))
            print("Set with: wt config github-repo owner/repo")
            sys.exit(1)

        print(c(f"Fetching issues from {repo}...", "dim"))

        # Get issues assigned to the current user
        result = subprocess.run(
            ["gh", "issue", "list", "-R", repo, "--assignee", "@me",
             "--json", "number,title,state"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(c(f"Error fetching issues: {result.stderr}", "red"))
            sys.exit(1)

        issues = json.loads(result.stdout)
        if not issues:
            print(c("No issues assigned to you.", "dim"))
            sys.exit(0)

        print()
        for i, issue in enumerate(issues, 1):
            state_color = "green" if issue["state"] == "OPEN" else "dim"
            state_str = c(f"[{issue['state'].lower()}]", state_color)
            print(f"  {i}. {state_str} {issue['title']} (#{issue['number']})")
        print()

        # Prompt for selection
        try:
            choice = input(f"Select issue (1-{len(issues)}) or q to cancel: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        if choice.lower() == "q" or not choice:
            sys.exit(0)

        try:
            idx = int(choice) - 1
            if idx < 0 or idx >= len(issues):
                print(c("Invalid selection.", "red"))
                sys.exit(1)
        except ValueError:
            print(c("Invalid selection.", "red"))
            sys.exit(1)

        selected = issues[idx]
        issue_ref = f"{repo}#{selected['number']}"

        # Prompt for role if not specified via --role
        if role_id is None:
            print(c("\n  Select role:\n", "bold"))
            for j, r in enumerate(data.get("roles", []), 1):
                print(f"    {j}. {r['label']} ({r['id']})")
            print()

            try:
                role_choice = input(f"  Role (1-{len(data['roles'])}): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                sys.exit(0)

            if role_choice:
                try:
                    role_idx = int(role_choice) - 1
                    if 0 <= role_idx < len(data["roles"]):
                        role_id = data["roles"][role_idx]["id"]
                    else:
                        print(c("Invalid selection, using 'other'.", "yellow"))
                        role_id = "other"
                except ValueError:
                    print(c("Invalid selection, using 'other'.", "yellow"))
                    role_id = "other"
            else:
                role_id = "other"

    # Default to 'other' if no role specified
    if role_id is None:
        role_id = "other"

    # Create the task (status: To Do) linked to the existing issue.
    res = create_task_from_issue(
        data, issue_ref, role_id=role_id, local_folder=local_folder, status="todo"
    )
    if res["error"]:
        print(c(res["error"], "red"))
        print(c("  Make sure the issue exists and you have access.", "dim"))
        sys.exit(1)
    if res["existed"]:
        t = res["task"]
        print(c("Task already exists for this issue:", "yellow"))
        print(f"  {t['title']} (id: {t['id']})")
        sys.exit(0)

    save(data)
    task = res["task"]
    print(c(f"✓ Created: {task['title']}", "green"))
    print(f"  [{roles.get(role_id, role_id)}] [{STATUS_LABELS.get(task['status'], task['status'])}]")
    print(c(f"  id: {task['id']}", "dim"))
    print(c(f"  GitHub: {task_current_issue(task, data)}", "cyan"))
    if task.get("local_folder"):
        print(c(f"  Folder: {task['local_folder']}", "dim"))


def _cli_tab_cleanup_prompt(unrelated_tabs):
    """CLI callback to prompt user about closing unrelated tabs."""
    if not unrelated_tabs:
        return []

    print(c("\n  Potentially unrelated tabs:", "yellow"))
    for i, tab_info in enumerate(unrelated_tabs, 1):
        print(f"    {i}. {tab_info['title'][:50]}")
        print(c(f"       {tab_info['url'][:60]}", "dim"))
        print(c(f"       Reason: {tab_info['reason']}", "dim"))

    try:
        response = input("\n  Close these tabs? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return []

    if response in ("y", "yes"):
        return unrelated_tabs
    return []


def cmd_arc(args):
    """Manage Arc browser integration."""
    if not args:
        print("Usage: wt arc <setup|status|sync|link|spaces>")
        sys.exit(1)

    subcmd = args[0].lower()

    if subcmd == "spaces":
        # List all Arc spaces
        try:
            from arc_browser import ArcSidebarManager
        except ImportError as e:
            print(c(f"Error: {e}", "red"))
            sys.exit(1)

        sidebar = ArcSidebarManager()
        spaces = sidebar.list_spaces()

        print(c("\n  Arc Spaces:\n", "bold"))
        for space in spaces:
            print(f"  {space['title']:<30} {space['id']}")
        print()
        print(c("  Use 'wt arc link <space-name>' to link to a space", "dim"))
        return

    if subcmd == "link":
        # Link to an existing space by name
        if len(args) < 2:
            print("Usage: wt arc link <space-name>")
            print("  Links to an existing Arc space (create it in Arc first)")
            sys.exit(1)

        space_name = " ".join(args[1:])

        try:
            from arc_browser import ArcSidebarManager
        except ImportError as e:
            print(c(f"Error: {e}", "red"))
            sys.exit(1)

        data = load()
        sidebar = ArcSidebarManager()

        space = sidebar.find_space_by_name(space_name)
        if not space:
            print(c(f"Space '{space_name}' not found.", "red"))
            print("Available spaces:")
            for s in sidebar.list_spaces():
                print(f"  {s['title']}")
            sys.exit(1)

        # Store the space ID
        data.setdefault("config", {})["arc_space_id"] = space["id"]
        data["config"]["tab_cleanup_enabled"] = True
        save(data)

        print(c(f"✓ Linked to space: {space_name}", "green"))
        print(c(f"  Space ID: {space['id']}", "dim"))
        print(c("  Tab cleanup enabled", "dim"))
        print()
        print("Now run 'wt arc sync' to create role folders.")
        return

    if subcmd == "setup":
        try:
            from arc_browser import TaskTabManager, ArcAppleScript, ArcSidebarManager, prompt_arc_restart
        except ImportError as e:
            print(c(f"Error: {e}", "red"))
            sys.exit(1)

        data = load()
        applescript = ArcAppleScript()
        sidebar = ArcSidebarManager()

        # Check for Arc Sync
        if sidebar.is_sync_enabled():
            print(c("Warning: Arc Sync appears to be enabled.", "yellow"))
            print()
            print("Arc Sync may overwrite local changes when Arc launches.")
            print("Recommended approach:")
            print("  1. Create the space manually in Arc (click + > New Space)")
            print("  2. Name it 'Workload Tracker'")
            print("  3. Run: wt arc link 'Workload Tracker'")
            print()
            print("Or disable Arc Sync temporarily in Arc Settings > Sync & Profiles.")
            print()
            try:
                response = input("Continue anyway? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                sys.exit(0)
            if response not in ("y", "yes"):
                sys.exit(0)

        # Check if Arc is running and try to close it
        import time as t
        if applescript.is_arc_running():
            print("Closing Arc...")
            applescript.quit_arc()
            t.sleep(2)

            # Wait for Arc to close
            attempts = 0
            while applescript.is_arc_running() and attempts < 5:
                t.sleep(1)
                attempts += 1

            # If still running, ask user
            if applescript.is_arc_running():
                print(c("Arc is still running.", "yellow"))
                print()
                print("Options:")
                print("  1. I'll close it myself")
                print("  2. Cancel")
                print()
                try:
                    response = input("Choose [1/2]: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    sys.exit(0)

                if response == "1":
                    print("Please close Arc completely, then press Enter...")
                    try:
                        input()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        sys.exit(0)

                    # Verify Arc is closed
                    if applescript.is_arc_running():
                        print(c("Error: Arc is still running.", "red"))
                        sys.exit(1)
                else:
                    print("Cancelled.")
                    sys.exit(0)

            print(c("✓ Arc is closed", "green"))

        # Clear any old Arc IDs before setup
        if data.get("config", {}).get("arc_space_id"):
            del data["config"]["arc_space_id"]
        for role in data.get("roles", []):
            if "arc_folder_id" in role:
                del role["arc_folder_id"]
        for task in data.get("tasks", []):
            if "arc_folder_id" in task:
                del task["arc_folder_id"]
        save(data)

        manager = TaskTabManager(data)

        # Step 1: Create the space via JSON (requires Arc to be quit)
        print("Creating Workload Tracker space...")
        result = manager.setup_space_and_folders(save)

        if result.get("errors"):
            for err in result["errors"]:
                print(c(f"  Error: {err}", "red"))
            sys.exit(1)

        print(c(f"✓ Created space: {result['space_id']}", "green"))

        # Enable tab cleanup by default
        data.setdefault("config", {})["tab_cleanup_enabled"] = True
        save(data)
        print(c("✓ Tab cleanup enabled", "green"))

        # Step 2: Launch Arc and create role folders via UI scripting
        print()
        print("Now launching Arc to create role folders via UI...")
        print(c("(This works with Arc Sync)", "dim"))

        applescript.launch_arc()
        import time as t
        t.sleep(2)

        # Create role folders using UI scripting
        role_labels = [r["label"] for r in data.get("roles", [])]
        created = applescript.create_folders_in_space("Workload Tracker", role_labels)

        if created == len(role_labels):
            print(c(f"✓ Created {created} role folders", "green"))
        else:
            print(c(f"Created {created}/{len(role_labels)} role folders", "yellow"))

        # Look up folder IDs from Arc's sidebar (with retry)
        print("Linking folder IDs...")
        t.sleep(2)  # Give Arc time to write sidebar

        sidebar = ArcSidebarManager()
        linked_count = 0
        for attempt in range(3):
            try:
                arc_data = sidebar.load_sidebar()
                container = arc_data['sidebar']['containers'][1]
                items = container.get('items', [])

                for role in data.get("roles", []):
                    if "arc_folder_id" in role:
                        continue  # Already linked
                    for item in items:
                        if (isinstance(item, dict) and
                            item.get("title") == role["label"] and
                            "list" in item.get("data", {})):
                            role["arc_folder_id"] = item["id"]
                            print(c(f"  ✓ Linked: {role['label']}", "dim"))
                            linked_count += 1
                            break

                if linked_count == len(role_labels):
                    break
                elif attempt < 2:
                    t.sleep(1)
            except Exception as e:
                if attempt == 2:
                    print(c(f"  Warning: Could not link folder IDs: {e}", "yellow"))

        save(data)

        # Step 3: Create nested folders for existing tasks
        active_tasks = [t for t in data.get("tasks", []) if t.get("status") != "done"]
        if active_tasks:
            print()
            print(f"Creating nested folders for {len(active_tasks)} active tasks...")

            # Build role label lookup
            role_lookup = {r["id"]: r["label"] for r in data.get("roles", [])}

            task_folders_created = 0
            for task in active_tasks:
                role_id = task.get("role_id", "other")
                role_label = role_lookup.get(role_id, "Other")

                print(c(f"  Creating: {task['title'][:40]}...", "dim") if len(task['title']) > 40 else c(f"  Creating: {task['title']}", "dim"))

                if applescript.create_nested_folder_by_name(task["title"], role_label):
                    task_folders_created += 1
                    t.sleep(0.3)  # Brief pause between folders
                else:
                    print(c(f"    Failed to create folder", "yellow"))

            if task_folders_created == len(active_tasks):
                print(c(f"✓ Created {task_folders_created} task folders", "green"))
            else:
                print(c(f"Created {task_folders_created}/{len(active_tasks)} task folders", "yellow"))

            # Link task folder IDs
            t.sleep(1)
            try:
                arc_data = sidebar.load_sidebar()
                container = arc_data['sidebar']['containers'][1]
                items = container.get('items', [])

                for task in active_tasks:
                    for item in items:
                        if (isinstance(item, dict) and
                            item.get("title") == task["title"] and
                            "list" in item.get("data", {})):
                            task["arc_folder_id"] = item["id"]
                            break
            except Exception:
                pass  # Non-fatal

            save(data)

        print()
        print(c("✓ Setup complete!", "green", "bold"))

    elif subcmd == "status":
        try:
            from arc_browser import TaskTabManager
        except ImportError as e:
            print(c(f"Error: {e}", "red"))
            sys.exit(1)

        data = load()
        manager = TaskTabManager(data)
        status = manager.get_status()

        print(c("\n  Arc Integration Status\n", "bold"))
        print(f"  Enabled:            {'Yes' if status['enabled'] else 'No'}")
        print(f"  Space ID:           {status['space_id'] or '(not set)'}")
        print(f"  Tab cleanup:        {'On' if status['tab_cleanup_enabled'] else 'Off'}")
        print(f"  Confidence:         {status['confidence_threshold']:.0%}")
        print(f"  Arc running:        {'Yes' if status['arc_running'] else 'No'}")
        print(f"  Role folders:       {status['role_folders']}")
        print(f"  Task folders:       {status['task_folders']}")
        print()

    elif subcmd == "sync":
        try:
            from arc_browser import TaskTabManager, ArcAppleScript, prompt_arc_restart
        except ImportError as e:
            print(c(f"Error: {e}", "red"))
            sys.exit(1)

        data = load()
        applescript = ArcAppleScript()

        if applescript.is_arc_running():
            print(c("Warning: Arc is running.", "yellow"))
            print("Sync requires Arc to be quit first for folder changes.")
            try:
                response = input("Quit Arc now? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                sys.exit(0)
            if response not in ("n", "no"):
                print("Quitting Arc...")
                applescript.quit_arc()
                import time as t
                t.sleep(1)

        manager = TaskTabManager(data)
        print("Syncing folders...")
        result = manager.sync_folders(save)

        if result.get("errors"):
            for err in result["errors"]:
                print(c(f"  Error: {err}", "red"))

        print(c(f"✓ Synced {result['roles_synced']} roles, {result['tasks_synced']} tasks", "green"))

        if result.get("restart_required"):
            prompt_arc_restart()

    else:
        print(c(f"Unknown arc subcommand: {subcmd}", "red"))
        print("Usage: wt arc <setup|status|sync>")
        sys.exit(1)


# ── iTerm2/tmux Integration ───────────────────────────────

def cmd_iterm(args):
    """Manage iTerm2/tmux integration."""
    if not args:
        print("Usage: wt iterm <command>")
        print()
        print("Commands:")
        print("  open <task>              Open iTerm2 terminal for a task")
        print("  close <task>             Close tmux session for a task")
        print("  set-folder <task> <path> Set local folder (e.g., git repo) for task")
        print("  clear-folder <task>      Clear local folder setting")
        print("  status                   Show integration status")
        print("  setup                    Enable iTerm integration")
        sys.exit(1)

    subcmd = args[0].lower()

    if subcmd == "setup":
        try:
            from iterm_manager import TaskTerminalManager
        except ImportError as e:
            print(c(f"Error: {e}", "red"))
            sys.exit(1)

        data = load()
        manager = TaskTerminalManager(data)

        # Optional: custom projects directory
        projects_dir = None
        if len(args) > 1:
            projects_dir = args[1]

        result = manager.setup(save, projects_dir)

        if result["error"]:
            print(c(f"Error: {result['error']}", "red"))
            sys.exit(1)

        print(c("✓ iTerm integration enabled", "green"))
        print(f"  Projects directory: {result['projects_dir']}")
        if result["created_dir"]:
            print(c("  (created directory)", "dim"))
        print()
        print("Press 'i' in TUI or use 'wt iterm open <task>' to open a terminal.")

    elif subcmd == "status":
        try:
            from iterm_manager import TaskTerminalManager
        except ImportError as e:
            print(c(f"Error: {e}", "red"))
            sys.exit(1)

        data = load()
        manager = TaskTerminalManager(data)
        status = manager.get_status()

        print(c("\n  iTerm Integration Status\n", "bold"))
        print(f"  Enabled:            {'Yes' if status['enabled'] else 'No'}")
        print(f"  Projects directory: {status['projects_dir']}")
        print(f"  Directory exists:   {'Yes' if status['projects_dir_exists'] else 'No'}")
        print(f"  iTerm running:      {'Yes' if status['iterm_running'] else 'No'}")
        print(f"  Tasks with sessions:{status['tasks_with_sessions']}")
        print(f"  Active sessions:    {status['active_sessions']}")
        if status['session_names']:
            print(f"  Sessions:           {', '.join(status['session_names'])}")
        print()

    elif subcmd == "open":
        if len(args) < 2:
            print("Usage: wt iterm open <task>")
            sys.exit(1)

        try:
            from iterm_manager import TaskTerminalManager
        except ImportError as e:
            print(c(f"Error: {e}", "red"))
            sys.exit(1)

        data = load()
        manager = TaskTerminalManager(data)

        # Check if enabled
        if not manager.is_enabled():
            print(c("iTerm integration not enabled.", "red"))
            print("Run 'wt iterm setup' first.")
            sys.exit(1)

        task_query = " ".join(args[1:])
        task = resolve_task(data, task_query)
        if not task:
            print(c(f"Task not found: {task_query}", "red"))
            sys.exit(1)

        print(f"Opening terminal for: {task['title']}")
        result = manager.open_terminal(task, save)

        if result["error"]:
            print(c(f"Error: {result['error']}", "red"))
            sys.exit(1)

        if result["session_created"]:
            print(c(f"✓ Created session: {result['session_name']}", "green"))
        else:
            print(c(f"✓ Opened session: {result['session_name']}", "green"))
        print(f"  Folder: {result['folder_path']}")

    elif subcmd == "close":
        if len(args) < 2:
            print("Usage: wt iterm close <task>")
            sys.exit(1)

        try:
            from iterm_manager import TaskTerminalManager
        except ImportError as e:
            print(c(f"Error: {e}", "red"))
            sys.exit(1)

        data = load()
        manager = TaskTerminalManager(data)

        task_query = " ".join(args[1:])
        task = resolve_task(data, task_query)
        if not task:
            print(c(f"Task not found: {task_query}", "red"))
            sys.exit(1)

        result = manager.close_session(task)

        if result["error"]:
            print(c(f"Error: {result['error']}", "red"))
            sys.exit(1)

        print(c(f"✓ Closed session for: {task['title']}", "green"))

    elif subcmd == "set-folder":
        if len(args) < 3:
            print("Usage: wt iterm set-folder <task> <path>")
            print("  Sets a local folder (e.g., git repo) for the task's terminal session")
            sys.exit(1)

        data = load()
        task_query = args[1]
        folder_path = args[2]

        task = resolve_task(data, task_query)
        if not task:
            print(c(f"Task not found: {task_query}", "red"))
            sys.exit(1)

        # Expand and validate path
        folder = Path(folder_path).expanduser().resolve()
        if not folder.exists():
            print(c(f"Folder does not exist: {folder}", "red"))
            sys.exit(1)
        if not folder.is_dir():
            print(c(f"Path is not a directory: {folder}", "red"))
            sys.exit(1)

        task["local_folder"] = str(folder)
        save(data)

        print(c(f"✓ Set local folder for: {task['title']}", "green"))
        print(f"  Folder: {folder}")

    elif subcmd == "clear-folder":
        if len(args) < 2:
            print("Usage: wt iterm clear-folder <task>")
            sys.exit(1)

        data = load()
        task_query = " ".join(args[1:])

        task = resolve_task(data, task_query)
        if not task:
            print(c(f"Task not found: {task_query}", "red"))
            sys.exit(1)

        if "local_folder" in task:
            del task["local_folder"]
            save(data)
            print(c(f"✓ Cleared local folder for: {task['title']}", "green"))
        else:
            print(c("No local folder was set for this task", "dim"))

    else:
        print(c(f"Unknown iterm subcommand: {subcmd}", "red"))
        print("Usage: wt iterm <open|close|status|setup|set-folder|clear-folder>")
        sys.exit(1)


# ── Google Calendar Integration ───────────────────────────

GCAL_CREDENTIALS_FILE = Path.home() / ".workload_tracker_gcal_credentials.json"
GCAL_TOKEN_FILE = Path.home() / ".workload_tracker_gcal_token.json"


def get_gcal_service():
    """Get authenticated Google Calendar service.

    Returns the service object or None if not authenticated.
    """
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        print(c("Google Calendar API not installed.", "red"))
        print("Install with: pip install google-api-python-client google-auth-oauthlib")
        return None

    SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
    creds = None

    # Load existing token
    if GCAL_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(GCAL_TOKEN_FILE), SCOPES)

    # Refresh or get new credentials
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not GCAL_CREDENTIALS_FILE.exists():
                print(c("Google Calendar credentials not found.", "red"))
                print()
                print("Setup instructions:")
                print("  1. Go to https://console.cloud.google.com/")
                print("  2. Create a project (or select existing) and enable 'Google Calendar API'")
                print("  3. Go to 'APIs & Services' > 'Credentials'")
                print("  4. Find your OAuth 2.0 Client ID (Desktop app), or create one")
                print("  5. Create a new client secret and download the JSON file")
                print("  6. Save as:")
                print(f"     {GCAL_CREDENTIALS_FILE}")
                print()
                print("Then run 'wt calendar' - browser opens for authorization.")
                return None

            flow = InstalledAppFlow.from_client_secrets_file(
                str(GCAL_CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)

        # Save token for next time
        GCAL_TOKEN_FILE.write_text(creds.to_json())

    return build('calendar', 'v3', credentials=creds)


def get_calendar_events(days_back: int = 1, calendar_id: str = "primary",
                        start_date=None, end_date=None) -> list[dict]:
    """Get events from Google Calendar for the specified date range.

    Args:
        days_back: Number of days to look back (default 1 = yesterday + today).
            Ignored if explicit start_date/end_date are provided.
        calendar_id: Google Calendar ID (default "primary", or email like "user@domain.com")
        start_date: Optional datetime.date for the inclusive range start.
        end_date: Optional datetime.date for the inclusive range end.

    Returns list of event dicts: {title, start_date, end_date, calendar_name, notes, duration_mins, uid}
    """
    service = get_gcal_service()
    if not service:
        return []

    # Calculate time range
    if start_date is not None and end_date is not None:
        # Whole-day boundaries for the provided date range.
        start_dt = datetime.combine(start_date, datetime.min.time())
        end_dt = datetime.combine(end_date, datetime.max.time()).replace(microsecond=0)
    else:
        now = datetime.now()
        start_dt = (now - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = now.replace(hour=23, minute=59, second=59, microsecond=0)

    # Convert to RFC3339 format
    start_str = start_dt.isoformat() + 'Z'
    end_str = end_dt.isoformat() + 'Z'

    try:
        events_result = service.events().list(
            calendarId=calendar_id,
            timeMin=start_str,
            timeMax=end_str,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
    except Exception as e:
        print(c(f"Error fetching calendar: {e}", "red"))
        return []

    events = []
    for item in events_result.get('items', []):
        # Skip all-day events (they have 'date' instead of 'dateTime')
        start_info = item.get('start', {})
        end_info = item.get('end', {})

        if 'dateTime' not in start_info:
            continue  # Skip all-day events

        # Parse timestamps
        start_dt_str = start_info.get('dateTime', '')
        end_dt_str = end_info.get('dateTime', '')

        try:
            # Handle timezone-aware ISO format
            start_dt = datetime.fromisoformat(start_dt_str.replace('Z', '+00:00'))
            end_dt = datetime.fromisoformat(end_dt_str.replace('Z', '+00:00'))

            # Convert to Unix timestamps
            start_ts = start_dt.timestamp()
            end_ts = end_dt.timestamp()
            duration_mins = (end_ts - start_ts) / 60
        except (ValueError, TypeError):
            continue

        events.append({
            "title": item.get('summary', '(No title)'),
            "start_date": start_ts,
            "end_date": end_ts,
            "calendar_name": calendar_id,
            "duration_mins": duration_mins,
            "uid": item.get('id', ''),
            "notes": item.get('description', ''),
        })

    return events


def cmd_calendar(args):
    """Import tasks from Google Calendar events."""
    data = load()
    config = data.get("config", {})

    # Get calendar ID from config (default to primary)
    calendar_id = config.get("calendar_id", "primary")

    # Check for subcommand
    if args and args[0].lower() == "setup":
        # Show setup instructions
        print(c("\n  Google Calendar Setup\n", "bold"))
        print("  1. Go to https://console.cloud.google.com/")
        print("  2. Create a project (or select existing) and enable 'Google Calendar API'")
        print("  3. Go to 'APIs & Services' > 'Credentials'")
        print("  4. Find your OAuth 2.0 Client ID (Desktop app), or create one")
        print("  5. Create a new client secret and download the JSON file")
        print("  6. Save as:")
        print(c(f"     {GCAL_CREDENTIALS_FILE}", "cyan"))
        print()
        print("  7. Run 'wt calendar' - browser opens for authorization")
        print()
        print("  Optional: Set a specific calendar ID:")
        print("    wt config calendar_id your.email@gmail.com")
        print()
        print(f"  Current calendar: {c(calendar_id, 'cyan')}")
        print(f"  Credentials file: {'Found' if GCAL_CREDENTIALS_FILE.exists() else c('Not found', 'red')}")
        print(f"  Token file: {'Found' if GCAL_TOKEN_FILE.exists() else 'Not found'}")
        print()
        return

    if args and args[0].lower() == "mappings":
        # List all event->task mappings
        mappings = config.get("calendar_event_mappings", {})
        if not mappings:
            print(c("No calendar event mappings configured.", "dim"))
            print("Use 'wt calendar map \"Event Title\" <task>' to create one.")
            return

        print(c("\n  Calendar Event Mappings\n", "bold"))
        for event_title, base_name in mappings.items():
            print(f"  {c(event_title, 'cyan')} → {base_name}")
        print()
        return

    if args and args[0].lower() == "map":
        # Create/update mapping: wt calendar map "Event Title" <task>
        if len(args) < 3:
            print("Usage: wt calendar map \"Event Title\" <task>")
            sys.exit(1)

        event_title = args[1]
        task_query = " ".join(args[2:])
        task = resolve_task(data, task_query)
        if not task:
            print(c(f"No task found matching '{task_query}'", "red"))
            sys.exit(1)

        new_base = strip_sprint_suffix(task["title"])

        # Check if already mapped
        existing_mapping = get_event_mapping(data, event_title)
        if existing_mapping:
            print(f"Updating mapping: '{event_title}' → '{existing_mapping}' => '{new_base}'")
        else:
            print(f"Creating mapping: '{event_title}' → '{new_base}'")

        set_event_mapping(data, event_title, new_base)
        save(data)
        print(c("✓ Mapping saved", "green"))
        return

    if args and args[0].lower() == "unmap":
        # Remove mapping: wt calendar unmap "Event Title"
        if len(args) < 2:
            print("Usage: wt calendar unmap \"Event Title\"")
            sys.exit(1)

        event_title = " ".join(args[1:])
        if remove_event_mapping(data, event_title):
            save(data)
            print(c(f"✓ Removed mapping for '{event_title}'", "green"))
        else:
            print(c(f"No mapping found for '{event_title}'", "yellow"))
        return

    if args and args[0].lower() == "import":
        # Import mode: wt calendar import <event-title> [--task <task-name>]
        if len(args) < 2:
            print("Usage: wt calendar import <event-title> [--task <task-name>]")
            sys.exit(1)

        # Parse --task flag
        target_task = None
        remaining_args = args[1:]
        if "--task" in remaining_args:
            idx = remaining_args.index("--task")
            if idx + 1 >= len(remaining_args):
                print(c("--task requires a task name", "red"))
                sys.exit(1)
            task_query = " ".join(remaining_args[idx + 1:])
            remaining_args = remaining_args[:idx]
            target_task = resolve_task(data, task_query)
            if not target_task:
                print(c(f"No task found matching '{task_query}'", "red"))
                sys.exit(1)

        if not remaining_args:
            print("Usage: wt calendar import <event-title> [--task <task-name>]")
            sys.exit(1)

        query = " ".join(remaining_args)

        # Get events to find a match
        events = get_calendar_events(days_back=7, calendar_id=calendar_id)  # Search wider range for import

        # Check which events are already imported
        imported_uids = get_imported_calendar_uids(data)

        # Find matching event (case-insensitive partial match)
        q = query.lower()
        matches = [e for e in events if q in e["title"].lower() and e["uid"] not in imported_uids]

        if not matches:
            # Check if it was already imported
            already = [e for e in events if q in e["title"].lower() and e["uid"] in imported_uids]
            if already:
                print(c(f"Event '{already[0]['title']}' was already imported.", "yellow"))
            else:
                print(c(f"No matching event found for '{query}'", "red"))
            sys.exit(1)

        if len(matches) > 1:
            print(c("Multiple matches found:", "yellow"))
            for e in matches:
                start = datetime.fromtimestamp(e["start_date"])
                print(f"  {start.strftime('%m/%d %H:%M')}  {e['title']} ({fmt_mins(e['duration_mins'])})")
            print(c("Be more specific.", "dim"))
            sys.exit(1)

        event = matches[0]

        # Show event details
        start_dt = datetime.fromtimestamp(event["start_date"])
        end_dt = datetime.fromtimestamp(event["end_date"])
        duration = event["duration_mins"]

        # Check for mapping if no --task flag provided
        mapped_task = None
        if not target_task:
            mapped_task_id = get_event_mapping(data, event["title"])
            if mapped_task_id:
                # Use the sprint-aware resolver so a recurring "X - Sprint NN"
                # task picks the sprint matching the event's date.
                mapped_task = resolve_event_to_task(data, event)
                if mapped_task:
                    # Show quick confirmation for mapped event
                    print(c(f"\n  Event: {event['title']}", "bold"))
                    print(f"  Duration: {fmt_mins(duration)}")
                    print(f"  Mapped to: {c(mapped_task['title'], 'cyan')}")
                    print()
                    print(f"  Log {fmt_mins(duration)} to '{mapped_task['title']}'?")
                    try:
                        choice = input("  [Y/n/minutes/other]: ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        sys.exit(0)

                    if choice == "other":
                        # Fall through to normal flow (unmapped)
                        mapped_task = None
                    elif choice == "n":
                        print(c("Skipped.", "dim"))
                        return
                    else:
                        # Parse minutes or use default
                        if choice == "" or choice == "y":
                            log_minutes = duration
                        else:
                            try:
                                log_minutes = float(choice)
                            except ValueError:
                                print(c("Invalid input, logging full duration.", "yellow"))
                                log_minutes = duration

                        # Log to mapped task
                        mapped_task["logs"].append({
                            "id": uid(),
                            "minutes": round(log_minutes, 2),
                            "note": f"Calendar: {event['title']}",
                            "at": event["end_date"],
                            "started_at": event["start_date"],
                            "ended_at": event["end_date"],
                            "calendar_event_uid": event["uid"],
                        })
                        save(data)
                        print(c(f"\n✓ Logged {fmt_mins(log_minutes)} to '{mapped_task['title']}'", "green"))
                        return
                else:
                    # Mapped task was deleted, warn user
                    print(c(f"Warning: Mapped task was deleted. Falling back to normal flow.", "yellow"))

        # Normal flow (no mapping or mapping bypassed)
        print(c(f"\n  Event: {event['title']}", "bold"))
        print(f"  Date:     {start_dt.strftime('%Y-%m-%d')}")
        print(f"  Time:     {start_dt.strftime('%H:%M')} - {end_dt.strftime('%H:%M')}")
        print(f"  Duration: {fmt_mins(duration)}")
        print(f"  Calendar: {event['calendar_name']}")
        print()

        # Prompt for time logging
        print()
        print(f"  Log {fmt_mins(duration)} of time?")
        try:
            time_choice = input("  [Y/n/minutes]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        log_minutes = None
        if time_choice == "" or time_choice == "y":
            log_minutes = duration
        elif time_choice != "n":
            try:
                log_minutes = float(time_choice)
            except ValueError:
                print(c("Invalid input, logging full duration.", "yellow"))
                log_minutes = duration

        if target_task:
            # Log to existing task
            if log_minutes and log_minutes > 0:
                target_task["logs"].append({
                    "id": uid(),
                    "minutes": round(log_minutes, 2),
                    "note": f"Calendar: {event['title']}",
                    "at": event["end_date"],
                    "started_at": event["start_date"],
                    "ended_at": event["end_date"],
                    "calendar_event_uid": event["uid"],
                })
                save(data)
                print(c(f"\n✓ Logged {fmt_mins(log_minutes)} to '{target_task['title']}'", "green"))

                # Offer to save mapping if not already mapped
                existing_mapping = get_event_mapping(data, event["title"])
                if not existing_mapping:
                    try:
                        save_mapping = input("  Save mapping for future events? [Y/n]: ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        return
                    if save_mapping in ("", "y", "yes"):
                        set_event_mapping(data, event["title"], target_task["id"])
                        save(data)
                        print(c(f"  ✓ Mapping saved: '{event['title']}' → '{target_task['title']}'", "green"))
            else:
                print(c("No time to log.", "dim"))
        else:
            # Create new task
            # Prompt for role
            roles = data.get("roles", [])
            print(c("  Select role:", "bold"))
            for i, r in enumerate(roles, 1):
                print(f"    {i}. {r['label']} ({r['id']})")
            print()

            try:
                role_choice = input(f"  Role (1-{len(roles)}): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                sys.exit(0)

            role_id = "other"
            if role_choice:
                try:
                    role_idx = int(role_choice) - 1
                    if 0 <= role_idx < len(roles):
                        role_id = roles[role_idx]["id"]
                except ValueError:
                    pass

            task = {
                "id": uid(),
                "title": event["title"],
                "description": event.get("notes", ""),
                "role_id": role_id,
                "status": "done",
                "logs": [],
                "created_at": time.time(),
                "calendar_event_uid": event["uid"],
            }

            if log_minutes and log_minutes > 0:
                task["logs"].append({
                    "id": uid(),
                    "minutes": round(log_minutes, 2),
                    "note": f"From calendar: {event['calendar_name']}",
                    "at": event["end_date"],
                    "started_at": event["start_date"],
                    "ended_at": event["end_date"],
                })

            data["tasks"].insert(0, task)
            save(data)

            role_label = get_roles(data).get(role_id, role_id)
            print(c(f"\n✓ Created: {task['title']}", "green"))
            print(f"  [{role_label}] [Done]")
            if log_minutes:
                print(c(f"  Logged: {fmt_mins(log_minutes)}", "dim"))
            print(c(f"  id: {task['id']}", "dim"))
        return

    # List mode: wt calendar [days]
    days_back = 1  # Default: yesterday and today
    if args:
        try:
            days_back = int(args[0])
        except ValueError:
            print(c(f"Invalid number of days: {args[0]}", "red"))
            sys.exit(1)

    events = get_calendar_events(days_back=days_back, calendar_id=calendar_id)

    if not events:
        print(c("No calendar events found.", "dim"))
        print(c(f"  (Calendar: {calendar_id})", "dim"))
        return

    # Check which events are already imported
    imported_uids = get_imported_calendar_uids(data)

    # Group events by date
    events_by_date = {}
    for e in events:
        date_key = datetime.fromtimestamp(e["start_date"]).strftime("%Y-%m-%d")
        events_by_date.setdefault(date_key, []).append(e)

    print(c(f"\n  Calendar events (past {days_back} day{'s' if days_back != 1 else ''}):\n", "bold"))

    for date_key in sorted(events_by_date.keys(), reverse=True):
        date_dt = datetime.strptime(date_key, "%Y-%m-%d")
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        if date_key == today:
            date_label = "Today"
        elif date_key == yesterday:
            date_label = "Yesterday"
        else:
            date_label = date_dt.strftime("%A, %b %d")

        print(c(f"  {date_label}", "cyan", "bold"))

        for e in events_by_date[date_key]:
            start_time = datetime.fromtimestamp(e["start_date"]).strftime("%H:%M")
            imported = e["uid"] in imported_uids

            if imported:
                status = c("✓", "green")
                title_fmt = c(e["title"][:45], "dim")
            else:
                status = " "
                title_fmt = e["title"][:45]

            duration = fmt_mins(e["duration_mins"])
            cal_name = c(f"[{e['calendar_name'][:15]}]", "dim")

            print(f"  {status} {start_time}  {title_fmt:<47} {duration:>7}  {cal_name}")

        print()

    # Show help
    not_imported = len([e for e in events if e["uid"] not in imported_uids])
    if not_imported > 0:
        print(c(f"  {not_imported} events available to import.", "dim"))
        print(c("  Use: wt calendar import <event-title> [--task <task-name>]", "dim"))
    else:
        print(c("  All events have been imported.", "dim"))
    print()


def _resolve_report_range(data, positional, sprint_query, last_value):
    """Resolve (start_date, end_date, sprint_dict) for cmd_report / MCP.

    Applies the precedence documented in the plan:
        positional dates > --sprint > --last > current sprint > last 7 days.
    Raises ``ValueError`` with a clear message on bad input.
    """
    if positional and (sprint_query or last_value is not None):
        raise ValueError("Provide either positional dates, --sprint, or --last (not multiple).")
    if sprint_query and last_value is not None:
        raise ValueError("Provide either positional dates, --sprint, or --last (not multiple).")

    if positional:
        if len(positional) != 2:
            raise ValueError("Expected two ISO dates: <start> <end>")
        try:
            start = datetime.strptime(positional[0], "%Y-%m-%d").date()
            end = datetime.strptime(positional[1], "%Y-%m-%d").date()
        except ValueError as e:
            raise ValueError(f"Invalid date (expected YYYY-MM-DD): {e}") from None
        if end < start:
            raise ValueError("End date must be on or after start date.")
        return start, end, None

    if sprint_query:
        sprints = get_cached_sprints(data) or get_all_sprints(data)
        if not sprints:
            raise ValueError("No sprints available (project not configured or query failed).")
        match = _match_sprint(sprints, sprint_query)
        if not match:
            raise ValueError(f"No sprint matching '{sprint_query}'.")
        # Sprint end_date follows the half-open convention; convert to inclusive.
        return match["start_date"], match["end_date"] - timedelta(days=1), match

    if last_value is not None:
        days = _parse_last_arg(last_value)
        today = datetime.now().date()
        return today - timedelta(days=days - 1), today, None

    # Default: current sprint, then 7-day fallback.
    current = get_current_sprint(data)
    if current:
        return current["start_date"], current["end_date"] - timedelta(days=1), current
    today = datetime.now().date()
    return today - timedelta(days=6), today, None


def cmd_report(args):
    """Show logged time across a date range."""
    positional: list[str] = []
    sprint_query: str | None = None
    last_value: str | None = None
    role_filter: str | None = None
    as_json = False

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--sprint":
            if i + 1 >= len(args):
                print(c("--sprint requires a value", "red")); sys.exit(1)
            sprint_query = args[i + 1]
            i += 2
        elif a == "--last":
            if i + 1 >= len(args):
                print(c("--last requires a value", "red")); sys.exit(1)
            last_value = args[i + 1]
            i += 2
        elif a == "--role":
            if i + 1 >= len(args):
                print(c("--role requires a value", "red")); sys.exit(1)
            role_filter = args[i + 1]
            i += 2
        elif a == "--json":
            as_json = True
            i += 1
        elif a in ("-h", "--help"):
            print("Usage: wt report [<start> <end> | --sprint NAME | --last Nd] [--role ROLE] [--json]")
            return
        elif a.startswith("--"):
            print(c(f"Unknown flag: {a}", "red")); sys.exit(1)
        else:
            positional.append(a)
            i += 1

    data = load()

    try:
        start_date, end_date, sprint = _resolve_report_range(
            data, positional, sprint_query, last_value
        )
    except ValueError as e:
        print(c(str(e), "red"), file=sys.stderr)
        sys.exit(1)

    if role_filter is not None:
        if role_filter not in get_role_ids(data):
            print(c(f"Unknown role '{role_filter}'. Known: {', '.join(get_role_ids(data))}", "red"),
                  file=sys.stderr)
            sys.exit(1)

    payload = build_time_report(data, start_date, end_date, sprint=sprint, role_id=role_filter)

    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    print(format_time_report(payload, use_color=True))


def _sprints_for_cli(data: dict) -> tuple[list[dict], bool]:
    """Sprint list for a CLI command, preferring the live fetch.

    Returns ``(sprints, from_cache)``. Falling back to
    ``config.sprints_cache`` keeps ``wt sprint`` / ``wt sync-sprints`` usable
    with no network; both key sets are the same date-object contract, unlike the
    camelCase ``startDate``/``duration`` that only the live fetch emits.
    """
    live = get_all_sprints(data)
    if live:
        return live, False
    return get_cached_sprints(data), True


def _task_group_sprint(task: dict, sprints: list[dict], current: dict | None) -> dict | None:
    """Which sprint *task* should be listed under in ``wt sprint``.

    The current sprint when the task has a binding there, else the task's newest
    resolvable binding, else its legacy ``sprint_id``. None → "Unassigned".
    """
    by_id = {s["id"]: s for s in sprints if s.get("id")}
    bindings = task_sprint_bindings(task, sprints)
    if current and any(b.get("sprint_id") == current["id"] for b in bindings):
        return current
    for b in reversed(bindings):
        s = by_id.get(b.get("sprint_id"))
        if s:
            return s
    return by_id.get(task.get("sprint_id"))


def cmd_sprint(args):
    """Show current sprint info and active tasks grouped by their sprint bindings."""
    data = load()
    all_sprints, from_cache = _sprints_for_cli(data)
    today = datetime.now().date()
    current = find_sprint_for_date(all_sprints, today) if all_sprints else None

    if not all_sprints:
        print(c("\n  No sprints found (project not configured or query failed).", "yellow"))
        return
    if from_cache:
        print(c("\n  (offline — using the persisted sprints cache)", "dim"))

    if current:
        print(c(f"\n  Current sprint: {current['title']}", "bold", "cyan"))
        # Read start_date/end_date (date objects), which both get_all_sprints()
        # and get_cached_sprints() provide. The old code read current["startDate"]
        # and current["duration"] — camelCase keys only the live fetch emits — so
        # a cache-backed sprint dict raised KeyError here (plan §1.7 neighbour).
        last_day = current["end_date"] - timedelta(days=1)
        days = (current["end_date"] - current["start_date"]).days
        print(c(f"  {current['start_date']} → {last_day}  ({days} days)", "dim"))
    else:
        print(c("\n  No active sprint right now.", "yellow"))

    tasks = [t for t in data.get("tasks", []) if t.get("status") != "done"]
    if not tasks:
        print(c("  No active tasks.", "dim"))
        print()
        return

    at = data.get("active_timer")
    groups: dict[str | None, list] = {}
    for t in tasks:
        group = _task_group_sprint(t, all_sprints, current)
        groups.setdefault(group["id"] if group else None, []).append(t)

    by_id = {s["id"]: s for s in all_sprints if s.get("id")}

    def group_key(sid):
        # Newest sprint first; "Unassigned" always last.
        s = by_id.get(sid)
        return (1, None) if s is None else (0, -s["start_date"].toordinal())

    for sid in sorted(groups, key=group_key):
        sprint = by_id.get(sid)
        label = sprint["title"] if sprint else "Unassigned"
        if sprint:
            label += f"  ({sprint['start_date']} → {sprint['end_date'] - timedelta(days=1)})"
        print(c(f"\n  {label}", "bold"))
        for t in groups[sid]:
            total = task_logged_mins(t) + task_live_mins(t, at)
            in_sprint = (task_mins_for_sprint(t, sid, all_sprints) if sid else 0.0)
            in_sprint += task_live_mins(t, at) if sid and sid == (current or {}).get("id") else 0
            running = at and at.get("task_id") == t["id"]
            dot = c("▶ ", "green") if running else "  "
            notes = []
            if sid and abs(in_sprint - total) > 1e-9:
                notes.append(f"total {fmt_mins(total)}")
            start = task_start_sprint(t, all_sprints)
            group = by_id.get(sid)
            if (start and group and start["id"] != sid
                    and start["start_date"] < group["start_date"]):
                # Carry-over: the work began in a *strictly earlier* sprint
                # (plan §3). A start sprint that is later than the group sprint
                # means the task's binding is behind its logs — that shows up as
                # the "total" note instead, and `wt sync-sprints` fixes it.
                notes.append(f"started {start['title']}")
            suffix = c("  (" + ", ".join(notes) + ")", "dim") if notes else ""
            shown = fmt_mins(in_sprint) if sid else fmt_mins(total)
            print(f"    {dot}{t['title'][:50]:<52} {shown}{suffix}")
    print()


def cmd_set_sprint(args):
    """Correct the sprint a task *started* in (plan §2.1/§3).

    Since Phase 3, which sprint a task's hours are billed to is derived from its
    log timestamps and materialised as ``sprint_issues`` bindings by
    ``wt sync-sprints`` — it is not something you set by hand any more. The one
    sprint field a human still owns is ``start_sprint``: "when did this work
    begin", used for the "started Sprint N" carry-over marker in ``wt sprint``.
    It is derived from the earliest log on first migration and then frozen, so a
    later log edit can't silently rewrite history; this command is the override.
    """
    if len(args) < 2:
        print("Usage: wt set-sprint <task> <sprint-title|none>")
        print("  Corrects the sprint the task STARTED in (start_sprint).")
        print("  Hours are attributed from log timestamps — run 'wt sync-sprints <task>'.")
        sys.exit(1)

    data = load()
    all_sprints, _ = _sprints_for_cli(data)
    if not all_sprints:
        print(c("No sprints found.", "red")); sys.exit(1)

    # First arg is task, rest is sprint title
    task = resolve_task(data, args[0])
    sprint_query = " ".join(args[1:])

    if sprint_query.lower() == "none":
        had = task.pop("start_sprint_id", None) is not None
        had = (task.pop("start_sprint", None) is not None) or had
        save(data)
        if had:
            print(c(f"✓ Cleared start sprint for '{task['title']}'", "green"))
            print(c("  It will be re-derived from the task's earliest log.", "dim"))
        else:
            print(c(f"'{task['title']}' had no start sprint set.", "dim"))
        return

    match = _match_sprint(all_sprints, sprint_query)
    if not match:
        # Show available sprints
        print(c(f"No sprint matching '{sprint_query}'.", "red"))
        print(c("  Available sprints:", "dim"))
        for s in all_sprints[-10:]:  # Show last 10
            print(c(f"    {s['title']}", "dim"))
        sys.exit(1)

    task["start_sprint"] = match["title"]
    task["start_sprint_id"] = match["id"]
    save(data)
    print(c(f"✓ '{task['title']}' now starts in {match['title']}", "green"))
    print(c("  (start sprint only — hours follow the logs; "
            "run 'wt sync-sprints' to re-derive bindings)", "dim"))


def _print_push_hint(task):
    if task_current_issue(task):
        print(c(f"  Run: wt push {task['id']} to sync project fields", "dim"))


def cmd_set_repo(args):
    """Set or clear the GitHub repo for a task."""
    if not args:
        print("Usage: wt set-repo <task> [owner/repo]")
        print("  Omit the repo to clear it")
        sys.exit(1)

    data = load()
    if len(args) < 2:
        task = resolve_task(data, " ".join(args))
        if task.pop("github_repo", None) is not None:
            save(data)
            print(c(f"✓ Cleared GitHub repo for '{task['title']}'", "yellow"))
        else:
            print(c(f"'{task['title']}' has no GitHub repo set.", "dim"))
        return

    task = resolve_task(data, args[0])
    repo = args[1]
    if "/" not in repo or repo.count("/") != 1:
        print(c("Error: Repo must be in owner/repo format", "red"))
        sys.exit(1)
    task["github_repo"] = repo
    save(data)
    print(c(f"✓ Set GitHub repo for '{task['title']}': {repo}", "green"))
    _print_push_hint(task)


def _cmd_set_project_option(args, key: str, label: str):
    """Shared logic for set-activity / set-type: first arg task, rest value."""
    if not args:
        print(f"Usage: wt set-{key} <task> [{label.lower()}]")
        print(f"  Omit the {label.lower()} to clear it")
        sys.exit(1)

    data = load()
    if len(args) < 2:
        task = resolve_task(data, args[0])
        if task.pop(key, None) is not None:
            save(data)
            print(c(f"✓ Cleared {label.lower()} for '{task['title']}'", "yellow"))
        else:
            print(c(f"'{task['title']}' has no {label.lower()} set.", "dim"))
        return

    # First arg is the task query, the rest joins into a (possibly multi-word)
    # value — same convention as set-sprint.
    task = resolve_task(data, args[0])
    value = " ".join(args[1:])

    options = get_cached_project_options(data).get(key)
    if options and value not in options:
        print(c(f"Unknown {label.lower()}: {value}", "red"))
        print(c("  Available options:", "dim"))
        for o in options:
            print(c(f"    {o}", "dim"))
        sys.exit(1)

    task[key] = value
    save(data)
    print(c(f"✓ Set {label.lower()} for '{task['title']}': {value}", "green"))
    _print_push_hint(task)


def cmd_set_activity(args):
    """Set or clear the GitHub Project activity for a task."""
    _cmd_set_project_option(args, "activity", "Activity")


def cmd_set_type(args):
    """Set or clear the GitHub Project type for a task."""
    _cmd_set_project_option(args, "type", "Type")


# ── Sprint reconcile (`wt sync-sprints`, formerly `wt split-sprint`) ──────────

def _reconcile_plan_lines(res: dict) -> list[str]:
    """Human-readable, itemised description of what a reconcile *would* do.

    Reads only the plan (``res["planned"]``) plus the read-only ``skipped``
    entries, so it is safe to render from a ``dry_run=True`` result.
    """
    lines: list[str] = []
    # Sprints getting a brand-new issue in this same plan: their close op has no
    # issue ref yet, so name it "the new issue" rather than "(no issue)".
    to_create = {op["sprint_id"] for op in res.get("planned", [])
                 if op["op"] == "create" and op.get("create_issue")}
    for op in res.get("planned", []):
        sprint = op.get("sprint") or op.get("sprint_id") or "?"
        kind = op["op"]
        if kind == "create":
            if op.get("create_issue"):
                lines.append(f"create  {sprint:<12} new issue \"{op['issue_title']}\" "
                             f"in {op['repo']} — {fmt_mins(op['minutes'])} → {op['hours']}h"
                             + ("  (then close)" if op.get("will_close") else ""))
            else:
                why = op.get("skipped_github") or "no repo"
                lines.append(f"create  {sprint:<12} local binding only ({why}) — "
                             f"{fmt_mins(op['minutes'])}")
        elif kind == "repoint":
            lines.append(f"repoint {sprint:<12} carry {op['issue']} forward from "
                         f"{op.get('from_sprint') or 'no sprint'} — "
                         f"{fmt_mins(op['minutes'])} → {op['hours']}h")
        elif kind == "hours":
            was = op.get("from_hours")
            was_s = "unknown" if was is None else f"{was}h"
            lines.append(f"hours   {sprint:<12} {op['issue']}: {was_s} → {op['hours']}h "
                         f"({fmt_mins(op['minutes'])})")
        elif kind == "close":
            if op.get("issue"):
                what = op["issue"]
            elif op["sprint_id"] in to_create:
                what = "the issue created above"
            else:
                what = "(binding has no issue — nothing to close on GitHub)"
            lines.append(f"close   {sprint:<12} {what} — sprint has ended")
        elif kind == "supersede":
            lines.append(f"SUPER   {sprint:<12} {op['issue']}: zero + close "
                         f"(duplicate of {op.get('primary')} for this sprint)")
        elif kind == "relabel":
            lines.append(f"relabel {sprint:<12} task sprint pointer moves from "
                         f"{op.get('from_sprint') or 'none'}")
    for sk in res.get("skipped", []):
        if sk.get("needs_issue"):
            lines.append(f"SKIP    {sk.get('sprint'):<12} {fmt_mins(sk.get('minutes') or 0)} "
                         f"has no issue — re-run with --create-issues to mint one")
        elif sk.get("reason") == "binding has no issue" and (sk.get("minutes") or 0) > 0:
            # A binding that exists but was never linked: the task has never had a
            # GitHub issue for that sprint. `wt done` creates the first one;
            # reconcile only mints issues for *unbound* sprints.
            lines.append(f"note    {sk.get('sprint'):<12} {fmt_mins(sk['minutes'])} bound but "
                         f"never linked to an issue — use 'wt link' or 'wt done'")
        elif sk.get("withheld_hours"):
            was = sk.get("from_hours")
            was_s = "unknown" if was is None else f"{was}h"
            lines.append(f"HOLD    {sk.get('sprint'):<12} {sk['issue']}: would set "
                         f"{was_s} → {sk['hours']}h, withheld")
    if res.get("unbillable"):
        where = ", ".join(f"{e['sprint']} {fmt_mins(e['minutes'])}"
                          for e in res["unbillable"])
        lines.append(f"WHY     {'':<12} hours withheld because this task's time in "
                     f"{where} has no issue to report on.")
        lines.append(f"        {'':<12} Narrowing the other issues would delete that "
                     f"time from the project. Add --create-issues.")
    if res.get("unassigned_minutes"):
        lines.append(f"note    {'':<12} {fmt_mins(res['unassigned_minutes'])} of logs fall "
                     f"outside every sprint (not reported to GitHub)")
    return lines


def _reconcile_outcome_lines(res: dict) -> list[str]:
    """Human-readable description of what a reconcile actually did."""
    lines: list[str] = []
    for e in res.get("created", []):
        issue = e.get("issue") or f"local binding only ({e.get('skipped_github') or 'no repo'})"
        lines.append(f"+ {e['sprint']}: {fmt_mins(e['minutes'])} → {issue}")
    for e in res.get("repointed", []):
        lines.append(f"→ {e['sprint']}: carried {e['issue']} forward from "
                     f"{e.get('from_sprint') or 'no sprint'}")
    for e in res.get("superseded", []):
        lines.append(f"✖ {e['sprint']}: {e['issue']} zeroed + closed "
                     f"(duplicate of {e.get('primary')})")
    for e in res.get("hours_updated", []):
        was = e.get("from_hours")
        was_s = "unknown" if was is None else f"{was}h"
        lines.append(f"= {e['sprint']}: {e['issue']} hours {was_s} → {e['hours']}h")
    for e in res.get("closed", []):
        lines.append(f"x {e['sprint']}: closed {e.get('issue') or '(no issue)'}")
    if res.get("relabeled"):
        r = res["relabeled"]
        lines.append(f". sprint pointer now {r['sprint']} (was {r.get('from_sprint') or 'none'})")
    for sk in res.get("skipped", []):
        if sk.get("needs_issue"):
            lines.append(f"! {sk.get('sprint')}: {fmt_mins(sk.get('minutes') or 0)} unbilled — "
                         f"re-run with --create-issues")
    for e in res.get("errors", []):
        lines.append(f"✗ {e.get('sprint')}: {e['error']}")
    return lines


def cmd_sync_sprints(args):
    """Reconcile a task's per-sprint issue bindings with its logs (plan §2.3).

    Replaces ``wt split-sprint``. Idempotent: a second run finds nothing to do.
    """
    dry_run = all_tasks = create_issues_flag = assume_yes = False
    query_parts: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--dry-run", "-n"):
            dry_run = True; i += 1
        elif a == "--all":
            all_tasks = True; i += 1
        elif a == "--create-issues":
            create_issues_flag = True; i += 1
        elif a in ("--yes", "-y"):
            assume_yes = True; i += 1
        elif a in ("-h", "--help"):
            print("Usage: wt sync-sprints [<task>] [--all] [--create-issues] [--dry-run] [--yes]")
            print("  <task>           reconcile one task (mints past-sprint issues as needed)")
            print("  --all            reconcile every task, recurrent included; does NOT")
            print("                   create issues unless --create-issues is also given")
            print("  --create-issues  allow minting new past-sprint GitHub issues")
            print("  --dry-run / -n   print the plan and exit, changing nothing")
            print("  --yes / -y       skip the confirmation prompt")
            return
        elif a.startswith("-"):
            print(c(f"Unknown flag: {a}", "red")); sys.exit(1)
        else:
            query_parts.append(a); i += 1

    if all_tasks and query_parts:
        print(c("Give either a task or --all, not both.", "red")); sys.exit(1)
    if not all_tasks and not query_parts:
        print("Usage: wt sync-sprints [<task>] [--all] [--create-issues] [--dry-run]")
        sys.exit(1)

    data = load()
    all_sprints, from_cache = _sprints_for_cli(data)
    if not all_sprints:
        print(c("No sprints found (project not configured or query failed).", "red"))
        sys.exit(1)
    if from_cache:
        print(c("(offline — using the persisted sprints cache)", "dim"))

    # Requirement (a): a blanket run must not mint issues by default. On the real
    # data an unrestricted --all would create 25 GitHub issues and close 25, so
    # --all is hours-and-closes only until --create-issues is passed explicitly.
    create_issues = create_issues_flag or not all_tasks

    if all_tasks:
        candidates = list(data.get("tasks", []))
    else:
        candidates = [resolve_task(data, " ".join(query_parts))]

    # Phase 5 removed the recurrent exclusion. Recurring work used to be a fresh
    # cloned task per sprint, so reconciling it would have minted a past-sprint
    # issue for every "… - Sprint N" copy; now a series is one perpetual task with
    # a binding per sprint, and reconcile is exactly the right thing to run on it —
    # it opens the new sprint's issue and closes the one that just ended, which is
    # what wt close-recurrent / wt new-recurrent used to do by hand.
    targets, skipped_tasks = list(candidates), []

    # Plan pass: dry_run=True is structurally read-only (see reconcile_task_sprints).
    plans = []
    for t in targets:
        res = reconcile_task_sprints(t, data, all_sprints, create_issues=create_issues,
                                     dry_run=True)
        if res.get("error") or res.get("planned") or any(
            sk.get("needs_issue") for sk in res.get("skipped", [])
        ):
            plans.append((t, res))

    if skipped_tasks:
        print(c(f"\n  Skipped {len(skipped_tasks)} task(s):", "yellow"))
        for t, why in skipped_tasks:
            print(c(f"    • {t['title']}  [{t.get('sprint', '?')}]  — {why}", "dim"))

    if not plans:
        print(c(f"\n  Nothing to do ({len(targets)} task(s) already in sync).", "dim"))
        return

    n_create = n_repoint = n_hours = n_close = n_needs_issue = 0
    print(c(f"\n  Plan for {len(plans)} task(s):", "bold"))
    for t, res in plans:
        print(c(f"\n    {t['title']}", "cyan"))
        if res.get("error"):
            print(c(f"      ! {res['error']}", "red"))
            continue
        breakdown = ", ".join(
            f"{e['sprint']}={fmt_mins(e['minutes'])}" for e in res.get("target", [])
        )
        if breakdown:
            print(c(f"      logs by sprint: {breakdown}", "dim"))
        for line in _reconcile_plan_lines(res):
            print(f"      {line}")
        for op in res.get("planned", []):
            if op["op"] == "create":
                n_create += 1 if op.get("create_issue") else 0
            elif op["op"] == "repoint":
                n_repoint += 1
            elif op["op"] == "hours":
                n_hours += 1
            elif op["op"] == "close":
                n_close += 1
        n_needs_issue += sum(1 for sk in res.get("skipped", []) if sk.get("needs_issue"))

    print(c(f"\n  Totals: {n_create} issue(s) to create, {n_repoint} to re-point, "
            f"{n_hours} hours update(s), {n_close} issue(s) to close.", "bold"))
    if n_needs_issue:
        print(c(f"  {n_needs_issue} past sprint(s) with unbilled time were NOT bound "
                f"(no --create-issues).", "yellow"))
    if all_tasks and not create_issues_flag:
        print(c("  --all does not create GitHub issues; add --create-issues to allow it.",
                "dim"))

    if dry_run:
        print(c("\n  Dry run — nothing was changed.", "yellow"))
        return

    if not assume_yes:
        try:
            response = input("\n  Proceed? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(); return
        if response in ("n", "no"):
            print(c("  Aborted.", "yellow"))
            return

    def on_progress(msg):
        print(c(f"    {msg}", "dim"), flush=True)

    failures = 0
    for t, _plan in plans:
        print(c(f"\n  {t['title']}", "cyan"))
        res = reconcile_task_sprints(t, data, all_sprints, create_issues=create_issues,
                                     save_callback=save, progress_callback=on_progress)
        lines = _reconcile_outcome_lines(res)
        for line in lines:
            print(c(f"    {line}", "green" if not line.startswith(("✗", "!")) else "yellow"))
        if not lines:
            print(c("    (nothing to do)", "dim"))
        if not res.get("success"):
            failures += 1
    save(data)
    if failures:
        print(c(f"\n  {failures} task(s) had errors.", "red"))
        sys.exit(1)
    print(c("\n  Done.", "green"))


def cmd_split_sprint(args):
    """**Deprecated** alias for ``wt sync-sprints``."""
    print(c("Note: 'wt split-sprint' is deprecated — use 'wt sync-sprints' "
            "(same arguments, plus --all / --dry-run / --create-issues).", "yellow"))
    cmd_sync_sprints(args)


COMMANDS = {
    "add": cmd_add,
    "add-issue": cmd_add_issue,
    "list": cmd_list,
    "ls": cmd_list,
    "start": cmd_start,
    "stop": cmd_stop,
    "log": cmd_log,
    "logs": cmd_logs,
    "edit-log": cmd_edit_log,
    "delete-log": cmd_delete_log,
    "split-log": cmd_split_log,
    "merge-logs": cmd_merge_logs,
    "done": cmd_done,
    "close-recurrent": cmd_close_recurrent,
    "new-recurrent": cmd_new_recurrent,
    "delete": cmd_delete,
    "del": cmd_delete,
    "rm": cmd_delete,
    "rename": cmd_rename,
    "mv": cmd_rename,
    "status": cmd_status,
    "notes": cmd_notes,
    "link": cmd_link,
    "unlink": cmd_unlink,
    "push": cmd_push,
    "config": cmd_config,
    "presence": cmd_presence,
    "roles": cmd_roles,
    "arc": cmd_arc,
    "iterm": cmd_iterm,
    "calendar": cmd_calendar,
    "cal": cmd_calendar,
    "report": cmd_report,
    "sprint": cmd_sprint,
    "set-sprint": cmd_set_sprint,
    "sync-sprints": cmd_sync_sprints,
    "split-sprint": cmd_split_sprint,  # deprecated alias for sync-sprints
    "set-repo": cmd_set_repo,
    "set-activity": cmd_set_activity,
    "set-type": cmd_set_type,
}


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        sys.exit(0)
    cmd = args[0].lower()
    if cmd not in COMMANDS:
        print(c(f"Unknown command: {cmd}", "red"))
        print("Commands: " + ", ".join(sorted(set(COMMANDS.keys()))))
        sys.exit(1)
    try:
        COMMANDS[cmd](args[1:])
    except DataFileUnreadable as exc:
        # The one failure worth catching here: on the second Mac this is the
        # Full Disk Access case, and a bare traceback buries the one line that
        # says what to do about it.
        print(c(f"\n  {exc}\n", "red"))
        sys.exit(2)
    except RefusingToEmptyDataFile as exc:
        print(c(f"\n  {exc}\n", "red"))
        sys.exit(2)


if __name__ == "__main__":
    main()
