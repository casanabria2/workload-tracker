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
            if let payload { applyCloseProgress(payload) }
        case "error":
            guard let payload = event.payload(StreamErrorPayload.self),
                  let error = payload.error else { break }
            // An operation's failure belongs to that operation, not to the
            // connection: the daemon is healthy, the close is what went wrong.
            // Before Phase 4 every `error` frame blanked the board.
            if let id = payload.operationId, id == activeCloseOperation {
                applyCloseFailure(code: error.code, message: error.message)
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

    // MARK: - The close workflow (never optimistic, never silent)

    /// Opens the §7.1 sheet and fetches the plan.
    ///
    /// The **only** request this makes is `POST /close/plan`, which is
    /// `reconcile_task_sprints(dry_run=True)` — write-free by construction on
    /// the Python side, so nothing is mutated and no `gh` command runs. The
    /// irreversible call happens in `confirmClose()` and nowhere else.
    func beginClose(_ task: TrackerTask) async {
        guard closeSheet == nil else { return }
        closeSheet = CloseSheetState(taskId: task.id, title: task.title, phase: .planning)
        do {
            let plan = try await client.planClose(taskId: task.id)
            guard closeSheet?.taskId == task.id else { return }
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
            closeSheet?.phase = .succeeded(lines: lines)
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
                closeSheet?.phase = .succeeded(lines: record.progress)
            }
            await refresh()
            return
        }
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

    var id: String { taskId }

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
        case succeeded(lines: [String])
    }

    /// Whether the sheet is at a point where dismissing loses nothing.
    var isDismissable: Bool {
        switch phase {
        case .closing: false
        default: true
        }
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
