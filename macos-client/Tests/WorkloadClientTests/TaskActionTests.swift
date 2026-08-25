import XCTest
@testable import WorkloadClient

/// `TaskAction` — the one table behind the menu bar, both context menus and the
/// inspector's button row.
///
/// The safety properties `ShelfActionTests` established for the shelf have to
/// survive the widening to board cards, and one new property arrives with it:
/// the two `close_task` routes are not interchangeable. Ending a recurrent
/// series stops a recurrence and closes a live issue with no reopen path;
/// closing a board task does not. Offering either one on the wrong kind of task
/// would put the strongest gate in the app in front of the wrong decision — or,
/// worse, put the weaker gate in front of the stronger consequence.
final class TaskActionTests: XCTestCase {

    // MARK: - Fixtures

    private func loadTasks() throws -> [TrackerTask] {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: "snapshot", withExtension: "json",
                              subdirectory: "Fixtures"))
        return try JSONDecoder().decode(Snapshot.self, from: Data(contentsOf: url)).tasks
    }

    private func task(status: TaskStatus) throws -> TrackerTask {
        let tasks = try loadTasks()
        return try XCTUnwrap(tasks.first { $0.status == status },
                             "the fixture has no \(status.rawValue) task")
    }

    // MARK: - Park

    /// Park is offered on ordinary board work, in both directions, with the
    /// title flipping rather than a second item appearing — the same
    /// one-item-two-states shape `startTimer` uses.
    func testParkIsOfferedOnBoardWorkAndFlipsItsTitle() throws {
        XCTAssertTrue(TaskAction.boardMenu.contains(.togglePark))
        let todo = try task(status: .todo)
        let parked = try task(status: .parked)
        XCTAssertEqual(TaskAction.togglePark.title(for: todo), "Park")
        XCTAssertEqual(TaskAction.togglePark.title(for: parked), "Unpark")
        XCTAssertTrue(TaskAction.togglePark
            .availability(for: todo, isTimerRunning: false).isAvailable)
        XCTAssertTrue(TaskAction.togglePark
            .availability(for: parked, isTimerRunning: false).isAvailable)
    }

    /// Neither a finished task nor a perpetual series can be parked, and the
    /// recurrent refusal is the load-bearing one — see `BoardDropRulesTests`.
    func testParkIsUnavailableForDoneAndRecurrentTasks() throws {
        for status in [TaskStatus.done, .recurrent] {
            let subject = try task(status: status)
            XCTAssertFalse(TaskAction.togglePark
                .availability(for: subject, isTimerRunning: false).isAvailable,
                           status.rawValue)
        }
        // A recurrent task is never even offered the item.
        XCTAssertFalse(TaskAction.recurrentMenu.contains(.togglePark))
    }

    /// Park is safe enough for a shortcut — it writes one field and the same
    /// item undoes it — unlike `endSeries`, which still has none.
    func testParkHasAShortcutAndEndSeriesStillDoesNot() {
        XCTAssertEqual(TaskAction.togglePark.shortcut, .togglePark)
        XCTAssertNil(TaskAction.shelf(.endSeries).shortcut)
        XCTAssertFalse(TaskAction.togglePark.isDestructive)
    }

    // MARK: - Which menu

    func testRecurrentTasksGetTheShelfMenuUnchanged() throws {
        let menu = TaskAction.menu(for: try task(status: .recurrent))
        XCTAssertEqual(menu, ShelfAction.menu.map(TaskAction.shelf))
    }

    func testBoardTasksAreNeverOfferedEndSeries() throws {
        for status in TaskStatus.boardColumns {
            let tasks = try loadTasks().filter { $0.status == status }
            for task in tasks {
                XCTAssertFalse(TaskAction.menu(for: task).contains(.shelf(.endSeries)),
                               "\(status.rawValue) offered End Series")
            }
        }
    }

    func testRecurrentTasksAreNeverOfferedMarkDone() throws {
        XCTAssertFalse(TaskAction.menu(for: try task(status: .recurrent)).contains(.markDone))
    }

    /// The menu is chosen by the task's own status, not by the view that asked,
    /// so a recurrent task selected on the *board* still cannot be "marked
    /// done" past the typed confirmation.
    func testTheMenuFollowsTheTaskNotTheSurface() throws {
        let recurrent = try task(status: .recurrent)
        XCTAssertEqual(TaskAction.menu(for: recurrent), TaskAction.recurrentMenu)
        XCTAssertNotEqual(TaskAction.menu(for: recurrent), TaskAction.boardMenu)
    }

    // MARK: - Ordering and danger

    func testTheDangerousItemIsLastAndSeparatedOnBothMenus() throws {
        let recurrentLast = try XCTUnwrap(TaskAction.recurrentMenu.last)
        XCTAssertEqual(recurrentLast, .shelf(.endSeries))
        XCTAssertTrue(recurrentLast.isSeparatedInMenu)
        XCTAssertTrue(recurrentLast.isDestructive)

        let boardLast = try XCTUnwrap(TaskAction.boardMenu.last)
        XCTAssertEqual(boardLast, .markDone)
        XCTAssertTrue(boardLast.isSeparatedInMenu)
    }

    func testNeitherMenuOpensOnADangerousItem() {
        XCTAssertNotEqual(TaskAction.recurrentMenu.first, .shelf(.endSeries))
        XCTAssertNotEqual(TaskAction.boardMenu.first, .markDone)
    }

    /// The exclusion `ShelfAction` asserts, restated at the surface that
    /// actually attaches shortcuts.
    func testEndSeriesHasNoKeyboardShortcut() {
        XCTAssertNil(TaskAction.shelf(.endSeries).shortcut)
    }

    /// Every other item has one, and every one of them is in the collision
    /// table — a shortcut invented here would bypass `AppShortcutTests`.
    func testEveryOtherActionsShortcutIsInTheTable() throws {
        for action in TaskAction.recurrentMenu + TaskAction.boardMenu
        where action != .shelf(.endSeries) {
            let shortcut = try XCTUnwrap(action.shortcut, "\(action.id) has no shortcut")
            XCTAssertTrue(AppShortcut.allCases.contains(shortcut))
            XCTAssertEqual(shortcut.owner, .taskMenu)
        }
    }

    /// Both menus draw from the same five slots; nothing is invented per
    /// surface.
    func testTheTwoMenusOverlapOnTheFirstFour() {
        XCTAssertEqual(Array(TaskAction.recurrentMenu.prefix(4)),
                       Array(TaskAction.boardMenu.prefix(4)))
    }

    // MARK: - Availability

    func testMarkDoneIsUnavailableOnAnAlreadyDoneTask() throws {
        let done = try task(status: .done)
        XCTAssertFalse(TaskAction.markDone
            .availability(for: done, isTimerRunning: false).isAvailable)
    }

    func testMarkDoneIsAvailableOnAnOpenTask() throws {
        let open = try task(status: .inProgress)
        XCTAssertTrue(TaskAction.markDone
            .availability(for: open, isTimerRunning: false).isAvailable)
    }

    /// Availability is delegated, not reimplemented: the shelf's rules still
    /// apply to a board card.
    func testShelfAvailabilityRulesStillApply() throws {
        let open = try task(status: .inProgress)
        XCTAssertEqual(TaskAction.shelf(.openIssue)
            .availability(for: open, isTimerRunning: false).isAvailable,
                       ShelfAction.openIssue
            .availability(for: open, isTimerRunning: false).isAvailable)
        XCTAssertFalse(TaskAction.shelf(.startTimer)
            .availability(for: open, isTimerRunning: true).isAvailable)
    }
}
