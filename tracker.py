#!/usr/bin/env python3
"""
Workload Tracker — keyboard-driven TUI for Carlos's four roles.
Data is persisted to ~/.workload_tracker.json

Keyboard shortcuts:
  n        — New task
  e        — Edit selected task
  d        — Delete selected task
  t        — Toggle timer on selected task
  l        — Manage time logs (add/edit/delete/split/merge)
  s        — Cycle status of selected task
  g        — Create and link GitHub issue
  o        — Open linked GitHub issue in browser
  c        — Import from Google Calendar
  a        — Toggle showing done tasks
  1-4      — Filter by role (1=DemoKit, 2=Demos, 3=Strategic, 4=Other, 0=All)
  tab      — Switch between Task board / Overview panels
  ↑↓       — Navigate tasks
  q / esc  — Quit / close modal

Notes column indicators:
  C        — Imported from calendar
  #        — Linked to GitHub issue
  +        — Has local notes
"""

import json
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.screen import ModalScreen
from textual.widgets import (
    Button, Checkbox, DataTable, Footer, Header, Input, Label,
    Select, Static, Switch, TabbedContent, TabPane, TextArea
)
from textual.reactive import reactive

from idle_detector import get_idle_seconds
from wt import (
    get_role_repo, create_github_issue, add_to_project_and_update, close_github_issue,
    sync_project_status, get_role_activity, update_project_activity, get_calendar_events,
    get_gcal_service, GCAL_CREDENTIALS_FILE, setup_issue_in_project, sync_project_hours,
    task_logged_mins as wt_task_logged_mins, task_uploaded_mins, task_pending_upload_mins,
    get_project_hours, mins_to_quarter_hours, fmt_mins as wt_fmt_mins, get_current_sprint
)

DATA_FILE = Path.home() / ".workload_tracker.json"
NOTES_DIR = Path.home() / ".workload_tracker_notes"

DEFAULT_ROLES = [
    {"id": "demokit",   "label": "Managing DemoKit",  "color": "blue"},
    {"id": "demos",     "label": "Demos & Workshops", "color": "green"},
    {"id": "strategic", "label": "Strategic Deals",   "color": "yellow"},
    {"id": "other",     "label": "Other",             "color": "white"},
]

STATUSES = ["todo", "inprogress", "done"]
STATUS_LABELS = {"todo": "To Do", "inprogress": "In Progress", "done": "Done"}
STATUS_COLORS = {"todo": "white", "inprogress": "blue", "done": "green"}


def uid() -> str:
    import random, string
    return datetime.now().strftime("%Y%m%d%H%M%S") + "".join(random.choices(string.ascii_lowercase, k=4))


def fmt_mins(mins: float) -> str:
    if not mins:
        return "0m"
    h = int(mins // 60)
    m = int(mins % 60)
    return f"{h}h {m}m" if h else f"{m}m"


def load_data() -> dict:
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
        data["roles"] = [r.copy() for r in DEFAULT_ROLES]
    return data


def save_data(data: dict):
    """Save data to disk, preserving roles and config from disk if modified externally."""
    # Reload roles and config from disk to preserve external changes (e.g., from CLI)
    if DATA_FILE.exists():
        try:
            disk_data = json.loads(DATA_FILE.read_text())
            # Preserve roles from disk (CLI may have modified them)
            if "roles" in disk_data:
                data["roles"] = disk_data["roles"]
            # Preserve config from disk
            if "config" in disk_data:
                data["config"] = disk_data["config"]
        except Exception:
            pass  # If we can't read, just save what we have
    DATA_FILE.write_text(json.dumps(data, indent=2))


def get_roles(data: dict) -> list:
    """Return list of role dicts"""
    return data.get("roles", [])


def get_role_map(data: dict) -> dict:
    """Return dict of role_id -> role dict"""
    return {r["id"]: r for r in data.get("roles", [])}


def task_logged_mins(task: dict) -> float:
    return sum(l.get("minutes", 0) for l in task.get("logs", []))


def task_live_mins(task: dict, active_timer: Optional[dict]) -> float:
    if active_timer and active_timer.get("task_id") == task["id"]:
        return (time.time() - active_timer["started_at"]) / 60
    return 0.0


def has_local_notes(task_id: str) -> bool:
    """Check if task has a local notes file."""
    p = NOTES_DIR / f"{task_id}.md"
    return p.exists() and p.stat().st_size > 0


# ──────────────────────────────────────────────────────────
# Modal: New / Edit Task
# ──────────────────────────────────────────────────────────

class TaskModal(ModalScreen):
    CSS = """
    TaskModal {
        align: center middle;
    }
    #modal-box {
        width: 60;
        height: auto;
        background: $surface;
        border: tall $primary;
        padding: 1 2;
    }
    #modal-box Label { margin-bottom: 1; }
    #modal-box Input, #modal-box Select { margin-bottom: 1; }
    #modal-box Horizontal { margin-bottom: 1; height: auto; }
    #modal-box Switch { margin-right: 1; }
    #modal-actions { margin-top: 1; }
    """

    def __init__(self, task_data: Optional[dict] = None, roles: Optional[list] = None):
        super().__init__()
        self._task_data = task_data
        self._roles = roles or []
        self._is_new = task_data is None

    def compose(self) -> ComposeResult:
        t = self._task_data or {}
        role_options = [(r["label"], r["id"]) for r in self._roles]
        default_role = self._roles[0]["id"] if self._roles else "other"
        status_options = [(STATUS_LABELS[s], s) for s in STATUSES]
        with Container(id="modal-box"):
            yield Label("Edit task" if self._task_data else "New task")
            yield Input(value=t.get("title", ""), placeholder="Task title...", id="inp-title")
            yield Input(value=t.get("description", ""), placeholder="Description (optional)", id="inp-desc")
            yield Select(role_options, value=t.get("role_id", default_role), id="sel-role", prompt="Select role")
            yield Select(status_options, value=t.get("status", "todo"), id="sel-status", prompt="Select status")
            if self._is_new:
                with Horizontal():
                    yield Switch(value=False, id="chk-github")
                    yield Label("Create GitHub issue")
            with Horizontal(id="modal-actions"):
                yield Button("Save  [s]", variant="primary", id="btn-save")
                yield Button("Cancel  [esc]", id="btn-cancel")

    def on_mount(self):
        self.query_one("#inp-title").focus()

    def on_key(self, event):
        if event.key == "escape":
            self.dismiss(None)
        elif event.key == "s" and not isinstance(self.focused, (Input, Switch)):
            self._save()

    @on(Button.Pressed, "#btn-save")
    def save(self):
        self._save()

    @on(Button.Pressed, "#btn-cancel")
    def cancel(self):
        self.dismiss(None)

    def _save(self):
        title = self.query_one("#inp-title").value.strip()
        if not title:
            self.query_one("#inp-title").focus()
            return
        desc = self.query_one("#inp-desc").value.strip()
        role_id = self.query_one("#sel-role").value or "demokit"
        status = self.query_one("#sel-status").value or "todo"
        result = {
            "id": self._task_data["id"] if self._task_data else uid(),
            "title": title,
            "description": desc,
            "role_id": role_id,
            "status": status,
            "logs": self._task_data.get("logs", []) if self._task_data else [],
            "created_at": self._task_data.get("created_at", time.time()) if self._task_data else time.time(),
        }
        # Preserve additional fields from existing task
        if self._task_data:
            for key in ("github_issue", "arc_folder_id", "archived_tabs"):
                if key in self._task_data:
                    result[key] = self._task_data[key]
            # Track if title changed for GitHub issue update
            if self._task_data.get("title") != title:
                result["_title_changed"] = True
                result["_old_title"] = self._task_data.get("title")
        # Check if user wants to create GitHub issue (new tasks only)
        if self._is_new:
            switch = self.query_one("#chk-github", Switch)
            if switch.value:
                result["_create_github_issue"] = True
        self.dismiss(result)


# ──────────────────────────────────────────────────────────
# Modal: Tab Cleanup (Arc Integration)
# ──────────────────────────────────────────────────────────

class TabCleanupModal(ModalScreen):
    """Modal for selecting which unrelated tabs to close."""
    CSS = """
    TabCleanupModal { align: center middle; }
    #cleanup-box {
        width: 70;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: tall $primary;
        padding: 1 2;
    }
    #cleanup-box Label { margin-bottom: 1; }
    .tab-item { margin-bottom: 1; }
    .tab-url { color: $text-muted; }
    .tab-reason { color: $text-muted; font-style: italic; }
    #tab-list { max-height: 20; border: solid $primary; margin-bottom: 1; padding: 1; }
    """

    def __init__(self, unrelated_tabs: list, task_title: str):
        super().__init__()
        self._tabs = unrelated_tabs
        self._task_title = task_title
        self._selected = set(range(len(unrelated_tabs)))  # All selected by default

    def compose(self) -> ComposeResult:
        with Container(id="cleanup-box"):
            yield Label(f"[bold]Tab Cleanup — {self._task_title}[/]")
            yield Label(f"Found {len(self._tabs)} potentially unrelated tabs:")
            with ScrollableContainer(id="tab-list"):
                for i, tab in enumerate(self._tabs):
                    yield Static(
                        f"[{'green' if i in self._selected else 'dim'}]●[/] "
                        f"{tab.get('title', 'Unknown')[:45]}\n"
                        f"  [dim]{tab.get('url', '')[:55]}[/]\n"
                        f"  [italic dim]{tab.get('reason', '')}[/]",
                        id=f"tab-{i}",
                        classes="tab-item"
                    )
            with Horizontal():
                yield Button("Close Selected", variant="primary", id="btn-close-tabs")
                yield Button("Keep All", id="btn-keep")

    def on_key(self, event):
        if event.key == "escape":
            self.dismiss([])

    @on(Button.Pressed, "#btn-close-tabs")
    def close_tabs(self):
        selected_tabs = [self._tabs[i] for i in sorted(self._selected)]
        self.dismiss(selected_tabs)

    @on(Button.Pressed, "#btn-keep")
    def keep_all(self):
        self.dismiss([])


# ──────────────────────────────────────────────────────────
# Modal: Confirm Create GitHub Issue
# ──────────────────────────────────────────────────────────

class ConfirmCreateIssueModal(ModalScreen):
    """Modal to confirm creating a GitHub issue for a task."""
    CSS = """
    ConfirmCreateIssueModal { align: center middle; }
    #issue-box {
        width: 60;
        height: auto;
        background: $surface;
        border: tall $primary;
        padding: 1 2;
    }
    #issue-box Label { margin-bottom: 1; }
    #issue-actions { margin-top: 1; }
    """

    def __init__(self, task_title: str, repo: str):
        super().__init__()
        self._task_title = task_title
        self._repo = repo

    def compose(self) -> ComposeResult:
        with Container(id="issue-box"):
            yield Label("[bold]Create GitHub Issue?[/]")
            yield Label(f"Task: '{self._task_title}'")
            yield Label(f"No GitHub issue linked.")
            yield Label(f"Create one in [cyan]{self._repo}[/]?")
            with Horizontal(id="issue-actions"):
                yield Button("Create Issue  [y]", variant="primary", id="btn-create")
                yield Button("Cancel  [esc]", id="btn-cancel-issue")

    def on_mount(self):
        self.query_one("#btn-create").focus()

    def on_key(self, event):
        if event.key == "escape":
            self.dismiss(False)
        elif event.key in ("y", "enter"):
            self.dismiss(True)

    @on(Button.Pressed, "#btn-create")
    def confirm(self):
        self.dismiss(True)

    @on(Button.Pressed, "#btn-cancel-issue")
    def cancel(self):
        self.dismiss(False)


# ──────────────────────────────────────────────────────────
# Modal: Create GitHub Issue
# ──────────────────────────────────────────────────────────

class CreateIssueModal(ModalScreen):
    """Modal to confirm creating a GitHub issue."""
    CSS = """
    CreateIssueModal { align: center middle; }
    #create-issue-box {
        width: 65;
        height: auto;
        background: $surface;
        border: tall $primary;
        padding: 1 2;
    }
    #create-issue-box Label { margin-bottom: 1; }
    #create-issue-actions { margin-top: 1; }
    """

    def __init__(self, task_title: str, repo: str, logged_mins: float = 0,
                 activity: str = None, sprint: str = None, status: str = "todo"):
        super().__init__()
        self._task_title = task_title
        self._repo = repo
        self._logged_mins = logged_mins
        self._activity = activity
        self._sprint = sprint
        self._status = status

    def compose(self) -> ComposeResult:
        hours = mins_to_quarter_hours(self._logged_mins) if self._logged_mins > 0 else 0
        with Container(id="create-issue-box"):
            yield Label("[bold]Create GitHub Issue[/]")
            yield Label(f"Task: '{self._task_title}'")
            yield Label(f"Repo: [cyan]{self._repo}[/]")
            yield Label("")
            yield Label("[bold]Project fields:[/]")
            yield Label(f"  Status: [cyan]{self._status}[/]")
            if self._activity:
                yield Label(f"  Activity: [cyan]{self._activity}[/]")
            if self._sprint:
                yield Label(f"  Sprint: [cyan]{self._sprint}[/]")
            if self._logged_mins > 0:
                yield Label(f"  Hours: [green]{hours}h[/] ({fmt_mins(self._logged_mins)})")
            yield Label("")
            with Horizontal(id="create-issue-actions"):
                yield Button("Create  [y]", variant="primary", id="btn-create-issue")
                yield Button("Cancel  [esc]", id="btn-cancel-create")

    def on_mount(self):
        self.query_one("#btn-create-issue").focus()

    def on_key(self, event):
        if event.key == "escape":
            self.dismiss(False)
        elif event.key in ("y", "enter"):
            self.dismiss(True)

    @on(Button.Pressed, "#btn-create-issue")
    def create(self):
        self.dismiss(True)

    @on(Button.Pressed, "#btn-cancel-create")
    def cancel(self):
        self.dismiss(False)


# ──────────────────────────────────────────────────────────
# Modal: Sync GitHub Issue to Project
# ──────────────────────────────────────────────────────────

class SyncIssueModal(ModalScreen):
    """Modal to confirm syncing project fields for an existing issue."""
    CSS = """
    SyncIssueModal { align: center middle; }
    #sync-issue-box {
        width: 65;
        height: auto;
        background: $surface;
        border: tall $primary;
        padding: 1 2;
    }
    #sync-issue-box Label { margin-bottom: 1; }
    #sync-issue-actions { margin-top: 1; }
    """

    def __init__(self, task_title: str, issue_ref: str, status: str,
                 activity: str = None, sprint: str = None, hours: float = 0):
        super().__init__()
        self._task_title = task_title
        self._issue_ref = issue_ref
        self._status = status
        self._activity = activity
        self._sprint = sprint
        self._hours = hours

    def compose(self) -> ComposeResult:
        with Container(id="sync-issue-box"):
            yield Label("[bold]Sync Project Fields[/]")
            yield Label(f"Task: '{self._task_title}'")
            yield Label(f"Issue: [cyan]{self._issue_ref}[/]")
            yield Label("")
            yield Label("[bold]Will update:[/]")
            yield Label(f"  Status: [cyan]{self._status}[/]")
            if self._activity:
                yield Label(f"  Activity: [cyan]{self._activity}[/]")
            if self._sprint:
                yield Label(f"  Sprint: [cyan]{self._sprint}[/]")
            if self._hours > 0:
                yield Label(f"  Hours: [green]{self._hours}h[/]")
            yield Label("")
            with Horizontal(id="sync-issue-actions"):
                yield Button("Sync  [y]", variant="primary", id="btn-sync-issue")
                yield Button("Cancel  [esc]", id="btn-cancel-sync")

    def on_mount(self):
        self.query_one("#btn-sync-issue").focus()

    def on_key(self, event):
        if event.key == "escape":
            self.dismiss(False)
        elif event.key in ("y", "enter"):
            self.dismiss(True)

    @on(Button.Pressed, "#btn-sync-issue")
    def confirm(self):
        self.dismiss(True)

    @on(Button.Pressed, "#btn-cancel-sync")
    def cancel(self):
        self.dismiss(False)


# ──────────────────────────────────────────────────────────
# Modal: Confirm Close Task (with hours comparison)
# ──────────────────────────────────────────────────────────

class ConfirmCloseTaskModal(ModalScreen):
    """Modal to confirm closing a task, showing local vs GH hours."""
    CSS = """
    ConfirmCloseTaskModal { align: center middle; }
    #close-task-box {
        width: 60;
        height: auto;
        background: $surface;
        border: tall $primary;
        padding: 1 2;
    }
    #close-task-box Label { margin-bottom: 1; }
    #close-task-actions { margin-top: 1; }
    .hours-row { margin-bottom: 1; }
    """

    def __init__(self, task_title: str, local_mins: float, gh_hours: float | None):
        super().__init__()
        self._task_title = task_title
        self._local_mins = local_mins
        self._gh_hours = gh_hours

    def compose(self) -> ComposeResult:
        local_hours = mins_to_quarter_hours(self._local_mins) if self._local_mins > 0 else 0
        with Container(id="close-task-box"):
            yield Label("[bold]Close Task?[/]")
            yield Label(f"Task: '{self._task_title}'")
            yield Label("")
            yield Label(f"[bold]Local logged:[/] {fmt_mins(self._local_mins)} → [green]{local_hours}h[/]", classes="hours-row")
            if self._gh_hours is not None:
                yield Label(f"[bold]GitHub project:[/] [cyan]{self._gh_hours}h[/]", classes="hours-row")
                if local_hours != self._gh_hours:
                    yield Label(f"[yellow]Hours will be updated to {local_hours}h[/]")
            else:
                yield Label("[dim]Not in GitHub project yet[/]", classes="hours-row")
            yield Label("")
            with Horizontal(id="close-task-actions"):
                yield Button("Close Task  [y]", variant="primary", id="btn-close-task")
                yield Button("Cancel  [esc]", id="btn-cancel-close")

    def on_mount(self):
        self.query_one("#btn-close-task").focus()

    def on_key(self, event):
        if event.key == "escape":
            self.dismiss(False)
        elif event.key in ("y", "enter"):
            self.dismiss(True)

    @on(Button.Pressed, "#btn-close-task")
    def confirm(self):
        self.dismiss(True)

    @on(Button.Pressed, "#btn-cancel-close")
    def cancel(self):
        self.dismiss(False)


# ──────────────────────────────────────────────────────────
# Modal: Confirm Close (No GitHub)
# ──────────────────────────────────────────────────────────

class ConfirmCloseNoGitHubModal(ModalScreen):
    """Modal to confirm closing a task without GitHub integration."""
    CSS = """
    ConfirmCloseNoGitHubModal { align: center middle; }
    #close-no-gh-box {
        width: 60;
        height: auto;
        background: $surface;
        border: tall $warning;
        padding: 1 2;
    }
    #close-no-gh-box Label { margin-bottom: 1; }
    #close-no-gh-actions { margin-top: 1; }
    """

    def __init__(self, task_title: str, logged_mins: float):
        super().__init__()
        self._task_title = task_title
        self._logged_mins = logged_mins

    def compose(self) -> ComposeResult:
        with Container(id="close-no-gh-box"):
            yield Label("[bold yellow]Close Without GitHub?[/]")
            yield Label(f"Task: '{self._task_title}'")
            yield Label("")
            yield Label(f"[bold]Logged:[/] {fmt_mins(self._logged_mins)}")
            yield Label("")
            yield Label("[yellow]No GitHub issue linked.[/]")
            yield Label("[yellow]No repo configured for this role.[/]")
            yield Label("[dim]Time will only be recorded locally.[/]")
            yield Label("")
            with Horizontal(id="close-no-gh-actions"):
                yield Button("Close Anyway  [y]", variant="warning", id="btn-close-no-gh")
                yield Button("Cancel  [esc]", id="btn-cancel-no-gh")

    def on_mount(self):
        self.query_one("#btn-close-no-gh").focus()

    def on_key(self, event):
        if event.key == "escape":
            self.dismiss(False)
        elif event.key in ("y", "enter"):
            self.dismiss(True)

    @on(Button.Pressed, "#btn-close-no-gh")
    def confirm(self):
        self.dismiss(True)

    @on(Button.Pressed, "#btn-cancel-no-gh")
    def cancel(self):
        self.dismiss(False)


# ──────────────────────────────────────────────────────────
# Modal: Confirm Delete
# ──────────────────────────────────────────────────────────

class ConfirmDeleteModal(ModalScreen):
    """Modal to confirm task deletion."""
    CSS = """
    ConfirmDeleteModal { align: center middle; }
    #confirm-box {
        width: 50;
        height: auto;
        background: $surface;
        border: tall $error;
        padding: 1 2;
    }
    #confirm-box Label { margin-bottom: 1; }
    #confirm-actions { margin-top: 1; }
    """

    def __init__(self, task_title: str):
        super().__init__()
        self._task_title = task_title

    def compose(self) -> ComposeResult:
        with Container(id="confirm-box"):
            yield Label("[bold red]Delete Task?[/]")
            yield Label(f"'{self._task_title}'")
            yield Label("[dim]This cannot be undone.[/]")
            with Horizontal(id="confirm-actions"):
                yield Button("Delete  [y]", variant="error", id="btn-confirm")
                yield Button("Cancel  [esc]", id="btn-cancel")

    def on_mount(self):
        self.query_one("#btn-cancel").focus()

    def on_key(self, event):
        if event.key == "escape":
            self.dismiss(False)
        elif event.key in ("y", "enter"):
            self.dismiss(True)

    @on(Button.Pressed, "#btn-confirm")
    def confirm(self):
        self.dismiss(True)

    @on(Button.Pressed, "#btn-cancel")
    def cancel(self):
        self.dismiss(False)


# ──────────────────────────────────────────────────────────
# Modal: Log Time
# ──────────────────────────────────────────────────────────

class LogTimeModal(ModalScreen):
    CSS = """
    LogTimeModal { align: center middle; }
    #log-box {
        width: 56;
        height: auto;
        background: $surface;
        border: tall $primary;
        padding: 1 2;
    }
    #log-box Label { margin-bottom: 1; }
    #log-box Input { margin-bottom: 1; }
    #log-list { max-height: 12; border: solid $primary; margin-bottom: 1; }
    """

    def __init__(self, task_data: dict):
        super().__init__()
        self._task = task_data

    def compose(self) -> ComposeResult:
        logs = list(reversed(self._task.get("logs", [])))
        with Container(id="log-box"):
            yield Label(f"Log time — {self._task['title']}")
            yield Input(placeholder="Minutes (e.g. 45)", id="inp-mins", type="number")
            yield Input(placeholder="Note (optional)", id="inp-note")
            with Horizontal():
                yield Button("Add  [a]", variant="primary", id="btn-add")
                yield Button("Close  [esc]", id="btn-close")
            yield Label("─── History ───")
            with ScrollableContainer(id="log-list"):
                if not logs:
                    yield Label("  No entries yet.")
                for log in logs:
                    dt = datetime.fromtimestamp(log.get("at", 0)).strftime("%m/%d %H:%M")
                    yield Label(f"  {fmt_mins(log['minutes'])}  {log.get('note','—')}  [{dt}]")

    def on_mount(self):
        self.query_one("#inp-mins").focus()

    def on_key(self, event):
        if event.key == "escape":
            self.dismiss(None)
        elif event.key == "a" and not isinstance(self.focused, Input):
            self._add()

    @on(Button.Pressed, "#btn-add")
    def add_entry(self):
        self._add()

    @on(Button.Pressed, "#btn-close")
    def close(self):
        self.dismiss(None)

    def _add(self):
        try:
            mins = float(self.query_one("#inp-mins").value)
        except ValueError:
            self.query_one("#inp-mins").focus()
            return
        if mins <= 0:
            return
        note = self.query_one("#inp-note").value.strip() or "Manual entry"
        log = {"id": uid(), "minutes": mins, "note": note, "at": time.time()}
        self.dismiss(log)


# ──────────────────────────────────────────────────────────
# Modal: Edit Log Entry
# ──────────────────────────────────────────────────────────

class EditLogEntryModal(ModalScreen):
    """Modal for editing a single log entry."""
    CSS = """
    EditLogEntryModal { align: center middle; }
    #edit-log-box {
        width: 56;
        height: auto;
        background: $surface;
        border: tall $primary;
        padding: 1 2;
    }
    #edit-log-box Label { margin-bottom: 1; }
    #edit-log-box Input { margin-bottom: 1; }
    """

    def __init__(self, log_entry: dict):
        super().__init__()
        self._log = log_entry

    def compose(self) -> ComposeResult:
        with Container(id="edit-log-box"):
            yield Label("[bold]Edit Log Entry[/]")
            yield Label(f"ID: {self._log.get('id', '?')[:15]}...")
            yield Input(
                value=str(self._log.get("minutes", 0)),
                placeholder="Minutes",
                id="inp-edit-mins",
                type="number"
            )
            yield Input(
                value=self._log.get("note", ""),
                placeholder="Note",
                id="inp-edit-note"
            )
            with Horizontal():
                yield Button("Save  [s]", variant="primary", id="btn-save-log")
                yield Button("Cancel  [esc]", id="btn-cancel-log")

    def on_mount(self):
        self.query_one("#inp-edit-mins").focus()

    def on_key(self, event):
        if event.key == "escape":
            self.dismiss(None)
        elif event.key == "s" and not isinstance(self.focused, Input):
            self._save()

    @on(Button.Pressed, "#btn-save-log")
    def save(self):
        self._save()

    @on(Button.Pressed, "#btn-cancel-log")
    def cancel(self):
        self.dismiss(None)

    def _save(self):
        try:
            mins = float(self.query_one("#inp-edit-mins").value)
        except ValueError:
            self.query_one("#inp-edit-mins").focus()
            return
        if mins <= 0:
            return
        note = self.query_one("#inp-edit-note").value.strip()
        self.dismiss({"minutes": mins, "note": note})


# ──────────────────────────────────────────────────────────
# Modal: Add Log Entry
# ──────────────────────────────────────────────────────────

class AddLogModal(ModalScreen):
    """Modal for adding a new log entry."""
    CSS = """
    AddLogModal { align: center middle; }
    #add-log-box {
        width: 56;
        height: auto;
        background: $surface;
        border: tall $primary;
        padding: 1 2;
    }
    #add-log-box Label { margin-bottom: 1; }
    #add-log-box Input { margin-bottom: 1; }
    """

    def compose(self) -> ComposeResult:
        with Container(id="add-log-box"):
            yield Label("[bold]Add Log Entry[/]")
            yield Input(placeholder="Minutes", id="inp-add-mins", type="number")
            yield Input(placeholder="Note (optional)", id="inp-add-note")
            with Horizontal():
                yield Button("Add  [enter]", variant="primary", id="btn-add-log")
                yield Button("Cancel  [esc]", id="btn-cancel-log")

    def on_mount(self):
        self.query_one("#inp-add-mins").focus()

    def on_key(self, event):
        if event.key == "escape":
            self.dismiss(None)
        elif event.key == "enter":
            self._add()

    @on(Button.Pressed, "#btn-add-log")
    def add(self):
        self._add()

    @on(Button.Pressed, "#btn-cancel-log")
    def cancel(self):
        self.dismiss(None)

    def _add(self):
        try:
            mins = float(self.query_one("#inp-add-mins").value)
        except ValueError:
            self.query_one("#inp-add-mins").focus()
            return
        if mins <= 0:
            return
        note = self.query_one("#inp-add-note").value.strip() or "Manual entry"
        self.dismiss({"minutes": mins, "note": note})


# ──────────────────────────────────────────────────────────
# Modal: Split Log Entry
# ──────────────────────────────────────────────────────────

class SplitLogModal(ModalScreen):
    """Modal for splitting a log entry."""
    CSS = """
    SplitLogModal { align: center middle; }
    #split-box {
        width: 56;
        height: auto;
        background: $surface;
        border: tall $primary;
        padding: 1 2;
    }
    #split-box Label { margin-bottom: 1; }
    #split-box Input { margin-bottom: 1; }
    """

    def __init__(self, log_entry: dict):
        super().__init__()
        self._log = log_entry

    def compose(self) -> ComposeResult:
        total = self._log.get("minutes", 0)
        with Container(id="split-box"):
            yield Label("[bold]Split Log Entry[/]")
            yield Label(f"Total: {fmt_mins(total)}")
            yield Label("Split at minute:")
            yield Input(
                placeholder=f"1-{int(total)-1}",
                id="inp-split-at",
                type="number"
            )
            yield Label("[dim]Creates two entries: first part + remainder[/]")
            with Horizontal():
                yield Button("Split  [s]", variant="primary", id="btn-split")
                yield Button("Cancel  [esc]", id="btn-cancel-split")

    def on_mount(self):
        self.query_one("#inp-split-at").focus()

    def on_key(self, event):
        if event.key == "escape":
            self.dismiss(None)
        elif event.key == "s" and not isinstance(self.focused, Input):
            self._split()

    @on(Button.Pressed, "#btn-split")
    def split(self):
        self._split()

    @on(Button.Pressed, "#btn-cancel-split")
    def cancel(self):
        self.dismiss(None)

    def _split(self):
        try:
            split_at = float(self.query_one("#inp-split-at").value)
        except ValueError:
            self.query_one("#inp-split-at").focus()
            return
        total = self._log.get("minutes", 0)
        if split_at <= 0 or split_at >= total:
            return
        self.dismiss(split_at)


# ──────────────────────────────────────────────────────────
# Modal: Confirm Delete Log
# ──────────────────────────────────────────────────────────

class ConfirmDeleteLogModal(ModalScreen):
    """Modal to confirm log deletion."""
    CSS = """
    ConfirmDeleteLogModal { align: center middle; }
    #confirm-log-box {
        width: 50;
        height: auto;
        background: $surface;
        border: tall $error;
        padding: 1 2;
    }
    #confirm-log-box Label { margin-bottom: 1; }
    """

    def __init__(self, log_entry: dict):
        super().__init__()
        self._log = log_entry

    def compose(self) -> ComposeResult:
        mins = self._log.get("minutes", 0)
        note = self._log.get("note", "—")
        with Container(id="confirm-log-box"):
            yield Label("[bold red]Delete Log Entry?[/]")
            yield Label(f"{fmt_mins(mins)} — {note[:30]}")
            yield Label("[dim]This cannot be undone.[/]")
            with Horizontal():
                yield Button("Delete  [y]", variant="error", id="btn-confirm-del")
                yield Button("Cancel  [esc]", id="btn-cancel-del")

    def on_mount(self):
        self.query_one("#btn-cancel-del").focus()

    def on_key(self, event):
        if event.key == "escape":
            self.dismiss(False)
        elif event.key in ("y", "enter"):
            self.dismiss(True)

    @on(Button.Pressed, "#btn-confirm-del")
    def confirm(self):
        self.dismiss(True)

    @on(Button.Pressed, "#btn-cancel-del")
    def cancel(self):
        self.dismiss(False)


# ──────────────────────────────────────────────────────────
# Modal: Edit Logs (Full Management)
# ──────────────────────────────────────────────────────────

class EditLogsModal(ModalScreen):
    """Full log management modal with add, edit, delete, split, merge."""
    BINDINGS = [
        Binding("a", "add_log", "Add log", priority=True),
        Binding("e", "edit_log", "Edit", priority=True),
        Binding("d", "delete_log", "Delete", priority=True),
        Binding("s", "split_log", "Split", priority=True),
        Binding("m", "merge_log", "Merge", priority=True),
        Binding("escape", "close_modal", "Close", priority=True),
    ]
    CSS = """
    EditLogsModal { align: center middle; }
    #logs-modal-box {
        width: 80;
        height: auto;
        max-height: 85%;
        background: $surface;
        border: tall $primary;
        padding: 1 2;
    }
    #logs-modal-box Label { margin-bottom: 1; }
    #logs-table { height: 20; margin-bottom: 1; }
    #logs-actions { margin-bottom: 1; }
    #logs-help { color: $text-muted; }
    """

    def __init__(self, task_dict: dict, data: dict, save_callback):
        super().__init__()
        self._task_dict = task_dict
        self._data = data
        self._save = save_callback
        self._selected_log_ids: set = set()

    def compose(self) -> ComposeResult:
        total = sum(l.get("minutes", 0) for l in self._task_dict.get("logs", []))
        with Container(id="logs-modal-box"):
            yield Label(f"[bold]Time Logs — {self._task_dict['title']}[/]")
            yield Label(f"Total: {fmt_mins(total)}")
            yield DataTable(id="logs-table", cursor_type="row", zebra_stripes=True)
            with Horizontal(id="logs-actions"):
                yield Button("Add  [a]", variant="primary", id="btn-add-log")
                yield Button("Edit  [e]", id="btn-edit")
                yield Button("Delete  [d]", variant="error", id="btn-delete")
                yield Button("Split  [s]", id="btn-split-log")
                yield Button("Merge  [m]", id="btn-merge")
                yield Button("Close  [esc]", id="btn-close-logs")
            yield Label("[dim]Keys: \\[a]dd \\[e]dit \\[d]elete \\[s]plit \\[m]erge with next row[/]", id="logs-help")

    def on_mount(self):
        self._build_table()
        table = self.query_one("#logs-table", DataTable)
        table.focus()

    def _build_table(self):
        table = self.query_one("#logs-table", DataTable)
        table.clear(columns=True)
        table.add_columns("ID", "Duration", "Note", "Time Range", "Date")
        logs = self._task_dict.get("logs", [])
        for log in logs:
            log_id = log.get("id", "?")[:11]
            mins = fmt_mins(log.get("minutes", 0))
            note = log.get("note", "—")[:25]
            started = log.get("started_at")
            ended = log.get("ended_at")
            at = log.get("at", 0)

            if started and ended:
                start_str = datetime.fromtimestamp(started).strftime("%H:%M")
                end_str = datetime.fromtimestamp(ended).strftime("%H:%M")
                time_range = f"{start_str}-{end_str}"
            else:
                time_range = "—"

            date_str = datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M") if at else "—"
            table.add_row(log_id, mins, note, time_range, date_str, key=log.get("id"))

    def _get_selected_log(self) -> Optional[dict]:
        table = self.query_one("#logs-table", DataTable)
        if table.cursor_row is None or table.row_count == 0:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            return next((l for l in self._task_dict.get("logs", []) if l.get("id") == key), None)
        except Exception:
            return None

    def on_key(self, event) -> None:
        """Handle keys before they reach child widgets."""
        # Let inputs handle their own keys
        focused = self.app.focused
        if isinstance(focused, Input):
            return

        key_actions = {
            "a": self._add_log,
            "e": self._edit_log,
            "d": self._delete_log,
            "s": self._split_log,
            "m": self._start_merge,
            "escape": lambda: self.dismiss(True),
        }
        if event.key in key_actions:
            event.stop()
            event.prevent_default()
            key_actions[event.key]()

    @on(Button.Pressed, "#btn-add-log")
    def add_btn(self):
        self._add_log()

    @on(Button.Pressed, "#btn-edit")
    def edit_btn(self):
        self._edit_log()

    @on(Button.Pressed, "#btn-delete")
    def delete_btn(self):
        self._delete_log()

    @on(Button.Pressed, "#btn-split-log")
    def split_btn(self):
        self._split_log()

    @on(Button.Pressed, "#btn-merge")
    def merge_btn(self):
        self._start_merge()

    @on(Button.Pressed, "#btn-close-logs")
    def close_btn(self):
        self.dismiss(True)

    def _add_log(self):
        self.app.push_screen(
            AddLogModal(),
            self._on_add_done
        )

    def _on_add_done(self, result: Optional[dict]):
        if not result:
            return
        log = {
            "id": uid(),
            "minutes": result["minutes"],
            "note": result["note"],
            "at": time.time()
        }
        self._task_dict.setdefault("logs", []).append(log)
        self._save(self._data)
        self._build_table()
        self._update_total()

    def _update_total(self):
        total = sum(l.get("minutes", 0) for l in self._task_dict.get("logs", []))
        # Update the total label - it's the second Label in the container
        labels = self.query("Label")
        if len(labels) > 1:
            labels[1].update(f"Total: {fmt_mins(total)}")

    def _edit_log(self):
        log = self._get_selected_log()
        if not log:
            return
        self.app.push_screen(
            EditLogEntryModal(log),
            lambda result: self._on_edit_done(log, result)
        )

    def _on_edit_done(self, log: dict, result: Optional[dict]):
        if not result:
            return
        log["minutes"] = result["minutes"]
        log["note"] = result["note"]
        self._save(self._data)
        self._build_table()
        self._update_total()

    def _delete_log(self):
        log = self._get_selected_log()
        if not log:
            return
        self.app.push_screen(
            ConfirmDeleteLogModal(log),
            lambda confirmed: self._on_delete_done(log, confirmed)
        )

    def _on_delete_done(self, log: dict, confirmed: bool):
        if not confirmed:
            return
        self._task_dict["logs"] = [l for l in self._task_dict.get("logs", []) if l.get("id") != log.get("id")]
        self._save(self._data)
        self._build_table()
        self._update_total()

    def _split_log(self):
        log = self._get_selected_log()
        if not log:
            return
        if log.get("minutes", 0) < 2:
            return  # Can't split less than 2 minutes
        self.app.push_screen(
            SplitLogModal(log),
            lambda split_at: self._on_split_done(log, split_at)
        )

    def _on_split_done(self, log: dict, split_at: Optional[float]):
        if not split_at:
            return

        total_mins = log.get("minutes", 0)
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
        logs = self._task_dict.get("logs", [])
        log_idx = next((i for i, l in enumerate(logs) if l.get("id") == log.get("id")), None)
        if log_idx is not None:
            logs[log_idx:log_idx+1] = [first_log, second_log]
            self._save(self._data)
            self._build_table()

    def _start_merge(self):
        """Merge requires selecting two rows. We'll use the current + next row."""
        table = self.query_one("#logs-table", DataTable)
        if table.row_count < 2:
            return

        logs = self._task_dict.get("logs", [])
        if table.cursor_row is None or table.cursor_row >= len(logs) - 1:
            return

        log1 = logs[table.cursor_row]
        log2 = logs[table.cursor_row + 1]

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

        # Remove old logs and insert merged at same position
        log_idx = table.cursor_row
        self._task_dict["logs"] = [l for l in logs if l.get("id") not in (log1.get("id"), log2.get("id"))]
        self._task_dict["logs"].insert(log_idx, merged_log)

        self._save(self._data)
        self._build_table()
        self._update_total()


# ──────────────────────────────────────────────────────────
# Modal: Calendar Time Confirmation
# ──────────────────────────────────────────────────────────

class CalendarTimeModal(ModalScreen):
    """Modal to confirm or adjust time when importing a calendar event."""
    CSS = """
    CalendarTimeModal { align: center middle; }
    #time-modal-box {
        width: 50;
        height: auto;
        background: $surface;
        border: tall $primary;
        padding: 1 2;
    }
    #time-modal-box Label { margin-bottom: 1; }
    #time-input { width: 15; margin-bottom: 1; }
    #time-actions { margin-top: 1; }
    """

    def __init__(self, event: dict):
        super().__init__()
        self._event = event
        self._duration = event["duration_mins"]

    def compose(self) -> ComposeResult:
        with Container(id="time-modal-box"):
            yield Label(f"[bold]{self._event['title']}[/]")
            yield Label(f"Duration: {fmt_mins(self._duration)}")
            yield Label("")
            yield Label("Log time (minutes):")
            yield Input(value=str(int(self._duration)), id="time-input", type="integer")
            with Horizontal(id="time-actions"):
                yield Button("Confirm", variant="primary", id="btn-confirm")
                yield Button("Cancel", id="btn-cancel")

    def on_mount(self):
        self.query_one("#time-input", Input).focus()

    def on_key(self, event):
        if event.key == "escape":
            self.dismiss(None)

    @on(Button.Pressed, "#btn-confirm")
    def on_confirm(self):
        self._do_confirm()

    @on(Button.Pressed, "#btn-cancel")
    def on_cancel(self):
        self.dismiss(None)

    @on(Input.Submitted, "#time-input")
    def on_input_submitted(self):
        self._do_confirm()

    def _do_confirm(self):
        try:
            time_input = self.query_one("#time-input", Input)
            minutes = float(time_input.value) if time_input.value else self._duration
            if minutes <= 0:
                self.notify("Time must be greater than 0", severity="warning")
                return
            self.dismiss(minutes)
        except ValueError:
            self.notify("Invalid time value", severity="warning")


# ──────────────────────────────────────────────────────────
# Modal: Calendar Import
# ──────────────────────────────────────────────────────────

class CalendarModal(ModalScreen):
    """Modal to list and import calendar events as tasks."""
    BINDINGS = [
        Binding("i", "import_event", "Import", priority=True),
        Binding("d", "delete_event", "Delete", priority=True),
        Binding("escape", "close_modal", "Close", priority=True),
    ]
    CSS = """
    CalendarModal { align: center middle; }
    #calendar-modal-box {
        width: 90;
        height: auto;
        max-height: 85%;
        background: $surface;
        border: tall $primary;
        padding: 1 2;
    }
    #calendar-modal-box Label { margin-bottom: 1; }
    #calendar-table { height: 15; margin-bottom: 1; }
    #calendar-actions { margin-bottom: 1; }
    #calendar-help { color: $text-muted; }
    #calendar-error { color: $error; margin-bottom: 1; }
    #role-select { width: 30; margin-bottom: 1; }
    #days-input { width: 10; }
    """

    def __init__(self, data: dict, save_callback):
        super().__init__()
        self._data = data
        self._save = save_callback
        self._events = []
        self._days_back = 1

    def compose(self) -> ComposeResult:
        roles = get_roles(self._data)
        with Container(id="calendar-modal-box"):
            yield Label("[bold]Import from Google Calendar[/]")
            yield Label("", id="calendar-error")
            with Horizontal():
                yield Label("Role: ")
                yield Select(
                    [(r["label"], r["id"]) for r in roles],
                    value=roles[0]["id"] if roles else "other",
                    id="role-select"
                )
            with Horizontal():
                yield Label("Days back: ")
                yield Input(value="1", id="days-input", type="integer")
                yield Button("Refresh", id="btn-refresh", variant="default")
            yield DataTable(id="calendar-table", cursor_type="row", zebra_stripes=True)
            with Horizontal(id="calendar-actions"):
                yield Button("Import  [i]", variant="primary", id="btn-import")
                yield Button("Delete  [d]", variant="error", id="btn-delete")
                yield Button("Close  [Esc]", id="btn-close")
            yield Label("[dim]\\[i] Import event  \\[d] Delete imported task  ✓ = already imported[/]", id="calendar-help")

    def on_mount(self):
        self._load_events()
        table = self.query_one("#calendar-table", DataTable)
        table.focus()

    def _load_events(self):
        """Load calendar events and populate the table."""
        error_label = self.query_one("#calendar-error", Label)
        table = self.query_one("#calendar-table", DataTable)

        # Preserve cursor position
        selected_key = None
        try:
            if table.cursor_row is not None and table.row_count > 0:
                selected_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            pass

        # Check if calendar is configured
        service = get_gcal_service()
        if not service:
            if not GCAL_CREDENTIALS_FILE.exists():
                error_label.update("[red]Calendar not configured. Run 'wt calendar setup' first.[/]")
            else:
                error_label.update("[red]Calendar authentication failed.[/]")
            return

        error_label.update("")

        # Get days back from input
        try:
            days_input = self.query_one("#days-input", Input)
            self._days_back = int(days_input.value) if days_input.value else 1
        except (ValueError, Exception):
            self._days_back = 1

        # Get calendar ID from config
        config = self._data.get("config", {})
        calendar_id = config.get("calendar_id", "primary")

        # Fetch events
        self._events = get_calendar_events(days_back=self._days_back, calendar_id=calendar_id)

        # Get already imported UIDs
        imported_uids = {t.get("calendar_event_uid") for t in self._data.get("tasks", [])}

        # Build table
        table.clear(columns=True)
        table.add_columns("", "Date", "Time", "Duration", "Title")

        for event in self._events:
            is_imported = event["uid"] in imported_uids
            status = "✓" if is_imported else " "
            start_dt = datetime.fromtimestamp(event["start_date"])
            date_str = start_dt.strftime("%m/%d")
            time_str = start_dt.strftime("%H:%M")
            duration = fmt_mins(event["duration_mins"])
            title = event["title"][:45]

            table.add_row(status, date_str, time_str, duration, title, key=event["uid"])

        # Restore cursor position
        if selected_key:
            try:
                for idx, row_key in enumerate(table.rows.keys()):
                    if row_key.value == selected_key:
                        table.cursor_coordinate = (idx, table.cursor_coordinate.column)
                        break
            except Exception:
                pass

    def _get_selected_event(self):
        table = self.query_one("#calendar-table", DataTable)
        if table.cursor_row is None or table.row_count == 0:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            return next((e for e in self._events if e["uid"] == key), None)
        except Exception:
            return None

    def action_import_event(self):
        self._do_import()

    def action_delete_event(self):
        self._do_delete()

    def action_close_modal(self):
        self.dismiss(False)

    @on(Button.Pressed, "#btn-import")
    def on_import(self):
        self._do_import()

    @on(Button.Pressed, "#btn-delete")
    def on_delete(self):
        self._do_delete()

    @on(Button.Pressed, "#btn-close")
    def on_close(self):
        self.dismiss(False)

    @on(Button.Pressed, "#btn-refresh")
    def on_refresh(self):
        self._load_events()

    def _do_delete(self):
        """Delete the task that was imported from the selected calendar event."""
        event = self._get_selected_event()
        if not event:
            self.notify("No event selected", severity="warning")
            return

        # Find the task with this calendar_event_uid
        task = next((t for t in self._data.get("tasks", []) if t.get("calendar_event_uid") == event["uid"]), None)
        if not task:
            self.notify("Event not imported yet", severity="warning")
            return

        # Delete the task
        self._data["tasks"] = [t for t in self._data["tasks"] if t["id"] != task["id"]]
        self._save(self._data)

        self.notify(f"Deleted: {task['title']}", severity="information")

        # Refresh table to remove the checkmark
        self._load_events()

    def _do_import(self):
        event = self._get_selected_event()
        if not event:
            self.notify("No event selected", severity="warning")
            return

        # Check if already imported
        imported_uids = {t.get("calendar_event_uid") for t in self._data.get("tasks", [])}
        if event["uid"] in imported_uids:
            self.notify("Event already imported", severity="warning")
            return

        # Store event and role for the callback
        role_select = self.query_one("#role-select", Select)
        self._pending_import = {
            "event": event,
            "role_id": role_select.value,
        }

        # Show time confirmation modal
        self.app.push_screen(CalendarTimeModal(event), self._on_time_confirmed)

    def _on_time_confirmed(self, minutes: float | None):
        """Callback after time confirmation modal."""
        if minutes is None or not hasattr(self, '_pending_import'):
            return

        event = self._pending_import["event"]
        role_id = self._pending_import["role_id"]
        del self._pending_import

        # Create task
        task = {
            "id": uid(),
            "title": event["title"],
            "description": event.get("notes", ""),
            "role_id": role_id,
            "status": "done",
            "logs": [{
                "id": uid(),
                "minutes": round(minutes, 2),
                "note": f"Calendar: {event['calendar_name']}",
                "at": event["end_date"],
                "started_at": event["start_date"],
                "ended_at": event["end_date"],
            }],
            "created_at": time.time(),
            "calendar_event_uid": event["uid"],
        }

        self._data["tasks"].insert(0, task)
        self._save(self._data)

        self.notify(f"Imported: {event['title']} ({fmt_mins(minutes)})", severity="information")

        # Refresh table to show the checkmark
        self._load_events()


# ──────────────────────────────────────────────────────────
# Main App
# ──────────────────────────────────────────────────────────

class WorkloadTracker(App):
    CSS = """
    Screen { background: $background; }
    Header { background: $primary; }
    Footer { background: $surface; }

    #main { height: 1fr; }

    /* Sidebar */
    #sidebar {
        width: 26;
        background: $surface;
        border-right: solid $primary;
        padding: 1;
    }
    #sidebar Label { color: $text-muted; margin-bottom: 1; }
    .role-stat { margin-bottom: 1; }
    .role-label { color: $text; }
    .role-time  { color: $text-muted; }

    /* Task panel */
    #task-panel { padding: 1; }
    #filter-bar { height: 3; margin-bottom: 1; }
    #filter-bar Label { margin-right: 1; color: $text-muted; }
    #filter-bar Select { width: 22; }
    #task-table { height: 1fr; }

    /* Overview */
    #overview { padding: 1; }
    .ov-header { color: $text; margin-bottom: 1; text-style: bold; }
    .ov-row { margin-bottom: 1; }

    DataTable { height: 1fr; }
    """

    BINDINGS = [
        Binding("n",   "new_task",    "New task"),
        Binding("e",   "edit_task",   "Edit"),
        Binding("d",   "delete_task", "Delete"),
        Binding("t",   "toggle_timer","Timer"),
        Binding("l",   "log_time",    "Manage logs"),
        Binding("s",   "cycle_status","Cycle status"),
        Binding("g",   "link_github", "GitHub"),
        Binding("o",   "open_github", "Open issue"),
        Binding("c",   "import_calendar", "Calendar"),
        Binding("a",   "toggle_show_done", "Show done"),
        Binding("1",   "filter_role_1", "DemoKit", show=False),
        Binding("2",   "filter_role_2", "Demos",   show=False),
        Binding("3",   "filter_role_3", "Strategic",show=False),
        Binding("4",   "filter_role_4", "Other",   show=False),
        Binding("0",   "filter_role_0", "All",     show=False),
        Binding("tab", "switch_tab",   "Overview"),
        Binding("q",   "quit",         "Quit"),
    ]

    filter_role: reactive[str] = reactive("all")
    active_tab: reactive[str] = reactive("board")
    show_done: reactive[bool] = reactive(False)

    def __init__(self):
        super().__init__()
        self._data = load_data()
        self._timer_task = None

    # ── Compose ────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        roles = get_roles(self._data)
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="sidebar"):
                yield Label("TODAY'S WORK")
                yield Static(id="sidebar-today")
                yield Label("─" * 20)
                yield Label("TIME BY ROLE")
                yield Static(id="sidebar-stats")
                yield Label("─" * 20)
                yield Label("ACTIVE TIMER")
                yield Static(id="sidebar-timer")
            with TabbedContent(id="tabs"):
                with TabPane("Task board  [b]", id="board"):
                    with Horizontal(id="filter-bar"):
                        yield Label("Role:")
                        yield Select(
                            [("All roles", "all")] + [(r["label"], r["id"]) for r in roles],
                            value="all", id="role-filter"
                        )
                    yield DataTable(id="task-table", cursor_type="row", zebra_stripes=True)
                with TabPane("Overview  [o]", id="overview-tab"):
                    yield ScrollableContainer(Static(id="overview-content"), id="overview")
        yield Footer()

    def on_mount(self):
        self._build_table()
        self._refresh_sidebar()
        self._refresh_overview()
        self.set_interval(1, self._tick)

    # ── Table ──────────────────────────────────────────────

    def _build_table(self):
        table = self.query_one("#task-table", DataTable)
        table.clear(columns=True)
        table.add_columns("●", "Title", "Role", "Status", "Logged", "N", "Description")
        self._populate_table()

    def _populate_table(self):
        table = self.query_one("#task-table", DataTable)

        # Preserve cursor position by saving the selected row key
        selected_key = None
        try:
            if table.cursor_row is not None and table.row_count > 0:
                selected_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            pass

        table.clear()
        tasks = self._visible_tasks()
        role_map = get_role_map(self._data)
        roles = get_roles(self._data)
        default_role = roles[-1] if roles else {"id": "other", "label": "Other", "color": "white"}
        for task in tasks:
            logged = task_logged_mins(task) + task_live_mins(task, self._data.get("active_timer"))
            role = role_map.get(task["role_id"], default_role)
            status = task.get("status", "todo")
            is_running = (
                self._data.get("active_timer") and
                self._data["active_timer"].get("task_id") == task["id"]
            )
            timer_dot = "▶" if is_running else " "
            # Notes indicator: # for GitHub issue, + for local notes, C for calendar
            if task.get("calendar_event_uid"):
                notes_icon = "C"
            elif task.get("github_issue"):
                notes_icon = "#"
            elif has_local_notes(task["id"]):
                notes_icon = "+"
            else:
                notes_icon = " "
            table.add_row(
                timer_dot,
                task["title"],
                role["label"],
                STATUS_LABELS.get(status, status),
                fmt_mins(logged),
                notes_icon,
                task.get("description", ""),
                key=task["id"],
            )

        # Restore cursor position
        if selected_key:
            try:
                for idx, row_key in enumerate(table.rows.keys()):
                    if row_key.value == selected_key:
                        table.cursor_coordinate = (idx, table.cursor_coordinate.column)
                        break
            except Exception:
                pass

    def _visible_tasks(self) -> list:
        tasks = self._data.get("tasks", [])
        if self.filter_role != "all":
            tasks = [t for t in tasks if t.get("role_id") == self.filter_role]
        if not self.show_done:
            tasks = [t for t in tasks if t.get("status") != "done"]
        return tasks

    def _selected_task(self) -> Optional[dict]:
        table = self.query_one("#task-table", DataTable)
        if table.cursor_row is None:
            return None
        row_key = table.get_row_at(table.cursor_row)
        # row_key is actually the cell values; use coordinate to get key
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            return next((t for t in self._data["tasks"] if t["id"] == key), None)
        except Exception:
            return None

    # ── Sidebar & Overview ────────────────────────────────

    def _refresh_sidebar(self):
        # Today's work section
        today_work = self._get_today_work()
        today_total = sum(mins for _, mins, _ in today_work)
        today_lines = []
        if today_work:
            today_lines.append(f"[bold]{fmt_mins(today_total)}[/] total\n")
            for title, mins, is_running in today_work[:6]:  # Show top 6
                prefix = "[green]▶[/] " if is_running else "  "
                today_lines.append(f"{prefix}{title[:16]}\n    {fmt_mins(mins)}")
            if len(today_work) > 6:
                today_lines.append(f"\n  [dim]+{len(today_work) - 6} more...[/]")
        else:
            today_lines.append("[dim]No work logged today[/]")
        self.query_one("#sidebar-today", Static).update("\n".join(today_lines))

        # Time by role section
        by_role = self._mins_by_role()
        total = sum(by_role.values())
        roles = get_roles(self._data)
        lines = []
        for role in roles:
            mins = by_role.get(role["id"], 0)
            pct = round((mins / total * 100)) if total else 0
            lines.append(f"[{role.get('color', 'white')}]{role['label'][:18]}[/]\n  {fmt_mins(mins)} ({pct}%)\n")
        self.query_one("#sidebar-stats", Static).update("\n".join(lines))
        self._refresh_timer_display()

    def _refresh_timer_display(self):
        at = self._data.get("active_timer")
        if at:
            task = next((t for t in self._data["tasks"] if t["id"] == at["task_id"]), None)
            elapsed = (time.time() - at["started_at"]) / 60
            name = task["title"][:18] if task else "?"
            self.query_one("#sidebar-timer", Static).update(
                f"[green]▶ {name}[/]\n  {fmt_mins(elapsed)}"
            )
        else:
            self.query_one("#sidebar-timer", Static).update("[dim]No timer running[/]")

    def _refresh_overview(self):
        by_role = self._mins_by_role()
        total = sum(by_role.values())
        tasks = self._data.get("tasks", [])
        roles = get_roles(self._data)

        lines = [f"[bold]Total logged:[/] {fmt_mins(total)}  ({len(tasks)} tasks)\n"]
        for role in roles:
            role_tasks = [t for t in tasks if t.get("role_id") == role["id"]]
            mins = by_role.get(role["id"], 0)
            pct = round(mins / total * 100) if total else 0
            bar_filled = int(pct / 5)
            bar = "█" * bar_filled + "░" * (20 - bar_filled)
            color = role.get('color', 'white')
            lines.append(
                f"[{color} bold]{role['label']}[/]\n"
                f"  [{color}]{bar}[/] {pct}%  {fmt_mins(mins)}  ({len(role_tasks)} tasks)"
            )
            for t in role_tasks:
                t_mins = task_logged_mins(t) + task_live_mins(t, self._data.get("active_timer"))
                status = STATUS_LABELS.get(t.get("status", "todo"), "")
                lines.append(f"    [dim]{'▶' if self._is_running(t) else '·'}[/] {t['title'][:40]}  [dim]{fmt_mins(t_mins)}  {status}[/]")
            lines.append("")

        self.query_one("#overview-content", Static).update("\n".join(lines))

    def _mins_by_role(self) -> dict:
        roles = get_roles(self._data)
        result = {r["id"]: 0.0 for r in roles}
        for task in self._data.get("tasks", []):
            rid = task.get("role_id", "other")
            result[rid] = result.get(rid, 0) + task_logged_mins(task) + task_live_mins(task, self._data.get("active_timer"))
        return result

    def _is_running(self, task: dict) -> bool:
        at = self._data.get("active_timer")
        return bool(at and at.get("task_id") == task["id"])

    def _get_today_work(self) -> list[tuple[str, float, bool]]:
        """Get tasks worked on today with their minutes logged today.

        Returns list of (task_title, minutes_today, is_running) tuples.
        """
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        active_timer = self._data.get("active_timer")
        result = []

        for task in self._data.get("tasks", []):
            # Sum logs from today
            today_mins = 0.0
            for log in task.get("logs", []):
                log_time = log.get("at", 0)
                if log_time >= today_start:
                    today_mins += log.get("minutes", 0)

            # Add live timer if running and started today
            is_running = self._is_running(task)
            if is_running and active_timer:
                started = active_timer.get("started_at", 0)
                if started >= today_start:
                    today_mins += (time.time() - started) / 60

            if today_mins > 0:
                result.append((task["title"], today_mins, is_running))

        # Sort by minutes descending
        result.sort(key=lambda x: x[1], reverse=True)
        return result

    # ── Tick ──────────────────────────────────────────────

    def _tick(self):
        if self._data.get("active_timer"):
            self._check_presence()
            self._refresh_sidebar()
            self._populate_table()
            self._refresh_overview()

    # ── Presence Detection ────────────────────────────────

    def _check_presence(self):
        """Check if user is idle and auto-stop timer if threshold exceeded."""
        config = self._data.get("config", {})
        if not config.get("presence_detection_enabled", False):
            return

        timeout_minutes = config.get("idle_timeout_minutes", 15)
        idle_seconds = get_idle_seconds()
        idle_minutes = idle_seconds / 60

        if idle_minutes >= timeout_minutes:
            self._auto_stop_timer(idle_seconds)

    def _auto_stop_timer(self, idle_seconds: float):
        """Auto-stop timer due to inactivity."""
        at = self._data.get("active_timer")
        if not at:
            return

        task = next((t for t in self._data["tasks"] if t["id"] == at["task_id"]), None)
        if not task:
            self._data["active_timer"] = None
            save_data(self._data)
            return

        # Calculate elapsed time
        started_at = at["started_at"]
        ended_at = time.time()
        elapsed_seconds = ended_at - started_at
        elapsed_minutes = elapsed_seconds / 60

        # Optionally subtract idle time
        config = self._data.get("config", {})
        if config.get("subtract_idle_time", True):
            logged_minutes = max(0, elapsed_minutes - (idle_seconds / 60))
            note = f"Timer session (auto-stopped, {int(idle_seconds / 60)}m idle subtracted)"
        else:
            logged_minutes = elapsed_minutes
            note = "Timer session (auto-stopped due to inactivity)"

        # Log time if meaningful
        if logged_minutes > 0.1:
            task.setdefault("logs", []).append({
                "id": uid(), "minutes": round(logged_minutes, 2),
                "note": note, "at": ended_at,
                "started_at": started_at, "ended_at": ended_at
            })

        self._data["active_timer"] = None
        save_data(self._data)

        # Sync hours to GitHub project if task has linked issue
        if task.get("github_issue"):
            self._sync_task_hours_async(task)

        # Notify user
        idle_mins = int(idle_seconds / 60)
        self.notify(
            f"Timer stopped: {idle_mins}m idle. Logged {fmt_mins(logged_minutes)} to '{task['title'][:20]}'",
            severity="warning",
            timeout=10
        )

    @work(thread=True)
    def _sync_task_hours_async(self, task: dict):
        """Sync task hours to GitHub project in background thread."""
        issue_ref = task.get("github_issue")
        if not issue_ref:
            return
        if sync_project_hours(issue_ref, task, self._data, save_data):
            hours = mins_to_quarter_hours(task_logged_mins(task))
            self.call_from_thread(
                self.notify, f"Synced {hours}h to {issue_ref}", severity="information"
            )

    # ── Actions ───────────────────────────────────────────

    def action_switch_tab(self):
        tabs = self.query_one("#tabs", TabbedContent)
        active = tabs.active
        tabs.active = "overview-tab" if active == "board" else "board"

    @on(Select.Changed, "#role-filter")
    def on_role_filter(self, event: Select.Changed):
        self.filter_role = event.value
        self._populate_table()

    def action_filter_role_0(self): self._set_filter("all")
    def action_filter_role_1(self): self._set_filter_by_index(0)
    def action_filter_role_2(self): self._set_filter_by_index(1)
    def action_filter_role_3(self): self._set_filter_by_index(2)
    def action_filter_role_4(self): self._set_filter_by_index(3)

    def _set_filter_by_index(self, index: int):
        roles = get_roles(self._data)
        if index < len(roles):
            self._set_filter(roles[index]["id"])

    def _set_filter(self, role_id: str):
        self.filter_role = role_id
        try:
            self.query_one("#role-filter", Select).value = role_id
        except Exception:
            pass
        self._populate_table()

    def action_toggle_show_done(self):
        self.show_done = not self.show_done
        self._populate_table()
        status = "shown" if self.show_done else "hidden"
        self.notify(f"Done tasks {status}", severity="information")

    def action_new_task(self):
        roles = get_roles(self._data)
        self.push_screen(TaskModal(roles=roles), self._on_task_saved)

    def action_import_calendar(self):
        self.push_screen(CalendarModal(self._data, save_data), self._on_calendar_closed)

    def _on_calendar_closed(self, result):
        # Refresh UI after calendar modal closes (events may have been imported)
        self._populate_table()
        self._refresh_sidebar()
        self._refresh_overview()

    def action_edit_task(self):
        task = self._selected_task()
        if task:
            roles = get_roles(self._data)
            self.push_screen(TaskModal(task_data=task, roles=roles), self._on_task_saved)

    def _on_task_saved(self, result: Optional[dict]):
        if not result:
            return

        # Check if title changed and task has GitHub issue
        title_changed = result.pop("_title_changed", False)
        old_title = result.pop("_old_title", None)
        create_issue = result.pop("_create_github_issue", False)

        tasks = self._data["tasks"]
        existing = next((i for i, t in enumerate(tasks) if t["id"] == result["id"]), None)
        is_new = existing is None
        if existing is not None:
            tasks[existing] = result
        else:
            tasks.insert(0, result)
        save_data(self._data)

        # Update GitHub issue title if needed
        if title_changed and result.get("github_issue"):
            self._update_github_issue_title(result)

        # Create GitHub issue for new task if requested
        if is_new and create_issue:
            self._create_github_issue_for_task(result)

        self._populate_table()
        self._refresh_sidebar()
        self._refresh_overview()

    @work(thread=True)
    def _update_github_issue_title(self, task: dict):
        """Update GitHub issue title in background thread."""
        from wt import update_issue_title
        if update_issue_title(task["github_issue"], task["title"]):
            self.call_from_thread(
                self.notify, f"Updated GitHub issue: {task['github_issue']}", severity="information"
            )
        else:
            self.call_from_thread(
                self.notify, "Failed to update GitHub issue title", severity="warning"
            )

    @work(thread=True)
    def _create_github_issue_for_task(self, task: dict, refresh_ui: bool = False):
        """Create GitHub issue for a task and set up project fields in background thread."""
        repo = get_role_repo(task, self._data)
        if not repo:
            self.call_from_thread(
                self.notify, "No GitHub repo configured for this role", severity="warning"
            )
            return
        try:
            # Create the issue
            issue_ref = create_github_issue(task, repo)
            task["github_issue"] = issue_ref
            save_data(self._data)
            self.call_from_thread(
                self.notify, f"Created issue: {issue_ref}", severity="information"
            )

            # Set up project fields (status, activity, sprint, hours)
            result = setup_issue_in_project(issue_ref, task, self._data)
            if result["success"]:
                save_data(self._data)  # Save uploaded_at markers
                hours = mins_to_quarter_hours(task_logged_mins(task))
                self.call_from_thread(
                    self.notify, f"Added to project: {hours}h", severity="information"
                )
            elif result["errors"]:
                self.call_from_thread(
                    self.notify, f"Project setup: {', '.join(result['errors'])}", severity="warning"
                )

            if refresh_ui:
                self.call_from_thread(self._populate_table)
        except Exception as e:
            self.call_from_thread(
                self.notify, f"Failed to create issue: {e}", severity="error"
            )

    def action_link_github(self):
        """Create and link a GitHub issue, or sync project fields if already linked."""
        task = self._selected_task()
        if not task:
            return

        if task.get("github_issue"):
            # Issue exists - offer to sync project fields
            self._sync_existing_issue(task)
            return

        repo = get_role_repo(task, self._data)
        if not repo:
            self.notify("No GitHub repo configured for this role", severity="warning")
            return
        # Show modal with project field preview
        logged_mins = task_logged_mins(task)
        activity = get_role_activity(task, self._data)
        sprint = get_current_sprint(self._data)
        sprint_title = sprint["title"] if sprint else None
        status = task.get("status", "todo")
        self.push_screen(
            CreateIssueModal(task["title"], repo, logged_mins, activity, sprint_title, status),
            lambda confirmed: self._on_create_issue_confirmed(task, confirmed)
        )

    def _sync_existing_issue(self, task: dict):
        """Sync project fields for an existing GitHub issue."""
        logged_mins = task_logged_mins(task)
        activity = get_role_activity(task, self._data)
        sprint = get_current_sprint(self._data)
        sprint_title = sprint["title"] if sprint else None
        status = task.get("status", "todo")
        hours = mins_to_quarter_hours(logged_mins) if logged_mins > 0 else 0

        self.push_screen(
            SyncIssueModal(task["title"], task["github_issue"], status, activity, sprint_title, hours),
            lambda confirmed: self._on_sync_issue_confirmed(task, confirmed)
        )

    def _on_sync_issue_confirmed(self, task: dict, confirmed: bool):
        """Callback after sync confirmation."""
        if not confirmed:
            return
        self._sync_issue_to_project(task)

    @work(thread=True)
    def _sync_issue_to_project(self, task: dict):
        """Sync all project fields for an existing issue."""
        issue_ref = task.get("github_issue")
        if not issue_ref:
            return

        result = setup_issue_in_project(issue_ref, task, self._data)
        if result["success"]:
            save_data(self._data)
            hours = mins_to_quarter_hours(task_logged_mins(task))
            sprint = get_current_sprint(self._data)
            sprint_title = sprint["title"] if sprint else "N/A"
            self.call_from_thread(
                self.notify, f"Synced to project: {hours}h, {sprint_title}", severity="information"
            )
            self.call_from_thread(self._populate_table)
        else:
            self.call_from_thread(
                self.notify, f"Sync errors: {', '.join(result['errors'])}", severity="warning"
            )

    def _on_create_issue_confirmed(self, task: dict, confirmed: bool):
        """Callback after issue creation confirmation."""
        if not confirmed:
            return  # User cancelled
        self._create_github_issue_for_task(task, refresh_ui=True)

    def action_open_github(self):
        """Open the linked GitHub issue in the default browser."""
        task = self._selected_task()
        if not task:
            return
        issue_ref = task.get("github_issue")
        if not issue_ref:
            self.notify("No GitHub issue linked to this task", severity="warning")
            return
        # Parse issue_ref (e.g., "owner/repo#123") to URL
        if "#" in issue_ref:
            repo_part, issue_num = issue_ref.rsplit("#", 1)
            url = f"https://github.com/{repo_part}/issues/{issue_num}"
        else:
            self.notify(f"Invalid issue reference: {issue_ref}", severity="error")
            return
        webbrowser.open(url)
        self.notify(f"Opened: {issue_ref}", severity="information")

    def action_delete_task(self):
        task = self._selected_task()
        if not task:
            return
        self.push_screen(
            ConfirmDeleteModal(task["title"]),
            lambda confirmed: self._on_delete_confirmed(task["id"], confirmed)
        )

    def _on_delete_confirmed(self, task_id: str, confirmed: bool):
        if not confirmed:
            return
        task = next((t for t in self._data["tasks"] if t["id"] == task_id), None)
        if not task:
            return
        if self._is_running(task):
            self._data["active_timer"] = None
        self._data["tasks"] = [t for t in self._data["tasks"] if t["id"] != task_id]
        save_data(self._data)
        self._populate_table()
        self._refresh_sidebar()
        self._refresh_overview()

    def action_toggle_timer(self):
        task = self._selected_task()
        if not task:
            return
        at = self._data.get("active_timer")
        if at and at.get("task_id") == task["id"]:
            # Stop timer — commit minutes
            started_at = at["started_at"]
            ended_at = time.time()
            elapsed = (ended_at - started_at) / 60
            if elapsed > 0.1:
                task.setdefault("logs", []).append({
                    "id": uid(), "minutes": round(elapsed, 2),
                    "note": "Timer session", "at": ended_at,
                    "started_at": started_at, "ended_at": ended_at
                })
            self._data["active_timer"] = None
            save_data(self._data)

            # Sync hours to GitHub project if task has linked issue
            if task.get("github_issue"):
                self._sync_task_hours_async(task)

            # Arc integration: tab cleanup
            self._arc_tab_cleanup(task)
        else:
            # Stop any running timer first
            stopped_task = None
            if at:
                prev = next((t for t in self._data["tasks"] if t["id"] == at["task_id"]), None)
                if prev:
                    started_at = at["started_at"]
                    ended_at = time.time()
                    elapsed = (ended_at - started_at) / 60
                    if elapsed > 0.1:
                        prev.setdefault("logs", []).append({
                            "id": uid(), "minutes": round(elapsed, 2),
                            "note": "Timer session", "at": ended_at,
                            "started_at": started_at, "ended_at": ended_at
                        })
                    stopped_task = prev
                    save_data(self._data)
                    # Sync hours for the stopped task
                    if prev.get("github_issue"):
                        self._sync_task_hours_async(prev)

            self._data["active_timer"] = {"task_id": task["id"], "started_at": time.time()}
            save_data(self._data)

            # Arc integration: focus space on start
            self._arc_on_task_started(task)

            # Arc integration: tab cleanup for previously stopped task
            if stopped_task:
                self._arc_tab_cleanup(stopped_task)

        self._populate_table()
        self._refresh_sidebar()
        self._refresh_overview()

    def _arc_on_task_started(self, task: dict):
        """Arc integration: focus space when starting a task."""
        if not self._data.get("config", {}).get("arc_space_id"):
            return
        try:
            from arc_browser import TaskTabManager
            manager = TaskTabManager(self._data)
            manager.on_task_started(task)
        except ImportError:
            pass

    def _arc_tab_cleanup(self, task: dict):
        """Arc integration: show tab cleanup modal if enabled (runs in background)."""
        if not self._data.get("config", {}).get("tab_cleanup_enabled"):
            return
        # Run classification in background to avoid blocking UI
        self._arc_tab_cleanup_worker(task)

    @work(thread=True)
    def _arc_tab_cleanup_worker(self, task: dict):
        """Background worker for tab classification."""
        try:
            from arc_browser import TaskTabManager
            manager = TaskTabManager(self._data)

            # Get tabs and classify (this makes API call)
            tabs = manager.applescript.get_all_tabs()
            if not tabs:
                return

            classifications = manager.classifier.classify_tabs(tabs, task)
            unrelated = manager.classifier.get_unrelated_tabs(classifications)

            if unrelated:
                unrelated_data = [
                    {"url": c.tab.url, "title": c.tab.title, "reason": c.reason}
                    for c in unrelated
                ]
                # Show modal on main thread
                self.call_from_thread(
                    self._show_tab_cleanup_modal, unrelated_data, task["title"], manager
                )
        except ImportError:
            pass
        except Exception as e:
            self.call_from_thread(
                self.notify, f"Tab cleanup failed: {e}", severity="warning"
            )

    def _show_tab_cleanup_modal(self, unrelated_data: list, task_title: str, manager):
        """Show the tab cleanup modal (called from main thread)."""
        self.push_screen(
            TabCleanupModal(unrelated_data, task_title),
            lambda tabs_to_close: self._on_tabs_cleanup(tabs_to_close, manager)
        )

    def _on_tabs_cleanup(self, tabs_to_close: list, manager):
        """Callback when user selects tabs to close."""
        if not tabs_to_close:
            return
        for _ in tabs_to_close:
            manager.applescript.close_current_tab()
            time.sleep(0.1)

    def action_log_time(self):
        selected = self._selected_task()
        if not selected:
            return
        self.push_screen(
            EditLogsModal(task_dict=selected, data=self._data, save_callback=save_data),
            lambda changed: self._on_logs_modal_closed(changed)
        )

    def _on_logs_modal_closed(self, changed: bool):
        if changed:
            self._populate_table()
            self._refresh_sidebar()
            self._refresh_overview()

    def action_cycle_status(self):
        task = self._selected_task()
        if not task:
            return
        current = task.get("status", "todo")
        idx = STATUSES.index(current) if current in STATUSES else 0
        new_status = STATUSES[(idx + 1) % len(STATUSES)]
        old_status = task["status"]

        # If transitioning to "done", use the close workflow
        if new_status == "done" and old_status != "done":
            self._close_task_with_workflow(task)
        else:
            task["status"] = new_status
            save_data(self._data)
            self._populate_table()
            # Sync status to GitHub project if task has a linked issue
            if task.get("github_issue"):
                self._sync_project_status_async(task, new_status)

    def _close_task_with_workflow(self, task: dict):
        """Handle the task closing workflow with GitHub integration."""
        # Check if role has a GitHub repo
        repo = get_role_repo(task, self._data)

        # If task already has a GitHub issue, always show confirmation and update project
        if task.get("github_issue"):
            self._fetch_gh_hours_and_confirm_close(task)
            return

        # Task has no GitHub issue - check if role has a repo to create one
        if repo:
            # Prompt to create issue with project field preview
            logged_mins = task_logged_mins(task)
            activity = get_role_activity(task, self._data)
            sprint = get_current_sprint(self._data)
            sprint_title = sprint["title"] if sprint else None
            self.push_screen(
                CreateIssueModal(task["title"], repo, logged_mins, activity, sprint_title, "done"),
                lambda confirmed: self._on_create_issue_for_close(task, repo, confirmed)
            )
        else:
            # No GitHub integration - confirm before closing
            logged_mins = task_logged_mins(task)
            self.push_screen(
                ConfirmCloseNoGitHubModal(task["title"], logged_mins),
                lambda confirmed: self._on_close_no_github_confirmed(task, confirmed)
            )

    def _on_close_no_github_confirmed(self, task: dict, confirmed: bool):
        """Handle confirmation for closing task without GitHub integration."""
        if not confirmed:
            return
        task["status"] = "done"
        save_data(self._data)
        self._populate_table()
        self._refresh_sidebar()
        self._refresh_overview()
        self.notify(f"Closed: {task['title']}", severity="information")
        self._arc_on_task_completed(task)

    def _fetch_gh_hours_and_confirm_close(self, task: dict):
        """Fetch GitHub hours in background, then show confirmation modal."""
        self._fetch_gh_hours_worker(task)

    @work(thread=True)
    def _fetch_gh_hours_worker(self, task: dict):
        """Fetch GitHub project hours in background."""
        gh_hours = None
        if task.get("github_issue"):
            gh_hours = get_project_hours(task["github_issue"], self._data)

        local_mins = task_logged_mins(task)
        self.call_from_thread(self._show_close_confirmation, task, local_mins, gh_hours)

    def _show_close_confirmation(self, task: dict, local_mins: float, gh_hours: float | None):
        """Show the close confirmation modal with hours comparison."""
        self.push_screen(
            ConfirmCloseTaskModal(task["title"], local_mins, gh_hours),
            lambda confirmed: self._on_close_confirmed(task, confirmed)
        )

    def _on_close_confirmed(self, task: dict, confirmed: bool):
        """Handle close confirmation response."""
        if not confirmed:
            return
        self._complete_close_workflow(task)

    def _on_create_issue_for_close(self, task: dict, repo: str, confirmed: bool):
        """Handle response from create issue modal during close workflow."""
        if not confirmed:
            self.notify("Task must have GitHub issue to close (role requires it)", severity="warning")
            return

        # Run blocking GitHub operations in a worker thread
        self._run_close_workflow(task, repo, create_issue=True)

    def _on_create_issue_response(self, task: dict, repo: str, create: bool):
        """Handle response from create issue modal."""
        if not create:
            self.notify("Task must have GitHub issue to close (role requires it)", severity="warning")
            return

        # Run blocking GitHub operations in a worker thread
        self._run_close_workflow(task, repo, create_issue=True)

    @work(thread=True)
    def _run_close_workflow(self, task: dict, repo: str, create_issue: bool = False):
        """Run the close workflow in a background thread to avoid blocking."""
        # Create issue if needed
        if create_issue:
            try:
                issue_ref = create_github_issue(task, repo)
                task["github_issue"] = issue_ref
                save_data(self._data)
                self.call_from_thread(self.notify, f"Created issue: {issue_ref}", severity="information")

                # Set up project fields for new issue (status, activity, sprint, hours)
                result = setup_issue_in_project(issue_ref, task, self._data)
                if result["success"]:
                    save_data(self._data)
            except Exception as e:
                self.call_from_thread(self.notify, f"Failed to create issue: {e}", severity="error")
                return

        # Update project if configured
        config = self._data.get("config", {})
        if config.get("github_project_number"):
            try:
                total_mins = task_logged_mins(task)
                hours = mins_to_quarter_hours(total_mins)
                add_to_project_and_update(task["github_issue"], hours, self._data)

                # Mark logs as uploaded
                from wt import mark_logs_uploaded
                mark_logs_uploaded(task)
                save_data(self._data)

                # Set activity if role has one configured
                activity = get_role_activity(task, self._data)
                if activity:
                    update_project_activity(task["github_issue"], activity, self._data)
                    self.call_from_thread(self.notify, f"Updated project (Hours: {hours}h, Activity: {activity})", severity="information")
                else:
                    self.call_from_thread(self.notify, f"Updated project (Hours: {hours}h)", severity="information")
            except Exception as e:
                self.call_from_thread(self.notify, f"Project update failed: {e}", severity="warning")

        # Close the GitHub issue
        if task.get("github_issue"):
            if close_github_issue(task["github_issue"]):
                self.call_from_thread(self.notify, f"Closed issue: {task['github_issue']}", severity="information")

        # Mark as done
        task["status"] = "done"
        save_data(self._data)

        # Update UI from main thread
        self.call_from_thread(self._finish_close_workflow, task)

    def _finish_close_workflow(self, task: dict):
        """Update UI after close workflow completes."""
        self._populate_table()
        self._refresh_sidebar()
        self._refresh_overview()
        self._arc_on_task_completed(task)

    def _complete_close_workflow(self, task: dict):
        """Complete the close workflow for task that already has an issue."""
        self._run_close_workflow(task, "", create_issue=False)

    def _arc_on_task_completed(self, task: dict):
        """Arc integration: archive tabs and remove folder when task is done."""
        if not task.get("arc_folder_id"):
            return
        try:
            from arc_browser import TaskTabManager
            manager = TaskTabManager(self._data)
            manager.on_task_completed(task, save_data)
            self.notify("Arc folder archived. Restart Arc to apply.", severity="information")
        except ImportError:
            pass

    @work(thread=True)
    def _sync_project_status_async(self, task: dict, status: str):
        """Sync task status to GitHub project in background thread."""
        if sync_project_status(task["github_issue"], status, self._data):
            status_label = {"todo": "Todo", "inprogress": "In Progress", "done": "Done"}.get(status, status)
            self.call_from_thread(self.notify, f"Project status: {status_label}", severity="information")


if __name__ == "__main__":
    app = WorkloadTracker()
    app.run()
