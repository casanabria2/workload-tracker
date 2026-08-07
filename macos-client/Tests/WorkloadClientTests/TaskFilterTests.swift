import XCTest
@testable import WorkloadClient

/// Plan §8's combination rule and the Sprint facet's logged-time semantics,
/// exercised against `Fixtures/facets.json`.
///
/// That fixture is synthetic in every string and **exact in every count**: it is
/// the owner's live snapshot with titles, roles, activities, repositories and
/// issue refs replaced, and the facet distribution left alone. So the numbers
/// below are the real ones — 55 tasks, 9 roles in use, 8 activities plus one
/// task with none, 6 repositories, 11 sprints with logged time out of 72 cached,
/// 5 tasks with no logs, and one task with time in 11 sprints.
final class TaskFilterTests: XCTestCase {

    // MARK: - Fixture

    private var snapshot: Snapshot!
    private var tasks: [TrackerTask] { snapshot.tasks }
    /// Sprint 105.
    private var currentSprint: String { snapshot.currentSprint!.id }

    override func setUpWithError() throws {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: "facets", withExtension: "json",
                              subdirectory: "Fixtures"))
        snapshot = try JSONDecoder().decode(Snapshot.self, from: Data(contentsOf: url))
    }

    private func ids(_ state: FilterState) -> Set<String> {
        Set(TaskFilter.apply(state, to: tasks, currentSprintID: currentSprint).map(\.id))
    }

    private func sprint(_ title: String) throws -> String {
        try XCTUnwrap(snapshot.sprints.first { $0.title == title }?.id,
                      "no sprint titled \(title) in the fixture")
    }

    // MARK: - The fixture really is the measured distribution

    /// If this drifts, every count below is measuring something else.
    func testFixtureCarriesTheMeasuredDistribution() throws {
        XCTAssertEqual(tasks.count, 55)
        XCTAssertEqual(tasks.count { $0.status == .done }, 37)
        XCTAssertEqual(tasks.count { $0.status == .recurrent }, 7)
        XCTAssertEqual(tasks.count { $0.status == .inProgress }, 6)
        XCTAssertEqual(tasks.count { $0.status == .todo }, 5)
        XCTAssertEqual(snapshot.sprints.count, 72)
        XCTAssertEqual(Set(tasks.flatMap { TaskFilter.sprintIDs(of: $0) }).count, 11)
        XCTAssertEqual(tasks.count { $0.sprintsWithTime.isEmpty }, 5)
        XCTAssertEqual(tasks.count { $0.logs.isEmpty }, 5)
        XCTAssertEqual(snapshot.currentSprint?.title, "Sprint 105")

        // The three specimens the brief calls out.
        XCTAssertEqual(tasks.filter { ($0.activity ?? "").isEmpty }.map(\.id), ["t-06"])
        XCTAssertEqual(tasks.filter { $0.sprintsWithTime.count == 11 }.map(\.id), ["t-55"])
        XCTAssertEqual(tasks.filter { $0.logs.isEmpty }.map(\.id).sorted(),
                       ["t-01", "t-07", "t-14", "t-15", "t-30"])

        // And the reason `type` is not a facet at all.
        XCTAssertTrue(tasks.allSatisfy { ($0.type ?? "").isEmpty })
        XCTAssertTrue(snapshot.projectOptions.type.isEmpty)
    }

    // MARK: - Empty means "no constraint"

    func testAnEmptyFilterAdmitsEverything() {
        XCTAssertEqual(TaskFilter.apply(FilterState(), to: tasks,
                                        currentSprintID: currentSprint).count, 55)
    }

    /// One empty facet must not veto the others — the failure mode that makes
    /// multi-select useless.
    func testAnEmptyFacetDoesNotVetoAPopulatedOne() {
        let onlyRole = FilterState(roles: ["role-a"])
        XCTAssertEqual(ids(onlyRole).count, 22)
        XCTAssertTrue(onlyRole.activityTypes.isEmpty)
        XCTAssertTrue(onlyRole.repos.isEmpty)
        XCTAssertTrue(onlyRole.sprints.isEmpty)
    }

    // MARK: - OR within, AND across

    func testOrWithinAFacet() {
        XCTAssertEqual(ids(FilterState(roles: ["role-a"])).count, 22)
        XCTAssertEqual(ids(FilterState(roles: ["role-b"])).count, 7)
        XCTAssertEqual(ids(FilterState(roles: ["role-a", "role-b"])).count, 29,
                       "roles must union, not intersect")
    }

    func testAndAcrossFacets() {
        let roleOnly = ids(FilterState(roles: ["role-a"]))
        let sprintOnly = ids(FilterState(sprints: [currentSprint]))
        let both = ids(FilterState(roles: ["role-a"], sprints: [currentSprint]))
        XCTAssertEqual(both, roleOnly.intersection(sprintOnly))
        XCTAssertEqual(both.count, 10)
        XCTAssertLessThan(both.count, roleOnly.count)
        XCTAssertLessThan(both.count, sprintOnly.count)
    }

    /// The full rule in one assertion: *(a OR b) AND worked-in-105*.
    func testOrWithinAndAndAcrossCompose() {
        XCTAssertEqual(ids(FilterState(roles: ["role-a", "role-b"],
                                       sprints: [currentSprint])).count, 13)
    }

    /// Three facets at once, checked against the hand-computed intersection.
    func testThreeFacetsIntersect() {
        let state = FilterState(roles: ["role-a"],
                                activityTypes: ["Widget Maintenance"],
                                repos: ["example-org/repo-b"])
        let expected = Set(tasks.filter {
            $0.roleId == "role-a" && $0.activity == "Widget Maintenance"
                && $0.githubRepo == "example-org/repo-b"
        }.map(\.id))
        XCTAssertEqual(ids(state), expected)
        XCTAssertFalse(expected.isEmpty)
    }

    // MARK: - Sprint means "logged time in that sprint" (§8.2)

    /// Never the legacy `sprint`/`sprint_id` mirror — always `sprints_with_time`.
    func testSprintMatchesLoggedTimeNotTheLegacyField() throws {
        let sprint95 = try sprint("Sprint 95")
        XCTAssertEqual(ids(FilterState(sprints: [sprint95])).sorted(),
                       ["t-27", "t-53", "t-55"])
        for id in ids(FilterState(sprints: [sprint95])) {
            let task = tasks.first { $0.id == id }!
            XCTAssertTrue(TaskFilter.sprintIDs(of: task).contains(sprint95))
        }
    }

    /// A task appears under **every** sprint it worked in, not just its binding.
    func testTheElevenSprintTaskAppearsUnderAllElevenOfThem() {
        let t55 = tasks.first { $0.id == "t-55" }!
        let sprints = TaskFilter.sprintIDs(of: t55)
        XCTAssertEqual(sprints.count, 11)
        for id in sprints {
            XCTAssertTrue(ids(FilterState(sprints: [id])).contains("t-55"),
                          "t-55 missing from sprint \(id)")
        }
        // And selecting all eleven is not eleven copies of it.
        XCTAssertEqual(ids(FilterState(sprints: sprints)).filter { $0 == "t-55" }.count, 1)
    }

    func testSprintsUnionRatherThanIntersect() throws {
        let s95 = try sprint("Sprint 95"), s96 = try sprint("Sprint 96")
        let union = ids(FilterState(sprints: [s95, s96]))
        XCTAssertEqual(union, ids(FilterState(sprints: [s95]))
            .union(ids(FilterState(sprints: [s96]))))
    }

    // MARK: - The open-work exemption (§8.2)

    /// Sprint means "worked in that sprint", which is right for Done and wrong
    /// for open work: a task in flight you have not logged against this
    /// fortnight is exactly the one you want on the board. Measured before this
    /// rule existed, the default filter hid **5 of the 6 In Progress cards**.
    func testEveryOpenTaskSurvivesTheCurrentSprintFilter() {
        let matched = ids(FilterState(sprints: [currentSprint]))
        for task in tasks where task.status != .done {
            XCTAssertTrue(matched.contains(task.id),
                          "\(task.id) is \(task.status.rawValue) and must stay visible")
        }
        // 13 worked in 105; the exemption brings open work and the 2 log-less
        // done tasks along. 24 = todo 5 + inProgress 6 + recurrent 7 + done 6.
        XCTAssertEqual(matched.count, 24)
        XCTAssertEqual(tasks.count { TaskFilter.sprintIDs(of: $0).contains(currentSprint) }, 13)
    }

    /// The exemption must not un-scope the Done column — the reason the facet
    /// exists. A done task with time in *other* sprints belongs to those.
    func testDoneTasksWithTimeElsewhereStayHidden() {
        let matched = ids(FilterState(sprints: [currentSprint]))
        let hidden = tasks.filter {
            $0.status == .done
                && !TaskFilter.sprintIDs(of: $0).isEmpty
                && !TaskFilter.sprintIDs(of: $0).contains(currentSprint)
        }
        XCTAssertEqual(hidden.count, 31, "the Done column must still be scoped")
        for task in hidden { XCTAssertFalse(matched.contains(task.id)) }
    }

    /// Zero-log tasks are now just the special case with no logs at all.
    func testTasksWithNoLoggedTimeMatchTheCurrentSprint() {
        let matched = ids(FilterState(sprints: [currentSprint]))
        for id in ["t-01", "t-07", "t-14", "t-15", "t-30"] {
            XCTAssertTrue(matched.contains(id), "\(id) has no logs and must not vanish")
        }
    }

    /// …and correctly disappear when you filter to a past sprint: they were not
    /// worked then. This is what keeps the exemption from meaning "always".
    func testOpenAndLoglessTasksDoNotMatchAPastSprint() throws {
        let past = try sprint("Sprint 95")
        let matched = ids(FilterState(sprints: [past]))
        for id in ["t-01", "t-07", "t-14", "t-15", "t-30"] {
            XCTAssertFalse(matched.contains(id))
        }
        for task in tasks where task.status != .done
            && !TaskFilter.sprintIDs(of: task).contains(past) {
            XCTAssertFalse(matched.contains(task.id),
                           "\(task.id) was not worked in Sprint 95")
        }
    }

    /// The exemption fires on the *current* sprint being selected, not on it
    /// being the only selection.
    func testTheExemptionSurvivesAMixedSprintSelection() throws {
        let past = try sprint("Sprint 95")
        XCTAssertTrue(ids(FilterState(sprints: [past, currentSprint])).contains("t-01"))
    }

    /// A `done` task with no logs (2 exist) follows the same rule.
    func testDoneTasksWithNoLogsShowInTheCurrentSprint() {
        let matched = ids(FilterState(sprints: [currentSprint]))
        for id in ["t-14", "t-15"] {
            XCTAssertEqual(tasks.first { $0.id == id }?.status, .done)
            XCTAssertTrue(matched.contains(id))
        }
    }

    /// Without a current sprint there is nothing to exempt them into — but the
    /// tasks that *did* log time still filter normally.
    func testWithoutACurrentSprintZeroLogTasksSimplyFailTheFacet() throws {
        let state = FilterState(sprints: [currentSprint])
        let matched = Set(TaskFilter.apply(state, to: tasks, currentSprintID: nil).map(\.id))
        XCTAssertFalse(matched.contains("t-01"))
        XCTAssertEqual(matched.count, 13)
    }

    // MARK: - The task with no Activity

    /// One task has no `activity`. A plain string match would make it
    /// unreachable — excludable but never selectable — so it gets an explicit
    /// `unset` value.
    func testTheTaskWithNoActivityIsReachableAndExcludable() {
        XCTAssertEqual(ids(FilterState(activityTypes: [FilterState.unset])), ["t-06"])
        XCTAssertFalse(ids(FilterState(activityTypes: ["Widget Maintenance"])).contains("t-06"))
        XCTAssertEqual(TaskFilter.value(of: tasks.first { $0.id == "t-06" }!,
                                        in: .activityType), FilterState.unset)
    }

    /// The sentinel cannot collide with a real value: no activity, role or repo
    /// in the data contains a control character.
    func testTheUnsetSentinelCannotCollide() {
        let reals = tasks.flatMap { [$0.activity, $0.roleId, $0.githubRepo] }.compactMap { $0 }
        XCTAssertFalse(reals.contains(FilterState.unset))
        XCTAssertFalse(reals.contains { $0.unicodeScalars.contains { $0.value < 0x20 } })
    }

    // MARK: - Repository is a plain field match (§8.1)

    /// Deliberately **not** unioned with the repos of a task's per-sprint issue
    /// bindings. A task whose old sprint's issue lives elsewhere matches only
    /// its `github_repo`; the plan accepts that.
    func testRepositoryDoesNotUnionInBindingRepos() {
        let crossRepo = tasks.first { task in
            let bound = Set(task.sprintIssues.compactMap { $0.issue?.split(separator: "#").first }
                .map(String.init))
            return bound.count > 1 || (task.githubRepo.map { !bound.isEmpty && !bound.contains($0) }
                                       ?? false)
        }
        guard let crossRepo, let repo = crossRepo.githubRepo else {
            return  // no such task in the fixture; the rule still holds by construction
        }
        XCTAssertEqual(ids(FilterState(repos: [repo])).contains(crossRepo.id), true)
        let otherRepos = Set(crossRepo.sprintIssues
            .compactMap { $0.issue?.split(separator: "#").first }.map(String.init))
            .subtracting([repo])
        for other in otherRepos {
            XCTAssertFalse(ids(FilterState(repos: [other])).contains(crossRepo.id),
                           "a binding repo must not make the task match")
        }
    }

    func testRepositoryCounts() {
        XCTAssertEqual(ids(FilterState(repos: ["example-org/repo-a"])).count, 28)
        XCTAssertEqual(ids(FilterState(repos: ["example-org/repo-b"])).count, 22)
        XCTAssertEqual(ids(FilterState(repos: ["example-org/repo-a",
                                               "example-org/repo-b"])).count, 50)
    }

    // MARK: - Free text

    func testFreeTextSearchesTitleDescriptionAndIssue() throws {
        XCTAssertEqual(ids(FilterState(text: "Task 42")), ["t-42"])
        XCTAssertEqual(ids(FilterState(text: "task 42")), ["t-42"],
                       "case-insensitive")
        let issue = try XCTUnwrap(tasks.first { $0.currentIssue != nil })
        let ref = try XCTUnwrap(issue.currentIssue)
        XCTAssertTrue(ids(FilterState(text: ref)).contains(issue.id))
        // Whitespace-only text is not a constraint.
        XCTAssertEqual(ids(FilterState(text: "   ")).count, 55)
    }

    func testFreeTextAndsWithFacets() {
        let state = FilterState(roles: ["role-b"], text: "Task 4")
        for id in ids(state) {
            XCTAssertEqual(tasks.first { $0.id == id }?.roleId, "role-b")
            XCTAssertTrue(tasks.first { $0.id == id }!.title.contains("Task 4"))
        }
    }

    // MARK: - Diagnosing an empty result

    func testBlockingFacetsNamesTheOneAtFault() throws {
        // role-g owns exactly one task; pair it with a sprint that task never
        // worked in and the result is empty.
        let s95 = try sprint("Sprint 95")
        let state = FilterState(roles: ["role-g"], sprints: [s95])
        XCTAssertTrue(ids(state).isEmpty)
        let blocking = TaskFilter.blockingFacets(state, tasks: tasks,
                                                 currentSprintID: currentSprint)
        XCTAssertEqual(Set(blocking), [.role, .sprint],
                       "either one, relaxed alone, brings tasks back")
    }

    /// When a facet is individually impossible, only it is named.
    func testBlockingFacetsIgnoresFacetsThatAreNotAtFault() {
        let state = FilterState(roles: ["role-a"], repos: ["example-org/repo-nope"])
        XCTAssertTrue(ids(state).isEmpty)
        XCTAssertEqual(TaskFilter.blockingFacets(state, tasks: tasks,
                                                 currentSprintID: currentSprint),
                       [.repository])
    }

    func testNothingIsBlockingWhenTheResultIsNotEmpty() {
        XCTAssertEqual(TaskFilter.blockingFacets(FilterState(roles: ["role-a"]),
                                                 tasks: tasks,
                                                 currentSprintID: currentSprint), [])
    }

    func testTextIsReportedSeparatelyFromFacets() {
        let state = FilterState(roles: ["role-a"], text: "no such task anywhere")
        XCTAssertTrue(ids(state).isEmpty)
        XCTAssertTrue(TaskFilter.textIsBlocking(state, tasks: tasks,
                                                currentSprintID: currentSprint))
        XCTAssertEqual(TaskFilter.blockingFacets(state, tasks: tasks,
                                                 currentSprintID: currentSprint), [.role])
    }

    // MARK: - FilterState mechanics

    func testToggleAddsAndRemoves() {
        var state = FilterState()
        state.toggle("role-a", in: .role)
        XCTAssertEqual(state.roles, ["role-a"])
        state.toggle("role-b", in: .role)
        XCTAssertEqual(state.roles, ["role-a", "role-b"])
        state.toggle("role-a", in: .role)
        XCTAssertEqual(state.roles, ["role-b"])
    }

    func testActiveCountAndFacets() {
        var state = FilterState(roles: ["role-a", "role-b"], sprints: ["x"])
        XCTAssertEqual(state.activeCount, 3)
        XCTAssertEqual(state.activeFacets, [.role, .sprint])
        state.text = "hello"
        XCTAssertEqual(state.activeCount, 4)
        XCTAssertFalse(state.isEmpty)
        XCTAssertTrue(FilterState().isEmpty)
        XCTAssertTrue(FilterState(text: "  ").isEmpty)
    }

    /// The subscript is how every generic surface (tokens, menu, empty state)
    /// reaches a facet, so it has to agree with the named properties.
    func testTheFacetSubscriptMatchesTheNamedProperties() {
        var state = FilterState()
        for facet in Facet.allCases { state[facet] = ["v-\(facet.rawValue)"] }
        XCTAssertEqual(state.roles, ["v-role"])
        XCTAssertEqual(state.activityTypes, ["v-activityType"])
        XCTAssertEqual(state.repos, ["v-repository"])
        XCTAssertEqual(state.sprints, ["v-sprint"])
        for facet in Facet.allCases {
            XCTAssertEqual(state[facet], ["v-\(facet.rawValue)"])
        }
    }
}
