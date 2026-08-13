import XCTest
@testable import WorkloadClient

/// Which task the menu bar acts on, and what `⌘T` does.
///
/// Two selections exist at once — a board card and a shelf row — and both panes
/// are on screen together, so "the selection" is undefined until something says
/// which one moved last. Getting that wrong is not cosmetic: `⌘T` on a stale
/// shelf row would start a timer on a perpetual task the user is not looking
/// at, and `⇧⌘D` would open the close preview for it.
@MainActor
final class MenuTargetTests: XCTestCase {

    private func snapshot(_ name: String) throws -> Snapshot {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: name, withExtension: "json",
                              subdirectory: "Fixtures"))
        return try JSONDecoder().decode(Snapshot.self, from: Data(contentsOf: url))
    }

    /// `snapshot.json` **has a running timer** (`t-running`), so anything about
    /// the idle case has to use the fixture that does not.
    private func makeStore(_ transport: StubTransport = StubTransport()) throws -> Store {
        Store(client: transport.makeClient(), snapshot: try snapshot("snapshot"))
    }

    private func makeIdleStore(_ transport: StubTransport = StubTransport()) throws -> Store {
        let idle = try snapshot("recurrent-shelf")
        XCTAssertNil(idle.activeTimer?.taskId, "the idle fixture grew a timer")
        return Store(client: transport.makeClient(), snapshot: idle)
    }

    // MARK: - Precedence

    func testNothingSelectedMeansNoMenuTask() throws {
        let store = try makeStore()
        XCTAssertNil(store.menuTask)
        XCTAssertTrue(store.menuActions.isEmpty)
    }

    func testTheBoardSelectionIsTheMenuTask() throws {
        let store = try makeStore()
        store.selectTask("t-multi-sprint")
        XCTAssertEqual(store.menuTask?.id, "t-multi-sprint")
        XCTAssertEqual(store.lastSelectionSurface, .board)
    }

    func testAShelfSelectionTakesOverWhenItMovesLast() throws {
        let store = try makeStore()
        store.selectTask("t-multi-sprint")
        store.shelfSelection = "t-recurrent"
        XCTAssertEqual(store.lastSelectionSurface, .shelf)
        XCTAssertEqual(store.menuTask?.id, "t-recurrent")
    }

    /// And back again — the stale shelf row must not keep the menu.
    func testTheBoardTakesItBack() throws {
        let store = try makeStore()
        store.shelfSelection = "t-recurrent"
        store.selectTask("t-running")
        XCTAssertEqual(store.lastSelectionSurface, .board)
        XCTAssertEqual(store.menuTask?.id, "t-running")
    }

    /// The other surface is the fallback, so a menu is never dead merely
    /// because the pane that moved last has an empty selection.
    func testTheOtherSurfaceIsTheFallback() throws {
        let store = try makeStore()
        store.shelfSelection = "t-recurrent"
        store.selectTask(nil)
        XCTAssertEqual(store.menuTask?.id, "t-recurrent")
    }

    /// A board selection pointing at a task the snapshot no longer has resolves
    /// to nothing rather than to some other card.
    func testAStaleSelectionResolvesToNothing() throws {
        let store = try makeStore()
        store.selectTask("t-does-not-exist")
        XCTAssertNil(store.menuTask)
    }

    // MARK: - The menu the target gets

    func testTheMenuMatchesTheTargetsKind() throws {
        let store = try makeStore()
        store.selectTask("t-multi-sprint")
        XCTAssertEqual(store.menuActions, TaskAction.boardMenu)
        store.shelfSelection = "t-recurrent"
        XCTAssertEqual(store.menuActions, TaskAction.recurrentMenu)
    }

    // MARK: - ⌘T

    func testToggleTimerStartsOnTheMenuTaskWhenIdle() async throws {
        let transport = StubTransport()
        transport.respond { request in
            switch request.path {
            case "/v1/timer/start":
                return .json(["started": true, "task_id": "b-open",
                              "title": "An open board task"])
            default:
                return .json(["tasks": [], "roles": []])
            }
        }
        let store = try makeIdleStore(transport)
        store.selectTask("b-open")
        XCTAssertTrue(store.canToggleTimer)
        await store.toggleTimer()
        XCTAssertTrue(transport.requests.contains { $0.line == "POST /v1/timer/start" },
                      transport.requests.map(\.line).description)
        XCTAssertFalse(transport.requests.contains { $0.line == "POST /v1/timer/stop" })
    }

    /// With a timer already running, `⌘T` **stops** it — and stops whatever is
    /// running, not whatever is selected. Starting a second timer on the
    /// selected card would silently displace the running one.
    func testToggleTimerStopsWhateverIsRunning() async throws {
        let transport = StubTransport()
        transport.respond { request in
            switch request.path {
            case "/v1/timer/stop":
                return .json(["stopped": true, "logged": true, "minutes": 12.0,
                              "title": "Ship the running task"])
            default:
                return .json(["tasks": [], "roles": []])
            }
        }
        let running = try snapshot("snapshot")
        try XCTSkipIf(running.activeTimer?.taskId == nil,
                      "the fixture has no running timer")
        let store = Store(client: transport.makeClient(), snapshot: running)
        store.selectTask("t-multi-sprint")   // *not* the running task
        await store.toggleTimer()
        XCTAssertTrue(transport.requests.contains { $0.line == "POST /v1/timer/stop" },
                      transport.requests.map(\.line).description)
        XCTAssertFalse(transport.requests.contains { $0.line == "POST /v1/timer/start" })
    }

    func testToggleTimerDoesNothingWithNoTargetAndNoTimer() async throws {
        let transport = StubTransport()
        let store = try makeIdleStore(transport)
        XCTAssertFalse(store.canToggleTimer)
        await store.toggleTimer()
        XCTAssertTrue(transport.requests.isEmpty,
                      transport.requests.map(\.line).description)
    }

    // MARK: - ⌘F

    func testFindBumpsTheFocusRequestEveryTime() throws {
        let store = try makeStore()
        let before = store.searchFocusRequests
        store.focusSearchField()
        store.focusSearchField()
        XCTAssertEqual(store.searchFocusRequests, before + 2,
                       "a repeated ⌘F must produce a fresh change to observe")
    }

    // MARK: - Undo support

    /// `applyFilter` is what the `UndoManager` calls back into. It has to
    /// install the value wholesale *and* suppress the current-sprint default —
    /// otherwise undoing back to an empty filter would be re-seeded on the next
    /// snapshot.
    func testApplyFilterInstallsAValueWholesale() throws {
        let store = try makeStore()
        let role = try XCTUnwrap(store.roles.first)
        store.applyFilter(FilterState(facet: .role, value: role.id))
        XCTAssertEqual(store.filter.roles, [role.id])
        store.applyFilter(FilterState())
        XCTAssertTrue(store.filter.isEmpty)
    }

    /// Only non-text changes are undoable — the search field owns its own undo
    /// stack, and registering per keystroke would bury the facet changes.
    func testTextOnlyChangesAreNotUndoWorthy() {
        let a = FilterState(roles: ["demokit"], text: "wid")
        let b = FilterState(roles: ["demokit"], text: "widget")
        XCTAssertEqual(a.withoutText, b.withoutText)

        let c = FilterState(roles: ["demos"], text: "wid")
        XCTAssertNotEqual(a.withoutText, c.withoutText)
    }
}
