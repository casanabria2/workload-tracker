#!/usr/bin/env python3
"""
Workload Tracker — keyboard-driven TUI for Carlos's four roles.
Data is persisted to ~/.workload_tracker.json

Keyboard shortcuts:
  n        — New task
  e        — Edit selected task
  d        — Delete selected task
  t        — Toggle timer on selected task
  l        — Log time manually on selected task
  s        — Cycle status of selected task
  1-4      — Filter by role (1=DemoKit, 2=Demos, 3=Strategic, 4=Other, 0=All)
  tab      — Switch between Task board / Overview panels
  ↑↓       — Navigate tasks
  q / esc  — Quit / close modal
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.screen import ModalScreen
from textual.widgets import (
    Button, DataTable, Footer, Header, Input, Label,
    Select, Static, TabbedContent, TabPane, TextArea
)
from textual.reactive import reactive

from idle_detector import get_idle_seconds

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
    #modal-actions { margin-top: 1; }
    """

    def __init__(self, task_data: Optional[dict] = None, roles: Optional[list] = None):
        super().__init__()
        self._task_data = task_data
        self._roles = roles or []

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
            with Horizontal(id="modal-actions"):
                yield Button("Save  [s]", variant="primary", id="btn-save")
                yield Button("Cancel  [esc]", id="btn-cancel")

    def on_mount(self):
        self.query_one("#inp-title").focus()

    def on_key(self, event):
        if event.key == "escape":
            self.dismiss(None)
        elif event.key == "s" and not isinstance(self.focused, Input):
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
        elif event.key == "y":
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
        Binding("l",   "log_time",    "Log time"),
        Binding("s",   "cycle_status","Cycle status"),
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
            # Notes indicator: # for GitHub issue, + for local notes
            if task.get("github_issue"):
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
        elapsed_seconds = time.time() - at["started_at"]
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
                "note": note, "at": time.time()
            })

        self._data["active_timer"] = None
        save_data(self._data)

        # Notify user
        idle_mins = int(idle_seconds / 60)
        self.notify(
            f"Timer stopped: {idle_mins}m idle. Logged {fmt_mins(logged_minutes)} to '{task['title'][:20]}'",
            severity="warning",
            timeout=10
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

    def action_new_task(self):
        roles = get_roles(self._data)
        self.push_screen(TaskModal(roles=roles), self._on_task_saved)

    def action_edit_task(self):
        task = self._selected_task()
        if task:
            roles = get_roles(self._data)
            self.push_screen(TaskModal(task_data=task, roles=roles), self._on_task_saved)

    def _on_task_saved(self, result: Optional[dict]):
        if not result:
            return
        tasks = self._data["tasks"]
        existing = next((i for i, t in enumerate(tasks) if t["id"] == result["id"]), None)
        if existing is not None:
            tasks[existing] = result
        else:
            tasks.insert(0, result)
        save_data(self._data)
        self._populate_table()
        self._refresh_sidebar()
        self._refresh_overview()

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
            elapsed = (time.time() - at["started_at"]) / 60
            if elapsed > 0.1:
                task.setdefault("logs", []).append({
                    "id": uid(), "minutes": round(elapsed, 2),
                    "note": "Timer session", "at": time.time()
                })
            self._data["active_timer"] = None
            save_data(self._data)

            # Arc integration: tab cleanup
            self._arc_tab_cleanup(task)
        else:
            # Stop any running timer first
            stopped_task = None
            if at:
                prev = next((t for t in self._data["tasks"] if t["id"] == at["task_id"]), None)
                if prev:
                    elapsed = (time.time() - at["started_at"]) / 60
                    if elapsed > 0.1:
                        prev.setdefault("logs", []).append({
                            "id": uid(), "minutes": round(elapsed, 2),
                            "note": "Timer session", "at": time.time()
                        })
                    stopped_task = prev
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
        """Arc integration: show tab cleanup modal if enabled."""
        if not self._data.get("config", {}).get("tab_cleanup_enabled"):
            return
        try:
            from arc_browser import TaskTabManager
            manager = TaskTabManager(self._data)

            # Get tabs and classify
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
                self.push_screen(
                    TabCleanupModal(unrelated_data, task["title"]),
                    lambda tabs_to_close: self._on_tabs_cleanup(tabs_to_close, manager)
                )
        except ImportError:
            pass

    def _on_tabs_cleanup(self, tabs_to_close: list, manager):
        """Callback when user selects tabs to close."""
        if not tabs_to_close:
            return
        for _ in tabs_to_close:
            manager.applescript.close_current_tab()
            time.sleep(0.1)

    def action_log_time(self):
        task = self._selected_task()
        if not task:
            return
        self.push_screen(LogTimeModal(task_data=task), lambda log: self._on_log_saved(task["id"], log))

    def _on_log_saved(self, task_id: str, log: Optional[dict]):
        if not log:
            return
        task = next((t for t in self._data["tasks"] if t["id"] == task_id), None)
        if task:
            task.setdefault("logs", []).append(log)
            save_data(self._data)
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
        task["status"] = new_status
        save_data(self._data)
        self._populate_table()

        # Arc integration: cleanup folder when marked done
        if new_status == "done" and old_status != "done":
            self._arc_on_task_completed(task)

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


if __name__ == "__main__":
    app = WorkloadTracker()
    app.run()
