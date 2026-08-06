import XCTest
@testable import WorkloadClient

/// What a drop actually *does*, at the transport.
///
/// `BoardDropRulesTests` proves the table; this proves the wiring obeys it —
/// which is the part that can rot. Every assertion is either "these exact
/// requests were issued" or "no request was issued", because that is the only
/// observation that distinguishes a rule from a comment.
@MainActor
final class BoardInteractionTests: XCTestCase {

    private func planData() throws -> Data {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: "close-plan", withExtension: "json",
                              subdirectory: "Fixtures"))
        return try Data(contentsOf: url)
    }

    private func snapshotData() throws -> Data {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: "snapshot", withExtension: "json",
                              subdirectory: "Fixtures"))
        return try Data(contentsOf: url)
    }

    private func makeStore(_ transport: StubTransport) throws -> Store {
        Store(client: transport.makeClient(),
              snapshot: try JSONDecoder().decode(Snapshot.self, from: try snapshotData()))
    }

    private func payload(_ id: String, _ status: TaskStatus) -> TaskDragPayload {
        TaskDragPayload(taskId: id, sourceStatus: status)
    }

    // MARK: - Rejected transitions issue nothing

    /// The strongest form of the rule: a refused drop must not reach the daemon
    /// at all — not as a status change, not as a plan, not as a probe.
    func testRejectedDropsIssueNoRequestWhatsoever() async throws {
        let transport = StubTransport()
        transport.respond { _ in
            XCTFail("a rejected drop must not issue a request")
            return .failure(code: "internal_error", message: "unreachable", status: 500)
        }
        let store = try makeStore(transport)

        // From Done — there is no reopen path in wt.py.
        await store.perform(drop: payload("t-done-nulls", .done), on: .todo)
        await store.perform(drop: payload("t-done-nulls", .done), on: .inProgress)
        // A recurrent card, in both directions.
        await store.perform(drop: payload("t-recurrent", .recurrent), on: .done)
        await store.perform(drop: payload("t-recurrent", .recurrent), on: .inProgress)
        await store.perform(drop: payload("t-minimal", .todo), on: .recurrent)
        // Same column.
        await store.perform(drop: payload("t-minimal", .todo), on: .todo)
        // An unknown status.
        await store.perform(drop: payload("t-future-status", .unknown("blocked")), on: .done)

        XCTAssertTrue(transport.requests.isEmpty,
                      "issued \(transport.requestLines)")
        XCTAssertNil(store.closeSheet, "a rejected drop must not open the close sheet")
        XCTAssertTrue(store.pendingStatus.isEmpty, "and must not move the card")
    }

    /// A refusal has to be visible. Silence would read as a broken drag.
    func testRejectionsAreExplainedToTheUser() async throws {
        let transport = StubTransport()
        transport.respond { _ in .failure(code: "internal_error", message: "x", status: 500) }
        let store = try makeStore(transport)

        await store.perform(drop: payload("t-done-nulls", .done), on: .inProgress)
        let reopen = try XCTUnwrap(store.feedback)
        XCTAssertTrue(reopen.isError)
        XCTAssertEqual(reopen.message, BoardDropRejection.reopenNotSupported.message)
        XCTAssertNotNil(reopen.hint)

        await store.perform(drop: payload("t-recurrent", .recurrent), on: .done)
        XCTAssertEqual(store.feedback?.message, BoardDropRejection.recurrentLocked.message)

        // "Already there" is not an error — it is a shrug.
        await store.perform(drop: payload("t-minimal", .todo), on: .todo)
        XCTAssertEqual(store.feedback?.isError, false)
    }

    // MARK: - Done goes through the sheet

    /// **The phase's central safety property.** A Done drop may send the
    /// write-free dry run and nothing else; the close waits for a confirm.
    func testDoneDropSendsOnlyTheDryRunAndOpensTheSheet() async throws {
        let transport = StubTransport()
        let plan = try planData()
        transport.respond { request in
            request.path.hasSuffix("/close/plan")
                ? .raw(plan)
                : .failure(code: "not_found", message: "unstubbed", status: 404)
        }
        let store = try makeStore(transport)

        await store.perform(drop: payload("t-multi-sprint", .inProgress), on: .done)

        XCTAssertEqual(transport.requestLines,
                       ["POST /v1/tasks/t-multi-sprint/close/plan"])
        XCTAssertNotNil(store.closeSheet)
        guard case .ready = try XCTUnwrap(store.closeSheet).phase else {
            return XCTFail("expected the sheet to be showing a plan")
        }
        // And the card has *not* moved to Done.
        XCTAssertTrue(store.pendingStatus.isEmpty)
        XCTAssertTrue(store.boardTasks(.inProgress).contains { $0.id == "t-multi-sprint" })
    }

    /// Even with a task the daemon would accept, the status endpoint must never
    /// be used for `done`: the daemon routes it into the close workflow.
    func testSettingDoneThroughTheStatusEndpointIsRefusedBeforeTheSocket() async throws {
        let transport = StubTransport()
        transport.respond { _ in
            XCTFail("setStatus(.done) must not reach the daemon")
            return .failure(code: "internal_error", message: "unreachable", status: 500)
        }
        let client = transport.makeClient()

        do {
            _ = try await client.setStatus(taskId: "t-multi-sprint", status: .done)
            XCTFail("expected a refusal")
        } catch let error as DaemonClientError {
            guard case .refusedLocally = error else {
                return XCTFail("expected .refusedLocally, got \(error)")
            }
        }
        XCTAssertTrue(transport.requests.isEmpty)
    }

    // MARK: - Optimistic moves

    func testOptimisticMoveAppliesImmediatelyAndSurvivesUntilTheSnapshotAgrees() async throws {
        let transport = StubTransport()
        let snapshot = try snapshotData()
        transport.respond { request in
            switch request.path {
            case "/v1/tasks/t-minimal/status":
                .json(["closed": false, "status": "inprogress", "old_status": "todo",
                       "project_synced": false])
            // The snapshot still says `todo` — the daemon's write has not been
            // observed yet. The overlay must hold, or the card flicks back.
            case "/v1/snapshot": .raw(snapshot)
            default: .failure(code: "not_found", message: "unstubbed", status: 404)
            }
        }
        let store = try makeStore(transport)
        XCTAssertTrue(store.boardTasks(.todo).contains { $0.id == "t-minimal" })

        await store.perform(drop: payload("t-minimal", .todo), on: .inProgress)

        let status = try XCTUnwrap(transport.requests.first {
            $0.path == "/v1/tasks/t-minimal/status"
        })
        XCTAssertEqual(status.method, "POST")
        XCTAssertEqual(status.body["status"], "inprogress")
        XCTAssertEqual(store.pendingStatus["t-minimal"]?.target, .inProgress)
        XCTAssertTrue(store.boardTasks(.inProgress).contains { $0.id == "t-minimal" })
        XCTAssertFalse(store.boardTasks(.todo).contains { $0.id == "t-minimal" })
    }

    /// The overlay is dropped once a snapshot confirms it, so a stale optimistic
    /// state cannot outlive the fact.
    func testTheOverlayClearsOnceTheSnapshotAgrees() async throws {
        let transport = StubTransport()
        let moved = try JSONSerialization.data(withJSONObject: {
            var object = try! JSONSerialization.jsonObject(with: try! snapshotData())
                as! [String: Any]
            var tasks = object["tasks"] as! [[String: Any]]
            for index in tasks.indices where tasks[index]["id"] as? String == "t-minimal" {
                tasks[index]["status"] = "inprogress"
            }
            object["tasks"] = tasks
            return object
        }())
        transport.respond { request in
            switch request.path {
            case "/v1/tasks/t-minimal/status":
                .json(["closed": false, "status": "inprogress", "project_synced": false])
            case "/v1/snapshot": .raw(moved)
            default: .failure(code: "not_found", message: "unstubbed", status: 404)
            }
        }
        let store = try makeStore(transport)

        await store.perform(drop: payload("t-minimal", .todo), on: .inProgress)

        XCTAssertTrue(store.pendingStatus.isEmpty,
                      "the snapshot confirmed the move, so the overlay is redundant")
        XCTAssertTrue(store.boardTasks(.inProgress).contains { $0.id == "t-minimal" })
    }

    /// The rollback. A daemon that refuses must leave the board exactly as it
    /// was, and say why.
    func testOptimisticMoveRollsBackWhenTheDaemonRefuses() async throws {
        let transport = StubTransport()
        transport.respond { request in
            request.path.hasSuffix("/status")
                ? .failure(code: "lock_timeout",
                           message: "another writer holds the data lock", status: 503)
                : .failure(code: "not_found", message: "unstubbed", status: 404)
        }
        let store = try makeStore(transport)

        await store.perform(drop: payload("t-minimal", .todo), on: .inProgress)

        XCTAssertEqual(transport.requestLines, ["POST /v1/tasks/t-minimal/status"],
                       "one attempt, no retry storm")
        XCTAssertTrue(store.pendingStatus.isEmpty, "the overlay must be rolled back")
        XCTAssertTrue(store.boardTasks(.todo).contains { $0.id == "t-minimal" },
                      "the card belongs where it started")
        XCTAssertFalse(store.boardTasks(.inProgress).contains { $0.id == "t-minimal" })
        let feedback = try XCTUnwrap(store.feedback)
        XCTAssertTrue(feedback.isError)
        XCTAssertTrue(feedback.message.contains("data lock"), feedback.message)
    }

    /// An unreachable daemon rolls back the same way a refusal does — the board
    /// must not keep showing a move that never happened.
    func testRollbackOnAnUnreachableDaemon() async throws {
        let store = Store(
            client: DaemonClient(configuration: .init(
                baseURL: URL(string: "http://127.0.0.1:1")!,
                tokenFileURL: FileManager.default.temporaryDirectory
                    .appendingPathComponent("wt-missing-token"),
                timeout: 1)),
            snapshot: try JSONDecoder().decode(Snapshot.self, from: try snapshotData()))

        await store.perform(drop: payload("t-minimal", .todo), on: .inProgress)

        XCTAssertTrue(store.pendingStatus.isEmpty)
        XCTAssertTrue(store.boardTasks(.todo).contains { $0.id == "t-minimal" })
        XCTAssertEqual(store.feedback?.isError, true)
    }

    // MARK: - Keyboard parity uses the same path

    /// `⌘←`/`⌘→` build a payload and call `perform(drop:on:)`, so the keyboard
    /// cannot acquire powers the mouse does not have.
    func testKeyboardColumnNeighbours() throws {
        let transport = StubTransport()
        transport.respond { _ in .failure(code: "not_found", message: "x", status: 404) }
        let store = try makeStore(transport)

        XCTAssertNil(store.neighbourColumn(of: .todo, offset: -1))
        XCTAssertEqual(store.neighbourColumn(of: .todo, offset: 1), .inProgress)
        XCTAssertEqual(store.neighbourColumn(of: .inProgress, offset: 1), .done)
        XCTAssertNil(store.neighbourColumn(of: .done, offset: 1))
        XCTAssertNil(store.neighbourColumn(of: .recurrent, offset: 1),
                     "the shelf is not part of the column strip")
    }

    // MARK: - Insertion indicator

    /// The indicator points at where the card will actually land under the
    /// column's own sort, not at the cursor — the board does not persist card
    /// order, so a cursor-following indicator would promise something the data
    /// model cannot keep.
    func testLandingIndexUsesTheColumnSort() throws {
        let transport = StubTransport()
        transport.respond { _ in .failure(code: "not_found", message: "x", status: 404) }
        let store = try makeStore(transport)

        // `t-no-logs` has never been logged, so it sorts to the bottom of
        // whichever column it lands in.
        let inProgress = store.boardTasks(.inProgress)
        XCTAssertEqual(store.landingIndex(of: "t-no-logs", movedTo: .inProgress),
                       inProgress.count)

        // A recently-logged card lands at the top of an empty-ish column.
        XCTAssertEqual(store.landingIndex(of: "t-multi-sprint", movedTo: .todo), 0)
    }
}
