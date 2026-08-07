import XCTest
@testable import WorkloadClient

/// Plan §8.3 — facets derived from the snapshot, ordered usefully, and hidden
/// when they cannot subtract anything.
final class FacetCatalogTests: XCTestCase {

    private var snapshot: Snapshot!
    private var catalog: FacetCatalog!

    override func setUpWithError() throws {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: "facets", withExtension: "json",
                              subdirectory: "Fixtures"))
        snapshot = try JSONDecoder().decode(Snapshot.self, from: Data(contentsOf: url))
        catalog = FacetCatalog.build(from: snapshot)
    }

    // MARK: - What is offered

    /// The headline of §8.3: the values **in use**, not the option cache.
    func testActivityOffersTheValuesInUseNotTheThirtySevenInTheCache() {
        let options = catalog.options(for: .activityType)
        XCTAssertEqual(options.count, 9, "8 activities in use plus \"No Activity\"")
        XCTAssertGreaterThan(snapshot.projectOptions.activity.count, 30,
                             "the editor's cache is much larger, and is not this")
        let offered = Set(options.map(\.value)).subtracting([FilterState.unset])
        let inUse = Set(snapshot.tasks.compactMap(\.activity).filter { !$0.isEmpty })
        XCTAssertEqual(offered, inUse)
    }

    func testSprintOffersTheElevenWithLoggedTimeNotAllSeventyTwo() {
        let options = catalog.options(for: .sprint)
        XCTAssertEqual(options.count, 11)
        XCTAssertEqual(snapshot.sprints.count, 72)
        XCTAssertEqual(options.map(\.label),
                       ["Sprint 105", "Sprint 104", "Sprint 103", "Sprint 102",
                        "Sprint 101", "Sprint 100", "Sprint 99", "Sprint 98",
                        "Sprint 97", "Sprint 96", "Sprint 95"],
                       "newest first")
    }

    func testRoleOffersTheNineInUseInSidebarOrder() {
        let options = catalog.options(for: .role)
        XCTAssertEqual(options.count, 9, "9 of the 10 defined roles own a task")
        XCTAssertEqual(options.map(\.value),
                       snapshot.roles.map(\.id)
                        .filter { id in snapshot.tasks.contains { $0.roleId == id } },
                       "the menu reads in the same order as the sidebar")
        XCTAssertFalse(options.contains { $0.value == "role-e" },
                       "the one role with no tasks cannot subtract anything")
    }

    func testRepositoryOffersTheSixInUse() {
        XCTAssertEqual(catalog.options(for: .repository).count, 6)
        XCTAssertEqual(catalog.options(for: .repository).first?.value, "example-org/repo-a",
                       "most-used first")
        XCTAssertEqual(catalog.options(for: .repository).first?.count, 28)
    }

    // MARK: - Counts agree with the filter

    /// Every count in the menu must equal the number of cards selecting it
    /// yields — including the current sprint's, which the zero-log exemption
    /// inflates above its `sprints_with_time` tally.
    func testEveryCountEqualsWhatSelectingItAdmits() {
        for facet in Facet.allCases {
            for option in catalog.options(for: facet) {
                let state = FilterState(facet: facet, value: option.value)
                let admitted = TaskFilter.apply(state, to: snapshot.tasks,
                                                currentSprintID: snapshot.currentSprint?.id)
                XCTAssertEqual(option.count, admitted.count,
                               "\(facet.displayName) / \(option.label)")
            }
        }
    }

    func testTheCurrentSprintCountIncludesTheZeroLogTasks() {
        let current = try! XCTUnwrap(snapshot.currentSprint?.id)
        let option = try! XCTUnwrap(catalog.options(for: .sprint).first { $0.value == current })
        XCTAssertEqual(option.count, 18)
        XCTAssertEqual(snapshot.tasks.count { TaskFilter.sprintIDs(of: $0).contains(current) },
                       13, "13 worked in it; the other 5 are the exemption")
    }

    /// Counts span the whole snapshot, recurrent included — the filter is shared
    /// with the shelf, so a board-scoped count would mean different things in
    /// different views.
    func testCountsIncludeRecurrentTasks() {
        let roleD = try! XCTUnwrap(catalog.options(for: .role).first { $0.value == "role-d" })
        XCTAssertEqual(roleD.count, 16)
        XCTAssertTrue(snapshot.tasks.contains { $0.roleId == "role-d" && $0.status == .recurrent })
    }

    // MARK: - Self-hide

    /// §8.3: below 2 distinct values a facet is hidden entirely.
    func testAllFourFacetsAreVisibleOnTheRealDistribution() {
        XCTAssertEqual(catalog.visibleFacets, [.role, .activityType, .repository, .sprint])
    }

    func testAFacetWithOneValueIsHidden() throws {
        let json = """
        {"tasks": [
          {"id": "a", "role_id": "r1", "github_repo": "org/only", "activity": "X"},
          {"id": "b", "role_id": "r2", "github_repo": "org/only", "activity": "Y"}
        ],
         "roles": [{"id": "r1"}, {"id": "r2"}]}
        """
        let small = try JSONDecoder().decode(Snapshot.self, from: Data(json.utf8))
        let catalog = FacetCatalog.build(from: small)
        XCTAssertEqual(catalog.options(for: .repository).count, 1)
        XCTAssertFalse(catalog.isVisible(.repository), "one repo cannot subtract anything")
        XCTAssertTrue(catalog.isVisible(.role))
        XCTAssertTrue(catalog.isVisible(.activityType))
        XCTAssertFalse(catalog.isVisible(.sprint), "no logged time anywhere")
        XCTAssertEqual(catalog.visibleFacets, [.role, .activityType])
    }

    func testAnEmptySnapshotOffersNothing() {
        XCTAssertEqual(FacetCatalog.build(from: nil).visibleFacets, [])
        XCTAssertEqual(FacetCatalog.empty.options(for: .role), [])
    }

    // MARK: - Type is not a facet at all

    /// §8.1 removes Type rather than relying on the self-hide rule to drop it:
    /// all 55 tasks have it unset and the Project's option list is empty, so it
    /// could never be a filter. Asserted on both the code and the data, because
    /// only together do they justify the decision.
    func testTypeIsNotAFacet() {
        XCTAssertEqual(Facet.allCases, [.role, .activityType, .repository, .sprint])
        XCTAssertFalse(Facet.allCases.contains { $0.rawValue.lowercased().contains("type")
            && $0 != .activityType })
        XCTAssertTrue(snapshot.tasks.allSatisfy { ($0.type ?? "").isEmpty })
        XCTAssertTrue(snapshot.projectOptions.type.isEmpty)
    }

    // MARK: - Labels

    func testLabelsNeverLeakTheSentinel() {
        XCTAssertEqual(catalog.label(for: FilterState.unset, in: .activityType), "No Activity")
        for facet in Facet.allCases {
            for option in catalog.options(for: facet) {
                XCTAssertFalse(option.label.isEmpty)
                XCTAssertFalse(option.label.unicodeScalars.contains { $0.value < 0x20 })
            }
        }
    }

    /// A value not in the catalog — a role deleted between launches, restored
    /// from `@SceneStorage` — still renders as something readable.
    func testAnUnknownValueFallsBackToItself() {
        XCTAssertEqual(catalog.label(for: "role-gone", in: .role), "role-gone")
        XCTAssertEqual(catalog.label(for: FilterState.unset, in: .sprint), "None")
    }

    /// A task carrying a role id the roles list does not define must still be
    /// selectable, or it would be filterable-out but never filterable-to.
    func testAnOrphanRoleIDIsStillOffered() throws {
        let json = """
        {"tasks": [{"id": "a", "role_id": "ghost"}, {"id": "b", "role_id": "r1"}],
         "roles": [{"id": "r1", "label": "Real"}]}
        """
        let small = try JSONDecoder().decode(Snapshot.self, from: Data(json.utf8))
        let catalog = FacetCatalog.build(from: small)
        XCTAssertEqual(catalog.options(for: .role).map(\.value), ["r1", "ghost"])
    }

    func testATaskWithNoRoleIsOffered() throws {
        let json = """
        {"tasks": [{"id": "a"}, {"id": "b", "role_id": "r1"}],
         "roles": [{"id": "r1", "label": "Real"}]}
        """
        let small = try JSONDecoder().decode(Snapshot.self, from: Data(json.utf8))
        let catalog = FacetCatalog.build(from: small)
        XCTAssertEqual(catalog.options(for: .role).map(\.label), ["Real", "No Role"])
        XCTAssertEqual(catalog.options(for: .role).last?.count, 1)
    }
}
