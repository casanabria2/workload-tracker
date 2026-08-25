import XCTest
@testable import WorkloadClient

/// Manual card order: the sort, the id list a drag persists, and the requests
/// a placed drop actually issues.
///
/// `BoardDropRulesTests` proves what a placement *means*; this proves the two
/// things that meaning rests on and that no drag session can be automated to
/// check: that a column sorts the way the rule claims, and that the list sent
/// to `POST /v1/tasks/reorder` describes the column the user was looking at —
/// including the cards a filter was hiding from them at the time.
@MainActor
final class BoardOrderTests: XCTestCase {

    // MARK: - Building blocks

    /// A task with only the keys this suite cares about. Decoded rather than
    /// constructed because `TrackerTask` declares `init(from:)` and so has no
    /// memberwise initialiser — and going through the decoder means these
    /// fixtures exercise the same `position` decoding the daemon's payload does.
    private func task(_ id: String, status: String = "todo",
                      position: Int? = nil,
                      lastLoggedAt: Double? = nil,
                      createdAt: Double? = nil) throws -> TrackerTask {
        var object: [String: Any] = ["id": id, "title": id, "status": status]
        if let position { object["position"] = position }
        if let lastLoggedAt { object["last_logged_at"] = lastLoggedAt }
        if let createdAt { object["created_at"] = createdAt }
        return try JSONDecoder().decode(
            TrackerTask.self, from: JSONSerialization.data(withJSONObject: object))
    }

    private func snapshot(_ tasks: [TrackerTask]) throws -> Snapshot {
        // Round-trips through the wire shape so the tasks arrive exactly as a
        // daemon payload would deliver them.
        let objects = try tasks.map { task -> [String: Any] in
            var object: [String: Any] = ["id": task.id, "title": task.title,
                                         "status": task.status.rawValue]
            if let position = task.position { object["position"] = position }
            if let logged = task.lastLoggedAt { object["last_logged_at"] = logged }
            if let created = task.createdAt { object["created_at"] = created }
            _ = try JSONSerialization.data(withJSONObject: object)
            return object
        }
        let data = try JSONSerialization.data(withJSONObject: ["tasks": objects])
        return try JSONDecoder().decode(Snapshot.self, from: data)
    }

    private func store(_ tasks: [TrackerTask],
                       transport: StubTransport = StubTransport()) throws -> Store {
        Store(client: transport.makeClient(), snapshot: try snapshot(tasks))
    }

    private func sortedIDs(_ tasks: [TrackerTask], in store: Store) -> [String] {
        tasks.sorted { Store.boardOrder($0, $1, position: { store.effectivePosition(of: $0) }) }
            .map(\.id)
    }

    // MARK: - The sort

    /// The band rule, which is the whole design in one assertion: a task nobody
    /// has dragged sorts **above** the hand-ordered block, not below it.
    ///
    /// Below would be the obvious implementation (`position ?? .max`) and is the
    /// wrong one: it files every newly created task at the bottom of a long
    /// column, where the owner will not see it. Above keeps the pre-ordering
    /// behaviour — new work is visible — while the arranged cards stay arranged.
    func testUnpositionedTasksSortAboveTheArrangedBlock() throws {
        let tasks = [
            try task("arranged-first", position: 0, lastLoggedAt: 100),
            try task("arranged-second", position: 1, lastLoggedAt: 900),
            try task("new-and-idle", createdAt: 10),
            try task("new-and-busy", lastLoggedAt: 500),
        ]
        let store = try store(tasks)
        XCTAssertEqual(sortedIDs(tasks, in: store),
                       ["new-and-busy", "new-and-idle",
                        "arranged-first", "arranged-second"])
    }

    /// Inside the unpositioned band nothing changed: recency, then creation
    /// date. This is the rule the board shipped with and it is still the whole
    /// sort for a tracker whose owner never drags anything.
    func testTheUnpositionedBandKeepsTheOldRecencySort() throws {
        let tasks = [
            try task("older-log", lastLoggedAt: 100),
            try task("newer-log", lastLoggedAt: 900),
            try task("never-logged-old", createdAt: 10),
            try task("never-logged-new", createdAt: 90),
        ]
        let store = try store(tasks)
        XCTAssertEqual(sortedIDs(tasks, in: store),
                       ["newer-log", "older-log",
                        "never-logged-new", "never-logged-old"])
    }

    /// Manual order beats recency, which is the point of the feature: logging
    /// time against a card must not tear it out of the place the owner put it.
    func testLoggingTimeDoesNotReshuffleAnArrangedColumn() throws {
        let tasks = [
            try task("first", position: 0, lastLoggedAt: 1),
            try task("second", position: 1, lastLoggedAt: 999_999),
            try task("third", position: 2, lastLoggedAt: 500),
        ]
        let store = try store(tasks)
        XCTAssertEqual(sortedIDs(tasks, in: store), ["first", "second", "third"])
    }

    /// A reorder renumbers a whole column, so equal positions should not occur.
    /// They are still broken deterministically rather than left to `sorted`'s
    /// instability, because "should not occur" is not "cannot arrive".
    func testTiedPositionsFallBackToRecencyRatherThanToLuck() throws {
        let tasks = [
            try task("stale", position: 3, lastLoggedAt: 100),
            try task("fresh", position: 3, lastLoggedAt: 900),
        ]
        let store = try store(tasks)
        XCTAssertEqual(sortedIDs(tasks, in: store), ["fresh", "stale"])
    }

    /// A snapshot from an older daemon — or from before anything was dragged —
    /// carries no `position` at all. That must decode, not throw.
    func testAPayloadWithNoPositionKeyDecodesAsUnpositioned() throws {
        let decoded = try JSONDecoder().decode(
            TrackerTask.self,
            from: JSONSerialization.data(withJSONObject: ["id": "t", "title": "t"]))
        XCTAssertNil(decoded.position)
    }

    // MARK: - What a drag persists

    /// The list sent to the daemon is the **unfiltered** column, even though
    /// the user placed the card against what the filter left on screen.
    ///
    /// This is the assertion that would catch the tempting shortcut of sending
    /// the drawn rows: with a filter on, "row 2" of the board is not row 2 of
    /// the column, and persisting the drawn list would drop every hidden card
    /// out of the order.
    func testTheOrderSentIsTheWholeColumnNotTheVisibleRows() throws {
        let tasks = [
            try task("keep-a", position: 0),
            try task("hidden", position: 1),
            try task("keep-b", position: 2),
            try task("mover", position: 3),
        ]
        let store = try store(tasks)
        XCTAssertEqual(
            store.reorderedIDs(in: .todo, moving: "mover", to: .before(taskId: "keep-b")),
            ["keep-a", "hidden", "mover", "keep-b"],
            "the hidden card keeps its place in the persisted order")
    }

    /// `.end` is the only way to say "last", because dropping on the bottom
    /// card means *before* it.
    func testEndAppendsAndColumnAppendsToo() throws {
        let tasks = [try task("a", position: 0),
                     try task("b", position: 1),
                     try task("mover", position: 2)]
        let store = try store(tasks)
        XCTAssertEqual(store.reorderedIDs(in: .todo, moving: "mover", to: .end),
                       ["a", "b", "mover"])
        XCTAssertEqual(store.reorderedIDs(in: .todo, moving: "a", to: .end),
                       ["b", "mover", "a"])
    }

    /// Dropping a card on itself is a no-op, not a card that vanishes or
    /// doubles. The remove-then-insert order is what makes this fall out.
    func testDroppingACardOnItselfChangesNothing() throws {
        let tasks = [try task("a", position: 0),
                     try task("b", position: 1),
                     try task("c", position: 2)]
        let store = try store(tasks)
        XCTAssertEqual(store.reorderedIDs(in: .todo, moving: "b", to: .before(taskId: "b")),
                       ["a", "b", "c"])
    }

    /// A snapshot landing mid-drag can retire the card the drop was aimed at.
    /// The move still has to happen — appending is a worse placement, losing
    /// the card is a bug.
    func testAnAnchorThatNoLongerExistsAppendsRatherThanDroppingTheCard() throws {
        let tasks = [try task("a", position: 0), try task("mover", position: 1)]
        let store = try store(tasks)
        XCTAssertEqual(
            store.reorderedIDs(in: .todo, moving: "mover", to: .before(taskId: "gone")),
            ["a", "mover"])
    }

    /// A cross-column drop is placed against the **destination**, and the card
    /// is already there optimistically by the time this is computed.
    func testACardArrivingFromAnotherColumnIsPlacedWithoutBeingDuplicated() throws {
        let tasks = [
            try task("dest-a", status: "inprogress", position: 0),
            try task("dest-b", status: "inprogress", position: 1),
            try task("mover", status: "todo", position: 0),
        ]
        let store = try store(tasks)
        XCTAssertEqual(
            store.reorderedIDs(in: .inProgress, moving: "mover",
                               to: .before(taskId: "dest-b")),
            ["dest-a", "mover", "dest-b"])
        XCTAssertEqual(
            store.reorderedIDs(in: .inProgress, moving: "mover", to: .end),
            ["dest-a", "dest-b", "mover"])
    }

    // MARK: - The requests a drop issues

    /// A same-column placed drop writes **one** request, and it is the reorder.
    /// No status is sent: the card did not change column, and a redundant
    /// status write here would be a `gh project` round trip for nothing.
    func testAReorderIssuesOnlyTheReorderRequest() async throws {
        let transport = StubTransport()
        transport.respond { request in
            .json(["task_ids": ["b", "a"], "positions": ["b": 0, "a": 1],
                   "count": 2], status: request.path.hasSuffix("/reorder") ? 200 : 500)
        }
        let store = try store([try task("a", position: 0), try task("b", position: 1)],
                              transport: transport)

        await store.perform(drop: TaskDragPayload(taskId: "b", sourceStatus: .todo),
                            on: .todo, at: .before(taskId: "a"))

        XCTAssertEqual(transport.requests.map(\.line).filter { $0.contains("/v1/tasks") },
                       ["POST /v1/tasks/reorder"])
        let body = try XCTUnwrap(transport.requests.first?.body["task_ids"])
        XCTAssertTrue(body.contains("b") && body.contains("a"), body)
    }

    /// A same-column drop on the column *background* still issues nothing.
    ///
    /// This is the conservative half of the design: manual ordering engages
    /// only when the user points at a place. A drag that lands on empty space
    /// must not quietly freeze the column's order.
    func testASameColumnBackgroundDropIssuesNothing() async throws {
        let transport = StubTransport()
        transport.respond { _ in
            XCTFail("a background drop in the same column must issue nothing")
            return .failure(code: "internal_error", message: "unreachable", status: 500)
        }
        let store = try store([try task("a", position: 0)], transport: transport)

        await store.perform(drop: TaskDragPayload(taskId: "a", sourceStatus: .todo),
                            on: .todo, at: .column)

        XCTAssertTrue(transport.requests.isEmpty)
    }

    /// A cross-column placed drop is status **then** order, in that sequence.
    ///
    /// The sequence is not cosmetic: `wt_api.set_status` clears `position`, so
    /// a reorder sent first would be erased by the status write that followed
    /// it, and the card would land at the top of the column instead of where it
    /// was dropped.
    func testACrossColumnPlacedDropWritesStatusBeforeOrder() async throws {
        let transport = StubTransport()
        transport.respond { request in
            if request.path.hasSuffix("/reorder") {
                return .json(["task_ids": ["x"], "positions": ["x": 0], "count": 1])
            }
            return .json(["closed": false, "status": "inprogress",
                          "old_status": "todo", "project_synced": false])
        }
        let store = try store([try task("mover", status: "todo"),
                               try task("dest", status: "inprogress", position: 0)],
                              transport: transport)

        await store.perform(drop: TaskDragPayload(taskId: "mover", sourceStatus: .todo),
                            on: .inProgress, at: .before(taskId: "dest"))

        XCTAssertEqual(transport.requests.map(\.line).filter { $0.hasPrefix("POST") },
                       ["POST /v1/tasks/mover/status", "POST /v1/tasks/reorder"])
    }

    /// A snapshot landing mid-drag can move the card into the destination
    /// column before the drop is handled. The placement must still be sent.
    ///
    /// `moveTask` answers "is it in that column now", not "did I write
    /// something" — precisely so this case reads as success. Treating the no-op
    /// as a failure would drop the placement on the floor and leave the card
    /// wherever the sort put it, with nothing said.
    func testACardAlreadyInTheDestinationStillGetsItsPlacement() async throws {
        let transport = StubTransport()
        transport.respond { request in
            request.path.hasSuffix("/reorder")
                ? .json(["task_ids": ["mover"], "positions": ["mover": 0], "count": 1])
                : .failure(code: "not_found", message: "no stub", status: 404)
        }
        // The payload says the card was picked up in To Do; the snapshot the
        // store holds already has it in In Progress.
        let store = try store([try task("mover", status: "inprogress"),
                               try task("dest", status: "inprogress", position: 0)],
                              transport: transport)

        await store.perform(drop: TaskDragPayload(taskId: "mover", sourceStatus: .todo),
                            on: .inProgress, at: .before(taskId: "dest"))

        XCTAssertEqual(transport.requests.map(\.line).filter { $0.hasPrefix("POST") },
                       ["POST /v1/tasks/reorder"],
                       "no status write is needed, but the placement still is")
    }

    /// A failed status change must not be followed by a reorder.
    ///
    /// Persisting an order for a column the card never reached would leave the
    /// board's stored order describing a arrangement that never existed, and it
    /// would do so silently — the status failure already told the user the move
    /// did not happen.
    func testAFailedStatusChangeSuppressesTheReorder() async throws {
        let transport = StubTransport()
        transport.respond { request in
            if request.path.hasSuffix("/reorder") {
                XCTFail("a reorder must not follow a refused status change")
            }
            return .failure(code: "invalid_status", message: "nope", status: 400)
        }
        let store = try store([try task("mover", status: "todo"),
                               try task("dest", status: "inprogress", position: 0)],
                              transport: transport)

        await store.perform(drop: TaskDragPayload(taskId: "mover", sourceStatus: .todo),
                            on: .inProgress, at: .before(taskId: "dest"))

        XCTAssertEqual(transport.requests.map(\.line), ["POST /v1/tasks/mover/status"])
        XCTAssertTrue(store.pendingOrder.isEmpty)
    }

    /// A refused reorder rolls the column back rather than keeping a placement
    /// the daemon never accepted.
    func testARefusedReorderRollsBackAndSaysSo() async throws {
        let transport = StubTransport()
        transport.respond { _ in
            .failure(code: "task_not_found", message: "No task found matching 'b'",
                     status: 404)
        }
        let store = try store([try task("a", position: 0), try task("b", position: 1)],
                              transport: transport)

        await store.perform(drop: TaskDragPayload(taskId: "b", sourceStatus: .todo),
                            on: .todo, at: .before(taskId: "a"))

        XCTAssertTrue(store.pendingOrder.isEmpty,
                      "the optimistic order must not survive a refusal")
        XCTAssertEqual(store.boardTasks(.todo).map(\.id), ["a", "b"])
        XCTAssertEqual(store.feedback?.isError, true)
    }

    /// While the write is in flight the column shows the new order, which is
    /// what makes a drop feel like it landed.
    func testTheColumnShowsTheNewOrderBeforeTheSnapshotConfirmsIt() async throws {
        let transport = StubTransport()
        transport.respond { request in
            request.path.hasSuffix("/reorder")
                ? .json(["task_ids": ["c", "a", "b"],
                         "positions": ["c": 0, "a": 1, "b": 2], "count": 3])
                : .failure(code: "not_found", message: "no snapshot stub", status: 404)
        }
        let store = try store([try task("a", position: 0),
                               try task("b", position: 1),
                               try task("c", position: 2)],
                              transport: transport)

        await store.perform(drop: TaskDragPayload(taskId: "c", sourceStatus: .todo),
                            on: .todo, at: .before(taskId: "a"))

        // The refresh that follows the write has no snapshot stub and fails, so
        // what is on screen here is purely the optimistic overlay — which is
        // exactly the state this asserts.
        XCTAssertEqual(store.boardTasks(.todo).map(\.id), ["c", "a", "b"])
        XCTAssertEqual(store.effectivePosition(of: try XCTUnwrap(
            store.boardTasks(.todo).first)), 0)
    }
}
