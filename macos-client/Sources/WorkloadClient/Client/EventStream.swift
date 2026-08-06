import Foundation

// MARK: - Frames

/// One decoded SSE frame from `GET /v1/events`.
///
/// The daemon emits five event names: `hello`, `changed`, `progress`, `error`
/// and `heartbeat`. `id` is present only on real events (heartbeats and `hello`
/// are framed with `id: 0`, which the daemon omits), so a gap in `id` means
/// events were missed — which is fine, because the stream is deliberately **not
/// replayable**: every `changed` means "refetch the snapshot".
struct DaemonEvent: Sendable, Equatable {
    /// The `id:` field, when the daemon sent one.
    var id: Int?
    /// The `event:` field. `"message"` per the SSE spec when absent, though the
    /// daemon always names its events.
    var name: String
    /// The `data:` field, raw. Multi-line data is joined with `\n` per spec.
    var data: String
    /// The `retry:` field in milliseconds, when present.
    var retry: Int?

    /// Decodes `data` as JSON into `type`, or `nil` if it does not fit.
    func payload<T: Decodable>(_ type: T.Type,
                               decoder: JSONDecoder = JSONDecoder()) -> T? {
        guard let bytes = data.data(using: .utf8) else { return nil }
        return try? decoder.decode(type, from: bytes)
    }
}

/// `event: hello` — sent immediately on connect.
struct HelloPayload: Decodable, Sendable, Equatable {
    let version: String?
    let heartbeatSeconds: Double?
    let at: TimeInterval?

    enum CodingKeys: String, CodingKey {
        case version, at
        case heartbeatSeconds = "heartbeat_seconds"
    }
}

/// `event: changed` — "the data file moved, refetch the snapshot".
///
/// `source` is `"daemon"` (the daemon wrote) or `"external"` (its 1 Hz watcher
/// saw the mtime move: a CLI write, a TUI save, or iCloud landing a copy from
/// the other Mac).
struct ChangedPayload: Decodable, Sendable, Equatable {
    let source: String?
    let reason: String?
    let mtime: TimeInterval?
    let at: TimeInterval?
}

/// `event: progress` — a long `gh`-backed operation reporting in.
struct ProgressPayload: Decodable, Sendable, Equatable {
    let operationId: String?
    let op: String?
    let taskId: String?
    /// `"started"` / `"running"` / `"completed"`.
    let state: String?
    let message: String?
    let at: TimeInterval?

    enum CodingKeys: String, CodingKey {
        case op, state, message, at
        case operationId = "operation_id"
        case taskId = "task_id"
    }
}

/// `event: error` — a background operation failed. Carries the same
/// `{"error": {code, message}}` envelope as an HTTP error body.
struct StreamErrorPayload: Decodable, Sendable {
    let operationId: String?
    let op: String?
    let taskId: String?
    let status: Int?
    let error: DaemonErrorBody?

    enum CodingKeys: String, CodingKey {
        case op, status, error
        case operationId = "operation_id"
        case taskId = "task_id"
    }

    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        operationId = try c.decodeIfPresent(String.self, forKey: .operationId)
        op = try c.decodeIfPresent(String.self, forKey: .op)
        taskId = try c.decodeIfPresent(String.self, forKey: .taskId)
        status = try c.decodeIfPresent(Int.self, forKey: .status)
        // The envelope wraps itself in an "error" key, so decode the whole
        // payload as the envelope rather than the nested value.
        error = try? DaemonErrorBody(from: decoder)
    }
}

// MARK: - Parser

/// A pure, incremental SSE frame parser.
///
/// Split out from the connection loop so it is unit-testable without a socket.
/// Implements the subset of the `text/event-stream` grammar the daemon emits:
/// `id:`, `event:`, `data:` (repeatable), `retry:`, comment lines starting with
/// `:`, and a blank line as the frame terminator. An optional single space after
/// the colon is stripped, per spec.
struct SSEFrameParser: Sendable {
    private var id: Int?
    private var name: String?
    private var dataLines: [String] = []
    private var retry: Int?

    init() {}

    /// Feeds one line (without its trailing newline). Returns a frame when the
    /// line terminated one.
    mutating func consume(line: String) -> DaemonEvent? {
        // A blank line dispatches the buffered frame.
        if line.isEmpty {
            defer { reset() }
            // A frame carrying no `data:` at all is not dispatched, per spec —
            // but a bare `retry:` still needs to reach the caller, so surface it
            // as a frame with an empty name.
            guard !dataLines.isEmpty || retry != nil else { return nil }
            return DaemonEvent(id: id,
                               name: name ?? (dataLines.isEmpty ? "" : "message"),
                               data: dataLines.joined(separator: "\n"),
                               retry: retry)
        }

        // Comment / keep-alive line.
        if line.hasPrefix(":") { return nil }

        let field: String
        var value: String
        if let colon = line.firstIndex(of: ":") {
            field = String(line[line.startIndex..<colon])
            value = String(line[line.index(after: colon)...])
            if value.hasPrefix(" ") { value.removeFirst() }
        } else {
            field = line
            value = ""
        }

        switch field {
        case "id": id = Int(value)
        case "event": name = value
        case "data": dataLines.append(value)
        case "retry": retry = Int(value)
        default: break  // Unknown fields are ignored, per spec.
        }
        return nil
    }

    private mutating func reset() {
        id = nil
        name = nil
        dataLines = []
        retry = nil
    }

    /// Convenience for tests and for parsing a complete buffered response.
    static func parse(_ text: String) -> [DaemonEvent] {
        var parser = SSEFrameParser()
        var events: [DaemonEvent] = []
        // `split(omittingEmptySubsequences: false)` keeps the blank lines that
        // terminate frames — dropping them would merge every frame into one.
        for line in text.replacingOccurrences(of: "\r\n", with: "\n")
            .split(separator: "\n", omittingEmptySubsequences: false) {
            if let event = parser.consume(line: String(line)) { events.append(event) }
        }
        return events
    }
}

// MARK: - Stream

/// A reconnecting SSE client for `GET /v1/events`.
///
/// Yields `Update`s on an `AsyncStream`. The consumer (`Store`) treats **every**
/// `changed` as "refetch the snapshot" — the daemon ignores `Last-Event-ID` by
/// design, so there is no backlog to replay and no incremental state to keep in
/// sync.
///
/// Reconnection is exponential with jitter, capped, and starts over on a clean
/// connect. The daemon's own `retry: 2000` hint is honoured as the floor.
actor EventStream {

    /// What the consumer sees.
    enum Update: Sendable {
        /// The stream connected and the daemon sent `hello`.
        case connected(HelloPayload?)
        /// A named event arrived.
        case event(DaemonEvent)
        /// The stream dropped; a reconnect is scheduled in `retryIn` seconds.
        case disconnected(error: String?, retryIn: TimeInterval)
    }

    private let client: DaemonClient
    private var task: _Concurrency.Task<Void, Never>?
    private var continuation: AsyncStream<Update>.Continuation?

    /// Backoff bounds. The floor matches the daemon's `retry: 2000`.
    private let minimumBackoff: TimeInterval = 2
    private let maximumBackoff: TimeInterval = 30

    init(client: DaemonClient) {
        self.client = client
    }

    /// Starts (or restarts) the connection loop and returns the update stream.
    func start() -> AsyncStream<Update> {
        stop()
        let (stream, continuation) = AsyncStream<Update>.makeStream(bufferingPolicy: .bufferingNewest(64))
        self.continuation = continuation
        self.task = _Concurrency.Task { [weak self] in
            await self?.run(yielding: continuation)
        }
        return stream
    }

    /// Cancels the connection loop and finishes the stream.
    func stop() {
        task?.cancel()
        task = nil
        continuation?.finish()
        continuation = nil
    }

    private func run(yielding continuation: AsyncStream<Update>.Continuation) async {
        var attempt = 0
        while !_Concurrency.Task.isCancelled {
            var connectedCleanly = false
            var failure: String?
            do {
                try await connectOnce(yielding: continuation) { connectedCleanly = true }
            } catch is CancellationError {
                return
            } catch {
                failure = (error as? LocalizedError)?.errorDescription
                    ?? error.localizedDescription
            }
            if _Concurrency.Task.isCancelled { return }

            // A connection that got as far as `hello` resets the backoff, so a
            // daemon restart reconnects in 2s rather than inheriting a long
            // delay from an earlier outage.
            attempt = connectedCleanly ? 0 : attempt + 1
            let delay = backoff(forAttempt: attempt)
            continuation.yield(.disconnected(error: failure, retryIn: delay))
            try? await _Concurrency.Task.sleep(for: .seconds(delay))
        }
    }

    /// One connection attempt: opens the stream and pumps frames until it ends.
    private func connectOnce(yielding continuation: AsyncStream<Update>.Continuation,
                             onConnect: () -> Void) async throws {
        let request = try await client.eventsRequest()
        let session = await client.sharedSession

        let bytes: URLSession.AsyncBytes
        let response: URLResponse
        do {
            (bytes, response) = try await session.bytes(for: request)
        } catch {
            throw DaemonClientError.unreachable(underlying: error)
        }

        let status = (response as? HTTPURLResponse)?.statusCode ?? 200
        guard (200...299).contains(status) else {
            // A 401 here is the same contract failure it is on a GET; surface
            // the code rather than a bare status.
            throw DaemonClientError.http(status: status, body: nil)
        }

        onConnect()
        var parser = SSEFrameParser()
        var announced = false

        for try await line in bytes.lines {
            guard let event = parser.consume(line: line) else { continue }
            if event.name == "hello", !announced {
                announced = true
                continuation.yield(.connected(event.payload(HelloPayload.self)))
                continue
            }
            continuation.yield(.event(event))
        }
        // `bytes.lines` finishing means the daemon closed the connection.
    }

    /// Exponential backoff with ±25% jitter, so two clients that lost the daemon
    /// at the same moment do not retry in lockstep.
    private func backoff(forAttempt attempt: Int) -> TimeInterval {
        let base = min(maximumBackoff, minimumBackoff * pow(2, Double(max(0, attempt - 1))))
        let jitter = Double.random(in: 0.75...1.25)
        return min(maximumBackoff, max(minimumBackoff, base * jitter))
    }
}

// MARK: - AsyncBytes line splitting

private extension URLSession.AsyncBytes {
    /// `URLSession.AsyncBytes.lines` already splits on `\n` and `\r\n`, but it
    /// drops the empty lines that terminate SSE frames. Re-derive them from the
    /// raw byte stream instead.
    var lines: AsyncThrowingStream<String, any Error> {
        AsyncThrowingStream { continuation in
            let task = _Concurrency.Task {
                var buffer: [UInt8] = []
                do {
                    for try await byte in self {
                        if byte == 0x0A {  // \n
                            if buffer.last == 0x0D { buffer.removeLast() }  // \r\n
                            continuation.yield(String(decoding: buffer, as: UTF8.self))
                            buffer.removeAll(keepingCapacity: true)
                        } else {
                            buffer.append(byte)
                        }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}
