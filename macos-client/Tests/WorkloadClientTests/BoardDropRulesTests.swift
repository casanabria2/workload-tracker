import XCTest
import UniformTypeIdentifiers
@testable import WorkloadClient

/// Plan §7's drop table, exercised as a pure function.
///
/// This is the safety-critical logic of Phase 4 — one of the nine cells runs
/// `gh issue create` and `gh issue close` — so it is deliberately reachable
/// without a window, a drag session, or a daemon. If proving "a Done drop needs
/// confirmation" required driving a UI, it would not get proven.
final class BoardDropRulesTests: XCTestCase {

    // MARK: - The whole table, cell by cell

    /// Every (source, target) pair over the three board columns, written out
    /// rather than generated, so the table in the test reads like the table in
    /// the plan and a change to one cell is visible in review.
    func testTheCompleteDropTable() {
        let expected: [(TaskStatus, TaskStatus, BoardDropDecision)] = [
            // from To Do
            (.todo, .todo, .rejected(.sameColumn)),
            (.todo, .inProgress, .optimisticStatus(.inProgress)),
            (.todo, .done, .confirmClose),
            // from In Progress
            (.inProgress, .todo, .optimisticStatus(.todo)),
            (.inProgress, .inProgress, .rejected(.sameColumn)),
            (.inProgress, .done, .confirmClose),
            // from Done — no reopen path exists in wt.py
            (.done, .todo, .rejected(.reopenNotSupported)),
            (.done, .inProgress, .rejected(.reopenNotSupported)),
            (.done, .done, .rejected(.sameColumn)),
        ]
        for (source, target, decision) in expected {
            XCTAssertEqual(BoardDropRules.decide(from: source, to: target), decision,
                           "\(source.rawValue) → \(target.rawValue)")
        }
    }

    /// Only one cell in the table may reach the close workflow, and it reaches
    /// it as a *confirmation*, never as a request.
    func testExactlyTwoCellsLeadToTheCloseSheetAndNoneToASilentClose() {
        let columns = TaskStatus.boardColumns
        let closing = columns.flatMap { source in
            columns.compactMap { target -> String? in
                BoardDropRules.decide(from: source, to: target) == .confirmClose
                    ? "\(source.rawValue)→\(target.rawValue)" : nil
            }
        }
        XCTAssertEqual(Set(closing), ["todo→done", "inprogress→done"])

        // And nothing anywhere produces an optimistic `.done`, which would be a
        // silent close: `DaemonClient.setStatus` refuses `done`, but the rule
        // table must not even ask.
        for source in columns + [.recurrent, .unknown("blocked")] {
            for target in columns + [.recurrent, .unknown("blocked")] {
                XCTAssertNotEqual(BoardDropRules.decide(from: source, to: target),
                                  .optimisticStatus(.done),
                                  "\(source.rawValue) → \(target.rawValue)")
            }
        }
    }

    // MARK: - Recurrent

    /// Closing a recurrent task ends the series and closes its live issue —
    /// CLAUDE.md warns about it explicitly. So a recurrent card is inert in both
    /// directions, including onto Done.
    func testRecurrentIsRejectedInEveryDirection() {
        for target in TaskStatus.boardColumns + [.recurrent] {
            XCTAssertEqual(BoardDropRules.decide(from: .recurrent, to: target),
                           .rejected(.recurrentLocked),
                           "recurrent → \(target.rawValue)")
        }
        for source in TaskStatus.boardColumns {
            XCTAssertEqual(BoardDropRules.decide(from: source, to: .recurrent),
                           .rejected(.recurrentLocked),
                           "\(source.rawValue) → recurrent")
        }
    }

    /// The prohibition beats the no-op: recurrent → recurrent must explain
    /// itself rather than shrug "already there".
    func testRecurrentBeatsSameColumn() {
        XCTAssertEqual(BoardDropRules.decide(from: .recurrent, to: .recurrent),
                       .rejected(.recurrentLocked))
    }

    func testRecurrentCardsAreNotEvenDraggable() {
        XCTAssertFalse(BoardDropRules.isDraggable(.recurrent))
        for status in TaskStatus.boardColumns {
            XCTAssertTrue(BoardDropRules.isDraggable(status))
        }
    }

    // MARK: - Unknown statuses

    /// The fixture carries a `blocked` task precisely because a status added on
    /// the Python side has happened before (`recurrent`). It must be inert, not
    /// coerced into a column.
    func testUnknownStatusIsRejectedRatherThanGuessed() {
        XCTAssertEqual(BoardDropRules.decide(from: .unknown("blocked"), to: .inProgress),
                       .rejected(.unknownStatus("blocked")))
        XCTAssertEqual(BoardDropRules.decide(from: .todo, to: .unknown("blocked")),
                       .rejected(.unknownStatus("blocked")))
        XCTAssertEqual(BoardDropRules.decide(from: .unknown("blocked"), to: .done),
                       .rejected(.unknownStatus("blocked")),
                       "an unknown source must not be closable")
    }

    // MARK: - Payload round trip

    func testPayloadCarriesTheSourceStatusThroughACodableRoundTrip() throws {
        for status in [TaskStatus.todo, .inProgress, .done, .recurrent, .unknown("blocked")] {
            let payload = TaskDragPayload(taskId: "t-1", sourceStatus: status)
            let decoded = try JSONDecoder().decode(
                TaskDragPayload.self, from: JSONEncoder().encode(payload))
            XCTAssertEqual(decoded, payload)
            XCTAssertEqual(decoded.status, status,
                           "an unknown status must survive the drag, not collapse to todo")
        }
    }

    func testAcceptsMirrorsDecide() {
        let payload = TaskDragPayload(taskId: "t-1", sourceStatus: .done)
        XCTAssertFalse(BoardDropRules.accepts(payload, in: .todo))
        XCTAssertFalse(BoardDropRules.accepts(payload, in: .done))

        let open = TaskDragPayload(taskId: "t-2", sourceStatus: .todo)
        XCTAssertTrue(BoardDropRules.accepts(open, in: .inProgress))
        XCTAssertTrue(BoardDropRules.accepts(open, in: .done),
                      "Done accepts the card — it is the sheet that gates it")
    }

    // MARK: - Copy

    /// Two refusals that look alike on screen mean different things: one is a
    /// missing feature, one is a deliberate prohibition. Collapsing them would
    /// make the recurrent warning read like a bug report.
    func testRejectionsExplainThemselvesDistinctly() {
        let reopen = BoardDropRejection.reopenNotSupported
        let recurrent = BoardDropRejection.recurrentLocked
        XCTAssertNotEqual(reopen.message, recurrent.message)
        XCTAssertNotEqual(reopen.hint, recurrent.hint)
        XCTAssertTrue(reopen.hint?.contains("gh issue reopen") == true, reopen.hint ?? "")
        XCTAssertTrue(recurrent.hint?.contains("series") == true, recurrent.hint ?? "")
        XCTAssertNil(BoardDropRejection.sameColumn.hint)
    }
}

// MARK: - Drag type registration

/// The drag type must stay system-registered.
///
/// This is a change-detector, deliberately, and **the reason it exists has
/// changed**. It used to guard against an impossibility: an `exportedAs:` type
/// could not work at all, because there was no Info.plist to declare it in.
/// Phase 9 (plan §12) added one, so the type is now genuinely declared and
/// registered with LaunchServices — the switch is possible.
///
/// What this now guards is a **verification gap, not a defect**. A SwiftUI drag
/// cannot be exercised from a test: synthesised mouse drags do not start one,
/// which is how the original bug shipped. So flipping `contentType` swaps a
/// hand-verified working drag for one nothing has checked, and it must be a
/// deliberate act followed by a manual drag in the bundled app — not a passing
/// test suite. `isDeclared` is still asserted, and is still worth nothing on its
/// own: it answered `true` throughout the era when the drag was broken.
///
/// See the note at the top of `BoardDrop.swift` and the "custom drag type"
/// section of `macos-client/README.md`.
final class DragTypeRegistrationTests: XCTestCase {
    func testPayloadUsesASystemRegisteredType() {
        XCTAssertEqual(TaskDragPayload.contentType, .json,
                       "Read the note at the top of BoardDrop.swift before changing this: "
                       + "the custom exportedAs: type is declared in Info.plist now, so it "
                       + "*can* work — but only a manual drag in the bundled .app can show "
                       + "that it does, and no test here can.")
        XCTAssertTrue(TaskDragPayload.contentType.isDeclared)
    }

    func testPayloadRoundTripsThroughItsRepresentation() async throws {
        let original = TaskDragPayload(taskId: "abc123", sourceStatus: .todo)
        let data = try JSONEncoder().encode(original)
        let back = try JSONDecoder().decode(TaskDragPayload.self, from: data)
        XCTAssertEqual(back, original)
        XCTAssertEqual(back.status, .todo)
    }
}
