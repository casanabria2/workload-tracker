#!/usr/bin/env python3
"""
iTerm2 and tmux integration for Workload Tracker.

Manages terminal sessions for tasks:
- Creates task folders organized by role
- Spawns tmux sessions with 3-pane layout
- Opens iTerm2 windows attached to tmux sessions
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Optional


def slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    # Lowercase and replace spaces/special chars with hyphens
    slug = text.lower()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[\s_]+', '-', slug)
    slug = re.sub(r'-+', '-', slug)
    return slug.strip('-')


class TmuxManager:
    """Manages tmux sessions for tasks."""

    def session_exists(self, name: str) -> bool:
        """Check if a tmux session exists."""
        result = subprocess.run(
            ["tmux", "has-session", "-t", name],
            capture_output=True
        )
        return result.returncode == 0

    def list_sessions(self) -> list[str]:
        """List all tmux sessions starting with 'wt-'."""
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return []
        return [s for s in result.stdout.strip().split('\n') if s.startswith('wt-')]

    def create_session(self, name: str, folder: Path) -> bool:
        """Create a tmux session with 3-pane layout.

        Layout:
        ┌────────────────┬────────────────┐
        │   Pane 0.0     │   Pane 0.1     │  ← 2/3 height
        │  (top-left)    │  (top-right)   │
        ├────────────────┴────────────────┤
        │         Pane 0.2                │  ← 1/3 height
        │        (bottom)                 │
        └─────────────────────────────────┘

        Returns True if successful.
        """
        folder_str = str(folder)

        # Create session with first pane (full screen)
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", name, "-c", folder_str],
            capture_output=True
        )
        if result.returncode != 0:
            return False

        # Split vertically to create bottom pane (0.0=top, 0.1=bottom)
        subprocess.run(
            ["tmux", "split-window", "-v", "-t", f"{name}:0.0", "-c", folder_str],
            capture_output=True
        )

        # Resize bottom pane to 33%
        subprocess.run(
            ["tmux", "resize-pane", "-t", f"{name}:0.1", "-y", "33%"],
            capture_output=True
        )

        # Split top pane horizontally (0.0=top-left, 0.1=top-right, 0.2=bottom)
        subprocess.run(
            ["tmux", "split-window", "-h", "-t", f"{name}:0.0", "-c", folder_str],
            capture_output=True
        )

        # Select top-left pane
        subprocess.run(
            ["tmux", "select-pane", "-t", f"{name}:0.0"],
            capture_output=True
        )

        return True

    def kill_session(self, name: str) -> bool:
        """Kill a tmux session."""
        result = subprocess.run(
            ["tmux", "kill-session", "-t", name],
            capture_output=True
        )
        return result.returncode == 0


class ItermAppleScript:
    """Controls iTerm2 via AppleScript."""

    def _run_applescript(self, script: str) -> tuple[bool, str]:
        """Run an AppleScript and return (success, output)."""
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True
        )
        return result.returncode == 0, result.stdout.strip()

    def is_iterm_running(self) -> bool:
        """Check if iTerm2 is running."""
        script = '''
        tell application "System Events"
            return (name of processes) contains "iTerm2"
        end tell
        '''
        success, output = self._run_applescript(script)
        return success and output == "true"

    def create_window_with_tmux(self, session_name: str, folder: Path, title: str = None) -> bool:
        """Open a new iTerm2 window and attach to tmux session.

        Returns True if successful.
        """
        folder_str = str(folder)
        window_title = title or session_name
        # Escape double quotes and backslashes for AppleScript
        safe_title = window_title.replace('\\', '\\\\').replace('"', '\\"')
        script = f'''
        tell application "iTerm2"
            activate
            set newWindow to (create window with default profile)
            tell current tab of newWindow
                set currentSession to current session
                tell currentSession
                    set name to "{safe_title}"
                    write text "cd '{folder_str}' && tmux attach-session -t {session_name}"
                end tell
            end tell
        end tell
        return "ok"
        '''
        success, output = self._run_applescript(script)
        if success and "ok" in output:
            # Position window using Hammerspoon (1920x1080 on left screen)
            self._position_window_with_hammerspoon()
            return True
        return False

    def _position_window_with_hammerspoon(self):
        """Position iTerm2 window to 1920x1080 on the left screen using Hammerspoon."""
        # Find the leftmost screen and position window there
        hs_command = '''
            local win = hs.application.get("iTerm2"):focusedWindow()
            if win then
                local screens = hs.screen.allScreens()
                table.sort(screens, function(a, b) return a:frame().x < b:frame().x end)
                local leftScreen = screens[1]
                local frame = leftScreen:frame()
                win:setFrame({x=frame.x, y=frame.y, w=1920, h=1080})
            end
        '''
        subprocess.run(["hs", "-c", hs_command], capture_output=True)

    def focus_window_with_session(self, session_name: str) -> bool:
        """Try to focus an existing iTerm window running the tmux session.

        Returns True if a window was found and focused.
        """
        # This searches iTerm windows for one running the tmux session
        script = f'''
        tell application "iTerm2"
            activate
            repeat with w in windows
                repeat with t in tabs of w
                    repeat with s in sessions of t
                        if name of s contains "{session_name}" then
                            select w
                            return "found"
                        end if
                    end repeat
                end repeat
            end repeat
        end tell
        return "not_found"
        '''
        success, output = self._run_applescript(script)
        return success and output == "found"


class TaskTerminalManager:
    """Main orchestrator for task terminal management."""

    DEFAULT_PROJECTS_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/WorkloadTracker"
    SYMLINK_PATH = Path.home() / "WorkloadTracker"

    def __init__(self, data: dict):
        """Initialize with workload tracker data."""
        self.data = data
        self.tmux = TmuxManager()
        self.iterm = ItermAppleScript()

    def is_enabled(self) -> bool:
        """Check if iTerm integration is enabled."""
        return self.data.get("config", {}).get("iterm_enabled", False)

    def get_projects_dir(self) -> Path:
        """Get the base projects directory."""
        config = self.data.get("config", {})
        dir_str = config.get("iterm_projects_dir")
        if dir_str:
            return Path(dir_str).expanduser()
        return self.DEFAULT_PROJECTS_DIR

    def get_terminal_path(self, folder: Path) -> Path:
        """Convert folder path to use symlink for shorter terminal prompts.

        If the folder is under the iCloud path and the symlink exists,
        returns the equivalent path using the symlink.
        """
        if not self.SYMLINK_PATH.exists():
            return folder

        try:
            # Check if folder is under the default iCloud path
            rel_path = folder.relative_to(self.DEFAULT_PROJECTS_DIR)
            return self.SYMLINK_PATH / rel_path
        except ValueError:
            # Not under the iCloud path, return as-is
            return folder

    def generate_session_name(self, task: dict) -> str:
        """Generate tmux session name for a task.

        Format: wt-{role}-{title_slug}
        """
        role_id = task.get("role_id", "other")
        title_slug = slugify(task.get("title", "task"))[:30]  # Limit length
        return f"wt-{role_id}-{title_slug}"

    def get_task_folder(self, task: dict) -> Path:
        """Get the folder path for a task.

        Structure: {base}/{role_id}/{title_slug}/
        """
        role_id = task.get("role_id", "other")
        title_slug = slugify(task.get("title", "task"))
        return self.get_projects_dir() / role_id / title_slug

    def ensure_task_folder(self, task: dict, save_callback) -> Path:
        """Create task folder if needed and store path in task.

        Returns the folder path.
        """
        # Check if task already has a folder
        existing = task.get("task_folder_path")
        if existing:
            folder = Path(existing)
            if folder.exists():
                return folder

        # Create new folder
        folder = self.get_task_folder(task)
        folder.mkdir(parents=True, exist_ok=True)

        # Store in task
        task["task_folder_path"] = str(folder)
        save_callback(self.data)

        return folder

    def open_terminal(self, task: dict, save_callback) -> dict:
        """Open iTerm2 terminal for task, creating folder and tmux session as needed.

        Returns dict with results.
        """
        results = {
            "folder_created": False,
            "session_created": False,
            "window_opened": False,
            "session_name": None,
            "folder_path": None,
            "error": None
        }

        try:
            # Ensure folder exists (using full iCloud path internally)
            folder = self.ensure_task_folder(task, save_callback)
            results["folder_path"] = str(folder)
            results["folder_created"] = not task.get("task_folder_path")

            # Use symlink path for terminal (shorter prompt)
            terminal_folder = self.get_terminal_path(folder)

            # Get or generate session name
            session_name = task.get("iterm_session_name")
            if not session_name:
                session_name = self.generate_session_name(task)
                task["iterm_session_name"] = session_name
                save_callback(self.data)

            results["session_name"] = session_name

            # Check if session exists
            if self.tmux.session_exists(session_name):
                # Try to focus existing window
                if self.iterm.focus_window_with_session(session_name):
                    results["window_opened"] = True
                else:
                    # Session exists but no window - open new window
                    if self.iterm.create_window_with_tmux(session_name, terminal_folder, task.get("title")):
                        results["window_opened"] = True
            else:
                # Create new session
                if self.tmux.create_session(session_name, terminal_folder):
                    results["session_created"] = True
                    # Open iTerm window
                    if self.iterm.create_window_with_tmux(session_name, terminal_folder, task.get("title")):
                        results["window_opened"] = True
                else:
                    results["error"] = "Failed to create tmux session"

        except Exception as e:
            results["error"] = str(e)

        return results

    def close_session(self, task: dict) -> dict:
        """Close tmux session for a task.

        Returns dict with results.
        """
        results = {
            "session_closed": False,
            "error": None
        }

        session_name = task.get("iterm_session_name")
        if not session_name:
            results["error"] = "No session associated with task"
            return results

        if not self.tmux.session_exists(session_name):
            results["error"] = "Session does not exist"
            return results

        if self.tmux.kill_session(session_name):
            results["session_closed"] = True
        else:
            results["error"] = "Failed to kill session"

        return results

    def get_status(self) -> dict:
        """Get iTerm integration status."""
        config = self.data.get("config", {})
        projects_dir = self.get_projects_dir()

        # Count tasks with terminals
        tasks_with_sessions = sum(
            1 for t in self.data.get("tasks", [])
            if t.get("iterm_session_name")
        )

        # Count active sessions
        active_sessions = self.tmux.list_sessions()

        return {
            "enabled": config.get("iterm_enabled", False),
            "projects_dir": str(projects_dir),
            "projects_dir_exists": projects_dir.exists(),
            "iterm_running": self.iterm.is_iterm_running(),
            "tasks_with_sessions": tasks_with_sessions,
            "active_sessions": len(active_sessions),
            "session_names": active_sessions
        }

    def setup(self, save_callback, projects_dir: Optional[str] = None) -> dict:
        """Enable iTerm integration and set up projects directory.

        Returns dict with setup results.
        """
        results = {
            "enabled": False,
            "projects_dir": None,
            "created_dir": False,
            "error": None
        }

        try:
            config = self.data.setdefault("config", {})
            config["iterm_enabled"] = True

            if projects_dir:
                config["iterm_projects_dir"] = projects_dir

            # Ensure projects directory exists
            dir_path = self.get_projects_dir()
            if not dir_path.exists():
                dir_path.mkdir(parents=True, exist_ok=True)
                results["created_dir"] = True

            results["projects_dir"] = str(dir_path)
            results["enabled"] = True
            save_callback(self.data)

        except Exception as e:
            results["error"] = str(e)

        return results
