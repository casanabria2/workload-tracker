# `macos-client/` — WorkloadTracker.app

A SwiftUI/AppKit board over `wt_daemon.py`. SwiftPM executable, no `.xcodeproj`,
zero third-party dependencies, macOS 26 unconditionally — the same conventions as
`~/dev/carlos/workload-macos-monitor`. The build itself is documented in
`docs/plan-macos-app.md`; this file is about **packaging** (plan §12, Phase 9).

## Build

```bash
cd ~/dev/carlos/workload-tracker/macos-client
./make-app.sh
```

The bundle lands in **`macos-client/dist/WorkloadTracker.app`** and is
`.gitignore`d — the script and `Info.plist` are committed, the artifact is not.
`./make-app.sh -o /Applications` installs it elsewhere; `--identity`,
`--no-hardened-runtime` and `--help` are the other flags.

The script is idempotent (re-run it after any code change) and never leaves a
half bundle: everything is assembled, linted and signed in a private staging
directory, and the live bundle is replaced only once the new one verifies.

## Run

```bash
open macos-client/dist/WorkloadTracker.app
```

`swift run` still works for development, but it is **not the same app**: a bare
executable has no bundle identifier, so it gets a different `UserDefaults` domain
(`WorkloadClient`) and no state restoration. See below.

## Why the bundle matters

This is not cosmetic packaging. Three things in this codebase silently did not
work without an `Info.plist` and a bundle identifier:

| # | Symptom | Cause |
|---|---|---|
| 1 | The Phase 4 board drag lifted a card and no drop callback ever fired | `UTType(exportedAs:)` needs `UTExportedTypeDeclarations` |
| 2 | `@SceneStorage("filterState")` never restored | AppKit state restoration is keyed by bundle identifier |
| 3 | `@SceneStorage("sidebarSelection")` never restored (Phase 3, unnoticed) | same |

(2) and (3) are **fixed and verified**: with the whole
`com.carlossanabria.workloadtracker` defaults domain deleted, the app was
relaunched and came back on the Timeline view with its role filter intact — so
the restore came from the scene store, not from the `AppSettings` mirror.

(1) is **unblocked but not switched on** — see "The custom drag type" below.

### The `UserDefaults` domain moved

Unbundled, `UserDefaults.standard` wrote to a domain named after the executable;
bundled, it uses the bundle identifier:

```bash
defaults read WorkloadClient                        # the swift run / swift build one
defaults read com.carlossanabria.workloadtracker    # the .app
```

So the first launch of the `.app` starts on **default settings** — daemon URL,
token path, repository path, `autoStartDaemon`, `opensTaskWindow`. All the
defaults are the right ones for this machine, so nothing needs doing; but if any
of them had been changed, carry them over explicitly, e.g.:

```bash
defaults write com.carlossanabria.workloadtracker daemonBaseURL \
    "$(defaults read WorkloadClient daemonBaseURL)"
```

### Keep the `AppSettings.lastFilterState` mirror

`AppSettings.lastFilterState` exists because `@SceneStorage` did not persist. It
does now — but the mirror should **stay**, because state restoration is user- and
launch-switchable in ways `@SceneStorage` reports nothing about:

* **System Settings → Desktop & Dock → "Close windows when quitting an
  application"** (`NSQuitAlwaysKeepsWindows = 0`) turns restoration off wholesale.
* `open --fresh` discards saved state by design.

In both cases the scene value silently reverts to its default and the mirror is
the only thing that remembers the filter. `RootView.onAppear` already prefers the
scene value and falls back to the mirror, which is the correct precedence.

## Signing

Ad-hoc (`codesign -s -`) with the hardened runtime, which builds and launches
fine. Nothing here is distributed, so there is **no notarization and no Developer
ID** — do not add either.

```
$ codesign -dv dist/WorkloadTracker.app
Identifier=com.carlossanabria.workloadtracker
Format=app bundle with Mach-O thin (arm64)
CodeDirectory v=20500 ... flags=0x10002(adhoc,runtime) ...
Signature=adhoc
TeamIdentifier=not set

$ spctl -a -vvv dist/WorkloadTracker.app
dist/WorkloadTracker.app: rejected
```

`spctl: rejected` is **expected and harmless**: Gatekeeper only assesses
quarantined bundles, and a locally built one carries no quarantine attribute. It
would matter only if the `.app` were sent to another machine (it must not be —
rebuild there instead).

## Permissions / TCC, once bundled

Bundling **changes who the OS asks about**, which is the practical difference to
watch for:

* TCC grants are per-**bundle identifier** for a bundled app, where the unbundled
  binary inherited the grants of the terminal that launched it. So the first time
  the `.app` needs something, macOS prompts again even though your terminal is
  already trusted. Nothing in the current build triggers a prompt: it makes local
  HTTP requests and reads a token file, neither of which is TCC-gated.
* **Second Mac / Full Disk Access (plan §12).** `~/.workload_tracker.json`
  resolves into `~/Library/Mobile Documents`, which is TCC-protected, and a
  spawned daemon inherits *the app's* context — so on the second Mac
  `WorkloadTracker.app` itself needs Full Disk Access, and granting Terminal is
  not enough. Symptom is the documented one: `ls` says *Operation not permitted*
  and the dataset looks empty. `autoStartDaemon` defaults to **off**, so today
  the app never spawns a daemon and this only bites if it is turned on.
* The daemon the app talks to is normally the LaunchAgent
  (`launchd/README.md`), which has its own separate Full Disk Access story.

## The custom drag type

`Info.plist` declares:

```xml
<key>UTExportedTypeDeclarations</key>
<array>
    <dict>
        <key>UTTypeIdentifier</key>
        <string>com.carlossanabria.workloadtracker.task</string>
        <key>UTTypeDescription</key>
        <string>Workload Tracker Task</string>
        <key>UTTypeConformsTo</key>
        <array>
            <string>public.data</string>
        </array>
    </dict>
</array>
```

and `make-app.sh` registers it (`lsregister -f`). Confirmed in the LaunchServices
database — `flags: active exported untrusted`, `conforms to: public.data`. The
`untrusted` flag comes from the ad-hoc signature.

**`TaskDragPayload.contentType` is still `.json` on purpose.** A SwiftUI drag
cannot be exercised by a test — synthesised mouse drags do not start one, which
is precisely how the original bug shipped — so switching the type would trade a
hand-verified working drag for an unverified one.

To switch, one line in `Models/BoardDrop.swift`:

```swift
static let contentType = UTType(exportedAs: "com.carlossanabria.workloadtracker.task")
```

then **one manual test, in the bundled app** (`open dist/WorkloadTracker.app`,
not `swift run`): drag a card from To Do to In Progress and confirm it moves. If
nothing happens, revert — do not start debugging the drop handlers, because they
are not the problem. `DragTypeRegistrationTests` will need its expectation
updated in the same change.

## App icon

There is none, deliberately. Plan §11 calls for an icon authored in **Icon
Composer** (the only way to get a correct layered macOS 26 icon), which is a GUI
tool. The generic application icon is an honest placeholder; a fabricated one
would not be. To add it later: author `AppIcon.icns` in Icon Composer, drop it in
`Contents/Resources/` from `make-app.sh`, and add `CFBundleIconFile` /
`CFBundleIconName` to `Info.plist`.

## Not done in Phase 9

* The plan's optional `SMAppService` login-item toggle for the daemon. The
  LaunchAgent in `launchd/` already covers "daemon up without a terminal".
* Anything from plan §11 (Phase 8). *(§10, Phase 7 — the Gantt — landed after
  this file was written; see below.)*

## The Timeline (Phase 7, plan §10)

Swift Charts, no third-party dependency. `Models/Timeline.swift` is the pure,
tested part — what a bar is at each zoom, where the x-range comes from, how the
timestamp-less logs are marked — and `Views/TimelineView.swift` only draws it.

Four things about it are counter-intuitive enough to be worth writing down,
because each of them was found by **looking at a screenshot of a build whose
tests were already green**:

| Symptom on screen | Cause | Fix |
|---|---|---|
| Ten rows 65pt tall, bars like blocks | `chartYVisibleDomain` left to fill the plot | derive the visible-row count from the pane height ÷ a fixed `rowHeight` |
| `Mon 10  Mon 10  Tue 11  Tue 11` | `.automatic(desiredCount: 7)` over a 3-day window with a day-resolution label | stride on the calendar unit the label names |
| Sprint boundaries drawn with nothing naming them | a mark's `.top` annotation scrolls with `chartScrollableAxes(.vertical)` and is clipped | put the sprint (and "Now") labels on a **top x axis** — axis chrome does not scroll |
| Nine role swatches wrapped onto two lines | a legend duplicating the summary strip | legend carries only the hatch, the running timer and the sprint rule |

**The hatch** (`Design/HatchPattern.swift`) is an `ImagePaint` over a generated
`NSImage` tile, because `ChartContent.foregroundStyle(_:)` takes a `ShapeStyle`
and `ImagePaint` is the only built-in one that tiles an arbitrary drawing. It is
tinted per role, so the tile is cached by resolved sRGB components.

**Zoom** is `⌘+`/`⌘-` in the menu bar plus the toolbar's segmented control. The
shortcuts live in `App.swift` rather than on the picker: a shortcut attached to a
view only fires while that view has focus, and the picker takes focus away from
the chart.

**The empty state is a real state.** The Sprint facet defaults to the current
sprint and a sprint has no logged time on the morning it opens, so the default
view is an empty range. The axis, the sprint rules and the "Now" line still draw;
an overlay explains, and offers Show All Sprints / Clear All Filters.

## Tests

```bash
swift build            # debug
swift test             # 250 tests, 4 skipped (the skips need a live daemon)
```

`swift test` never touches `~/.workload_tracker.json` and makes no GitHub calls.
