import XCTest
@testable import WorkloadClient

/// The close-confirmation sheet (plan §7.1) and the flow behind it.
///
/// Written and passing **before** the Done drop target existed. That ordering is
/// the point: `POST /v1/tasks/{id}/close` runs `gh issue create` and
/// `gh issue close` against the owner's real GitHub org, so no code path that
/// can reach it may exist until the gate in front of it is proven.
///
/// Every assertion here is about *which requests were issued*, not about
/// rendering — the safety property is "nothing irreversible left the process
/// without an explicit confirm", and that is observable at the transport.
@MainActor
final class CloseSheetTests: XCTestCase {

    // MARK: - Fixtures

    /// The fixture as raw bytes. `Data` is `Sendable`, so it can be captured by
    /// the stub's responder closure; a `[String: Any]` cannot.
    private func planData() throws -> Data {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: "close-plan", withExtension: "json",
                              subdirectory: "Fixtures"))
        return try Data(contentsOf: url)
    }

    /// The fixture with one edit applied, re-encoded. Used to reach the plan
    /// shapes the owner's data does not currently contain.
    private func planData(_ edit: (inout [String: Any]) -> Void) throws -> Data {
        var object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: try planData()) as? [String: Any])
        edit(&object)
        return try JSONSerialization.data(withJSONObject: object)
    }

    private func loadPlan() throws -> ClosePlanResponse {
        try JSONDecoder().decode(ClosePlanResponse.self, from: try planData())
    }

    private func loadSnapshot() throws -> Snapshot {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: "snapshot", withExtension: "json",
                              subdirectory: "Fixtures"))
        return try JSONDecoder().decode(Snapshot.self, from: Data(contentsOf: url))
    }

    private func makeStore(_ transport: StubTransport) throws -> Store {
        Store(client: transport.makeClient(), snapshot: try loadSnapshot())
    }

    private func task(_ id: String, in store: Store) throws -> TrackerTask {
        try XCTUnwrap(store.tasks.first { $0.id == id })
    }

    // MARK: - The plan decodes into the rows the sheet renders

    func testPlanDecoding() throws {
        let plan = try loadPlan()
        XCTAssertEqual(plan.repo, "example-org/example-repo")
        XCTAssertFalse(plan.needsIssue)
        XCTAssertEqual(plan.willCreateIssues, 1)
        XCTAssertTrue(plan.plan.dryRun, "close/plan must be a dry run")
        XCTAssertTrue(plan.isActionable)
        XCTAssertEqual(plan.plan.target.count, 3)
        XCTAssertEqual(plan.planLines.count, 3)
    }

    /// The count the warning is built from. `will_create_issues` alone
    /// under-counts, because `close_task` mints a task's *first* issue itself
    /// rather than through the reconcile plan.
    func testIssuesToCreateAddsTheFirstIssueCase() throws {
        let plan = try loadPlan()
        XCTAssertEqual(plan.issuesToCreate, 1)
        XCTAssertFalse(plan.createIssueOnConfirm)

        let unlinked = try JSONDecoder().decode(
            ClosePlanResponse.self,
            from: try planData { raw in
                raw["needs_issue"] = true
                raw["current_issue"] = NSNull()
            })
        XCTAssertEqual(unlinked.issuesToCreate, 2,
                       "one from the reconcile plan plus the task's first issue")
        XCTAssertTrue(unlinked.createIssueOnConfirm,
                      "confirming must authorise exactly what the preview announced")
    }

    func testPlanRowsMatchTheSpecLayout() throws {
        let rows = try loadPlan().rows
        XCTAssertEqual(rows.map(\.sprintTitle), ["Sprint 100", "Sprint 101", "Sprint 102"])

        // Sprint 100: bound, hours already synced, sprint ended -> close only.
        XCTAssertEqual(rows[0].issue, "example-org/example-repo#377")
        XCTAssertFalse(rows[0].createsIssue)
        XCTAssertTrue(rows[0].closesIssue)
        XCTAssertEqual(rows[0].actions, ["close issue — sprint has ended"])

        // Sprint 101: the "(no issue) CREATE issue, set hours, close" line.
        XCTAssertNil(rows[1].issue)
        XCTAssertTrue(rows[1].createsIssue)
        XCTAssertTrue(rows[1].closesIssue)
        XCTAssertEqual(rows[1].actions, ["CREATE issue in example-org/example-repo",
                                         "set hours to 3.5h",
                                         "close issue — sprint has ended"])

        // Sprint 102: hours update, issue stays open.
        XCTAssertEqual(rows[2].issue, "example-org/example-repo#402")
        XCTAssertFalse(rows[2].createsIssue)
        XCTAssertFalse(rows[2].closesIssue, "the current sprint's issue stays open")
        XCTAssertEqual(rows[2].actions, ["update hours 6h → 12.5h"])
    }

    /// The Hours field is quarter hours, so the sheet must not print `21.0h`
    /// where the CLI prints `21h`, nor round `12.25h` away.
    func testHoursFormatting() {
        XCTAssertEqual(ClosePlanResponse.hours(21), "21h")
        XCTAssertEqual(ClosePlanResponse.hours(0), "0h")
        XCTAssertEqual(ClosePlanResponse.hours(12.5), "12.5h")
        XCTAssertEqual(ClosePlanResponse.hours(12.25), "12.25h")
        XCTAssertEqual(ClosePlanResponse.hours(3.75), "3.75h")
    }

    /// A withheld-hours plan must say so on the row. Getting this wrong would
    /// show a confident "update hours" for a write that is deliberately not made.
    func testWithheldHoursSurfacesOnTheRow() throws {
        let decoded = try JSONDecoder().decode(
            ClosePlanResponse.self,
            from: try planData { raw in
                var plan = raw["plan"] as! [String: Any]
                plan["planned"] = []
                plan["skipped"] = [[
                    "sprint": "Sprint 102", "sprint_id": "sp-102",
                    "issue": "example-org/example-repo#402",
                    "minutes": 739.27, "hours": 12.5, "from_hours": 6.0,
                    "withheld_hours": true, "reason": "hours withheld — unreported time",
                ]]
                plan["unbillable"] = [["sprint": "Sprint 101", "sprint_id": "sp-101",
                                       "minutes": 210.0]]
                raw["plan"] = plan
                raw["will_create_issues"] = 0
            })

        let row = try XCTUnwrap(decoded.rows.first { $0.sprintTitle == "Sprint 102" })
        XCTAssertTrue(row.hoursWithheld)
        XCTAssertTrue(row.actions.contains { $0.hasPrefix("hours withheld") })
        XCTAssertEqual(decoded.issuesToCreate, 0)
        XCTAssertFalse(decoded.plan.unbillable.isEmpty)
    }

    /// A dry run that failed means the real close would fail too, so the confirm
    /// button must be off rather than offering a guaranteed error.
    func testFailedDryRunIsNotActionable() throws {
        let decoded = try JSONDecoder().decode(
            ClosePlanResponse.self,
            from: try planData { raw in
                var plan = raw["plan"] as! [String: Any]
                plan["success"] = false
                plan["error"] = "No sprints found"
                raw["plan"] = plan
            })
        XCTAssertFalse(decoded.isActionable)
    }

    // MARK: - Opening the sheet sends the dry run and nothing else

    func testBeginCloseIssuesOnlyTheDryRun() async throws {
        let transport = StubTransport()
        let planJSON = try planData()
        transport.respond { _ in .raw(planJSON) }
        let store = try makeStore(transport)

        await store.beginClose(try task("t-multi-sprint", in: store))

        XCTAssertEqual(transport.requestLines,
                       ["POST /v1/tasks/t-multi-sprint/close/plan"],
                       "opening the sheet must send the dry run and nothing else")
        guard case .ready(let plan) = try XCTUnwrap(store.closeSheet).phase else {
            return XCTFail("expected .ready, got \(String(describing: store.closeSheet?.phase))")
        }
        XCTAssertEqual(plan.issuesToCreate, 1)
    }

    /// **The load-bearing test of this phase.** Dropping on Done opens a sheet;
    /// abandoning it must leave GitHub untouched.
    func testCancellingTheSheetNeverIssuesAClose() async throws {
        let transport = StubTransport()
        let planJSON = try planData()
        transport.respond { _ in .raw(planJSON) }
        let store = try makeStore(transport)

        await store.beginClose(try task("t-multi-sprint", in: store))
        store.dismissCloseSheet()

        XCTAssertNil(store.closeSheet)
        XCTAssertFalse(transport.requestLines.contains {
            $0 == "POST /v1/tasks/t-multi-sprint/close"
        }, "a cancelled sheet must not have closed anything")
        XCTAssertEqual(transport.requestLines,
                       ["POST /v1/tasks/t-multi-sprint/close/plan"])
    }

    /// `confirmClose` is a no-op unless the sheet is showing a plan the user
    /// could have read. Without this, a stray call could close a task while the
    /// dry run was still in flight.
    func testConfirmDoesNothingWithoutAPlanOnScreen() async throws {
        let transport = StubTransport()
        transport.respond { _ in
            .failure(code: "not_found", message: "unstubbed", status: 404)
        }
        let store = try makeStore(transport)

        await store.confirmClose()
        XCTAssertTrue(transport.requests.isEmpty, "no sheet, no request")
        XCTAssertNil(store.closeSheet)
    }

    // MARK: - Confirming

    func testConfirmIssuesTheCloseWithThePlansCreateIssueFlag() async throws {
        let transport = StubTransport()
        let planJSON = try planData()
        transport.respond { request in
            switch request.path {
            case "/v1/tasks/t-multi-sprint/close/plan": .raw(planJSON)
            case "/v1/tasks/t-multi-sprint/close":
                .json(["operation_id": "op-1", "op": "close", "task_id": "t-multi-sprint",
                       "state": "running", "progress": []], status: 202)
            // Anything else is a 404 rather than a plausible-looking body:
            // a stubbed-but-empty `/v1/snapshot` would silently blank the store
            // mid-test and make the board assertions race the refresh.
            default: .failure(code: "not_found", message: "unstubbed", status: 404)
            }
        }
        let store = try makeStore(transport)

        await store.beginClose(try task("t-multi-sprint", in: store))
        await store.confirmClose()

        let close = try XCTUnwrap(transport.requests.first {
            $0.path == "/v1/tasks/t-multi-sprint/close"
        })
        XCTAssertEqual(close.method, "POST")
        // The fixture's task already has an issue, so the workflow must not be
        // authorised to mint one.
        XCTAssertEqual(close.body["create_issue"], "false")
        guard case .closing(let operationId, _, _) = try XCTUnwrap(store.closeSheet).phase else {
            return XCTFail("expected .closing, got \(String(describing: store.closeSheet?.phase))")
        }
        XCTAssertEqual(operationId, "op-1")
        // Cancels the fallback poller, which would otherwise keep asking the
        // stub about op-1 for the next ten minutes.
        store.dismissCloseSheet()
    }

    /// When the preview says "an issue will be created", the confirm has to
    /// carry the authorisation — `wt_api.close()` *refuses* rather than mints
    /// when `create_issue` is false, so a mismatch turns into a failed close.
    func testConfirmAuthorisesIssueCreationWhenThePreviewAnnouncedIt() async throws {
        let planJSON = try planData { raw in
            raw["needs_issue"] = true
            raw["current_issue"] = NSNull()
        }

        let transport = StubTransport()
        transport.respond { request in
            request.path.hasSuffix("/close/plan")
                ? .raw(planJSON)
                : .json(["operation_id": "op-2", "state": "running", "progress": []],
                        status: 202)
        }
        let store = try makeStore(transport)

        await store.beginClose(try task("t-multi-sprint", in: store))
        await store.confirmClose()

        let close = try XCTUnwrap(transport.requests.first { $0.path.hasSuffix("/close") })
        XCTAssertEqual(close.body["create_issue"], "true")
        store.dismissCloseSheet()
    }

    /// A failed close leaves the task open — `close_task` aborts on a failed
    /// reconcile so hours cannot be mis-reported — and the sheet must say so
    /// rather than reporting success.
    func testAFailedCloseSurfacesAndLeavesTheTaskOpen() async throws {
        let transport = StubTransport()
        let planJSON = try planData()
        transport.respond { request in
            if request.path.hasSuffix("/close/plan") { return .raw(planJSON) }
            if request.path.hasSuffix("/close") {
                return .failure(code: "reconcile_failed",
                                message: "Sprint reconcile failed: gh exploded",
                                status: 502)
            }
            return .failure(code: "not_found", message: "unstubbed", status: 404)
        }
        let store = try makeStore(transport)

        await store.beginClose(try task("t-multi-sprint", in: store))
        await store.confirmClose()

        guard case .failed(let message, let code, _) =
                try XCTUnwrap(store.closeSheet).phase else {
            return XCTFail("expected .failed, got \(String(describing: store.closeSheet?.phase))")
        }
        XCTAssertEqual(code, "reconcile_failed")
        XCTAssertTrue(message.contains("gh exploded"), message)
        // The board still shows the task where it was.
        XCTAssertTrue(store.boardTasks(.inProgress).contains { $0.id == "t-multi-sprint" })
        XCTAssertFalse(store.boardTasks(.done).contains { $0.id == "t-multi-sprint" })
    }

    /// A plan that cannot be produced must not fall through to a close.
    func testAFailedPlanStopsAtThePreview() async throws {
        let transport = StubTransport()
        transport.respond { _ in
            .failure(code: "no_sprints", message: "No sprints found", status: 409)
        }
        let store = try makeStore(transport)

        await store.beginClose(try task("t-multi-sprint", in: store))
        guard case .planFailed(let message) = try XCTUnwrap(store.closeSheet).phase else {
            return XCTFail("expected .planFailed")
        }
        XCTAssertTrue(message.contains("No sprints found"), message)

        await store.confirmClose()
        XCTAssertFalse(transport.requestLines.contains { $0.hasSuffix("/close") },
                       "a failed dry run must not be confirmable")
    }

    // MARK: - Operation records

    func testOperationRecordDecodesBothTerminalStates() throws {
        let decoder = JSONDecoder()
        let running = try decoder.decode(OperationRecord.self, from: Data("""
        {"operation_id": "op-9", "op": "close", "task_id": "t", "state": "running",
         "progress": ["reconcile started"], "result": null, "error": null}
        """.utf8))
        XCTAssertFalse(running.isTerminal)
        XCTAssertNil(running.error)
        XCTAssertEqual(running.progress, ["reconcile started"])

        let failed = try decoder.decode(OperationRecord.self, from: Data("""
        {"operation_id": "op-9", "state": "failed", "progress": [],
         "error": {"code": "close_failed", "message": "no issue and no permission"}}
        """.utf8))
        XCTAssertTrue(failed.isTerminal)
        XCTAssertTrue(failed.didFail)
        XCTAssertEqual(failed.error?.code, .closeFailed)
    }
}
