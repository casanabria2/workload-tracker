import XCTest
@testable import WorkloadClient

/// The inspector's model (plan §11).
///
/// Ordering is the substance here. A panel that lists a task's logs and its
/// per-sprint bindings in the wrong order is wrong in the way a chart is wrong:
/// it renders, it looks plausible, and nothing goes red. Two orderings have
/// specific traps —
///
/// * logs must sort on `log_effective_date` (`started_at` before `at`), because
///   29 of the owner's 419 logs have no `started_at` at all;
/// * bindings must sort on the sprint's **start date**, because sorting on the
///   title puts `Sprint 99` after `Sprint 100`.
final class TaskInspectorTests: XCTestCase {

    // MARK: - Fixtures

    private func loadSnapshot() throws -> Snapshot {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: "snapshot", withExtension: "json",
                              subdirectory: "Fixtures"))
        return try JSONDecoder().decode(Snapshot.self, from: Data(contentsOf: url))
    }

    private func model(_ taskID: String) throws -> TaskInspectorModel {
        let snapshot = try loadSnapshot()
        let task = try XCTUnwrap(snapshot.tasks.first { $0.id == taskID })
        let role = snapshot.roles.first { $0.id == task.roleId }
        return TaskInspectorModel(task: task,
                                  roleLabel: role?.displayName ?? "no role",
                                  sprints: snapshot.sprints)
    }

    // MARK: - Bindings

    func testBindingsAreNewestSprintFirst() throws {
        let model = try model("t-multi-sprint")
        XCTAssertEqual(model.bindings.map(\.sprint),
                       ["Sprint 102", "Sprint 101", "Sprint 100"])
    }

    /// Not on the title. `Sprint 100` sorts *before* `Sprint 99` as a string,
    /// which is exactly backwards, so the ordering has to come from the dates.
    func testBindingOrderIsByDateNotByTitle() throws {
        let dates = try model("t-multi-sprint").bindings.compactMap(\.startDate)
        XCTAssertEqual(dates, dates.sorted(by: >))
        XCTAssertEqual(dates.count, 3)
    }

    /// Each binding carries the minutes the **logs** put in that sprint, from
    /// `sprints_with_time` — never a figure re-derived in Swift, and never
    /// GitHub's cached `hours_synced`.
    func testBindingMinutesComeFromSprintsWithTime() throws {
        let model = try model("t-multi-sprint")
        let byTitle = Dictionary(uniqueKeysWithValues:
            model.bindings.map { ($0.sprint, $0.loggedMins) })
        XCTAssertEqual(byTitle["Sprint 100"], 375)
        XCTAssertEqual(byTitle["Sprint 101"], 210)
        XCTAssertEqual(byTitle["Sprint 102"], 105)
    }

    /// **A sprint with logged time and no binding is still a row.** That gap is
    /// precisely what `wt sync-sprints` exists to close; hiding it would make
    /// the inspector agree with GitHub rather than with the logs.
    func testASprintWithTimeAndNoBindingStillAppears() throws {
        let model = try model("t-done-nulls")
        XCTAssertEqual(model.bindings.count, 1)
        let only = try XCTUnwrap(model.bindings.first)
        XCTAssertEqual(only.sprint, "Sprint 100")
        XCTAssertNil(only.issue)
        XCTAssertNil(only.hoursSynced)
        XCTAssertEqual(only.loggedMins, 30)
    }

    /// A never-synced binding that has time is out of sync — "GitHub has not
    /// been told" and "GitHub was told zero" are different facts.
    func testNeverSyncedBindingWithTimeIsOutOfSync() throws {
        let only = try XCTUnwrap(try model("t-done-nulls").bindings.first)
        XCTAssertTrue(only.isOutOfSync)
        XCTAssertEqual(try model("t-done-nulls").outOfSyncBindings.count, 1)
    }

    /// `hours_synced` is in **hours**; `loggedMins` is in minutes. Comparing
    /// them without the conversion would report every synced binding as broken.
    func testHoursSyncedIsComparedInTheRightUnit() {
        let matching = TaskInspectorModel.Binding(
            sprintId: "sp-1", sprint: "Sprint 1", issue: "o/r#1", isClosed: false,
            hoursSynced: 2, loggedMins: 120, startDate: "2026-01-01")
        XCTAssertFalse(matching.isOutOfSync)

        let drifted = TaskInspectorModel.Binding(
            sprintId: "sp-1", sprint: "Sprint 1", issue: "o/r#1", isClosed: false,
            hoursSynced: 2, loggedMins: 180, startDate: "2026-01-01")
        XCTAssertTrue(drifted.isOutOfSync)
    }

    func testATaskWithNoSprintsHasNoBindings() throws {
        XCTAssertTrue(try model("t-minimal").bindings.isEmpty)
    }

    // MARK: - Logs

    func testLogsAreNewestFirstOnTheEffectiveDate() throws {
        let logs = try model("t-multi-sprint").logs
        let dates = logs.map { $0.effectiveDate ?? 0 }
        XCTAssertEqual(dates, dates.sorted(by: >))
        XCTAssertEqual(logs.count, 3)
    }

    func testATaskWithNoLogsHasNone() throws {
        XCTAssertTrue(try model("t-no-logs").logs.isEmpty)
    }

    // MARK: - Details

    func testDetailsAlwaysLeadWithStatusAndRole() throws {
        let details = try model("t-multi-sprint").details
        XCTAssertEqual(details.first?.label, "Status")
        XCTAssertEqual(details.dropFirst().first?.label, "Role")
    }

    /// The issue row reads `current_issue`, which the daemon resolves through
    /// `task_current_issue()` — never the legacy `github_issue` mirror.
    func testTheIssueRowUsesTheResolvedCurrentIssue() throws {
        let model = try model("t-multi-sprint")
        let issue = model.details.first { $0.label == "Current issue" }
        XCTAssertEqual(issue?.value, "example-org/other-repo#7")
        XCTAssertTrue(issue?.isMonospaced ?? false)
    }

    /// Absent fields produce no row rather than an empty one, so the grid never
    /// shows "Activity: —".
    func testAbsentFieldsProduceNoRow() throws {
        let labels = try model("t-minimal").details.map(\.label)
        XCTAssertFalse(labels.contains("Repository"))
        XCTAssertFalse(labels.contains("Current issue"))
        XCTAssertFalse(labels.contains("Activity"))
    }

    // MARK: - Accessibility

    func testAccessibilityDescriptionNamesTheTaskAndItsTotals() throws {
        let model = try model("t-multi-sprint")
        let text = model.accessibilityDescription
        XCTAssertTrue(text.hasPrefix(model.title), text)
        XCTAssertTrue(text.contains("reportable"), text)
        XCTAssertTrue(text.contains("3 log entries"), text)
        XCTAssertTrue(text.contains("3 sprint bindings"), text)
    }

    func testAccessibilityDescriptionIsSingularForOne() throws {
        let text = try model("t-running").accessibilityDescription
        XCTAssertTrue(text.contains("1 log entry"), text)
        XCTAssertTrue(text.contains("1 sprint binding"), text)
    }
}

// MARK: - Quarter-hour rounding is not "out of sync"

/// The tracker reports hours to GitHub through `wt.round_to_quarter_hours`,
/// which rounds **up** to the next 15 minutes. Comparing that against raw
/// logged minutes flagged almost every binding as out of sync — the owner saw
/// it on real data — so these pin the reported figure against the Python side.
final class BindingRoundingTests: XCTestCase {
    private func binding(logged: Double, synced: Double?) -> TaskInspectorModel.Binding {
        TaskInspectorModel.Binding(sprintId: "s", sprint: "Sprint 105", issue: "o/r#1",
                              isClosed: true, hoursSynced: synced,
                              loggedMins: logged, startDate: "2026-07-27")
    }

    /// Values measured from the owner's own data on 2026-08-13, with the figure
    /// `wt.mins_to_quarter_hours` produced for each.
    func testReportedMinutesMatchesThePythonRounding() {
        let cases: [(hoursLogged: Double, reportedHours: Double)] = [
            (23.74, 23.75), (20.82, 21.0), (15.32, 15.5),
            (1.13, 1.25), (1.64, 1.75), (2.83, 3.0),
            (3.25, 3.25),   // already on a quarter — must not round up a full step
            (0.0, 0.0),     // nothing logged reports nothing
        ]
        for (logged, expected) in cases {
            XCTAssertEqual(TaskInspectorModel.Binding.reportedMinutes(logged * 60),
                           expected * 60, accuracy: 0.001,
                           "\(logged)h should report as \(expected)h")
        }
    }

    func testRoundingAloneIsNotOutOfSync() {
        // 15h 19m logged, GitHub told 15.5h — exactly what syncing would send.
        XCTAssertFalse(binding(logged: 15.32 * 60, synced: 15.5).isOutOfSync)
        XCTAssertFalse(binding(logged: 1.13 * 60, synced: 1.25).isOutOfSync)
        XCTAssertFalse(binding(logged: 23.74 * 60, synced: 23.75).isOutOfSync)
    }

    func testARealGapIsStillFlagged() {
        // The appenv#1413 case: GitHub carried 12.5h for a sprint with no work.
        XCTAssertTrue(binding(logged: 0, synced: 12.5).isOutOfSync)
        // A whole quarter-hour step out is a real difference, not rounding.
        XCTAssertTrue(binding(logged: 15.32 * 60, synced: 15.25).isOutOfSync)
        XCTAssertTrue(binding(logged: 15.32 * 60, synced: 15.75).isOutOfSync)
        // Never synced, but time logged.
        XCTAssertTrue(binding(logged: 60, synced: nil).isOutOfSync)
        // Never synced and nothing logged is fine.
        XCTAssertFalse(binding(logged: 0, synced: nil).isOutOfSync)
    }
}
