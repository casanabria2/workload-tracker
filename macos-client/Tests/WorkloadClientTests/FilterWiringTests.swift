import XCTest
@testable import WorkloadClient

/// The **wiring**, not the rules.
///
/// Phase 4 shipped a drag-and-drop feature that did nothing while 95 tests
/// stayed green, because every one of them exercised a pure function and none
/// of them touched the path the app actually takes. So these tests deliberately
/// go through the objects the views hold: `Store.filteredBoardTasks` is the
/// accessor `BoardColumn` renders from, `BoardView.cursor(for:)` is the literal
/// call the key handler makes, `Store.toggle` is what both the sidebar row and
/// the toolbar menu invoke, and `FilterStateCodec` is what `@SceneStorage`
/// stores. Nothing here reimplements any of them.
@MainActor
final class FilterWiringTests: XCTestCase {

    private var snapshot: Snapshot!

    override func setUpWithError() throws {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: "facets", withExtension: "json",
                              subdirectory: "Fixtures"))
        snapshot = try JSONDecoder().decode(Snapshot.self, from: Data(contentsOf: url))
    }

    private func makeStore() -> Store { Store(previewSnapshot: snapshot) }

    private var currentSprint: String { snapshot.currentSprint!.id }

    // MARK: - The default

    /// §8.2: the Sprint facet defaults to the current sprint, which is what
    /// makes the 37-card Done column tractable without a separate scope control.
    func testTheFirstSnapshotSeedsTheCurrentSprint() {
        let store = makeStore()
        XCTAssertEqual(store.filter.sprints, [currentSprint])
        XCTAssertEqual(store.filteredTasks.count, 24)
        XCTAssertEqual(store.tasks.count, 55)
    }

    /// A filter restored from `@SceneStorage` wins, and the seed does not
    /// re-apply — otherwise a deliberately cleared Sprint facet would come back
    /// on the next refresh.
    func testARestoredFilterCancelsTheDefaultSeed() {
        let store = Store()
        store.restoreFilter(FilterState(roles: ["role-a"]))
        XCTAssertTrue(store.filter.sprints.isEmpty)
    }

    func testClearingIsNotUndoneByTheNextSnapshot() async throws {
        let transport = StubTransport()
        let data = try JSONEncoder().encode(snapshot)
        transport.respond { request in
            request.path == "/v1/snapshot" ? .raw(data)
                : .json(["ok": true])
        }
        let store = Store(client: transport.makeClient(), snapshot: nil)
        await store.refresh()
        XCTAssertEqual(store.filter.sprints, [currentSprint], "seeded on first snapshot")

        store.clearFilters()
        await store.refresh()
        XCTAssertTrue(store.filter.sprints.isEmpty, "a refresh must not re-seed")
        XCTAssertEqual(store.filteredTasks.count, 55)
    }

    // MARK: - One state, three surfaces

    /// Plan §8.4's "one state, two views of it". The sidebar row calls
    /// `store.toggle(id, in: .role)`; so does the toolbar menu's `Toggle`; so
    /// does removing a token. There is no second setter to drift from.
    func testTheSidebarAndTheMenuWriteTheSameState() {
        let store = makeStore()
        store.clearFilters()

        // What the sidebar row does.
        store.toggle("role-a", in: .role)
        XCTAssertEqual(store.filter.roles, ["role-a"])
        XCTAssertTrue(store.isSelected("role-a", in: .role))
        XCTAssertEqual(store.filteredTasks.count, 22)

        // What the menu's toggle does, on a second value.
        store.toggle("role-b", in: .role)
        XCTAssertEqual(store.filter.roles, ["role-a", "role-b"])
        XCTAssertEqual(store.filteredTasks.count, 29)

        // And the sidebar's checkmark reads the same state back.
        XCTAssertTrue(store.isSelected("role-b", in: .role))
        XCTAssertFalse(store.isSelected("role-c", in: .role))
    }

    /// Removing a token in the search field is the third surface, and it has to
    /// uncheck the sidebar row.
    func testRemovingATokenUnchecksTheRole() {
        let store = makeStore()
        store.clearFilters()
        store.toggle("role-a", in: .role)
        store.toggle("role-b", in: .role)

        var tokens = store.filterTokens
        XCTAssertEqual(tokens.count, 2)
        tokens.removeAll { $0.value == "role-a" }
        store.applyTokens(tokens)

        XCTAssertFalse(store.isSelected("role-a", in: .role))
        XCTAssertTrue(store.isSelected("role-b", in: .role))
    }

    /// Tokens are a lossless projection of the facet state, across all four
    /// facets at once.
    func testTokensRoundTripEveryFacet() {
        let store = makeStore()
        store.filter = FilterState(roles: ["role-a"],
                                   activityTypes: [FilterState.unset],
                                   repos: ["example-org/repo-b"],
                                   sprints: [currentSprint],
                                   text: "keep me")
        let before = store.filter
        store.applyTokens(store.filterTokens)
        XCTAssertEqual(store.filter, before, "the projection lost or invented a value")

        // And they render as something a human can read. `display` is the
        // accessibility label; the on-screen chip is the bare `label`, because
        // the toolbar's search field cannot fit two qualified ones.
        let labels = store.filterTokens.map(\.display)
        XCTAssertTrue(labels.contains("Activity Type: No Activity"), "\(labels)")
        XCTAssertTrue(labels.contains("Role: Widget Kit"), "\(labels)")
        XCTAssertTrue(labels.contains("Sprint 105"), "\(labels)")
        XCTAssertFalse(labels.contains("Sprint: Sprint 105"),
                       "a label that already names its facet must not stutter")
        for label in labels {
            XCTAssertFalse(label.unicodeScalars.contains { $0.value < 0x20 })
        }
        XCTAssertFalse(store.filterTokens.contains { $0.label.isEmpty })
    }

    /// Free text is not a token and must survive a token edit untouched.
    func testTokenEditsLeaveTheSearchTextAlone() {
        let store = makeStore()
        store.filter = FilterState(roles: ["role-a"], text: "widget")
        store.applyTokens([])
        XCTAssertTrue(store.filter.roles.isEmpty)
        XCTAssertEqual(store.filter.text, "widget")
    }

    /// Two facets can offer the same string; their tokens must stay distinct.
    func testTokenIDsAreFacetQualified() {
        let a = FilterToken(facet: .role, value: "x", label: "X")
        let b = FilterToken(facet: .repository, value: "x", label: "X")
        XCTAssertNotEqual(a.id, b.id)
    }

    // MARK: - The board renders and navigates the same array

    /// The columns and the keyboard cursor are built from one accessor, so they
    /// cannot disagree about what is on screen.
    func testTheCursorIsBuiltFromTheRenderedColumns() {
        let store = makeStore()
        store.filter = FilterState(roles: ["role-a"])
        let cursor = BoardView.cursor(for: store)
        XCTAssertEqual(cursor.columns,
                       TaskStatus.boardColumns.map { store.filteredBoardTasks($0).map(\.id) })
        XCTAssertEqual(cursor.reachableIDs,
                       Set(store.filteredTasks.filter { $0.status != .recurrent }.map(\.id)))
    }

    /// **The property the brief calls for.** A card the filter hides must not be
    /// reachable by any keyboard move.
    func testAFilteredOutCardIsUnreachableByKeyboard() {
        let store = makeStore()
        store.clearFilters()
        let everything = BoardView.cursor(for: store)
        let hidden = try! XCTUnwrap(
            store.boardTasks(.done).first { $0.roleId != "role-a" }?.id)
        XCTAssertTrue(everything.contains(hidden))

        store.filter = FilterState(roles: ["role-a"])
        let cursor = BoardView.cursor(for: store)
        XCTAssertFalse(cursor.contains(hidden))

        // Walk the whole board with both axes; the hidden card never appears.
        var seen: Set<String> = []
        var id = cursor.firstID
        for _ in 0..<400 {
            guard let current = id else { break }
            seen.insert(current)
            id = cursor.move(from: current, byRow: 1)
            if id == current { id = cursor.move(from: current, byColumn: 1) }
            if id == current { break }
        }
        XCTAssertFalse(seen.contains(hidden))
        XCTAssertTrue(seen.isSubset(of: cursor.reachableIDs))
    }

    /// A filter applied while a card is selected must drop the selection, or
    /// `⌘→` would move an invisible card. `BoardView` calls this on
    /// `.onChange(of: store.filter)`.
    func testRevalidationDropsASelectionTheFilterHid() {
        let store = makeStore()
        store.clearFilters()
        let victim = try! XCTUnwrap(
            store.boardTasks(.done).first { $0.roleId != "role-a" }?.id)

        store.filter = FilterState(roles: ["role-a"])
        let cursor = BoardView.cursor(for: store)
        let revalidated = cursor.revalidated(victim)
        XCTAssertNotEqual(revalidated, victim)
        XCTAssertEqual(revalidated, cursor.firstID)
        XCTAssertTrue(cursor.contains(try! XCTUnwrap(revalidated)))
    }

    func testRevalidationKeepsAStillVisibleSelection() {
        let store = makeStore()
        store.filter = FilterState(roles: ["role-a"])
        let cursor = BoardView.cursor(for: store)
        let survivor = try! XCTUnwrap(cursor.firstID)
        XCTAssertEqual(cursor.revalidated(survivor), survivor)
    }

    func testAnEmptyBoardHasNoSelection() {
        let store = makeStore()
        store.filter = FilterState(repos: ["example-org/nope"])
        let cursor = BoardView.cursor(for: store)
        XCTAssertTrue(cursor.isEmpty)
        XCTAssertNil(cursor.revalidated("t-01"))
        XCTAssertNil(cursor.move(from: nil, byColumn: 1))
    }

    /// Cursor mechanics that the old inline implementation had and must keep:
    /// empty columns are skipped, the ends hold, rows clamp.
    func testCursorMovementMechanics() {
        let cursor = BoardCursor(columns: [["a", "b"], [], ["c"]])
        XCTAssertEqual(cursor.firstID, "a")
        XCTAssertEqual(cursor.move(from: "a", byRow: 1), "b")
        XCTAssertEqual(cursor.move(from: "b", byRow: 1), "b", "clamped at the bottom")
        XCTAssertEqual(cursor.move(from: "a", byRow: -1), "a", "clamped at the top")
        XCTAssertEqual(cursor.move(from: "b", byColumn: 1), "c", "skips the empty column")
        XCTAssertEqual(cursor.move(from: "c", byColumn: 1), "c", "the last column holds")
        XCTAssertEqual(cursor.move(from: "c", byColumn: -1), "a")
        XCTAssertEqual(cursor.move(from: "zzz", byRow: 1), "a", "an unknown id resets")
    }

    // MARK: - Columns, counts and the empty state

    /// The header shows filtered **and** total, so a filter can never hide work
    /// silently (plan risk #6). The two numbers come from two accessors, and
    /// this pins their relationship.
    func testColumnsCarryBothCounts() {
        let store = makeStore()  // default: the current sprint
        XCTAssertEqual(store.filteredBoardTasks(.todo).count, 5)
        XCTAssertEqual(store.boardTasks(.todo).count, 5)
        XCTAssertEqual(store.filteredBoardTasks(.inProgress).count, 6)
        XCTAssertEqual(store.boardTasks(.inProgress).count, 6)
        XCTAssertEqual(store.filteredBoardTasks(.done).count, 6)
        XCTAssertEqual(store.boardTasks(.done).count, 37)
        XCTAssertEqual(store.filteredRecurrentTasks.count, 7)
        XCTAssertEqual(store.recurrentTasks.count, 7)
    }

    /// §7: the Done column obeys the Sprint filter instead of owning a scope
    /// control — 37 done tasks, 6 of them worked in the current sprint.
    func testTheDoneColumnObeysTheSprintFilter() throws {
        let store = makeStore()
        XCTAssertEqual(store.filteredBoardTasks(.done).count, 6)

        let past = try XCTUnwrap(snapshot.sprints.first { $0.title == "Sprint 97" }?.id)
        store.filter = FilterState(sprints: [past])
        let done = store.filteredBoardTasks(.done)
        XCTAssertFalse(done.isEmpty)
        for task in done {
            XCTAssertTrue(TaskFilter.sprintIDs(of: task).contains(past))
        }
    }

    /// The drop indicator counts against the drawn column, not the unfiltered
    /// one — otherwise it would point at a row that is not on screen.
    func testTheLandingIndexCountsTheFilteredColumn() {
        let store = makeStore()
        store.clearFilters()
        let unfiltered = store.landingIndex(of: "t-55", movedTo: .done)
        store.filter = FilterState(roles: ["role-g"])
        let filtered = store.landingIndex(of: "t-55", movedTo: .done)
        XCTAssertLessThan(filtered, unfiltered)
        XCTAssertLessThanOrEqual(filtered, store.filteredBoardTasks(.done).count)
    }

    func testTheEmptyStateNamesTheFacetsAtFault() throws {
        let store = makeStore()
        let s95 = try XCTUnwrap(snapshot.sprints.first { $0.title == "Sprint 95" }?.id)
        store.filter = FilterState(roles: ["role-g"], sprints: [s95])
        XCTAssertTrue(store.filteredTasks.isEmpty)
        XCTAssertEqual(Set(store.blockingFacets), [.role, .sprint])
        XCTAssertFalse(store.textIsBlocking)
    }

    // MARK: - Persistence

    func testTheSceneStorageCodecRoundTrips() throws {
        let state = FilterState(roles: ["role-a", "role-b"],
                                activityTypes: [FilterState.unset],
                                repos: ["example-org/repo-b"],
                                sprints: [currentSprint],
                                text: "widget")
        let encoded = FilterStateCodec.encode(state)
        XCTAssertEqual(FilterStateCodec.decode(encoded), state)
    }

    /// "The user cleared everything" and "nothing has ever been stored" must
    /// stay distinguishable, or clearing the Sprint facet is undone on relaunch.
    func testAnEmptyFilterEncodesToSomethingRatherThanNothing() {
        let encoded = FilterStateCodec.encode(FilterState())
        XCTAssertFalse(encoded.isEmpty)
        XCTAssertEqual(FilterStateCodec.decode(encoded), FilterState())
        XCTAssertNil(FilterStateCodec.decode(""), "nothing stored")
        XCTAssertNil(FilterStateCodec.decode("not json"))
    }

    /// A payload written by a build with fewer keys must still decode; the
    /// snapshot models take the same care and for the same reason.
    func testAPartialPayloadDecodes() throws {
        let state = try XCTUnwrap(FilterStateCodec.decode(#"{"roles":["role-a"]}"#))
        XCTAssertEqual(state.roles, ["role-a"])
        XCTAssertTrue(state.sprints.isEmpty)
        XCTAssertEqual(state.text, "")
    }

    // MARK: - Phase 4 still holds

    /// Filtering is read-only. A filter must not make a refused drop reach the
    /// daemon, and must not gain a card powers it did not have.
    func testFilteringDoesNotWeakenTheDropRules() async throws {
        let transport = StubTransport()
        transport.respond { _ in
            XCTFail("a rejected drop must issue nothing, filtered or not")
            return .failure(code: "internal_error", message: "unreachable", status: 500)
        }
        let store = Store(client: transport.makeClient(), snapshot: snapshot)
        store.filter = FilterState(roles: ["role-a"])

        let recurrent = try XCTUnwrap(store.recurrentTasks.first)
        await store.perform(drop: TaskDragPayload(taskId: recurrent.id,
                                                  sourceStatus: .recurrent), on: .inProgress)
        let done = try XCTUnwrap(store.boardTasks(.done).first)
        await store.perform(drop: TaskDragPayload(taskId: done.id, sourceStatus: .done),
                            on: .todo)

        XCTAssertTrue(transport.requests.isEmpty, "issued \(transport.requestLines)")
        XCTAssertNil(store.closeSheet)
        XCTAssertTrue(store.pendingStatus.isEmpty)
    }

    /// The optimistic overlay and the filter compose: a card moved to In
    /// Progress shows there immediately **and** still obeys the filter.
    func testTheOptimisticOverlayComposesWithTheFilter() async throws {
        let transport = StubTransport()
        let data = try JSONEncoder().encode(snapshot)
        transport.respond { request in
            request.path.hasSuffix("/status")
                ? .json(["closed": false, "status": "inprogress", "project_synced": false])
                : .raw(data)
        }
        let store = Store(client: transport.makeClient(), snapshot: snapshot)
        // A To Do card that the current-sprint default admits.
        store.filter = FilterState(sprints: [currentSprint])
        let card = try XCTUnwrap(store.filteredBoardTasks(.todo).first)

        await store.perform(drop: TaskDragPayload(taskId: card.id, sourceStatus: .todo),
                            on: .inProgress)

        XCTAssertEqual(store.pendingStatus[card.id]?.target, .inProgress)
        XCTAssertTrue(store.filteredBoardTasks(.inProgress).contains { $0.id == card.id },
                      "the filter must not swallow an optimistically moved card")
        XCTAssertFalse(store.filteredBoardTasks(.todo).contains { $0.id == card.id })

        // …and it is still hidden by a filter that excludes it.
        store.filter = FilterState(roles: ["\u{1}nothing"])
        XCTAssertFalse(store.filteredBoardTasks(.inProgress).contains { $0.id == card.id })
    }
}
