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
from wt import sync_project_status

DATA_FILE = Path.home() / ".workload_tracker.json"
NOTES_DIR = Path.home() / ".workload_tracker_notes"

DEFAULT_ROLES = [
    {"id": "demokit",   "label": "Managing DemoKit",  "color": "blue"},
    {"id": "demos",     "label": "Demos & Workshops", "color": "green"},
    {"id": "strategic", "label": "Strategic Deals",   "color": "yellow"},
    {"id": "other",     "label": "Other",             "color": "white"},
]

STATUS_LABELS = {"todo": "To Do", "inprogress": "In Progress", "done": "Done"}

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
    return data


def save(data: dict):
    DATA_FILE.write_text(json.dumps(data, indent=2))


def get_roles(data: dict) -> dict:
    """Return dict of role_id -> label"""
    return {r["id"]: r["label"] for r in data.get("roles", [])}


def get_role_ids(data: dict) -> list:
    """Return list of role IDs"""
    return [r["id"] for r in data.get("roles", [])]


def get_role_repo(task: dict, data: dict) -> str | None:
    """Get the GitHub repo for a task's role. Returns None if not configured."""
    role_id = task.get("role_id", "other")
    role = next((r for r in data.get("roles", []) if r["id"] == role_id), None)
    return role.get("github_repo") if role else None


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


@mcp.tool()
def add_task(
    title: str,
    role: str = "other",
    status: str = "todo",
    description: str = "",
    github_issue: str = "",
) -> str:
    """Add a new task to the workload tracker.

    Args:
        title: The task title (required)
        role: Role ID (use list_roles to see available roles)
        status: One of: todo, inprogress, done (default: todo)
        description: Optional task description
        github_issue: Optional GitHub issue reference (e.g., owner/repo#123)
    """
    data = load()
    roles = get_roles(data)

    if role not in roles:
        return f"Error: Invalid role '{role}'. Available: {', '.join(roles.keys())}"
    if status not in STATUS_LABELS:
        return f"Error: Invalid status '{status}'. Use: todo, inprogress, done"

    task = {
        "id": uid(),
        "title": title,
        "description": description,
        "role_id": role,
        "status": status,
        "logs": [],
        "created_at": time.time(),
    }
    if github_issue:
        task["github_issue"] = github_issue
    data["tasks"].insert(0, task)
    save(data)
    result = f"Created task '{title}' (id: {task['id']}) [{roles[role]}] [{STATUS_LABELS[status]}]"
    if github_issue:
        result += f" [GitHub: {github_issue}]"
    return result


@mcp.tool()
def list_tasks(role: str | None = None, status: str | None = None, include_done: bool = False) -> str:
    """List all tasks, optionally filtered by role or status.

    By default, done tasks are hidden. Use include_done=True or status="done" to see them.

    Args:
        role: Filter by role ID (use list_roles to see available roles)
        status: Filter by status (todo, inprogress, done)
        include_done: Include done tasks in the list (default: False)
    """
    data = load()
    tasks = data.get("tasks", [])
    at = data.get("active_timer")
    roles = get_roles(data)

    if role:
        tasks = [t for t in tasks if t.get("role_id") == role]
    if status:
        tasks = [t for t in tasks if t.get("status") == status]
    elif not include_done:
        # Hide done tasks by default unless explicitly filtering by status or include_done
        tasks = [t for t in tasks if t.get("status") != "done"]

    if not tasks:
        return "No tasks found."

    lines = []
    for task in tasks:
        running = at and at.get("task_id") == task["id"]
        logged = task_logged_mins(task) + task_live_mins(task, at)
        timer_icon = "▶ " if running else ""
        lines.append(
            f"{timer_icon}{task['title']}\n"
            f"  ID: {task['id']} | Role: {roles.get(task['role_id'], task['role_id'])} | "
            f"Status: {STATUS_LABELS.get(task['status'], task['status'])} | Time: {fmt_mins(logged)}"
        )

    return "\n\n".join(lines)


@mcp.tool()
def get_task(task_query: str) -> str:
    """Get details of a specific task by ID or title.

    Args:
        task_query: Task ID or partial title to search for
    """
    data = load()
    task = resolve_task(data, task_query)
    if not task:
        return f"No task found matching '{task_query}'"

    at = data.get("active_timer")
    roles = get_roles(data)
    logged = task_logged_mins(task) + task_live_mins(task, at)
    running = at and at.get("task_id") == task["id"]

    lines = [
        f"Title: {task['title']}",
        f"ID: {task['id']}",
        f"Role: {roles.get(task['role_id'], task['role_id'])}",
        f"Status: {STATUS_LABELS.get(task['status'], task['status'])}",
        f"Time logged: {fmt_mins(logged)}",
        f"Timer running: {'Yes' if running else 'No'}",
        f"GitHub Issue: {task.get('github_issue') or '(none)'}",
        f"Description: {task.get('description') or '(none)'}",
    ]

    if task.get("logs"):
        lines.append("\nTime logs:")
        for log in reversed(task["logs"][-5:]):  # Last 5 logs
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
    - If the role has a configured GitHub repo, the task must have a linked issue
    - The issue is added to the configured GitHub project with logged hours

    Args:
        task_query: Task ID or partial title
        status: New status: todo, inprogress, or done
        create_issue: If True and setting to done, create GitHub issue if missing
    """
    data = load()
    task = resolve_task(data, task_query)
    if not task:
        return f"No task found matching '{task_query}'"

    if status not in STATUS_LABELS:
        return f"Invalid status '{status}'. Use: todo, inprogress, done"

    old_status = task.get("status", "todo")

    # Special handling for closing tasks (transitioning to "done")
    if status == "done" and old_status != "done":
        return _close_task_mcp(task, data, create_issue)

    task["status"] = status
    save(data)

    # Sync status to GitHub project if task has a linked issue
    result_msg = f"Changed '{task['title']}' from {STATUS_LABELS[old_status]} to {STATUS_LABELS[status]}"
    if task.get("github_issue"):
        if sync_project_status(task["github_issue"], status, data):
            result_msg += f" (project synced)"

    return result_msg


def _close_task_mcp(task: dict, data: dict, create_issue: bool) -> str:
    """Handle task closing workflow for MCP."""
    import subprocess
    import re

    result_lines = []

    # Check if role has a GitHub repo
    repo = get_role_repo(task, data)

    if not repo:
        # No GitHub integration - just close
        task["status"] = "done"
        save(data)
        return f"Closed '{task['title']}' (no GitHub integration for this role)"

    # Check if task has GitHub issue
    if not task.get("github_issue"):
        if create_issue:
            # Create the issue
            try:
                # Read local notes if they exist
                npath = NOTES_DIR / f"{task['id']}.md"
                body = ""
                if npath.exists():
                    body = npath.read_text()

                cmd = ["gh", "issue", "create", "-R", repo, "--title", task["title"]]
                if body:
                    cmd.extend(["--body", body])
                else:
                    cmd.extend(["--body", f"Task created from workload tracker: {task['title']}"])

                gh_result = subprocess.run(cmd, capture_output=True, text=True)
                if gh_result.returncode != 0:
                    return f"Error: Failed to create issue: {gh_result.stderr}"

                # Parse issue URL
                url = gh_result.stdout.strip()
                url_match = re.match(r'https?://github\.com/([^/]+/[^/]+)/issues/(\d+)', url)
                if url_match:
                    issue_ref = f"{url_match.group(1)}#{url_match.group(2)}"
                    task["github_issue"] = issue_ref
                    save(data)
                    result_lines.append(f"Created issue: {issue_ref}")
                else:
                    return f"Error: Could not parse issue URL: {url}"
            except Exception as e:
                return f"Error creating issue: {e}"
        else:
            return (
                f"Error: Task '{task['title']}' has no GitHub issue linked.\n"
                f"This role ({task.get('role_id')}) requires issues in {repo}.\n"
                f"Either:\n"
                f"  - Link an existing issue: link_github_issue('{task['title']}', 'owner/repo#123')\n"
                f"  - Create one: set_task_status('{task['title']}', 'done', create_issue=True)"
            )

    # Update project if configured
    config = data.get("config", {})
    if config.get("github_project_number"):
        try:
            owner = config.get("github_project_owner", "grafana")
            project_num = config.get("github_project_number")
            issue_url = f"https://github.com/{task['github_issue'].replace('#', '/issues/')}"

            # Add to project
            add_result = subprocess.run([
                "gh", "project", "item-add", str(project_num),
                "--owner", owner, "--url", issue_url, "--format", "json"
            ], capture_output=True, text=True)

            if add_result.returncode == 0:
                total_mins = sum(l.get("minutes", 0) for l in task.get("logs", []))
                hours = round(total_mins / 60)
                result_lines.append(f"Added to project (Hours: {hours})")
            else:
                result_lines.append(f"Warning: Could not add to project: {add_result.stderr}")
        except Exception as e:
            result_lines.append(f"Warning: Project update failed: {e}")

    # Close the GitHub issue
    if task.get("github_issue"):
        close_result = subprocess.run(
            ["gh", "issue", "close", *gh_issue_args(task["github_issue"])],
            capture_output=True, text=True
        )
        if close_result.returncode == 0:
            result_lines.append(f"Closed issue: {task['github_issue']}")
        else:
            result_lines.append(f"Warning: Could not close issue: {close_result.stderr}")

    # Mark as done
    task["status"] = "done"
    save(data)

    result_lines.insert(0, f"Closed '{task['title']}'")

    return "\n".join(result_lines)


@mcp.tool()
def delete_task(task_query: str) -> str:
    """Delete a task.

    Args:
        task_query: Task ID or partial title
    """
    data = load()
    task = resolve_task(data, task_query)
    if not task:
        return f"No task found matching '{task_query}'"

    data["tasks"] = [t for t in data["tasks"] if t["id"] != task["id"]]
    if (data.get("active_timer") or {}).get("task_id") == task["id"]:
        data["active_timer"] = None
    save(data)

    return f"Deleted task '{task['title']}'"


@mcp.tool()
def rename_task(task_query: str, new_title: str) -> str:
    """Rename a task. Also updates the linked GitHub issue title if present.

    Args:
        task_query: Task ID or partial title
        new_title: The new title for the task
    """
    import subprocess

    data = load()
    task = resolve_task(data, task_query)
    if not task:
        return f"No task found matching '{task_query}'"

    old_title = task["title"]
    task["title"] = new_title
    save(data)

    result_lines = [f"Renamed '{old_title}' → '{new_title}'"]

    # Update linked GitHub issue title if present
    if task.get("github_issue"):
        gh_result = subprocess.run(
            ["gh", "issue", "edit", *gh_issue_args(task["github_issue"]), "--title", new_title],
            capture_output=True, text=True
        )
        if gh_result.returncode == 0:
            result_lines.append(f"Updated GitHub issue: {task['github_issue']}")
        else:
            result_lines.append(f"Warning: Failed to update GitHub issue title: {gh_result.stderr}")

    return "\n".join(result_lines)


@mcp.tool()
def get_status() -> str:
    """Get an overview of time logged by role and any running timer."""
    data = load()
    tasks = data.get("tasks", [])
    at = data.get("active_timer")
    roles = get_roles(data)

    by_role: dict[str, float] = {role_id: 0.0 for role_id in roles}
    for task in tasks:
        rid = task.get("role_id", "other")
        by_role[rid] = by_role.get(rid, 0) + task_logged_mins(task) + task_live_mins(task, at)

    total = sum(by_role.values())
    lines = [f"Workload Tracker — {len(tasks)} tasks — {fmt_mins(total)} total\n"]

    for role_id, label in roles.items():
        mins = by_role.get(role_id, 0)
        pct = round(mins / total * 100) if total else 0
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        lines.append(f"{label:<25} {bar} {pct:>3}%  {fmt_mins(mins)}")

    if at:
        task = next((t for t in tasks if t["id"] == at["task_id"]), None)
        elapsed = (time.time() - at["started_at"]) / 60
        lines.append(f"\n▶ Timer running: {task['title'] if task else '?'} ({fmt_mins(elapsed)})")

    return "\n".join(lines)


@mcp.tool()
def get_notes_path(task_query: str) -> str:
    """Get the notes location for a task. Returns GitHub issue info if linked, else local file path.

    Args:
        task_query: Task ID or partial title
    """
    data = load()
    task = resolve_task(data, task_query)
    if not task:
        return f"No task found matching '{task_query}'"

    # Check for GitHub issue first
    if task.get("github_issue"):
        gh_ref = task["github_issue"]
        return (
            f"Task '{task['title']}' is linked to GitHub issue: {gh_ref}\n"
            f"View: gh issue view {gh_ref}\n"
            f"Comment: gh issue comment {gh_ref}"
        )

    # Fall back to local notes
    NOTES_DIR.mkdir(exist_ok=True)
    notes_path = NOTES_DIR / f"{task['id']}.md"

    if not notes_path.exists():
        notes_path.write_text(f"# {task['title']}\n\n")

    return f"Notes file for '{task['title']}':\n{notes_path}"


@mcp.tool()
def link_github_issue(task_query: str, github_issue: str) -> str:
    """Link a task to a GitHub issue.

    Args:
        task_query: Task ID or partial title
        github_issue: GitHub issue reference (owner/repo#123, URL, or bare number with default repo)
    """
    import subprocess

    data = load()
    task = resolve_task(data, task_query)
    if not task:
        return f"No task found matching '{task_query}'"

    # Normalize issue reference
    github_issue, err = normalize_issue_ref(github_issue, data)
    if err:
        return f"Error: {err}"

    # Validate the issue exists
    result = subprocess.run(
        ["gh", "issue", "view", *gh_issue_args(github_issue), "--json", "number,title"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return f"Error: Could not find GitHub issue: {github_issue}"

    import json as json_mod
    issue_info = json_mod.loads(result.stdout)

    task["github_issue"] = github_issue
    save(data)

    return f"Linked '{task['title']}' to GitHub issue #{issue_info['number']}: {issue_info['title']}"


@mcp.tool()
def unlink_github_issue(task_query: str) -> str:
    """Unlink a task from its GitHub issue.

    Args:
        task_query: Task ID or partial title
    """
    data = load()
    task = resolve_task(data, task_query)
    if not task:
        return f"No task found matching '{task_query}'"

    if not task.get("github_issue"):
        return f"Task '{task['title']}' is not linked to a GitHub issue."

    old_issue = task["github_issue"]
    del task["github_issue"]
    save(data)
    return f"Unlinked '{task['title']}' from {old_issue}"


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

    if not task.get("github_issue"):
        return f"Task '{task['title']}' is not linked to a GitHub issue."

    result = subprocess.run(
        ["gh", "issue", "view", *gh_issue_args(task["github_issue"]), "--json", "title,body,comments"],
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

    if not task.get("github_issue"):
        return f"Task '{task['title']}' is not linked to a GitHub issue."

    result = subprocess.run(
        ["gh", "issue", "comment", *gh_issue_args(task["github_issue"]), "-b", comment],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return f"Error adding comment: {result.stderr}"

    return f"Added comment to {task['github_issue']}"


@mcp.tool()
def list_roles() -> str:
    """List all available roles with their GitHub repo configuration."""
    data = load()
    lines = ["Available roles:\n"]
    for r in data.get("roles", []):
        task_count = len([t for t in data["tasks"] if t.get("role_id") == r["id"]])
        repo = r.get("github_repo", "")
        repo_str = f"→ {repo}" if repo else "(no repo)"
        lines.append(f"  {r['id']:<15} {r['label']:<25} {repo_str:<35} ({task_count} tasks)")
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
def set_role_repo(role_id: str, github_repo: str | None = None) -> str:
    """Set or clear the GitHub repo for a role.

    When a role has a configured repo, tasks with that role require a GitHub issue
    when being closed. The issue is automatically created in the configured repo.

    Args:
        role_id: The role ID to update
        github_repo: GitHub repo in owner/repo format, or None to clear
    """
    data = load()
    role = next((r for r in data["roles"] if r["id"] == role_id), None)

    if not role:
        return f"Error: Role '{role_id}' not found."

    if github_repo:
        # Validate format
        if "/" not in github_repo or github_repo.count("/") != 1:
            return "Error: Repo must be in owner/repo format (e.g., 'grafana/field-eng')"
        role["github_repo"] = github_repo
        save(data)
        return f"Set GitHub repo for '{role_id}': {github_repo}"
    else:
        if "github_repo" in role:
            del role["github_repo"]
            save(data)
            return f"Cleared GitHub repo for role: {role_id}"
        else:
            return f"Role '{role_id}' has no GitHub repo set."


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

    # Check if task already exists for this issue
    for t in data["tasks"]:
        if t.get("github_issue") == issue_ref:
            return f"Task already exists for {issue_ref}: '{t['title']}' (id: {t['id']})"

    task = {
        "id": uid(),
        "title": issue_info["title"],
        "description": "",
        "role_id": role,
        "status": status,
        "logs": [],
        "created_at": time.time(),
        "github_issue": issue_ref,
    }
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


if __name__ == "__main__":
    mcp.run()
