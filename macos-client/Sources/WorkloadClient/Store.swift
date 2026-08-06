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
/// "up but idle". Phase 3 renders read-only, so nothing here mutates.
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
            lastProgress = event.payload(ProgressPayload.self)
        case "error":
            if let payload = event.payload(StreamErrorPayload.self),
               let error = payload.error {
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
    func boardTasks(_ status: TaskStatus) -> [TrackerTask] {
        tasks
            .filter { $0.status == status }
            .sorted { lhs, rhs in
                switch (lhs.lastLoggedAt, rhs.lastLoggedAt) {
                case let (l?, r?): l > r
                case (_?, nil): true
                case (nil, _?): false
                case (nil, nil): (lhs.createdAt ?? 0) > (rhs.createdAt ?? 0)
                }
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
}

/// One sidebar Roles row.
struct RoleSummary: Identifiable, Equatable {
    let role: Role
    let color: Color
    let taskCount: Int
    let loggedMins: Double

    var id: String { role.id }
}

/// What the sidebar can select. The Roles section is navigation-shaped in Phase
/// 3 (it scopes the board to one role); Phase 5 turns it into a multi-select
/// filter writing shared `FilterState`.
enum SidebarSelection: Hashable, Codable {
    case board
    case timeline
    case overview
    case role(String)
}
