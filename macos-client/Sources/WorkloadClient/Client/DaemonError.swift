import Foundation

/// A machine-readable error code from the daemon.
///
/// The daemon's error body is always
/// `{"error": {"code", "message", "details"?}}` and the **code** is the stable
/// contract — `wt_api.py` raises 23 of them and `wt_daemon.py` adds 9 of its
/// own, with an import-time assertion in the daemon that the two sets never
/// collide and that every `wt_api` code is mapped to a status.
///
/// Modelled as a `RawRepresentable` enum with an `unknown` case rather than a
/// closed enum so a code added on the Python side degrades to "show the
/// message" instead of failing to decode the error itself.
enum DaemonErrorCode: RawRepresentable, Sendable, Hashable {
    // wt_api.ERROR_CODES — the 23 command-layer codes.
    case ambiguousTask, closeFailed, githubFailed, invalidArgs, invalidMinutes
    case invalidRepo, invalidRole, invalidSplit, invalidStatus, issueNotFound
    case logNotFound, noActiveTimer, noChanges, noDefaultRepo, noRepo, noSprints
    case notLinked, reconcileFailed, sameLog, sprintNotFound, taskNotFound
    case unknownActivity, unknownType

    // wt_daemon.DAEMON_ERROR_CODES — transport and daemon-state codes.
    case unauthorized, notFound, methodNotAllowed, badJSON, badRequest
    case lockTimeout, dataUnreadable, unavailable, internalError

    /// A code this build does not know about.
    case unknown(String)

    private static let table: [String: DaemonErrorCode] = [
        "ambiguous_task": .ambiguousTask, "close_failed": .closeFailed,
        "github_failed": .githubFailed, "invalid_args": .invalidArgs,
        "invalid_minutes": .invalidMinutes, "invalid_repo": .invalidRepo,
        "invalid_role": .invalidRole, "invalid_split": .invalidSplit,
        "invalid_status": .invalidStatus, "issue_not_found": .issueNotFound,
        "log_not_found": .logNotFound, "no_active_timer": .noActiveTimer,
        "no_changes": .noChanges, "no_default_repo": .noDefaultRepo,
        "no_repo": .noRepo, "no_sprints": .noSprints, "not_linked": .notLinked,
        "reconcile_failed": .reconcileFailed, "same_log": .sameLog,
        "sprint_not_found": .sprintNotFound, "task_not_found": .taskNotFound,
        "unknown_activity": .unknownActivity, "unknown_type": .unknownType,
        "unauthorized": .unauthorized, "not_found": .notFound,
        "method_not_allowed": .methodNotAllowed, "bad_json": .badJSON,
        "bad_request": .badRequest, "lock_timeout": .lockTimeout,
        "data_unreadable": .dataUnreadable, "unavailable": .unavailable,
        "internal_error": .internalError,
    ]

    init(rawValue: String) {
        self = Self.table[rawValue] ?? .unknown(rawValue)
    }

    /// Written as a switch rather than a reverse lookup table.
    ///
    /// A `[DaemonErrorCode: String]` reverse map **deadlocks**: `Hashable` is
    /// synthesized from `RawRepresentable`, so hashing a case calls `rawValue`,
    /// which would read the static map, which is still inside its own
    /// `dispatch_once`. It traps on first use, which the round-trip test caught.
    var rawValue: String {
        switch self {
        case .ambiguousTask: "ambiguous_task"
        case .closeFailed: "close_failed"
        case .githubFailed: "github_failed"
        case .invalidArgs: "invalid_args"
        case .invalidMinutes: "invalid_minutes"
        case .invalidRepo: "invalid_repo"
        case .invalidRole: "invalid_role"
        case .invalidSplit: "invalid_split"
        case .invalidStatus: "invalid_status"
        case .issueNotFound: "issue_not_found"
        case .logNotFound: "log_not_found"
        case .noActiveTimer: "no_active_timer"
        case .noChanges: "no_changes"
        case .noDefaultRepo: "no_default_repo"
        case .noRepo: "no_repo"
        case .noSprints: "no_sprints"
        case .notLinked: "not_linked"
        case .reconcileFailed: "reconcile_failed"
        case .sameLog: "same_log"
        case .sprintNotFound: "sprint_not_found"
        case .taskNotFound: "task_not_found"
        case .unknownActivity: "unknown_activity"
        case .unknownType: "unknown_type"
        case .unauthorized: "unauthorized"
        case .notFound: "not_found"
        case .methodNotAllowed: "method_not_allowed"
        case .badJSON: "bad_json"
        case .badRequest: "bad_request"
        case .lockTimeout: "lock_timeout"
        case .dataUnreadable: "data_unreadable"
        case .unavailable: "unavailable"
        case .internalError: "internal_error"
        case .unknown(let raw): raw
        }
    }

    /// Every code this build recognises. Used by the round-trip test that
    /// guards against a typo in the table above.
    static var allKnown: [DaemonErrorCode] { Array(table.values) }
}

/// The `{"error": {...}}` envelope, decoded.
struct DaemonErrorBody: Decodable, Sendable {
    let code: DaemonErrorCode
    let message: String
    /// Free-form extra context (`available`, `task_id`, …). Kept as a string
    /// map so no call site has to know each code's detail shape.
    let details: [String: String]

    private struct Envelope: Decodable {
        struct Detail: Decodable {
            let code: String?
            let message: String?
            let details: [String: JSONValue]?
        }
        let error: Detail
    }

    init(from decoder: any Decoder) throws {
        let envelope = try Envelope(from: decoder)
        code = DaemonErrorCode(rawValue: envelope.error.code ?? "internal_error")
        message = envelope.error.message ?? "The daemon returned an error."
        details = (envelope.error.details ?? [:]).mapValues(\.stringValue)
    }
}

/// Errors surfaced by `DaemonClient`.
///
/// `unreachable` is deliberately its own case, exactly as in
/// workload-macos-monitor's `TrackerError`: a daemon that is down must render as
/// a distinct state and never as an empty board.
enum DaemonClientError: Error, Sendable {
    /// Connection refused / timeout / offline. The daemon is not answering.
    case unreachable(underlying: any Error)
    /// The bearer token file is missing or unreadable.
    case missingToken(path: String, underlying: (any Error)?)
    /// The daemon answered with its typed `{"error": {code, message}}` body.
    case api(code: DaemonErrorCode, message: String, status: Int, details: [String: String])
    /// A non-2xx response whose body was not the typed envelope.
    case http(status: Int, body: String?)
    /// The response was 2xx but did not decode into the expected shape.
    case decoding(underlying: any Error)
    /// The configured base URL string was not a valid URL.
    case invalidBaseURL(String)

    /// Whether this represents "the daemon is not there", as opposed to "the
    /// daemon said no".
    var isUnreachable: Bool {
        if case .unreachable = self { return true }
        return false
    }

    /// The daemon's code, when there was one.
    var code: DaemonErrorCode? {
        if case .api(let code, _, _, _) = self { return code }
        return nil
    }
}

extension DaemonClientError: LocalizedError {
    var errorDescription: String? {
        switch self {
        case .unreachable:
            "The workload daemon is not responding."
        case .missingToken(let path, _):
            "Could not read the daemon token at \(path)."
        case .api(let code, let message, _, _):
            "\(message) (\(code.rawValue))"
        case .http(let status, _):
            "The daemon returned HTTP \(status)."
        case .decoding:
            "The daemon's response could not be decoded."
        case .invalidBaseURL(let string):
            "“\(string)” is not a valid daemon URL."
        }
    }

    var recoverySuggestion: String? {
        switch self {
        case .unreachable:
            "Check that wt_daemon.py is running: "
            + "launchctl print gui/$(id -u)/com.carlossanabria.wtdaemon"
        case .missingToken:
            "The daemon writes the token on first run, mode 0600."
        case .api(.unauthorized, _, _, _):
            "The token file and the running daemon disagree. Restart the daemon."
        case .api(.dataUnreadable, _, _, _):
            "The daemon refused to touch the data file. Check Full Disk Access."
        default:
            nil
        }
    }
}

/// A minimal `Any`-free JSON value, used only to flatten error `details` into
/// strings without importing a JSON library.
enum JSONValue: Decodable, Sendable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case array([JSONValue])
    case object([String: JSONValue])
    case null

    init(from decoder: any Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(Double.self) { self = .number(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode([JSONValue].self) { self = .array(v) }
        else if let v = try? c.decode([String: JSONValue].self) { self = .object(v) }
        else { self = .null }
    }

    var stringValue: String {
        switch self {
        case .string(let v): v
        case .number(let v): v == v.rounded() ? String(Int(v)) : String(v)
        case .bool(let v): v ? "true" : "false"
        case .array(let v): v.map(\.stringValue).joined(separator: ", ")
        case .object(let v): v.map { "\($0.key)=\($0.value.stringValue)" }
            .sorted().joined(separator: ", ")
        case .null: ""
        }
    }
}
