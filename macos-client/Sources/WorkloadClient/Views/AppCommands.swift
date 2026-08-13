import SwiftUI

/// The **View** menu (plan §11).
///
/// Everything here reads its shortcut from `AppShortcut`, so a new item cannot
/// invent a binding the collision check has not seen. Three items that used to
/// live somewhere else have moved here:
///
/// * The **view switch** (`⌘1`/`⌘2`/`⌘3`) is new. The sidebar was the only way
///   to change pane.
/// * The **shelf toggle** kept its `⌥⌘R` but moved its *registration* here from
///   the Board's toolbar `Toggle`. The toolbar control stays — it is the visible
///   affordance — but it no longer carries the shortcut, because a shortcut
///   registered twice is a collision with itself, and the toolbar copy only
///   existed while the Board was on screen.
/// * The **inspector** toggle (`⌥⌘I`) is new, and is the only way to reach the
///   panel from the keyboard.
struct ViewCommands: View {
    @Environment(Store.self) private var store
    @Binding var selection: SidebarSelection?

    var body: some View {
        Button(AppShortcut.showBoard.title) { select(.board) }
            .shortcut(.showBoard)
        Button(AppShortcut.showTimeline.title) { select(.timeline) }
            .shortcut(.showTimeline)
        Button(AppShortcut.showOverview.title) { select(.overview) }
            .shortcut(.showOverview)

        Divider()

        Toggle(AppShortcut.toggleInspector.title, isOn: inspectorBinding)
            .shortcut(.toggleInspector)
        Toggle(AppShortcut.toggleShelf.title, isOn: shelfBinding)
            .shortcut(.toggleShelf)
            // The shelf lives on the Board. Leaving it enabled on the Timeline
            // would let ⌥⌘R silently change a pane the user cannot see.
            .disabled(store.selection != .board)

        Divider()

        Button(AppShortcut.refresh.title) {
            _Concurrency.Task { await store.refresh() }
        }
        .shortcut(.refresh)

        Button(AppShortcut.clearFilters.title) { store.clearFilters() }
            .shortcut(.clearFilters)
            .disabled(!store.isFiltering)

        Divider()

        // Plan §10's `⌘+`/`⌘-`. In the menu bar rather than on the Timeline's
        // segmented control, because a shortcut attached to a view only fires
        // while that view is on screen *and* focused, and the picker takes focus
        // away from the chart.
        Button(AppShortcut.zoomIn.title) { store.zoomTimeline(in: true) }
            .shortcut(.zoomIn)
            .disabled(store.selection != .timeline || !store.canZoomTimelineIn)
        Button(AppShortcut.zoomOut.title) { store.zoomTimeline(in: false) }
            .shortcut(.zoomOut)
            .disabled(store.selection != .timeline || !store.canZoomTimelineOut)

        Divider()

        // Timeframe navigation. **`⌥←`/`⌥→`, deliberately not `⌘←`/`⌘→`** — the
        // Board binds those to *moving the selected card* between columns
        // (`BoardView.handle(_:)`), and a menu-bar shortcut wins over a view's
        // `onKeyPress`, so reusing them would silently retire the board's
        // keyboard move. `AppShortcut.moveCardLeft`/`moveCardRight` are in the
        // table for exactly that reason, though no menu binds them.
        Button(AppShortcut.previousPeriod.title) { store.stepTimeline(.previous) }
            .shortcut(.previousPeriod)
            .disabled(store.selection != .timeline || !store.canStepTimeline(.previous))
        Button(AppShortcut.nextPeriod.title) { store.stepTimeline(.next) }
            .shortcut(.nextPeriod)
            .disabled(store.selection != .timeline || !store.canStepTimeline(.next))
        Button(AppShortcut.today.title) { store.timelineToToday() }
            .shortcut(.today)
            .disabled(store.selection != .timeline || !store.canReturnToToday)
    }

    private func select(_ new: SidebarSelection) {
        selection = new
        store.selection = new
    }

    private var inspectorBinding: Binding<Bool> {
        Binding(get: { store.showsInspector },
                set: { store.showsInspector = $0 })
    }

    private var shelfBinding: Binding<Bool> {
        Binding(get: { store.showsRecurrentShelf },
                set: { store.showsRecurrentShelf = $0 })
    }
}

/// The **Edit** menu's addition: `⌘F` focuses the filter field (plan §11).
///
/// Added *after* the standard text-editing group rather than replacing it, so
/// Cut/Copy/Paste and — importantly — **Undo/Redo** stay AppKit's. Those two are
/// routed through the responder chain: a text field undoes its own typing, and
/// the window's `UndoManager` undoes a filter change, which `RootView`
/// registers. Binding our own `⌘Z` would have shadowed text editing everywhere.
struct FindCommands: View {
    @Environment(Store.self) private var store

    var body: some View {
        Button(AppShortcut.findFilter.title) { store.focusSearchField() }
            .shortcut(.findFilter)
    }
}
