import SwiftUI

/// The **Task** menu (plan §9, and the menu-bar half of §11's Task group).
///
/// Through Phase 7 it acted only on the recurrent shelf, so a selected *board*
/// card had no menu-bar presence at all and `⌘T` on it did nothing. It now acts
/// on whichever selection moved last — see `Store.menuTask` and `TaskSurface` —
/// and its items come from `TaskAction.menu(for:)`, the same table the board's
/// card context menu, the shelf's row context menu and the inspector's button
/// row all read. Those four surfaces cannot present a different item set, a
/// different order or a different gate, because there is only one table.
///
/// Shortcuts come from `AppShortcut` via `TaskAction.shortcut`, and
/// **`End Series` has none**: a shortcut is a way to invoke an action without
/// reading its name, which is exactly the accident its typed confirmation
/// exists to prevent. That exclusion is asserted in `ShelfActionTests` and
/// again in `TaskActionTests`, not just observed here.
struct TaskMenuCommands: View {
    @Environment(Store.self) private var store
    @FocusedValue(\.trackerTask) private var focusedTask

    /// The focused scene value when there is one, else the store's own idea of
    /// the selection. The fallback matters because `focusedSceneValue` is
    /// cleared whenever the key window's focus leaves the publishing view — a
    /// sheet, or the Settings window — and a Task menu that empties out while a
    /// card is plainly still selected reads as a bug.
    private var task: TrackerTask? { focusedTask ?? store.menuTask }

    var body: some View {
        if let task {
            Text(heading(for: task))
            Divider()
            ForEach(TaskAction.menu(for: task)) { action in
                if action.isSeparatedInMenu { Divider() }
                item(action, task: task)
            }
        } else {
            Text("No task selected")
            Divider()
            // The one action that needs no selection: whatever is running can
            // always be stopped.
            Button("Stop Timer") {
                _Concurrency.Task { await store.stopTimer() }
            }
            .shortcut(.toggleTimer)
            .disabled(store.snapshot?.activeTimer == nil)
        }
    }

    private func heading(for task: TrackerTask) -> String {
        (task.status == .recurrent ? "Recurrent: " : "") + task.title
    }

    @ViewBuilder
    private func item(_ action: TaskAction, task: TrackerTask) -> some View {
        let availability = action.availability(
            for: task, isTimerRunning: store.isTimerRunning(on: task))
        // `startTimer` is the one item whose *title* flips with state: with a
        // timer running on this task, ⌘T stops it rather than being dead.
        let isRunning = store.isTimerRunning(on: task)
        let stops = action == .shelf(.startTimer) && isRunning
        let button = Button(role: action.isDestructive ? .destructive : nil) {
            _Concurrency.Task {
                if stops {
                    await store.stopTimer()
                } else {
                    await store.perform(action, on: task)
                }
            }
        } label: {
            Label(stops ? "Stop Timer" : action.title,
                  systemImage: stops ? "stop.circle" : action.systemImage)
        }
        .disabled(!stops && !availability.isAvailable)

        if let shortcut = action.shortcut {
            button.shortcut(shortcut)
        } else {
            button
        }
    }
}

// MARK: - Focused value plumbing

/// The task the menu bar acts on.
///
/// Renamed from `shelfTask` in Phase 8: the same key now carries a board card
/// as well, chosen by `Store.menuTask`. One key rather than two, because two
/// would need a precedence rule in the *view* — and the rule ("whichever
/// selection moved last") is a fact about the store, not about the menu.
struct TrackerTaskFocusedValueKey: FocusedValueKey {
    typealias Value = TrackerTask
}

extension FocusedValues {
    var trackerTask: TrackerTask? {
        get { self[TrackerTaskFocusedValueKey.self] }
        set { self[TrackerTaskFocusedValueKey.self] = newValue }
    }
}
