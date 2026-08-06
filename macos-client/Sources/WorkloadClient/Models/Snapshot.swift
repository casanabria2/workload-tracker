import Foundation

// A Codable mirror of `GET /v1/snapshot` (wt_api.snapshot / wt_api.task_view).
//
// Two conventions inherited from workload-macos-monitor's Models.swift, both
// load-bearing:
//
// 1. **Every field the daemon may omit decodes as optional, and every
//    collection/number defaults rather than throwing.** The monitor survived
//    two contract changes (`active_window_id`, `last_logged_at`) purely because
//    of this. A snapshot that grows a field must not break an older client, and
//    a snapshot that loses one must not blank the board.
// 2. **Snake_case keys are mapped explicitly via `CodingKeys`**, not via
//    `.convertFromSnakeCase`, so the wire name of each field is greppable from
//    Swift.

// MARK: - Snapshot

/// The whole UI state in one round trip.
struct Snapshot: Codable, Sendable, Equatable {
    /// Epoch seconds when the daemon rendered this snapshot.
    let generatedAt: TimeInterval?
    let tasks: [TrackerTask]
    let roles: [Role]
    /// The full persisted sprint calendar (`config.sprints_cache`) — 72 entries
    /// on the owner's data, available offline.
    let sprints: [Sprint]
    /// The sprint containing today, or `nil` if today falls outside the cache.
    let currentSprint: Sprint?
    /// `nil` when no timer is running. Carries raw epoch `started_at` so the
    /// client ticks elapsed time locally instead of polling.
    let activeTimer: ActiveTimer?
    /// The GitHub Project's full Activity/Type option lists, for the editor's
    /// pickers. Not the filter bar's source (plan §8.3).
    let projectOptions: ProjectOptions
    let config: SnapshotConfig
    /// Whether the data file was *actually* readable, as opposed to `wt.load()`
    /// masking an unreadable file as an empty tracker (plan risk #9). The
    /// Full-Disk-Access state renders off this, never off `tasks.isEmpty`.
    let dataFile: DataFileProbe?

    enum CodingKeys: String, CodingKey {
        case generatedAt = "generated_at"
        case tasks, roles, sprints
        case currentSprint = "current_sprint"
        case activeTimer = "active_timer"
        case projectOptions = "project_options"
        case config
        case dataFile = "data_file"
    }

    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        generatedAt = try c.decodeIfPresent(TimeInterval.self, forKey: .generatedAt)
        tasks = try c.decodeIfPresent([TrackerTask].self, forKey: .tasks) ?? []
        roles = try c.decodeIfPresent([Role].self, forKey: .roles) ?? []
        sprints = try c.decodeIfPresent([Sprint].self, forKey: .sprints) ?? []
        currentSprint = try c.decodeIfPresent(Sprint.self, forKey: .currentSprint)
        activeTimer = try c.decodeIfPresent(ActiveTimer.self, forKey: .activeTimer)
        projectOptions = try c.decodeIfPresent(ProjectOptions.self, forKey: .projectOptions)
            ?? ProjectOptions()
        config = try c.decodeIfPresent(SnapshotConfig.self, forKey: .config)
            ?? SnapshotConfig()
        dataFile = try c.decodeIfPresent(DataFileProbe.self, forKey: .dataFile)
    }
}

// MARK: - Task

/// Tracker statuses. The `unknown` case exists so a status added on the Python
/// side (which happened once already — `recurrent`) renders as an unrecognised
/// pill rather than failing the whole snapshot decode.
enum TaskStatus: RawRepresentable, Codable, Sendable, Hashable {
    case todo
    case inProgress
    case recurrent
    case done
    case unknown(String)

    init(rawValue: String) {
        switch rawValue {
        case "todo": self = .todo
        case "inprogress": self = .inProgress
        case "recurrent": self = .recurrent
        case "done": self = .done
        default: self = .unknown(rawValue)
        }
    }

    var rawValue: String {
        switch self {
        case .todo: "todo"
        case .inProgress: "inprogress"
        case .recurrent: "recurrent"
        case .done: "done"
        case .unknown(let raw): raw
        }
    }

    /// The three Kanban columns, in board order. `recurrent` is deliberately
    /// absent: those tasks live in their own shelf (plan §7, §9).
    static let boardColumns: [TaskStatus] = [.todo, .inProgress, .done]

    /// Display title used for column headers and status pills.
    var displayName: String {
        switch self {
        case .todo: "To Do"
        case .inProgress: "In Progress"
        case .recurrent: "Recurrent"
        case .done: "Done"
        case .unknown(let raw): raw.capitalized
        }
    }
}

/// One task as the daemon renders it. Named `TrackerTask` rather than `Task` to
/// avoid colliding with Swift concurrency's `Task`; the monitor uses the same
/// name for the same reason.
struct TrackerTask: Codable, Sendable, Identifiable, Equatable {
    let id: String
    let title: String
    let description: String
    let status: TaskStatus
    /// The daemon's human label for `status` (`"In Progress"`). Optional: the
    /// client can always fall back to `status.displayName`.
    let statusLabel: String?
    let roleId: String?
    let createdAt: TimeInterval?

    /// Per-task GitHub Project fields. `activity` and `githubRepo` are the two
    /// filter facets (plan §8); `type` is carried for the editor only and is
    /// `nil` on every task in the owner's current data.
    let activity: String?
    let githubRepo: String?
    let type: String?

    /// Per-sprint time attribution, computed in Python from log timestamps.
    /// Swift never re-derives this (plan §8.2). Zero-minute sprints are already
    /// dropped by `task_sprints_with_time()`.
    let sprintsWithTime: [SprintTime]
    let startSprint: String?
    let startSprintId: String?
    /// One GitHub issue binding per sprint the task has time in.
    let sprintIssues: [SprintBinding]
    /// Resolved via `task_current_issue()`, never the legacy `github_issue` key.
    let currentIssue: String?

    let loggedMins: Double
    /// Minutes accrued by a *running* timer on this task, 0 otherwise.
    let liveMins: Double
    /// The sprint-filtered total that gets reported to GitHub — never
    /// `loggedMins` for a cross-sprint task.
    let reportableMins: Double
    /// Epoch seconds of the most recent log entry, `nil` if never logged.
    let lastLoggedAt: TimeInterval?
    let logs: [LogEntry]

    let localFolder: String?
    /// Ordered tab URLs for the task's dedicated Safari window.
    let tabs: [String]
    /// Safari window id while the task's window is open.
    let activeWindowId: Int?

    enum CodingKeys: String, CodingKey {
        case id, title, description, status, activity, type, logs, tabs
        case statusLabel = "status_label"
        case roleId = "role_id"
        case createdAt = "created_at"
        case githubRepo = "github_repo"
        case sprintsWithTime = "sprints_with_time"
        case startSprint = "start_sprint"
        case startSprintId = "start_sprint_id"
        case sprintIssues = "sprint_issues"
        case currentIssue = "current_issue"
        case loggedMins = "logged_mins"
        case liveMins = "live_mins"
        case reportableMins = "reportable_mins"
        case lastLoggedAt = "last_logged_at"
        case localFolder = "local_folder"
        case activeWindowId = "active_window_id"
    }

    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? ""
        description = try c.decodeIfPresent(String.self, forKey: .description) ?? ""
        let rawStatus = try c.decodeIfPresent(String.self, forKey: .status) ?? "todo"
        status = TaskStatus(rawValue: rawStatus)
        statusLabel = try c.decodeIfPresent(String.self, forKey: .statusLabel)
        roleId = try c.decodeIfPresent(String.self, forKey: .roleId)
        createdAt = try c.decodeIfPresent(TimeInterval.self, forKey: .createdAt)
        activity = try c.decodeIfPresent(String.self, forKey: .activity)
        githubRepo = try c.decodeIfPresent(String.self, forKey: .githubRepo)
        type = try c.decodeIfPresent(String.self, forKey: .type)
        sprintsWithTime = try c.decodeIfPresent([SprintTime].self, forKey: .sprintsWithTime) ?? []
        startSprint = try c.decodeIfPresent(String.self, forKey: .startSprint)
        startSprintId = try c.decodeIfPresent(String.self, forKey: .startSprintId)
        sprintIssues = try c.decodeIfPresent([SprintBinding].self, forKey: .sprintIssues) ?? []
        currentIssue = try c.decodeIfPresent(String.self, forKey: .currentIssue)
        loggedMins = try c.decodeIfPresent(Double.self, forKey: .loggedMins) ?? 0
        liveMins = try c.decodeIfPresent(Double.self, forKey: .liveMins) ?? 0
        reportableMins = try c.decodeIfPresent(Double.self, forKey: .reportableMins) ?? 0
        lastLoggedAt = try c.decodeIfPresent(TimeInterval.self, forKey: .lastLoggedAt)
        logs = try c.decodeIfPresent([LogEntry].self, forKey: .logs) ?? []
        localFolder = try c.decodeIfPresent(String.self, forKey: .localFolder)
        tabs = try c.decodeIfPresent([String].self, forKey: .tabs) ?? []
        activeWindowId = try c.decodeIfPresent(Int.self, forKey: .activeWindowId)
    }

    /// Whether this task carries any GitHub binding at all.
    var hasGitHub: Bool { currentIssue != nil || !(githubRepo ?? "").isEmpty }

    /// Minutes this task logged in `sprintId`, or 0 if it has none there.
    func minutes(inSprint sprintId: String) -> Double {
        sprintsWithTime.first { $0.sprintId == sprintId }?.totalMins ?? 0
    }
}

// MARK: - Task sub-objects

/// One `{sprint_id, sprint_title, total_mins}` entry from
/// `task_sprints_with_time()`. The bulky per-entry `logs` key is stripped by the
/// daemon — the task's full `logs` array is sent once.
struct SprintTime: Codable, Sendable, Hashable {
    let sprintId: String?
    let sprintTitle: String?
    let fieldId: String?
    /// ISO `yyyy-MM-dd`, used to sort sprints newest-first.
    let startDate: String?
    let totalMins: Double

    enum CodingKeys: String, CodingKey {
        case sprintId = "sprint_id"
        case sprintTitle = "sprint_title"
        case fieldId = "field_id"
        case startDate = "start_date"
        case totalMins = "total_mins"
    }

    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sprintId = try c.decodeIfPresent(String.self, forKey: .sprintId)
        sprintTitle = try c.decodeIfPresent(String.self, forKey: .sprintTitle)
        fieldId = try c.decodeIfPresent(String.self, forKey: .fieldId)
        startDate = try c.decodeIfPresent(String.self, forKey: .startDate)
        totalMins = try c.decodeIfPresent(Double.self, forKey: .totalMins) ?? 0
    }
}

/// One entry of `task.sprint_issues[]` — the task's GitHub issue for one sprint.
///
/// `state` is the *issue's* open/closed state, independent of the task's status.
/// `hoursSynced` caches what GitHub was last told so a reconcile can skip no-op
/// API calls; it is never a source of truth.
struct SprintBinding: Codable, Sendable, Hashable {
    let sprintId: String?
    /// The sprint's title, e.g. `"Sprint 105"`.
    let sprint: String?
    /// Always a full `owner/repo#n` ref — a task's issues can live in different
    /// repos across sprints. `nil` for a binding that was never linked.
    let issue: String?
    let state: String?
    let hoursSynced: Double?
    let syncedAt: TimeInterval?
    let createdAt: TimeInterval?
    /// Present only when two bindings collided on one sprint during migration.
    let supersededIssues: [String]?

    enum CodingKeys: String, CodingKey {
        case sprint, issue, state
        case sprintId = "sprint_id"
        case hoursSynced = "hours_synced"
        case syncedAt = "synced_at"
        case createdAt = "created_at"
        case supersededIssues = "superseded_issues"
    }

    var isClosed: Bool { state == "closed" }
}

/// One time-log entry. `minutes` is the source of truth; `startedAt`/`endedAt`
/// are present on timer sessions but absent on 29 of the owner's 416 logs, which
/// is why they are optional (plan §10).
struct LogEntry: Codable, Sendable, Identifiable, Hashable {
    let id: String
    let minutes: Double
    let note: String
    let at: TimeInterval?
    let startedAt: TimeInterval?
    let endedAt: TimeInterval?
    let uploadedAt: TimeInterval?
    /// Set when the entry came from a Google Calendar import; prevents re-import.
    let calendarEventUid: String?

    enum CodingKeys: String, CodingKey {
        case id, minutes, note, at
        case startedAt = "started_at"
        case endedAt = "ended_at"
        case uploadedAt = "uploaded_at"
        case calendarEventUid = "calendar_event_uid"
    }

    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decodeIfPresent(String.self, forKey: .id) ?? UUID().uuidString
        minutes = try c.decodeIfPresent(Double.self, forKey: .minutes) ?? 0
        note = try c.decodeIfPresent(String.self, forKey: .note) ?? ""
        at = try c.decodeIfPresent(TimeInterval.self, forKey: .at)
        startedAt = try c.decodeIfPresent(TimeInterval.self, forKey: .startedAt)
        endedAt = try c.decodeIfPresent(TimeInterval.self, forKey: .endedAt)
        uploadedAt = try c.decodeIfPresent(TimeInterval.self, forKey: .uploadedAt)
        calendarEventUid = try c.decodeIfPresent(String.self, forKey: .calendarEventUid)
    }

    /// Mirrors `wt.log_effective_date()`: prefer when the work started over when
    /// it was recorded.
    var effectiveDate: TimeInterval? { startedAt ?? at }

    /// True for the logs that have no wall-clock span. The Gantt must render
    /// these honestly rather than fabricating a time of day (plan §10).
    var hasWallClock: Bool { startedAt != nil && endedAt != nil }
}

// MARK: - Top-level sub-objects

/// A tracker role. Roles are pure categorization; GitHub repo/activity/type are
/// per-task fields, not role fields.
struct Role: Codable, Sendable, Identifiable, Hashable {
    let id: String
    let label: String?
    /// The color *name* stored in the data file (`blue`, `white`, `magenta`, …),
    /// mapped onto system colors by `RolePalette`. Never a hex string.
    let color: String?

    /// The label if set, otherwise the id — three of the owner's roles have a
    /// label that differs from the id, and one (`iron infusion`) has a space.
    var displayName: String {
        if let label, !label.isEmpty { return label }
        return id
    }
}

/// One sprint from `config.sprints_cache`. Dates are ISO `yyyy-MM-dd` strings on
/// the wire, exactly as `save_sprints_cache()` persists them.
struct Sprint: Codable, Sendable, Identifiable, Hashable {
    let id: String
    let title: String?
    let startDate: String?
    let endDate: String?
    let fieldId: String?

    enum CodingKeys: String, CodingKey {
        case id, title
        case startDate = "start_date"
        case endDate = "end_date"
        case fieldId = "field_id"
    }

    var displayName: String { title ?? id }

    /// `startDate` parsed, for sorting and for the Gantt's x-range.
    var start: Date? { Self.isoDay(startDate) }
    var end: Date? { Self.isoDay(endDate) }

    private static let dayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .iso8601)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone.current
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    private static func isoDay(_ string: String?) -> Date? {
        guard let string, !string.isEmpty else { return nil }
        return dayFormatter.date(from: string)
    }
}

/// The running timer. Only `task_id` and raw epoch `started_at` are sent on the
/// v1 snapshot — the client resolves the title from `tasks` and ticks elapsed
/// locally (plan §4).
struct ActiveTimer: Codable, Sendable, Equatable {
    let taskId: String?
    let startedAt: TimeInterval?

    enum CodingKeys: String, CodingKey {
        case taskId = "task_id"
        case startedAt = "started_at"
    }

    /// Seconds elapsed since the timer started, relative to `now`.
    func elapsed(asOf now: Date = Date()) -> TimeInterval {
        guard let startedAt else { return 0 }
        return max(0, now.timeIntervalSince1970 - startedAt)
    }
}

/// The GitHub Project's full option lists, for the task editor's pickers.
/// The owner's data has 38 activity options and **zero** type options, so an
/// empty list here is normal and must not be read as "not loaded".
struct ProjectOptions: Codable, Sendable, Equatable {
    let activity: [String]
    let type: [String]

    init(activity: [String] = [], type: [String] = []) {
        self.activity = activity
        self.type = type
    }

    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        activity = try c.decodeIfPresent([String].self, forKey: .activity) ?? []
        type = try c.decodeIfPresent([String].self, forKey: .type) ?? []
    }
}

/// The slice of `config` the snapshot carries.
///
/// `githubProjectNumber` is decoded through `FlexibleInt` because the live data
/// file stores it as the **string** `"565"` while `wt config` would accept an
/// int — a strict `Int` here fails the whole snapshot decode.
struct SnapshotConfig: Codable, Sendable, Equatable {
    let githubProjectOwner: String?
    let githubProjectNumber: FlexibleInt?
    let githubRepo: String?

    init(githubProjectOwner: String? = nil,
         githubProjectNumber: FlexibleInt? = nil,
         githubRepo: String? = nil) {
        self.githubProjectOwner = githubProjectOwner
        self.githubProjectNumber = githubProjectNumber
        self.githubRepo = githubRepo
    }

    enum CodingKeys: String, CodingKey {
        case githubProjectOwner = "github_project_owner"
        case githubProjectNumber = "github_project_number"
        case githubRepo = "github_repo"
    }
}

/// An integer that may arrive as a JSON number or a JSON string.
struct FlexibleInt: Codable, Sendable, Equatable, CustomStringConvertible {
    let value: Int?
    let raw: String?

    init(from decoder: any Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() {
            value = nil
            raw = nil
        } else if let int = try? c.decode(Int.self) {
            value = int
            raw = String(int)
        } else {
            let text = try c.decode(String.self)
            value = Int(text)
            raw = text
        }
    }

    func encode(to encoder: any Encoder) throws {
        var c = encoder.singleValueContainer()
        if let raw { try c.encode(raw) } else { try c.encodeNil() }
    }

    var description: String { raw ?? "—" }
}

/// `probe_data_file()`'s verdict, carried on both `/v1/snapshot` and
/// `/v1/health`.
///
/// This is the difference between "the tracker is empty" and "the data file
/// could not be read" — the second-Mac Full-Disk-Access failure, which
/// `wt.load()` otherwise masks as an empty dataset (plan risk #9).
struct DataFileProbe: Codable, Sendable, Equatable {
    let path: String?
    let readable: Bool
    /// One of `ok` / `missing` / `permission_denied` / `empty_file` /
    /// `unparseable` / `no_tasks`.
    let reason: String?
    let mtime: TimeInterval?
    let size: Int?
    let tasks: Int?
    let detail: String?

    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        path = try c.decodeIfPresent(String.self, forKey: .path)
        readable = try c.decodeIfPresent(Bool.self, forKey: .readable) ?? true
        reason = try c.decodeIfPresent(String.self, forKey: .reason)
        mtime = try c.decodeIfPresent(TimeInterval.self, forKey: .mtime)
        size = try c.decodeIfPresent(Int.self, forKey: .size)
        tasks = try c.decodeIfPresent(Int.self, forKey: .tasks)
        detail = try c.decodeIfPresent(String.self, forKey: .detail)
    }

    /// A user-facing explanation for the states that need action.
    var advice: String? {
        switch reason {
        case "ok", nil: nil
        case "permission_denied":
            "The data file is inside iCloud Drive. Grant Full Disk Access to the "
            + "daemon's Python interpreter, then restart it."
        case "missing": "The data file does not exist at this path."
        case "empty_file": "The file is a dataless iCloud placeholder. Run "
            + "`brctl download ~/WorkloadTracker/.workload_tracker.json`."
        case "unparseable": "The file exists but is not valid JSON. Repair it by hand."
        case "no_tasks": "The file parsed but contains no tasks."
        default: detail
        }
    }
}

// MARK: - Health

/// `GET /v1/health`.
struct Health: Codable, Sendable, Equatable {
    let ok: Bool
    let version: String?
    let pid: Int?
    let port: Int?
    let legacyPort: Int?
    let startedAt: TimeInterval?
    let uptimeSeconds: Double?
    let dataFile: DataFileProbe?
    /// Whether `tracker.py` is holding :7373. When it is, the TUI can clobber
    /// the daemon's writes (plan risk #1) and the UI shows a persistent warning.
    let tuiBridge: TUIBridge?
    let subscribers: Int?
    let python: String?

    struct TUIBridge: Codable, Sendable, Equatable {
        let port: Int?
        let running: Bool
    }

    enum CodingKeys: String, CodingKey {
        case ok, version, pid, port, subscribers, python
        case legacyPort = "legacy_port"
        case startedAt = "started_at"
        case uptimeSeconds = "uptime_seconds"
        case dataFile = "data_file"
        case tuiBridge = "tui_bridge"
    }

    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = try c.decodeIfPresent(Bool.self, forKey: .ok) ?? false
        version = try c.decodeIfPresent(String.self, forKey: .version)
        pid = try c.decodeIfPresent(Int.self, forKey: .pid)
        port = try c.decodeIfPresent(Int.self, forKey: .port)
        legacyPort = try c.decodeIfPresent(Int.self, forKey: .legacyPort)
        startedAt = try c.decodeIfPresent(TimeInterval.self, forKey: .startedAt)
        uptimeSeconds = try c.decodeIfPresent(Double.self, forKey: .uptimeSeconds)
        dataFile = try c.decodeIfPresent(DataFileProbe.self, forKey: .dataFile)
        tuiBridge = try c.decodeIfPresent(TUIBridge.self, forKey: .tuiBridge)
        subscribers = try c.decodeIfPresent(Int.self, forKey: .subscribers)
        python = try c.decodeIfPresent(String.self, forKey: .python)
    }
}
