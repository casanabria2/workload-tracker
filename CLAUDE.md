# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Applications

```bash
# TUI (main app, requires textual)
python3 tracker.py

# CLI companion
python3 wt.py <command>

# Stream Deck HTTP bridge (runs on localhost:7373)
python3 streamdeck_bridge.py

# MCP server for Claude integration
python3 mcp_server.py
```

Install dependencies: `pip install -r requirements.txt`

## Architecture

Five single-file Python tools sharing one data file (`~/.workload_tracker.json`):

- **tracker.py** — Textual TUI with modal screens for task editing and time logging. Uses reactive properties for filtering and a 1-second interval timer for live updates.
- **wt.py** — Stateless CLI that reads/writes the JSON file directly. Commands: add, list, start, stop, log, logs, edit-log, delete-log, split-log, merge-logs, notes, link, unlink, done, delete, status, roles, arc, tabs, presence, config.
- **idle_detector.py** — macOS idle detection module using `ioreg` to query HIDIdleTime.
- **streamdeck_bridge.py** — HTTP server exposing actions at `/timer/toggle`, `/log/<minutes>`, `/status`, `/filter/<role>`.
- **mcp_server.py** — MCP server enabling Claude to manage tasks directly. Tools: add_task, list_tasks, get_task, start_timer, stop_timer, log_time, list_logs, edit_log, delete_log, split_log, merge_logs, set_task_status, delete_task, get_status, get_notes_path, link_github_issue, unlink_github_issue, view_github_issue, add_github_comment, list_roles, add_role, update_role, delete_role, setup_arc_space, get_arc_status, cleanup_task_tabs, sync_arc_folders.
- **arc_browser.py** — Arc browser integration for task-based tab management. Hybrid AppleScript/JSON approach.

### Data Model

Plain JSON with three top-level keys:
- `tasks[]` — Each task has: id, title, description, role_id, status, logs[], created_at, and optionally `github_issue`
- `active_timer` — `{task_id, started_at}` or null
- `roles[]` — Each role has: id, label, color. Roles are user-configurable via `wt roles` commands.

Time tracking: `logs[]` array of log entries. Timer sessions auto-commit as log entries when stopped.

Log entry structure:
```json
{
  "id": "20260403085012abcd",
  "minutes": 45.5,
  "note": "Timer session",
  "at": 1712181070,
  "started_at": 1712177400,  // optional: when work started
  "ended_at": 1712181060     // optional: when work ended
}
```

- `minutes` is the source of truth (allows manual adjustment)
- `started_at`/`ended_at` are automatically captured for timer sessions
- Existing logs without timestamps remain valid (backward compatible)

GitHub integration: Tasks can be linked to GitHub issues via `wt link <task> owner/repo#123`. When linked, `wt notes` opens the issue in browser instead of local notes file. The `github_issue` field stores the reference (e.g., `owner/repo#123`).

Arc browser integration: Tasks can have associated Arc folders. When enabled, the tracker creates a "Workload Tracker" space in Arc with role folders and task subfolders. Tab cleanup uses Claude API to classify which tabs are related to the current task.

- `arc_folder_id` — UUID of Arc folder for task (optional)
- `archived_tabs[]` — Tabs archived when task completed: `{url, title, archived_at}`
- `config.arc_space_id` — UUID of Workload Tracker space
- `config.tab_cleanup_enabled` — Enable tab classification on timer stop
- `config.tab_confidence_threshold` — Confidence threshold for unrelated tab detection (default: 0.7)
- `config.presence_detection_enabled` — Enable auto-stop timer on idle (default: false)
- `config.idle_timeout_minutes` — Minutes of inactivity before auto-stop (default: 15)
- `config.subtract_idle_time` — Subtract idle time from logged session (default: true)

### Domain Constants

- **Roles**: Stored in data file, defaults to `demokit`, `demos`, `strategic`, `other`. Can be managed via `wt roles add/update/delete`.
- **Statuses**: `todo`, `inprogress`, `done`
- Keyboard shortcuts 1-4 map to first 4 roles by order, 0 = all

### Key Patterns

- `uid()` generates timestamp-based IDs (duplicated in all three files)
- `task_logged_mins()` sums historical logs; `task_live_mins()` calculates running timer elapsed
- `resolve_task()` in wt.py does fuzzy title matching for CLI convenience
- TUI refreshes three things on state change: table, sidebar stats, overview panel

### Arc Integration

Setup: `wt arc setup` creates the "Workload Tracker" space and role folders in Arc. Requires Arc to be quit first.

Hybrid approach:
- **AppleScript operations** (no restart): get tabs, open tabs, close tabs, focus space
- **JSON operations** (restart required): create/delete spaces and folders, move tabs

Key classes in `arc_browser.py`:
- `ArcSidebarManager` — Read/write `~/Library/Application Support/Arc/StorableSidebar.json`
- `ArcAppleScript` — AppleScript commands for tab operations
- `TabClassifier` — Claude API for classifying tab relevance to tasks
- `TaskTabManager` — Orchestrates the workflow hooks

### Time Log Management

Full log editing capabilities via CLI, TUI, and MCP:

```bash
wt logs <task>                              # List all logs with timestamps
wt edit-log <task> <log-id> [--minutes M] [--note N]  # Edit entry
wt delete-log <task> <log-id>               # Delete entry (with confirmation)
wt split-log <task> <log-id> <minutes>      # Split at minute mark
wt merge-logs <task> <log-id-1> <log-id-2>  # Combine two entries
```

Log IDs are timestamp-based (e.g., `20260403085012abcd`). Commands accept ID prefixes for convenience.

**TUI**: Press `l` on a task to open the log management modal. Keyboard shortcuts: `a`=add, `e`=edit, `d`=delete, `s`=split, `m`=merge (merges current + next row).

**Split logic**: A 60min log split at 25min creates two entries (25min + 35min) with proportionally divided timestamps.

**Merge logic**: Combines minutes, concatenates notes as "Merged: note1 + note2", uses earliest start and latest end timestamps.

### Presence Detection

Auto-stops the timer when the user is idle (away from keyboard/mouse) for a configurable period. macOS only.

```bash
wt presence              # Show status
wt presence on           # Enable with default 15-minute timeout
wt presence off          # Disable
wt presence 20           # Set timeout to 20 minutes and enable
```

Implementation:
- `idle_detector.py` queries macOS `ioreg -c IOHIDSystem` for HIDIdleTime (nanoseconds since last input)
- `tracker.py` checks idle time in the `_tick()` loop (runs every 1 second when timer active)
- When idle exceeds threshold, timer auto-stops and logs time (optionally subtracting idle time)
- User receives a Textual notification in the TUI

## Known Limitations

- TUI reads `active_timer` on launch but timer display may need manual refresh to start ticking
- Stream Deck `/filter/<role>` endpoint doesn't sync to TUI (separate processes)
- No `wt edit` command for tasks yet (use TUI for editing task title/description/role)
- No export/report functionality
- Arc integration requires Arc to be quit for folder changes
- Arc Sync may interfere with sidebar JSON modifications
