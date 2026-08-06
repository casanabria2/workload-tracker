# `launchd/` — keeping `wt_daemon.py` up

The menu-bar monitor (`~/dev/carlos/workload-macos-monitor`) talks HTTP to the
tracker. Historically that meant `tracker.py`'s in-process bridge on **:7373**,
which only exists while the TUI is open — close the TUI and the menu bar goes to
"tracker unreachable".

`wt_daemon.py` serves the same contract (plan §5.4), so pointing the monitor at
the daemon makes it work with the TUI closed. This LaunchAgent keeps the daemon
up at login.

## Install

```bash
cd ~/dev/carlos/workload-tracker
sed -e "s|__REPO__|$PWD|g" -e "s|__HOME__|$HOME|g" \
    launchd/com.carlossanabria.wtdaemon.plist \
    > ~/Library/LaunchAgents/com.carlossanabria.wtdaemon.plist
plutil -lint ~/Library/LaunchAgents/com.carlossanabria.wtdaemon.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.carlossanabria.wtdaemon.plist
```

plists do not expand `~` or environment variables in paths, hence the `sed`.
Keeping the template placeholder-based means the second Mac can check the repo
out anywhere.

Then point the monitor at the daemon (one key, no code change — its base URL is
already user-configurable):

```bash
defaults write WorkloadMonitor trackerBaseURL "http://127.0.0.1:7375"
```

## Verify

```bash
launchctl print gui/$(id -u)/com.carlossanabria.wtdaemon | grep -E 'state|pid|last exit'
curl -s http://127.0.0.1:7375/status                       # legacy, unauthenticated
curl -s -H "Authorization: Bearer $(cat ~/.workload_tracker_daemon_token)" \
     http://127.0.0.1:7374/v1/health | python3 -m json.tool
tail -f ~/Library/Logs/wt-daemon.log
```

`/v1/health`'s `data_file.readable` is the one to watch — see Full Disk Access
below.

## Uninstall / restart

```bash
launchctl bootout gui/$(id -u)/com.carlossanabria.wtdaemon        # stop + unload
launchctl kickstart -k gui/$(id -u)/com.carlossanabria.wtdaemon   # restart in place
defaults delete WorkloadMonitor trackerBaseURL                    # monitor back to :7373
```

Removing the agent without resetting `trackerBaseURL` leaves the monitor showing
"unreachable" until `tracker.py` is opened — reset both together.

## Three things that will bite on a fresh machine

**1. Full Disk Access is a separate grant for launchd.** A launchd-spawned
process does *not* inherit the Terminal/iTerm TCC grant. The data file resolves
into `~/Library/Mobile Documents`, so on the second Mac the agent may be denied
even though the CLI works fine in your terminal. Symptom:
`/v1/health` → `data_file.readable: false`, `reason` naming the error, and
the daemon **refusing to write** rather than treating the file as empty. Grant
Full Disk Access to `/usr/bin/python3`… no — grant it to the **venv interpreter
the plist runs** (`<repo>/venv/bin/python3.14`, the real file behind the
`python` symlink), then `launchctl kickstart -k`.

Verified working on the primary Mac (`readable: true`, 55 tasks). Untested on
the second Mac.

**2. `PATH` does not include Homebrew.** launchd hands a process a minimal
`PATH`, and the daemon shells out to `gh` for every GitHub operation. The plist
sets `PATH` explicitly with `/opt/homebrew/bin` first. Without it, reads work and
every issue/hours sync fails with "gh not found" — a failure that only shows up
on a write, long after install looked successful.

**3. `KeepAlive` is `SuccessfulExit=false`, not `true`.** `wt_daemon` exits 0
when it finds another daemon already answering (plan §5.5: "a daemon is running"
is the desired end state). A bare `KeepAlive=true` would respawn that clean exit
forever on a 10 s throttle. Crashes still restart — verified by `kill -9`, which
launchd replaced within the throttle window.

## Still true: the TUI can clobber the daemon

`tracker.py` holds the whole dataset in memory and saves wholesale, so with both
running the TUI can still overwrite daemon writes. Phase 0's lock cannot fix
that; it needs the TUI to become a daemon client, which is a follow-up in
`docs/plan-macos-app.md` §14. In practice: **if the TUI is open, make edits
there.** The daemon's job is to keep the monitor fed when it isn't.
