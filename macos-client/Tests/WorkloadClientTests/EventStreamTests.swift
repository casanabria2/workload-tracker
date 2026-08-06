import XCTest
@testable import WorkloadClient

/// `SSEFrameParser` tests. The parser is split out from the connection loop
/// precisely so this can run without a socket.
///
/// The frames below are the exact shapes `wt_daemon.ApiHandler._sse()` writes:
/// `id:` only on real events (heartbeat and hello go out with `id: 0`, which the
/// daemon omits), `event:` always, one `data:` line of compact JSON, then a
/// blank line.
final class EventStreamTests: XCTestCase {

    func testParsesTheDaemonsHelloFrame() {
        let text = """
        retry: 2000

        event: hello
        data: {"version": "1.0.0", "heartbeat_seconds": 15.0, "at": 1786022323.7}

        """
        let events = SSEFrameParser.parse(text)
        // The bare `retry:` frame is surfaced too, so the reconnect floor is
        // observable rather than silently swallowed.
        XCTAssertEqual(events.count, 2)
        XCTAssertEqual(events[0].retry, 2000)

        let hello = events[1]
        XCTAssertEqual(hello.name, "hello")
        XCTAssertNil(hello.id)
        let payload = hello.payload(HelloPayload.self)
        XCTAssertEqual(payload?.version, "1.0.0")
        XCTAssertEqual(payload?.heartbeatSeconds, 15.0)
    }

    func testParsesChangedProgressErrorAndHeartbeat() {
        let text = """
        id: 7
        event: changed
        data: {"source": "external", "reason": "file_mtime", "mtime": 1786022400.0, "at": 1786022401.0}

        id: 8
        event: progress
        data: {"operation_id": "20260806101112abcd", "op": "reconcile", "task_id": "t-1", "state": "running", "message": "Sprint 104: creating issue", "at": 1786022402.0}

        id: 9
        event: error
        data: {"operation_id": "20260806101112abcd", "op": "close", "task_id": "t-1", "status": 502, "error": {"code": "github_failed", "message": "gh issue close failed"}}

        event: heartbeat
        data: {"now": 1786022417.0}

        """
        let events = SSEFrameParser.parse(text)
        XCTAssertEqual(events.map(\.name), ["changed", "progress", "error", "heartbeat"])
        XCTAssertEqual(events.map(\.id), [7, 8, 9, nil])

        let changed = events[0].payload(ChangedPayload.self)
        XCTAssertEqual(changed?.source, "external")
        XCTAssertEqual(changed?.reason, "file_mtime")

        let progress = events[1].payload(ProgressPayload.self)
        XCTAssertEqual(progress?.operationId, "20260806101112abcd")
        XCTAssertEqual(progress?.op, "reconcile")
        XCTAssertEqual(progress?.state, "running")
        XCTAssertEqual(progress?.message, "Sprint 104: creating issue")

        let failure = events[2].payload(StreamErrorPayload.self)
        XCTAssertEqual(failure?.status, 502)
        XCTAssertEqual(failure?.error?.code, .githubFailed)
        XCTAssertEqual(failure?.error?.message, "gh issue close failed")
    }

    /// Blank lines are the frame terminator. Dropping them (which the naive
    /// `split(separator:)` does) merges every frame into one.
    func testBlankLineTerminatesAFrame() {
        var parser = SSEFrameParser()
        XCTAssertNil(parser.consume(line: "event: changed"))
        XCTAssertNil(parser.consume(line: #"data: {"source": "daemon"}"#))
        let event = parser.consume(line: "")
        XCTAssertEqual(event?.name, "changed")
        XCTAssertEqual(event?.data, #"{"source": "daemon"}"#)
        // Parser state resets: a second blank line emits nothing.
        XCTAssertNil(parser.consume(line: ""))
    }

    func testMultilineDataIsJoinedWithNewlines() {
        let events = SSEFrameParser.parse("event: changed\ndata: line one\ndata: line two\n\n")
        XCTAssertEqual(events.count, 1)
        XCTAssertEqual(events[0].data, "line one\nline two")
    }

    func testCommentLinesAreIgnored() {
        let events = SSEFrameParser.parse(": keep-alive\nevent: heartbeat\ndata: {}\n\n")
        XCTAssertEqual(events.count, 1)
        XCTAssertEqual(events[0].name, "heartbeat")
    }

    func testOnlyOneLeadingSpaceIsStripped() {
        let events = SSEFrameParser.parse("event: x\ndata:  two spaces\n\n")
        XCTAssertEqual(events[0].data, " two spaces")
    }

    func testFieldWithNoColon() {
        let events = SSEFrameParser.parse("event: x\ndata\n\n")
        XCTAssertEqual(events.count, 1)
        XCTAssertEqual(events[0].data, "")
    }

    func testCRLFFramingParsesIdentically() {
        let crlf = "event: changed\r\ndata: {}\r\n\r\n"
        let events = SSEFrameParser.parse(crlf)
        XCTAssertEqual(events.count, 1)
        XCTAssertEqual(events[0].name, "changed")
    }

    func testUnknownFieldsAreIgnored() {
        let events = SSEFrameParser.parse("event: x\nsomething: else\ndata: {}\n\n")
        XCTAssertEqual(events.count, 1)
        XCTAssertEqual(events[0].name, "x")
    }

    /// A partial frame with no terminating blank line is not dispatched — the
    /// consumer must not act on half a JSON document.
    func testUnterminatedFrameIsNotDispatched() {
        XCTAssertTrue(SSEFrameParser.parse("event: changed\ndata: {\"a\":").isEmpty)
    }

    /// A gap in `id` is detectable, which is all the client needs: the stream is
    /// deliberately not replayable, so every `changed` means "refetch".
    func testIdsAreExposedSoAGapIsDetectable() {
        let events = SSEFrameParser.parse(
            "id: 3\nevent: changed\ndata: {}\n\nid: 9\nevent: changed\ndata: {}\n\n")
        XCTAssertEqual(events.compactMap(\.id), [3, 9])
    }
}
