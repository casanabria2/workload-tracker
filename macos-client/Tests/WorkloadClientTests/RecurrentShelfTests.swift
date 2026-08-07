import XCTest
@testable import WorkloadClient

/// The recurrent shelf's row actions (plan §9), asserted **at the transport**.
///
/// Every assertion here is about *which requests were issued*, not about
/// rendering. The safety property of this phase is "nothing irreversible left
/// the process without an explicit confirmation", and that is observable as a
/// list of HTTP requests — which is the only form of it that a later refactor of
/// the views cannot quietly break.
///
/// Two of the five actions call `gh` against the owner's real org:
/// `syncSprints` mints the new sprint's issue and closes the ended one, and
/// `endSeries` closes the series' live issue with no reopen path.
@MainActor
final class RecurrentShelfTests: XCTestCase {

    // MARK: - Fixtures

    private func loadSnapshot() throws -> Snapshot {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: "recurrent-shelf", withExtension: "json",
                              subdirectory: "Fixtures"))
        return try JSONDecoder().decode(Snapshot.self, from: Data(contentsOf: url))
    }

    private func makeStore(_ transport: StubTransport) throws -> Store {
        Store(client: transport.makeClient(), snapshot: try loadSnapshot())
    }

    private func task(_ id: String, in store: Store) throws -> TrackerTask {
        try XCTUnwrap(store.tasks.first { $0.id == id })
    }

    /// A `close/plan` body shaped like the ones measured against the owner's
    /// real recurrent tasks: **`planned` is empty**. The reconcile only emits a
    /// `close` op for a sprint that has *ended*, and a perpetual task's current
    /// binding is by definition the current sprint — so the plan says "no
    /// change" while the close would still close the live issue.
    private func recurrentClosePlan(currentIssue: String? = "example-org/other-repo#401")
        -> [String: Any] {
        var object: [String: Any] = [
            "title": "Ad-hoc questions",
            "repo": "example-org/other-repo",
            "needs_issue": false,
            "will_create_issues": 0,
            "plan_lines": [],
            "plan": [
                "success": true, "dry_run": true, "current_sprint": "Sprint 102",
                "target": [["sprint_id": "sp-102", "sprint": "Sprint 102",
                            "minutes": 169.0, "hours": 3.0]],
                "planned": [], "skipped": [], "unbillable": [],
                "bindings": [["sprint_id": "sp-102", "sprint": "Sprint 102",
                              "issue": currentIssue as Any, "state": "open"]],
                "unassigned_minutes": 0,
            ] as [String: Any],
        ]
        if let currentIssue { object["current_issue"] = currentIssue }
        return object
    }

    /// A reconcile dry run with one real GitHub consequence.
    private func reconcileDryRun(planned: [[String: Any]]) -> [String: Any] {
        [
            "dry_run": true,
            "success": true,
            "plan_lines": planned.map { "plan line for \($0["op"] ?? "?")" },
            "outcome_lines": [],
            "result": [
                "success": true, "dry_run": true, "current_sprint": "Sprint 102",
                "target": [["sprint_id": "sp-102", "sprint": "Sprint 102",
                            "minutes": 169.0, "hours": 3.0]],
                "planned": planned, "skipped": [], "unbillable": [],
                "bindings": [], "unassigned_minutes": 0,
            ] as [String: Any],
        ]
    }

    // MARK: - Shelf partitioning and the Sprint facet (plan §9)

    func testShelfHoldsOnlyRecurrentTasks() throws {
        let store = try makeStore(StubTransport())
        XCTAssertEqual(Set(store.recurrentTasks.map(\.id)),
                       ["r-quiet", "r-live", "r-unlinked", "r-series"])
        XCTAssertFalse(store.recurrentTasks.contains { $0.id == "b-open" })
    }

    /// **The §9 exception.** Selecting a past sprint alone must not empty the
    /// shelf, and must still subtract from the board.
    func testShelfIgnoresTheSprintFacet() throws {
        let store = try makeStore(StubTransport())
        store.restoreFilter(FilterState(sprints: ["sp-100"]))

        XCTAssertEqual(store.filteredRecurrentTasks.count, 4,
                       "the shelf must keep every perpetual row under any sprint selection")
        XCTAssertTrue(store.filteredBoardTasks(.inProgress).isEmpty,
                      "the board must still obey the sprint facet")
    }

    /// The regression this replaces: with the full filter applied, a past-sprint
    /// selection wiped the shelf, because §8.2's open-work exemption only
    /// rescues a task when the *current* sprint is among the selection.
    func testTheOldFullFilterWouldHaveEmptiedTheShelf() throws {
        let store = try makeStore(StubTransport())
        let pastOnly = FilterState(sprints: ["sp-100"])
        let underOldRule = TaskFilter.apply(pastOnly, to: store.recurrentTasks,
                                            currentSprintID: "sp-102")
        XCTAssertEqual(underOldRule.count, 1,
                       "only r-quiet has sp-100 time; the other three vanished")
        XCTAssertEqual(TaskFilter.applyToShelf(pastOnly, to: store.recurrentTasks,
                                               currentSprintID: "sp-102").count, 4)
    }

    /// Every other facet still applies to the shelf.
    func testShelfStillObeysTheOtherFacets() throws {
        let store = try makeStore(StubTransport())
        store.restoreFilter(FilterState(roles: ["demokit"]))
        XCTAssertEqual(store.filteredRecurrentTasks.map(\.id), ["r-live"])

        store.restoreFilter(FilterState(repos: ["example-org/example-repo"]))
        XCTAssertEqual(Set(store.filteredRecurrentTasks.map(\.id)), ["r-quiet", "r-series"])

        store.restoreFilter(FilterState(text: "questions"))
        XCTAssertEqual(store.filteredRecurrentTasks.map(\.id), ["r-live"])
    }

    /// The "This sprint" column still reads the selection, so the facet changes
    /// what you read even though it does not change which rows exist.
    func testShelfSprintColumnFollowsTheSelection() throws {
        let store = try makeStore(StubTransport())
        XCTAssertEqual(store.shelfSprint?.id, "sp-102")

        store.restoreFilter(FilterState(sprints: ["sp-100"]))
        XCTAssertEqual(store.shelfSprint?.id, "sp-100")
        let quiet = try task("r-quiet", in: store)
        XCTAssertEqual(quiet.minutes(inSprint: try XCTUnwrap(store.shelfSprint?.id)), 90)

        // Several selected: no single column to show, so it falls back to today.
        store.restoreFilter(FilterState(sprints: ["sp-100", "sp-101"]))
        XCTAssertEqual(store.shelfSprint?.id, "sp-102")
    }

    // MARK: - End Series: the strongest gate in the app

    /// Opening the sheet sends the **dry run and nothing else**.
    func testEndSeriesOnlySendsTheDryRun() async throws {
        let transport = StubTransport()
        let plan = try JSONSerialization.data(withJSONObject: recurrentClosePlan())
        transport.respond { _ in .raw(plan) }
        let store = try makeStore(transport)

        await store.perform(.endSeries, on: try task("r-live", in: store))

        XCTAssertEqual(transport.requestLines, ["POST /v1/tasks/r-live/close/plan"])
        XCTAssertFalse(transport.requestLines.contains { $0.hasSuffix("/close") },
                       "the irreversible close must not be reachable by opening a sheet")
    }

    /// **The gate.** Confirming with the name untyped sends nothing at all.
    func testConfirmingWithoutTypingTheNameSendsNothing() async throws {
        let transport = StubTransport()
        let plan = try JSONSerialization.data(withJSONObject: recurrentClosePlan())
        transport.respond { _ in .raw(plan) }
        let store = try makeStore(transport)

        await store.perform(.endSeries, on: try task("r-live", in: store))
        transport.reset()

        // The button is disabled in the sheet, but the store must refuse too.
        await store.confirmClose()
        XCTAssertEqual(transport.requestLines, [],
                       "an unsatisfied typed confirmation must not reach POST /close")

        // A near miss is still a miss.
        store.updateEndSeriesConfirmation("Ad-hoc question")
        await store.confirmClose()
        XCTAssertEqual(transport.requestLines, [])

        // And the sheet is still sitting in `.ready`, not half-closed.
        if case .ready = try XCTUnwrap(store.closeSheet).phase {} else {
            XCTFail("the sheet left .ready without a confirmation")
        }
    }

    /// Typing the exact name is what unlocks it — and only then does `POST
    /// /close` appear.
    func testTypingTheNameUnlocksTheClose() async throws {
        let transport = StubTransport()
        let plan = try JSONSerialization.data(withJSONObject: recurrentClosePlan())
        transport.respond { request in
            request.path.hasSuffix("/close/plan")
                ? .raw(plan)
                : .json(["operation_id": "op-1", "state": "running", "progress": []],
                        status: 202)
        }
        let store = try makeStore(transport)

        await store.perform(.endSeries, on: try task("r-live", in: store))
        transport.reset()

        store.updateEndSeriesConfirmation("ad-hoc questions")
        await store.confirmClose()

        XCTAssertEqual(transport.requestLines, ["POST /v1/tasks/r-live/close"])
        XCTAssertEqual(transport.requests.first?.body["create_issue"], "false")
    }

    /// The gate is on the *state*, so it holds however the sheet is driven.
    func testGateIsEvaluatedInTheModelNotTheView() throws {
        let plan = try JSONDecoder().decode(
            ClosePlanResponse.self,
            from: try JSONSerialization.data(withJSONObject: recurrentClosePlan()))
        var sheet = CloseSheetState(taskId: "r-live", title: "Ad-hoc questions",
                                    phase: .ready(plan),
                                    endSeries: EndSeriesConfirmation(
                                        seriesName: "Ad-hoc questions",
                                        issue: "example-org/other-repo#401",
                                        bindingCount: 2))
        XCTAssertFalse(sheet.allowsConfirmation(plan))
        sheet.endSeries?.typed = "Ad-hoc questions"
        XCTAssertTrue(sheet.allowsConfirmation(plan))

        // A board close has no gate and is allowed as soon as the plan is.
        let board = CloseSheetState(taskId: "b-open", title: "Board task",
                                    phase: .ready(plan), endSeries: nil)
        XCTAssertTrue(board.allowsConfirmation(plan))
    }

    /// A recurrent task with no issue still gets the gate — the local recurrence
    /// ends either way — and the prose says nothing is closed on GitHub.
    func testEndSeriesWithoutAnIssueStillGated() async throws {
        let transport = StubTransport()
        let plan = try JSONSerialization.data(
            withJSONObject: recurrentClosePlan(currentIssue: nil))
        transport.respond { _ in .raw(plan) }
        let store = try makeStore(transport)

        await store.perform(.endSeries, on: try task("r-unlinked", in: store))
        let gate = try XCTUnwrap(store.closeSheet?.endSeries)
        XCTAssertNil(gate.issue)
        XCTAssertFalse(gate.isSatisfied)
        XCTAssertTrue(gate.consequenceLines.contains { $0.contains("no linked issue") })
    }

    /// The gate names the **series**, falling back to the title when the daemon
    /// sends no canonical name — which is every task today.
    func testGateNamesTheSeriesWhenTheDaemonSendsOne() async throws {
        let transport = StubTransport()
        let plan = try JSONSerialization.data(withJSONObject: recurrentClosePlan())
        transport.respond { _ in .raw(plan) }
        let store = try makeStore(transport)

        await store.perform(.endSeries, on: try task("r-series", in: store))
        XCTAssertEqual(store.closeSheet?.endSeries?.seriesName, "Canonical Series Name")
        store.dismissCloseSheet()

        await store.perform(.endSeries, on: try task("r-live", in: store))
        XCTAssertEqual(store.closeSheet?.endSeries?.seriesName, "Ad-hoc questions",
                       "with no series name from the daemon, the title is what is named")
    }

    // MARK: - Sync Sprints: dry run, then confirm

    func testSyncSprintsOnlySendsTheDryRunFirst() async throws {
        let transport = StubTransport()
        let dry = try JSONSerialization.data(withJSONObject: reconcileDryRun(planned: [
            ["op": "create", "sprint_id": "sp-102", "sprint": "Sprint 102",
             "create_issue": true, "repo": "example-org/other-repo",
             "issue_title": "Ad-hoc questions (Sprint 102)", "hours": 3.0,
             "minutes": 169.0, "will_close": false],
        ]))
        transport.respond { _ in .raw(dry) }
        let store = try makeStore(transport)

        await store.perform(.syncSprints, on: try task("r-live", in: store))

        XCTAssertEqual(transport.requestLines, ["POST /v1/tasks/r-live/reconcile"])
        XCTAssertEqual(transport.requests.first?.body["dry_run"], "true",
                       "the preview must be the write-free branch")
        XCTAssertEqual(transport.requests.first?.body["create_issues"], "false",
                       "issue creation is opted into, matching wt sync-sprints' safety rule")
    }

    func testSyncSprintsConfirmationRunsItForReal() async throws {
        let transport = StubTransport()
        let dry = try JSONSerialization.data(withJSONObject: reconcileDryRun(planned: [
            ["op": "close", "sprint_id": "sp-101", "sprint": "Sprint 101",
             "issue": "example-org/other-repo#400"],
        ]))
        transport.respond { request in
            request.body["dry_run"] == "true"
                ? .raw(dry)
                : .json(["operation_id": "op-9", "state": "running", "progress": []],
                        status: 202)
        }
        let store = try makeStore(transport)

        await store.perform(.syncSprints, on: try task("r-live", in: store))
        transport.reset()
        await store.confirmSyncSprints()

        XCTAssertEqual(transport.requestLines, ["POST /v1/tasks/r-live/reconcile"])
        XCTAssertEqual(transport.requests.first?.body["dry_run"], "false")
    }

    /// A plan that would do nothing is not confirmable — measured on the owner's
    /// data, six of seven recurrent tasks plan exactly nothing, so this is the
    /// common case and must not offer a button that runs `gh` for no reason.
    func testANoOpPlanCannotBeConfirmed() async throws {
        let transport = StubTransport()
        let dry = try JSONSerialization.data(withJSONObject: reconcileDryRun(planned: []))
        transport.respond { _ in .raw(dry) }
        let store = try makeStore(transport)

        await store.perform(.syncSprints, on: try task("r-quiet", in: store))
        guard case .ready(let plan) = try XCTUnwrap(store.syncSheet).phase else {
            return XCTFail("expected a ready sheet")
        }
        XCTAssertTrue(plan.isNoOp)
        XCTAssertFalse(plan.isActionable)

        transport.reset()
        await store.confirmSyncSprints()
        XCTAssertEqual(transport.requestLines, [], "a no-op plan must send nothing")
    }

    /// Toggling "create missing issues" re-plans, so the preview can never
    /// describe a smaller run than the button will start.
    func testTogglingIssueCreationRePlans() async throws {
        let transport = StubTransport()
        let dry = try JSONSerialization.data(withJSONObject: reconcileDryRun(planned: []))
        transport.respond { _ in .raw(dry) }
        let store = try makeStore(transport)

        await store.perform(.syncSprints, on: try task("r-quiet", in: store))
        transport.reset()
        await store.setSyncCreatesIssues(true)

        XCTAssertEqual(transport.requestLines, ["POST /v1/tasks/r-quiet/reconcile"])
        XCTAssertEqual(transport.requests.first?.body["dry_run"], "true")
        XCTAssertEqual(transport.requests.first?.body["create_issues"], "true")
    }

    /// A failed dry run leaves nothing to confirm.
    func testAFailedDryRunIsNotConfirmable() async throws {
        let transport = StubTransport()
        transport.respond { _ in .failure(code: "no_sprints", message: "No sprints found") }
        let store = try makeStore(transport)

        await store.perform(.syncSprints, on: try task("r-live", in: store))
        guard case .planFailed = try XCTUnwrap(store.syncSheet).phase else {
            return XCTFail("expected planFailed")
        }
        transport.reset()
        await store.confirmSyncSprints()
        XCTAssertEqual(transport.requestLines, [])
    }

    // MARK: - The impact count the confirmation is built around

    func testImpactCountsIrreversibleOperations() throws {
        let response = try JSONDecoder().decode(
            ReconcileResponse.self,
            from: try JSONSerialization.data(withJSONObject: reconcileDryRun(planned: [
                ["op": "create", "sprint_id": "sp-100", "sprint": "Sprint 100",
                 "create_issue": true, "repo": "r", "hours": 1.0, "will_close": true],
                ["op": "close", "sprint_id": "sp-101", "sprint": "Sprint 101",
                 "issue": "r#2"],
                ["op": "hours", "sprint_id": "sp-102", "sprint": "Sprint 102",
                 "issue": "r#3", "hours": 3.0, "from_hours": 1.5],
            ])))
        let impact = response.impact
        XCTAssertEqual(impact.issuesCreated, 1)
        XCTAssertEqual(impact.issuesClosed, 2, "the created-then-closed one counts in both")
        XCTAssertEqual(impact.hoursUpdated, 1)
        XCTAssertTrue(impact.isIrreversible)
        XCTAssertTrue(response.isActionable)
    }

    func testImpactOfAPlanThatOnlyUpdatesHoursIsNotIrreversible() throws {
        let response = try JSONDecoder().decode(
            ReconcileResponse.self,
            from: try JSONSerialization.data(withJSONObject: reconcileDryRun(planned: [
                ["op": "hours", "sprint_id": "sp-102", "sprint": "Sprint 102",
                 "issue": "r#3", "hours": 3.0, "from_hours": 1.5],
            ])))
        XCTAssertFalse(response.impact.isIrreversible)
        XCTAssertEqual(response.impact.summary, "1 hours field updated.")
        XCTAssertEqual(response.rows.count, 1)
    }

    // MARK: - The benign actions

    /// The timer flag is **always sent**, never omitted.
    ///
    /// The daemon's `browser` default flipped from `true` to `false` in
    /// `0fdf2d7`. A client that leaned on the server default would have changed
    /// behaviour that day without a line of its own changing, so the flag is
    /// explicit and this asserts it reaches the wire.
    func testStartTimerAlwaysSendsTheBrowserFlag() async throws {
        let transport = StubTransport()
        // Only the timer path answers. The post-write `refresh()` gets a 404,
        // which leaves the existing snapshot in place — answering it with this
        // body would decode as a snapshot with no tasks and lose the fixture.
        transport.respond { request in
            request.path == "/v1/timer/start"
                ? .json(["task_id": "r-live", "title": "Ad-hoc questions",
                         "started_at": 1776600000.0, "stopped": NSNull()])
                : .failure(code: "not_found", message: "unstubbed", status: 404)
        }
        let store = try makeStore(transport)
        let restore = AppSettings.opensTaskWindow
        defer { AppSettings.opensTaskWindow = restore }

        AppSettings.opensTaskWindow = false
        await store.perform(.startTimer, on: try task("r-live", in: store))
        var start = try XCTUnwrap(transport.requests.first { $0.path == "/v1/timer/start" })
        XCTAssertEqual(start.body["browser"], "false")
        XCTAssertEqual(start.body["task_id"], "r-live")

        // And the capability is still reachable when the setting asks for it.
        transport.reset()
        AppSettings.opensTaskWindow = true
        await store.perform(.startTimer, on: try task("r-quiet", in: store))
        start = try XCTUnwrap(transport.requests.first { $0.path == "/v1/timer/start" })
        XCTAssertEqual(start.body["browser"], "true")
    }

    /// The shipped default must be **off**, matching the daemon and the plan's
    /// decision that the Safari integration is a removal target.
    func testTaskWindowDefaultsOff() {
        let restore = AppSettings.opensTaskWindow
        defer { AppSettings.opensTaskWindow = restore }
        UserDefaults.standard.removeObject(forKey: "opensTaskWindow")
        XCTAssertFalse(AppSettings.opensTaskWindow)
    }

    /// Start Timer is a no-op on the task the timer is already running on, and
    /// issues no request.
    func testStartTimerIsInertWhenAlreadyRunningOnThatTask() async throws {
        let transport = StubTransport()
        transport.respond { _ in .json([:]) }
        var object = try XCTUnwrap(JSONSerialization.jsonObject(
            with: try Data(contentsOf: XCTUnwrap(
                Bundle.module.url(forResource: "recurrent-shelf", withExtension: "json",
                                  subdirectory: "Fixtures")))) as? [String: Any])
        object["active_timer"] = ["task_id": "r-live", "started_at": 1776600000.0]
        let snapshot = try JSONDecoder().decode(
            Snapshot.self, from: try JSONSerialization.data(withJSONObject: object))
        let store = Store(client: transport.makeClient(), snapshot: snapshot)

        await store.perform(.startTimer, on: try task("r-live", in: store))
        XCTAssertEqual(transport.requestLines, [])
    }

    /// Open Issue is read-only and unavailable without one.
    func testOpenIssueIsInertWithoutAnIssue() async throws {
        let transport = StubTransport()
        transport.respond { _ in .json(["opened": true]) }
        let store = try makeStore(transport)

        await store.perform(.openIssue, on: try task("r-unlinked", in: store))
        XCTAssertEqual(transport.requestLines, [])

        await store.perform(.openIssue, on: try task("r-live", in: store))
        XCTAssertEqual(transport.requestLines, ["POST /v1/tasks/r-live/github/open"])
    }

    /// Log Time opens a sheet rather than logging anything, and the sheet's
    /// commit is what writes.
    func testLogTimeOpensASheetBeforeWriting() async throws {
        let transport = StubTransport()
        transport.respond { _ in
            .json(["task_id": "r-live",
                   "log": ["id": "log-new", "minutes": 30.0, "note": "Manual entry"]],
                  status: 201)
        }
        let store = try makeStore(transport)

        await store.perform(.logTime, on: try task("r-live", in: store))
        XCTAssertEqual(transport.requestLines, [], "opening the sheet must not log")
        XCTAssertEqual(store.logSheet?.taskId, "r-live")

        await store.confirmLogTime(minutes: 30, note: "")
        // A successful write is followed by a resync, so filter to the writes.
        let writes = transport.requests.filter { $0.method == "POST" }
        XCTAssertEqual(writes.map(\.line), ["POST /v1/tasks/r-live/logs"])
        XCTAssertEqual(writes.first?.body["minutes"], "30")
        XCTAssertNil(store.logSheet, "a successful log dismisses the sheet")
    }

    func testAmountParsingAcceptsMinutesAndDurations() {
        XCTAssertEqual(LogTimeSheetView.parse("30"), 30)
        XCTAssertEqual(LogTimeSheetView.parse("45m"), 45)
        XCTAssertEqual(LogTimeSheetView.parse("1h"), 60)
        XCTAssertEqual(LogTimeSheetView.parse("1.5h"), 90)
        XCTAssertEqual(LogTimeSheetView.parse("1h 30m"), 90)
        XCTAssertEqual(LogTimeSheetView.parse(" 2H 15M "), 135)
        for bad in ["", "   ", "abc", "0", "-15", "h"] {
            XCTAssertNil(LogTimeSheetView.parse(bad), "‘\(bad)’ should not parse")
        }
    }

    // MARK: - "Completed" is not "GitHub took it"

    /// The bug this replaces was observed end to end: an End Series run whose
    /// `gh issue close` failed left the binding `state: "open"`, and the sheet
    /// said *"The task is closed and its issues are up to date."*
    ///
    /// `wt.close_task` marks the task done even when the GitHub half fails, and
    /// the daemon still reports the operation `completed` — the failure rides
    /// along as a non-fatal `error` in the operation's `result`.
    func testACompletedCloseWithAGitHubErrorReportsAWarning() {
        let outcome = OperationOutcome(success: true, issueClosed: false,
                                       error: "gh issue close failed (exit 97)")
        let summary = outcome.summary(expectedIssueClose: true)
        XCTAssertTrue(summary.isWarning)
        XCTAssertTrue(summary.message.contains("GitHub did not take"))
        XCTAssertTrue(summary.message.contains("exit 97"))
        XCTAssertTrue(outcome.hasGitHubShortfall)
    }

    /// A silent shortfall — no error string, but the issue simply was not closed.
    func testAnUnclosedIssueIsAWarningEvenWithoutAnErrorString() {
        let outcome = OperationOutcome(success: true, issueClosed: false)
        XCTAssertTrue(outcome.summary(expectedIssueClose: true).isWarning)
        // ...but not when there was no issue to close in the first place.
        XCTAssertFalse(outcome.summary(expectedIssueClose: false).isWarning)
    }

    func testACleanCloseReportsSuccess() {
        let outcome = OperationOutcome(success: true, issueClosed: true,
                                       projectUpdated: true)
        let summary = outcome.summary(expectedIssueClose: true)
        XCTAssertFalse(summary.isWarning)
        XCTAssertTrue(summary.message.contains("up to date"))
    }

    func testATaskWithNoRepoSaysSoRatherThanWarning() {
        let outcome = OperationOutcome(success: true, issueClosed: false,
                                       skippedGitHub: true)
        let summary = outcome.summary(expectedIssueClose: false)
        XCTAssertFalse(summary.isWarning)
        XCTAssertTrue(summary.message.contains("no GitHub repository"))
    }

    /// The outcome must survive the wire. This is the daemon's real terminal
    /// `progress` frame shape.
    func testOutcomeDecodesFromTheTerminalProgressFrame() throws {
        let frame = """
        {"operation_id":"op-1","op":"close","state":"completed",
         "message":"close completed",
         "result":{"success":true,"issue_created":false,"issue_closed":false,
                   "project_updated":false,"skipped_github":false,
                   "error":"Failed to close issue"}}
        """
        let payload = try JSONDecoder().decode(ProgressPayload.self,
                                               from: Data(frame.utf8))
        let outcome = try XCTUnwrap(payload.result)
        XCTAssertTrue(outcome.success)
        XCTAssertFalse(outcome.issueClosed)
        XCTAssertEqual(outcome.error, "Failed to close issue")
        XCTAssertTrue(outcome.summary(expectedIssueClose: true).isWarning)
    }

    /// A daemon that sends no `result` must not break the sheet, and must not
    /// invent a warning either.
    func testAMissingResultDegradesToAPlainSuccess() throws {
        let frame = """
        {"operation_id":"op-1","op":"close","state":"completed","message":"done"}
        """
        let payload = try JSONDecoder().decode(ProgressPayload.self,
                                               from: Data(frame.utf8))
        XCTAssertNil(payload.result)
        XCTAssertFalse(OperationOutcome().summary(expectedIssueClose: true).isWarning)
    }

    /// The reconcile half of the same rule.
    func testAFailedReconcileReportsAWarningNotSuccess() throws {
        var object = reconcileDryRun(planned: [])
        object["dry_run"] = false
        var result = try XCTUnwrap(object["result"] as? [String: Any])
        result["success"] = false
        result["error"] = "gh project item-edit failed"
        object["result"] = result
        object["success"] = false
        let response = try JSONDecoder().decode(
            ReconcileResponse.self, from: try JSONSerialization.data(withJSONObject: object))
        XCTAssertTrue(response.completionSummary.isWarning)
        XCTAssertTrue(response.completionSummary.message.contains("item-edit failed"))
    }

    func testASucceededReconcileReportsSuccess() throws {
        var object = reconcileDryRun(planned: [])
        object["dry_run"] = false
        let response = try JSONDecoder().decode(
            ReconcileResponse.self, from: try JSONSerialization.data(withJSONObject: object))
        XCTAssertFalse(response.completionSummary.isWarning)
    }

    /// The operation record's `result` has to be re-decodable as *either* shape,
    /// because `close` and `reconcile` return different envelopes under one key.
    func testOperationResultDecodesAsEitherShape() throws {
        let closeRecord = """
        {"operation_id":"a","op":"close","state":"completed","progress":[],
         "result":{"success":true,"issue_closed":true}}
        """
        let close = try JSONDecoder().decode(OperationRecord.self,
                                             from: Data(closeRecord.utf8))
        XCTAssertEqual(close.result?.issueClosed, true)

        let reconcileRecord = """
        {"operation_id":"b","op":"reconcile","state":"completed","progress":[],
         "result":{"success":false,"dry_run":false,"plan_lines":[],
                   "outcome_lines":["x"],"result":{"success":false,"error":"boom"}}}
        """
        let reconcile = try JSONDecoder().decode(OperationRecord.self,
                                                 from: Data(reconcileRecord.utf8))
        XCTAssertEqual(reconcile.reconcileResult?.outcomeLines, ["x"])
        XCTAssertTrue(try XCTUnwrap(reconcile.reconcileResult).completionSummary.isWarning)
    }

    // MARK: - Sizing (the Phase 3 invariant, still holding)

    /// Row actions live in a context menu precisely so this stays true: a row's
    /// content is unchanged, so the shelf is still exactly as tall as its rows.
    func testShelfStillSizesToItsRows() {
        let one = RecurrentShelfView.naturalHeight(rows: 1)
        let two = RecurrentShelfView.naturalHeight(rows: 2)
        XCTAssertEqual(two - one, 28, accuracy: 0.001,
                       "a row must still cost exactly one row's height")
        let sevenRows: CGFloat = 30 + 1 + 28 + (7 * 28) + 8
        XCTAssertEqual(RecurrentShelfView.naturalHeight(rows: 7), sevenRows)
        XCTAssertEqual(RecurrentShelfView.naturalHeight(rows: 500),
                       RecurrentShelfView.maximumHeight, "a long list scrolls, not grows")
        XCTAssertEqual(RecurrentShelfView.naturalHeight(rows: 0),
                       RecurrentShelfView.naturalHeight(rows: 1))
    }

    func testLogTimeRefusesANonPositiveAmount() async throws {
        let transport = StubTransport()
        transport.respond { _ in .json([:]) }
        let store = try makeStore(transport)

        await store.perform(.logTime, on: try task("r-live", in: store))
        await store.confirmLogTime(minutes: 0, note: "")
        XCTAssertEqual(transport.requestLines, [])
        XCTAssertNotNil(store.logSheet, "the sheet stays open on a refused amount")
    }
}
