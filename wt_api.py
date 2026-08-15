#!/usr/bin/env python3
"""wt_api — the dict-returning command layer (docs/plan-macos-app.md §4, Phase 1).

``wt.py`` holds the primitives (sprint bindings, reconcile, GitHub project sync,
time accounting). ``mcp_server.py`` used to hold the *command-level assembly and
validation* on top of them — baked into English strings, so nothing else could
reuse it. This module extracts that layer once so there is **one command layer
and several front ends**: the MCP server today, ``wt_daemon.py`` (Phase 2) and
the Swift client (Phase 3) next, and optionally the CLI later.

Rules this module obeys, and any new function here must keep obeying:

* **Pure over a passed-in ``data`` dict.** No ``wt.load()``/``wt.save()`` inside;
  the caller owns the transaction (Phase 2 wraps whole transactions in
  ``wt.data_lock()``). Functions whose *original* MCP implementation persisted
  mid-operation take an explicit ``save_callback`` so the write ordering is
  preserved exactly, rather than being silently moved.
* **No printing, no ``sys.exit``, no argparse.** Return a dict, or raise.
* **Validation failures raise :class:`WtError`** carrying a *stable machine code*
  (see ``ERROR_CODES``) plus a human message. A Swift client switches on the
  code; ``mcp_server.py`` renders the message.
* **Every ``wt`` helper is reached as ``wt.<name>(...)``**, never
  ``from wt import <name>``. The regression harnesses monkeypatch ``wt`` module
  *attributes* to keep the tests offline; a ``from``-import would bind a copy and
  quietly escape to real GitHub.

-------------------------------------------------------------------------------
Phase 1 scope — what is, and is not, routed through here
-------------------------------------------------------------------------------

``tools/test_mcp_phase3.py`` drives only part of ``mcp_server.py``'s 43 tools, so
only the tools it actually covers were refactored onto this module. Everything
else stays on its existing code path until it has regression coverage.

**Refactored onto wt_api (15 tools, all covered by tools/test_mcp_phase3.py):**
    add_task, list_tasks, get_task, set_task_status, sync_task_sprints,
    set_sprint, link_github_issue, unlink_github_issue, push_task_to_github,
    get_notes_path, rename_task, delete_task, list_sprints,
    get_current_sprint_info, get_status

**Left on their current code path (28 tools, no regression coverage):**
    * Arc — **deprecated, deliberately not ported**: setup_arc_space,
      get_arc_status, cleanup_task_tabs, sync_arc_folders
    * timers: start_timer, stop_timer  (wt_api equivalents exist below and are
      unit-tested, but the MCP tools still carry their own Arc side effects)
    * time logs: log_time, list_logs, edit_log, delete_log, split_log,
      merge_logs  (ditto — wt_api equivalents exist and are unit-tested)
    * reporting: report_time_range
    * GitHub extras: view_github_issue, add_github_comment,
      create_task_from_issue
    * roles: list_roles, add_role, update_role, delete_role
    * per-task project fields: set_task_repo, set_task_activity, set_task_type
      (wt_api equivalents exist and are unit-tested)
    * retired: close_previous_recurrent_tasks (hard-refuses; nothing to extract)

The wt_api surface is nevertheless *complete* per the plan, because Phase 2's
daemon needs the timer/log/close primitives. The unwired functions are covered
directly by ``tools/test_wt_api.py``.
"""

from __future__ import annotations

import random
import string
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import wt

__all__ = [
    "WtError", "ERROR_CODES", "STATUS_LABELS",
    "task_last_logged_at", "uid",
    "resolve_task", "require_task",
    "snapshot", "task_view", "task_detail",
    "create_task", "update_task", "set_status", "rename_task", "delete_task",
    "set_task_repo", "set_task_activity", "set_task_type",
    "start_timer", "stop_timer",
    "add_log", "edit_log", "delete_log", "split_log", "merge_logs",
    "plan_close", "close", "raise_on_failure",
    "plan_reconcile", "apply_reconcile", "reconcile",
    "normalize_issue_ref", "verify_issue", "ensure_issue", "link_issue",
    "unlink_issue",
    "push_to_github", "notes_target",
    "set_start_sprint", "sprints_overview", "current_sprint_info",
    "status_overview", "list_tasks",
]


# ---------------------------------------------------------------- errors ------

#: Every machine code this module can raise. Phase 2's daemon maps these to HTTP
#: statuses and the Swift client localizes them, so **codes are API surface**:
#: rename one only with a deliberate, coordinated change.
ERROR_CODES = (
    "task_not_found",      # no task matches the query/id
    "ambiguous_task",      # a title substring matched more than one task
    "invalid_role",        # role id is not in data["roles"]
    "invalid_status",      # status is not one of STATUS_LABELS
    "invalid_repo",        # github_repo is not owner/repo
    "unknown_activity",    # activity not in config.project_options_cache
    "unknown_type",        # type not in config.project_options_cache
    "sprint_not_found",    # no sprint matches the given title
    "no_sprints",          # neither the live fetch nor the cache produced any
    "no_repo",             # operation needs the task to have a github_repo
    "not_linked",          # task has no current GitHub issue binding
    "no_default_repo",     # a bare issue number with no config.github_repo
    "issue_not_found",     # `gh issue view` could not find the ref
    "log_not_found",       # no log matches the id/prefix
    "no_changes",          # an edit was asked for with nothing to change
    "invalid_minutes",     # minutes <= 0
    "invalid_split",       # split point outside (0, total)
    "same_log",            # merge of a log with itself
    "no_active_timer",     # stop_timer with nothing running
    "invalid_args",        # mutually exclusive / missing arguments
    "close_failed",        # wt.close_task returned success=False
    "reconcile_failed",    # wt.reconcile_task_sprints returned success=False
    "github_failed",       # a gh invocation returned non-zero
)


class WtError(Exception):
    """A validation or operation failure with a stable machine code.

    ``code`` is one of :data:`ERROR_CODES` and is what programmatic callers
    branch on. ``message`` is human-readable and is what a front end prints;
    front-end-specific wording (e.g. "use list_sprints()") belongs in the front
    end, not here. ``details`` carries structured context (the offending value,
    the list of valid options, …) so a UI can build a picker rather than parse
    prose.
    """

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"WtError({self.code!r}, {self.message!r})"

    def as_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message,
                          **({"details": self.details} if self.details else {})}}


STATUS_LABELS = {"todo": "To Do", "inprogress": "In Progress",
                 "recurrent": "Recurrent", "done": "Done"}


# ------------------------------------------------------------- primitives -----

def uid() -> str:
    """Timestamp-based id, matching ``wt.uid()`` / ``tracker.uid()``."""
    return datetime.now().strftime("%Y%m%d%H%M%S") + "".join(
        random.choices(string.ascii_lowercase, k=4))


def task_last_logged_at(task: dict) -> float | None:
    """Epoch seconds of the task's most recent time-log entry, or None.

    Moved here from ``tracker.py`` in Phase 1 (docs/plan-macos-app.md §5.4): the
    TUI's HTTP bridge, this module's :func:`snapshot`, and Phase 2's legacy
    ``:7373`` contract all need it, and two copies would drift.

    Uses each log's ``at`` timestamp (the canonical record time, always set on
    timer sessions and manual logs); falls back to ``ended_at``/``started_at``
    for any legacy entry missing ``at``. Returns None when the task has no logs.
    """
    stamps = [
        ts
        for l in task.get("logs", [])
        if (ts := l.get("at") or l.get("ended_at") or l.get("started_at")) is not None
    ]
    return max(stamps) if stamps else None


def task_live_mins(task: dict, active_timer: dict | None) -> float:
    """Minutes elapsed on *task*'s running timer, or 0.0."""
    if active_timer and active_timer.get("task_id") == task.get("id"):
        return (time.time() - active_timer["started_at"]) / 60
    return 0.0


def _roles(data: dict) -> dict:
    return {r["id"]: r.get("label", r["id"]) for r in data.get("roles", [])}


def _iso(value):
    """ISO-8601 a date/datetime, pass anything else through unchanged."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def resolve_task(data: dict, query: str) -> dict | None:
    """Find a task by exact id, else by unique case-insensitive title substring.

    Returns None both when nothing matched and when the substring was ambiguous
    — same contract as ``mcp_server.resolve_task``. Use :func:`require_task` when
    you want the two cases distinguished.
    """
    tasks = data.get("tasks", [])
    match = next((t for t in tasks if t.get("id") == query), None)
    if match:
        return match
    q = (query or "").lower()
    matches = [t for t in tasks if q in t.get("title", "").lower()]
    return matches[0] if len(matches) == 1 else None


def require_task(data: dict, query: str) -> dict:
    """:func:`resolve_task` or raise ``task_not_found`` / ``ambiguous_task``."""
    tasks = data.get("tasks", [])
    match = next((t for t in tasks if t.get("id") == query), None)
    if match:
        return match
    q = (query or "").lower()
    matches = [t for t in tasks if q in t.get("title", "").lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise WtError("ambiguous_task",
                      f"'{query}' matches {len(matches)} tasks",
                      query=query, titles=[t.get("title") for t in matches])
    raise WtError("task_not_found", f"No task found matching '{query}'",
                  query=query)


def _find_log(task: dict, log_id: str) -> dict:
    log = next((l for l in task.get("logs", [])
                if l.get("id", "").startswith(log_id)), None)
    if log is None:
        raise WtError("log_not_found",
                      f"No log found with ID starting with '{log_id}'",
                      log_id=log_id)
    return log


# ----------------------------------------------------------------- snapshot ---

def task_view(task: dict, data: dict, sprints: list[dict] | None = None,
              active_timer: dict | None = ...) -> dict:
    """One task, rendered for a UI. JSON-serializable, no network calls.

    *sprints* defaults to the persisted ``config.sprints_cache`` so this is
    offline by construction (docs/plan-macos-app.md §4). Pass the live list only
    if the caller already has one.
    """
    if sprints is None:
        sprints = wt.get_cached_sprints(data)
    if active_timer is ...:
        active_timer = data.get("active_timer")

    # Strip the bulky per-entry ``logs`` key: the task's full log array is sent
    # once, below, and re-sending each log inside every sprint entry roughly
    # doubles the payload for no gain (plan §4).
    per_sprint = []
    for entry in wt.task_sprints_with_time(task, sprints):
        per_sprint.append({
            "sprint_id": entry.get("sprint_id"),
            "sprint_title": entry.get("sprint_title"),
            "field_id": entry.get("field_id"),
            "start_date": _iso(entry.get("start_date")),
            "total_mins": entry.get("total_mins"),
        })

    return {
        "id": task.get("id"),
        "title": task.get("title"),
        "description": task.get("description") or "",
        "status": task.get("status"),
        "status_label": STATUS_LABELS.get(task.get("status"), task.get("status")),
        "role_id": task.get("role_id"),
        "created_at": task.get("created_at"),

        # The two per-task fields the filter bar needs (plan §8), plus `type`
        # for the editor.
        "activity": task.get("activity"),
        "github_repo": task.get("github_repo"),
        "type": task.get("type"),

        # Sprint attribution stays in Python: Swift never re-derives it from log
        # timestamps and sprint date ranges (plan §8.2).
        "sprints_with_time": per_sprint,
        "start_sprint": task.get("start_sprint"),
        "start_sprint_id": task.get("start_sprint_id"),
        "sprint_issues": [dict(b) for b in (task.get("sprint_issues") or [])],
        # Never the raw github_issue key — a task has one issue *per sprint* and
        # the flat field is a legacy mirror of the current one.
        "current_issue": wt.task_current_issue(task, data),

        "logged_mins": wt.task_logged_mins(task),
        "live_mins": task_live_mins(task, active_timer),
        "reportable_mins": wt.task_reportable_mins(task, sprints),
        "last_logged_at": task_last_logged_at(task),
        "logs": [dict(l) for l in task.get("logs", [])],

        # Integration state the editor / board affordances read.
        "local_folder": task.get("local_folder"),
    }


def task_detail(data: dict, task_id: str,
                sprints: list[dict] | None = None) -> dict:
    """:func:`task_view` plus the labels and ordered bindings a detail view needs.

    Offline: sprints default to ``config.sprints_cache``. Bindings come back in
    ``task_sprint_bindings`` order, each flagged with whether it is the *current*
    one (identity-compared against ``current_binding``, which is how the MCP
    server has always marked it).
    """
    task = require_task(data, task_id)
    if sprints is None:
        sprints = wt.get_cached_sprints(data)
    view = task_view(task, data, sprints)
    roles = _roles(data)
    start = wt.task_start_sprint(task, sprints)
    current = wt.current_binding(task, data)
    view.update({
        "task": task,
        "role_label": roles.get(task.get("role_id"), task.get("role_id")),
        "running": bool((data.get("active_timer") or {}).get("task_id")
                        == task.get("id")),
        "start_sprint_label": (task.get("start_sprint")
                               or (start["title"] if start else None)),
        "bindings": [
            dict(b, current=(current is not None and b is current))
            for b in wt.task_sprint_bindings(task, sprints)
        ],
        # The legacy mirror, exposed only because the MCP detail view has always
        # printed it. Do not add new readers.
        "legacy_sprint": task.get("sprint"),
    })
    return view


def snapshot(data: dict) -> dict:
    """Everything a UI renders, in one JSON-serializable document.

    Offline by construction: sprints come from ``config.sprints_cache``, never
    from ``gh``. Filtering is deliberately *not* done here — 55 tasks and 419
    logs fit in one payload, so every facet is applied client-side (plan §5.2).
    """
    sprints = wt.get_cached_sprints(data)
    active_timer = data.get("active_timer")
    today = datetime.now().date()
    current = wt.find_sprint_for_date(sprints, today)

    return {
        "generated_at": time.time(),
        "tasks": [task_view(t, data, sprints, active_timer)
                  for t in data.get("tasks", [])],
        "roles": [
            {"id": r.get("id"), "label": r.get("label"), "color": r.get("color")}
            for r in data.get("roles", [])
        ],
        # The raw persisted cache: already ISO strings, already the shape
        # save_sprints_cache() writes.
        "sprints": [dict(s) for s in
                    data.get("config", {}).get("sprints_cache", [])],
        "current_sprint": None if not current else {
            "id": current.get("id"),
            "title": current.get("title"),
            "start_date": _iso(current.get("start_date")),
            "end_date": _iso(current.get("end_date")),
            "field_id": current.get("field_id"),
        },
        # Raw epoch ``started_at`` so a client ticks elapsed time locally rather
        # than polling (plan §4).
        "active_timer": None if not active_timer else {
            "task_id": active_timer.get("task_id"),
            "started_at": active_timer.get("started_at"),
        },
        # Needed by the task editor's Activity/Type pickers — the *full* option
        # list, unlike the filter bar which only offers values in use (§8.3).
        "project_options": wt.get_cached_project_options(data) or {},
        "config": {
            "github_project_owner": data.get("config", {}).get("github_project_owner"),
            "github_project_number": data.get("config", {}).get("github_project_number"),
            "github_repo": data.get("config", {}).get("github_repo"),
        },
    }


# -------------------------------------------------------------- validation ----

def _check_role(data: dict, role: str) -> str:
    roles = _roles(data)
    if role not in roles:
        raise WtError("invalid_role",
                      f"Invalid role '{role}'. Available: {', '.join(roles.keys())}",
                      role=role, available=list(roles.keys()))
    return roles[role]


def _check_status(status: str) -> str:
    if status not in STATUS_LABELS:
        raise WtError("invalid_status",
                      f"Invalid status '{status}'. Use: todo, inprogress, "
                      f"recurrent, done",
                      status=status, available=list(STATUS_LABELS))
    return STATUS_LABELS[status]


def _check_repo(repo: str) -> str:
    if "/" not in repo or repo.count("/") != 1:
        raise WtError("invalid_repo", "github_repo must be in owner/repo format",
                      github_repo=repo)
    return repo


def _check_project_option(data: dict, key: str, label: str, value: str) -> str:
    options = wt.get_cached_project_options(data).get(key)
    if options and value not in options:
        # Spelled out rather than f"unknown_{key}" so the codes are greppable —
        # tools/test_wt_api.py cross-checks ERROR_CODES against the source.
        code = "unknown_activity" if key == "activity" else "unknown_type"
        raise WtError(code,
                      f"Unknown {label} '{value}'. Available: {', '.join(options)}",
                      **{key: value, "available": list(options)})
    return value


# ------------------------------------------------------------ task commands ---

def create_task(data: dict, *, title: str, role: str = "other",
                status: str = "todo", description: str = "",
                github_issue: str = "", sprint: str = "",
                github_repo: str = "", activity: str = "",
                type: str = "") -> dict:
    """Create a task and insert it at the head of ``data["tasks"]``.

    *sprint* semantics match the CLI/MCP: a title assigns that sprint, the
    string ``"none"`` assigns none, and an empty value auto-assigns the current
    sprint. Note this writes the **legacy** ``sprint``/``sprint_id`` mirror,
    which is what every existing creation path does; the authoritative
    per-sprint attribution is derived from log timestamps at reconcile time.

    Raises ``invalid_role``, ``invalid_status``, ``invalid_repo``,
    ``unknown_activity``, ``unknown_type``, ``sprint_not_found``.
    """
    role_label = _check_role(data, role)
    status_label = _check_status(status)
    if github_repo:
        _check_repo(github_repo)
    if activity:
        _check_project_option(data, "activity", "activity", activity)
    if type:
        _check_project_option(data, "type", "type", type)

    task = {
        "id": uid(),
        "title": title,
        "description": description,
        "role_id": role,
        "status": status,
        "logs": [],
        "created_at": time.time(),
    }
    if github_repo:
        task["github_repo"] = github_repo
    if activity:
        task["activity"] = activity
    if type:
        task["type"] = type

    if sprint and sprint.lower() != "none":
        match = wt._match_sprint(wt.get_all_sprints(data), sprint)
        if not match:
            raise WtError("sprint_not_found", f"Sprint '{sprint}' not found.",
                          sprint=sprint)
        task["sprint"] = match["title"]
        task["sprint_id"] = match["id"]
    elif not sprint:
        current = wt.get_current_sprint(data)
        if current:
            task["sprint"] = current["title"]
            task["sprint_id"] = current["id"]

    # After the sprint assignment, so the binding lands on the right sprint.
    if github_issue:
        wt.set_task_current_issue(task, github_issue, data)

    data.setdefault("tasks", []).insert(0, task)
    return {
        "task": task,
        "role_label": role_label,
        "status_label": status_label,
        "sprint": task.get("sprint"),
        "github_issue": github_issue or None,
    }


_UPDATABLE = {"title", "description", "role_id", "status", "github_repo",
              "activity", "type", "local_folder"}


def update_task(data: dict, task_id: str, **fields) -> dict:
    """Patch a task's editable fields. Unknown keys raise ``invalid_args``.

    A ``None`` value clears the key (except ``title``/``role_id``/``status``,
    which are required and rejected as ``invalid_args``). Renaming through here
    does **not** touch GitHub — use :func:`rename_task` for that.
    """
    task = require_task(data, task_id)
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise WtError("invalid_args",
                      f"Cannot update: {', '.join(sorted(unknown))}",
                      fields=sorted(unknown), allowed=sorted(_UPDATABLE))

    changed = {}
    for key, value in fields.items():
        if key == "role_id" and value is not None:
            _check_role(data, value)
        elif key == "status" and value is not None:
            _check_status(value)
        elif key == "github_repo" and value:
            _check_repo(value)
        elif key == "activity" and value:
            _check_project_option(data, "activity", "activity", value)
        elif key == "type" and value:
            _check_project_option(data, "type", "type", value)

        if value in (None, "") and key not in ("title", "role_id", "status"):
            if task.pop(key, None) is not None:
                changed[key] = None
        else:
            if value in (None, ""):
                raise WtError("invalid_args", f"'{key}' cannot be empty", field=key)
            if task.get(key) != value:
                task[key] = value
                changed[key] = value
    return {"task": task, "changed": changed}


def rename_task(data: dict, task_id: str, new_title: str, *,
                save_callback=None, sync_issue_title: bool = True) -> dict:
    """Rename a task and (optionally) retitle its *current* binding's issue.

    Past-sprint issues keep their ``(Sprint N)`` titles — retitling those is not
    this operation's job and would need the suffix re-applied per binding.

    ``save_callback`` is invoked after the local rename and *before* the GitHub
    call, preserving the ordering the MCP tool has always had: the local edit
    survives even if ``gh`` fails.
    """
    task = require_task(data, task_id)
    old_title = task.get("title")
    task["title"] = new_title
    if save_callback:
        save_callback(data)

    out = {"old_title": old_title, "new_title": new_title,
           "issue": None, "issue_updated": False, "issue_error": None}
    issue_ref = wt.task_current_issue(task, data)
    out["issue"] = issue_ref
    if issue_ref and sync_issue_title:
        import subprocess
        res = subprocess.run(
            ["gh", "issue", "edit", *gh_issue_args(issue_ref), "--title", new_title],
            capture_output=True, text=True)
        if res.returncode == 0:
            out["issue_updated"] = True
        else:
            out["issue_error"] = res.stderr
    return out


def delete_task(data: dict, task_id: str, *, save_callback=None,
                delete_issue: bool = True) -> dict:
    """Remove a task, clearing the active timer if it was the running one.

    Only the *current* binding's issue is deleted on GitHub. Past-sprint
    bindings are separate, already-closed issues; they are named in
    ``other_issues`` rather than silently orphaned or destroyed.
    """
    task = require_task(data, task_id)
    issue_ref = wt.task_current_issue(task, data)
    other_issues = [r for r in wt.task_issue_refs(task) if r != issue_ref]

    data["tasks"] = [t for t in data.get("tasks", []) if t.get("id") != task["id"]]
    if (data.get("active_timer") or {}).get("task_id") == task["id"]:
        data["active_timer"] = None
    if save_callback:
        save_callback(data)

    issue_deleted = None
    if issue_ref and delete_issue:
        issue_deleted = bool(wt.delete_github_issue(issue_ref))
    return {"title": task.get("title"), "task": task, "issue": issue_ref,
            "issue_deleted": issue_deleted, "other_issues": other_issues}


def set_task_repo(data: dict, task_id: str, github_repo: str | None = None) -> dict:
    """Set or clear a task's ``github_repo``. Empty/None clears it."""
    task = require_task(data, task_id)
    if github_repo:
        _check_repo(github_repo)
        task["github_repo"] = github_repo
        return {"task": task, "github_repo": github_repo, "cleared": False,
                "changed": True}
    had = task.pop("github_repo", None)
    return {"task": task, "github_repo": None, "cleared": had is not None,
            "changed": had is not None}


def _set_project_option(data: dict, task_id: str, key: str, label: str,
                        value: str | None) -> dict:
    task = require_task(data, task_id)
    if value:
        _check_project_option(data, key, label, value)
        task[key] = value
        return {"task": task, key: value, "cleared": False, "changed": True}
    had = task.pop(key, None)
    return {"task": task, key: None, "cleared": had is not None,
            "changed": had is not None}


def set_task_activity(data: dict, task_id: str, activity: str | None = None) -> dict:
    """Set or clear a task's GitHub Project Activity value."""
    return _set_project_option(data, task_id, "activity", "activity", activity)


def set_task_type(data: dict, task_id: str, type: str | None = None) -> dict:
    """Set or clear a task's GitHub Project Type value."""
    return _set_project_option(data, task_id, "type", "type", type)


def list_tasks(data: dict, *, role: str | None = None, status: str | None = None,
               include_done: bool = False) -> list[dict]:
    """Task summaries for a list view. Done tasks are hidden unless asked for.

    An explicit ``status`` filter always wins over ``include_done`` — including
    ``status="done"``, which is how a caller asks for only the done ones. No
    shadow-task filter exists any more: cross-sprint work lives in the task's own
    ``sprint_issues`` bindings, so there is nothing hidden to exclude.
    """
    tasks = data.get("tasks", [])
    active_timer = data.get("active_timer")
    roles = _roles(data)

    if role:
        tasks = [t for t in tasks if t.get("role_id") == role]
    if status:
        tasks = [t for t in tasks if t.get("status") == status]
    elif not include_done:
        tasks = [t for t in tasks if t.get("status") != "done"]

    out = []
    for t in tasks:
        logged = wt.task_logged_mins(t)
        live = task_live_mins(t, active_timer)
        out.append({
            "id": t.get("id"),
            "title": t.get("title"),
            "role_id": t.get("role_id"),
            "role_label": roles.get(t.get("role_id"), t.get("role_id")),
            "status": t.get("status"),
            "status_label": STATUS_LABELS.get(t.get("status"), t.get("status")),
            "logged_mins": logged,
            "live_mins": live,
            "total_mins": logged + live,
            "running": bool(active_timer
                            and active_timer.get("task_id") == t.get("id")),
            "sprint": t.get("sprint"),
            "task": t,
        })
    return out


def status_overview(data: dict) -> dict:
    """Per-role time totals plus the running timer — the ``get_status`` payload."""
    tasks = data.get("tasks", [])
    active_timer = data.get("active_timer")
    roles = _roles(data)

    by_role: dict[str, float] = {rid: 0.0 for rid in roles}
    for t in tasks:
        rid = t.get("role_id", "other")
        by_role[rid] = (by_role.get(rid, 0.0) + wt.task_logged_mins(t)
                        + task_live_mins(t, active_timer))
    total = sum(by_role.values())

    active = None
    if active_timer:
        t = next((x for x in tasks
                  if x.get("id") == active_timer.get("task_id")), None)
        active = {
            "task_id": active_timer.get("task_id"),
            "title": t.get("title") if t else None,
            "started_at": active_timer.get("started_at"),
            "elapsed_mins": (time.time() - active_timer["started_at"]) / 60,
        }

    return {
        "n_tasks": len(tasks),
        "total_mins": total,
        "by_role": [
            {"role_id": rid, "label": label, "mins": by_role.get(rid, 0.0),
             "pct": round(by_role.get(rid, 0.0) / total * 100) if total else 0}
            for rid, label in roles.items()
        ],
        "active": active,
    }


# ----------------------------------------------------------------- timers -----

def start_timer(data: dict, task_id: str) -> dict:
    """Start the timer on a task, committing any running one first.

    A sub-3-second session is discarded rather than logged, matching every other
    start path. Starting a timer has no effect on the desktop: the Safari
    task-window integration this used to drive is gone, and Arc space focus was
    never performed here.
    """
    task = require_task(data, task_id)
    prev, stopped = None, None
    active_timer = data.get("active_timer")

    if active_timer:
        prev = next((t for t in data.get("tasks", [])
                     if t.get("id") == active_timer.get("task_id")), None)
        if prev is not None:
            stopped = _commit_timer(prev, active_timer)

    data["active_timer"] = {"task_id": task["id"], "started_at": time.time()}
    return {"task": task, "started_at": data["active_timer"]["started_at"],
            "stopped": stopped}


#: The shortest session ``_commit_timer`` will record, in minutes. A stop three
#: seconds after a start is a misclick, not work.
MIN_LOGGED_MINUTES = 0.05


def stop_timer(data: dict, *, note: str | None = None,
               subtract_minutes: float = 0.0,
               min_minutes: float = MIN_LOGGED_MINUTES) -> dict:
    """Stop the running timer and log the elapsed session.

    Raises ``no_active_timer`` when nothing is running, so a caller can tell
    "stopped nothing" from "stopped something of zero length".

    The three keyword-only extras are **additive and defaulted**, so every
    existing caller keeps its exact behaviour; they exist for the daemon's
    presence loop (:meth:`wt_daemon.Daemon._auto_stop_idle_timer`), which needs
    the TUI's auto-stop log shape:

    * *note* replaces the ``"Timer session"`` note (the TUI writes
      ``"Timer session (auto-stopped, 20m idle subtracted)"``).
    * *subtract_minutes* is removed from the logged minutes — the idle tail the
      user was away for. ``minutes`` in the result stays the **full** elapsed
      time either way, so a caller can still report what the clock ran for.
    * *min_minutes* is the floor the *logged* (post-subtraction) minutes must
      clear to be written at all. The TUI uses 0.1 on this path; the ordinary
      stop keeps the historical 0.05 on the raw elapsed.
    """
    active_timer = data.get("active_timer")
    if not active_timer:
        raise WtError("no_active_timer", "No timer is currently running.")

    task = next((t for t in data.get("tasks", [])
                 if t.get("id") == active_timer.get("task_id")), None)
    if task is not None:
        stopped = _commit_timer(task, active_timer, note=note,
                                subtract_minutes=subtract_minutes,
                                min_minutes=min_minutes)
    else:
        elapsed = (time.time() - active_timer["started_at"]) / 60
        stopped = {
            "task_id": active_timer.get("task_id"), "title": None,
            "minutes": elapsed, "logged": False, "log": None,
            "logged_minutes": max(0.0, elapsed - max(0.0, subtract_minutes or 0.0)),
            "subtracted_minutes": max(0.0, subtract_minutes or 0.0),
        }
    data["active_timer"] = None
    return {"task": task, **stopped}


def _commit_timer(task: dict, active_timer: dict, *, note: str | None = None,
                  subtract_minutes: float = 0.0,
                  min_minutes: float = MIN_LOGGED_MINUTES) -> dict:
    """Append the ``"Timer session"`` log for *active_timer*, if long enough.

    With the defaults this is exactly what it has always been: the whole elapsed
    session, noted ``"Timer session"``, written when it exceeds 0.05 min. See
    :func:`stop_timer` for what the keyword-only arguments are for.
    """
    started_at = active_timer["started_at"]
    ended_at = time.time()
    elapsed = (ended_at - started_at) / 60
    subtracted = max(0.0, subtract_minutes or 0.0)
    logged_minutes = max(0.0, elapsed - subtracted)
    log = None
    if logged_minutes > min_minutes:
        log = {
            "id": uid(),
            "minutes": round(logged_minutes, 2),
            "note": note or "Timer session",
            "at": ended_at,
            "started_at": started_at,
            "ended_at": ended_at,
        }
        task.setdefault("logs", []).append(log)
    return {"task_id": task.get("id"), "title": task.get("title"),
            "minutes": elapsed, "logged": log is not None, "log": log,
            "logged_minutes": logged_minutes,
            "subtracted_minutes": subtracted}


# ------------------------------------------------------------------- logs -----

def add_log(data: dict, task_id: str, minutes: float, note: str = "Manual entry",
            started_at: float | None = None, ended_at: float | None = None,
            calendar_event_uid: str | None = None) -> dict:
    """Append a manual time-log entry. ``minutes`` must be > 0."""
    task = require_task(data, task_id)
    if minutes is None or minutes <= 0:
        raise WtError("invalid_minutes", "Minutes must be greater than 0",
                      minutes=minutes)
    log = {"id": uid(), "minutes": minutes, "note": note,
           "at": ended_at or time.time()}
    if started_at is not None:
        log["started_at"] = started_at
    if ended_at is not None:
        log["ended_at"] = ended_at
    if calendar_event_uid:
        log["calendar_event_uid"] = calendar_event_uid
    task.setdefault("logs", []).append(log)
    return {"task": task, "log": log}


def edit_log(data: dict, task_id: str, log_id: str, minutes: float | None = None,
             note: str | None = None) -> dict:
    """Change a log's minutes and/or note. ``log_id`` may be an id prefix."""
    task = require_task(data, task_id)
    if minutes is None and note is None:
        raise WtError("no_changes", "Specify minutes and/or note to update")
    log = _find_log(task, log_id)
    old = {"minutes": log.get("minutes", 0), "note": log.get("note", "")}
    if minutes is not None:
        log["minutes"] = minutes
    if note is not None:
        log["note"] = note
    return {"task": task, "log": log, "old": old}


def delete_log(data: dict, task_id: str, log_id: str) -> dict:
    """Remove a log entry. ``log_id`` may be an id prefix."""
    task = require_task(data, task_id)
    log = _find_log(task, log_id)
    task["logs"] = [l for l in task.get("logs", []) if l.get("id") != log.get("id")]
    return {"task": task, "log": log}


def split_log(data: dict, task_id: str, log_id: str,
              split_at_minutes: float) -> dict:
    """Split one log into two at a minute mark, dividing timestamps pro rata.

    The split point must lie strictly inside ``(0, total)`` — otherwise
    ``invalid_split``, since a 0-minute or full-length half is never wanted.
    """
    task = require_task(data, task_id)
    logs = task.get("logs", [])
    idx = next((i for i, l in enumerate(logs)
                if l.get("id", "").startswith(log_id)), None)
    if idx is None:
        raise WtError("log_not_found",
                      f"No log found with ID starting with '{log_id}'",
                      log_id=log_id)
    log = logs[idx]
    total = log.get("minutes", 0)
    if split_at_minutes <= 0 or split_at_minutes >= total:
        raise WtError("invalid_split",
                      f"Split point must be between 0 and {total}",
                      split_at_minutes=split_at_minutes, total_mins=total)

    first_mins = split_at_minutes
    second_mins = total - split_at_minutes
    note = log.get("note", "")
    started, ended = log.get("started_at"), log.get("ended_at")

    if started and ended:
        mid = started + (ended - started) * (first_mins / total)
        first = {"id": uid(), "minutes": round(first_mins, 2),
                 "note": f"{note} (1/2)", "at": mid,
                 "started_at": started, "ended_at": mid}
        second = {"id": uid(), "minutes": round(second_mins, 2),
                  "note": f"{note} (2/2)", "at": ended,
                  "started_at": mid, "ended_at": ended}
    else:
        at = log.get("at", time.time())
        first = {"id": uid(), "minutes": round(first_mins, 2),
                 "note": f"{note} (1/2)", "at": at}
        second = {"id": uid(), "minutes": round(second_mins, 2),
                  "note": f"{note} (2/2)", "at": at}

    logs[idx:idx + 1] = [first, second]
    return {"task": task, "first": first, "second": second,
            "total_mins": total}


def merge_logs(data: dict, task_id: str, log_id_1: str, log_id_2: str) -> dict:
    """Combine two logs: minutes summed, notes concatenated, span widened."""
    task = require_task(data, task_id)
    log1 = _find_log(task, log_id_1)
    log2 = _find_log(task, log_id_2)
    if log1.get("id") == log2.get("id"):
        raise WtError("same_log", "Cannot merge a log with itself",
                      log_id=log1.get("id"))

    combined = log1.get("minutes", 0) + log2.get("minutes", 0)
    merged = {
        "id": uid(),
        "minutes": round(combined, 2),
        "note": f"Merged: {log1.get('note', '')} + {log2.get('note', '')}",
        "at": max(log1.get("at", 0), log2.get("at", 0)),
    }
    if log1.get("started_at") and log2.get("started_at"):
        merged["started_at"] = min(log1["started_at"], log2["started_at"])
    if log1.get("ended_at") and log2.get("ended_at"):
        merged["ended_at"] = max(log1["ended_at"], log2["ended_at"])

    task["logs"] = [l for l in task.get("logs", [])
                    if l.get("id") not in (log1.get("id"), log2.get("id"))]
    task["logs"].append(merged)
    task["logs"].sort(key=lambda x: x.get("at", 0))
    return {"task": task, "merged": merged,
            "sources": [dict(log1), dict(log2)], "total_mins": combined}


# ------------------------------------------------------- status / close -------

def set_status(data: dict, task_id: str, status: str, *,
               create_issue: bool = False, save_callback=None,
               on_progress=None) -> dict:
    """Change a task's status; ``done`` runs the full close workflow.

    Non-``done`` transitions write the status, persist (via *save_callback*, at
    exactly the point the MCP tool always persisted), then push Status to the
    GitHub Project through the *current binding's* issue.

    ``done`` delegates to :func:`close`, whose result is returned with
    ``closed=True`` so a caller can tell the two shapes apart.
    """
    task = require_task(data, task_id)
    _check_status(status)
    old_status = task.get("status", "todo")

    if status == "done" and old_status != "done":
        result = close(data, task_id, create_issue=create_issue,
                       save_callback=save_callback, on_progress=on_progress)
        result["closed"] = True
        result["old_status"] = old_status
        return result

    task["status"] = status
    if save_callback:
        save_callback(data)

    project_synced = False
    issue_ref = wt.task_current_issue(task, data)
    if issue_ref:
        project_synced = bool(wt.sync_project_status(issue_ref, status, data))
    return {"closed": False, "task": task, "old_status": old_status,
            "status": status, "old_status_label": STATUS_LABELS.get(old_status),
            "status_label": STATUS_LABELS.get(status),
            "issue": issue_ref, "project_synced": project_synced}


def plan_close(data: dict, task_id: str, sprints: list[dict] | None = None) -> dict:
    """Preview a close: the reconcile plan a real close would execute.

    ``dry_run=True`` is write-free by construction in ``reconcile_task_sprints``
    (the planner is a separate pass from the executor), so this makes no GitHub
    calls and mutates nothing. ``closing=True`` matches what ``close_task``
    passes, so the preview does not reserve an empty current-sprint binding.
    """
    task = require_task(data, task_id)
    if sprints is None:
        sprints = wt.get_all_sprints(data) or wt.get_cached_sprints(data)
    repo = wt.get_task_repo(task)
    plan = wt.reconcile_task_sprints(task, data, sprints, dry_run=True,
                                     closing=True)
    return {
        "task": task,
        "title": task.get("title"),
        "repo": repo,
        "needs_issue": bool(repo) and not wt.task_current_issue(task, data),
        "current_issue": wt.task_current_issue(task, data),
        "plan": plan,
        "will_create_issues": sum(1 for op in plan.get("planned", [])
                                  if op["op"] == "create" and op.get("create_issue")),
        "plan_lines": wt._reconcile_plan_lines(plan),
    }


def close(data: dict, task_id: str, *, create_issue: bool = False,
          save_callback=None, on_progress=None, comment_callback=None) -> dict:
    """Run the shared ``wt.close_task`` workflow and normalize its result.

    ``create_issue`` is wired to ``close_task``'s ``prompt_callback``: False
    makes the workflow *refuse* rather than mint an issue, because
    ``close_task`` with no callback creates one unconditionally — which is not
    what ``create_issue=False`` means.

    Never raises on a GitHub failure: ``close_task`` marks the task done even
    when the project update fails, so a non-fatal ``error`` is passed through in
    the result. A *fatal* failure (no issue and no permission to make one, or a
    failed reconcile, which aborts the close so hours cannot be mis-reported)
    comes back as ``success: False`` — callers that prefer an exception can use
    ``raise_on_failure``.
    """
    task = require_task(data, task_id)
    repo = wt.get_task_repo(task)
    if save_callback is None:
        raise WtError("invalid_args",
                      "close() needs a save_callback: wt.close_task persists "
                      "mid-workflow so a gh failure cannot strand the task")

    result = wt.close_task(task, data, save_callback,
                           prompt_callback=lambda _msg: bool(create_issue),
                           comment_callback=comment_callback)
    out = dict(result)
    out["task"] = task
    out["title"] = task.get("title")
    out["repo"] = repo
    out["current_issue"] = wt.task_current_issue(task, data)
    binding = wt.current_binding(task, data)
    out["hours_synced"] = binding.get("hours_synced") if binding else None
    out["outcome_lines"] = (wt._reconcile_outcome_lines(result["reconcile_result"])
                            if result.get("reconcile_result") else [])
    if on_progress:
        for line in out["outcome_lines"]:
            on_progress(line)
    return out


def raise_on_failure(result: dict) -> dict:
    """Turn a failed :func:`close` result into a ``WtError``.

    A reconcile failure is reported as ``reconcile_failed`` rather than
    ``close_failed`` because the two mean different things to a client: a failed
    reconcile *aborts* the close (the task stays open) precisely so hours cannot
    be mis-reported, and is worth retrying; a ``close_failed`` generally needs
    the user to link or authorise an issue first.
    """
    if result.get("success"):
        return result
    error = result.get("error") or "unknown error"
    code = "reconcile_failed" if error.startswith("Sprint reconcile failed") \
        else "close_failed"
    raise WtError(code, error,
                  **{k: result.get(k) for k in ("title", "repo", "current_issue")})


# -------------------------------------------------------------- reconcile -----

def plan_reconcile(data: dict, *, task_id: str | None = None,
                   all_tasks: bool = False, sprints: list[dict] | None = None,
                   create_issues: bool | None = None) -> dict:
    """Plan a reconcile over one task or every task, without touching anything.

    ``create_issues`` defaults to True for a single task and **False** for
    ``all_tasks``: a blanket run over the real history wants to mint a couple of
    dozen issues for sprints predating this workflow, so that has to be opted
    into (CLAUDE.md, "two safety rules baked into sync-sprints").

    Returns the plan for each task that has one, plus the counts a caller
    renders. ``recurrent`` tasks are **not** excluded — Phase 5 merged the old
    per-sprint clones into one perpetual task with a binding per sprint, so
    reconcile is exactly what they need.
    """
    if all_tasks and task_id:
        raise WtError("invalid_args",
                      "Pass either task_query or all_tasks=True, not both.")
    if not all_tasks and not task_id:
        raise WtError("invalid_args", "task_query is required unless all_tasks=True.")

    from_cache = False
    if sprints is None:
        sprints, from_cache = wt._sprints_for_cli(data)
    if not sprints:
        raise WtError("no_sprints",
                      "No sprints found (project not configured or query failed).")

    if create_issues is None:
        create_issues = not all_tasks

    if all_tasks:
        targets = list(data.get("tasks", []))
    else:
        targets = [require_task(data, task_id)]

    plans = []
    for t in targets:
        res = wt.reconcile_task_sprints(t, data, sprints,
                                        create_issues=create_issues, dry_run=True)
        if res.get("error") or res.get("planned") or any(
                sk.get("needs_issue") for sk in res.get("skipped", [])):
            plans.append({"task": t, "result": res})

    totals = {"create": 0, "repoint": 0, "hours": 0, "close": 0, "needs_issue": 0}
    for entry in plans:
        res = entry["result"]
        for op in res.get("planned", []):
            if op["op"] == "create":
                totals["create"] += 1 if op.get("create_issue") else 0
            elif op["op"] in totals:
                totals[op["op"]] += 1
        totals["needs_issue"] += sum(1 for sk in res.get("skipped", [])
                                     if sk.get("needs_issue"))

    return {"sprints": sprints, "from_cache": from_cache,
            "create_issues": create_issues, "all_tasks": all_tasks,
            "targets": targets, "plans": plans, "totals": totals}


def apply_reconcile(data: dict, plan: dict, *, save_callback=None,
                    on_progress=None) -> list[dict]:
    """Execute a :func:`plan_reconcile` result, task by task.

    Re-runs the reconcile for real rather than replaying the plan: reconcile is
    a diff against a *derived* target state, so re-deriving it is both correct
    and how idempotency stays structural rather than guarded.
    """
    out = []
    for entry in plan["plans"]:
        task = entry["task"]
        res = wt.reconcile_task_sprints(
            task, data, plan["sprints"], create_issues=plan["create_issues"],
            save_callback=save_callback, progress_callback=on_progress)
        out.append({"task": task, "result": res,
                    "outcome_lines": wt._reconcile_outcome_lines(res),
                    "success": bool(res.get("success"))})
    return out


def reconcile(data: dict, task_id: str, *, sprints: list[dict] | None = None,
              create_issues: bool = True, dry_run: bool = False,
              save_callback=None, on_progress=None) -> dict:
    """Reconcile one task's per-sprint bindings against its logs."""
    task = require_task(data, task_id)
    if sprints is None:
        sprints, _ = wt._sprints_for_cli(data)
    if not sprints:
        raise WtError("no_sprints",
                      "No sprints found (project not configured or query failed).")
    res = wt.reconcile_task_sprints(
        task, data, sprints, create_issues=create_issues, dry_run=dry_run,
        save_callback=None if dry_run else save_callback,
        progress_callback=on_progress)
    return {"task": task, "result": res, "dry_run": dry_run,
            "plan_lines": wt._reconcile_plan_lines(res),
            "outcome_lines": [] if dry_run else wt._reconcile_outcome_lines(res),
            "success": bool(res.get("success"))}


# ---------------------------------------------------------------- GitHub ------

def normalize_issue_ref(data: dict, issue_ref: str) -> str:
    """Normalize a URL / bare number / ``owner/repo#n`` to ``owner/repo#n``.

    A bare number needs ``config.github_repo`` — otherwise ``no_default_repo``.
    """
    import re
    url = re.match(r'https?://github\.com/([^/]+/[^/]+)/issues/(\d+)', issue_ref)
    if url:
        return f"{url.group(1)}#{url.group(2)}"
    bare = re.match(r'^#?(\d+)$', issue_ref)
    if bare:
        repo = data.get("config", {}).get("github_repo")
        if not repo:
            raise WtError(
                "no_default_repo",
                "Issue number requires a default repo. Set config github_repo "
                "first, or use full reference: owner/repo#123",
                issue_ref=issue_ref)
        return f"{repo}#{bare.group(1)}"
    return issue_ref


def gh_issue_args(issue_ref: str) -> list[str]:
    """``owner/repo#123`` -> ``["-R", "owner/repo", "123"]`` for the gh CLI."""
    import re
    m = re.match(r'^([^#]+)#(\d+)$', issue_ref)
    if m:
        return ["-R", m.group(1), m.group(2)]
    return [issue_ref]


def verify_issue(issue_ref: str, fields: str = "number,title") -> dict:
    """``gh issue view`` the ref and return the decoded JSON.

    Raises ``issue_not_found`` on a non-zero exit. ``import subprocess`` is
    function-local on purpose: it resolves through ``sys.modules`` at call time,
    which is how the harnesses' recording fake intercepts it.
    """
    import json as _json
    import subprocess
    res = subprocess.run(
        ["gh", "issue", "view", *gh_issue_args(issue_ref), "--json", fields],
        capture_output=True, text=True)
    if res.returncode != 0:
        raise WtError("issue_not_found",
                      f"Could not find GitHub issue: {issue_ref}",
                      issue_ref=issue_ref, stderr=res.stderr)
    return _json.loads(res.stdout)


def ensure_issue(data: dict, task_id: str) -> dict:
    """Return the task's current issue, minting one in its repo if missing.

    Raises ``no_repo`` when the task has no ``github_repo`` — a task without one
    skips GitHub integration entirely, so there is nowhere to put an issue — and
    ``github_failed`` when ``gh issue create`` blows up. The new ref is written
    as a binding on the task's current sprint (never only the legacy flat key).
    """
    task = require_task(data, task_id)
    existing = wt.task_current_issue(task, data)
    if existing:
        return {"task": task, "issue": existing, "created": False}
    repo = wt.get_task_repo(task)
    if not repo:
        raise WtError("no_repo",
                      f"Task '{task.get('title')}' has no github_repo, so no "
                      f"issue can be created for it",
                      task_id=task.get("id"), title=task.get("title"))
    try:
        issue_ref = wt.create_github_issue(task, repo)
    except Exception as exc:  # noqa: BLE001 - surfaced as a coded error
        raise WtError("github_failed", f"Failed to create issue: {exc}",
                      task_id=task.get("id"), repo=repo) from exc
    wt.set_task_current_issue(task, issue_ref, data)
    return {"task": task, "issue": issue_ref, "created": True}


def link_issue(data: dict, task_id: str, issue_ref: str, *,
               verify: bool = True) -> dict:
    """Bind a GitHub issue to a task's current sprint.

    Writes the binding (and the legacy ``github_issue`` mirror) via
    ``set_task_current_issue``, and pins ``github_repo`` from the ref when the
    task has none, so the close workflow engages.
    """
    task = require_task(data, task_id)
    issue_ref = normalize_issue_ref(data, issue_ref)
    info = verify_issue(issue_ref) if verify else None
    wt.set_task_current_issue(task, issue_ref, data)
    repo_pinned = False
    if not task.get("github_repo") and "#" in issue_ref:
        task["github_repo"] = issue_ref.split("#", 1)[0]
        repo_pinned = True
    return {"task": task, "issue": issue_ref, "issue_info": info,
            "repo_pinned": repo_pinned}


def unlink_issue(data: dict, task_id: str) -> dict:
    """Clear a task's *current* binding. Past-sprint bindings are left alone."""
    task = require_task(data, task_id)
    if not wt.task_current_issue(task, data):
        raise WtError("not_linked",
                      f"Task '{task.get('title')}' is not linked to a GitHub issue.",
                      task_id=task.get("id"), title=task.get("title"))
    old = wt.clear_task_current_issue(task, data)
    return {"task": task, "old_issue": old, "remaining": wt.task_issue_refs(task)}


def push_to_github(data: dict, task_id: str,
                   sprints: list[dict] | None = None) -> dict:
    """Push Status/Activity/Type/Sprint/Hours to the task's current issue.

    Hours are the **sprint-filtered** total (``task_reportable_mins``), never the
    task total: past sprints' hours already live on their own bindings' issues,
    so the total would double-count. Does not close the issue.
    """
    task = require_task(data, task_id)
    issue_ref = wt.task_current_issue(task, data)
    if not issue_ref:
        raise WtError("not_linked",
                      f"Task '{task.get('title')}' has no linked GitHub issue",
                      task_id=task.get("id"), title=task.get("title"))
    result = wt.setup_issue_in_project(issue_ref, task, data)
    if sprints is None:
        sprints = wt.get_all_sprints(data)
    hours = wt.mins_to_quarter_hours(wt.task_reportable_mins(task, sprints))
    return {"task": task, "issue": issue_ref, "hours": hours,
            "success": bool(result.get("success")),
            "errors": result.get("errors") or []}


def notes_target(data: dict, task_id: str, notes_dir: Path | None = None) -> dict:
    """Where a task's notes live: its GitHub issue, else a local markdown file.

    Creates the local file (seeded with the title) when it does not exist, which
    is what every existing notes path does.
    """
    task = require_task(data, task_id)
    issue_ref = wt.task_current_issue(task, data)
    if issue_ref:
        return {"kind": "issue", "task": task, "issue": issue_ref, "path": None}
    notes_dir = Path(notes_dir) if notes_dir else (
        Path.home() / ".workload_tracker_notes")
    notes_dir.mkdir(exist_ok=True)
    path = notes_dir / f"{task['id']}.md"
    if not path.exists():
        path.write_text(f"# {task['title']}\n\n")
    return {"kind": "file", "task": task, "issue": None, "path": path}


# ---------------------------------------------------------------- sprints -----

def set_start_sprint(data: dict, task_id: str, sprint_title: str) -> dict:
    """Correct (or clear) the sprint a task *started* in.

    This is not "assign the task to a sprint": which sprint any minute of work
    belongs to is derived from the log's timestamp and materialised as
    ``sprint_issues`` bindings by reconcile. ``start_sprint`` is the one sprint
    field a human still owns, and it is frozen once derived so a later log edit
    cannot rewrite history. Pass ``"none"`` to clear it and let it re-derive.
    """
    task = require_task(data, task_id)
    if (sprint_title or "").lower() == "none":
        had = task.pop("start_sprint_id", None) is not None
        had = (task.pop("start_sprint", None) is not None) or had
        return {"task": task, "cleared": True, "had": had, "sprint": None}

    sprints, _ = wt._sprints_for_cli(data)
    if not sprints:
        raise WtError("no_sprints", "No sprints found.")
    match = wt._match_sprint(sprints, sprint_title)
    if not match:
        raise WtError("sprint_not_found",
                      f"No sprint matching '{sprint_title}'.",
                      sprint=sprint_title,
                      recent=[s["title"] for s in sprints[-5:]])
    task["start_sprint"] = match["title"]
    task["start_sprint_id"] = match["id"]
    return {"task": task, "cleared": False, "had": True, "sprint": match}


def sprints_overview(data: dict) -> dict:
    """Every sprint iteration plus which one is current.

    Prefers the live fetch and falls back to ``config.sprints_cache``, so this
    still answers offline. Both sources carry ``start_date``/``end_date`` as
    date objects — the camelCase ``startDate`` only the live fetch emits is not
    read anywhere.
    """
    sprints, from_cache = wt._sprints_for_cli(data)
    today = datetime.now().date()
    current = next((s for s in sprints
                    if s["start_date"] <= today < s["end_date"]), None)
    return {
        "sprints": sprints,
        "from_cache": from_cache,
        "current": current,
        "current_id": current["id"] if current else None,
    }


def current_sprint_info(data: dict) -> dict | None:
    """The current sprint with its inclusive last day, or None."""
    overview = sprints_overview(data)
    current = overview["current"]
    if not current:
        return None
    return {
        "sprint": current,
        "title": current["title"],
        "start_date": current["start_date"],
        "last_day": current["end_date"] - timedelta(days=1),
        "days": (current["end_date"] - current["start_date"]).days,
        "from_cache": overview["from_cache"],
    }
