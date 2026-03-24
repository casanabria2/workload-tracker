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

Four single-file Python tools sharing one data file (`~/.workload_tracker.json`):

- **tracker.py** — Textual TUI with modal screens for task editing and time logging. Uses reactive properties for filtering and a 1-second interval timer for live updates.
- **wt.py** — Stateless CLI that reads/writes the JSON file directly. Commands: add, list, start, stop, log, notes, link, unlink, done, delete, status, roles.
- **streamdeck_bridge.py** — HTTP server exposing actions at `/timer/toggle`, `/log/<minutes>`, `/status`, `/filter/<role>`.
- **mcp_server.py** — MCP server enabling Claude to manage tasks directly. Tools: add_task, list_tasks, get_task, start_timer, stop_timer, log_time, set_task_status, delete_task, get_status, get_notes_path, link_github_issue, unlink_github_issue, view_github_issue, add_github_comment, list_roles, add_role, update_role, delete_role.

### Data Model

Plain JSON with three top-level keys:
- `tasks[]` — Each task has: id, title, description, role_id, status, logs[], created_at, and optionally `github_issue`
- `active_timer` — `{task_id, started_at}` or null
- `roles[]` — Each role has: id, label, color. Roles are user-configurable via `wt roles` commands.

Time tracking: `logs[]` array of `{id, minutes, note, at}` entries. Timer sessions auto-commit as log entries when stopped.

GitHub integration: Tasks can be linked to GitHub issues via `wt link <task> owner/repo#123`. When linked, `wt notes` opens the issue in browser instead of local notes file. The `github_issue` field stores the reference (e.g., `owner/repo#123`).

### Domain Constants

- **Roles**: Stored in data file, defaults to `demokit`, `demos`, `strategic`, `other`. Can be managed via `wt roles add/update/delete`.
- **Statuses**: `todo`, `inprogress`, `done`
- Keyboard shortcuts 1-4 map to first 4 roles by order, 0 = all

### Key Patterns

- `uid()` generates timestamp-based IDs (duplicated in all three files)
- `task_logged_mins()` sums historical logs; `task_live_mins()` calculates running timer elapsed
- `resolve_task()` in wt.py does fuzzy title matching for CLI convenience
- TUI refreshes three things on state change: table, sidebar stats, overview panel

## Known Limitations

- TUI reads `active_timer` on launch but timer display may need manual refresh to start ticking
- Stream Deck `/filter/<role>` endpoint doesn't sync to TUI (separate processes)
- No `wt edit` command yet
- No export/report functionality
