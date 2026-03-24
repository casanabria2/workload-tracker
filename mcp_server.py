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
                "args": ["/Users/carlos/dev/carlos/workload-tracker/mcp_server.py"]
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
def list_tasks(role: str | None = None, status: str | None = None) -> str:
    """List all tasks, optionally filtered by role or status.

    Args:
        role: Filter by role ID (use list_roles to see available roles)
        status: Filter by status (todo, inprogress, done)
    """
    data = load()
    tasks = data.get("tasks", [])
    at = data.get("active_timer")
    roles = get_roles(data)

    if role:
        tasks = [t for t in tasks if t.get("role_id") == role]
    if status:
        tasks = [t for t in tasks if t.get("status") == status]

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
            elapsed = (time.time() - at["started_at"]) / 60
            if elapsed > 0.05:
                prev.setdefault("logs", []).append({
                    "id": uid(),
                    "minutes": round(elapsed, 2),
                    "note": "Timer session",
                    "at": time.time(),
                })
            result_lines.append(f"Stopped timer on '{prev['title']}' ({fmt_mins(elapsed)})")

    # Start new timer
    data["active_timer"] = {"task_id": task["id"], "started_at": time.time()}
    save(data)
    result_lines.append(f"Started timer on '{task['title']}'")

    return "\n".join(result_lines)


@mcp.tool()
def stop_timer() -> str:
    """Stop the currently running timer and log the elapsed time."""
    data = load()
    at = data.get("active_timer")

    if not at:
        return "No timer is currently running."

    task = next((t for t in data["tasks"] if t["id"] == at["task_id"]), None)
    elapsed = (time.time() - at["started_at"]) / 60

    if task and elapsed > 0.05:
        task.setdefault("logs", []).append({
            "id": uid(),
            "minutes": round(elapsed, 2),
            "note": "Timer session",
            "at": time.time(),
        })

    data["active_timer"] = None
    save(data)

    return f"Stopped timer on '{task['title'] if task else '?'}' ({fmt_mins(elapsed)})"


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
def set_task_status(task_query: str, status: str) -> str:
    """Set the status of a task.

    Args:
        task_query: Task ID or partial title
        status: New status: todo, inprogress, or done
    """
    data = load()
    task = resolve_task(data, task_query)
    if not task:
        return f"No task found matching '{task_query}'"

    if status not in STATUS_LABELS:
        return f"Invalid status '{status}'. Use: todo, inprogress, done"

    old_status = task.get("status", "todo")
    task["status"] = status
    save(data)

    return f"Changed '{task['title']}' from {STATUS_LABELS[old_status]} to {STATUS_LABELS[status]}"


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
    """List all available roles."""
    data = load()
    lines = ["Available roles:\n"]
    for r in data.get("roles", []):
        task_count = len([t for t in data["tasks"] if t.get("role_id") == r["id"]])
        lines.append(f"  {r['id']:<15} {r['label']:<30} ({task_count} tasks)")
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


if __name__ == "__main__":
    mcp.run()
