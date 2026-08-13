import SwiftUI

/// **Every keyboard shortcut the app registers, in one table.**
///
/// Plan §11 asks for "a real menu bar, since every TUI binding needs a
/// discoverable home", and the risk of granting that wish piecemeal is a
/// shortcut that silently shadows one that already worked. That failure is
/// invisible: nothing warns, the older binding simply stops firing. It has a
/// precedent in this codebase — `App.swift` records why the Timeline's
/// timeframe navigation is `⌥←`/`⌥→` rather than `⌘←`/`⌘→`, which the Board
/// already binds to *moving a card*.
///
/// So the table is the source of truth and the menus read from it. Two
/// properties follow, both asserted in `AppShortcutTests`:
///
/// 1. **No two entries share a (key, modifiers) pair.** Including the two that
///    are not menu items at all — `⌘←`/`⌘→` live in `BoardView.onKeyPress`, and
///    a menu-bar shortcut wins over a view's key handler, so they have to be in
///    the collision check even though nothing here registers them.
/// 2. **Nothing dangerous gets one.** `ShelfAction.endSeries` has no entry, and
///    `allowsKeyboardShortcut` is checked at the call site as well.
///
/// `Undo`/`Redo` are deliberately absent: they are AppKit's, routed through the
/// responder chain to whatever has focus (a text field's own undo stack, or the
/// window's `UndoManager`, which `RootView` registers filter changes with). A
/// `⌘Z` of ours would shadow text editing in the filter field and in Settings.
enum AppShortcut: String, CaseIterable, Identifiable, Sendable {

    // MARK: View

    case showBoard
    case showTimeline
    case showOverview
    case refresh
    case clearFilters
    case findFilter
    case toggleInspector
    case toggleShelf
    case zoomIn
    case zoomOut
    case previousPeriod
    case nextPeriod
    case today

    // MARK: Task

    /// One slot, not two: Start and Stop are the same menu item in two states,
    /// so they are one entry and can never collide with each other.
    case toggleTimer
    case logTime
    case openIssue
    case syncSprints
    case markDone

    // MARK: Board key handler — registered by no menu

    case moveCardLeft
    case moveCardRight

    var id: String { rawValue }

    // MARK: - The table

    /// Which surface registers the shortcut. `boardKeyHandler` entries are
    /// listed so the collision check sees them; nothing in a menu binds them.
    enum Owner: String, Sendable {
        case viewMenu
        case taskMenu
        case boardKeyHandler
    }

    var owner: Owner {
        switch self {
        case .showBoard, .showTimeline, .showOverview, .refresh, .clearFilters,
             .findFilter, .toggleInspector, .toggleShelf, .zoomIn, .zoomOut,
             .previousPeriod, .nextPeriod, .today:
            return .viewMenu
        case .toggleTimer, .logTime, .openIssue, .syncSprints, .markDone:
            return .taskMenu
        case .moveCardLeft, .moveCardRight:
            return .boardKeyHandler
        }
    }

    var key: KeyEquivalent {
        switch self {
        case .showBoard: "1"
        case .showTimeline: "2"
        case .showOverview: "3"
        case .refresh: "r"
        case .clearFilters: "k"
        case .findFilter: "f"
        case .toggleInspector: "i"
        case .toggleShelf: "r"
        case .zoomIn: "+"
        case .zoomOut: "-"
        case .previousPeriod: .leftArrow
        case .nextPeriod: .rightArrow
        case .today: "t"
        case .toggleTimer: "t"
        case .logTime: "l"
        case .openIssue: "g"
        case .syncSprints: "s"
        case .markDone: "d"
        case .moveCardLeft: .leftArrow
        case .moveCardRight: .rightArrow
        }
    }

    var modifiers: EventModifiers {
        switch self {
        case .showBoard, .showTimeline, .showOverview, .refresh, .findFilter,
             .zoomIn, .zoomOut, .toggleTimer, .logTime, .openIssue,
             .moveCardLeft, .moveCardRight:
            return .command
        case .clearFilters, .syncSprints, .markDone:
            return [.shift, .command]
        case .toggleInspector, .toggleShelf, .today, .previousPeriod, .nextPeriod:
            // `previousPeriod`/`nextPeriod` are ⌥-only on purpose — see the
            // note in `App.swift`. Everything else here is ⌥⌘.
            return self == .previousPeriod || self == .nextPeriod
                ? .option : [.option, .command]
        }
    }

    /// The menu item's title. Items whose title flips with state (the timer)
    /// override this at the call site.
    var title: String {
        switch self {
        case .showBoard: "Board"
        case .showTimeline: "Timeline"
        case .showOverview: "Overview"
        case .refresh: "Refresh Snapshot"
        case .clearFilters: "Clear All Filters"
        case .findFilter: "Find"
        case .toggleInspector: "Inspector"
        case .toggleShelf: "Recurrent Shelf"
        case .zoomIn: "Zoom In"
        case .zoomOut: "Zoom Out"
        case .previousPeriod: "Previous Period"
        case .nextPeriod: "Next Period"
        case .today: "Today"
        case .toggleTimer: "Start Timer"
        case .logTime: "Log Time…"
        case .openIssue: "Open Issue"
        case .syncSprints: "Sync Sprints…"
        case .markDone: "Mark Done…"
        case .moveCardLeft: "Move Card Left"
        case .moveCardRight: "Move Card Right"
        }
    }

    // MARK: - Collision detection

    /// The pair a collision is defined on. Two entries with equal signatures
    /// would fight, and the loser is silent.
    var signature: String {
        "\(modifiers.rawValue)|\(key.character)"
    }

    /// `⇧⌘K`-style, for documentation and `.help` strings. Order matches the
    /// macOS convention (⌃⌥⇧⌘).
    var display: String {
        var out = ""
        if modifiers.contains(.control) { out += "⌃" }
        if modifiers.contains(.option) { out += "⌥" }
        if modifiers.contains(.shift) { out += "⇧" }
        if modifiers.contains(.command) { out += "⌘" }
        switch key {
        case .leftArrow: out += "←"
        case .rightArrow: out += "→"
        case .upArrow: out += "↑"
        case .downArrow: out += "↓"
        default: out += String(key.character).uppercased()
        }
        return out
    }

    /// Every entry, grouped by owner — the table this phase reports.
    static func table(for owner: Owner) -> [AppShortcut] {
        allCases.filter { $0.owner == owner }
    }
}

// MARK: - Applying one

extension View {
    /// Binds a menu item to its table entry, so a menu can never invent a
    /// shortcut the collision check has not seen.
    func shortcut(_ shortcut: AppShortcut) -> some View {
        keyboardShortcut(shortcut.key, modifiers: shortcut.modifiers)
    }
}
