import Foundation

/// Async HTTP client for `wt_daemon.py`'s authenticated `/v1` API.
///
/// An `actor` for the same reason `TrackerClient` is one in the monitor: the
/// token is cached state and every call is `async`, so isolation is free and the
/// UI layer stays `@MainActor`.
///
/// **Phase 3 is read-only.** The only endpoints exposed here are `GET`s. Writes
/// arrive in Phase 4 with the Kanban board, and they must not be added
/// speculatively — every `POST` against the live daemon touches the owner's real
/// work history.
actor DaemonClient {

    /// Where to reach the daemon and how to authenticate.
    struct Configuration: Sendable, Equatable {
        var baseURL: URL
        /// Path to the bearer token file the daemon writes at mode 0600.
        var tokenFileURL: URL
        var timeout: TimeInterval

        static let `default` = Configuration(
            baseURL: URL(string: AppSettings.defaultBaseURL)!,
            tokenFileURL: AppSettings.defaultTokenFileURL,
            timeout: 8
        )

        /// Builds a configuration from the current user defaults, falling back
        /// to the defaults above when a stored value is blank or malformed.
        static func fromSettings() -> Configuration {
            var config = Configuration.default
            if let url = URL(string: AppSettings.baseURLString) { config.baseURL = url }
            config.tokenFileURL = AppSettings.tokenFileURL
            return config
        }
    }

    private let session: URLSession
    private let decoder: JSONDecoder
    private var configuration: Configuration
    /// Cached token. Re-read on a 401 so rotating the file does not need a
    /// relaunch.
    private var cachedToken: String?

    init(configuration: Configuration = .fromSettings(),
         session: URLSession? = nil) {
        self.configuration = configuration
        if let session {
            self.session = session
        } else {
            let config = URLSessionConfiguration.ephemeral
            config.requestCachePolicy = .reloadIgnoringLocalCacheData
            config.timeoutIntervalForRequest = 8
            // The SSE stream must not be torn down by the resource timeout; it
            // is deliberately long-lived. `EventStream` builds its own request
            // but shares this session.
            config.timeoutIntervalForResource = .infinity
            self.session = URLSession(configuration: config)
        }
        self.decoder = JSONDecoder()
    }

    /// Replaces the endpoint configuration (Settings changed). Drops the cached
    /// token, since a different daemon may use a different one.
    func reconfigure(_ configuration: Configuration) {
        self.configuration = configuration
        self.cachedToken = nil
    }

    var currentConfiguration: Configuration { configuration }

    // MARK: - Read-only API

    /// `GET /v1/health`. Also the attach probe used by `DaemonProcess`.
    func health() async throws -> Health {
        try await get("/v1/health", as: Health.self)
    }

    /// `GET /v1/snapshot` — the whole UI state in one round trip.
    func snapshot() async throws -> Snapshot {
        try await get("/v1/snapshot", as: Snapshot.self)
    }

    /// A cheap reachability probe that never throws, for the attach-first
    /// lifecycle decision.
    func isReachable() async -> Bool {
        (try? await health())?.ok == true
    }

    /// The authenticated request `EventStream` should open. Built here so the
    /// token handling lives in exactly one place.
    func eventsRequest() throws -> URLRequest {
        var request = URLRequest(url: configuration.baseURL.appending(path: "/v1/events"),
                                 // No timeout: the stream is open until the
                                 // daemon or the client closes it.
                                 timeoutInterval: .infinity)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        request.setValue("no-cache", forHTTPHeaderField: "Cache-Control")
        request.setValue("Bearer \(try token())", forHTTPHeaderField: "Authorization")
        return request
    }

    /// The shared session, so `EventStream` reuses one connection pool.
    var sharedSession: URLSession { session }

    // MARK: - Token

    /// Reads (and caches) the bearer token. Trimmed, because the daemon writes a
    /// trailing newline and `secrets.compare_digest` on the Python side compares
    /// the stripped value.
    private func token() throws -> String {
        if let cachedToken { return cachedToken }
        let path = configuration.tokenFileURL
        do {
            let raw = try String(contentsOf: path, encoding: .utf8)
            let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty else {
                throw DaemonClientError.missingToken(path: path.path, underlying: nil)
            }
            cachedToken = trimmed
            return trimmed
        } catch let error as DaemonClientError {
            throw error
        } catch {
            throw DaemonClientError.missingToken(path: path.path, underlying: error)
        }
    }

    // MARK: - Transport

    private func get<T: Decodable>(_ path: String, as type: T.Type) async throws -> T {
        var request = URLRequest(url: configuration.baseURL.appending(path: path),
                                 timeoutInterval: configuration.timeout)
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("Bearer \(try token())", forHTTPHeaderField: "Authorization")

        do {
            return try await perform(request, as: type)
        } catch DaemonClientError.api(.unauthorized, let message, let status, let details) {
            // The token may have rotated under us (the daemon regenerates it if
            // the file is deleted). Re-read once, then give up.
            cachedToken = nil
            guard let retryToken = try? token() else {
                throw DaemonClientError.api(code: .unauthorized, message: message,
                                            status: status, details: details)
            }
            request.setValue("Bearer \(retryToken)", forHTTPHeaderField: "Authorization")
            return try await perform(request, as: type)
        }
    }

    private func perform<T: Decodable>(_ request: URLRequest, as type: T.Type) async throws -> T {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            // Connection refused, offline, timeout, DNS — all "the daemon is
            // not there", which is a different UI state from "empty board".
            throw DaemonClientError.unreachable(underlying: error)
        }

        let status = (response as? HTTPURLResponse)?.statusCode ?? 200
        guard (200...299).contains(status) else {
            throw Self.mapErrorBody(data, status: status, decoder: decoder)
        }

        do {
            return try decoder.decode(type, from: data)
        } catch {
            throw DaemonClientError.decoding(underlying: error)
        }
    }

    /// Maps `{"error": {"code", "message", "details"?}}` onto a typed error
    /// **preserving the code**, falling back to a plain HTTP error when the body
    /// is not the envelope (which should not happen — the daemon turns even
    /// unhandled exceptions into `internal_error` — but a proxy or a crash could
    /// produce one).
    nonisolated static func mapErrorBody(_ data: Data, status: Int,
                                         decoder: JSONDecoder) -> DaemonClientError {
        if let body = try? decoder.decode(DaemonErrorBody.self, from: data) {
            return .api(code: body.code, message: body.message,
                        status: status, details: body.details)
        }
        let text = String(data: data, encoding: .utf8)
        return .http(status: status, body: text?.isEmpty == false ? text : nil)
    }
}
