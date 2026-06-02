# Workload Tracker

Keyboard-first task tracker with time logging, built around your four Field Engineering roles.

## Files

```
workload_tracker/
├── tracker.py          — Full TUI (Textual), keyboard-driven; also hosts the HTTP bridge
├── wt.py               — CLI for quick terminal commands
├── mcp_server.py       — MCP server for Claude integration
├── _wt                 — Zsh completion script
└── requirements.txt
```

Data is stored at `~/.workload_tracker.json` — all three tools share the same file.
Task notes are stored in `~/.workload_tracker_notes/<task_id>.md`.

---

## Setup

```bash
# Create venv and install dependencies
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Make scripts executable
chmod +x tracker.py wt.py

# Add wt CLI to PATH (symlink to ~/.local/bin)
mkdir -p ~/.local/bin
ln -sf "$(pwd)/wt.py" ~/.local/bin/wt
```

Make sure `~/.local/bin` is in your PATH. Add to `~/.zshrc` if needed:
```bash
export PATH="$HOME/.local/bin:$PATH"
```

### Zsh autocompletion

```bash
# Symlink completion script to zsh site-functions
ln -sf "$(pwd)/_wt" "$(brew --prefix)/share/zsh/site-functions/_wt"

# Clear completion cache and restart shell
rm -f ~/.zcompdump*
exec zsh
```

Now you can tab-complete commands and task names:
```bash
wt <Tab>           # shows: add, list, start, stop, log, notes, done, delete, status
wt notes <Tab>     # shows task titles
wt add --role <Tab> # shows: demokit, demos, strategic, other
```

---

## TUI — tracker.py

```bash
python3 tracker.py
```

### Keyboard shortcuts

| Key       | Action                                      |
|-----------|---------------------------------------------|
| `n`       | New task                                    |
| `e`       | Edit selected task                          |
| `d`       | Delete selected task                        |
| `t`       | Toggle timer on selected task               |
| `l`       | Log time manually on selected task          |
| `s`       | Cycle status (To Do → In Progress → Done)   |
| `1`       | Filter: Managing DemoKit                    |
| `2`       | Filter: Demos & Workshops                   |
| `3`       | Filter: Strategic Deals                     |
| `4`       | Filter: Other                               |
| `0`       | Filter: All roles                           |
| `a`       | Toggle showing done tasks                   |
| `r`       | Reload data from disk and refresh screen    |
| `Tab`     | Switch between Task Board / Overview        |
| `↑ ↓`     | Navigate tasks                              |
| `q`       | Quit                                        |

---

## CLI — wt.py

Quick commands without opening the TUI. All changes instantly appear in the TUI.

```bash
# Add tasks
wt add "Support Banco Galicia" --role strategic --status inprogress
wt add "NVIDIA Kratos demo" --role demos --status todo
wt add "DemoKit PR review" --role demokit

# List tasks
wt list
wt list --role strategic

# Timer control
wt start "Banco Galicia"       # partial title match works
wt stop

# Log time manually
wt log "Banco Galicia" 45 "Call with customer"
wt log "DemoKit PR" 30

# Update status
wt done "DemoKit PR"

# Close recurrent tasks (that have a GitHub issue) from a prior sprint
wt close-recurrent                 # previous sprint only (default)
wt close-recurrent --all-previous  # every earlier sprint
wt close-recurrent --dry-run       # preview without making changes

# Recreate the prior sprint's recurring tasks (open and closed) in the current
# sprint, each with its own GitHub issue (sprint-suffixed titles get re-suffixed)
wt new-recurrent                   # previous sprint only (default)
wt new-recurrent --all-previous    # source every earlier sprint
wt new-recurrent --dry-run         # preview without creating anything

# Task notes (opens in $EDITOR or GitHub issue)
wt notes "Banco Galicia"

# Link task to GitHub issue (uses issue for notes instead of local file)
wt link "Banco Galicia" owner/repo#123
wt unlink "Banco Galicia"

# Overview
wt status

# Delete
wt delete "old task"

# Manage roles
wt roles                        # list all roles
wt roles add myteam "My Team"   # add new role
wt roles update myteam "Team X" # rename role
wt roles delete myteam          # delete role (must have no tasks)
```

---

## Stream Deck — built into tracker.py

The HTTP bridge runs **inside the TUI** on a background thread, so there's no
separate process to launch. Just run the tracker — the bridge listens on
`http://localhost:7373` for as long as the TUI is open, and its actions mutate
the same in-memory data the UI shows (changes appear live, no refresh needed):

```bash
python3 tracker.py
```

If port 7373 is already in use (e.g. a stale standalone bridge), the TUI shows
a warning notification and starts without the bridge.

### Button configuration

In Stream Deck software, use **"Open URL"** action with these URLs:

| Button label       | URL                                          |
|--------------------|----------------------------------------------|
| ▶/⏸ Timer         | `http://localhost:7373/timer/toggle`         |
| Log 15m            | `http://localhost:7373/log/15`               |
| Log 30m            | `http://localhost:7373/log/30`               |
| Log 60m            | `http://localhost:7373/log/60`               |
| DemoKit            | `http://localhost:7373/filter/demokit`       |
| Demos              | `http://localhost:7373/filter/demos`         |
| Strategic          | `http://localhost:7373/filter/strategic`     |
| Status             | `http://localhost:7373/status`               |

The timer toggle will:
- **Start**: timer on the most recently added in-progress task
- **Stop**: commit elapsed time as a log entry (identical to the TUI `t`-key
  stop — same `"Timer session"` log note, GitHub hours sync, and Arc cleanup)

### API contract

The bridge serves JSON on `http://127.0.0.1:7373`. Beyond the Stream Deck
buttons above, these endpoints are intended for a companion client app (e.g. a
menu-bar timer). All state changes mutate the live in-memory data and refresh
the TUI.

| Method & path        | Body                | Response                                                                              |
|----------------------|---------------------|---------------------------------------------------------------------------------------|
| `GET /status`        | —                   | `{ "active_timer": { "task_id", "title", "role", "started_at" } }` — `null` when idle |
| `GET /tasks`         | —                   | `{ "tasks": [ { "id", "title", "role", "status" } ] }` (non-done, non-shadow)         |
| `POST /timer/start`  | `{ "task_id": "…" }`| `{ "action": "started", "task": "…" }`                                                |
| `POST /timer/stop`   | —                   | `{ "action": "stopped", "task": "…", "logged_minutes": 8.0 }`                         |

`started_at` is raw epoch seconds (e.g. `1780339520.71`) so the client can tick
elapsed locally between polls. `/status` also includes convenience fields
(`elapsed`, `tasks` count, `time_by_role`) that clients may ignore.

`POST /timer/start` stops any already-running timer first (with the same stop
semantics as above), then starts the new one.

### Reachable vs. idle

The bridge only listens while `tracker.py` is open. A client should treat a
**connection error** (refused/timeout) as a distinct "tracker unreachable"
state — not the same as a successful `GET /status` returning
`active_timer: null`, which means the tracker is up but idle.

### Availability

The bridge is available whenever `tracker.py` is running, so there's no longer
a separate bridge daemon to auto-start. Keep the TUI open (e.g. in a dedicated
terminal or tmux session) and the Stream Deck buttons will work.

---

## Hammerspoon integration (optional)

Add to your `~/.hammerspoon/init.lua` to trigger from hotkeys:

```lua
-- Workload Tracker hotkeys
hs.hotkey.bind({"ctrl", "alt"}, "T", function()
  hs.execute("curl -s http://localhost:7373/timer/toggle")
  hs.notify.new({title="Workload Tracker", informativeText="Timer toggled"}):send()
end)

hs.hotkey.bind({"ctrl", "alt"}, "L", function()
  -- Quick-log 15 min
  hs.execute("curl -s http://localhost:7373/log/15")
  hs.notify.new({title="Workload Tracker", informativeText="Logged 15 minutes"}):send()
end)
```

---

## MCP Server — mcp_server.py

Allows Claude (via Claude Code or Claude Desktop) to interact directly with tasks.

### Available tools

| Tool | Description |
|------|-------------|
| `add_task` | Create a new task with title, role, status, github_issue |
| `list_tasks` | List all tasks, optionally filter by role/status |
| `get_task` | Get details of a specific task |
| `start_timer` | Start timer on a task |
| `stop_timer` | Stop the running timer |
| `log_time` | Log time manually to a task |
| `set_task_status` | Change task status (todo/inprogress/done) |
| `close_previous_recurrent_tasks` | Close recurrent tasks (with a GitHub issue) from the previous sprint, or all earlier sprints with `all_previous=True` |
| `delete_task` | Delete a task |
| `get_status` | Get time summary by role |
| `get_notes_path` | Get notes location (GitHub issue or local file path) |
| `link_github_issue` | Link a task to a GitHub issue |
| `unlink_github_issue` | Unlink a task from its GitHub issue |
| `view_github_issue` | View GitHub issue body and comments |
| `add_github_comment` | Add a comment to the linked GitHub issue |

### Claude Code setup

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "workload-tracker": {
      "command": "/Users/carlos/dev/carlos/workload-tracker/venv/bin/python3",
      "args": ["/Users/carlos/dev/carlos/workload-tracker/mcp_server.py"]
    }
  }
}
```

### Claude Desktop setup

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "workload-tracker": {
      "command": "/Users/carlos/dev/carlos/workload-tracker/venv/bin/python3",
      "args": ["/Users/carlos/dev/carlos/workload-tracker/mcp_server.py"]
    }
  }
}
```

Then restart Claude Code/Desktop to load the MCP server.
