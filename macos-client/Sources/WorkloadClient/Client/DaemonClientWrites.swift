import Foundation

// The first write surface in this app. Everything before Phase 4 was `GET`.
//
// Two rules are enforced here rather than in the view layer, because a view can
// be refactored and a rule in a view is a rule you can lose:
//
// 1. **`setStatus` refuses `done`.** `wt_daemon`'s `POST /v1/tasks/{id}/status`
//    routes `{"status": "done"}` straight into `_close_operation` — the real
//    close workflow, with `gh issue create` and `gh issue close` in it. So the
//    innocuous-looking status endpoint is a GitHub write in disguise, and the
//    only way to reach it is `closeTask(_:createIssue:)`, which the §7.1 sheet
//    gates. A `done` here throws before a socket is opened.
// 2. **`planClose` is the only thing a Done drop may send before confirmation.**
//    It is `reconcile_task_sprints(dry_run=True)`, write-free by construction.

extension DaemonClient {

    /// `POST /v1/tasks/{id}/status` — the optimistic Kanban drop.
    ///
    /// - Throws: `DaemonClientError.refusedLocally` for `.done`, without
    ///   issuing a request. Closing runs through `closeTask` and the close
    ///   sheet; see the note at the top of this file.
    func setStatus(taskId: String, status: TaskStatus) async throws -> StatusChange {
        guard status != .done else {
            throw DaemonClientError.refusedLocally(
                reason: "Marking a task done runs the GitHub close workflow. "
                + "It has to go through the close confirmation sheet.")
        }
        return try await post("/v1/tasks/\(escape(taskId))/status",
                              body: ["status": .string(status.rawValue)],
                              as: StatusChange.self)
    }

    /// `POST /v1/tasks/{id}/close/plan` — the §7.1 preview.
    ///
    /// Write-free by construction on the Python side. `offline` makes the daemon
    /// resolve sprints from `config.sprints_cache` instead of GitHub, which is
    /// the difference between an instant sheet and one that waits on a `gh`
    /// round trip.
    func planClose(taskId: String, offline: Bool = false) async throws -> ClosePlanResponse {
        try await post("/v1/tasks/\(escape(taskId))/close/plan",
                       body: ["offline": .bool(offline)],
                       as: ClosePlanResponse.self)
    }

    /// `POST /v1/tasks/{id}/close` — **the irreversible one.**
    ///
    /// Returns `202` with an `operation_id`; the work runs on a daemon thread
    /// and reports through SSE `progress` events. Only ever called from an
    /// explicit confirmation.
    ///
    /// - Parameter createIssue: authorises the workflow to mint the task's first
    ///   GitHub issue. `wt_api.close()` *refuses* rather than mints when this is
    ///   false, so it must match what the preview told the user.
    func closeTask(taskId: String, createIssue: Bool) async throws -> OperationRecord {
        try await post("/v1/tasks/\(escape(taskId))/close",
                       body: ["create_issue": .bool(createIssue)],
                       as: OperationRecord.self)
    }

    /// `GET /v1/operations/{id}` — the reconnect path for a client that missed
    /// the terminal SSE event.
    func operation(id: String) async throws -> OperationRecord {
        try await get("/v1/operations/\(escape(id))", as: OperationRecord.self)
    }

    // MARK: - Phase 6: the recurrent shelf's row actions

    /// `POST /v1/tasks/{id}/reconcile` with `dry_run: true` — the Sync Sprints
    /// preview.
    ///
    /// Write-free twice over: `wt_api.reconcile` passes `save_callback=None`
    /// when `dry_run` is set, and the daemon serves this branch through
    /// `Daemon.read(...)` rather than as an operation, so it never takes the
    /// write path at all. Returns `200` synchronously, unlike the real run.
    ///
    /// - Parameter createIssues: previews minting issues for past sprints that
    ///   have time but no issue. Must match what `runReconcile` is later given,
    ///   or the preview understates the real run.
    func planReconcile(taskId: String, createIssues: Bool) async throws -> ReconcileResponse {
        try await post("/v1/tasks/\(escape(taskId))/reconcile",
                       body: ["dry_run": .bool(true),
                              "create_issues": .bool(createIssues)],
                       as: ReconcileResponse.self)
    }

    /// `POST /v1/tasks/{id}/reconcile` for real — **irreversible.**
    ///
    /// On a recurrent task this mints the new sprint's issue and closes the
    /// ended one. Only ever called from an explicit confirmation on a sheet
    /// already showing `planReconcile`'s output. Returns `202` with an
    /// `operation_id`; progress arrives on the SSE stream.
    func runReconcile(taskId: String, createIssues: Bool) async throws -> OperationRecord {
        try await post("/v1/tasks/\(escape(taskId))/reconcile",
                       body: ["dry_run": .bool(false),
                              "create_issues": .bool(createIssues)],
                       as: OperationRecord.self)
    }

    /// `POST /v1/timer/start`.
    ///
    /// - Parameter browser: opens the task's dedicated Safari window.
    ///   **Always passed explicitly.** The daemon's default flipped from `true`
    ///   to `false` in `0fdf2d7`; a client that relies on a server default
    ///   changes behaviour when the server does, silently. `AppSettings`
    ///   supplies it, defaulting off.
    func startTimer(taskId: String, browser: Bool) async throws -> TimerStart {
        try await post("/v1/timer/start",
                       body: ["task_id": .string(taskId), "browser": .bool(browser)],
                       as: TimerStart.self)
    }

    /// `POST /v1/timer/stop`. Logs a `"Timer session"` entry and, unless the
    /// daemon was started with `--no-github-sync-on-stop`, syncs hours to the
    /// current issue's project field afterwards (a field update, not an issue
    /// mutation).
    func stopTimer(browser: Bool) async throws -> TimerStop {
        try await post("/v1/timer/stop",
                       body: ["browser": .bool(browser)],
                       as: TimerStop.self)
    }

    /// `POST /v1/tasks/{id}/logs` — a manual time entry. Local only; touches no
    /// GitHub issue.
    func addLog(taskId: String, minutes: Double, note: String) async throws -> AddedLog {
        try await post("/v1/tasks/\(escape(taskId))/logs",
                       body: ["minutes": .double(minutes), "note": .string(note)],
                       as: AddedLog.self)
    }

    /// `POST /v1/tasks/{id}/github/open` — opens the current issue in the
    /// default browser. Read-only: the daemon uses `webbrowser`, not `gh`, so it
    /// costs no API budget and mutates nothing.
    ///
    /// - Parameter open: `false` resolves the URL without launching anything,
    ///   which is how this is exercised in tests.
    @discardableResult
    func openIssue(taskId: String, open: Bool = true) async throws -> OpenedIssue {
        try await post("/v1/tasks/\(escape(taskId))/github/open",
                       body: ["open": .bool(open)],
                       as: OpenedIssue.self)
    }

    // MARK: - Transport

    /// A minimal JSON body value. The write bodies this phase sends are flat
    /// string/bool maps, so a full `Encodable` generic would be ceremony.
    enum BodyValue: Sendable {
        case string(String)
        case bool(Bool)
        case int(Int)
        case double(Double)

        var json: Any {
            switch self {
            case .string(let v): v
            case .bool(let v): v
            case .int(let v): v
            case .double(let v): v
            }
        }
    }

    private func escape(_ component: String) -> String {
        component.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? component
    }

    private func post<T: Decodable>(_ path: String,
                                    body: [String: BodyValue],
                                    as type: T.Type) async throws -> T {
        var request = URLRequest(url: currentConfiguration.baseURL.appending(path: path),
                                 timeoutInterval: currentConfiguration.timeout)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(
            withJSONObject: body.mapValues(\.json))
        return try await authorized(request, as: type)
    }
}

/// `POST /v1/tasks/{id}/status`'s success body (the non-`done` shape of
/// `wt_api.set_status`).
struct StatusChange: Decodable, Sendable, Equatable {
    /// Always false on this path — `closed: true` only comes back from the
    /// `done` branch, which this client refuses to reach.
    let closed: Bool
    let status: String?
    let oldStatus: String?
    let statusLabel: String?
    /// The task's current-binding issue, if any.
    let issue: String?
    /// Whether the GitHub Project's Status field was updated too.
    let projectSynced: Bool

    enum CodingKeys: String, CodingKey {
        case closed, status, issue
        case oldStatus = "old_status"
        case statusLabel = "status_label"
        case projectSynced = "project_synced"
    }

    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        closed = try c.decodeIfPresent(Bool.self, forKey: .closed) ?? false
        status = try c.decodeIfPresent(String.self, forKey: .status)
        oldStatus = try c.decodeIfPresent(String.self, forKey: .oldStatus)
        statusLabel = try c.decodeIfPresent(String.self, forKey: .statusLabel)
        issue = try c.decodeIfPresent(String.self, forKey: .issue)
        projectSynced = try c.decodeIfPresent(Bool.self, forKey: .projectSynced) ?? false
    }
}

// MARK: - Phase 6 response bodies

/// `POST /v1/timer/start`.
struct TimerStart: Decodable, Sendable, Equatable {
    let taskId: String?
    let title: String?
    let startedAt: TimeInterval?
    /// The task a previously running timer was committed to, when starting this
    /// one displaced it. `nil` when nothing was running.
    let stopped: String?

    enum CodingKeys: String, CodingKey {
        case title, stopped
        case taskId = "task_id"
        case startedAt = "started_at"
    }

    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        taskId = try c.decodeIfPresent(String.self, forKey: .taskId)
        title = try c.decodeIfPresent(String.self, forKey: .title)
        startedAt = try c.decodeIfPresent(TimeInterval.self, forKey: .startedAt)
        // The daemon sends the displaced task as a dict on some paths and a
        // string on others; only its presence matters here.
        stopped = try? c.decodeIfPresent(String.self, forKey: .stopped)
    }
}

/// `POST /v1/timer/stop`.
struct TimerStop: Decodable, Sendable, Equatable {
    let taskId: String?
    let title: String?
    let minutes: Double?
    /// False when the session was under three seconds and discarded rather than
    /// logged, matching every other tracker entry point.
    let logged: Bool

    enum CodingKeys: String, CodingKey {
        case title, minutes, logged
        case taskId = "task_id"
    }

    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        taskId = try c.decodeIfPresent(String.self, forKey: .taskId)
        title = try c.decodeIfPresent(String.self, forKey: .title)
        minutes = try c.decodeIfPresent(Double.self, forKey: .minutes)
        logged = try c.decodeIfPresent(Bool.self, forKey: .logged) ?? true
    }
}

/// `POST /v1/tasks/{id}/logs`.
struct AddedLog: Decodable, Sendable, Equatable {
    let taskId: String?
    let log: LogEntry?

    enum CodingKeys: String, CodingKey {
        case log
        case taskId = "task_id"
    }

    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        taskId = try c.decodeIfPresent(String.self, forKey: .taskId)
        log = try c.decodeIfPresent(LogEntry.self, forKey: .log)
    }
}

/// `POST /v1/tasks/{id}/github/open`.
struct OpenedIssue: Decodable, Sendable, Equatable {
    let taskId: String?
    let issue: String?
    let url: String?
    let opened: Bool

    enum CodingKeys: String, CodingKey {
        case issue, url, opened
        case taskId = "task_id"
    }

    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        taskId = try c.decodeIfPresent(String.self, forKey: .taskId)
        issue = try c.decodeIfPresent(String.self, forKey: .issue)
        url = try c.decodeIfPresent(String.self, forKey: .url)
        opened = try c.decodeIfPresent(Bool.self, forKey: .opened) ?? false
    }
}
