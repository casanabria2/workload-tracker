import XCTest
@testable import WorkloadClient

/// Decoding tests against `Fixtures/snapshot.json`.
///
/// The fixture is **synthetic** on purpose: the real snapshot carries the
/// owner's task titles, GitHub issue refs and hours, and this repository may be
/// public. Every shape the real data exhibits is reproduced there with invented
/// content — see the `_comment` block at the top of the file.
final class SnapshotDecodingTests: XCTestCase {

    private let decoder = JSONDecoder()

    private func loadFixture() throws -> Snapshot {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: "snapshot", withExtension: "json",
                              subdirectory: "Fixtures"),
            "Fixtures/snapshot.json missing from the test bundle")
        return try decoder.decode(Snapshot.self, from: Data(contentsOf: url))
    }

    private func task(_ id: String, in snapshot: Snapshot) throws -> TrackerTask {
        try XCTUnwrap(snapshot.tasks.first { $0.id == id }, "no task \(id)")
    }

    // MARK: - Top level

    func testDecodesTopLevel() throws {
        let snapshot = try loadFixture()
        XCTAssertEqual(snapshot.tasks.count, 7)
        XCTAssertEqual(snapshot.roles.count, 7)
        XCTAssertEqual(snapshot.sprints.count, 3)
        XCTAssertEqual(snapshot.generatedAt ?? 0, 1786022400.5, accuracy: 0.001)
        XCTAssertEqual(snapshot.currentSprint?.id, "sp-102")
        XCTAssertEqual(snapshot.currentSprint?.title, "Sprint 102")
    }

    func testDecodesActiveTimer() throws {
        let snapshot = try loadFixture()
        let timer = try XCTUnwrap(snapshot.activeTimer)
        XCTAssertEqual(timer.taskId, "t-running")
        XCTAssertEqual(timer.startedAt ?? 0, 1786020000.0, accuracy: 0.001)
        // Elapsed ticks locally against a supplied `now`.
        let now = Date(timeIntervalSince1970: 1786020000.0 + 125)
        XCTAssertEqual(timer.elapsed(asOf: now), 125, accuracy: 0.001)
        XCTAssertEqual(Duration.formatElapsed(timer.elapsed(asOf: now)), "2:05")
    }

    func testDecodesRolesIncludingDuplicateAndWhiteColors() throws {
        let snapshot = try loadFixture()
        XCTAssertEqual(snapshot.roles.map(\.id).prefix(3), ["demokit", "demos", "other"])
        XCTAssertEqual(snapshot.roles.filter { $0.color == "white" }.count, 3)
        XCTAssertEqual(snapshot.roles.filter { $0.color == "blue" }.count, 2)
        // A role id containing a space (the real data has `iron infusion`).
        XCTAssertTrue(snapshot.roles.contains { $0.id == "spare parts" })
    }

    func testDecodesSprintDates() throws {
        let snapshot = try loadFixture()
        let sprint = try XCTUnwrap(snapshot.sprints.first { $0.id == "sp-101" })
        XCTAssertEqual(sprint.startDate, "2026-03-30")
        let start = try XCTUnwrap(sprint.start, "ISO yyyy-MM-dd should parse")
        let end = try XCTUnwrap(sprint.end)
        XCTAssertLessThan(start, end)
    }

    /// The live data stores `github_project_number` as the **string** `"565"`.
    /// A strict `Int` here would fail the entire snapshot decode.
    func testProjectNumberDecodesFromAString() throws {
        let snapshot = try loadFixture()
        XCTAssertEqual(snapshot.config.githubProjectNumber?.value, 565)
        XCTAssertEqual(snapshot.config.githubProjectNumber?.raw, "565")
        XCTAssertEqual(snapshot.config.githubProjectOwner, "example-org")
    }

    func testProjectNumberAlsoDecodesFromANumber() throws {
        let json = Data(#"{"github_project_number": 565}"#.utf8)
        let config = try decoder.decode(SnapshotConfig.self, from: json)
        XCTAssertEqual(config.githubProjectNumber?.value, 565)
    }

    /// Zero Type options is the real state of the owner's project cache — an
    /// empty list must decode, not be read as "absent".
    func testProjectOptions() throws {
        let snapshot = try loadFixture()
        XCTAssertEqual(snapshot.projectOptions.activity.count, 3)
        XCTAssertTrue(snapshot.projectOptions.type.isEmpty)
    }

    func testDataFileProbe() throws {
        let snapshot = try loadFixture()
        let probe = try XCTUnwrap(snapshot.dataFile)
        XCTAssertTrue(probe.readable)
        XCTAssertEqual(probe.reason, "ok")
        XCTAssertNil(probe.advice)
    }

    /// The state that must never render as an empty board.
    func testUnreadableProbeCarriesAdvice() throws {
        let json = Data("""
        {"path": "/x", "readable": false, "reason": "permission_denied",
         "detail": "Operation not permitted"}
        """.utf8)
        let probe = try decoder.decode(DataFileProbe.self, from: json)
        XCTAssertFalse(probe.readable)
        XCTAssertNotNil(probe.advice)
        XCTAssertTrue(probe.advice!.contains("Full Disk Access"))
    }

    // MARK: - Tasks

    func testDecodesEveryPlannedTaskField() throws {
        let snapshot = try loadFixture()
        let task = try self.task("t-multi-sprint", in: snapshot)

        XCTAssertEqual(task.title, "Rebuild the widget pipeline")
        XCTAssertEqual(task.status, .inProgress)
        XCTAssertEqual(task.statusLabel, "In Progress")
        XCTAssertEqual(task.roleId, "demokit")
        XCTAssertEqual(task.activity, "Widget Maintenance")
        XCTAssertEqual(task.githubRepo, "example-org/example-repo")
        XCTAssertNil(task.type)
        XCTAssertEqual(task.startSprint, "Sprint 100")
        XCTAssertEqual(task.startSprintId, "sp-100")
        XCTAssertEqual(task.currentIssue, "example-org/other-repo#7")
        XCTAssertEqual(task.loggedMins, 690)
        XCTAssertEqual(task.reportableMins, 105)
        XCTAssertEqual(task.liveMins, 0)
        XCTAssertEqual(task.lastLoggedAt ?? 0, 1776400000.0, accuracy: 0.001)
        XCTAssertEqual(task.localFolder, "/tmp/fixture/widget")
        XCTAssertEqual(task.tabs.count, 2)
        XCTAssertNil(task.activeWindowId)
        XCTAssertTrue(task.hasGitHub)
    }

    func testDecodesSprintsWithTime() throws {
        let snapshot = try loadFixture()
        let task = try self.task("t-multi-sprint", in: snapshot)
        XCTAssertEqual(task.sprintsWithTime.count, 3)
        XCTAssertEqual(task.sprintsWithTime.map(\.sprintId), ["sp-100", "sp-101", "sp-102"])
        XCTAssertEqual(task.sprintsWithTime.map(\.totalMins), [375, 210, 105])
        // The convenience accessor the board and the shelf read.
        XCTAssertEqual(task.minutes(inSprint: "sp-101"), 210)
        XCTAssertEqual(task.minutes(inSprint: "sp-999"), 0)
    }

    /// Bindings carry a full `owner/repo#n` ref — and a task's issues can live
    /// in different repos across sprints, which the fixture exercises.
    func testDecodesSprintIssueBindings() throws {
        let snapshot = try loadFixture()
        let task = try self.task("t-multi-sprint", in: snapshot)
        XCTAssertEqual(task.sprintIssues.count, 3)

        let first = task.sprintIssues[0]
        XCTAssertEqual(first.sprintId, "sp-100")
        XCTAssertEqual(first.sprint, "Sprint 100")
        XCTAssertEqual(first.issue, "example-org/example-repo#101")
        XCTAssertTrue(first.isClosed)
        XCTAssertEqual(first.hoursSynced, 6.25)

        // A binding that was never linked: issue null, hours never synced.
        let unlinked = task.sprintIssues[1]
        XCTAssertNil(unlinked.issue)
        XCTAssertNil(unlinked.hoursSynced)
        XCTAssertFalse(unlinked.isClosed)

        // A binding in a different repo from the task's own github_repo.
        XCTAssertEqual(task.sprintIssues[2].issue, "example-org/other-repo#7")
    }

    func testDecodesLogsIncludingTheOnesWithNoWallClock() throws {
        let snapshot = try loadFixture()
        let task = try self.task("t-multi-sprint", in: snapshot)
        XCTAssertEqual(task.logs.count, 3)

        let timed = task.logs[0]
        XCTAssertTrue(timed.hasWallClock)
        XCTAssertEqual(timed.minutes, 375)
        XCTAssertEqual(timed.effectiveDate, timed.startedAt)

        // 29 of the owner's 416 logs look like this: `at` only.
        let untimed = task.logs[2]
        XCTAssertFalse(untimed.hasWallClock)
        XCTAssertNil(untimed.startedAt)
        XCTAssertEqual(untimed.effectiveDate, untimed.at)

        // Explicit nulls for started_at/ended_at behave like absence.
        let nulled = try self.task("t-done-nulls", in: snapshot).logs[0]
        XCTAssertFalse(nulled.hasWallClock)
        XCTAssertEqual(nulled.note, "")
    }

    func testCalendarImportedLogCarriesItsUID() throws {
        let snapshot = try loadFixture()
        let task = try self.task("t-recurrent", in: snapshot)
        XCTAssertEqual(task.logs.last?.calendarEventUid, "fixture-event-uid-1")
    }

    /// A task with no logs at all — 5 exist in the real data, 3 of them live
    /// To Do cards that must not vanish.
    func testTaskWithNoLogs() throws {
        let snapshot = try loadFixture()
        let task = try self.task("t-no-logs", in: snapshot)
        XCTAssertTrue(task.logs.isEmpty)
        XCTAssertTrue(task.sprintsWithTime.isEmpty)
        XCTAssertTrue(task.sprintIssues.isEmpty)
        XCTAssertNil(task.lastLoggedAt)
        XCTAssertNil(task.currentIssue)
        XCTAssertEqual(task.loggedMins, 0)
        XCTAssertFalse(task.hasGitHub)  // github_repo is "" here, not null.
    }

    /// The graceful-degradation case: every optional key **absent**. This is the
    /// convention that let the monitor survive two contract changes.
    func testTaskWithEveryOptionalFieldAbsent() throws {
        let snapshot = try loadFixture()
        let task = try self.task("t-minimal", in: snapshot)
        XCTAssertEqual(task.status, .todo)
        XCTAssertEqual(task.description, "")
        XCTAssertNil(task.statusLabel)
        XCTAssertNil(task.roleId)
        XCTAssertNil(task.activity)
        XCTAssertNil(task.githubRepo)
        XCTAssertNil(task.createdAt)
        XCTAssertNil(task.lastLoggedAt)
        XCTAssertEqual(task.loggedMins, 0)
        XCTAssertEqual(task.reportableMins, 0)
        XCTAssertTrue(task.logs.isEmpty)
        XCTAssertTrue(task.tabs.isEmpty)
        XCTAssertTrue(task.sprintIssues.isEmpty)
    }

    /// A status added on the Python side must not fail the whole decode.
    func testUnknownStatusRoundTrips() throws {
        let snapshot = try loadFixture()
        let task = try self.task("t-future-status", in: snapshot)
        XCTAssertEqual(task.status, .unknown("blocked"))
        XCTAssertEqual(task.status.rawValue, "blocked")
        XCTAssertEqual(task.status.displayName, "Blocked")
        XCTAssertFalse(TaskStatus.boardColumns.contains(task.status))
    }

    func testBoardColumnsExcludeRecurrent() {
        XCTAssertEqual(TaskStatus.boardColumns, [.todo, .inProgress, .done])
        XCTAssertFalse(TaskStatus.boardColumns.contains(.recurrent))
    }

    /// An entirely empty document still yields a usable, empty snapshot rather
    /// than throwing — the daemon has never sent one, but a partial response
    /// must not crash the board.
    func testEmptyDocumentDecodes() throws {
        let snapshot = try decoder.decode(Snapshot.self, from: Data("{}".utf8))
        XCTAssertTrue(snapshot.tasks.isEmpty)
        XCTAssertTrue(snapshot.roles.isEmpty)
        XCTAssertNil(snapshot.activeTimer)
        XCTAssertNil(snapshot.dataFile)
    }

    // MARK: - Health

    func testHealthDecodes() throws {
        let json = Data("""
        {"ok": true, "version": "1.0.0", "pid": 50414, "port": 7374,
         "legacy_port": 7375, "started_at": 1786022323.78, "uptime_seconds": 349.7,
         "data_file": {"path": "/x", "readable": true, "reason": "ok"},
         "tui_bridge": {"port": 7373, "running": false},
         "subscribers": 0, "allow_empty": false, "python": "3.14.6"}
        """.utf8)
        let health = try decoder.decode(Health.self, from: json)
        XCTAssertTrue(health.ok)
        XCTAssertEqual(health.version, "1.0.0")
        XCTAssertEqual(health.legacyPort, 7375)
        XCTAssertEqual(health.tuiBridge?.running, false)
        XCTAssertEqual(health.dataFile?.readable, true)
    }

    // MARK: - Errors

    /// The daemon's `{"error": {code, message}}` body must map onto a typed
    /// error **preserving the code**.
    func testErrorBodyPreservesTheCode() throws {
        let json = Data("""
        {"error": {"code": "invalid_role", "message": "Invalid role 'nope'.",
                   "details": {"role": "nope", "available": ["demokit", "other"]}}}
        """.utf8)
        let body = try decoder.decode(DaemonErrorBody.self, from: json)
        XCTAssertEqual(body.code, .invalidRole)
        XCTAssertEqual(body.code.rawValue, "invalid_role")
        XCTAssertEqual(body.details["role"], "nope")
        XCTAssertEqual(body.details["available"], "demokit, other")
    }

    func testUnknownErrorCodeDegradesRatherThanFailing() throws {
        let json = Data(#"{"error": {"code": "brand_new_code", "message": "hi"}}"#.utf8)
        let body = try decoder.decode(DaemonErrorBody.self, from: json)
        XCTAssertEqual(body.code, .unknown("brand_new_code"))
        XCTAssertEqual(body.code.rawValue, "brand_new_code")
    }

    /// Every code in the table round-trips through `rawValue`, so a typo in the
    /// reverse map is loud. The count pins the 23 `wt_api` codes plus the
    /// daemon's 9.
    func testErrorCodeTableRoundTrips() {
        let known = DaemonErrorCode.allKnown
        XCTAssertEqual(known.count, 32)
        for code in known {
            XCTAssertEqual(DaemonErrorCode(rawValue: code.rawValue), code,
                           "\(code.rawValue) did not round-trip")
        }
    }

    func testUnreachableIsDistinctFromAnAPIError() {
        let unreachable = DaemonClientError.unreachable(
            underlying: URLError(.cannotConnectToHost))
        let apiError = DaemonClientError.api(code: .taskNotFound, message: "no",
                                             status: 404, details: [:])
        XCTAssertTrue(unreachable.isUnreachable)
        XCTAssertNil(unreachable.code)
        XCTAssertFalse(apiError.isUnreachable)
        XCTAssertEqual(apiError.code, .taskNotFound)
    }

    func testMapErrorBodyFallsBackToPlainHTTP() {
        let error = DaemonClient.mapErrorBody(Data("<html>502</html>".utf8),
                                              status: 502, decoder: JSONDecoder())
        guard case .http(let status, let body) = error else {
            return XCTFail("expected .http, got \(error)")
        }
        XCTAssertEqual(status, 502)
        XCTAssertEqual(body, "<html>502</html>")
    }

    // MARK: - Formatting

    func testDurationFormatting() {
        XCTAssertEqual(Duration.format(minutes: 0), "—")
        XCTAssertEqual(Duration.formatZeroed(minutes: 0), "0m")
        XCTAssertEqual(Duration.format(minutes: 45), "45m")
        XCTAssertEqual(Duration.format(minutes: 60), "1h")
        XCTAssertEqual(Duration.format(minutes: 375), "6h 15m")
        XCTAssertEqual(Duration.formatElapsed(65), "1:05")
        XCTAssertEqual(Duration.formatElapsed(3725), "1:02:05")
    }
}
