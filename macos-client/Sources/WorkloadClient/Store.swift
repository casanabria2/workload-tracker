import Foundation
import Observation
import SwiftUI

/// The single source of truth for the UI.
///
/// Holds the last `/v1/snapshot`, refetches it whenever the SSE stream says
/// `changed`, and runs a 1 Hz tick so live-timer labels re-render without
/// polling the daemon.
///
/// **`connection` is not derived from `snapshot == nil`.** A down daemon and an
/// empty tracker are different states and must render differently — the same
/// distinction workload-macos-monitor draws between "tracker unreachable" and
/// "up but idle".
///
/// Phase 4 added the first writes. Two properties of that, both deliberate:
/// status changes are **optimistic with rollback** (`pendingStatus`), and the
/// close workflow is **never optimistic and never silent** — it goes through
/// `beginClose` → a preview the user reads → `confirmClose`.
@Observable
@MainActor
final class Store {

    /// Where the client stands with the daemon.
    enum ConnectionState: Equatable {
        /// Nothing attempted yet, or a fetch is in flight with no prior data.
        case connecting
        /// A snapshot was fetched and the event stream is open.
        case live
        /// A snapshot was fetched but the event stream is down, so changes made
        /// elsewhere will not appear until the next manual refresh.
        case degraded(reason: String)
        /// The daemon is not answering. Distinct from "no tasks".
        case unreachable(reason: String)
        /// The daemon answered with an error.
        case failed(code: String?, message: String)

        var isUnreachable: Bool {
            if case .unreachable = self { return true }
            return false
        }
    }

    // MARK: - Observable state

    private(set) var snapshot: Snapshot?
    private(set) var health: Health?
    private(set) var connection: ConnectionState = .connecting
    private(set) var lastUpdated: Date?
    private(set) var daemonMode: DaemonProcess.Mode = .absent(reason: "not started")
    /// The most recent `progress` event, so a later phase's close sheet has
    /// somewhere to read from. Phase 3 only shows it in the status line.
    private(set) var lastProgress: ProgressPayload?

    /// Advanced once a second so views that show elapsed time re-render.
    /// Reading this in a view body is what subscribes it to the tick.
    private(set) var now: Date = .now

    /// Sidebar selection. Persisted by the view via `@SceneStorage`; held here
    /// so menu commands can drive it in a later phase.
    var selection: SidebarSelection = .board

    /// Whether the recurrent shelf is expanded.
    var showsRecurrentShelf: Bool = true

    // MARK: - Phase 7 timeline state

    /// The Gantt's zoom level (plan §10). Held here rather than in the view so
    /// the `⌘+`/`⌘-` menu commands and the toolbar's segmented control write one
    /// value, and so it survives switching to the Board and back. Persisted by
    /// `RootView` via `@SceneStorage`.
    var timelineZoom: TimelineZoom = .week

    /// The Gantt's **explicit viewport**, or `nil` when the range is derived
    /// (Sprint facet → logged time → current sprint → around now).
    ///
    /// Written only by `stepTimeline` and cleared only by `timelineToToday` and
    /// by a change to the Sprint facet — see `TimelineAnchor` for the rule. Not
    /// persisted: a viewport is where you *were*, and reopening the app on a
    /// fortnight in March because that is where you stopped scrolling last week
    /// would be worse than opening on the present.
    private(set) var timelineAnchor: TimelineAnchor?

    /// The Sprint facet that navigation took away, so **Today** can hand it
    /// back. `nil` means navigation has taken nothing.
    private var sprintFilterReleasedByNavigation: Set<String>?

    /// **The selected task, shared by the Board and the Timeline** (plan §10:
    /// "clicking a bar selects the task and syncs the Inspector and Board
    /// selection").
    ///
    /// It lived in `BoardView` as `@State` through Phase 6, which made the two
    /// views' selections independent by construction. One property, two readers.
    var boardSelection: String?

    // MARK: - Phase 5 filter state

    /// **The** filter (plan §8.1). One instance, written by the sidebar's Roles
    /// rows, by the toolbar's facet menu and by the search field alike, and read
    /// by every view of the snapshot. Persisted by `RootView` via
    /// `@SceneStorage`.
    var filter = FilterState()

    /// The options each facet offers, recomputed whenever the snapshot changes.
    ///
    /// Cached rather than computed on access: building it probes every task
    /// against every candidate value, and a SwiftUI body can read it many times
    /// per frame.
    private(set) var facets: FacetCatalog = .empty

    /// Guards the one-shot default. The Sprint facet defaults to the current
    /// sprint (§8.2), but only until either a snapshot has seeded it or a
    /// persisted filter has been restored — otherwise deliberately clearing the
    /// sprint filter would be undone by the next refresh.
    private var didSeedDefaultSprint = false

    // MARK: - Phase 4 write state

    /// The close-confirmation sheet, or `nil` when none is open. Nothing
    /// irreversible happens while this is in `.ready`.
    private(set) var closeSheet: CloseSheetState?

    /// Transient user-facing feedback from a board interaction: a refused drop,
    /// a rolled-back status change, a completed close.
    private(set) var feedback: BoardFeedback?

    /// Status changes that have been applied to the UI but not yet confirmed by
    /// a snapshot. Keyed by task id. `boardTasks` reads through this, which is
    /// what makes a drop feel instant.
    private(set) var pendingStatus: [String: PendingStatusChange] = [:]

    /// The operation id of the close currently running, so SSE `progress` and
    /// `error` events can be routed to the sheet rather than to the connection
    /// state.
    private var activeCloseOperation: String?
    private var closePollTask: _Concurrency.Task<Void, Never>?
    private var feedbackTask: _Concurrency.Task<Void, Never>?

    // MARK: - Collaborators

    private let client: DaemonClient
    private let events: EventStream
    private let process: DaemonProcess

    private var tickTask: _Concurrency.Task<Void, Never>?
    private var streamTask: _Concurrency.Task<Void, Never>?
    private var refreshTask: _Concurrency.Task<Void, Never>?

    init(client: DaemonClient = DaemonClient()) {
        self.client = client
        self.events = EventStream(client: client)
        self.process = DaemonProcess()
    }

    /// A store pre-loaded with a snapshot and no networking, for SwiftUI
    /// previews and for testing the derived board partition without a daemon.
    /// `start()` is never called on one of these.
    convenience init(previewSnapshot: Snapshot, now: Date = .now) {
        self.init()
        self.snapshot = previewSnapshot
        self.connection = .live
        self.lastUpdated = now
        self.now = now
        self.adoptSnapshotForFiltering()
    }

    /// A store with a snapshot **and** a client, but with `start()` never
    /// called — so no event stream, no tick, no daemon process.
    ///
    /// This is how the write paths are tested: the mutation methods run for
    /// real against a stubbed transport, and the test can assert on exactly
    /// which requests were issued (or that none were).
    convenience init(client: DaemonClient, snapshot: Snapshot?, now: Date = .now) {
        self.init(client: client)
        self.snapshot = snapshot
        self.connection = snapshot == nil ? .connecting : .live
        self.lastUpdated = snapshot == nil ? nil : now
        self.now = now
        self.adoptSnapshotForFiltering()
    }

    // MARK: - Lifecycle

    /// Attaches to (or starts) the daemon, fetches the first snapshot, opens the
    /// event stream and starts the tick. Safe to call once, from `.task`.
    func start() {
        guard tickTask == nil else { return }
        startTick()
        streamTask = _Concurrency.Task { [weak self] in
            guard let self else { return }
            await self.connect()
        }
    }

    /// Cancels everything and terminates a daemon **we** spawned. A launchd
    /// daemon is deliberately left running.
    func shutDown() async {
        tickTask?.cancel(); tickTask = nil
        streamTask?.cancel(); streamTask = nil
        refreshTask?.cancel(); refreshTask = nil
        closePollTask?.cancel(); closePollTask = nil
        feedbackTask?.cancel(); feedbackTask = nil
        await events.stop()
        await process.terminateIfSpawned()
    }

    private func startTick() {
        tickTask = _Concurrency.Task { [weak self] in
            while !_Concurrency.Task.isCancelled {
                try? await _Concurrency.Task.sleep(for: .seconds(1))
                guard let self else { return }
                self.now = .now
            }
        }
    }

    private func connect() async {
        daemonMode = await process.ensureRunning(client: client,
                                                 allowSpawn: AppSettings.autoStartDaemon)
        await refresh()

        // The stream runs for the lifetime of the app; it reconnects itself.
        let updates = await events.start()
        for await update in updates {
            switch update {
            case .connected:
                if snapshot != nil { connection = .live }
                // A reconnect means we may have missed events, and the stream is
                // not replayable — so resync unconditionally.
                await refresh()
            case .event(let event):
                await handle(event)
            case .disconnected(let error, let retryIn):
                let reason = error ?? "the event stream closed"
                if snapshot == nil {
                    connection = .unreachable(reason: reason)
                } else {
                    connection = .degraded(
                        reason: "\(reason) — retrying in \(Int(retryIn))s")
                }
            }
        }
    }

    private func handle(_ event: DaemonEvent) async {
        switch event.name {
        case "changed":
            // Not replayable by design: every `changed` means "refetch".
            await refresh()
        case "progress":
            let payload = event.payload(ProgressPayload.self)
            lastProgress = payload
            if let payload {
                applyCloseProgress(payload)
                applySyncProgress(payload)
            }
        case "error":
            guard let payload = event.payload(StreamErrorPayload.self),
                  let error = payload.error else { break }
            // An operation's failure belongs to that operation, not to the
            // connection: the daemon is healthy, the close is what went wrong.
            // Before Phase 4 every `error` frame blanked the board.
            if let id = payload.operationId, id == activeCloseOperation {
                applyCloseFailure(code: error.code, message: error.message)
            } else if let id = payload.operationId, id == activeSyncOperation {
                applySyncFailure(message: error.message, code: error.code.rawValue)
            } else if payload.op != nil {
                show(.error("\(payload.op ?? "operation") failed: \(error.message)"))
            } else {
                connection = .failed(code: error.code.rawValue, message: error.message)
            }
        case "heartbeat", "hello":
            break
        default:
            break
        }
    }

    // MARK: - Fetching

    /// Refetches `/v1/snapshot` and `/v1/health`. Coalesced: a refresh already
    /// in flight is not duplicated by a burst of `changed` events.
    func refresh() async {
        if let refreshTask {
            await refreshTask.value
            return
        }
        let task = _Concurrency.Task { [weak self] () -> Void in
            await self?.performRefresh()
        }
        refreshTask = task
        await task.value
        refreshTask = nil
    }

    private func performRefresh() async {
        do {
            let fetched = try await client.snapshot()
            self.snapshot = fetched
            self.prunePendingStatus(against: fetched)
            self.adoptSnapshotForFiltering()
            // Health is supplementary (TUI-running warning, daemon version); a
            // failure there must not blank a snapshot that arrived fine.
            self.health = try? await client.health()
            self.lastUpdated = .now
            self.connection = .live
        } catch let error as DaemonClientError {
            switch error {
            case .unreachable:
                self.connection = .unreachable(
                    reason: error.errorDescription ?? "The daemon is not responding.")
            case .missingToken(let path, _):
                self.connection = .unreachable(
                    reason: "No daemon token at \(path).")
            case .api(let code, let message, _, _):
                self.connection = .failed(code: code.rawValue, message: message)
            case .http(let status, _):
                self.connection = .failed(code: nil, message: "HTTP \(status)")
            case .decoding(let underlying):
                self.connection = .failed(
                    code: "decoding",
                    message: "The snapshot did not decode: \(underlying)")
            case .invalidBaseURL(let string):
                self.connection = .failed(code: nil, message: "Bad daemon URL: \(string)")
            case .refusedLocally(let reason):
                // Unreachable on a read path — only writes refuse locally — but
                // exhaustive so a future refusal can't be silently swallowed.
                self.connection = .failed(code: "refused_locally", message: reason)
            }
        } catch {
            self.connection = .failed(code: nil, message: error.localizedDescription)
        }
    }

    /// Re-reads Settings and reconnects. Called when the base URL or token path
    /// changes.
    func reconfigureAndReconnect() {
        _Concurrency.Task { [weak self] in
            guard let self else { return }
            await self.client.reconfigure(.fromSettings())
            await self.events.stop()
            self.streamTask?.cancel()
            self.connection = .connecting
            self.snapshot = nil
            self.streamTask = _Concurrency.Task { [weak self] in
                await self?.connect()
            }
        }
    }

    // MARK: - Derived views of the snapshot

    var tasks: [TrackerTask] { snapshot?.tasks ?? [] }
    var roles: [Role] { snapshot?.roles ?? [] }
    var currentSprint: Sprint? { snapshot?.currentSprint }
    var dataFile: DataFileProbe? { snapshot?.dataFile ?? health?.dataFile }

    /// True when the daemon can read the data file. False is the second-Mac
    /// Full-Disk-Access state, which must never be rendered as "no tasks".
    var dataFileReadable: Bool { dataFile?.readable ?? true }

    /// True when `tracker.py` holds :7373 and could clobber daemon writes.
    var tuiIsRunning: Bool { health?.tuiBridge?.running ?? false }

    /// Non-recurrent tasks in a board column, newest activity first.
    ///
    /// Sorted by last-logged descending so the top of each column is the work
    /// most recently touched; never-logged tasks sort to the bottom by creation
    /// date, which keeps a freshly created To Do card visible.
    ///
    /// Reads through `pendingStatus`, so a dropped card appears in its new
    /// column immediately and reappears in the old one if the write fails.
    func boardTasks(_ status: TaskStatus) -> [TrackerTask] {
        tasks.filter { effectiveStatus(of: $0) == status }.sorted(by: Self.boardOrder)
    }

    /// The column sort, factored out so the drop indicator can predict where a
    /// card will land using the same rule the column will apply to it.
    static func boardOrder(_ lhs: TrackerTask, _ rhs: TrackerTask) -> Bool {
        switch (lhs.lastLoggedAt, rhs.lastLoggedAt) {
        case let (l?, r?): l > r
        case (_?, nil): true
        case (nil, _?): false
        case (nil, nil): (lhs.createdAt ?? 0) > (rhs.createdAt ?? 0)
        }
    }

    /// The perpetual tasks, shown in their own shelf rather than on the board.
    var recurrentTasks: [TrackerTask] {
        tasks.filter { $0.status == .recurrent }
            .sorted { $0.title.localizedCaseInsensitiveCompare($1.title) == .orderedAscending }
    }

    /// Tasks whose status is none of the four known ones. Empty in practice, but
    /// rendering them somewhere beats dropping them silently.
    var unclassifiedTasks: [TrackerTask] {
        tasks.filter { task in
            if case .unknown = task.status { return true }
            return false
        }
    }

    /// One row per role for the sidebar: label, color, task count and total
    /// logged time. Roles with no tasks are kept — the sidebar is a directory of
    /// what exists, not a filter of what is populated.
    var roleSummaries: [RoleSummary] {
        roles.enumerated().map { index, role in
            let owned = tasks.filter { $0.roleId == role.id }
            return RoleSummary(
                role: role,
                color: RolePalette.color(for: role, index: index),
                taskCount: owned.count,
                loggedMins: owned.reduce(0) { $0 + $1.loggedMins }
            )
        }
    }

    /// The task the running timer belongs to, if any.
    var activeTimerTask: TrackerTask? {
        guard let id = snapshot?.activeTimer?.taskId else { return nil }
        return tasks.first { $0.id == id }
    }

    /// Elapsed seconds on the running timer, recomputed against `now` so it
    /// re-renders on the 1 Hz tick.
    var activeTimerElapsed: TimeInterval? {
        guard let timer = snapshot?.activeTimer else { return nil }
        return timer.elapsed(asOf: now)
    }

    /// Total minutes logged across every task in the current sprint. Shown in
    /// the sidebar footer.
    var currentSprintMinutes: Double {
        guard let sprintID = currentSprint?.id else { return 0 }
        return tasks.reduce(0) { $0 + $1.minutes(inSprint: sprintID) }
    }

    // MARK: - Filtering (plan §8)

    /// Rebuilds the facet catalog for a freshly arrived snapshot and, exactly
    /// once, seeds the Sprint facet with the current sprint.
    private func adoptSnapshotForFiltering() {
        facets = FacetCatalog.build(from: snapshot)
        guard !didSeedDefaultSprint else { return }
        guard let id = currentSprint?.id else { return }
        didSeedDefaultSprint = true
        filter.sprints = [id]
    }

    /// Installs a filter restored from `@SceneStorage` and cancels the
    /// current-sprint default, so a filter the user deliberately left empty
    /// stays empty across a relaunch.
    func restoreFilter(_ state: FilterState) {
        didSeedDefaultSprint = true
        filter = state
        releaseTimelineAnchor()
    }

    /// Everything the filter admits, recurrent included.
    var filteredTasks: [TrackerTask] {
        TaskFilter.apply(filter, to: tasks, currentSprintID: currentSprint?.id)
    }

    /// **The board's columns.** The one accessor the view renders from *and*
    /// the keyboard cursor is built from, so a card hidden by a filter cannot be
    /// reached by `⌘←`/`⌘→`.
    ///
    /// Composes with the optimistic overlay rather than replacing it:
    /// `boardTasks` already reads through `pendingStatus`.
    func filteredBoardTasks(_ status: TaskStatus) -> [TrackerTask] {
        TaskFilter.apply(filter, to: boardTasks(status), currentSprintID: currentSprint?.id)
    }

    /// The shelf's rows. **The Sprint facet is ignored here** (plan §9) — see
    /// `TaskFilter.applyToShelf`. The shelf's "This sprint" column still reads
    /// the selected sprint, so the facet changes what you read, not which rows
    /// exist.
    var filteredRecurrentTasks: [TrackerTask] {
        TaskFilter.applyToShelf(filter, to: recurrentTasks, currentSprintID: currentSprint?.id)
    }

    /// The sprint the shelf's "This sprint" column totals.
    ///
    /// The single selected sprint when the user picked exactly one, otherwise
    /// today's. With several selected there is no one column to show, so it
    /// falls back rather than silently summing a subset.
    var shelfSprint: Sprint? {
        if filter.sprints.count == 1, let id = filter.sprints.first,
           let picked = snapshot?.sprints.first(where: { $0.id == id }) {
            return picked
        }
        return currentSprint
    }

    var isFiltering: Bool { !filter.isEmpty }

    /// Turns one facet value on or off. The sidebar's Roles rows and the
    /// toolbar's facet menu both come through here — plan §8.4's "one state, two
    /// views of it" is enforced by there being no other mutator.
    func toggle(_ value: String, in facet: Facet) {
        filter.toggle(value, in: facet)
        if facet == .sprint { releaseTimelineAnchor() }
    }

    func isSelected(_ value: String, in facet: Facet) -> Bool {
        filter[facet].contains(value)
    }

    func clearFilters() {
        filter = FilterState()
        releaseTimelineAnchor()
    }

    func clear(_ facet: Facet) {
        filter[facet] = []
        if facet == .sprint { releaseTimelineAnchor() }
    }

    /// The active facet values as search-field tokens (§8.4).
    var filterTokens: [FilterToken] {
        Facet.allCases.flatMap { facet in
            // Sorted by the catalog's own order so tokens do not shuffle as the
            // underlying `Set` rehashes.
            facets.options(for: facet)
                .filter { filter[facet].contains($0.value) }
                .map { FilterToken(facet: facet, value: $0.value, label: $0.label) }
            + filter[facet].subtracting(facets.options(for: facet).map(\.value)).sorted()
                .map { FilterToken(facet: facet, value: $0,
                                   label: facets.label(for: $0, in: facet)) }
        }
    }

    /// Replaces the facet selections with exactly the tokens that survive.
    ///
    /// This is how removing a token in the search field unchecks the matching
    /// sidebar row and menu item: there is one state, and the token list is a
    /// projection of it that can be written back.
    func applyTokens(_ tokens: [FilterToken]) {
        let before = filter.sprints
        for facet in Facet.allCases {
            filter[facet] = Set(tokens.filter { $0.facet == facet }.map(\.value))
        }
        // Removing a sprint chip in the search field is touching the facet, and
        // the facet wins over the anchor when it is touched.
        if filter.sprints != before { releaseTimelineAnchor() }
    }

    /// Which facets to name in the empty state, and offer a one-click clear for.
    var blockingFacets: [Facet] {
        TaskFilter.blockingFacets(filter, tasks: tasks, currentSprintID: currentSprint?.id)
    }

    /// Whether the free-text term alone is what emptied the result.
    var textIsBlocking: Bool {
        TaskFilter.textIsBlocking(filter, tasks: tasks, currentSprintID: currentSprint?.id)
    }

    // MARK: - The timeline (plan §10)

    /// Everything the Gantt draws, for the current filter, zoom and tick.
    ///
    /// Built on demand rather than cached: it is a single pass over the filtered
    /// tasks' logs (419 on the owner's data, far below any threshold worth
    /// caching for), and it has to recompute on the 1 Hz tick anyway because the
    /// running-timer bar grows against `now`.
    ///
    /// **`filteredTasks`, not `tasks`** — plan §8.1's one filter state, read
    /// here rather than re-implemented.
    var timeline: TimelineData {
        TimelineModel.build(tasks: filteredTasks,
                            roles: roles,
                            sprints: snapshot?.sprints ?? [],
                            currentSprint: currentSprint,
                            selectedSprintIDs: filter.sprints,
                            zoom: timelineZoom,
                            activeTimer: snapshot?.activeTimer,
                            now: now,
                            anchor: timelineAnchor,
                            navigationTasks: timelineNavigableTasks)
    }

    /// What the Previous/Next bounds are computed from: everything the filter
    /// admits **with the Sprint facet taken out**, because stepping releases
    /// that facet. Without this, the default view (an empty current sprint)
    /// would disable both buttons.
    private var timelineNavigableTasks: [TrackerTask] {
        var unscoped = filter
        unscoped.sprints = []
        return TaskFilter.apply(unscoped, to: tasks, currentSprintID: currentSprint?.id)
    }

    /// The tasks the Gantt actually plots: everything the filter admits, **less
    /// the recurrent ones**, which live on the shelf (plan §9).
    ///
    /// Exists so the view's empty state counts the same population the chart
    /// draws. `TimelineModel.build` applies the same rule itself rather than
    /// trusting a caller to pre-filter.
    var timelineTasks: [TrackerTask] {
        filteredTasks.filter { $0.status != .recurrent }
    }

    // MARK: Timeframe navigation

    /// Steps the viewport by one unit of the current zoom.
    ///
    /// **Also releases the Sprint facet** when one is set — see `TimelineAnchor`
    /// for why, and `timelineToToday()` for the inverse. No-ops (and the
    /// controls disable) at the ends of the data.
    func stepTimeline(_ direction: TimelineNavigation.Direction) {
        let data = timeline
        guard let next = TimelineNavigation.step(direction,
                                                 from: data.range,
                                                 zoom: timelineZoom,
                                                 sprints: snapshot?.sprints ?? [],
                                                 bounds: data.navigationBounds)
        else { return }
        timelineAnchor = next
        if !filter.sprints.isEmpty {
            if sprintFilterReleasedByNavigation == nil {
                sprintFilterReleasedByNavigation = filter.sprints
            }
            filter.sprints = []
            show(.info("Showing \(next.label). The Sprint filter was released so "
                       + "the range could move — Today puts it back."))
        }
    }

    func canStepTimeline(_ direction: TimelineNavigation.Direction) -> Bool {
        let data = timeline
        return TimelineNavigation.step(direction,
                                       from: data.range,
                                       zoom: timelineZoom,
                                       sprints: snapshot?.sprints ?? [],
                                       bounds: data.navigationBounds) != nil
    }

    /// Returns the viewport to the present: drops the anchor, so the range is
    /// derived again, and restores whatever Sprint facet navigation released.
    func timelineToToday() {
        timelineAnchor = nil
        if let released = sprintFilterReleasedByNavigation {
            if filter.sprints.isEmpty { filter.sprints = released }
            sprintFilterReleasedByNavigation = nil
        }
    }

    /// Whether Today has anything to undo. False on the default view, which is
    /// already the present.
    var canReturnToToday: Bool {
        timelineAnchor != nil || sprintFilterReleasedByNavigation != nil
    }

    /// Called by every Sprint-facet mutation: touching the facet makes it
    /// authoritative again, so the anchor goes and there is nothing left to
    /// restore.
    private func releaseTimelineAnchor() {
        timelineAnchor = nil
        sprintFilterReleasedByNavigation = nil
    }

    /// `⌘+` / `⌘-`. No-ops at the ends rather than wrapping, which is what every
    /// other zoom control on the system does.
    func zoomTimeline(in zoomIn: Bool) {
        guard let next = zoomIn ? timelineZoom.zoomedIn : timelineZoom.zoomedOut
        else { return }
        timelineZoom = next
    }

    var canZoomTimelineIn: Bool { timelineZoom.zoomedIn != nil }
    var canZoomTimelineOut: Bool { timelineZoom.zoomedOut != nil }

    /// Selects a task from either view. Clicking a Gantt bar comes through here,
    /// which is what keeps the Board's cursor on the same card.
    func selectTask(_ id: String?) {
        boardSelection = id
    }

    // MARK: - Optimistic status

    /// The status the board should draw this task in: the pending one if a
    /// change is in flight, otherwise the snapshot's.
    func effectiveStatus(of task: TrackerTask) -> TaskStatus {
        pendingStatus[task.id]?.target ?? task.status
    }

    /// Drops the optimistic overlay for any task the snapshot now agrees with,
    /// and for any change old enough that the daemon plainly never applied it.
    ///
    /// The overlay is *not* cleared the moment the `POST` returns: the SSE
    /// `changed` → refetch round trip can land after that, and clearing early
    /// makes the card flick back to its old column for a frame.
    private func prunePendingStatus(against snapshot: Snapshot) {
        guard !pendingStatus.isEmpty else { return }
        let byID = Dictionary(snapshot.tasks.map { ($0.id, $0) },
                              uniquingKeysWith: { first, _ in first })
        for (id, pending) in pendingStatus {
            let confirmed = byID[id]?.status == pending.target
            let stale = Date.now.timeIntervalSince(pending.startedAt) > Self.pendingStatusTTL
            if confirmed || stale || byID[id] == nil { pendingStatus[id] = nil }
        }
    }

    /// How long an unconfirmed optimistic change survives. Long enough for a
    /// slow `gh project` round trip on the status sync, short enough that a
    /// dropped event does not leave the board permanently lying.
    private static let pendingStatusTTL: TimeInterval = 20

    /// Applies a status change optimistically and rolls it back if the daemon
    /// refuses. Never used for `.done` — `DaemonClient.setStatus` throws on it,
    /// and `beginClose` is the only route to a close.
    func moveTask(_ task: TrackerTask, to status: TaskStatus) async {
        let previous = effectiveStatus(of: task)
        guard previous != status else { return }
        pendingStatus[task.id] = PendingStatusChange(
            target: status, previous: previous, startedAt: .now)
        do {
            _ = try await client.setStatus(taskId: task.id, status: status)
            // Left in place until a snapshot confirms it; see prunePendingStatus.
            await refresh()
        } catch {
            pendingStatus[task.id] = nil
            let detail = (error as? LocalizedError)?.errorDescription
                ?? error.localizedDescription
            show(.error("Could not move “\(task.title)” to \(status.displayName): \(detail)"))
        }
    }

    /// **The single entry point for "move this card to that column."**
    ///
    /// Drag-and-drop and `⌘←`/`⌘→` both come through here, so the two can never
    /// diverge on what is allowed — and neither can bypass the rule table.
    /// Nothing irreversible happens in this method: the `.confirmClose` branch
    /// opens the sheet, which only sends the write-free dry run.
    func perform(drop payload: TaskDragPayload, on column: TaskStatus) async {
        switch BoardDropRules.decide(payload, to: column) {
        case .rejected(let why):
            // A same-column drop is a shrug, not an error; the other two are
            // worth explaining.
            show(BoardFeedback(message: why.message,
                               isError: why != .sameColumn,
                               hint: why.hint))
        case .optimisticStatus(let status):
            guard let task = tasks.first(where: { $0.id == payload.taskId }) else { return }
            await moveTask(task, to: status)
        case .confirmClose:
            guard let task = tasks.first(where: { $0.id == payload.taskId }) else { return }
            await beginClose(task)
        }
    }

    /// The board column immediately left or right of `status`, or `nil` at the
    /// ends. Drives `⌘←` / `⌘→`.
    func neighbourColumn(of status: TaskStatus, offset: Int) -> TaskStatus? {
        guard let index = TaskStatus.boardColumns.firstIndex(of: status) else { return nil }
        let target = index + offset
        guard TaskStatus.boardColumns.indices.contains(target) else { return nil }
        return TaskStatus.boardColumns[target]
    }

    /// Where a card will land in `column` once it moves there.
    ///
    /// Uses the column's own sort, not the pointer's position: the board does
    /// not persist card order (it is derived from `last_logged_at`), so an
    /// indicator that followed the cursor would promise a placement the data
    /// model cannot keep.
    ///
    /// Counts against the **filtered** column, because that is what is drawn. A
    /// status change never alters a task's role, activity, repo or logged time,
    /// so a card that was visible before the move is still visible after it.
    func landingIndex(of taskId: String, movedTo column: TaskStatus) -> Int {
        guard let moved = tasks.first(where: { $0.id == taskId }) else { return 0 }
        var destination = filteredBoardTasks(column).filter { $0.id != taskId }
        destination.append(moved)
        destination.sort(by: Self.boardOrder)
        return destination.firstIndex { $0.id == taskId } ?? destination.count - 1
    }

    // MARK: - The close workflow (never optimistic, never silent)

    /// Opens the §7.1 sheet and fetches the plan.
    ///
    /// The **only** request this makes is `POST /close/plan`, which is
    /// `reconcile_task_sprints(dry_run=True)` — write-free by construction on
    /// the Python side, so nothing is mutated and no `gh` command runs. The
    /// irreversible call happens in `confirmClose()` and nowhere else.
    /// Opens the close sheet with the **End Series** gate attached (plan §9).
    ///
    /// The only route from the shelf to `close_task`. It funnels into
    /// `beginClose` rather than duplicating it, so the dry-run-then-confirm
    /// shape is shared and there is still exactly one `POST /close` call site.
    func beginEndSeries(_ task: TrackerTask) async {
        await beginClose(task, endSeries: EndSeriesConfirmation(
            task: task, seriesName: RecurrentSeries.canonicalName(for: task)))
    }

    /// The typed confirmation, as the sheet's text field writes it.
    func updateEndSeriesConfirmation(_ typed: String) {
        closeSheet?.endSeries?.typed = typed
    }

    func beginClose(_ task: TrackerTask,
                    endSeries: EndSeriesConfirmation? = nil) async {
        guard closeSheet == nil else { return }
        closeSheet = CloseSheetState(taskId: task.id, title: task.title,
                                     phase: .planning, endSeries: endSeries,
                                     expectsIssueClose: task.currentIssue != nil)
        do {
            let plan = try await client.planClose(taskId: task.id)
            guard closeSheet?.taskId == task.id else { return }
            closeSheet?.expectsIssueClose = plan.currentIssue != nil || plan.needsIssue
            closeSheet?.phase = .ready(plan)
        } catch {
            guard closeSheet?.taskId == task.id else { return }
            closeSheet?.phase = .planFailed(
                (error as? LocalizedError)?.errorDescription ?? error.localizedDescription)
        }
    }

    /// Sends `POST /close`. Only reachable from an explicit confirmation on a
    /// sheet already showing the plan.
    ///
    /// `create_issue` comes from the plan the user just read, so the app can
    /// never authorise an issue creation the preview did not mention.
    func confirmClose() async {
        guard let sheet = closeSheet, case .ready(let plan) = sheet.phase else { return }
        // **The gate, enforced in the model.** The End Series sheet also
        // disables its button, but a disabled button is a view detail; this is
        // the check that makes "no typed name, no `gh issue close`" a property
        // of the store, assertable at the transport with no UI involved.
        guard sheet.allowsConfirmation(plan) else { return }
        closeSheet?.phase = .closing(operationId: nil, plan: plan, lines: [])
        do {
            let record = try await client.closeTask(taskId: sheet.taskId,
                                                    createIssue: plan.createIssueOnConfirm)
            guard closeSheet?.taskId == sheet.taskId else { return }
            activeCloseOperation = record.operationId
            closeSheet?.phase = .closing(operationId: record.operationId,
                                         plan: plan, lines: record.progress)
            // Detached on purpose: `confirmClose()` must return as soon as the
            // daemon has accepted the operation. Awaiting the poller here left
            // the caller — and the test suite — blocked for the poll's whole
            // lifetime.
            closePollTask?.cancel()
            closePollTask = _Concurrency.Task { [weak self] in
                await self?.pollCloseOperation(record.operationId)
            }
        } catch {
            let code = (error as? DaemonClientError)?.code
            applyCloseFailure(code: code,
                              message: (error as? LocalizedError)?.errorDescription
                              ?? error.localizedDescription)
        }
    }

    /// Dismisses the sheet. Safe at any phase: a close already handed to the
    /// daemon keeps running, and the resulting `changed` event refreshes the
    /// board — the sheet is a view of the operation, not its owner.
    func dismissCloseSheet() {
        closeSheet = nil
        activeCloseOperation = nil
        closePollTask?.cancel()
        closePollTask = nil
    }

    private func applyCloseProgress(_ payload: ProgressPayload) {
        guard let id = payload.operationId, id == activeCloseOperation,
              let sheet = closeSheet,
              case .closing(let operationId, let plan, var lines) = sheet.phase else { return }
        if let message = payload.message, !message.isEmpty { lines.append(message) }
        if payload.state == "completed" {
            activeCloseOperation = nil
            closePollTask?.cancel()
            closeSheet?.phase = .succeeded(lines: lines, outcome: payload.result)
            _Concurrency.Task { [weak self] in await self?.refresh() }
        } else {
            closeSheet?.phase = .closing(operationId: operationId, plan: plan, lines: lines)
        }
    }

    private func applyCloseFailure(code: DaemonErrorCode?, message: String) {
        activeCloseOperation = nil
        closePollTask?.cancel()
        let lines: [String]
        if case .closing(_, _, let existing) = closeSheet?.phase { lines = existing }
        else { lines = [] }
        closeSheet?.phase = .failed(message: message, code: code?.rawValue, lines: lines)
        // The task stays open — `close_task` aborts on a failed reconcile
        // precisely so hours cannot be mis-reported. Resync so the board agrees.
        _Concurrency.Task { [weak self] in await self?.refresh() }
    }

    /// Reads `GET /v1/operations/{id}` until it reaches a terminal state.
    ///
    /// Belt and braces to the SSE stream, which is the primary channel: a close
    /// that finished between the `202` and our subscribing to its id would
    /// otherwise leave the sheet spinning forever.
    ///
    /// Runs detached, and never before its first sleep issues a request — so a
    /// caller that inspects the transport straight after `confirmClose()` sees
    /// exactly the `close/plan` and `close` it expects.
    private func pollCloseOperation(_ id: String) async {
        for _ in 0..<600 {
            guard activeCloseOperation == id,
                  !_Concurrency.Task.isCancelled else { return }
            try? await _Concurrency.Task.sleep(for: .seconds(1))
            guard activeCloseOperation == id, !_Concurrency.Task.isCancelled,
                  let record = try? await client.operation(id: id) else { continue }
            guard record.isTerminal else { continue }
            activeCloseOperation = nil
            if record.didFail {
                closeSheet?.phase = .failed(message: record.error?.message
                                            ?? "The close failed.",
                                            code: record.error?.code.rawValue,
                                            lines: record.progress)
            } else {
                closeSheet?.phase = .succeeded(lines: record.progress,
                                               outcome: record.result)
            }
            await refresh()
            return
        }
    }

    // MARK: - The recurrent shelf (plan §9)

    /// The Sync Sprints sheet, or `nil` when none is open.
    private(set) var syncSheet: SyncSheetState?
    /// The Log Time sheet, or `nil` when none is open.
    private(set) var logSheet: LogSheetState?
    /// The shelf row the Task menu acts on.
    var shelfSelection: String?

    /// The operation id of a running reconcile, so SSE `progress` and `error`
    /// events route to the sync sheet instead of the connection state.
    private var activeSyncOperation: String?
    private var syncPollTask: _Concurrency.Task<Void, Never>?

    /// The selected shelf row, resolved against the current snapshot.
    var selectedShelfTask: TrackerTask? {
        guard let id = shelfSelection else { return nil }
        return recurrentTasks.first { $0.id == id }
    }

    /// Dispatches a row action. **The single entry point** for the shelf, the
    /// Task menu and the context menu alike, so the three cannot diverge on what
    /// an action does or on what gates it.
    ///
    /// Nothing irreversible happens in this method: the two GitHub-touching
    /// cases open a sheet whose only request is a write-free dry run.
    func perform(_ action: ShelfAction, on task: TrackerTask) async {
        guard action.availability(for: task, isTimerRunning: isTimerRunning(on: task))
            .isAvailable else { return }
        switch action {
        case .startTimer:
            await startTimer(on: task)
        case .logTime:
            logSheet = LogSheetState(taskId: task.id, title: task.title)
        case .openIssue:
            await openIssue(of: task)
        case .syncSprints:
            await beginSyncSprints(task)
        case .endSeries:
            await beginEndSeries(task)
        }
    }

    func isTimerRunning(on task: TrackerTask) -> Bool {
        snapshot?.activeTimer?.taskId == task.id
    }

    // MARK: Start timer

    /// Starts the timer on a shelf row.
    ///
    /// `browser` is passed explicitly and comes from Settings, which defaults it
    /// off — matching the daemon since `0fdf2d7`, and the plan's decision that
    /// the Safari integration is a removal target. Sent rather than omitted so a
    /// future change to the server default cannot move this client silently.
    func startTimer(on task: TrackerTask) async {
        do {
            let started = try await client.startTimer(taskId: task.id,
                                                      browser: AppSettings.opensTaskWindow)
            if let displaced = started.stopped {
                show(.info("Started “\(task.title)”. The timer on \(displaced) was logged."))
            } else {
                show(.info("Timer started on “\(task.title)”."))
            }
            await refresh()
        } catch {
            show(.error("Could not start the timer on “\(task.title)”: \(describe(error))"))
        }
    }

    /// Stops whatever is running. Used by the Task menu's Start/Stop toggle.
    func stopTimer() async {
        do {
            let stopped = try await client.stopTimer(browser: AppSettings.opensTaskWindow)
            let minutes = stopped.minutes ?? 0
            show(.info(stopped.logged
                       ? "Logged \(Duration.format(minutes: minutes)) to "
                         + "“\(stopped.title ?? "the task")”."
                       : "Timer stopped; the session was too short to log."))
            await refresh()
        } catch {
            show(.error("Could not stop the timer: \(describe(error))"))
        }
    }

    // MARK: Open issue

    func openIssue(of task: TrackerTask) async {
        do {
            let opened = try await client.openIssue(taskId: task.id)
            if !opened.opened {
                show(.error("Could not open \(opened.issue ?? "the issue") in a browser.",
                            hint: opened.url))
            }
        } catch {
            show(.error("Could not open the issue for “\(task.title)”: \(describe(error))"))
        }
    }

    // MARK: Log time

    func dismissLogSheet() { logSheet = nil }

    /// Commits the Log Time sheet. Local only — no GitHub call.
    func confirmLogTime(minutes: Double, note: String) async {
        guard let sheet = logSheet, minutes > 0 else { return }
        logSheet?.isSubmitting = true
        do {
            _ = try await client.addLog(taskId: sheet.taskId, minutes: minutes,
                                        note: note.isEmpty ? "Manual entry" : note)
            logSheet = nil
            show(.info("Logged \(Duration.format(minutes: minutes)) to “\(sheet.title)”."))
            await refresh()
        } catch {
            logSheet?.isSubmitting = false
            logSheet?.error = describe(error)
        }
    }

    // MARK: Sync sprints (never optimistic, never automatic)

    /// Opens the Sync Sprints sheet and fetches the plan.
    ///
    /// The **only** request this makes is the `dry_run: true` reconcile, which
    /// the daemon serves through its read path. The irreversible call happens in
    /// `confirmSyncSprints()` and nowhere else.
    func beginSyncSprints(_ task: TrackerTask) async {
        guard syncSheet == nil else { return }
        syncSheet = SyncSheetState(taskId: task.id, title: task.title, phase: .planning)
        await reloadSyncPlan()
    }

    /// Re-runs the dry run — used on open and whenever the user toggles
    /// "create missing issues", because that flag changes the plan.
    func reloadSyncPlan() async {
        guard let sheet = syncSheet else { return }
        syncSheet?.phase = .planning
        do {
            let plan = try await client.planReconcile(taskId: sheet.taskId,
                                                      createIssues: sheet.createIssues)
            guard syncSheet?.taskId == sheet.taskId else { return }
            syncSheet?.phase = .ready(plan)
        } catch {
            guard syncSheet?.taskId == sheet.taskId else { return }
            syncSheet?.phase = .planFailed(describe(error))
        }
    }

    /// Toggles issue creation and re-plans, so the preview never describes a
    /// different run from the one the button will start.
    func setSyncCreatesIssues(_ value: Bool) async {
        guard syncSheet?.createIssues != value else { return }
        syncSheet?.createIssues = value
        await reloadSyncPlan()
    }

    /// Sends the real reconcile. Only reachable from an explicit confirmation on
    /// a sheet already showing the plan.
    func confirmSyncSprints() async {
        guard let sheet = syncSheet, case .ready(let plan) = sheet.phase,
              plan.isActionable else { return }
        syncSheet?.phase = .running(operationId: nil, plan: plan, lines: [])
        do {
            let record = try await client.runReconcile(taskId: sheet.taskId,
                                                       createIssues: sheet.createIssues)
            guard syncSheet?.taskId == sheet.taskId else { return }
            activeSyncOperation = record.operationId
            syncSheet?.phase = .running(operationId: record.operationId,
                                        plan: plan, lines: record.progress)
            syncPollTask?.cancel()
            syncPollTask = _Concurrency.Task { [weak self] in
                await self?.pollSyncOperation(record.operationId)
            }
        } catch {
            applySyncFailure(message: describe(error),
                             code: (error as? DaemonClientError)?.code?.rawValue)
        }
    }

    func dismissSyncSheet() {
        syncSheet = nil
        activeSyncOperation = nil
        syncPollTask?.cancel()
        syncPollTask = nil
    }

    private func applySyncProgress(_ payload: ProgressPayload) {
        guard let id = payload.operationId, id == activeSyncOperation,
              let sheet = syncSheet,
              case .running(let operationId, let plan, var lines) = sheet.phase else { return }
        if let message = payload.message, !message.isEmpty { lines.append(message) }
        if payload.state == "completed" {
            activeSyncOperation = nil
            syncPollTask?.cancel()
            syncSheet?.phase = .succeeded(
                lines: lines,
                outcome: payload.result(as: ReconcileResponse.self))
            _Concurrency.Task { [weak self] in await self?.refresh() }
        } else {
            syncSheet?.phase = .running(operationId: operationId, plan: plan, lines: lines)
        }
    }

    private func applySyncFailure(message: String, code: String?) {
        activeSyncOperation = nil
        syncPollTask?.cancel()
        let lines: [String]
        if case .running(_, _, let existing) = syncSheet?.phase { lines = existing }
        else { lines = [] }
        syncSheet?.phase = .failed(message: message, code: code, lines: lines)
        _Concurrency.Task { [weak self] in await self?.refresh() }
    }

    /// Belt and braces to the SSE stream, mirroring `pollCloseOperation`.
    private func pollSyncOperation(_ id: String) async {
        for _ in 0..<600 {
            guard activeSyncOperation == id, !_Concurrency.Task.isCancelled else { return }
            try? await _Concurrency.Task.sleep(for: .seconds(1))
            guard activeSyncOperation == id, !_Concurrency.Task.isCancelled,
                  let record = try? await client.operation(id: id) else { continue }
            guard record.isTerminal else { continue }
            activeSyncOperation = nil
            if record.didFail {
                syncSheet?.phase = .failed(message: record.error?.message
                                           ?? "The reconcile failed.",
                                           code: record.error?.code.rawValue,
                                           lines: record.progress)
            } else {
                syncSheet?.phase = .succeeded(lines: record.progress,
                                              outcome: record.reconcileResult)
            }
            await refresh()
            return
        }
    }

    private func describe(_ error: any Error) -> String {
        (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
    }

    // MARK: - Feedback

    /// Shows a transient message and clears it after a few seconds.
    func show(_ feedback: BoardFeedback) {
        self.feedback = feedback
        feedbackTask?.cancel()
        feedbackTask = _Concurrency.Task { [weak self] in
            try? await _Concurrency.Task.sleep(for: .seconds(feedback.isError ? 8 : 4))
            guard let self, !_Concurrency.Task.isCancelled else { return }
            if self.feedback == feedback { self.feedback = nil }
        }
    }

    func clearFeedback() {
        feedbackTask?.cancel()
        feedback = nil
    }
}

/// An optimistic status change awaiting confirmation from a snapshot.
struct PendingStatusChange: Equatable, Sendable {
    let target: TaskStatus
    /// Kept so a rollback restores what was on screen, not what the (possibly
    /// stale) snapshot says.
    let previous: TaskStatus
    let startedAt: Date
}

/// A transient board message: a refused drop, a rolled-back move, a finished
/// close.
struct BoardFeedback: Identifiable, Equatable, Sendable {
    let id: UUID
    let message: String
    let isError: Bool
    /// A short "why not" that some refusals carry (§7's rejected transitions).
    let hint: String?

    init(message: String, isError: Bool, hint: String? = nil) {
        self.id = UUID()
        self.message = message
        self.isError = isError
        self.hint = hint
    }

    static func error(_ message: String, hint: String? = nil) -> BoardFeedback {
        BoardFeedback(message: message, isError: true, hint: hint)
    }

    static func info(_ message: String, hint: String? = nil) -> BoardFeedback {
        BoardFeedback(message: message, isError: false, hint: hint)
    }
}

/// The close sheet's state machine (plan §7.1).
///
/// `ready` is the important one: the plan is on screen and **nothing has been
/// sent but the dry run**. The only transition out of it that touches GitHub is
/// the user pressing Close Task.
struct CloseSheetState: Identifiable, Equatable {
    let taskId: String
    let title: String
    var phase: Phase
    /// The extra gate in front of ending a recurrent series (plan §9), or `nil`
    /// for an ordinary board close.
    ///
    /// Sharing one sheet and one `confirmClose()` between the two is deliberate:
    /// there is exactly one route from this app to `close_task`, so a second
    /// route cannot be added by accident. The recurrent case tightens that route
    /// rather than going around it.
    var endSeries: EndSeriesConfirmation?

    var id: String { taskId }

    /// Whether the confirm button may be enabled at all.
    ///
    /// For a board close this is just "the plan is actionable". For a series it
    /// additionally requires the typed name — and that gate is evaluated *here*,
    /// in the state, not in the view, so a refactored button cannot lose it.
    func allowsConfirmation(_ plan: ClosePlanResponse) -> Bool {
        guard plan.isActionable else { return false }
        guard let endSeries else { return true }
        return endSeries.isSatisfied
    }

    enum Phase: Equatable {
        /// `POST /close/plan` in flight.
        case planning
        /// The plan is rendered. No irreversible call has been made.
        case ready(ClosePlanResponse)
        /// The dry run itself failed, so there is nothing to confirm.
        case planFailed(String)
        /// `POST /close` accepted; `lines` grow from SSE `progress` events.
        case closing(operationId: String?, plan: ClosePlanResponse, lines: [String])
        /// The close failed. **The task stays open** — a failed reconcile aborts
        /// the close so hours cannot be mis-reported.
        case failed(message: String, code: String?, lines: [String])
        /// The operation completed. `outcome` is what it actually achieved —
        /// **"completed" does not mean GitHub took it** (see `OperationOutcome`),
        /// so the success message is derived from this rather than assumed.
        case succeeded(lines: [String], outcome: OperationOutcome?)
    }

    /// Whether the sheet is at a point where dismissing loses nothing.
    var isDismissable: Bool {
        switch phase {
        case .closing: false
        default: true
        }
    }

    /// Whether this close was *meant* to close a GitHub issue.
    ///
    /// Stored rather than derived from `phase`, because it has to survive the
    /// transition *out* of `.ready` — it is read in `.succeeded`, by which point
    /// the plan is gone. Seeded from the task and refined when the plan lands.
    /// Lets "the issue was not closed" be told apart from a task that
    /// legitimately has no repo, where closing nothing is correct.
    var expectsIssueClose: Bool = false
}

/// The Sync Sprints sheet's state machine (plan §9).
///
/// `ready` is the important one: the dry run is on screen and **nothing has been
/// sent but that dry run**. The only transition out of it that touches GitHub is
/// the user pressing the confirm button.
struct SyncSheetState: Identifiable, Equatable {
    let taskId: String
    let title: String
    var phase: Phase
    /// Whether the run may mint issues for past sprints that have time but none.
    ///
    /// Defaults to **false**, matching `wt sync-sprints --all`'s safety rule: a
    /// blanket run over a long history can want to mint a couple of dozen
    /// issues, so creation is opted into rather than out of. Toggling it
    /// re-plans, so the preview always describes the run the button will start.
    var createIssues: Bool = false

    var id: String { taskId }

    enum Phase: Equatable {
        case planning
        /// The dry run is rendered. No irreversible call has been made.
        case ready(ReconcileResponse)
        case planFailed(String)
        case running(operationId: String?, plan: ReconcileResponse, lines: [String])
        case failed(message: String, code: String?, lines: [String])
        /// Completed. `outcome` is the reconcile's own report — a completed
        /// operation is not automatically a successful one.
        case succeeded(lines: [String], outcome: ReconcileResponse?)
    }

    var isDismissable: Bool {
        if case .running = phase { return false }
        return true
    }
}

/// The Log Time sheet's state. Local-only, so it has no dry run — but it still
/// gets a sheet, because it appends to the owner's irreplaceable work history
/// and the amount should be typed and read rather than guessed.
struct LogSheetState: Identifiable, Equatable {
    let taskId: String
    let title: String
    var isSubmitting: Bool = false
    var error: String?

    var id: String { taskId }
}

/// One sidebar Roles row.
struct RoleSummary: Identifiable, Equatable {
    let role: Role
    let color: Color
    let taskCount: Int
    let loggedMins: Double

    var id: String { role.id }
}

/// One active facet value, rendered as a removable chip in the search field.
///
/// `.searchable(text:tokens:)` owns the array, so removing a chip hands back a
/// shorter one; `Store.applyTokens` writes that difference into the single
/// `FilterState`.
struct FilterToken: Identifiable, Hashable, Sendable {
    let facet: Facet
    let value: String
    let label: String

    /// Facet-qualified: two facets can legitimately offer the same string (a
    /// role id and an activity name have collided before in this data).
    var id: String { "\(facet.rawValue)\u{1}\(value)" }

    /// `Activity Type: Workshop` — the facet has to be in the chip, or two
    /// chips reading `Workshop` and `Sprint 105` look like one list.
    ///
    /// Except when the label already opens with the facet's name, which every
    /// sprint's does: `Sprint: Sprint 105` is a stutter, and it is what the
    /// first render of this actually showed.
    var display: String {
        label.hasPrefix(facet.displayName) ? label : "\(facet.displayName): \(label)"
    }
}

/// What the sidebar can select.
///
/// The Roles section **is no longer navigation**: Phase 5 made those rows
/// multi-select toggles over the shared `FilterState.roles` (§8.4), so there is
/// no `.role` case any more and `RootView.decode` folds a persisted `role:…`
/// from an earlier build back to `.board`.
enum SidebarSelection: Hashable, Codable {
    case board
    case timeline
    case overview
}
