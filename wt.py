#!/usr/bin/env python3
"""
wt — Workload Tracker CLI
Quick command-line interface to manage tasks without launching the full TUI.

Usage:
    wt add "Task title" --role strategic --status inprogress
    wt list [--role strategic] [--all]
    wt start <task-id or partial title>
    wt stop
    wt log <task-id or partial title> <minutes> [note]
    wt notes <task-id or partial title>
    wt status
    wt done <task-id or partial title>
    wt delete <task-id or partial title>
    wt rename <task> <new title>       — Rename a task

    wt logs <task>                              — List all time logs for a task
    wt edit-log <task> <log-id> [--minutes M] [--note N]  — Edit log entry
    wt delete-log <task> <log-id>               — Delete log entry
    wt split-log <task> <log-id> <minutes>      — Split log at minute mark
    wt merge-logs <task> <log-id-1> <log-id-2>  — Merge two log entries

    wt link <task> <github-issue>  — Link task to GitHub issue
    wt unlink <task>               — Unlink task from GitHub issue

    wt add-issue [url-or-ref] [--role ROLE]  — Create task from GitHub issue
    wt add-issue [--role ROLE]               — Interactive: show assigned issues

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
    wt roles set-repo <id> [repo]     — Set/clear GitHub repo for a role
    wt roles set-activity <id> [act]  — Set/clear GitHub Project activity for a role
    wt roles set-type <id> [type]     — Set/clear GitHub Project type for a role

    wt calendar                  — List events from yesterday & today
    wt calendar <days>           — List events from last N days
    wt calendar import <event> [--task <task>]  — Import event (or log to existing task)
    wt calendar setup            — Show Google Calendar setup instructions

    wt arc setup                 — Set up Arc browser integration
    wt arc status                — Show Arc integration status
    wt arc sync                  — Sync folders with current roles/tasks

    wt iterm setup               — Enable iTerm2/tmux integration
    wt iterm open <task>         — Open iTerm2 terminal for a task
    wt iterm close <task>        — Close tmux session for a task
    wt iterm set-folder <task> <path> — Set local folder for task
    wt iterm clear-folder <task> — Clear local folder setting
    wt iterm status              — Show iTerm integration status

    wt tabs                      — List tabs in current task's folder
    wt tabs cleanup              — Manually trigger tab cleanup

Notes are stored in ~/.workload_tracker_notes/<task_id>.md
Tasks linked to GitHub issues use the issue for notes instead.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta

DATA_FILE = Path.home() / ".workload_tracker.json"
NOTES_DIR = Path.home() / ".workload_tracker_notes"

DEFAULT_ROLES = [
    {"id": "demokit",   "label": "Managing DemoKit",  "color": "blue"},
    {"id": "demos",     "label": "Demos & Workshops", "color": "green"},
    {"id": "strategic", "label": "Strategic Deals",   "color": "yellow"},
    {"id": "other",     "label": "Other",             "color": "white"},
]

STATUS_LABELS = {"todo": "To Do", "inprogress": "In Progress", "done": "Done"}
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


def load() -> dict:
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text())
        except Exception:
            data = {}
    else:
        data = {}
    # Ensure required keys exist
    data.setdefault("tasks", [])
    data.setdefault("active_timer", None)
    # Initialize roles if missing
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


def fmt_mins(mins: float) -> str:
    if not mins:
        return "0m"
    h = int(mins // 60)
    m = int(mins % 60)
    return f"{h}h {m}m" if h else f"{m}m"


def task_logged_mins(task: dict) -> float:
    return sum(l.get("minutes", 0) for l in task.get("logs", []))


def task_logged_mins_for_sprint(task: dict, sprints: list) -> float:
    """Sum log minutes that fall within the task's assigned sprint.

    If the task has no sprint_id or sprint not found, returns total logged minutes.
    Used when reporting hours to GitHub: a task split across sprints keeps all
    logs locally but should only report its assigned sprint's hours to GH.
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
      - "262" -> "owner/repo#262" (uses task's role repo, then config github_repo)
      - "#262" -> "owner/repo#262" (uses task's role repo, then config github_repo)
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
        # Try task's role repo first, then global config
        repo = None
        if task:
            repo = get_role_repo(task, data)
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

def get_role_repo(task: dict, data: dict) -> str | None:
    """Get the GitHub repo for a task's role. Returns None if not configured."""
    role_id = task.get("role_id", "other")
    role = next((r for r in data.get("roles", []) if r["id"] == role_id), None)
    return role.get("github_repo") if role else None


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
    "done": "Done",
}


def get_project_info(data: dict) -> dict:
    """Get project ID and field information.

    Returns dict with project_id, status_field, hours_field, status_options, etc.
    Raises Exception if project not configured or fields missing.
    """
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

    return {
        "owner": owner,
        "project_num": project_num,
        "project_id": project_id,
        "status_field": status_field,
        "hours_field": hours_field,
        "activity_field": activity_field,
        "sprint_field": sprint_field,
        "status_options": status_options,
        "activity_options": activity_options,
    }


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


def sprint_summary_for_task(task: dict, sprints: list[dict]) -> list[dict]:
    """Get per-sprint breakdown of logged time for a task.

    Returns list of dicts sorted by sprint start date:
        [{sprint_id, sprint_title, field_id, start_date, logs, total_mins}, ...]
    Only includes sprints that have logged time (excludes None bucket).
    """
    buckets = bucket_logs_by_sprint(task, sprints)
    sprint_map = {s["id"]: s for s in sprints}
    result = []
    for sprint_id, logs in buckets.items():
        if sprint_id is None:
            continue
        s = sprint_map.get(sprint_id, {})
        result.append({
            "sprint_id": sprint_id,
            "sprint_title": s.get("title", "Unknown"),
            "field_id": s.get("field_id"),
            "start_date": s.get("startDate", ""),
            "logs": logs,
            "total_mins": sum(l.get("minutes", 0) for l in logs),
        })
    result.sort(key=lambda x: x["start_date"])
    return result


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


def split_cross_sprint_task(task: dict, data: dict, save_callback,
                            all_sprints: list[dict] = None,
                            progress_callback=None) -> dict:
    """Split a task that has logs spanning multiple sprints.

    Creates shadow tasks for previous sprints with their own GH issues.
    The original task is updated to the most recent sprint.

    Args:
        progress_callback: Optional function(msg) called with progress updates.

    Returns dict with:
        success, sprint_tasks_created, main_sprint, error
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

    summary = sprint_summary_for_task(task, all_sprints)
    if len(summary) <= 1:
        result["error"] = "Task only has time in one sprint"
        return result

    # Most recent sprint (last in sorted list) stays on original task
    main_sprint_info = summary[-1]
    previous_sprints = summary[:-1]

    repo = get_role_repo(task, data)
    config = data.get("config", {})
    has_project = bool(config.get("github_project_number"))

    # Pre-fetch project info once (avoids repeated API calls)
    pi = None
    if has_project:
        progress("Fetching project info...")
        try:
            pi = get_project_info(data)
        except Exception:
            pass

    for sprint_info in previous_sprints:
        sprint_label = sprint_info["sprint_title"]
        shadow_title = f"{task['title']} ({sprint_label})"
        shadow_task = {
            "id": uid(),
            "title": shadow_title,
            "description": f"Sprint split from: {task['title']}",
            "role_id": task.get("role_id", "other"),
            "status": "done",
            "logs": [{
                "id": uid(),
                "minutes": sprint_info["total_mins"],
                "note": f"Sprint split: {fmt_mins(sprint_info['total_mins'])} from {task['title']}",
                "at": time.time(),
            }],
            "created_at": time.time(),
            "sprint": sprint_label,
            "sprint_id": sprint_info["sprint_id"],
            "cross_sprint_parent": task["id"],
        }

        created_info = {
            "sprint": sprint_label,
            "total_mins": sprint_info["total_mins"],
            "issue_ref": None,
        }

        # Create GH issue if role has a repo
        if repo:
            try:
                progress(f"  {sprint_label}: Creating issue...")
                issue_ref = create_github_issue(
                    {**shadow_task, "title": shadow_title}, repo
                )
                shadow_task["github_issue"] = issue_ref
                created_info["issue_ref"] = issue_ref

                if pi:
                    progress(f"  {sprint_label}: Adding to project...")
                    item_id = add_issue_to_project(issue_ref, data)

                    progress(f"  {sprint_label}: Setting fields...")
                    sync_project_status(issue_ref, "done", data, project_info=pi, item_id=item_id)

                    hours = mins_to_quarter_hours(sprint_info["total_mins"])
                    update_project_hours(issue_ref, hours, data, project_info=pi, item_id=item_id)

                    if sprint_info.get("field_id"):
                        update_project_sprint(
                            issue_ref, sprint_info["sprint_id"],
                            sprint_info["field_id"], data,
                            project_info=pi, item_id=item_id
                        )

                    activity = get_role_activity(task, data)
                    if activity:
                        update_project_activity(issue_ref, activity, data, project_info=pi, item_id=item_id)

                # Add comment linking to main task
                main_issue = task.get("github_issue", "")
                if main_issue:
                    progress(f"  {sprint_label}: Adding comment...")
                    comment = f"Sprint split from {main_issue}. See that issue for full details and notes."
                    add_issue_comment(issue_ref, comment)

                progress(f"  {sprint_label}: Closing issue...")
                close_github_issue(issue_ref)
                mark_logs_uploaded(shadow_task)

            except Exception as e:
                created_info["error"] = str(e)

        data["tasks"].append(shadow_task)
        result["sprint_tasks_created"].append(created_info)

    # Update main task sprint to most recent
    task["sprint"] = main_sprint_info["sprint_title"]
    task["sprint_id"] = main_sprint_info["sprint_id"]
    result["main_sprint"] = main_sprint_info["sprint_title"]

    # Update main task's GH issue hours to only the most recent sprint's hours
    if task.get("github_issue") and pi:
        progress(f"  Main task: Updating hours and sprint...")
        main_item_id = add_issue_to_project(task["github_issue"], data)
        main_hours = mins_to_quarter_hours(main_sprint_info["total_mins"])
        update_project_hours(task["github_issue"], main_hours, data, project_info=pi, item_id=main_item_id)
        if main_sprint_info.get("field_id"):
            update_project_sprint(
                task["github_issue"], main_sprint_info["sprint_id"],
                main_sprint_info["field_id"], data,
                project_info=pi, item_id=main_item_id
            )

    # Mark all original task logs as uploaded
    mark_logs_uploaded(task)
    save_callback(data)

    result["success"] = True
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


def get_role_activity(task: dict, data: dict) -> str | None:
    """Get the GitHub Project activity for a task's role. Returns None if not configured."""
    role_id = task.get("role_id", "other")
    role = next((r for r in data.get("roles", []) if r["id"] == role_id), None)
    return role.get("activity") if role else None


def get_role_type(task: dict, data: dict) -> str | None:
    """Get the GitHub Project type for a task's role. Returns None if not configured."""
    role_id = task.get("role_id", "other")
    role = next((r for r in data.get("roles", []) if r["id"] == role_id), None)
    return role.get("type") if role else None


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
    """Add issue to project and set up all fields (Status, Activity, Sprint, Hours).

    Args:
        issue_ref: GitHub issue reference (owner/repo#number)
        task: Task dict with role_id, status, logs
        data: Full data dict with config and roles

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

        # Set activity based on role
        activity = get_role_activity(task, data)
        if activity:
            if not update_project_activity(issue_ref, activity, data, project_info=pi, item_id=item_id):
                result["errors"].append(f"Failed to set activity: {activity}")

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

        # Set hours (filtered by task's sprint if assigned, rounded to 0.25 hours)
        sprint_mins = task_logged_mins_for_sprint(task, all_sprints)
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
    """Sync task to GitHub project - updates Hours, Status, Activity, and Sprint.

    Calculates total logged time, rounds to nearest 0.25 hours, and updates project.
    Also syncs Status, Activity, and Sprint fields.
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

    # Sync activity based on role
    activity = get_role_activity(task, data)
    if activity:
        if not update_project_activity(issue_ref, activity, data):
            success = False

    # Sync sprint: use task's stored sprint
    all_sprints = get_all_sprints(data)
    sprint_id = task.get("sprint_id")
    if sprint_id:
        field_id = all_sprints[0]["field_id"] if all_sprints else None
        if field_id:
            update_project_sprint(issue_ref, sprint_id, field_id, data)

    # Sync hours (filtered by task's sprint if assigned)
    sprint_mins = task_logged_mins_for_sprint(task, all_sprints)
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
        Dict with results: {success, issue_created, issue_closed, project_updated, skipped_github, comment_added, error}
    """
    result = {
        "success": False,
        "issue_created": False,
        "issue_closed": False,
        "project_updated": False,
        "skipped_github": False,
        "comment_added": False,
        "error": None
    }

    # 1. Check if role has a GitHub repo
    repo = get_role_repo(task, data)

    if not repo:
        # No GitHub integration for this role - just close
        task["status"] = "done"
        save_callback(data)
        result["success"] = True
        result["skipped_github"] = True
        return result

    # 2. Ensure GitHub issue exists
    if not task.get("github_issue"):
        if prompt_callback:
            create = prompt_callback(
                f"Task '{task['title']}' has no GitHub issue. Create one in {repo}?"
            )
            if not create:
                result["error"] = "Task must have GitHub issue to close (role requires it)"
                return result

        try:
            issue_ref = create_github_issue(task, repo)
            task["github_issue"] = issue_ref
            result["issue_created"] = True
            save_callback(data)
        except Exception as e:
            result["error"] = f"Failed to create issue: {e}"
            return result

    # 3. Add to project and update fields
    config = data.get("config", {})
    if config.get("github_project_number"):
        try:
            total_mins = task_logged_mins(task)
            hours = mins_to_quarter_hours(total_mins)
            add_to_project_and_update(task["github_issue"], hours, data)
            result["project_updated"] = True

            # Set activity if role has one configured
            activity = get_role_activity(task, data)
            if activity:
                update_project_activity(task["github_issue"], activity, data)

            # Set sprint from task's stored sprint
            sprint_id = task.get("sprint_id")
            if sprint_id:
                all_sprints = get_all_sprints(data)
                field_id = all_sprints[0]["field_id"] if all_sprints else None
                if field_id:
                    update_project_sprint(task["github_issue"], sprint_id, field_id, data)

            # Set type if role has one configured
            type_val = get_role_type(task, data)
            if type_val:
                update_project_type(task["github_issue"], type_val, data)
        except Exception as e:
            # Project update is non-fatal - still mark task as done
            result["error"] = f"Project update failed: {e}"

    # 4. Check for comments and prompt for closing comment if none
    if comment_callback and not issue_has_comments(task["github_issue"]):
        comment = comment_callback(
            f"Issue {task['github_issue']} has no comments. Add a closing comment?"
        )
        if comment:
            if add_issue_comment(task["github_issue"], comment):
                result["comment_added"] = True

    # 5. Close the GitHub issue
    if close_github_issue(task["github_issue"]):
        result["issue_closed"] = True

    # 6. Mark as done
    task["status"] = "done"
    mark_logs_uploaded(task)
    save_callback(data)
    result["success"] = True
    return result


# ── Commands ──────────────────────────────────────────────

def cmd_add(args):
    if not args:
        print("Usage: wt add <title> [--role ROLE] [--status STATUS] [--desc DESC] [--sprint SPRINT]")
        sys.exit(1)

    data = load()
    roles = get_roles(data)

    # Parse title and flags
    title_parts = []
    role_id = "other"
    status = "todo"
    desc = ""
    sprint_override = None
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
        else:
            title_parts.append(args[i]); i += 1
    title = " ".join(title_parts)
    if not title:
        print(c("Title is required.", "red")); sys.exit(1)

    task = {
        "id": uid(), "title": title, "description": desc,
        "role_id": role_id, "status": status,
        "logs": [], "created_at": time.time()
    }

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


def cmd_list(args):
    data = load()
    tasks = data.get("tasks", [])
    at = data.get("active_timer")
    roles = get_roles(data)
    role_ids = get_role_ids(data)

    filter_role = None
    show_done = False
    show_shadows = False
    i = 0
    while i < len(args):
        if args[i] == "--role" and i + 1 < len(args):
            filter_role = resolve_role(data, args[i+1]); i += 2
        elif args[i] in ("--all", "-a"):
            show_done = True; i += 1
        elif args[i] == "--shadows":
            show_shadows = True; i += 1
        else:
            i += 1

    if filter_role:
        tasks = [t for t in tasks if t.get("role_id") == filter_role]

    # Hide done tasks by default
    if not show_done:
        tasks = [t for t in tasks if t.get("status") != "done"]

    # Hide shadow tasks (cross-sprint splits) by default
    if not show_shadows:
        tasks = [t for t in tasks if not t.get("cross_sprint_parent")]

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
            if t.get("github_issue"):
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
            print(c(f"  (No GitHub integration for this role)", "dim"))
        else:
            if result["issue_created"]:
                print(c(f"  Created issue: {task['github_issue']}", "dim"))
            if result.get("comment_added"):
                print(c(f"  Added closing comment", "dim"))
            if result["issue_closed"]:
                print(c(f"  Closed issue: {task['github_issue']}", "dim"))
            if result["project_updated"]:
                hours = round(sum(l.get("minutes", 0) for l in task.get("logs", [])) / 60)
                print(c(f"  Updated project (Status: Done, Hours: {hours})", "dim"))
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


def cmd_delete(args):
    if not args:
        print("Usage: wt delete <task-id or title>"); sys.exit(1)
    data = load()
    task = resolve_task(data, " ".join(args))
    data["tasks"] = [t for t in data["tasks"] if t["id"] != task["id"]]
    if (data.get("active_timer") or {}).get("task_id") == task["id"]:
        data["active_timer"] = None
    save(data)
    print(c(f"✓ Deleted: {task['title']}", "yellow"))


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

    # Update linked GitHub issue title if present
    if task.get("github_issue"):
        if update_issue_title(task["github_issue"], new_title):
            print(c(f"  Updated GitHub issue: {task['github_issue']}", "dim"))
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
    if task.get("github_issue"):
        gh_ref = task["github_issue"]
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
    """Manage roles: list, add, update, delete, set-repo"""
    data = load()

    if not args:
        # List roles
        print(c("\n  Roles:\n", "bold"))
        for r in data.get("roles", []):
            task_count = len([t for t in data["tasks"] if t.get("role_id") == r["id"]])
            repo = r.get("github_repo", "")
            activity = r.get("activity", "")
            repo_str = f"→ {repo}" if repo else "(no repo)"
            activity_str = f"[{activity}]" if activity else ""
            print(f"  {r['id']:<15} {r['label']:<25} {repo_str:<40} {activity_str}")
            if task_count:
                print(f"  {'':<15} {'':<25} ({task_count} tasks)")
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

    elif subcmd == "set-repo":
        if len(args) < 2:
            print("Usage: wt roles set-repo <id> [repo]")
            print("  Set a GitHub repo for a role (owner/repo format)")
            print("  Omit repo to clear the setting")
            sys.exit(1)
        role_id = args[1].lower()

        role = next((r for r in data["roles"] if r["id"] == role_id), None)
        if not role:
            print(c(f"Role '{role_id}' not found.", "red")); sys.exit(1)

        if len(args) < 3:
            # Clear the repo
            if "github_repo" in role:
                del role["github_repo"]
                save(data)
                print(c(f"✓ Cleared GitHub repo for role: {role_id}", "yellow"))
            else:
                print(c(f"Role '{role_id}' has no GitHub repo set.", "dim"))
        else:
            repo = args[2]
            # Validate repo format (owner/repo)
            if "/" not in repo or repo.count("/") != 1:
                print(c("Error: Repo must be in owner/repo format", "red"))
                sys.exit(1)
            role["github_repo"] = repo
            save(data)
            print(c(f"✓ Set GitHub repo for {role_id}: {repo}", "green"))

    elif subcmd == "set-activity":
        if len(args) < 2:
            print("Usage: wt roles set-activity <id> [activity]")
            print("  Set a GitHub Project activity for a role")
            print("  Omit activity to clear the setting")
            sys.exit(1)
        role_id = args[1].lower()

        role = next((r for r in data["roles"] if r["id"] == role_id), None)
        if not role:
            print(c(f"Role '{role_id}' not found.", "red")); sys.exit(1)

        if len(args) < 3:
            # Clear the activity
            if "activity" in role:
                del role["activity"]
                save(data)
                print(c(f"✓ Cleared activity for role: {role_id}", "yellow"))
            else:
                print(c(f"Role '{role_id}' has no activity set.", "dim"))
        else:
            activity = " ".join(args[2:])  # Allow multi-word activities
            role["activity"] = activity
            save(data)
            print(c(f"✓ Set activity for {role_id}: {activity}", "green"))

    elif subcmd == "set-type":
        if len(args) < 2:
            print("Usage: wt roles set-type <id> [type]")
            print("  Set a GitHub Project type for a role")
            print("  Omit type to clear the setting")
            sys.exit(1)
        role_id = args[1].lower()

        role = next((r for r in data["roles"] if r["id"] == role_id), None)
        if not role:
            print(c(f"Role '{role_id}' not found.", "red")); sys.exit(1)

        if len(args) < 3:
            # Clear the type
            if "type" in role:
                del role["type"]
                save(data)
                print(c(f"✓ Cleared type for role: {role_id}", "yellow"))
            else:
                print(c(f"Role '{role_id}' has no type set.", "dim"))
        else:
            type_val = " ".join(args[2:])  # Allow multi-word types
            role["type"] = type_val
            save(data)
            print(c(f"✓ Set type for {role_id}: {type_val}", "green"))

    else:
        print(c(f"Unknown roles subcommand: {subcmd}", "red"))
        print("Usage: wt roles [add|update|delete|set-repo|set-activity|set-type] ...")
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
    # Resolve task first so we can use its role repo for bare issue numbers
    query = " ".join(args[:-1])
    task = resolve_task(data, query)
    # Issue ref is the last argument - use task's role repo if available
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

    # Store the issue reference (normalized form)
    task["github_issue"] = issue_ref
    save(data)
    print(c(f"Linked '{task['title']}' to GitHub issue #{issue_info['number']}: {issue_info['title']}", "green"))


def cmd_unlink(args):
    """Unlink a task from its GitHub issue."""
    if not args:
        print("Usage: wt unlink <task-id or title>"); sys.exit(1)

    data = load()
    task = resolve_task(data, " ".join(args))

    if not task.get("github_issue"):
        print(c(f"Task '{task['title']}' is not linked to a GitHub issue.", "yellow"))
        sys.exit(0)

    old_issue = task["github_issue"]
    del task["github_issue"]
    save(data)
    print(c(f"Unlinked '{task['title']}' from {old_issue}", "green"))


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


def cmd_add_issue(args):
    """Create a task from a GitHub issue."""
    data = load()
    roles = get_roles(data)
    role_ids = get_role_ids(data)

    # Parse --role flag
    role_id = None
    remaining_args = []
    i = 0
    while i < len(args):
        if args[i] == "--role" and i + 1 < len(args):
            role_id = resolve_role(data, args[i + 1])
            i += 2
        else:
            remaining_args.append(args[i])
            i += 1

    if remaining_args:
        # Direct mode: create from URL/ref (normalize handles bare numbers)
        issue_ref = normalize_issue_ref(remaining_args[0], data)
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

    # Fetch issue details
    result = subprocess.run(
        ["gh", "issue", "view", *gh_issue_args(issue_ref), "--json", "number,title,state,url"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(c(f"Could not find GitHub issue: {issue_ref}", "red"))
        print(c("  Make sure the issue exists and you have access.", "dim"))
        sys.exit(1)

    issue_info = json.loads(result.stdout)

    # Map GitHub state to task status
    # OPEN -> inprogress (user is working on it), CLOSED -> done
    gh_state = issue_info.get("state", "OPEN").upper()
    status = "done" if gh_state == "CLOSED" else "inprogress"

    # Check if task already exists for this issue
    for t in data["tasks"]:
        if t.get("github_issue") == issue_ref:
            print(c(f"Task already exists for {issue_ref}:", "yellow"))
            print(f"  {t['title']} (id: {t['id']})")
            sys.exit(0)

    # Default to 'other' if no role specified
    if role_id is None:
        role_id = "other"

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
    data["tasks"].insert(0, task)
    save(data)

    print(c(f"✓ Created: {task['title']}", "green"))
    print(f"  [{roles.get(role_id, role_id)}] [{STATUS_LABELS.get(status, status)}]")
    print(c(f"  id: {task['id']}", "dim"))
    print(c(f"  GitHub: {issue_ref}", "cyan"))


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


def get_calendar_events(days_back: int = 1, calendar_id: str = "primary") -> list[dict]:
    """Get events from Google Calendar for the specified date range.

    Args:
        days_back: Number of days to look back (default 1 = yesterday + today)
        calendar_id: Google Calendar ID (default "primary", or email like "user@domain.com")

    Returns list of event dicts: {title, start_date, end_date, calendar_name, notes, duration_mins, uid}
    """
    service = get_gcal_service()
    if not service:
        return []

    # Calculate time range
    now = datetime.now()
    start_date = (now - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
    end_date = now.replace(hour=23, minute=59, second=59, microsecond=0)

    # Convert to RFC3339 format
    start_str = start_date.isoformat() + 'Z'
    end_str = end_date.isoformat() + 'Z'

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
        print(c(f"\n  Event: {event['title']}", "bold"))
        print(f"  Date:     {start_dt.strftime('%Y-%m-%d')}")
        print(f"  Time:     {start_dt.strftime('%H:%M')} - {end_dt.strftime('%H:%M')}")
        print(f"  Duration: {fmt_mins(event['duration_mins'])}")
        print(f"  Calendar: {event['calendar_name']}")
        print()

        # Prompt for time logging
        duration = event["duration_mins"]
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


def cmd_tabs(args):
    """List or manage tabs for the current task."""
    try:
        from arc_browser import TaskTabManager, ArcAppleScript
    except ImportError as e:
        print(c(f"Error: {e}", "red"))
        sys.exit(1)

    data = load()
    at = data.get("active_timer")

    subcmd = args[0].lower() if args else "list"

    if subcmd == "list":
        if not at:
            print(c("No active timer. Start a task first.", "dim"))
            return

        task = next((t for t in data["tasks"] if t["id"] == at["task_id"]), None)
        if not task:
            print(c("Active task not found.", "red"))
            return

        folder_id = task.get("arc_folder_id")
        if not folder_id:
            print(c(f"Task '{task['title']}' has no Arc folder.", "dim"))
            return

        from arc_browser import ArcSidebarManager
        sidebar = ArcSidebarManager()
        tabs = sidebar.get_tabs_in_folder(folder_id)

        if not tabs:
            print(c(f"No tabs in folder for '{task['title']}'", "dim"))
            return

        print(c(f"\n  Tabs for: {task['title']}\n", "bold"))
        for tab in tabs:
            print(f"  • {tab['title'][:50]}")
            print(c(f"    {tab['url'][:60]}", "dim"))
        print()

    elif subcmd == "cleanup":
        if not at:
            print(c("No active timer. Start a task first.", "dim"))
            return

        task = next((t for t in data["tasks"] if t["id"] == at["task_id"]), None)
        if not task:
            print(c("Active task not found.", "red"))
            return

        manager = TaskTabManager(data)
        print(f"Analyzing tabs for '{task['title']}'...")
        result = manager.on_task_stopped(task, prompt_callback=_cli_tab_cleanup_prompt)

        if result.get("error"):
            print(c(f"Error: {result['error']}", "red"))
        elif result.get("tabs_closed"):
            print(c(f"✓ Closed {result['tabs_closed']} tabs", "green"))
        elif not result.get("unrelated_tabs"):
            print(c("All tabs appear related to the task.", "green"))

    elif subcmd == "restore":
        # Restore archived tabs for a task
        if len(args) < 2:
            print("Usage: wt tabs restore <task-id or title>")
            return

        task = resolve_task(data, " ".join(args[1:]))
        archived = task.get("archived_tabs", [])
        if not archived:
            print(c(f"No archived tabs for '{task['title']}'", "dim"))
            return

        print(c(f"\n  Archived tabs for: {task['title']}\n", "bold"))
        for i, tab in enumerate(archived, 1):
            print(f"  {i}. {tab['title'][:50]}")
            print(c(f"     {tab['url'][:60]}", "dim"))

        try:
            response = input("\n  Open these tabs? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if response not in ("n", "no"):
            applescript = ArcAppleScript()
            opened = applescript.open_urls([t["url"] for t in archived])
            print(c(f"✓ Opened {opened} tabs", "green"))

    else:
        print(c(f"Unknown tabs subcommand: {subcmd}", "red"))
        print("Usage: wt tabs [list|cleanup|restore <task>]")
        sys.exit(1)


def cmd_sprint(args):
    """Show current sprint info and tasks grouped by sprint."""
    data = load()
    all_sprints = get_all_sprints(data)
    current = get_current_sprint(data)

    if current:
        print(c(f"\n  Current sprint: {current['title']}", "bold", "cyan"))
        print(c(f"  Start: {current['startDate']}, Duration: {current['duration']} days", "dim"))
    elif all_sprints:
        print(c("\n  No active sprint right now.", "yellow"))
    else:
        print(c("\n  No sprints found (project not configured or query failed).", "yellow"))
        return

    tasks = [t for t in data.get("tasks", [])
             if t.get("status") != "done" and not t.get("cross_sprint_parent")]
    if not tasks:
        print(c("  No active tasks.", "dim"))
        print()
        return

    at = data.get("active_timer")
    by_sprint = {}
    for t in tasks:
        by_sprint.setdefault(t.get("sprint", "Unassigned"), []).append(t)

    for sprint_name in sorted(by_sprint.keys()):
        sprint_tasks = by_sprint[sprint_name]
        print(c(f"\n  {sprint_name}", "bold"))
        for t in sprint_tasks:
            logged = task_logged_mins(t) + task_live_mins(t, at)
            running = at and at.get("task_id") == t["id"]
            dot = c("▶ ", "green") if running else "  "
            print(f"    {dot}{t['title'][:50]:<52} {fmt_mins(logged)}")
    print()


def cmd_set_sprint(args):
    """Set or change the sprint for a task."""
    if len(args) < 2:
        print("Usage: wt set-sprint <task> <sprint-title>")
        sys.exit(1)

    data = load()
    all_sprints = get_all_sprints(data)
    if not all_sprints:
        print(c("No sprints found.", "red")); sys.exit(1)

    # First arg is task, rest is sprint title
    task = resolve_task(data, args[0])
    sprint_query = " ".join(args[1:])

    if sprint_query.lower() == "none":
        task.pop("sprint", None)
        task.pop("sprint_id", None)
        save(data)
        print(c(f"✓ Cleared sprint for '{task['title']}'", "green"))
        return

    match = _match_sprint(all_sprints, sprint_query)
    if not match:
        # Show available sprints
        print(c(f"No sprint matching '{sprint_query}'.", "red"))
        print(c("  Available sprints:", "dim"))
        for s in all_sprints[-10:]:  # Show last 10
            print(c(f"    {s['title']}", "dim"))
        sys.exit(1)

    task["sprint"] = match["title"]
    task["sprint_id"] = match["id"]
    save(data)
    print(c(f"✓ Set sprint for '{task['title']}' to {match['title']}", "green"))


def cmd_split_sprint(args):
    """Split cross-sprint task: create shadow tasks for previous sprints."""
    if not args:
        print("Usage: wt split-sprint <task>"); sys.exit(1)

    data = load()
    task = resolve_task(data, " ".join(args))
    all_sprints = get_all_sprints(data)

    if not all_sprints:
        print(c("No sprints found.", "red")); sys.exit(1)

    summary = sprint_summary_for_task(task, all_sprints)
    if len(summary) <= 1:
        sprint_name = summary[0]["sprint_title"] if summary else task.get("sprint", "unknown")
        print(c(f"Task '{task['title']}' only has time in {sprint_name}. No split needed.", "dim"))
        return

    # Show breakdown
    print(c(f"\n  Sprint breakdown for: {task['title']}\n", "bold"))
    for s in summary:
        marker = " ← current" if s == summary[-1] else ""
        print(f"    {s['sprint_title']:<20} {fmt_mins(s['total_mins']):<10} ({len(s['logs'])} logs){marker}")

    print(f"\n  This will create {len(summary) - 1} shadow task(s) for previous sprints.")
    try:
        response = input("  Proceed? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(); return

    if response in ("n", "no"):
        return

    def on_progress(msg):
        print(c(f"  {msg}", "dim"), flush=True)

    result = split_cross_sprint_task(task, data, save, all_sprints, progress_callback=on_progress)
    if result.get("success"):
        for st in result.get("sprint_tasks_created", []):
            issue = st.get("issue_ref", "no issue")
            print(c(f"  ✓ {st['sprint']}: {fmt_mins(st['total_mins'])} → {issue}", "green"))
        print(c(f"  ✓ Main task updated to {result.get('main_sprint', '?')}", "green"))
    else:
        print(c(f"  ✗ Split failed: {result.get('error', 'unknown')}", "red"))


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
    "delete": cmd_delete,
    "del": cmd_delete,
    "rm": cmd_delete,
    "rename": cmd_rename,
    "mv": cmd_rename,
    "status": cmd_status,
    "notes": cmd_notes,
    "link": cmd_link,
    "unlink": cmd_unlink,
    "config": cmd_config,
    "presence": cmd_presence,
    "roles": cmd_roles,
    "arc": cmd_arc,
    "iterm": cmd_iterm,
    "tabs": cmd_tabs,
    "calendar": cmd_calendar,
    "cal": cmd_calendar,
    "sprint": cmd_sprint,
    "set-sprint": cmd_set_sprint,
    "split-sprint": cmd_split_sprint,
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
    COMMANDS[cmd](args[1:])


if __name__ == "__main__":
    main()
