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
- **wt.py** — Stateless CLI that reads/writes the JSON file directly. Commands: add, list, start, stop, log, logs, edit-log, delete-log, split-log, merge-logs, notes, link, unlink, done, delete, rename, status, roles, arc, iterm, tabs, presence, config, calendar.
- **idle_detector.py** — macOS idle detection module using `ioreg` to query HIDIdleTime.
- **streamdeck_bridge.py** — HTTP server exposing actions at `/timer/toggle`, `/log/<minutes>`, `/status`, `/filter/<role>`.
- **mcp_server.py** — MCP server enabling Claude to manage tasks directly. Tools: add_task, list_tasks, get_task, start_timer, stop_timer, log_time, list_logs, edit_log, delete_log, split_log, merge_logs, set_task_status, delete_task, rename_task, get_status, get_notes_path, link_github_issue, unlink_github_issue, view_github_issue, add_github_comment, list_roles, add_role, update_role, delete_role, set_role_repo, setup_arc_space, get_arc_status, cleanup_task_tabs, sync_arc_folders.
- **arc_browser.py** — Arc browser integration for task-based tab management. Hybrid AppleScript/JSON approach.
- **iterm_manager.py** — iTerm2/tmux integration for task-based terminal sessions. Creates folders per task and manages tmux sessions with 3-pane layout.

### Data Model

Plain JSON with three top-level keys:
- `tasks[]` — Each task has: id, title, description, role_id, status, logs[], created_at, and optionally `github_issue`, `calendar_event_uid`
- `active_timer` — `{task_id, started_at}` or null
- `roles[]` — Each role has: id, label, color, and optionally `github_repo`. Roles are user-configurable via `wt roles` commands.

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

iTerm2/tmux integration: Tasks can have associated terminal sessions and folders.

- `iterm_session_name` — tmux session name for task (e.g., `wt-demokit-my-task`)
- `task_folder_path` — Path to task's project folder (auto-created in WorkloadTracker)
- `local_folder` — Optional path to local git repo or custom folder (overrides task_folder_path for terminal sessions)
- `config.iterm_enabled` — Enable iTerm integration (default: false)
- `config.iterm_projects_dir` — Base directory for task folders (default: `~/Library/Mobile Documents/com~apple~CloudDocs/WorkloadTracker`, symlinked to `~/WorkloadTracker` for shorter terminal prompts)

### Domain Constants

- **Roles**: Stored in data file, defaults to `demokit`, `demos`, `strategic`, `other`. Can be managed via `wt roles add/update/delete`.
- **Statuses**: `todo`, `inprogress`, `done`
- Done tasks are hidden by default in all list views (CLI, TUI, MCP)
- Keyboard shortcuts 1-4 map to first 4 roles by order, 0 = all, `a` = toggle done tasks, `i` = open iTerm (TUI)

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

### iTerm2/tmux Integration

Each task can have an associated terminal session with a dedicated project folder. Uses Hammerspoon for window positioning.

```bash
wt iterm setup               # Enable iTerm integration
wt iterm open <task>         # Open iTerm2 terminal for a task
wt iterm close <task>        # Close tmux session for a task
wt iterm set-folder <task> <path>  # Set local folder (e.g., git repo)
wt iterm clear-folder <task> # Clear local folder setting
wt iterm status              # Show iTerm integration status
```

**Local folder**: If a task has a `local_folder` set, the terminal opens in that directory instead of the auto-created WorkloadTracker folder. Set via CLI (`wt iterm set-folder`) or TUI (edit task with `e`, fill "Local folder path" field).

**TUI keybindings**:
- `i` — Open iTerm2 terminal for selected task
- `e` — Edit task (includes local folder field)

**Folder structure** (when no local_folder set, organized by role + title slug):
```
~/Library/Mobile Documents/com~apple~CloudDocs/WorkloadTracker/
├── demokit/
│   └── my-task-slug/
├── demos/
│   └── another-task/
└── other/
    └── misc-task/
```

Note: A symlink `~/WorkloadTracker` is used in terminal sessions for shorter prompts.

**tmux layout** (3-pane using `main-horizontal`):
```
┌────────────────┬────────────────┐
│   Pane 0       │   Pane 1       │  ← 2/3 height
│  (top-left)    │  (top-right)   │
├────────────────┴────────────────┤
│         Pane 2                  │  ← 1/3 height
│        (bottom)                 │
└─────────────────────────────────┘
```

**Window positioning**: Hammerspoon positions new windows at (111, 35) with size 3440x1410.

Key classes in `iterm_manager.py`:
- `TmuxManager` — Create/kill tmux sessions with 3-pane layout (uses `main-horizontal`)
- `ItermAppleScript` — Open iTerm2 windows via AppleScript, position with Hammerspoon
- `TaskTerminalManager` — Main orchestrator, manages folders and sessions

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

### Google Calendar Integration

Import calendar events as tasks with automatic time logging.

```bash
wt calendar                  # List events from yesterday & today
wt calendar 7                # List events from last 7 days
wt calendar import <event>   # Import event as task (prompts for time)
wt calendar setup            # Show setup instructions
```

**Setup**: Requires Google Calendar API credentials (`~/.workload_tracker_gcal_credentials.json`):
1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project (or select existing) and enable **Google Calendar API**
3. Go to **APIs & Services → Credentials**
4. Find your OAuth 2.0 Client ID (Desktop app type), or create one
5. Create a new client secret and download the JSON file
6. Save as `~/.workload_tracker_gcal_credentials.json`
7. Run `wt calendar` — browser opens for authorization, token is saved automatically

**Configuration**:
```bash
wt config calendar_id your.email@gmail.com  # Use specific calendar (default: primary)
```

**Import flow**:
1. Shows event details (title, time, duration)
2. Prompts for role selection
3. Prompts for time: `[Y/n/minutes]` - confirm, skip, or adjust duration
4. Creates task with status "done" and logs time with original timestamps

**Tracking**: Imported events store `calendar_event_uid` to prevent duplicate imports. Already-imported events show with ✓ in the list.
- User receives a Textual notification in the TUI

### Task Closing Workflow with GitHub Project Integration

When a task is marked as "done" (via CLI `wt done`, TUI status cycling, or MCP `set_task_status`), a workflow triggers based on the role's GitHub repo configuration:

**Role → Repository Mapping:**

Each role can have an optional `github_repo` field:

```bash
wt roles set-repo demokit grafana/field-eng-demo-kit
wt roles set-repo demos grafana/field-eng
wt roles set-repo strategic grafana/field-eng
wt roles set-repo testing casanabria2/workload-tracker  # for testing
# "other" role has no repo (skips GitHub integration)
```

**Close Workflow:**

1. If the role has **no configured repo**: Task is simply marked as done (no GitHub integration)
2. If the role **has a configured repo**:
   - Task must have a linked GitHub issue
   - If no issue exists, user is prompted to create one (with local notes as body)
   - Issue is added to the configured GitHub project (if configured)
   - Project item is updated with Status=Done and logged hours
   - **GitHub issue is automatically closed**

**Configuration:**

```bash
wt config github_project_owner grafana      # Org that owns the project
wt config github_project_number 123         # Project number
```

Config values in `~/.workload_tracker.json`:

```json
{
  "config": {
    "github_project_owner": "grafana",
    "github_project_number": 123
  },
  "roles": [
    {"id": "demokit", "label": "Managing DemoKit", "color": "blue", "github_repo": "grafana/field-eng-demo-kit"},
    {"id": "demos", "label": "Demos & Workshops", "color": "green", "github_repo": "grafana/field-eng"},
    {"id": "other", "label": "Other", "color": "white"}
  ]
}
```

**MCP Usage:**

```python
# List tasks (done tasks hidden by default)
list_tasks()                         # Active tasks only
list_tasks(include_done=True)        # Include done tasks
list_tasks(status="done")            # Only done tasks

# Close a task (prompts if issue creation needed in CLI/TUI)
set_task_status("My task", "done")

# Close and auto-create issue if missing
set_task_status("My task", "done", create_issue=True)

# Configure role repos
set_role_repo("demokit", "grafana/field-eng-demo-kit")
set_role_repo("other")  # Clear repo (disables GitHub integration for role)
```

### GitHub CLI (gh) Reference

Key patterns for working with the `gh` CLI:

**Issue Operations:**
```bash
gh issue create -R owner/repo --title "Title" --body "Body" --assignee @me
gh issue view 123 -R owner/repo --json number,state,assignees
gh issue edit 123 -R owner/repo --add-assignee @me  # Idempotent, won't duplicate
gh issue close 123 -R owner/repo
gh issue edit 123 -R owner/repo --title "New title"  # Update title
gh issue delete 123 -R owner/repo --yes  # Permanent deletion (admin only)
```

**Project Operations:**

The `gh project item-edit` command requires **full IDs**, not numbers or names:
```bash
# Get project ID (not the number!)
gh project view 123 --owner org --format json  # Returns {"id": "PVT_xxx", ...}

# Get field IDs
gh project field-list 123 --owner org --format json
# Returns: {"fields": [{"id": "PVTF_xxx", "name": "Status", "options": [{"id": "abc", "name": "Done"}]}]}

# Add item to project (uses project number + owner)
gh project item-add 123 --owner org --url https://github.com/owner/repo/issues/456 --format json

# Edit item (uses project ID, item ID, field ID, option ID - NO --owner flag)
gh project item-edit --project-id PVT_xxx --id PVTI_xxx --field-id PVTF_xxx --single-select-option-id abc
gh project item-edit --project-id PVT_xxx --id PVTI_xxx --field-id PVTF_yyy --number 5
```

**Important gotchas:**
- `gh project item-edit` does NOT accept `--owner` flag (unlike other project commands)
- Field names like "Status" or "Hours" must be resolved to field IDs first
- Single-select options like "Done" must be resolved to option IDs
- Project number (e.g., 123) vs project ID (e.g., PVT_xxx) are different things
- `--add-assignee @me` is idempotent - safe to call even if already assigned

### Renaming Tasks

Tasks can be renamed via CLI, TUI, or MCP. When a task has a linked GitHub issue, renaming automatically updates the issue title:

```bash
wt rename "old task name" "new task name"
# Also updates the linked GitHub issue title if present
```

**MCP:**
```python
rename_task("old name", "new name")  # Updates GitHub issue title if linked
```

**TUI:** Press `e` on a task to edit. Changes to the title are synced to GitHub.

## Known Limitations

- TUI reads `active_timer` on launch but timer display may need manual refresh to start ticking
- Stream Deck `/filter/<role>` endpoint doesn't sync to TUI (separate processes)
- No export/report functionality
- Arc integration requires Arc to be quit for folder changes
- Arc Sync may interfere with sidebar JSON modifications

## Zsh Autocompletion

The `_wt` file provides zsh tab completion for the `wt` CLI. When adding new commands or subcommands, update this file to maintain autocompletion support.

**File location:** `_wt` (symlinked to zsh site-functions)

**Structure:**
```zsh
_wt() {
    # 1. Define commands array with descriptions
    commands=(
        'calendar:Import tasks from Google Calendar'
        'newcmd:Description of new command'
    )

    # 2. Handle command completion (CURRENT == 2)
    if (( CURRENT == 2 )); then
        _describe -t commands 'command' commands
        return
    fi

    # 3. Handle subcommand/argument completion in case statement
    case "${words[2]}" in
        newcmd)
            # Subcommand completion at position 3
            if (( CURRENT == 3 )); then
                local -a subcommands
                subcommands=('sub1:Description' 'sub2:Description')
                _describe -t subcommands 'subcommand' subcommands
            fi
            ;;
    esac
}
```

**Key patterns:**

- `compadd "${array[@]}"` — Add completions (zsh auto-quotes spaces)
- `compadd -Q "${array[@]}"` — Add completions without zsh quoting
- `_describe -t tag 'description' array` — Show completions with descriptions
- `CURRENT` — Current word position (2=command, 3=first arg, etc.)
- `${words[2]}` — The command being completed

**Dynamic completions (e.g., task names):**
```zsh
tasks=("${(@f)$(python3 -c "
import json
from pathlib import Path
data = json.loads((Path.home() / '.workload_tracker.json').read_text())
for t in data.get('tasks', []):
    print(t['title'])
" 2>/dev/null)}")
compadd "${tasks[@]}"
```

**Using venv Python** (for commands needing extra packages):
```zsh
local wt_dir="${0:A:h}"
local venv_python="${wt_dir}/venv/bin/python"
[[ -x "$venv_python" ]] || venv_python="python3"
```

**After modifying `_wt`:** User must reload completions:
```bash
rm -f ~/.zcompdump* && exec zsh
```
