# TODO

## 1. Better Time Logging ([#3](https://github.com/casanabria2/workload-tracker/issues/3))

Implement improved time logging to track working sessions individually and allow editing.

**Current behavior:** Time is logged as simple `{id, minutes, note, at}` entries in the `logs[]` array. No way to edit or correct entries.

**Desired features:**
- Track each working session with start/end timestamps (not just total minutes)
- Allow editing log entries for cases when timer was forgotten running
- Support splitting/merging log entries
- Add CLI command `wt edit-log <task> <log-id>` to modify existing entries
- TUI screen for viewing and editing time logs per task

## 2. Presence Detection ([#4](https://github.com/casanabria2/workload-tracker/issues/4))

Implement automatic task stopping when user is away from computer.

**Features:**
- Detect user inactivity (no keyboard/mouse input) after configurable timeout
- Automatically stop running timer when user is detected as away
- Option to subtract idle time from the logged session
- Store idle threshold in config (e.g., `config.idle_timeout_minutes`)
- Notification when timer is auto-stopped due to inactivity
- macOS: Use `ioreg` to query HIDIdleTime for system idle detection
