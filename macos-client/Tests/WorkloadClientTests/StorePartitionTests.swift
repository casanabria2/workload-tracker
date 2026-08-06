import XCTest
@testable import WorkloadClient

/// The board partition: which task lands in which column, what the shelf holds,
/// and what the sidebar's Roles section shows. Driven off the synthetic fixture
/// through `Store(previewSnapshot:)`, so no daemon is involved.
@MainActor
final class StorePartitionTests: XCTestCase {

    private func makeStore() throws -> Store {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: "snapshot", withExtension: "json",
                              subdirectory: "Fixtures"))
        let snapshot = try JSONDecoder().decode(Snapshot.self, from: Data(contentsOf: url))
        // Pinned `now` so the elapsed-timer assertion is not clock-dependent.
        return Store(previewSnapshot: snapshot,
                     now: Date(timeIntervalSince1970: 1786020000.0 + 90))
    }

    func testRecurrentTasksAreExcludedFromTheBoardColumns() throws {
        let store = try makeStore()
        let columnIDs = TaskStatus.boardColumns.flatMap { store.boardTasks($0).map(\.id) }
        XCTAssertFalse(columnIDs.contains("t-recurrent"))
        XCTAssertEqual(store.recurrentTasks.map(\.id), ["t-recurrent"])
    }

    func testColumnMembership() throws {
        let store = try makeStore()
        XCTAssertEqual(Set(store.boardTasks(.todo).map(\.id)), ["t-no-logs", "t-minimal"])
        XCTAssertEqual(Set(store.boardTasks(.inProgress).map(\.id)),
                       ["t-multi-sprint", "t-running"])
        XCTAssertEqual(store.boardTasks(.done).map(\.id), ["t-done-nulls"])
    }

    /// A status this client does not know must not silently vanish from every
    /// surface — it is absent from the columns but reachable.
    func testUnknownStatusIsNotDroppedSilently() throws {
        let store = try makeStore()
        XCTAssertEqual(store.unclassifiedTasks.map(\.id), ["t-future-status"])
    }

    /// Most-recently-logged first; never-logged tasks sort last by creation
    /// date, so a freshly created To Do card stays visible.
    func testColumnSortPutsNeverLoggedTasksLast() throws {
        let store = try makeStore()
        XCTAssertEqual(store.boardTasks(.todo).map(\.id), ["t-no-logs", "t-minimal"])
        XCTAssertEqual(store.boardTasks(.inProgress).map(\.id),
                       ["t-running", "t-multi-sprint"])
    }

    /// Roles with no tasks are kept — the sidebar is a directory of what exists.
    func testRoleSummariesIncludeEmptyRoles() throws {
        let store = try makeStore()
        XCTAssertEqual(store.roleSummaries.count, 7)
        let empty = try XCTUnwrap(store.roleSummaries.first { $0.role.id == "role-empty" })
        XCTAssertEqual(empty.taskCount, 0)
        XCTAssertEqual(empty.loggedMins, 0)

        let demokit = try XCTUnwrap(store.roleSummaries.first { $0.role.id == "demokit" })
        XCTAssertEqual(demokit.taskCount, 1)
        XCTAssertEqual(demokit.loggedMins, 690)
        XCTAssertEqual(Duration.format(minutes: demokit.loggedMins), "11h 30m")
    }

    func testActiveTimerResolvesToItsTask() throws {
        let store = try makeStore()
        XCTAssertEqual(store.activeTimerTask?.id, "t-running")
        XCTAssertEqual(store.activeTimerElapsed ?? 0, 90, accuracy: 0.001)
    }

    /// Current-sprint minutes come from the timestamp-bucketed
    /// `sprints_with_time`, never from the legacy `sprint`/`sprint_id` fields.
    func testCurrentSprintTotalIsDerivedFromLoggedTime() throws {
        let store = try makeStore()
        XCTAssertEqual(store.currentSprint?.id, "sp-102")
        // 105 (multi-sprint) + 60 (recurrent) + 45 (running) = 210
        XCTAssertEqual(store.currentSprintMinutes, 210)
    }

    /// A readable data file plus zero tasks is an empty tracker. An *unreadable*
    /// one is the Full-Disk-Access state — a different thing entirely.
    func testDataFileReadabilityIsSeparateFromEmptiness() throws {
        let store = try makeStore()
        XCTAssertTrue(store.dataFileReadable)

        let json = Data("""
        {"tasks": [], "data_file": {"readable": false, "reason": "permission_denied"}}
        """.utf8)
        let broken = Store(previewSnapshot:
                            try JSONDecoder().decode(Snapshot.self, from: json))
        XCTAssertTrue(broken.tasks.isEmpty)
        XCTAssertFalse(broken.dataFileReadable)
    }

    /// The state the whole `ConnectionState` enum exists for: a down daemon is
    /// never the same as an empty board.
    func testUnreachableIsNotAnEmptyBoard() {
        let unreachable = Store.ConnectionState.unreachable(reason: "refused")
        XCTAssertTrue(unreachable.isUnreachable)
        XCTAssertFalse(Store.ConnectionState.live.isUnreachable)
        XCTAssertFalse(Store.ConnectionState.degraded(reason: "x").isUnreachable)
    }

    /// End to end: a `Store` pointed at a port with nothing on it must land on
    /// `.unreachable` with **no** snapshot, which is exactly what `RootView`
    /// routes to `UnreachableView` on. If this ever came back `.live` with an
    /// empty snapshot, a down daemon would render as a board with no cards —
    /// the failure this whole state machine exists to prevent.
    func testDeadDaemonDrivesTheUnreachableStateNotAnEmptyBoard() async {
        let client = DaemonClient(configuration: .init(
            baseURL: URL(string: "http://127.0.0.1:59998")!,
            tokenFileURL: AppSettings.defaultTokenFileURL,
            timeout: 3))
        let store = Store(client: client)
        await store.refresh()

        XCTAssertNil(store.snapshot, "no snapshot should have been fetched")
        XCTAssertTrue(store.tasks.isEmpty)
        guard case .unreachable(let reason) = store.connection else {
            return XCTFail("expected .unreachable, got \(store.connection)")
        }
        XCTAssertFalse(reason.isEmpty)
    }
}
