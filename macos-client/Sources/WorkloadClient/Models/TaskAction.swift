import Foundation

/// What the **Task** menu offers for one task, on either surface.
///
/// Plan §11: "Menu items act on the selected card via `FocusedValueKey`. Every
/// menu action is also a card context menu." Through Phase 7 the Task menu only
/// knew about the recurrent shelf — a board card had a context menu of column
/// moves and no menu-bar presence at all, so `⌘T` on a selected board card did
/// nothing.
///
/// This is the pure table that closes that gap. It is deliberately **not** a
/// new set of actions: four of the five come straight from `ShelfAction`, which
/// already owns the gates in front of the two irreversible ones. The only
/// addition is `markDone`, which is the board's Done-column drop by another
/// route and goes through the same `beginClose` preview.
///
/// Two rules, both asserted in `TaskActionTests`:
///
/// * **`endSeries` is offered only for recurrent tasks, `markDone` only for
///   non-recurrent ones.** They are the same underlying `close_task` call, but
///   they are not the same decision: ending a series stops a recurrence and
///   closes a live issue with no reopen path, which is why it keeps its typed
///   confirmation and its exclusion from every keyboard shortcut.
/// * **The dangerous item is always last and always separated**, on both
///   surfaces, exactly as `ShelfAction.menu` already guarantees for the shelf.
enum TaskAction: Equatable, Sendable, Identifiable {
    /// One of the shelf's five, reused verbatim.
    case shelf(ShelfAction)
    /// Close a non-recurrent task through the §7.1 preview sheet.
    case markDone

    var id: String {
        switch self {
        case .shelf(let action): action.rawValue
        case .markDone: "markDone"
        }
    }

    var title: String {
        switch self {
        case .shelf(let action): action.title
        case .markDone: "Mark Done…"
        }
    }

    var systemImage: String {
        switch self {
        case .shelf(let action): action.systemImage
        case .markDone: "checkmark.circle"
        }
    }

    /// Whether the item is drawn in the destructive style and sits behind a
    /// divider at the foot of the menu.
    var isDestructive: Bool {
        switch self {
        case .shelf(let action): action == .endSeries
        case .markDone: false
        }
    }

    var isSeparatedInMenu: Bool {
        switch self {
        case .shelf(let action): action.isSeparatedInMenu
        case .markDone: true
        }
    }

    /// The table entry, or `nil` for an action that must never be invokable
    /// without reading its name.
    var shortcut: AppShortcut? {
        switch self {
        case .shelf(.startTimer): .toggleTimer
        case .shelf(.logTime): .logTime
        case .shelf(.openIssue): .openIssue
        case .shelf(.syncSprints): .syncSprints
        case .shelf(.endSeries): nil          // never — see ShelfAction
        case .markDone: .markDone
        }
    }

    /// Whether the action applies, and why not when it does not.
    func availability(for task: TrackerTask,
                      isTimerRunning: Bool) -> ShelfActionAvailability {
        switch self {
        case .shelf(let action):
            return action.availability(for: task, isTimerRunning: isTimerRunning)
        case .markDone:
            return task.status == .done
                ? .unavailable("This task is already done.")
                : .available
        }
    }

    // MARK: - The menus

    /// What a **recurrent** task offers: the shelf's five, unchanged.
    static let recurrentMenu: [TaskAction] = ShelfAction.menu.map(TaskAction.shelf)

    /// What a **board** task offers: the same first four, then Mark Done in the
    /// place `End Series` occupies on the shelf — last, separated, and reached
    /// through the close preview rather than acting immediately.
    static let boardMenu: [TaskAction] =
        ShelfAction.menu.filter { $0 != .endSeries }.map(TaskAction.shelf) + [.markDone]

    /// The menu for a task, chosen by its status rather than by which view
    /// asked — so a recurrent row can never be offered `markDone` and a board
    /// card can never be offered `endSeries`, whichever surface has focus.
    static func menu(for task: TrackerTask) -> [TaskAction] {
        task.status == .recurrent ? recurrentMenu : boardMenu
    }
}

// MARK: - Which surface the menu bar is acting on

/// Where the task the Task menu acts on came from.
///
/// The Board and the recurrent shelf each hold their own selection and both are
/// on screen at once, so "the selection" is ambiguous until you say which one
/// moved last. Tracking that is what stops a stale shelf row swallowing `⌘T`
/// while the user is arrowing around the board.
enum TaskSurface: String, Sendable, Equatable {
    case board
    case shelf
}
