# Plan: a cmux custom sidebar for current-sprint tasks

Status: **researched + validated prototype, not yet implemented.**

Goal: a sidebar in cmux listing the current sprint's workload-tracker tasks,
where each row can (1) open the task's GitHub issue in a cmux browser split,
(2) create a cmux workspace named after the task, and (3) focus the workspace
that already belongs to the task.

---

## 1. What cmux actually gives us

cmux ships a **custom sidebars** feature (beta, on by default). It is not a
plugin API — it is a *runtime-interpreted SwiftUI-style file*:

```
~/.config/cmux/sidebars/<name>.swift     # interpreted Swift (preferred)
~/.config/cmux/sidebars/<name>.json      # declarative, static only
```

The file is a single top-level view expression (no `struct`, no `var body`).
It renders as real native SwiftUI in the sidebar, hot-reloads on save, and taps
can dispatch real cmux commands. Verified on **cmux 0.64.20 (100)**.

Docs: `cmux docs sidebars` · https://cmux.com/docs/custom-sidebars ·
raw: `https://raw.githubusercontent.com/manaflow-ai/cmux/main/docs/custom-sidebars.md`

Surfacing it:

| Command | Effect |
| --- | --- |
| `cmux sidebar validate [name]` | Parse-check; safe, read-only |
| `cmux sidebar open <name>` | Open as a normal Bonsplit pane tab (iteration loop) |
| `cmux sidebar select <name>` | Activate it in the **left** sidebar |
| `cmux sidebar reload [name]` | Validate then reload every valid sidebar |

Also selectable by right-clicking the sidebar toggle button. The filename is the
menu label, so use short kebab-case.

### 1.1 The hard constraint

The interpreter has **no filesystem, shell, or network access**. The only live
bindings are:

- `workspaces[]` — `id`, `title`, `selected`, `pinned`, `index`, `directory`,
  `ports`, `unread`, `tabs`, and *when present* `description`, `color`,
  `branch`/`dirty`, `pr`/`prs`, `progress`, `latestMessage`, `latestPrompt`,
  `latestAt`, `remote`
- `tabs[]` (per workspace), `workspaceCount`, `selectedTitle`, `selectedId`,
  `unreadTotal`
- `clock` — `{ time, hour, minute, second, weekday, epoch }`

The docs are explicit: *"data cmux doesn't track (custom domain collections)
won't appear."* There is no hook to inject `~/.workload_tracker.json`.

**Therefore: the task list must be code-generated into the `.swift` file.**
We bake tracker data in as literals and regenerate on change. The file
hot-reloads, so this is effectively a push-based data feed.

This is the central architectural decision, and it is forced.

### 1.2 What stays live

Baking is only half the picture. The *cmux* half stays live, which is what makes
the three actions work:

- Workspace matching is resolved **at render time** against the live
  `workspaces` binding — so "does a workspace exist for this task yet?" is
  always current, and the row renders either **Go** or **New** accordingly.
- The sidebar re-evaluates ~1×/sec, so a baked `started_at` epoch plus
  `clock.epoch` yields a **live ticking timer** for the active task without
  regenerating anything.

---

## 2. Task ↔ workspace correlation

The weak point of any design here. Title matching alone is not enough — real
drift already exists in this setup:

| cmux workspace | tracker task |
| --- | --- |
| `Build Partner Demo Kit` | `Build AMER Partner Demo Kit` |
| `Infra Layer Ochestration` (sic) | Brokkr infra task |

Chosen strategy — **stamp the workspace `description` with a stable key**:

1. When the sidebar creates a workspace it passes
   `description: "wt:<task_id>"`. `workspace.create` accepts `description`, and
   `description` is exposed to sidebar bindings.
2. Matching is then an exact `w.description.contains("wt:<task_id>")`.
3. Fallback for workspaces created *before* the sidebar existed: normalized
   (lowercased) exact title match.

`extension.sidebar.snapshot` confirms `description` is currently `null` on every
existing workspace, so nothing is being clobbered.

Rejected alternatives:

- **`directory`/`cwd` matching** — clean and invisible, but current sprint tasks
  have no `local_folder`, and creating folders as a side effect of rendering a
  sidebar is worse than a description string.
- **Workspace env vars** — persist correctly but are *not* exposed to sidebar
  bindings.
- **`cmux todo` / `set-status`** — per-workspace state, also not in the binding
  list.
- **One workspace per task, always** — pollutes the workspace list with tasks
  that are not being worked on.

If the visible `wt:<id>` string is unwanted in the sidebar, either set
`sidebar.showWorkspaceDescription: false`, or make the description
human-readable and still matchable, e.g. `"#6239 · wt:20260728120000aaaa"`.

---

## 3. Action wiring (verified method names)

`cmux(...)` in a sidebar dispatches through the same dispatcher as the CLI, so
any of the 255 methods from `cmux capabilities` is reachable.

| Requirement | Call |
| --- | --- |
| Open GitHub issue in cmux browser | `cmux("browser.open_split", url: "https://github.com/owner/repo/issues/N")` |
| Create workspace named after task | `cmux("workspace.create", name: <title>, description: "wt:<id>")` |
| Focus the task's workspace | `cmux("workspace.select", workspace_id: workspaces[i].id)` |

Notes:

- Prefer `browser.open_split` over `file.open`. `file.open` routes URLs through
  the app's file-open path, and with `browser.hostsToOpenInEmbeddedBrowser`
  currently empty a GitHub URL may open in the **external** default browser.
  `browser.open_split` is unambiguously the embedded cmux browser.
- `workspace.create` also accepts `cwd`, `command`, `workspace_env`, `group_id`.
  `command:` is the hook for a future "create workspace **and** start the timer"
  action (`command: "wt start <id>"`), since `cmux()` cannot run arbitrary shell.
- **These methods are lenient**: called with missing or wrong-typed params they
  silently fall back to defaults and still act (`workspace.create {}` creates an
  untitled workspace). There is no validation error to catch a typo'd param
  name — a wrong name degrades silently. Verify every action by tapping it.

---

## 4. Implementation

### 4.1 Files

| Path | Purpose |
| --- | --- |
| `cmux_sidebar.py` | New module: reads tracker data, renders the `.swift` file. Mirrors the existing single-purpose-module split (`arc_browser.py`, `iterm_manager.py`, `browser_window.py`). |
| `wt.py` | New `cmux-sidebar` subcommand; optional generate-on-`save()` hook. |
| `_wt` | Zsh completion for the new command. |
| `~/.config/cmux/sidebars/wt-sprint.swift` | Generated output. Never hand-edited. |

### 4.2 Generator contract

```
wt cmux-sidebar [--out PATH] [--no-reload] [--dry-run]
```

1. `load()`, resolve the current sprint via `get_current_sprint(data)`, falling
   back to `get_cached_sprints(data)` so the common path needs **no network
   call**.
2. Select tasks where `sprint_id == current`, `status != "done"`, and
   `cross_sprint_parent` is unset. Sort `inprogress` → `todo` → `recurrent`,
   then by title. Cap the list (the docs warn against long lists; ~25 is ample).
3. Per task emit: `key` (`wt:<id>`), `title`, `issue`, `url`, `role`, `status`,
   `hours` (`task_logged_mins`), and for the active timer task
   `active` + `startedAt` epoch.
4. Derive the issue URL from `github_issue` (`owner/repo#N` →
   `https://github.com/owner/repo/issues/N`).
5. Write the file, then `cmux sidebar reload wt-sprint` unless `--no-reload`.

**Escaping matters.** Task titles are free text and land inside Swift string
literals in generated code. Escape `\` and `"`, and strip newlines. Titles in
this dataset already contain characters like `/` and `-`; a stray quote would
silently break the whole sidebar. Emit via a single escape helper, and let
`cmux sidebar validate` gate the write in `--dry-run`.

### 4.3 Refresh triggers

Baked data goes stale, so regeneration needs to be driven:

1. **`wt cmux-sidebar`** — explicit, and the primitive the others call.
2. **Tail of `wt.save()`**, guarded by a new `config.cmux_sidebar_enabled`
   flag. CLI, TUI, and MCP all funnel through `save()`, so this covers every
   local mutation. Wrap in try/except — the tracker must never fail because a
   sidebar could not be written (same discipline as the Arc/Safari hooks).
3. **A launchd agent every ~2 min** as the safety net for changes arriving from
   the *other* Mac over iCloud, which produce no local `save()`.

Live elapsed time needs no trigger — it is computed from `clock.epoch` minus the
baked `startedAt`.

### 4.4 Phasing

- **Phase 1** — `cmux_sidebar.py` + `wt cmux-sidebar`, generating the file with
  the three actions. Verify with `cmux sidebar validate` then
  `cmux sidebar open wt-sprint`, and tap each of the three buttons.
- **Phase 2** — generate-on-`save()` behind the config flag; launchd agent.
- **Phase 3** — polish: role colour dots, live elapsed for the active task,
  per-row `.contextMenu`, hours-vs-GitHub drift indicator.
- **Phase 4** (optional) — timer control via `workspace.create(command:)`;
  group task workspaces under a cmux workspace group.

---

## 5. Validation already done

A full prototype was written to `~/.config/cmux/sidebars/wt-sprint.swift` with
real Sprint 105 data (3 open tasks) and **passes `cmux sidebar validate`**:

```
OK wt-sprint [swift] /Users/carlos/.config/cmux/sidebars/wt-sprint.swift
1 valid, 0 invalid.
```

It exercises every construct the generator depends on: a baked
array-of-dictionaries literal, `for i in 0..<tasks.count` with `let t = tasks[i]`,
`t["key"]` subscripts, untyped `func` helpers containing loops with early
`return`, matching against the live `workspaces` binding, conditional
**Go**/**New** rendering, all three `cmux(...)` actions, `.contextMenu`,
`Circle().fill(...)` status dots, and SF Symbols.

### Caveats on that prototype

- **`validate` is parse-level only.** Runtime errors render *inline in the
  sidebar*, and there is no programmatic way to read a rendered custom sidebar
  (`extension.sidebar.snapshot` returns the live workspace **data context**, not
  a render of your file; `read-screen` only works on terminal surfaces). So
  Phase 1 must end with a human opening the pane and tapping the buttons.
- Two constructs are within the documented subset but worth watching on first
  render: the 8-digit hex `"#00000000"` passed to `.background(...)`, and
  loops-with-early-`return` inside `func`.
- **Your IDE will flag the generated file.** SourceKit reports errors like
  `Cannot find 'VStack' in scope` and `Expected ':' following argument label`
  because this is cmux's interpreted dialect, not compilable Swift (untyped
  `func` params, injected globals, bare top-level view). Expected and harmless;
  the file must still be named `.swift` for cmux to prefer it. Consider a
  generated-file header (already present) and excluding the path from any Swift
  tooling.

### Prerequisites

- Custom sidebars beta enabled: Settings → Custom Sidebars
  (`customSidebars.beta.enabled`). On by default.
- `customSidebars.renderer` — leave `"inProcess"` (default) for hover/focus/
  keyboard support. `"remote"` is the out-of-process containment lane for
  untrusted sources; not needed for a self-generated file.
- `cmux config get` only exposes font-size keys, so these are set via Settings
  or by editing `~/.config/cmux/cmux.json` (back it up to a timestamped `.bak`
  first, then `cmux reload-config`).

---

## 6. Open questions

1. Show recurrent tasks in the sprint list, or only non-recurrent ones? (Sprint
   105 currently has none — `wt new-recurrent` has not been run for it.)
2. Should tapping **New** also start the tracker timer for that task?
3. Left sidebar (`sidebar select`, replaces the normal workspace list) or a
   right-side pane (`sidebar open`, coexists)? The pane keeps the native
   workspace list visible and is the better default.
4. Is a visible `wt:<id>` in the workspace description acceptable, or should it
   be hidden / made human-readable?
