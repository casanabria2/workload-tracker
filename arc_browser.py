#!/usr/bin/env python3
"""
Arc Browser integration for Workload Tracker.

Manages tabs within a dedicated "Workload Tracker" space in Arc Browser:
- Creates folder hierarchy: Space > Role folders > Task folders
- Tracks tabs associated with tasks
- Classifies tabs using Claude API when switching tasks
- Archives tab URLs when tasks are completed

Hybrid approach:
- AppleScript: Get tabs, open tabs, close tabs (no restart required)
- JSON manipulation: Create/delete spaces/folders, move tabs (restart required)
"""

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Arc's sidebar data location
ARC_SIDEBAR_PATH = Path.home() / "Library/Application Support/Arc/StorableSidebar.json"

# Space and folder constants
WORKLOAD_TRACKER_SPACE_NAME = "Workload Tracker"


@dataclass
class Tab:
    """Represents an Arc browser tab."""
    id: str
    url: str
    title: str
    space_name: Optional[str] = None
    folder_id: Optional[str] = None


@dataclass
class TabClassification:
    """Result of classifying a tab's relevance to a task."""
    tab: Tab
    is_related: bool
    confidence: float
    reason: str


class ArcSidebarManager:
    """Manages Arc's StorableSidebar.json for creating spaces and folders.

    Note: Changes to StorableSidebar.json require Arc to be quit and restarted.
    """

    def __init__(self, sidebar_path: Path = ARC_SIDEBAR_PATH):
        self.sidebar_path = sidebar_path
        self._backup_dir = Path.home() / ".workload_tracker_arc_backups"

    def load_sidebar(self) -> dict:
        """Load and parse StorableSidebar.json."""
        if not self.sidebar_path.exists():
            raise FileNotFoundError(f"Arc sidebar not found at {self.sidebar_path}")
        return json.loads(self.sidebar_path.read_text())

    def save_sidebar(self, data: dict):
        """Backup and save StorableSidebar.json.

        Warning: Arc must be quit before saving, or changes will be overwritten.
        """
        # Create backup
        self._backup_dir.mkdir(exist_ok=True)
        backup_path = self._backup_dir / f"StorableSidebar_{int(time.time())}.json"
        if self.sidebar_path.exists():
            shutil.copy2(self.sidebar_path, backup_path)

        # Keep only last 10 backups
        backups = sorted(self._backup_dir.glob("StorableSidebar_*.json"))
        for old_backup in backups[:-10]:
            old_backup.unlink()

        # Save
        self.sidebar_path.write_text(json.dumps(data, indent=2))

    def _find_container_with_spaces(self, data: dict) -> Optional[dict]:
        """Find the container that holds spaces (usually the first one)."""
        containers = data.get("sidebar", {}).get("containers", [])
        for container in containers:
            if "spaces" in container:
                return container
        return None

    def _generate_uuid(self) -> str:
        """Generate a UUID for Arc items."""
        return str(uuid.uuid4()).upper()

    def get_workload_tracker_space(self) -> Optional[dict]:
        """Find the Workload Tracker space if it exists."""
        data = self.load_sidebar()
        container = self._find_container_with_spaces(data)
        if not container:
            return None

        for space in container.get("spaces", []):
            if isinstance(space, dict) and space.get("title") == WORKLOAD_TRACKER_SPACE_NAME:
                return space
        return None

    def ensure_workload_tracker_space(self) -> str:
        """Create 'Workload Tracker' space if it doesn't exist.

        Returns: Space ID (new or existing).
        """
        data = self.load_sidebar()
        container = self._find_container_with_spaces(data)
        if not container:
            raise RuntimeError("Could not find Arc container with spaces")

        # Check if space already exists
        for space in container.get("spaces", []):
            if isinstance(space, dict) and space.get("title") == WORKLOAD_TRACKER_SPACE_NAME:
                return space["id"]

        # Create new space
        space_id = self._generate_uuid()
        new_space = {
            "id": space_id,
            "title": WORKLOAD_TRACKER_SPACE_NAME,
            "customInfo": {
                "iconType": "emoji",
                "emoji": "📋"
            }
        }
        container["spaces"].append(new_space)

        # Initialize items array if needed
        if "items" not in container:
            container["items"] = []

        self.save_sidebar(data)
        return space_id

    def get_space_id(self) -> Optional[str]:
        """Get the Workload Tracker space ID if it exists."""
        space = self.get_workload_tracker_space()
        return space["id"] if space else None

    def create_role_folder(self, role_id: str, role_label: str, space_id: str) -> str:
        """Create a folder for a role in the Workload Tracker space.

        Returns: Folder ID.
        """
        data = self.load_sidebar()
        container = self._find_container_with_spaces(data)
        if not container:
            raise RuntimeError("Could not find Arc container")

        items = container.setdefault("items", [])

        # Check if folder already exists
        for item in items:
            if (isinstance(item, dict) and
                item.get("title") == role_label and
                item.get("parentID") == space_id and
                "list" in item.get("data", {})):
                return item["id"]

        # Create new folder
        folder_id = self._generate_uuid()
        new_folder = {
            "id": folder_id,
            "title": role_label,
            "data": {"list": {"isOpen": True}},
            "childrenIds": [],
            "parentID": space_id
        }
        items.append(new_folder)

        self.save_sidebar(data)
        return folder_id

    def create_task_folder(self, task_id: str, task_title: str, role_folder_id: str) -> str:
        """Create a subfolder for a task under a role folder.

        Returns: Task folder ID.
        """
        data = self.load_sidebar()
        container = self._find_container_with_spaces(data)
        if not container:
            raise RuntimeError("Could not find Arc container")

        items = container.setdefault("items", [])

        # Find parent folder to update childrenIds
        parent_folder = None
        for item in items:
            if isinstance(item, dict) and item.get("id") == role_folder_id:
                parent_folder = item
                break

        # Create task folder
        folder_id = self._generate_uuid()
        new_folder = {
            "id": folder_id,
            "title": task_title,
            "data": {"list": {"isOpen": True}},
            "childrenIds": [],
            "parentID": role_folder_id,
            # Store task_id in custom data for reference
            "_workload_task_id": task_id
        }
        items.append(new_folder)

        # Update parent's childrenIds
        if parent_folder:
            parent_folder.setdefault("childrenIds", []).append(folder_id)

        self.save_sidebar(data)
        return folder_id

    def get_folder_by_task_id(self, task_id: str) -> Optional[dict]:
        """Find a folder by its associated task ID."""
        data = self.load_sidebar()
        container = self._find_container_with_spaces(data)
        if not container:
            return None

        for item in container.get("items", []):
            if isinstance(item, dict) and item.get("_workload_task_id") == task_id:
                return item
        return None

    def delete_task_folder(self, folder_id: str) -> list[dict]:
        """Delete a task folder and return its tabs for archiving.

        Returns: List of tab data that was in the folder.
        """
        data = self.load_sidebar()
        container = self._find_container_with_spaces(data)
        if not container:
            return []

        items = container.get("items", [])
        tabs_in_folder = []
        folder_to_delete = None
        parent_id = None

        # Find the folder and collect its tabs
        for item in items:
            if isinstance(item, dict) and item.get("id") == folder_id:
                folder_to_delete = item
                parent_id = item.get("parentID")
                break

        if not folder_to_delete:
            return []

        # Collect tabs (items with "tab" in data and parentID matching folder)
        for item in items:
            if (isinstance(item, dict) and
                item.get("parentID") == folder_id and
                "tab" in item.get("data", {})):
                tab_data = item["data"]["tab"]
                tabs_in_folder.append({
                    "url": tab_data.get("savedURL", ""),
                    "title": tab_data.get("savedTitle", item.get("title", "")),
                })

        # Remove folder and its children from items
        children_to_remove = set([folder_id])
        children_to_remove.update(folder_to_delete.get("childrenIds", []))
        container["items"] = [
            item for item in items
            if not isinstance(item, dict) or item.get("id") not in children_to_remove
        ]

        # Update parent's childrenIds
        for item in container["items"]:
            if isinstance(item, dict) and item.get("id") == parent_id:
                children = item.get("childrenIds", [])
                if folder_id in children:
                    children.remove(folder_id)
                break

        self.save_sidebar(data)
        return tabs_in_folder

    def get_tabs_in_folder(self, folder_id: str) -> list[dict]:
        """Get all tabs in a folder."""
        data = self.load_sidebar()
        container = self._find_container_with_spaces(data)
        if not container:
            return []

        tabs = []
        for item in container.get("items", []):
            if (isinstance(item, dict) and
                item.get("parentID") == folder_id and
                "tab" in item.get("data", {})):
                tab_data = item["data"]["tab"]
                tabs.append({
                    "id": item["id"],
                    "url": tab_data.get("savedURL", ""),
                    "title": tab_data.get("savedTitle", item.get("title", "")),
                })
        return tabs

    def get_role_folders(self, space_id: str) -> list[dict]:
        """Get all role folders in the Workload Tracker space."""
        data = self.load_sidebar()
        container = self._find_container_with_spaces(data)
        if not container:
            return []

        folders = []
        for item in container.get("items", []):
            if (isinstance(item, dict) and
                item.get("parentID") == space_id and
                "list" in item.get("data", {})):
                folders.append({
                    "id": item["id"],
                    "title": item.get("title", ""),
                })
        return folders


class ArcAppleScript:
    """AppleScript operations for Arc Browser.

    These operations don't require restarting Arc.
    """

    def _run_applescript(self, script: str) -> tuple[bool, str]:
        """Run an AppleScript and return (success, output)."""
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True
        )
        return result.returncode == 0, result.stdout.strip()

    def is_arc_running(self) -> bool:
        """Check if Arc is running."""
        script = '''
        tell application "System Events"
            return (name of processes) contains "Arc"
        end tell
        '''
        success, output = self._run_applescript(script)
        return success and output == "true"

    def quit_arc(self) -> bool:
        """Quit Arc gracefully."""
        script = '''
        tell application "Arc"
            quit
        end tell
        '''
        success, _ = self._run_applescript(script)
        return success

    def launch_arc(self) -> bool:
        """Launch Arc."""
        script = '''
        tell application "Arc"
            activate
        end tell
        '''
        success, _ = self._run_applescript(script)
        return success

    def get_all_tabs(self) -> list[Tab]:
        """Get all open tabs from the front window."""
        script = '''
        tell application "Arc"
            set tabList to {}
            try
                tell front window
                    repeat with t in tabs
                        set tabURL to URL of t
                        set tabTitle to title of t
                        set tabId to id of t
                        set end of tabList to tabId & "|||" & tabURL & "|||" & tabTitle
                    end repeat
                end tell
            end try
            return tabList
        end tell
        '''
        success, output = self._run_applescript(script)
        if not success or not output:
            return []

        tabs = []
        for line in output.split(", "):
            parts = line.strip().split("|||")
            if len(parts) >= 3:
                tabs.append(Tab(
                    id=parts[0],
                    url=parts[1],
                    title=parts[2]
                ))
        return tabs

    def get_active_tab(self) -> Optional[Tab]:
        """Get the currently active tab."""
        script = '''
        tell application "Arc"
            try
                tell front window
                    set t to active tab
                    return (id of t) & "|||" & (URL of t) & "|||" & (title of t)
                end tell
            end try
        end tell
        '''
        success, output = self._run_applescript(script)
        if not success or not output:
            return None

        parts = output.split("|||")
        if len(parts) >= 3:
            return Tab(id=parts[0], url=parts[1], title=parts[2])
        return None

    def open_url(self, url: str) -> bool:
        """Open a URL in Arc."""
        script = f'''
        tell application "Arc"
            activate
            tell front window
                make new tab with properties {{URL:"{url}"}}
            end tell
        end tell
        '''
        success, _ = self._run_applescript(script)
        return success

    def open_urls(self, urls: list[str]) -> int:
        """Open multiple URLs in Arc. Returns count of successfully opened tabs."""
        opened = 0
        for url in urls:
            if self.open_url(url):
                opened += 1
        return opened

    def close_current_tab(self) -> bool:
        """Close the current tab using keyboard shortcut."""
        script = '''
        tell application "System Events"
            tell process "Arc"
                keystroke "w" using command down
            end tell
        end tell
        '''
        success, _ = self._run_applescript(script)
        return success

    def focus_space_by_name(self, space_name: str) -> bool:
        """Try to focus a space by name.

        Note: Arc's AppleScript support for spaces is limited.
        This uses keyboard navigation which may not be reliable.
        """
        # Arc doesn't have great AppleScript support for spaces
        # We'll try using the menu
        script = f'''
        tell application "Arc"
            activate
        end tell
        tell application "System Events"
            tell process "Arc"
                -- Try to access space via menu
                try
                    click menu item "{space_name}" of menu "Spaces" of menu bar 1
                    return "ok"
                end try
            end tell
        end tell
        return "failed"
        '''
        success, output = self._run_applescript(script)
        return success and output == "ok"


class TabClassifier:
    """Uses Claude API to classify which tabs are related to a task."""

    def __init__(self, confidence_threshold: float = 0.7):
        self.confidence_threshold = confidence_threshold
        self._client = None

    def _get_client(self):
        """Lazy-load the Anthropic client."""
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.Anthropic()
            except ImportError:
                raise ImportError("anthropic package required for tab classification. Install with: pip install anthropic")
        return self._client

    def classify_tabs(self, tabs: list[Tab], task: dict) -> list[TabClassification]:
        """Classify which tabs are related to the given task.

        Args:
            tabs: List of tabs to classify
            task: Task dict with title, description, etc.

        Returns:
            List of TabClassification results
        """
        if not tabs:
            return []

        client = self._get_client()

        # Build prompt
        task_context = f"Task: {task.get('title', 'Unknown')}"
        if task.get("description"):
            task_context += f"\nDescription: {task['description']}"
        if task.get("github_issue"):
            task_context += f"\nGitHub Issue: {task['github_issue']}"

        tabs_text = "\n".join([
            f"{i+1}. [{tab.title}] {tab.url}"
            for i, tab in enumerate(tabs)
        ])

        prompt = f"""Analyze these browser tabs and determine which are likely related to the given task.

{task_context}

Tabs:
{tabs_text}

For each tab, respond with a JSON array containing objects with:
- "index": tab number (1-based)
- "is_related": true/false
- "confidence": 0.0-1.0
- "reason": brief explanation

Consider:
- Documentation pages related to technologies in the task
- GitHub issues/PRs/code related to the task
- Stack Overflow or similar Q&A about task-related topics
- Generic tabs (email, social media, unrelated docs) are NOT related

Respond ONLY with the JSON array, no other text."""

        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}]
            )

            # Parse response
            response_text = response.content[0].text.strip()
            # Handle potential markdown code blocks
            if response_text.startswith("```"):
                response_text = re.sub(r"^```(?:json)?\n?", "", response_text)
                response_text = re.sub(r"\n?```$", "", response_text)

            classifications_data = json.loads(response_text)

            results = []
            for item in classifications_data:
                idx = item.get("index", 0) - 1
                if 0 <= idx < len(tabs):
                    results.append(TabClassification(
                        tab=tabs[idx],
                        is_related=item.get("is_related", True),
                        confidence=item.get("confidence", 0.5),
                        reason=item.get("reason", "")
                    ))

            return results

        except Exception as e:
            # On error, mark all tabs as related (safe default)
            return [
                TabClassification(
                    tab=tab,
                    is_related=True,
                    confidence=0.0,
                    reason=f"Classification failed: {e}"
                )
                for tab in tabs
            ]

    def get_unrelated_tabs(self, classifications: list[TabClassification]) -> list[TabClassification]:
        """Filter to tabs that are likely unrelated to the task."""
        return [
            c for c in classifications
            if not c.is_related and c.confidence >= self.confidence_threshold
        ]


class TaskTabManager:
    """Orchestrates tab management for task workflows."""

    def __init__(self, data: dict):
        """Initialize with workload tracker data."""
        self.data = data
        self.sidebar = ArcSidebarManager()
        self.applescript = ArcAppleScript()
        self.classifier = TabClassifier(
            confidence_threshold=data.get("config", {}).get("tab_confidence_threshold", 0.7)
        )

    def is_arc_integration_enabled(self) -> bool:
        """Check if Arc integration is configured."""
        return bool(self.data.get("config", {}).get("arc_space_id"))

    def setup_space_and_folders(self, save_callback) -> dict:
        """Set up the Workload Tracker space with role folders.

        Args:
            save_callback: Function to save the data dict

        Returns:
            Dict with setup results
        """
        results = {
            "space_id": None,
            "role_folders": {},
            "restart_required": False,
            "errors": []
        }

        try:
            # Create space
            space_id = self.sidebar.ensure_workload_tracker_space()
            results["space_id"] = space_id
            results["restart_required"] = True

            # Store in config
            config = self.data.setdefault("config", {})
            config["arc_space_id"] = space_id

            # Create role folders
            for role in self.data.get("roles", []):
                try:
                    folder_id = self.sidebar.create_role_folder(
                        role["id"],
                        role["label"],
                        space_id
                    )
                    results["role_folders"][role["id"]] = folder_id

                    # Store mapping
                    role["arc_folder_id"] = folder_id
                except Exception as e:
                    results["errors"].append(f"Failed to create folder for {role['id']}: {e}")

            save_callback(self.data)

        except Exception as e:
            results["errors"].append(str(e))

        return results

    def get_role_folder_id(self, role_id: str) -> Optional[str]:
        """Get the Arc folder ID for a role."""
        for role in self.data.get("roles", []):
            if role["id"] == role_id and "arc_folder_id" in role:
                return role["arc_folder_id"]
        return None

    def on_task_created(self, task: dict, save_callback) -> dict:
        """Create a folder for a new task.

        Returns:
            Dict with creation results
        """
        results = {
            "folder_id": None,
            "restart_required": False,
            "error": None
        }

        if not self.is_arc_integration_enabled():
            return results

        role_folder_id = self.get_role_folder_id(task.get("role_id", "other"))
        if not role_folder_id:
            results["error"] = f"No Arc folder for role {task.get('role_id')}"
            return results

        try:
            folder_id = self.sidebar.create_task_folder(
                task["id"],
                task["title"],
                role_folder_id
            )
            task["arc_folder_id"] = folder_id
            results["folder_id"] = folder_id
            results["restart_required"] = True
            save_callback(self.data)
        except Exception as e:
            results["error"] = str(e)

        return results

    def on_task_started(self, task: dict) -> dict:
        """Focus the task's folder/space when task starts.

        Returns:
            Dict with focus results
        """
        results = {
            "focused": False,
            "error": None
        }

        if not self.is_arc_integration_enabled():
            return results

        # Try to focus the Workload Tracker space
        if self.applescript.focus_space_by_name(WORKLOAD_TRACKER_SPACE_NAME):
            results["focused"] = True

        return results

    def on_task_stopped(self, task: dict, prompt_callback=None) -> dict:
        """Handle tab cleanup when task timer is stopped.

        Args:
            task: The task that was stopped
            prompt_callback: Optional function(unrelated_tabs) -> list[tabs_to_close]
                            If None, no tabs are closed.

        Returns:
            Dict with cleanup results
        """
        results = {
            "tabs_classified": 0,
            "unrelated_tabs": [],
            "tabs_closed": 0,
            "error": None
        }

        if not self.data.get("config", {}).get("tab_cleanup_enabled"):
            return results

        try:
            # Get current tabs
            tabs = self.applescript.get_all_tabs()
            if not tabs:
                return results

            # Classify tabs
            classifications = self.classifier.classify_tabs(tabs, task)
            results["tabs_classified"] = len(classifications)

            # Get unrelated tabs
            unrelated = self.classifier.get_unrelated_tabs(classifications)
            results["unrelated_tabs"] = [
                {"url": c.tab.url, "title": c.tab.title, "reason": c.reason}
                for c in unrelated
            ]

            # If callback provided, let user choose which to close
            if prompt_callback and unrelated:
                tabs_to_close = prompt_callback(unrelated)
                for _ in tabs_to_close:
                    if self.applescript.close_current_tab():
                        results["tabs_closed"] += 1
                        time.sleep(0.1)  # Brief pause between closes

        except Exception as e:
            results["error"] = str(e)

        return results

    def on_task_completed(self, task: dict, save_callback) -> dict:
        """Archive tabs and remove folder when task is completed.

        Returns:
            Dict with completion results
        """
        results = {
            "tabs_archived": 0,
            "folder_deleted": False,
            "restart_required": False,
            "error": None
        }

        folder_id = task.get("arc_folder_id")
        if not folder_id:
            return results

        try:
            # Get and archive tabs
            tabs = self.sidebar.get_tabs_in_folder(folder_id)
            if tabs:
                task.setdefault("archived_tabs", [])
                for tab in tabs:
                    task["archived_tabs"].append({
                        "url": tab["url"],
                        "title": tab["title"],
                        "archived_at": time.time()
                    })
                results["tabs_archived"] = len(tabs)

            # Delete folder
            self.sidebar.delete_task_folder(folder_id)
            del task["arc_folder_id"]
            results["folder_deleted"] = True
            results["restart_required"] = True

            save_callback(self.data)

        except Exception as e:
            results["error"] = str(e)

        return results

    def sync_folders(self, save_callback) -> dict:
        """Sync Arc folders with current roles and tasks.

        Returns:
            Dict with sync results
        """
        results = {
            "roles_synced": 0,
            "tasks_synced": 0,
            "restart_required": False,
            "errors": []
        }

        space_id = self.data.get("config", {}).get("arc_space_id")
        if not space_id:
            results["errors"].append("Arc space not set up. Run 'wt arc setup' first.")
            return results

        # Sync role folders
        for role in self.data.get("roles", []):
            if "arc_folder_id" not in role:
                try:
                    folder_id = self.sidebar.create_role_folder(
                        role["id"],
                        role["label"],
                        space_id
                    )
                    role["arc_folder_id"] = folder_id
                    results["roles_synced"] += 1
                    results["restart_required"] = True
                except Exception as e:
                    results["errors"].append(f"Role {role['id']}: {e}")

        # Sync task folders for non-done tasks
        for task in self.data.get("tasks", []):
            if task.get("status") == "done":
                continue
            if "arc_folder_id" not in task:
                role_folder_id = self.get_role_folder_id(task.get("role_id", "other"))
                if role_folder_id:
                    try:
                        folder_id = self.sidebar.create_task_folder(
                            task["id"],
                            task["title"],
                            role_folder_id
                        )
                        task["arc_folder_id"] = folder_id
                        results["tasks_synced"] += 1
                        results["restart_required"] = True
                    except Exception as e:
                        results["errors"].append(f"Task {task['id']}: {e}")

        save_callback(self.data)
        return results

    def get_status(self) -> dict:
        """Get Arc integration status."""
        config = self.data.get("config", {})
        space_id = config.get("arc_space_id")

        status = {
            "enabled": bool(space_id),
            "space_id": space_id,
            "tab_cleanup_enabled": config.get("tab_cleanup_enabled", False),
            "confidence_threshold": config.get("tab_confidence_threshold", 0.7),
            "arc_running": self.applescript.is_arc_running(),
            "role_folders": sum(1 for r in self.data.get("roles", []) if "arc_folder_id" in r),
            "task_folders": sum(1 for t in self.data.get("tasks", []) if "arc_folder_id" in t),
        }
        return status

    def restore_archived_tabs(self, task: dict) -> int:
        """Restore archived tabs for a task.

        Returns:
            Number of tabs opened
        """
        archived = task.get("archived_tabs", [])
        if not archived:
            return 0

        urls = [tab["url"] for tab in archived]
        return self.applescript.open_urls(urls)


def prompt_arc_restart() -> bool:
    """Prompt user to restart Arc.

    Returns:
        True if user confirmed restart
    """
    applescript = ArcAppleScript()

    if not applescript.is_arc_running():
        print("Arc is not running. Changes will take effect when you launch Arc.")
        return False

    try:
        response = input("Restart Arc now to apply changes? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False

    if response in ("", "y", "yes"):
        print("Quitting Arc...")
        applescript.quit_arc()
        time.sleep(1)
        print("Launching Arc...")
        applescript.launch_arc()
        return True

    print("Remember to restart Arc for changes to take effect.")
    return False
