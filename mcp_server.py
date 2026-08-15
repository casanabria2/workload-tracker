#!/usr/bin/env python3
"""
MCP Server for Workload Tracker.

Allows Claude to interact directly with tasks: create, list, log time, etc.

Usage:
    python3 mcp_server.py

Add to Claude Code MCP settings:
    {
        "mcpServers": {
            "workload-tracker": {
                "command": "python3",
                "args": ["/path/to/workload-tracker/mcp_server.py"]
            }
        }
    }
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from wt import sync_project_status, get_all_sprints, get_current_sprint, _match_sprint, delete_github_issue, setup_issue_in_project, mins_to_quarter_hours
from wt import build_time_report, format_time_report, _parse_last_arg, get_cached_sprints, get_role_ids
from wt import get_task_repo, get_cached_project_options, _migrate_role_github_fields
# Phase 3 (docs/plan-sprint-bindings.md §4): shadow tasks are gone, replaced by
# per-sprint ``sprint_issues`` bindings. Every ``github_issue`` read goes through
# ``task_current_issue`` and every write through ``set_task_current_issue`` /
# ``clear_task_current_issue``; hours come from ``task_reportable_mins``; the
# cross-sprint split is now ``reconcile_task_sprints``.
from wt import _migrate_shadows_to_bindings, _migrate_recurrent_series_to_bindings
from wt import (task_current_issue, set_task_current_issue, clear_task_current_issue,
                current_binding, task_issue_refs, task_sprint_bindings,
                task_start_sprint, task_reportable_mins, task_sprints_with_time,
                reconcile_task_sprints, close_task)
from wt import _reconcile_outcome_lines, _reconcile_plan_lines, _sprints_for_cli
from wt import _resolve_data_file, fmt_mins as wt_fmt_mins
# Several of the names above are no longer called here — Phase 1 moved their
# callers into wt_api — but they are deliberately still imported. Two reasons,
# both load-bearing:
#   * tools/test_mcp_phase3.py §1 asserts that the sprint-binding accessors are
#     bound on this module (and that the pre-Phase-3 shims are not);
#   * McpStubs monkeypatches mcp_server.get_all_sprints / get_current_sprint /
#     delete_github_issue / sync_project_status by name to keep the harness
#     offline. Dropping the import would silently un-stub them.
# Do not "tidy" them away without updating that harness first.
# Phase 0 of docs/plan-macos-app.md §3: one atomic, locked write path shared by
# the CLI, the TUI and this server. Never write DATA_FILE directly.
from wt import save as wt_save, data_lock  # noqa: F401  (data_lock re-exported)
from datetime import timedelta
# Phase 1 of docs/plan-macos-app.md §4: the dict-returning command layer. The
# tools below *format* wt_api results instead of reimplementing the assembly and
# validation; wt_api is the same layer Phase 2's daemon and the Swift client use.
#
# Only the tools tools/test_mcp_phase3.py actually exercises were moved onto it —
# see the scope block at the top of wt_api.py for the exact split. Everything
# else (Arc, iTerm, tabs, calendar, roles, logs, reporting) stays on its current
# code path until it has regression coverage.
import wt_api
from wt_api import WtError

# Same ``WT_DATA_FILE`` override wt.py uses (Phase 0), so a refactor can be
# exercised against a throwaway copy instead of the live, iCloud-synced file.
# Production runs never set it and this resolves to ~/.workload_tracker.json.
DATA_FILE = _resolve_data_file()
NOTES_DIR = Path.home() / ".workload_tracker_notes"

DEFAULT_ROLES = [
    {"id": "demokit",   "label": "Managing DemoKit",  "color": "blue"},
    {"id": "demos",     "label": "Demos & Workshops", "color": "green"},
    {"id": "strategic", "label": "Strategic Deals",   "color": "yellow"},
    {"id": "other",     "label": "Other",             "color": "white"},
]

STATUS_LABELS = {"todo": "To Do", "inprogress": "In Progress", "recurrent": "Recurrent", "done": "Done"}

mcp = FastMCP("workload-tracker")


def uid() -> str:
    import random
    import string
    return datetime.now().strftime("%Y%m%d%H%M%S") + "".join(
        random.choices(string.ascii_lowercase, k=4)
    )


def load() -> dict:
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text())
        except Exception:
            data = {}
    else:
        data = {}
    data.setdefault("tasks", [])
    data.setdefault("active_timer", None)
    if "roles" not in data:
        data["roles"] = DEFAULT_ROLES.copy()
    # Move role github fields onto tasks (idempotent, same as wt.load()).
    mutated = _migrate_role_github_fields(data)
    # Convert cross-sprint shadow tasks into per-sprint bindings, and strip
    # re-introduced shadows on every load. This MUST run here: wt.load() already
    # does it, so without it the MCP server would be the one process still seeing
    # (and double-counting) shadow tasks that wt.py has converted — or, worse,
    # would write a shadow-bearing file back over a migrated one. Note the
    # deliberate non-short-circuit: both migrations always run.
    mutated = _migrate_shadows_to_bindings(data) or mutated
    mutated = _migrate_recurrent_series_to_bindings(data) or mutated
    if mutated:
        save(data)
    return data


def save(data: dict):
    """Persist *data* via wt.save() — atomic replace under the sidecar lock.

    Same name and signature as before, so no caller changes; the duplicated
    ``write_text`` it replaced was a second, unlocked write path.
    """
    wt_save(data, path=DATA_FILE)


def get_roles(data: dict) -> dict:
    """Return dict of role_id -> label"""
    return {r["id"]: r["label"] for r in data.get("roles", [])}


def get_role_ids(data: dict) -> list:
    """Return list of role IDs"""
    return [r["id"] for r in data.get("roles", [])]


def fmt_mins(mins: float) -> str:
    if not mins:
        return "0m"
    h = int(mins // 60)
    m = int(mins % 60)
    return f"{h}h {m}m" if h else f"{m}m"


def task_logged_mins(task: dict) -> float:
    return sum(log.get("minutes", 0) for log in task.get("logs", []))


def task_live_mins(task: dict, active_timer: dict | None) -> float:
    if active_timer and active_timer.get("task_id") == task["id"]:
        return (time.time() - active_timer["started_at"]) / 60
    return 0.0


def resolve_task(data: dict, query: str) -> dict | None:
    """Find task by ID or partial title match."""
    tasks = data.get("tasks", [])
    # Exact ID match
    match = next((t for t in tasks if t["id"] == query), None)
    if match:
        return match
    # Partial title match (case-insensitive)
    q = query.lower()
    matches = [t for t in tasks if q in t["title"].lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return None  # Ambiguous
    return None


def normalize_issue_ref(issue_ref: str, data: dict) -> tuple[str, str | None]:
    """Normalize issue reference, using default repo for bare numbers.

    Returns (normalized_ref, error_message). If error_message is set, ref is invalid.
    """
    import re

    # Handle full GitHub URL
    url_match = re.match(r'https?://github\.com/([^/]+/[^/]+)/issues/(\d+)', issue_ref)
    if url_match:
        return f"{url_match.group(1)}#{url_match.group(2)}", None

    # Handle bare number or #number
    bare_match = re.match(r'^#?(\d+)$', issue_ref)
    if bare_match:
        repo = data.get("config", {}).get("github_repo")
        if not repo:
            return "", "Issue number requires a default repo. Set config github_repo first, or use full reference: owner/repo#123"
        return f"{repo}#{bare_match.group(1)}", None

    # Already in owner/repo#number format
    return issue_ref, None


def gh_issue_args(issue_ref: str) -> list[str]:
    """Convert owner/repo#123 format to gh command args: ["-R", "owner/repo", "123"]."""
    import re
    match = re.match(r'^([^#]+)#(\d+)$', issue_ref)
    if match:
        return ["-R", match.group(1), match.group(2)]
    # Fallback (URL or other format) - let gh handle it
    return [issue_ref]


# ── wt_api glue ────────────────────────────────────────────────────────────
#
# wt_api distinguishes "no such task" from "that substring matched several", but
# every MCP tool has always collapsed both into one message (mcp_server's own
# resolve_task returns None on an ambiguous match). Keep that contract.
_NO_TASK = ("task_not_found", "ambiguous_task")


def _no_task(task_query: str) -> str:
    return f"No task found matching '{task_query}'"


@mcp.tool()
def add_task(
    title: str,
    role: str = "other",
    status: str = "todo",
    description: str = "",
    github_issue: str = "",
    sprint: str = "",
    github_repo: str = "",
    activity: str = "",
    type: str = "",
) -> str:
    """Add a new task to the workload tracker.

    Args:
        title: The task title (required)
        role: Role ID (use list_roles to see available roles)
        status: One of: todo, inprogress, recurrent, done (default: todo)
        description: Optional task description
        github_issue: Optional GitHub issue reference (e.g., owner/repo#123)
        sprint: Sprint title (e.g., "Sprint 43"). Auto-assigns current sprint if empty.
        github_repo: Optional GitHub repo (owner/repo) for issue creation and the
            close workflow. Tasks without a repo skip GitHub integration.
        activity: Optional GitHub Project Activity field value for this task.
        type: Optional GitHub Project Type field value for this task.
    """
    data = load()
    try:
        res = wt_api.create_task(
            data, title=title, role=role, status=status, description=description,
            github_issue=github_issue, sprint=sprint, github_repo=github_repo,
            activity=activity, type=type)
    except WtError as e:
        if e.code == "sprint_not_found":
            return (f"Error: Sprint '{sprint}' not found. "
                    f"Use list_sprints() to see available sprints.")
        return f"Error: {e.message}"

    save(data)
    task = res["task"]
    sprint_info = f" [{task.get('sprint', 'no sprint')}]" if task.get("sprint") else ""
    result = (f"Created task '{title}' (id: {task['id']}) [{res['role_label']}] "
              f"[{res['status_label']}]{sprint_info}")
    if github_issue:
        result += f" [GitHub: {github_issue}]"
    return result


@mcp.tool()
def list_tasks(role: str | None = None, status: str | None = None, include_done: bool = False) -> str:
    """List all tasks, optionally filtered by role or status.

    By default, done tasks are hidden. Use include_done=True or status="done" to see them.

    Args:
        role: Filter by role ID (use list_roles to see available roles)
        status: Filter by status (todo, inprogress, recurrent, done)
        include_done: Include done tasks in the list (default: False)
    """
    # No shadow-task filter any more: cross-sprint work lives in the task's own
    # ``sprint_issues`` bindings, so there is nothing hidden to exclude (plan
    # §1.1). ``load()`` strips any shadow an older wt.py might reintroduce.
    data = load()
    rows = wt_api.list_tasks(data, role=role, status=status,
                             include_done=include_done)
    if not rows:
        return "No tasks found."

    lines = []
    for r in rows:
        timer_icon = "▶ " if r["running"] else ""
        sprint_str = f" | Sprint: {r['sprint']}" if r.get("sprint") else ""
        lines.append(
            f"{timer_icon}{r['title']}\n"
            f"  ID: {r['id']} | Role: {r['role_label']} | "
            f"Status: {r['status_label']} | Time: {fmt_mins(r['total_mins'])}{sprint_str}"
        )

    return "\n\n".join(lines)


@mcp.tool()
def get_task(task_query: str) -> str:
    """Get details of a specific task by ID or title.

    Includes the task's per-sprint GitHub issue bindings (``sprint_issues``) and
    the sprint the work started in. A task worked across sprint boundaries has
    one binding per sprint — its "current" issue is the newest one, and the
    earlier ones are closed and carry that sprint's hours.

    Args:
        task_query: Task ID or partial title to search for
    """
    data = load()
    # Offline sprint list: get_task is a read-only lookup and must not depend on
    # the network. wt_api.task_detail defaults to config.sprints_cache; bindings
    # carry their own sprint titles, so the cache is only needed to order them
    # and to derive an unfrozen start sprint.
    try:
        d = wt_api.task_detail(data, task_query)
    except WtError as e:
        if e.code in _NO_TASK:
            return _no_task(task_query)
        return f"Error: {e.message}"

    lines = [
        f"Title: {d['title']}",
        f"ID: {d['id']}",
        f"Role: {d['role_label']}",
        f"Sprint: {d['legacy_sprint'] or '(none)'}",
        f"Start sprint: {d['start_sprint_label'] or '(unknown)'}",
        f"Status: {d['status_label']}",
        f"Time logged: {fmt_mins(d['logged_mins'] + d['live_mins'])}",
        f"Timer running: {'Yes' if d['running'] else 'No'}",
        f"GitHub Issue: {d['current_issue'] or '(none)'}",
        f"GitHub Repo: {d['github_repo'] or '(none)'}",
        f"Activity: {d['activity'] or '(none)'}",
        f"Type: {d['type'] or '(none)'}",
        f"Description: {d['description'] or '(none)'}",
    ]

    if d["bindings"]:
        lines.append(f"\nSprint issues ({len(d['bindings'])}):")
        for b in d["bindings"]:
            marker = " ← current" if b["current"] else ""
            hours = b.get("hours_synced")
            hours_str = f"{hours}h" if hours is not None else "hours not synced"
            lines.append(
                f"  {b.get('sprint') or b.get('sprint_id') or '(no sprint)':<12} "
                f"{b.get('issue') or '(no issue)'}  [{b.get('state') or '?'}, "
                f"{hours_str}]{marker}"
            )

    if d["sprints_with_time"]:
        lines.append("\nLogged time by sprint:")
        for e in d["sprints_with_time"]:
            lines.append(f"  {e['sprint_title']:<12} {fmt_mins(e['total_mins'])}")

    if d["logs"]:
        lines.append("\nTime logs:")
        for log in reversed(d["logs"][-5:]):  # Last 5 logs
            dt = datetime.fromtimestamp(log.get("at", 0)).strftime("%Y-%m-%d %H:%M")
            lines.append(f"  {fmt_mins(log['minutes'])} - {log.get('note', '')} [{dt}]")

    return "\n".join(lines)


@mcp.tool()
def start_timer(task_query: str) -> str:
    """Start the timer on a task. Stops any currently running timer.

    Args:
        task_query: Task ID or partial title
    """
    data = load()
    task = resolve_task(data, task_query)
    if not task:
        return f"No task found matching '{task_query}'"

    at = data.get("active_timer")
    result_lines = []

    # Stop current timer if running
    if at:
        prev = next((t for t in data["tasks"] if t["id"] == at["task_id"]), None)
        if prev:
            started_at = at["started_at"]
            ended_at = time.time()
            elapsed = (ended_at - started_at) / 60
            if elapsed > 0.05:
                prev.setdefault("logs", []).append({
                    "id": uid(),
                    "minutes": round(elapsed, 2),
                    "note": "Timer session",
                    "at": ended_at,
                    "started_at": started_at,
                    "ended_at": ended_at,
                })
            result_lines.append(f"Stopped timer on '{prev['title']}' ({fmt_mins(elapsed)})")

    # Start new timer
    data["active_timer"] = {"task_id": task["id"], "started_at": time.time()}
    save(data)
    result_lines.append(f"Started timer on '{task['title']}'")

    # Arc integration: focus space
    if data.get("config", {}).get("arc_space_id"):
        try:
            from arc_browser import TaskTabManager
            manager = TaskTabManager(data)
            result = manager.on_task_started(task)
            if result.get("focused"):
                result_lines.append("[Arc: Focused Workload Tracker space]")
        except ImportError:
            pass

    return "\n".join(result_lines)


@mcp.tool()
def stop_timer() -> str:
    """Stop the currently running timer and log the elapsed time."""
    data = load()
    at = data.get("active_timer")

    if not at:
        return "No timer is currently running."

    task = next((t for t in data["tasks"] if t["id"] == at["task_id"]), None)
    started_at = at["started_at"]
    ended_at = time.time()
    elapsed = (ended_at - started_at) / 60

    if task and elapsed > 0.05:
        task.setdefault("logs", []).append({
            "id": uid(),
            "minutes": round(elapsed, 2),
            "note": "Timer session",
            "at": ended_at,
            "started_at": started_at,
            "ended_at": ended_at,
        })

    data["active_timer"] = None
    save(data)

    result_lines = [f"Stopped timer on '{task['title'] if task else '?'}' ({fmt_mins(elapsed)})"]

    # Arc integration: report unrelated tabs (don't auto-close in MCP)
    if task and data.get("config", {}).get("tab_cleanup_enabled"):
        try:
            from arc_browser import TaskTabManager
            manager = TaskTabManager(data)
            tabs = manager.applescript.get_all_tabs()
            if tabs:
                classifications = manager.classifier.classify_tabs(tabs, task)
                unrelated = manager.classifier.get_unrelated_tabs(classifications)
                if unrelated:
                    result_lines.append(f"\n[Arc] Found {len(unrelated)} potentially unrelated tabs:")
                    for c in unrelated[:5]:  # Show first 5
                        result_lines.append(f"  - {c.tab.title[:40]}")
                    if len(unrelated) > 5:
                        result_lines.append(f"  ... and {len(unrelated) - 5} more")
                    result_lines.append("Use cleanup_task_tabs() to close them.")
        except ImportError:
            pass

    return "\n".join(result_lines)


@mcp.tool()
def log_time(task_query: str, minutes: float, note: str = "Manual entry") -> str:
    """Log time to a task manually.

    Args:
        task_query: Task ID or partial title
        minutes: Number of minutes to log
        note: Optional note for this time entry
    """
    data = load()
    task = resolve_task(data, task_query)
    if not task:
        return f"No task found matching '{task_query}'"

    if minutes <= 0:
        return "Minutes must be greater than 0"

    task.setdefault("logs", []).append({
        "id": uid(),
        "minutes": minutes,
        "note": note,
        "at": time.time(),
    })
    save(data)

    return f"Logged {fmt_mins(minutes)} to '{task['title']}' ({note})"


@mcp.tool()
def list_logs(task_query: str) -> str:
    """List all time logs for a task.

    Args:
        task_query: Task ID or partial title
    """
    data = load()
    task = resolve_task(data, task_query)
    if not task:
        return f"No task found matching '{task_query}'"

    logs = task.get("logs", [])
    if not logs:
        return f"No time logs for '{task['title']}'"

    total = sum(l.get("minutes", 0) for l in logs)
    lines = [
        f"Time logs for: {task['title']}",
        f"Total: {fmt_mins(total)}",
        ""
    ]

    for log in logs:
        log_id = log.get("id", "?")[:11]
        mins = fmt_mins(log.get("minutes", 0))
        note = log.get("note", "—")[:30]
        started = log.get("started_at")
        ended = log.get("ended_at")
        at = log.get("at", 0)

        if started and ended:
            start_str = datetime.fromtimestamp(started).strftime("%H:%M")
            end_str = datetime.fromtimestamp(ended).strftime("%H:%M")
            time_range = f"[{start_str}-{end_str}]"
        else:
            time_range = ""

        at_str = datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M") if at else ""
        lines.append(f"{log_id}...  {mins:>7}  {note:<30}  {time_range:>13}  {at_str}")

    return "\n".join(lines)


@mcp.tool()
def report_time_range(
    start_date: str | None = None,
    end_date: str | None = None,
    sprint: str | None = None,
    last_days: int | None = None,
    role: str | None = None,
    as_json: bool = True,
) -> str:
    """Show all logged time between two dates.

    Provide one of: ``start_date`` + ``end_date`` (YYYY-MM-DD), a ``sprint``
    title (e.g. "Sprint 100"), or ``last_days`` (e.g. 7). If none are given,
    defaults to the current sprint, falling back to the last 7 days when no
    current sprint is available.

    Args:
        start_date: ISO date YYYY-MM-DD (inclusive); requires end_date.
        end_date:   ISO date YYYY-MM-DD (inclusive); requires start_date.
        sprint:     Sprint title to use as the date range.
        last_days:  Number of days back from today (inclusive).
        role:       Optional role id filter.
        as_json:    When True (default) returns the structured JSON document
            documented in CLAUDE.md / wt report --json. When False, returns
            the same plain-text rendering the CLI prints (no ANSI codes).

    Returns the report as a string.
    """
    # Mutual-exclusion check (positional dates vs sprint vs last_days).
    has_positional = bool(start_date or end_date)
    if has_positional and (sprint or last_days is not None):
        return "Error: provide either start_date+end_date, sprint, or last_days (not multiple)."
    if sprint and last_days is not None:
        return "Error: provide either start_date+end_date, sprint, or last_days (not multiple)."

    data = load()

    if has_positional:
        if not (start_date and end_date):
            return "Error: both start_date and end_date are required when using positional dates."
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError as e:
            return f"Error: invalid date (expected YYYY-MM-DD): {e}"
        if end < start:
            return "Error: end_date must be on or after start_date."
        resolved_sprint = None
    elif sprint:
        sprints = get_cached_sprints(data) or get_all_sprints(data)
        if not sprints:
            return "Error: no sprints available (project not configured or query failed)."
        match = _match_sprint(sprints, sprint)
        if not match:
            return f"Error: no sprint matching '{sprint}'."
        start = match["start_date"]
        end = match["end_date"] - timedelta(days=1)
        resolved_sprint = match
    elif last_days is not None:
        try:
            days = _parse_last_arg(str(last_days))
        except ValueError as e:
            return f"Error: {e}"
        today = datetime.now().date()
        start = today - timedelta(days=days - 1)
        end = today
        resolved_sprint = None
    else:
        current = get_current_sprint(data)
        if current:
            start = current["start_date"]
            end = current["end_date"] - timedelta(days=1)
            resolved_sprint = current
        else:
            today = datetime.now().date()
            start = today - timedelta(days=6)
            end = today
            resolved_sprint = None

    if role is not None and role not in get_role_ids(data):
        return f"Error: unknown role '{role}'. Known: {', '.join(get_role_ids(data))}"

    payload = build_time_report(data, start, end, sprint=resolved_sprint, role_id=role)

    if as_json:
        return json.dumps(payload, indent=2, ensure_ascii=False)
    return format_time_report(payload, use_color=False)


@mcp.tool()
def edit_log(task_query: str, log_id: str, minutes: float | None = None, note: str | None = None) -> str:
    """Edit a log entry's minutes or note.

    Args:
        task_query: Task ID or partial title
        log_id: Log ID or prefix (first 8+ characters)
        minutes: New minutes value (optional)
        note: New note text (optional)
    """
    data = load()
    task = resolve_task(data, task_query)
    if not task:
        return f"No task found matching '{task_query}'"

    if minutes is None and note is None:
        return "Error: specify minutes and/or note to update"

    logs = task.get("logs", [])
    log = next((l for l in logs if l.get("id", "").startswith(log_id)), None)
    if not log:
        return f"No log found with ID starting with '{log_id}'"

    old_mins = log.get("minutes", 0)
    old_note = log.get("note", "")

    if minutes is not None:
        log["minutes"] = minutes
    if note is not None:
        log["note"] = note

    save(data)

    changes = []
    if minutes is not None:
        changes.append(f"{fmt_mins(old_mins)} → {fmt_mins(minutes)}")
    if note is not None:
        changes.append(f"note → '{note}'")

    return f"Updated log: {', '.join(changes)}"


@mcp.tool()
def delete_log(task_query: str, log_id: str) -> str:
    """Delete a log entry.

    Args:
        task_query: Task ID or partial title
        log_id: Log ID or prefix (first 8+ characters)
    """
    data = load()
    task = resolve_task(data, task_query)
    if not task:
        return f"No task found matching '{task_query}'"

    logs = task.get("logs", [])
    log = next((l for l in logs if l.get("id", "").startswith(log_id)), None)
    if not log:
        return f"No log found with ID starting with '{log_id}'"

    mins = log.get("minutes", 0)
    note = log.get("note", "—")

    task["logs"] = [l for l in logs if l.get("id") != log.get("id")]
    save(data)

    return f"Deleted log: {fmt_mins(mins)} — {note}"


@mcp.tool()
def split_log(task_query: str, log_id: str, split_at_minutes: float) -> str:
    """Split a log entry at a specified minute mark.

    Args:
        task_query: Task ID or partial title
        log_id: Log ID or prefix (first 8+ characters)
        split_at_minutes: Minute mark to split at (creates two entries)
    """
    data = load()
    task = resolve_task(data, task_query)
    if not task:
        return f"No task found matching '{task_query}'"

    logs = task.get("logs", [])
    log_idx = next((i for i, l in enumerate(logs) if l.get("id", "").startswith(log_id)), None)
    if log_idx is None:
        return f"No log found with ID starting with '{log_id}'"

    log = logs[log_idx]
    total_mins = log.get("minutes", 0)

    if split_at_minutes <= 0 or split_at_minutes >= total_mins:
        return f"Error: split point must be between 0 and {total_mins}"

    first_mins = split_at_minutes
    second_mins = total_mins - split_at_minutes
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

    return f"Split {fmt_mins(total_mins)} into {fmt_mins(first_mins)} + {fmt_mins(second_mins)}"


@mcp.tool()
def merge_logs(task_query: str, log_id_1: str, log_id_2: str) -> str:
    """Merge two log entries into one.

    Args:
        task_query: Task ID or partial title
        log_id_1: First log ID or prefix
        log_id_2: Second log ID or prefix
    """
    data = load()
    task = resolve_task(data, task_query)
    if not task:
        return f"No task found matching '{task_query}'"

    logs = task.get("logs", [])
    log1 = next((l for l in logs if l.get("id", "").startswith(log_id_1)), None)
    log2 = next((l for l in logs if l.get("id", "").startswith(log_id_2)), None)

    if not log1:
        return f"No log found with ID starting with '{log_id_1}'"
    if not log2:
        return f"No log found with ID starting with '{log_id_2}'"
    if log1.get("id") == log2.get("id"):
        return "Error: cannot merge a log with itself"

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

    if started1 and started2:
        merged_log["started_at"] = min(started1, started2)
    if ended1 and ended2:
        merged_log["ended_at"] = max(ended1, ended2)

    # Remove old logs and add merged
    task["logs"] = [l for l in logs if l.get("id") not in (log1.get("id"), log2.get("id"))]
    task["logs"].append(merged_log)
    task["logs"].sort(key=lambda x: x.get("at", 0))

    save(data)
    return f"Merged {fmt_mins(log1.get('minutes', 0))} + {fmt_mins(log2.get('minutes', 0))} = {fmt_mins(combined_mins)}"


@mcp.tool()
def set_task_status(task_query: str, status: str, create_issue: bool = False) -> str:
    """Set the status of a task.

    When setting status to 'done', this triggers the close workflow:
    - If the task has a GitHub repo set, it must have a linked issue
    - The issue is added to the configured GitHub project with logged hours

    Args:
        task_query: Task ID or partial title
        status: New status: todo, inprogress, recurrent, or done
        create_issue: If True and setting to done, create GitHub issue if missing
    """
    data = load()
    task = resolve_task(data, task_query)
    if not task:
        return f"No task found matching '{task_query}'"

    if status not in STATUS_LABELS:
        return f"Invalid status '{status}'. Use: todo, inprogress, recurrent, done"

    try:
        res = wt_api.set_status(data, task["id"], status,
                                create_issue=create_issue, save_callback=save)
    except WtError as e:
        return f"Error: {e.message}"

    # Transitioning to "done" runs the full close workflow.
    if res["closed"]:
        return _render_close(res, create_issue)

    result_msg = (f"Changed '{task['title']}' from {res['old_status_label']} "
                  f"to {res['status_label']}")
    if res["project_synced"]:
        result_msg += " (project synced)"
    return result_msg


def _render_close(result: dict, create_issue: bool) -> str:
    """Render a :func:`wt_api.close` result.

    The workflow itself lives in ``wt_api.close`` -> ``wt.close_task``, so the MCP
    close does exactly what ``wt done`` and the TUI's ``D`` do: reconcile the
    per-sprint bindings first (so each past sprint's hours land on that sprint's
    own issue), report the *current binding's* sprint hours rather than the task
    total, set Status/Activity/Type/Sprint on the project item, and close only
    the current binding's issue.

    ``create_issue`` only affects the wording of the refusal — ``wt_api.close``
    has already wired it to ``close_task``'s ``prompt_callback``, where False
    means "refuse" rather than "mint one anyway".
    """
    title = result["title"]

    if not result["success"]:
        error = result.get("error") or "unknown error"
        if not create_issue and "must have GitHub issue" in error:
            return (
                f"Error: Task '{title}' has no GitHub issue linked.\n"
                f"This task has a repo configured ({result.get('repo')}), so closing requires an issue.\n"
                f"Either:\n"
                f"  - Link an existing issue: link_github_issue('{title}', 'owner/repo#123')\n"
                f"  - Create one: set_task_status('{title}', 'done', create_issue=True)"
            )
        return f"Error closing '{title}': {error}"

    if result.get("skipped_github"):
        return f"Closed '{title}' (task has no repo — no GitHub integration)"

    issue_ref = result["current_issue"]
    lines = [f"Closed '{title}'"]
    if result.get("issue_created"):
        lines.append(f"  Created issue: {issue_ref}")

    # Per-sprint reconcile detail (issues minted/carried forward/closed for the
    # sprints this task spanned). Rendered by wt so the wording matches `wt done`.
    for line in result.get("outcome_lines") or []:
        lines.append(f"  {line}")

    if result.get("project_updated"):
        hours = result.get("hours_synced")
        lines.append("  Added to project"
                     + (f" (Hours: {hours})" if hours is not None else ""))
    if result.get("issue_closed"):
        lines.append(f"  Closed issue: {issue_ref}")
    if result.get("comment_added"):
        lines.append("  Added closing comment")
    if result.get("error"):
        # close_task marks the task done even when the project update fails.
        lines.append(f"  Warning: {result['error']}")

    return "\n".join(lines)


@mcp.tool()
def close_previous_recurrent_tasks(all_previous: bool = False, dry_run: bool = False) -> str:
    """RETIRED — recurring work no longer has per-sprint copies to close.

    Use sync_task_sprints(all_tasks=True, create_issues=True) instead: it closes
    the sprint that just ended and opens the new one on the same perpetual task.

    Args:
        all_previous: ignored; kept so an old call still gets the explanation.
        dry_run: ignored; kept so an old call still gets the explanation.
    """
    # The planner behind this tool (wt.find_recurrent_tasks_to_close /
    # close_previous_sprint_recurrent_tasks) has been deleted. Its selection rule
    # — status == "recurrent" plus a prior-sprint sprint_id — matches the merged
    # perpetual task, so a call would have ended a live recurrence. Only the
    # refusal remains, so a caller gets a pointer rather than a missing tool.
    return (
        "close_previous_recurrent_tasks has been retired.\n\n"
        "Recurring work is now one perpetual task with a GitHub issue per\n"
        "sprint, so there are no per-sprint copies to close. The merged task\n"
        "matches this tool's old selection rule, so running it would end the\n"
        "recurrence outright.\n\n"
        "Use sync_task_sprints(all_tasks=True, create_issues=True) instead: it\n"
        "closes the sprint that just ended and opens the new one."
    )


@mcp.tool()
def delete_task(task_query: str) -> str:
    """Delete a task.

    Args:
        task_query: Task ID or partial title
    """
    # Past-sprint bindings are separate, already-closed issues. Deleting them is
    # not what delete_task ever did (they used to hang off shadow tasks), so
    # wt_api names them instead of silently orphaning them.
    data = load()
    try:
        res = wt_api.delete_task(data, task_query, save_callback=save)
    except WtError as e:
        if e.code in _NO_TASK:
            return _no_task(task_query)
        return f"Error: {e.message}"

    result = f"Deleted task '{res['title']}'"
    if res["issue"]:
        if res["issue_deleted"]:
            result += f"\nDeleted GitHub issue: {res['issue']}"
        else:
            result += (f"\nWarning: Failed to delete GitHub issue {res['issue']} "
                       f"(may need admin permissions)")
    if res["other_issues"]:
        result += (f"\nNote: {len(res['other_issues'])} past-sprint issue(s) "
                   f"left in place: " + ", ".join(res["other_issues"]))
    return result


@mcp.tool()
def rename_task(task_query: str, new_title: str) -> str:
    """Rename a task. Also updates the linked GitHub issue title if present.

    Args:
        task_query: Task ID or partial title
        new_title: The new title for the task
    """
    # wt_api.rename_task also retitles the *current* binding's GitHub issue.
    # Past-sprint issues keep their " (Sprint N)" titles — renaming those is not
    # this tool's job (and would need the suffix re-applied per binding).
    data = load()
    try:
        res = wt_api.rename_task(data, task_query, new_title, save_callback=save)
    except WtError as e:
        if e.code in _NO_TASK:
            return _no_task(task_query)
        return f"Error: {e.message}"

    result_lines = [f"Renamed '{res['old_title']}' → '{res['new_title']}'"]
    if res["issue"]:
        if res["issue_updated"]:
            result_lines.append(f"Updated GitHub issue: {res['issue']}")
        else:
            result_lines.append("Warning: Failed to update GitHub issue title: "
                                f"{res['issue_error']}")

    return "\n".join(result_lines)


@mcp.tool()
def get_status() -> str:
    """Get an overview of time logged by role and any running timer."""
    data = load()
    ov = wt_api.status_overview(data)

    lines = [f"Workload Tracker — {ov['n_tasks']} tasks — "
             f"{fmt_mins(ov['total_mins'])} total\n"]

    for entry in ov["by_role"]:
        pct = entry["pct"]
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        lines.append(f"{entry['label']:<25} {bar} {pct:>3}%  {fmt_mins(entry['mins'])}")

    if ov["active"]:
        active = ov["active"]
        lines.append(f"\n▶ Timer running: {active['title'] or '?'} "
                     f"({fmt_mins(active['elapsed_mins'])})")

    return "\n".join(lines)


@mcp.tool()
def get_notes_path(task_query: str) -> str:
    """Get the notes location for a task. Returns GitHub issue info if linked, else local file path.

    Args:
        task_query: Task ID or partial title
    """
    data = load()
    try:
        res = wt_api.notes_target(data, task_query, NOTES_DIR)
    except WtError as e:
        if e.code in _NO_TASK:
            return _no_task(task_query)
        return f"Error: {e.message}"

    title = res["task"]["title"]
    if res["kind"] == "issue":
        return (
            f"Task '{title}' is linked to GitHub issue: {res['issue']}\n"
            f"View: gh issue view {res['issue']}\n"
            f"Comment: gh issue comment {res['issue']}"
        )
    return f"Notes file for '{title}':\n{res['path']}"


@mcp.tool()
def push_task_to_github(task_query: str) -> str:
    """Sync a task's logged time, status, activity, and sprint to its linked GitHub issue.

    Updates all GitHub Project fields that would be set during a close, but
    does NOT close the issue. The task remains in its current status.

    Pushes to the task's *current* sprint binding's issue, with only that sprint's
    hours — past sprints' hours already live on their own bindings' issues, so the
    task total would double-count.

    Args:
        task_query: Task ID or partial title
    """
    data = load()
    try:
        res = wt_api.push_to_github(data, task_query)
    except WtError as e:
        if e.code in _NO_TASK:
            return f"ERROR: no task matching '{task_query}'"
        if e.code == "not_linked":
            return (f"ERROR: task '{e.details['title']}' has no linked "
                    f"GitHub issue")
        return f"ERROR: {e.message}"
    save(data)  # persist the mark_logs_uploaded side-effect
    if res["success"]:
        return f"Pushed '{res['task']['title']}' to {res['issue']}: {res['hours']}h"
    return f"Push completed with errors: {', '.join(res['errors'])}"


@mcp.tool()
def link_github_issue(task_query: str, github_issue: str) -> str:
    """Link a task to a GitHub issue.

    Args:
        task_query: Task ID or partial title
        github_issue: GitHub issue reference (owner/repo#123, URL, or bare number with default repo)
    """
    # wt_api.link_issue normalizes the ref, validates it with `gh issue view`,
    # stores it on the task's current sprint *binding* (mirroring the legacy flat
    # key), and pins github_repo so the close workflow engages.
    data = load()
    try:
        res = wt_api.link_issue(data, task_query, github_issue)
    except WtError as e:
        if e.code in _NO_TASK:
            return _no_task(task_query)
        return f"Error: {e.message}"
    save(data)

    info = res["issue_info"]
    return (f"Linked '{res['task']['title']}' to GitHub issue "
            f"#{info['number']}: {info['title']}")


@mcp.tool()
def unlink_github_issue(task_query: str) -> str:
    """Unlink a task from its GitHub issue.

    Args:
        task_query: Task ID or partial title
    """
    data = load()
    try:
        res = wt_api.unlink_issue(data, task_query)
    except WtError as e:
        if e.code in _NO_TASK:
            return _no_task(task_query)
        if e.code == "not_linked":
            return e.message
        return f"Error: {e.message}"
    save(data)

    msg = f"Unlinked '{res['task']['title']}' from {res['old_issue']}"
    if res["remaining"]:
        msg += (f"\nStill bound to {len(res['remaining'])} past-sprint issue(s): "
                + ", ".join(res["remaining"]))
    return msg


@mcp.tool()
def view_github_issue(task_query: str) -> str:
    """View the GitHub issue body and comments for a linked task.

    Args:
        task_query: Task ID or partial title
    """
    import subprocess

    data = load()
    task = resolve_task(data, task_query)
    if not task:
        return f"No task found matching '{task_query}'"

    issue_ref = task_current_issue(task, data)
    if not issue_ref:
        return f"Task '{task['title']}' is not linked to a GitHub issue."

    result = subprocess.run(
        ["gh", "issue", "view", *gh_issue_args(issue_ref), "--json", "title,body,comments"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return f"Error viewing issue: {result.stderr}"

    import json as json_mod
    issue = json_mod.loads(result.stdout)
    lines = [
        f"# {issue['title']}",
        "",
        issue.get('body') or '(no body)',
        "",
        f"--- Comments ({len(issue.get('comments', []))}) ---"
    ]
    for c in issue.get('comments', [])[-5:]:  # Last 5 comments
        author = c.get('author', {}).get('login', '?')
        body = c.get('body', '')[:200]
        lines.append(f"\n[{author}]: {body}...")

    return "\n".join(lines)


@mcp.tool()
def add_github_comment(task_query: str, comment: str) -> str:
    """Add a comment to the GitHub issue linked to a task.

    Args:
        task_query: Task ID or partial title
        comment: The comment text to add
    """
    import subprocess

    data = load()
    task = resolve_task(data, task_query)
    if not task:
        return f"No task found matching '{task_query}'"

    issue_ref = task_current_issue(task, data)
    if not issue_ref:
        return f"Task '{task['title']}' is not linked to a GitHub issue."

    result = subprocess.run(
        ["gh", "issue", "comment", *gh_issue_args(issue_ref), "-b", comment],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return f"Error adding comment: {result.stderr}"

    return f"Added comment to {issue_ref}"


@mcp.tool()
def list_roles() -> str:
    """List all available roles."""
    data = load()
    lines = ["Available roles:\n"]
    for r in data.get("roles", []):
        task_count = len([t for t in data["tasks"] if t.get("role_id") == r["id"]])
        lines.append(f"  {r['id']:<15} {r['label']:<35} ({task_count} tasks)")
    return "\n".join(lines)


@mcp.tool()
def add_role(role_id: str, label: str) -> str:
    """Add a new role.

    Args:
        role_id: Unique identifier for the role (lowercase, no spaces)
        label: Display label for the role
    """
    data = load()
    role_id = role_id.lower().strip()

    if any(r["id"] == role_id for r in data["roles"]):
        return f"Error: Role '{role_id}' already exists."

    data["roles"].append({"id": role_id, "label": label, "color": "white"})
    save(data)
    return f"Created role: {role_id} ({label})"


@mcp.tool()
def update_role(role_id: str, new_label: str) -> str:
    """Update an existing role's label.

    Args:
        role_id: The role ID to update
        new_label: New display label
    """
    data = load()
    role = next((r for r in data["roles"] if r["id"] == role_id), None)

    if not role:
        return f"Error: Role '{role_id}' not found."

    old_label = role["label"]
    role["label"] = new_label
    save(data)
    return f"Updated role: {role_id} ('{old_label}' → '{new_label}')"


@mcp.tool()
def delete_role(role_id: str) -> str:
    """Delete a role. Will fail if any tasks use this role.

    Args:
        role_id: The role ID to delete
    """
    data = load()
    role = next((r for r in data["roles"] if r["id"] == role_id), None)

    if not role:
        return f"Error: Role '{role_id}' not found."

    task_count = len([t for t in data["tasks"] if t.get("role_id") == role_id])
    if task_count > 0:
        return f"Error: Cannot delete role '{role_id}': {task_count} tasks use it. Reassign or delete those tasks first."

    data["roles"] = [r for r in data["roles"] if r["id"] != role_id]
    save(data)
    return f"Deleted role: {role_id}"


@mcp.tool()
def set_task_repo(task_query: str, github_repo: str | None = None) -> str:
    """Set or clear the GitHub repo for a task.

    When a task has a repo, closing it requires a GitHub issue (auto-created in
    that repo if missing). Tasks without a repo skip GitHub integration entirely.

    Args:
        task_query: Task ID or partial title to search for
        github_repo: GitHub repo in owner/repo format, or None/empty to clear
    """
    data = load()
    task = resolve_task(data, task_query)
    if not task:
        return f"No task found matching '{task_query}'"

    if github_repo:
        if "/" not in github_repo or github_repo.count("/") != 1:
            return "Error: Repo must be in owner/repo format (e.g., 'grafana/field-eng')"
        task["github_repo"] = github_repo
        save(data)
        return f"Set GitHub repo for '{task['title']}': {github_repo}"
    if task.pop("github_repo", None) is not None:
        save(data)
        return f"Cleared GitHub repo for '{task['title']}'"
    return f"'{task['title']}' has no GitHub repo set."


def _set_task_project_option(task_query: str, key: str, label: str, value: str | None) -> str:
    """Shared logic for set_task_activity / set_task_type."""
    data = load()
    task = resolve_task(data, task_query)
    if not task:
        return f"No task found matching '{task_query}'"

    if value:
        options = get_cached_project_options(data).get(key)
        if options and value not in options:
            return f"Error: Unknown {label.lower()} '{value}'. Available: {', '.join(options)}"
        task[key] = value
        save(data)
        return f"Set {label.lower()} for '{task['title']}': {value}"
    if task.pop(key, None) is not None:
        save(data)
        return f"Cleared {label.lower()} for '{task['title']}'"
    return f"'{task['title']}' has no {label.lower()} set."


@mcp.tool()
def set_task_activity(task_query: str, activity: str | None = None) -> str:
    """Set or clear the GitHub Project Activity field value for a task.

    Validated against the cached project option list when available
    (config.project_options_cache).

    Args:
        task_query: Task ID or partial title to search for
        activity: Activity option name, or None/empty to clear
    """
    return _set_task_project_option(task_query, "activity", "Activity", activity)


@mcp.tool()
def set_task_type(task_query: str, type: str | None = None) -> str:
    """Set or clear the GitHub Project Type field value for a task.

    Validated against the cached project option list when available
    (config.project_options_cache).

    Args:
        task_query: Task ID or partial title to search for
        type: Type option name, or None/empty to clear
    """
    return _set_task_project_option(task_query, "type", "Type", type)


@mcp.tool()
def create_task_from_issue(issue_ref: str, role: str = "other") -> str:
    """Create a task from a GitHub issue.

    Args:
        issue_ref: GitHub issue URL, reference (owner/repo#123), or bare number (uses default repo)
        role: Role ID for the new task (default: other)
    """
    import subprocess

    data = load()
    roles = get_roles(data)

    if role not in roles:
        return f"Error: Invalid role '{role}'. Available: {', '.join(roles.keys())}"

    # Normalize issue reference
    issue_ref, err = normalize_issue_ref(issue_ref, data)
    if err:
        return f"Error: {err}"

    # Fetch issue details
    result = subprocess.run(
        ["gh", "issue", "view", *gh_issue_args(issue_ref), "--json", "number,title,state,url"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return f"Error: Could not find GitHub issue: {issue_ref}"

    issue_info = json.loads(result.stdout)

    # Map GitHub state to task status
    # OPEN -> inprogress, CLOSED -> done
    gh_state = issue_info.get("state", "OPEN").upper()
    status = "done" if gh_state == "CLOSED" else "inprogress"

    # Check if a task already exists for this issue — check every binding, not
    # just the legacy field, so a past-sprint issue matches too.
    for t in data["tasks"]:
        if issue_ref in task_issue_refs(t):
            return f"Task already exists for {issue_ref}: '{t['title']}' (id: {t['id']})"

    task = {
        "id": uid(),
        "title": issue_info["title"],
        "description": "",
        "role_id": role,
        "status": status,
        "logs": [],
        "created_at": time.time(),
    }
    # The issue ref pins the task's repo
    if "#" in issue_ref:
        task["github_repo"] = issue_ref.split("#", 1)[0]
    # Record the link as a binding (and mirror the legacy github_issue key).
    set_task_current_issue(task, issue_ref, data)
    data["tasks"].insert(0, task)
    save(data)

    return (
        f"Created task '{task['title']}' (id: {task['id']})\n"
        f"Role: {roles[role]} | Status: {STATUS_LABELS[status]}\n"
        f"GitHub: {issue_ref}"
    )


@mcp.tool()
def setup_arc_space() -> str:
    """Set up Arc browser integration with Workload Tracker space and role folders.

    Note: Arc must be quit before running this. Changes require Arc restart.
    """
    try:
        from arc_browser import TaskTabManager, ArcAppleScript
    except ImportError:
        return "Error: arc_browser module not found."

    data = load()
    applescript = ArcAppleScript()

    if applescript.is_arc_running():
        return (
            "Error: Arc is currently running.\n"
            "Please quit Arc first, then run this command again.\n"
            "Changes to Arc's sidebar require Arc to be closed."
        )

    manager = TaskTabManager(data)
    result = manager.setup_space_and_folders(save)

    if result.get("errors"):
        return "Errors:\n" + "\n".join(result["errors"])

    lines = [
        f"Created Workload Tracker space: {result['space_id']}",
        f"Created {len(result.get('role_folders', {}))} role folders",
    ]

    # Enable tab cleanup
    data.setdefault("config", {})["tab_cleanup_enabled"] = True
    save(data)
    lines.append("Tab cleanup enabled")
    lines.append("\nRestart Arc to see the changes.")

    return "\n".join(lines)


@mcp.tool()
def get_arc_status() -> str:
    """Get the current Arc browser integration status."""
    try:
        from arc_browser import TaskTabManager
    except ImportError:
        return "Error: arc_browser module not found."

    data = load()
    manager = TaskTabManager(data)
    status = manager.get_status()

    lines = [
        "Arc Integration Status:",
        f"  Enabled: {'Yes' if status['enabled'] else 'No'}",
        f"  Space ID: {status['space_id'] or '(not set)'}",
        f"  Tab cleanup: {'On' if status['tab_cleanup_enabled'] else 'Off'}",
        f"  Confidence threshold: {status['confidence_threshold']:.0%}",
        f"  Arc running: {'Yes' if status['arc_running'] else 'No'}",
        f"  Role folders: {status['role_folders']}",
        f"  Task folders: {status['task_folders']}",
    ]
    return "\n".join(lines)


@mcp.tool()
def cleanup_task_tabs(task_query: str | None = None, close_tabs: bool = False) -> str:
    """Analyze and optionally close unrelated tabs for a task.

    Args:
        task_query: Task ID or partial title (uses active task if not specified)
        close_tabs: If True, close the unrelated tabs. If False, just report them.
    """
    try:
        from arc_browser import TaskTabManager
    except ImportError:
        return "Error: arc_browser module not found."

    data = load()

    # Find task
    if task_query:
        task = resolve_task(data, task_query)
        if not task:
            return f"No task found matching '{task_query}'"
    else:
        at = data.get("active_timer")
        if not at:
            return "No active timer. Specify a task or start a timer first."
        task = next((t for t in data["tasks"] if t["id"] == at["task_id"]), None)
        if not task:
            return "Active task not found."

    manager = TaskTabManager(data)
    tabs = manager.applescript.get_all_tabs()

    if not tabs:
        return "No tabs found in Arc."

    classifications = manager.classifier.classify_tabs(tabs, task)
    unrelated = manager.classifier.get_unrelated_tabs(classifications)

    if not unrelated:
        return f"All {len(tabs)} tabs appear related to '{task['title']}'."

    lines = [f"Task: {task['title']}", f"Found {len(unrelated)} potentially unrelated tabs:\n"]

    for c in unrelated:
        lines.append(f"  • {c.tab.title[:50]}")
        lines.append(f"    {c.tab.url[:60]}")
        lines.append(f"    Reason: {c.reason}\n")

    if close_tabs:
        closed = 0
        for _ in unrelated:
            if manager.applescript.close_current_tab():
                closed += 1
                import time as t
                t.sleep(0.1)
        lines.append(f"\nClosed {closed} tabs.")
    else:
        lines.append("\nSet close_tabs=True to close these tabs.")

    return "\n".join(lines)


@mcp.tool()
def sync_arc_folders() -> str:
    """Sync Arc folders with current roles and tasks.

    Creates missing role and task folders. Requires Arc to be quit.
    """
    try:
        from arc_browser import TaskTabManager, ArcAppleScript
    except ImportError:
        return "Error: arc_browser module not found."

    data = load()

    if not data.get("config", {}).get("arc_space_id"):
        return "Error: Arc space not set up. Run setup_arc_space() first."

    applescript = ArcAppleScript()
    if applescript.is_arc_running():
        return (
            "Error: Arc is currently running.\n"
            "Please quit Arc first, then run this command again."
        )

    manager = TaskTabManager(data)
    result = manager.sync_folders(save)

    lines = [
        f"Synced {result['roles_synced']} role folders",
        f"Synced {result['tasks_synced']} task folders",
    ]

    if result.get("errors"):
        lines.append("\nErrors:")
        lines.extend(f"  - {e}" for e in result["errors"])

    if result.get("restart_required"):
        lines.append("\nRestart Arc to see the changes.")

    return "\n".join(lines)


# ── Sprint Management ──────────────────────────────────────


@mcp.tool()
def list_sprints() -> str:
    """List all available sprint iterations from the GitHub project."""
    # wt_api.sprints_overview prefers the live fetch and falls back to
    # config.sprints_cache so this still answers offline. It reads the
    # start_date/end_date date objects both sources provide — the old code read
    # the camelCase startDate/duration keys that only the live fetch emits, so a
    # cache-backed dict raised KeyError.
    data = load()
    ov = wt_api.sprints_overview(data)
    if not ov["sprints"]:
        return "No sprints found (project not configured or query failed)."

    lines = ["Available sprints:"
             + ("  (offline — persisted cache)" if ov["from_cache"] else "")]
    for s in ov["sprints"]:
        marker = " ← current" if s["id"] == ov["current_id"] else ""
        days = (s["end_date"] - s["start_date"]).days
        lines.append(f"  {s['title']} ({s['start_date']}, {days} days){marker}")
    return "\n".join(lines)


@mcp.tool()
def get_current_sprint_info() -> str:
    """Get information about the current sprint."""
    data = load()
    info = wt_api.current_sprint_info(data)
    if not info:
        return "No active sprint found."
    return (
        f"Current sprint: {info['title']}\n"
        f"Start: {info['start_date']}\n"
        f"End: {info['last_day']}\n"
        f"Duration: {info['days']} days"
        + ("\n(offline — persisted sprints cache)" if info["from_cache"] else "")
    )


@mcp.tool()
def set_sprint(task_query: str, sprint_title: str) -> str:
    """Correct the sprint a task *started* in.

    Since the move to per-sprint issue bindings this no longer re-points a task
    forward. Which sprint a task's hours are billed to is derived from its log
    timestamps and materialised as ``sprint_issues`` bindings by
    ``sync_task_sprints`` — it is not something to set by hand. The one sprint
    field a human still owns is ``start_sprint`` ("when did this work begin"),
    which is derived from the earliest log and then frozen so a later log edit
    can't rewrite history. This tool is the override.

    To change which sprint a task's *time* lands in, edit the log timestamps and
    run ``sync_task_sprints(task_query)``.

    Args:
        task_query: Task ID or partial title
        sprint_title: Sprint title (e.g., "Sprint 43"), or "none" to clear the
            start sprint so it is re-derived from the task's earliest log.
    """
    data = load()
    try:
        res = wt_api.set_start_sprint(data, task_query, sprint_title)
    except WtError as e:
        if e.code in _NO_TASK:
            return _no_task(task_query)
        if e.code == "sprint_not_found":
            available = ", ".join(e.details.get("recent") or [])
            return f"No sprint matching '{sprint_title}'. Recent: {available}"
        return e.message  # no_sprints -> "No sprints found."
    save(data)

    title = res["task"]["title"]
    if res["cleared"]:
        if res["had"]:
            return (f"Cleared the start sprint for '{title}' — it will be "
                    f"re-derived from the task's earliest log.")
        return f"'{title}' had no start sprint set."
    return (f"'{title}' now starts in {res['sprint']['title']} (start sprint only — "
            f"hours follow the logs; run sync_task_sprints to re-derive bindings)")


@mcp.tool()
def sync_task_sprints(
    task_query: str | None = None,
    all_tasks: bool = False,
    create_issues: bool | None = None,
    dry_run: bool = False,
) -> str:
    """Reconcile a task's per-sprint GitHub issue bindings against its logs.

    Replaces the old ``sprint_split`` tool. For each sprint the task has logged
    time in (plus the current sprint while the task is open) there should be one
    ``sprint_issues`` binding carrying that sprint's hours. This creates the
    missing ones, carries the task's current issue forward to its most recent
    sprint, re-pushes hours that have drifted, and closes issues whose sprint has
    ended. Idempotent: a second run finds nothing to do.

    Recurrent tasks are **skipped** and reported — they intentionally span
    sprints and are handled by close_previous_recurrent_tasks / ``wt
    new-recurrent`` instead.

    Args:
        task_query: Task ID or partial title. Required unless all_tasks=True.
        all_tasks: Reconcile every non-recurrent task instead of one.
        create_issues: Whether new GitHub issues may be minted for sprints that
            have none. Defaults to True for a single task and **False** for
            all_tasks=True, because a blanket run over the real data would mint
            ~25 issues; pass True explicitly to allow that. Sprints that would
            need an issue are reported rather than bound issue-less, so a later
            create_issues=True run can still mint them.
        dry_run: Print the plan and change nothing (no GitHub calls at all).
    """
    if all_tasks and task_query:
        return "Error: pass either task_query or all_tasks=True, not both."
    if not all_tasks and not task_query:
        return "Error: task_query is required unless all_tasks=True."

    data = load()
    # Plan pass. dry_run=True is structurally read-only in reconcile_task_sprints
    # (it plans, then returns without executing), so this makes no GitHub calls.
    #
    # Phase 5 removed the recurrent exclusion: a recurring series is no longer a
    # cloned task per sprint but one perpetual task with a binding per sprint, so
    # reconcile is exactly what it needs — it closes the sprint that just ended
    # and mints the new one, which is what close_previous_recurrent_tasks and
    # wt new-recurrent used to do by hand.
    #
    # Requirement (a) — a blanket run must not mint issues by default — lives in
    # wt_api.plan_reconcile, which defaults create_issues to `not all_tasks`.
    try:
        plan = wt_api.plan_reconcile(data, task_id=task_query, all_tasks=all_tasks,
                                     create_issues=create_issues)
    except WtError as e:
        if e.code in _NO_TASK:
            return _no_task(task_query)
        return e.message

    header = []
    if plan["from_cache"]:
        header.append("(offline — using the persisted sprints cache)")

    if not plan["plans"]:
        return "\n".join(header + [
            f"Nothing to do ({len(plan['targets'])} task(s) already in sync)."])

    lines = list(header)
    lines.append(f"{'Plan' if dry_run else 'Reconciling'} for "
                 f"{len(plan['plans'])} task(s):")
    for entry in plan["plans"]:
        t, res = entry["task"], entry["result"]
        lines.append(f"\n  {t['title']}")
        if res.get("error"):
            lines.append(f"    ! {res['error']}")
            continue
        breakdown = ", ".join(f"{e['sprint']}={wt_fmt_mins(e['minutes'])}"
                              for e in res.get("target", []))
        if breakdown:
            lines.append(f"    logs by sprint: {breakdown}")
        for line in _reconcile_plan_lines(res):
            lines.append(f"    {line}")

    totals = plan["totals"]
    lines.append(f"\nTotals: {totals['create']} issue(s) to create, "
                 f"{totals['repoint']} to re-point, "
                 f"{totals['hours']} hours update(s), "
                 f"{totals['close']} issue(s) to close.")
    if totals["needs_issue"]:
        lines.append(f"{totals['needs_issue']} past sprint(s) with unbilled time "
                     f"were NOT bound (create_issues is False).")
    if all_tasks and not plan["create_issues"]:
        lines.append("all_tasks does not create GitHub issues; "
                     "pass create_issues=True to allow it.")

    if dry_run:
        lines.append("\nDry run — nothing was changed.")
        return "\n".join(lines)

    lines.append("\nResults:")
    failures = 0
    for entry in wt_api.apply_reconcile(data, plan, save_callback=save):
        lines.append(f"\n  {entry['task']['title']}")
        outcome = entry["outcome_lines"]
        for line in outcome:
            lines.append(f"    {line}")
        if not outcome:
            lines.append("    (nothing to do)")
        if not entry["success"]:
            failures += 1
    save(data)
    lines.append(f"\n{'Done.' if not failures else f'{failures} task(s) had errors.'}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
