import SwiftUI

/// The `NavigationSplitView` shell: sidebar, a detail pane that switches on the
/// sidebar selection, and a trailing inspector — all inside the connection-state
/// gate.
struct RootView: View {
    @Environment(Store.self) private var store
    /// Owned by the `App` scene so the View menu's `⌘1`/`⌘2`/`⌘3` and the
    /// sidebar's `List(selection:)` write the same value.
    @Binding var selection: SidebarSelection?
    /// The window's undo manager. Filter changes are registered with it, so the
    /// standard **Edit ▸ Undo** works on them without this app binding `⌘Z` and
    /// shadowing text editing everywhere. `nil` in previews and in tests.
    @Environment(\.undoManager) private var undoManager
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    @SceneStorage("sidebarSelection") private var storedSelection: String = "board"
    /// The whole `FilterState` as JSON (plan §8.1: "persists in `@SceneStorage`
    /// across launches"). `@SceneStorage` only stores primitives, so it
    /// round-trips through `FilterStateCodec` — the same shape the sidebar
    /// selection already uses.
    @SceneStorage("filterState") private var storedFilter: String = ""
    /// The Gantt's zoom (plan §10/§11: "`@SceneStorage` for selection, zoom
    /// level, shelf height and `FilterState`").
    @SceneStorage("timelineZoom") private var storedZoom: String = ""
    /// Whether the inspector was open (plan §11).
    @SceneStorage("inspectorVisible") private var storedInspector: Bool = false
    /// Whether the recurrent shelf was expanded. The shelf's *height* is not
    /// stored: `RecurrentShelfView.naturalHeight(rows:)` computes it from the
    /// row count, and persisting a stale number would reintroduce the Phase 5
    /// bug where the pane was drawn shorter than its contents.
    @SceneStorage("recurrentShelfVisible") private var storedShelf: Bool = true

    var body: some View {
        NavigationSplitView {
            SidebarView(selection: $selection)
        } detail: {
            // **A trailing pane, not `.inspector`.**
            //
            // Plan §11 asks for `.inspector`, and this was written with it
            // twice — attached to the `NavigationSplitView`, then attached to
            // the detail column. Neither worked, and the failures were only
            // ever visible in a screenshot:
            //
            //   * At the default window width the panel did not appear at all.
            //     The menu item existed and was enabled; toggling it changed no
            //     pixels. Dragged out to 2200pt by hand it appeared instantly.
            //   * With room to appear it was drawn clipped to a 97–130pt strip
            //     of its 460pt of content. `.inspectorColumnWidth(min: 300, …)`
            //     did not move it; nor did an explicit `.frame(minWidth: 300)`.
            //     The board's three columns are `maxWidth: .infinity` and take
            //     everything the split proposes, and the inspector column never
            //     got a share back.
            //   * Toggling the recurrent shelf while it was open killed the app
            //     in `_NSViewLayout` with an AppKit exception — three
            //     reproductions, same stack, on a freshly-cleared profile.
            //
            // The plan's *intent* is "a panel, not a modal, because it's
            // inspection of a selection". A fixed-width trailing pane is that
            // panel, and its width is arithmetic rather than negotiation, so
            // it cannot lose. `.inspector`'s extras — the toolbar affordance
            // and a user-draggable divider — are what this gives up, and the
            // View menu's ⌥⌘I replaces the first.
            HStack(spacing: 0) {
                VStack(spacing: 0) {
                    WarningBanners()
                    detail
                }
                .frame(minWidth: 520, minHeight: 420)

                if store.showsInspector {
                    Divider()
                    InspectorView()
                        .environment(store)
                        .frame(width: Self.inspectorWidth)
                        .transition(.move(edge: .trailing).combined(with: .opacity))
                }
            }
            .animation(reduceMotion ? .easeInOut(duration: 0.12) : .snappy(duration: 0.22),
                       value: store.showsInspector)
        }
        .navigationTitle(navigationTitle)
        .navigationSubtitle(store.currentSprint?.displayName ?? "")
        .task { store.start() }
        .onAppear {
            selection = Self.decode(storedSelection)
            store.selection = selection ?? .board
            store.showsInspector = storedInspector
            store.showsRecurrentShelf = storedShelf
            // Restoring *before* the first snapshot lands is what cancels the
            // current-sprint default, so a filter the user cleared stays clear.
            //
            // The scene value wins when there is one; `AppSettings` is the
            // fallback that survives the cases AppKit state restoration does
            // not (see `AppSettings.lastFilterState`).
            if let restored = FilterStateCodec.decode(storedFilter)
                ?? FilterStateCodec.decode(AppSettings.lastFilterState) {
                store.restoreFilter(restored)
            }
            if let zoom = TimelineZoom(rawValue: storedZoom) {
                store.timelineZoom = zoom
            }
        }
        .onChange(of: store.timelineZoom) { _, new in storedZoom = new.rawValue }
        .onChange(of: store.showsInspector) { _, new in storedInspector = new }
        .onChange(of: store.showsRecurrentShelf) { _, new in storedShelf = new }
        .onChange(of: selection) { _, new in
            storedSelection = Self.encode(new ?? .board)
            if let new { store.selection = new }
        }
        .onChange(of: store.filter) { old, new in
            let encoded = FilterStateCodec.encode(new)
            storedFilter = encoded
            AppSettings.lastFilterState = encoded
            registerFilterUndo(from: old, to: new)
            announceFilterChange()
        }
        // Plan §11: "Honour `accessibilityReduceMotion` (cross-fade instead of
        // card spring)". Applied at the root so it covers every pane rather
        // than each animated site remembering to ask.
        .animation(reduceMotion ? .easeInOut(duration: 0.12) : .snappy(duration: 0.22),
                   value: store.selection)
        .sheet(isPresented: shortcutHelpBinding) {
            ShortcutHelpView()
        }
    }

    /// The trailing pane's width. Wide enough for `owner/repo#1234` on one line
    /// in the details grid, narrow enough to leave the board its three columns
    /// at the 1440pt default size.
    static let inspectorWidth: CGFloat = 340

    private var shortcutHelpBinding: Binding<Bool> {
        Binding(get: { store.showsShortcutHelp },
                set: { store.showsShortcutHelp = $0 })
    }

    // MARK: - Undo

    /// Registers a filter change with the window's `UndoManager`.
    ///
    /// Plan §11 asks for "Undo `⌘Z` through `UndoManager` for **local-only**
    /// mutations". A filter is exactly that: nothing about it reaches the
    /// daemon, the data file or GitHub, so undoing one cannot lose work. No
    /// *write* is undoable, and deliberately so — there is no un-close and no
    /// un-`gh issue close`.
    ///
    /// Free-text edits are skipped. The search field has its own undo stack and
    /// registering per keystroke here would bury the facet changes under it.
    private func registerFilterUndo(from old: FilterState, to new: FilterState) {
        guard let undoManager, !undoManager.isUndoing, !undoManager.isRedoing else { return }
        guard old.withoutText != new.withoutText else { return }
        undoManager.registerUndo(withTarget: store) { target in
            _Concurrency.MainActor.assumeIsolated { target.applyFilter(old) }
        }
        undoManager.setActionName("Filter Change")
    }

    // MARK: - Accessibility

    /// Plan §11: "Active filters announced when they change, so a VoiceOver user
    /// isn't looking at a silently-reduced board."
    ///
    /// The wording is `Store.filterAnnouncement`, which is pure and asserted in
    /// tests — VoiceOver itself cannot be driven headlessly, so what is provable
    /// is the string, not the speech.
    private func announceFilterChange() {
        AccessibilityNotification.Announcement(store.filterAnnouncement).post()
    }

    // MARK: - Detail routing

    @ViewBuilder
    private var detail: some View {
        switch store.connection {
        case .connecting where store.snapshot == nil:
            ConnectingView()
        case .unreachable(let reason) where store.snapshot == nil:
            UnreachableView(reason: reason,
                            baseURL: AppSettings.baseURLString) {
                _Concurrency.Task { await store.refresh() }
            }
        case .failed(let code, let message) where store.snapshot == nil:
            DaemonFailureView(code: code, message: message) {
                _Concurrency.Task { await store.refresh() }
            }
        default:
            // A snapshot exists: render it, and let `WarningBanners` carry any
            // degraded/unreachable state rather than blanking the board.
            content
        }
    }

    @ViewBuilder
    private var content: some View {
        switch selection ?? .board {
        case .board:
            BoardView()
        case .timeline:
            TimelineView()
        case .overview:
            PhasePlaceholderView(
                title: "Overview",
                symbol: "chart.pie",
                summary: "Per-role and per-sprint totals across the whole dataset.",
                phase: "Arrives in a later phase of docs/plan-macos-app.md"
            )
        }
    }

    private var navigationTitle: String {
        switch selection ?? .board {
        case .board: "Board"
        case .timeline: "Timeline"
        case .overview: "Overview"
        }
    }

    // MARK: - SceneStorage codec

    /// `SidebarSelection` is `Codable`, but `@SceneStorage` only stores
    /// primitives, so it round-trips through a short string.
    private static func encode(_ selection: SidebarSelection) -> String {
        switch selection {
        case .board: "board"
        case .timeline: "timeline"
        case .overview: "overview"
        }
    }

    private static func decode(_ raw: String) -> SidebarSelection {
        // `role:…` was Phase 3's per-role board scope. Roles are a filter facet
        // now, so a scene saved by that build lands on the board.
        if raw.hasPrefix("role:") { return .board }
        switch raw {
        case "timeline": return .timeline
        case "overview": return .overview
        default: return .board
        }
    }
}

/// `FilterState` ⇄ the single `String` `@SceneStorage` can hold.
///
/// Its own type rather than a pair of `RootView` methods so the round trip is
/// unit-testable — a codec that silently fails to decode would quietly retire
/// filter persistence with nothing going red.
enum FilterStateCodec {
    /// An **empty** filter still encodes to JSON, not to `""`. "The user cleared
    /// everything" and "nothing has ever been stored" must stay distinguishable,
    /// or clearing the Sprint facet would be undone by the default seed on the
    /// next launch.
    static func encode(_ state: FilterState) -> String {
        guard let data = try? JSONEncoder().encode(state) else { return "" }
        return String(decoding: data, as: UTF8.self)
    }

    /// `nil` for "nothing stored", which is different from "an empty filter was
    /// stored" — only the latter should cancel the current-sprint default.
    static func decode(_ raw: String) -> FilterState? {
        guard !raw.isEmpty else { return nil }
        return try? JSONDecoder().decode(FilterState.self, from: Data(raw.utf8))
    }
}

// MARK: - Help

/// **Help ▸ Keyboard Shortcuts**, rendered from `AppShortcut` itself.
///
/// Not a hand-written list: a documentation table that can disagree with the
/// bindings is worse than none, and this one cannot — it is the same table the
/// menus register from and the collision test reads.
struct ShortcutHelpView: View {
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Keyboard Shortcuts").font(.title3.weight(.semibold))
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    group("View", .viewMenu)
                    group("Task", .taskMenu)
                    group("Board", .boardKeyHandler,
                          footnote: "Held by the board itself, not the menu bar. "
                          + "Arrow keys alone move the selection.")
                }
            }
            .frame(maxHeight: 420)
            HStack {
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(width: 460)
    }

    @ViewBuilder
    private func group(_ title: String, _ owner: AppShortcut.Owner,
                       footnote: String? = nil) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.headline)
            Grid(alignment: .leadingFirstTextBaseline,
                 horizontalSpacing: 16, verticalSpacing: 8) {
                ForEach(AppShortcut.table(for: owner)) { shortcut in
                    GridRow {
                        Text(shortcut.display)
                            .font(.body.monospaced())
                            .gridColumnAlignment(.leading)
                            .frame(minWidth: 48, alignment: .leading)
                        Text(shortcut.title)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("\(shortcut.title), \(shortcut.display)")
                }
            }
            if let footnote {
                Text(footnote).font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}
