import SwiftUI

/// The **Task** menu (plan §9, and the menu-bar half of §11's Task group).
///
/// It acts on the selected recurrent-shelf row, published as a focused scene
/// value by `BoardView`. Items come from `ShelfActionMenu`, so the menu bar and
/// the row's context menu are literally the same view — they cannot present a
/// different item set, a different order, or a different gate.
///
/// The one thing the menu bar adds is keyboard shortcuts, and it adds them from
/// `ShelfAction.allowsKeyboardShortcut`. **`End Series` is excluded**: a
/// shortcut is a way to invoke an action without reading its name, which is
/// exactly the accident its typed confirmation exists to prevent. That
/// exclusion is asserted in `ShelfActionTests`, not just observed here.
struct TaskMenuCommands: View {
    @Environment(Store.self) private var store
    @FocusedValue(\.shelfTask) private var shelfTask

    var body: some View {
        if let task = shelfTask {
            Text("Recurrent: \(task.title)")
            Divider()
            ForEach(ShelfAction.menu) { action in
                if action.isSeparatedInMenu { Divider() }
                item(action, task: task)
            }
        } else {
            Text("No recurrent task selected")
            Divider()
            Button("Stop Timer") {
                _Concurrency.Task { await store.stopTimer() }
            }
            .keyboardShortcut("t", modifiers: .command)
            .disabled(store.snapshot?.activeTimer == nil)
        }
    }

    @ViewBuilder
    private func item(_ action: ShelfAction, task: TrackerTask) -> some View {
        let availability = action.availability(
            for: task, isTimerRunning: store.isTimerRunning(on: task))
        let button = Button(role: action == .endSeries ? .destructive : nil) {
            _Concurrency.Task { await store.perform(action, on: task) }
        } label: {
            Label(action.title, systemImage: action.systemImage)
        }
        .disabled(!availability.isAvailable)

        if let shortcut = Self.shortcut(for: action), action.allowsKeyboardShortcut {
            button.keyboardShortcut(shortcut.key, modifiers: shortcut.modifiers)
        } else {
            button
        }
    }

    /// Shortcuts matching the TUI bindings in §11. `endSeries` has none, and
    /// `ShelfAction.allowsKeyboardShortcut` is checked at the call site too, so
    /// adding one here by accident still would not attach it.
    private static func shortcut(for action: ShelfAction)
        -> (key: KeyEquivalent, modifiers: EventModifiers)? {
        switch action {
        case .startTimer: ("t", .command)
        case .logTime: ("l", .command)
        case .openIssue: ("g", .command)
        case .syncSprints: ("s", [.shift, .command])
        case .endSeries: nil
        }
    }
}

// MARK: - Focused value plumbing

/// The recurrent-shelf row the menu bar acts on.
struct ShelfTaskFocusedValueKey: FocusedValueKey {
    typealias Value = TrackerTask
}

extension FocusedValues {
    var shelfTask: TrackerTask? {
        get { self[ShelfTaskFocusedValueKey.self] }
        set { self[ShelfTaskFocusedValueKey.self] = newValue }
    }
}
