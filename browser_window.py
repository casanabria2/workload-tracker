#!/usr/bin/env python3
"""
Safari window integration for Workload Tracker.

Each task remembers an ordered list of browser tab URLs. When a task's timer
starts, those tabs open in a dedicated Safari window (one OS window per task).
Switching/stopping snapshots the current window's tabs back to the task and
closes the window.

Safari-specific scripting notes (validated live on this machine):
- Tabs must be made in the *window*, not the document, or Safari raises
  ``-10024 "Can't make or move that element into that container."``
- A closed Safari window leaves a bounded, invisible empty "zombie" window
  (``visible=false``, ``tabs=0``). ``saving no`` does not prevent it, but the
  next ``make new document`` recycles it, so the count stays ~1. We treat a
  window as valid only when it exists *and* has ``> 0`` tabs.
- This feature uses only direct Safari scripting (no System Events), so the
  one-time macOS Automation permission grant is sufficient.
"""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)

APP = "Safari"


def run_applescript(script: str) -> str:
    """Run an AppleScript via ``osascript`` and return its stdout.

    Raises ``RuntimeError`` with the stderr text on a non-zero exit.
    """
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def _escape(url: str) -> str:
    """Escape a URL for safe embedding in an AppleScript string literal."""
    return url.replace("\\", "\\\\").replace('"', '\\"')


class SafariWindowManager:
    """Manage per-task Safari windows (open / snapshot / focus / close)."""

    def open_window(self, urls: list[str]) -> int:
        """Open a new Safari window containing ``urls`` and return its window id.

        The first URL becomes a new document; subsequent URLs are added as
        tabs of the front window (the Safari-correct form).
        """
        if not urls:
            raise ValueError("open_window requires at least one URL")

        first = _escape(urls[0])
        tab_lines = "\n".join(
            f'        make new tab with properties {{URL:"{_escape(u)}"}}'
            for u in urls[1:]
        )
        script = f'''
        tell application "{APP}"
            make new document with properties {{URL:"{first}"}}
            tell front window
{tab_lines}
            end tell
            return id of front window
        end tell
        '''
        out = run_applescript(script)
        return int(out)

    def snapshot_window(self, window_id: int | None) -> list[str]:
        """Return the ordered tab URLs of the given window.

        If ``window_id`` is None, snapshot the front window instead. Returns an
        empty list if the window is gone.
        """
        if window_id is None:
            target = "front window"
        else:
            target = f"(first window whose id is {int(window_id)})"

        script = f'''
        tell application "{APP}"
            set urlList to {{}}
            try
                repeat with tb in tabs of {target}
                    set end of urlList to (URL of tb)
                end repeat
            end try
            set AppleScript's text item delimiters to linefeed
            return urlList as text
        end tell
        '''
        try:
            out = run_applescript(script)
        except RuntimeError as exc:
            logger.warning("snapshot_window failed: %s", exc)
            return []
        if not out:
            return []
        return [line for line in out.split("\n") if line]

    def focus_window(self, window_id: int) -> bool:
        """Bring the given window to the front. Returns True on success."""
        script = f'''
        tell application "{APP}"
            try
                set index of (first window whose id is {int(window_id)}) to 1
                activate
                return "ok"
            on error
                return "gone"
            end try
        end tell
        '''
        try:
            return run_applescript(script) == "ok"
        except RuntimeError as exc:
            logger.warning("focus_window failed: %s", exc)
            return False

    def close_window(self, window_id: int) -> bool:
        """Close the given window. Returns True on success.

        Safari leaves a bounded, invisible empty window behind (documented
        limitation); since we null ``active_window_id`` on close we never
        re-reference it, so this is cosmetic only.
        """
        script = f'''
        tell application "{APP}"
            try
                close window id {int(window_id)} saving no
                return "ok"
            on error
                return "gone"
            end try
        end tell
        '''
        try:
            return run_applescript(script) == "ok"
        except RuntimeError as exc:
            logger.warning("close_window failed: %s", exc)
            return False

    def window_exists(self, window_id: int | None) -> bool:
        """True only if the window exists *and* has at least one tab.

        Guards against Safari's hidden empty zombie windows.
        """
        if window_id is None:
            return False
        script = f'''
        tell application "{APP}"
            try
                set w to (first window whose id is {int(window_id)})
                if (count of tabs of w) > 0 then
                    return "yes"
                else
                    return "no"
                end if
            on error
                return "no"
            end try
        end tell
        '''
        try:
            return run_applescript(script) == "yes"
        except RuntimeError as exc:
            logger.warning("window_exists failed: %s", exc)
            return False

    def open_task_window(self, task: dict) -> int | None:
        """Open (or focus) the dedicated Safari window for a task.

        If the task already has a valid ``active_window_id``, focus it. Else
        open ``task["tabs"]`` in a new window and store its id on the task
        (the caller is responsible for persisting via ``save``). Returns the
        window id, or None when the task has no saved tabs.
        """
        existing = task.get("active_window_id")
        if self.window_exists(existing):
            self.focus_window(existing)
            return existing

        urls = task.get("tabs") or []
        if not urls:
            return None

        try:
            window_id = self.open_window(urls)
        except (RuntimeError, ValueError) as exc:
            logger.warning("open_task_window failed: %s", exc)
            return None
        task["active_window_id"] = window_id
        return window_id

    def snapshot_task_tabs(self, task: dict) -> list[str]:
        """Snapshot the task's window tabs into ``task["tabs"]`` and return them.

        Uses ``active_window_id`` when present, else the front window. The
        caller is responsible for persisting via ``save``.
        """
        urls = self.snapshot_window(task.get("active_window_id"))
        if urls:
            task["tabs"] = urls
        return urls
