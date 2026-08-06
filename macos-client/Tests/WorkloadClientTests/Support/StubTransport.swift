import Foundation
@testable import WorkloadClient

/// An in-process HTTP stub for `DaemonClient`, built on `URLProtocol`.
///
/// The point is not to avoid a network round trip — it is to make **"which
/// requests were issued"** an assertable fact. Phase 4's safety properties are
/// all of that shape:
///
/// * a rejected drop must issue **no** request at all;
/// * a Done drop must issue `close/plan` and **not** `close`;
/// * a failed status change must roll the board back.
///
/// A mock `DaemonClient` protocol would prove the same things about a mock. This
/// drives the real client, the real URL building, the real bearer-token header
/// and the real error decoding, and records what came out the bottom.
final class StubTransport: @unchecked Sendable {

    /// One recorded request.
    struct Recorded: Equatable {
        let method: String
        let path: String
        /// The decoded JSON body, flattened to strings so assertions stay short.
        let body: [String: String]

        var line: String { "\(method) \(path)" }
    }

    /// What to answer with.
    struct Response {
        let status: Int
        let body: Data

        static func json(_ object: Any, status: Int = 200) -> Response {
            Response(status: status,
                     body: try! JSONSerialization.data(withJSONObject: object))
        }

        /// Pre-encoded body. `Data` is `Sendable`, so a fixture captured this
        /// way can cross into the `@Sendable` responder closure; `[String: Any]`
        /// cannot.
        static func raw(_ data: Data, status: Int = 200) -> Response {
            Response(status: status, body: data)
        }

        /// The daemon's error envelope.
        static func failure(code: String, message: String, status: Int = 400) -> Response {
            .json(["error": ["code": code, "message": message]], status: status)
        }
    }

    private let lock = NSLock()
    private var recorded: [Recorded] = []
    private var handler: (@Sendable (Recorded) -> Response)?
    let port: Int
    let tokenFileURL: URL

    /// Every request the client actually sent, in order.
    var requests: [Recorded] {
        lock.lock(); defer { lock.unlock() }
        return recorded
    }

    var requestLines: [String] { requests.map(\.line) }

    init(file: StaticString = #filePath, line: UInt = #line) {
        self.port = StubTransport.nextPort()
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("wt-stub-\(port)", isDirectory: true)
        try? FileManager.default.createDirectory(at: directory,
                                                 withIntermediateDirectories: true)
        self.tokenFileURL = directory.appendingPathComponent("token")
        try? "stub-token-\(port)\n".write(to: tokenFileURL, atomically: true, encoding: .utf8)
        StubTransport.register(self)
    }

    deinit { StubTransport.unregister(port) }

    /// Installs the responder. Called with each request; returns its response.
    func respond(_ handler: @escaping @Sendable (Recorded) -> Response) {
        lock.lock(); defer { lock.unlock() }
        self.handler = handler
    }

    /// A `DaemonClient` wired to this stub.
    func makeClient(timeout: TimeInterval = 5) -> DaemonClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubURLProtocol.self]
        return DaemonClient(
            configuration: .init(baseURL: URL(string: "http://127.0.0.1:\(port)")!,
                                 tokenFileURL: tokenFileURL,
                                 timeout: timeout),
            session: URLSession(configuration: configuration))
    }

    func reset() {
        lock.lock(); defer { lock.unlock() }
        recorded.removeAll()
    }

    // MARK: - Called from the URLProtocol

    fileprivate func handle(_ request: Recorded) -> Response {
        lock.lock()
        recorded.append(request)
        let handler = self.handler
        lock.unlock()
        return handler?(request)
            ?? .failure(code: "not_found", message: "no stub for \(request.line)", status: 404)
    }

    // MARK: - Registry

    // `URLProtocol` is instantiated by the loading system with no way to inject
    // context, so instances are found by the port in the request URL. Each
    // transport takes a unique fake port, which also keeps parallel test classes
    // from seeing each other's requests.
    private static let registryLock = NSLock()
    nonisolated(unsafe) private static var registry: [Int: StubTransport] = [:]
    nonisolated(unsafe) private static var portCounter = 49_000

    private static func nextPort() -> Int {
        registryLock.lock(); defer { registryLock.unlock() }
        portCounter += 1
        return portCounter
    }

    private static func register(_ transport: StubTransport) {
        registryLock.lock(); defer { registryLock.unlock() }
        registry[transport.port] = transport
    }

    private static func unregister(_ port: Int) {
        registryLock.lock(); defer { registryLock.unlock() }
        registry[port] = nil
    }

    fileprivate static func transport(forPort port: Int) -> StubTransport? {
        registryLock.lock(); defer { registryLock.unlock() }
        return registry[port]
    }
}

/// The `URLProtocol` that routes to a `StubTransport`.
final class StubURLProtocol: URLProtocol {

    override class func canInit(with request: URLRequest) -> Bool {
        guard let port = request.url?.port else { return false }
        return StubTransport.transport(forPort: port) != nil
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let url = request.url, let port = url.port,
              let transport = StubTransport.transport(forPort: port) else {
            client?.urlProtocol(self, didFailWithError: URLError(.badURL))
            return
        }

        let recorded = StubTransport.Recorded(
            method: request.httpMethod ?? "GET",
            path: url.path,
            body: Self.decodeBody(request))
        let response = transport.handle(recorded)

        let http = HTTPURLResponse(url: url, statusCode: response.status,
                                   httpVersion: "HTTP/1.1",
                                   headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: http, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: response.body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    /// `URLProtocol` sees an uploaded body as `httpBodyStream`, not `httpBody`,
    /// so both have to be read or every recorded body comes back empty.
    private static func decodeBody(_ request: URLRequest) -> [String: String] {
        var data = request.httpBody
        if data == nil, let stream = request.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var buffer = Data()
            let chunk = UnsafeMutablePointer<UInt8>.allocate(capacity: 4096)
            defer { chunk.deallocate() }
            while stream.hasBytesAvailable {
                let read = stream.read(chunk, maxLength: 4096)
                if read <= 0 { break }
                buffer.append(chunk, count: read)
            }
            data = buffer
        }
        guard let data, !data.isEmpty,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return [:] }
        return object.mapValues { value in
            if let bool = value as? Bool { return bool ? "true" : "false" }
            return String(describing: value)
        }
    }
}
