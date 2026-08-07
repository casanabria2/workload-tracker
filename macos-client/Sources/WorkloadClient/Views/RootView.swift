import SwiftUI

/// The `NavigationSplitView` shell: sidebar plus a detail pane that switches on
/// the sidebar selection, wrapped in the connection-state gate.
struct RootView: View {
    @Environment(Store.self) private var store
    @SceneStorage("sidebarSelection") private var storedSelection: String = "board"
    /// The whole `FilterState` as JSON (plan §8.1: "persists in `@SceneStorage`
    /// across launches"). `@SceneStorage` only stores primitives, so it
    /// round-trips through `FilterStateCodec` — the same shape the sidebar
    /// selection already uses.
    @SceneStorage("filterState") private var storedFilter: String = ""
    @State private var selection: SidebarSelection? = .board

    var body: some View {
        NavigationSplitView {
            SidebarView(selection: $selection)
        } detail: {
            VStack(spacing: 0) {
                WarningBanners()
                detail
            }
            .frame(minWidth: 700, minHeight: 420)
        }
        .navigationTitle(navigationTitle)
        .navigationSubtitle(store.currentSprint?.displayName ?? "")
        .task { store.start() }
        .onAppear {
            selection = Self.decode(storedSelection)
            // Restoring *before* the first snapshot lands is what cancels the
            // current-sprint default, so a filter the user cleared stays clear.
            //
            // The scene value wins when there is one; `AppSettings` is the
            // fallback that actually survives a relaunch of this un-bundled
            // executable (see `AppSettings.lastFilterState`).
            if let restored = FilterStateCodec.decode(storedFilter)
                ?? FilterStateCodec.decode(AppSettings.lastFilterState) {
                store.restoreFilter(restored)
            }
        }
        .onChange(of: selection) { _, new in
            storedSelection = Self.encode(new ?? .board)
            if let new { store.selection = new }
        }
        .onChange(of: store.filter) { _, new in
            let encoded = FilterStateCodec.encode(new)
            storedFilter = encoded
            AppSettings.lastFilterState = encoded
        }
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
            PhasePlaceholderView(
                title: "Timeline",
                symbol: "chart.bar.xaxis",
                summary: "A zoomable Gantt of logged time — one bar per session at "
                + "Day/Week, one per task at Sprint/Quarter, with the "
                + "timestamp-less logs drawn as approximate rather than invented.",
                phase: "Arrives in Phase 7 of docs/plan-macos-app.md"
            )
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
