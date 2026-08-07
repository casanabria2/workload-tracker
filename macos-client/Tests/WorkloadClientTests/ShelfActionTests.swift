import XCTest
@testable import WorkloadClient

/// The recurrent shelf's action table and the End-series gate (plan §9).
///
/// **Written and passing before the shelf could invoke anything.** That ordering
/// is the same one Phase 4 used for the close sheet, and for the same reason:
/// `POST /v1/tasks/{id}/close` on a recurrent task ends the recurrence *and*
/// runs `gh issue close` against the owner's real org, with no reopen path. No
/// code that can reach it may exist before the gate in front of it is proven.
final class ShelfActionTests: XCTestCase {

    // MARK: - The action table

    func testMenuOrderIsTotalAndStable() {
        let order = ShelfAction.menu
        XCTAssertEqual(order.count, ShelfAction.allCases.count)
        XCTAssertEqual(order.map(\.rawValue),
                       ["startTimer", "logTime", "openIssue", "syncSprints", "endSeries"])
        XCTAssertEqual(Set(ShelfAction.allCases.map(\.menuOrder)).count,
                       ShelfAction.allCases.count,
                       "two actions share a menu position, so the order is not deterministic")
    }

    /// Guardrail: End series must not be the item the pointer lands on.
    func testEndSeriesIsNeverFirstInTheMenu() {
        XCTAssertNotEqual(ShelfAction.menu.first, .endSeries)
        XCTAssertEqual(ShelfAction.menu.last, .endSeries,
                       "the most destructive action belongs at the far end of the menu")
    }

    /// Guardrail: no keyboard shortcut may invoke it. A shortcut is a way to run
    /// an action without reading its name.
    func testEndSeriesForbidsAKeyboardShortcut() {
        XCTAssertFalse(ShelfAction.endSeries.allowsKeyboardShortcut)
        for action in ShelfAction.allCases where action != .endSeries {
            XCTAssertTrue(action.allowsKeyboardShortcut, "\(action.rawValue)")
        }
    }

    /// Guardrail: it must be visually separated from the benign items above it.
    func testDangerousActionsAreSeparated() {
        XCTAssertTrue(ShelfAction.endSeries.isSeparatedInMenu)
        XCTAssertTrue(ShelfAction.syncSprints.isSeparatedInMenu)
        XCTAssertFalse(ShelfAction.startTimer.isSeparatedInMenu)
    }

    /// The two GitHub-irreversible actions are exactly the two the brief names.
    func testIrreversibleActionsAreGatedByAPreview() {
        let irreversible = ShelfAction.allCases.filter(\.touchesGitHubIrreversibly)
        XCTAssertEqual(Set(irreversible), [.syncSprints, .endSeries])
        for action in irreversible {
            XCTAssertTrue(action.gate.requiresPreview,
                          "\(action.rawValue) can call gh irreversibly with no preview")
        }
        XCTAssertEqual(ShelfAction.endSeries.gate, .typedConfirmation)
        XCTAssertEqual(ShelfAction.syncSprints.gate, .dryRunPreview)
    }

    func testReadOnlyActionsAreNotWrites() {
        XCTAssertFalse(ShelfAction.openIssue.isWrite)
        XCTAssertEqual(ShelfAction.openIssue.gate, .none)
        XCTAssertTrue(ShelfAction.startTimer.isWrite)
        XCTAssertFalse(ShelfAction.startTimer.touchesGitHubIrreversibly)
    }

    // MARK: - Availability

    func testOpenIssueIsUnavailableWithoutAnIssue() throws {
        let withIssue = try Fixtures.recurrentTask(id: "r-1", issue: "org/repo#1")
        let without = try Fixtures.recurrentTask(id: "r-2", issue: nil)
        XCTAssertTrue(ShelfAction.openIssue
            .availability(for: withIssue, isTimerRunning: false).isAvailable)
        let missing = ShelfAction.openIssue.availability(for: without, isTimerRunning: false)
        XCTAssertFalse(missing.isAvailable)
        XCTAssertNotNil(missing.reason)
    }

    func testStartTimerIsUnavailableWhileAlreadyRunning() throws {
        let task = try Fixtures.recurrentTask(id: "r-1", issue: "org/repo#1")
        XCTAssertTrue(ShelfAction.startTimer
            .availability(for: task, isTimerRunning: false).isAvailable)
        XCTAssertFalse(ShelfAction.startTimer
            .availability(for: task, isTimerRunning: true).isAvailable)
    }

    /// End series stays available even with no issue — the local recurrence
    /// still ends, and the confirmation says so rather than the menu hiding it.
    func testEndSeriesIsAvailableWithoutAnIssue() throws {
        let task = try Fixtures.recurrentTask(id: "r-2", issue: nil)
        XCTAssertTrue(ShelfAction.endSeries
            .availability(for: task, isTimerRunning: false).isAvailable)
    }

    // MARK: - The typed confirmation

    func testConfirmationStartsUnsatisfied() {
        let gate = EndSeriesConfirmation(seriesName: "Stand Up Calls - casanabria",
                                         issue: "grafana/field-eng#6299",
                                         bindingCount: 11)
        XCTAssertFalse(gate.isSatisfied)
        XCTAssertNotNil(gate.validationHint)
    }

    func testConfirmationRequiresTheExactName() {
        var gate = EndSeriesConfirmation(seriesName: "Stand Up Calls - casanabria",
                                         issue: "grafana/field-eng#6299",
                                         bindingCount: 11)
        for wrong in ["Stand Up Calls", "stand up", "yes", "Stand Up Calls - casanabri",
                      "Stand Up Calls - casanabria x", " ", ""] {
            gate.typed = wrong
            XCTAssertFalse(gate.isSatisfied, "‘\(wrong)’ must not satisfy the gate")
        }
    }

    /// Case and surrounding/interior whitespace are forgiven — the point is to
    /// make the user reproduce the *name*, not to test their shift key. This
    /// mirrors how `wt.recurrent_series_for_title()` normalises a title.
    func testConfirmationForgivesCaseAndWhitespaceOnly() {
        var gate = EndSeriesConfirmation(seriesName: "Stand Up Calls - casanabria",
                                         issue: nil, bindingCount: 0)
        for right in ["Stand Up Calls - casanabria",
                      "  Stand Up Calls - casanabria  ",
                      "stand up calls - casanabria",
                      "Stand  Up   Calls - casanabria"] {
            gate.typed = right
            XCTAssertTrue(gate.isSatisfied, "‘\(right)’ should satisfy the gate")
            XCTAssertNil(gate.validationHint)
        }
    }

    /// An empty series name must never be satisfiable — otherwise an empty text
    /// field would unlock the button.
    func testEmptySeriesNameIsNeverSatisfied() {
        var gate = EndSeriesConfirmation(seriesName: "", issue: nil, bindingCount: 0)
        gate.typed = ""
        XCTAssertFalse(gate.isSatisfied)
        gate.typed = "   "
        XCTAssertFalse(gate.isSatisfied)
    }

    func testConfirmationBuiltFromATaskNamesItsIssue() throws {
        let task = try Fixtures.recurrentTask(id: "r-1", issue: "grafana/field-eng#6299",
                                              title: "Stand Up Calls - casanabria",
                                              bindings: 11)
        let gate = EndSeriesConfirmation(task: task)
        XCTAssertEqual(gate.seriesName, "Stand Up Calls - casanabria")
        XCTAssertEqual(gate.issue, "grafana/field-eng#6299")
        XCTAssertEqual(gate.bindingCount, 11)
    }

    /// The consequence prose must state the two things the ordinary close
    /// preview does **not**: the recurrence ends, and the live issue closes.
    func testConsequenceLinesNameTheIssueAndTheRecurrence() throws {
        let task = try Fixtures.recurrentTask(id: "r-1", issue: "grafana/field-eng#6299",
                                              title: "Stand Up Calls - casanabria",
                                              bindings: 11)
        let lines = EndSeriesConfirmation(task: task).consequenceLines
        XCTAssertTrue(lines.contains { $0.contains("recurrence ends") })
        XCTAssertTrue(lines.contains { $0.contains("grafana/field-eng#6299")
            && $0.contains("closed") })
        XCTAssertTrue(lines.contains { $0.contains("11 per-sprint bindings stay") })
    }

    /// The singular takes a singular verb — the first render said
    /// "1 per-sprint binding stay on the task; their hours".
    func testConsequenceLinesAgreeInNumber() throws {
        let one = try Fixtures.recurrentTask(id: "r-1", issue: "org/repo#1", bindings: 1)
        let lines = EndSeriesConfirmation(task: one).consequenceLines
        XCTAssertTrue(lines.contains {
            $0.contains("1 per-sprint binding stays") && $0.contains("its hours")
        }, "got: \(lines)")
    }

    func testConsequenceLinesSayWhenThereIsNoIssue() throws {
        let task = try Fixtures.recurrentTask(id: "r-2", issue: nil, bindings: 0)
        let lines = EndSeriesConfirmation(task: task).consequenceLines
        XCTAssertTrue(lines.contains { $0.contains("no linked issue") })
        XCTAssertFalse(lines.contains { $0.contains("per-sprint binding") })
    }

    // MARK: - Series resolution (the snapshot gap)

    /// The snapshot does not carry `recurrent_series` today. This asserts the
    /// **honest** behaviour — an em dash and a stated reason — rather than a
    /// Swift-side reimplementation of `RECURRENT_SERIES_ALIASES`, which
    /// CLAUDE.md forbids.
    func testSeriesIsUnresolvedWhenTheDaemonSendsNoName() throws {
        let tasks = [
            try Fixtures.recurrentTask(id: "r-1", issue: nil,
                                       title: "Stand Up Calls - casanabria"),
            try Fixtures.recurrentTask(id: "r-2", issue: nil, title: "1:1 with TomD"),
        ]
        XCTAssertFalse(RecurrentSeries.isSupported(by: tasks))
        XCTAssertNotNil(RecurrentSeries.unsupportedReason(for: tasks))
        for task in tasks {
            XCTAssertNil(RecurrentSeries.canonicalName(for: task))
            XCTAssertEqual(RecurrentSeries.displayName(for: task), "—")
        }
    }

    /// Two rows of one series must not be merged just because their titles look
    /// alike — each unresolved task forms its own group.
    func testUnresolvedTasksAreNeverGroupedTogether() throws {
        let tasks = [
            try Fixtures.recurrentTask(id: "r-1", issue: nil,
                                       title: "Ad-hoc Slack Questions"),
            try Fixtures.recurrentTask(id: "r-2", issue: nil,
                                       title: "Ad-hoc Slack Questions - casanabria"),
        ]
        XCTAssertEqual(RecurrentSeries.groups(in: tasks).count, 2,
                       "fuzzy title matching must not merge drifted titles")
    }

    /// And when the daemon *does* send the field, it is used verbatim and
    /// grouping collapses. This is the forward-compatibility contract.
    func testSeriesIsUsedWhenTheDaemonSendsIt() throws {
        let tasks = [
            try Fixtures.recurrentTask(id: "r-1", issue: nil,
                                       title: "Ad-hoc Slack Questions",
                                       series: "Ad-hoc Slack Questions - casanabria"),
            try Fixtures.recurrentTask(id: "r-2", issue: nil,
                                       title: "Ad-hoc Slack Questions - casanabria",
                                       series: "Ad-hoc Slack Questions - casanabria"),
        ]
        XCTAssertTrue(RecurrentSeries.isSupported(by: tasks))
        XCTAssertNil(RecurrentSeries.unsupportedReason(for: tasks))
        XCTAssertEqual(RecurrentSeries.displayName(for: tasks[0]),
                       "Ad-hoc Slack Questions - casanabria")
        XCTAssertEqual(RecurrentSeries.groups(in: tasks).count, 1)
    }

    /// A whitespace-only value is not a series name.
    func testBlankSeriesIsTreatedAsAbsent() throws {
        let task = try Fixtures.recurrentTask(id: "r-1", issue: nil, series: "   ")
        XCTAssertNil(RecurrentSeries.canonicalName(for: task))
    }
}

// MARK: - Fixtures

/// Synthetic tasks, built by decoding JSON so they go through the same
/// `init(from:)` the daemon's payload does.
enum Fixtures {
    static func recurrentTask(id: String,
                              issue: String?,
                              title: String = "A recurring thing",
                              bindings: Int = 0,
                              series: String? = nil,
                              loggedMins: Double = 0,
                              sprintsWithTime: [(String, Double)] = []) throws -> TrackerTask {
        var object: [String: Any] = [
            "id": id,
            "title": title,
            "status": "recurrent",
            "role_id": "other",
            "logged_mins": loggedMins,
            "sprint_issues": (0..<bindings).map { index in
                ["sprint_id": "sp-\(index)", "sprint": "Sprint \(100 + index)",
                 "issue": issue ?? "org/repo#\(index)", "state": "closed"] as [String: Any]
            },
            "sprints_with_time": sprintsWithTime.map { entry in
                ["sprint_id": entry.0, "sprint_title": entry.0, "total_mins": entry.1]
                    as [String: Any]
            },
        ]
        if let issue { object["current_issue"] = issue }
        if let series { object["recurrent_series"] = series }
        let data = try JSONSerialization.data(withJSONObject: object)
        return try JSONDecoder().decode(TrackerTask.self, from: data)
    }
}
