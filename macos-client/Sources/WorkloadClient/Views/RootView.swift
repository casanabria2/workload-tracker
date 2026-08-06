import SwiftUI

/// The `NavigationSplitView` shell: sidebar plus a detail pane that switches on
/// the sidebar selection, wrapped in the connection-state gate.
struct RootView: View {
    @Environment(Store.self) private var store
    @SceneStorage("sidebarSelection") private var storedSelection: String = "board"
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
        .onAppear { selection = Self.decode(storedSelection) }
        .onChange(of: selection) { _, new in
            storedSelection = Self.encode(new ?? .board)
            if let new { store.selection = new }
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
            BoardView(roleFilter: nil)
        case .role(let id):
            BoardView(roleFilter: id)
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
        case .role(let id):
            store.roles.first { $0.id == id }?.displayName ?? id
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
        case .role(let id): "role:\(id)"
        }
    }

    private static func decode(_ raw: String) -> SidebarSelection {
        if raw.hasPrefix("role:") { return .role(String(raw.dropFirst(5))) }
        switch raw {
        case "timeline": return .timeline
        case "overview": return .overview
        default: return .board
        }
    }
}
