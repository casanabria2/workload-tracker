#!/usr/bin/env python3
"""
wt — Workload Tracker CLI
Quick command-line interface to manage tasks without launching the full TUI.

Usage:
    wt add "Task title" --role strategic --status inprogress
    wt list [--role strategic]
    wt start <task-id or partial title>
    wt stop
    wt log <task-id or partial title> <minutes> [note]
    wt notes <task-id or partial title>
    wt status
    wt done <task-id or partial title>
    wt delete <task-id or partial title>

    wt link <task> <github-issue>  — Link task to GitHub issue
    wt unlink <task>               — Unlink task from GitHub issue

    wt add-issue [url-or-ref] [--role ROLE]  — Create task from GitHub issue
    wt add-issue [--role ROLE]               — Interactive: show assigned issues

    wt config                    — Show all config
    wt config <key>              — Show config value
    wt config <key> <value>      — Set config value

    wt roles                     — List all roles
    wt roles add <id> <label>    — Add a new role
    wt roles update <id> <label> — Update role label
    wt roles delete <id>         — Delete a role

Notes are stored in ~/.workload_tracker_notes/<task_id>.md
Tasks linked to GitHub issues use the issue for notes instead.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime

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


def fmt_mins(mins: float) -> str:
    if not mins:
        return "0m"
    h = int(mins // 60)
    m = int(mins % 60)
    return f"{h}h {m}m" if h else f"{m}m"


def task_logged_mins(task: dict) -> float:
    return sum(l.get("minutes", 0) for l in task.get("logs", []))


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
    # Partial title match (case-insensitive)
    q = query.lower()
    matches = [t for t in tasks if q in t["title"].lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(c("Ambiguous match. Did you mean:", "yellow"))
        for t in matches:
            print(f"  {t['id']}  {t['title']}")
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


def normalize_issue_ref(issue_ref: str, data: dict) -> str:
    """Normalize issue reference, using default repo for bare numbers.

    Handles:
      - "262" -> "owner/repo#262" (uses config github_repo)
      - "#262" -> "owner/repo#262" (uses config github_repo)
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


# ── Commands ──────────────────────────────────────────────

def cmd_add(args):
    if not args:
        print("Usage: wt add <title> [--role ROLE] [--status STATUS] [--desc DESC]")
        sys.exit(1)

    data = load()
    roles = get_roles(data)

    # Parse title and flags
    title_parts = []
    role_id = "other"
    status = "todo"
    desc = ""
    i = 0
    while i < len(args):
        if args[i] == "--role" and i + 1 < len(args):
            role_id = resolve_role(data, args[i+1]); i += 2
        elif args[i] == "--status" and i + 1 < len(args):
            status = args[i+1]; i += 2
        elif args[i] == "--desc" and i + 1 < len(args):
            desc = args[i+1]; i += 2
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
    data["tasks"].insert(0, task)
    save(data)
    print(c(f"✓ Added: {title}", "green") + f"  [{roles.get(role_id, role_id)}]  [{STATUS_LABELS.get(status, status)}]")
    print(c(f"  id: {task['id']}", "dim"))


def cmd_list(args):
    data = load()
    tasks = data.get("tasks", [])
    at = data.get("active_timer")
    roles = get_roles(data)
    role_ids = get_role_ids(data)

    filter_role = None
    i = 0
    while i < len(args):
        if args[i] == "--role" and i + 1 < len(args):
            filter_role = resolve_role(data, args[i+1]); i += 2
        else:
            i += 1

    if filter_role:
        tasks = [t for t in tasks if t.get("role_id") == filter_role]

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
            print(f"  {dot}{t['title'][:50]:<52} {notes_icon} {time_str:<10} {status_str}")
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
            elapsed = (time.time() - at["started_at"]) / 60
            if elapsed > 0.05:
                prev.setdefault("logs", []).append({
                    "id": uid(), "minutes": round(elapsed, 2),
                    "note": "Timer session", "at": time.time()
                })
        print(c(f"⏹  Stopped: {prev['title'] if prev else '?'}", "yellow"))

    data["active_timer"] = {"task_id": task["id"], "started_at": time.time()}
    save(data)
    print(c(f"▶  Started: {task['title']}", "green"))


def cmd_stop(args):
    data = load()
    at = data.get("active_timer")
    if not at:
        print(c("No active timer.", "dim")); return
    task = next((t for t in data["tasks"] if t["id"] == at["task_id"]), None)
    elapsed = (time.time() - at["started_at"]) / 60
    if task and elapsed > 0.05:
        task.setdefault("logs", []).append({
            "id": uid(), "minutes": round(elapsed, 2),
            "note": "Timer session", "at": time.time()
        })
    data["active_timer"] = None
    save(data)
    print(c(f"⏹  Stopped: {task['title'] if task else '?'}  ({fmt_mins(elapsed)})", "yellow"))


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


def cmd_done(args):
    if not args:
        print("Usage: wt done <task-id or title>"); sys.exit(1)
    data = load()
    task = resolve_task(data, " ".join(args))
    task["status"] = "done"
    save(data)
    print(c(f"✓ Marked done: {task['title']}", "green"))


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
    """Manage roles: list, add, update, delete"""
    data = load()

    if not args:
        # List roles
        print(c("\n  Roles:\n", "bold"))
        for r in data.get("roles", []):
            task_count = len([t for t in data["tasks"] if t.get("role_id") == r["id"]])
            print(f"  {r['id']:<15} {r['label']:<30} ({task_count} tasks)")
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
    # Issue ref is the last argument
    issue_ref = normalize_issue_ref(args[-1], data)
    query = " ".join(args[:-1])
    task = resolve_task(data, query)

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

    # Set value
    value = args[1]
    config[key_normalized] = value
    save(data)
    print(c(f"✓ Set {key}: {value}", "green"))


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


COMMANDS = {
    "add": cmd_add,
    "add-issue": cmd_add_issue,
    "list": cmd_list,
    "ls": cmd_list,
    "start": cmd_start,
    "stop": cmd_stop,
    "log": cmd_log,
    "done": cmd_done,
    "delete": cmd_delete,
    "del": cmd_delete,
    "rm": cmd_delete,
    "status": cmd_status,
    "notes": cmd_notes,
    "link": cmd_link,
    "unlink": cmd_unlink,
    "config": cmd_config,
    "roles": cmd_roles,
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
