import SwiftUI
import XCTest
@testable import WorkloadClient

/// The collision audit, as a test rather than as a promise.
///
/// A shadowed keyboard shortcut is the quietest bug this app can ship: nothing
/// warns, nothing throws, the older binding simply stops firing and the only
/// symptom is a user pressing a key that used to work. Phase 8 added nine
/// shortcuts to a codebase that already had eleven, so the audit had to become
/// something that fails on its own.
final class AppShortcutTests: XCTestCase {

    /// The property the whole table exists for.
    func testNoTwoShortcutsCollide() {
        var seen: [String: AppShortcut] = [:]
        for shortcut in AppShortcut.allCases {
            if let other = seen[shortcut.signature] {
                XCTFail("\(shortcut.rawValue) collides with \(other.rawValue) "
                        + "on \(shortcut.display)")
            }
            seen[shortcut.signature] = shortcut
        }
        XCTAssertEqual(seen.count, AppShortcut.allCases.count)
    }

    /// The specific collision the plan warns about by name: the Board's card
    /// move and the Timeline's timeframe navigation both want the arrow keys,
    /// and a menu-bar shortcut beats a view's `onKeyPress`. They are kept apart
    /// by the modifier, not by luck.
    func testTimelineNavigationDoesNotShadowTheBoardCardMove() {
        XCTAssertEqual(AppShortcut.moveCardLeft.modifiers, .command)
        XCTAssertEqual(AppShortcut.moveCardRight.modifiers, .command)
        XCTAssertEqual(AppShortcut.previousPeriod.modifiers, .option)
        XCTAssertEqual(AppShortcut.nextPeriod.modifiers, .option)
        XCTAssertNotEqual(AppShortcut.moveCardLeft.signature,
                          AppShortcut.previousPeriod.signature)
        XCTAssertNotEqual(AppShortcut.moveCardRight.signature,
                          AppShortcut.nextPeriod.signature)
    }

    /// The Board's two are in the table but must never be registered by a menu,
    /// because a menu item's shortcut fires regardless of what has focus.
    func testBoardKeyHandlerEntriesAreNotMenuItems() {
        XCTAssertEqual(Set(AppShortcut.table(for: .boardKeyHandler)),
                       [.moveCardLeft, .moveCardRight])
        for shortcut in AppShortcut.table(for: .boardKeyHandler) {
            XCTAssertEqual(shortcut.owner, .boardKeyHandler)
        }
    }

    /// `⌘R` refreshes; `⌥⌘R` toggles the shelf. One character, two bindings,
    /// and the only thing keeping them apart is the modifier set.
    func testRefreshAndShelfShareAKeyButNotAShortcut() {
        XCTAssertEqual(AppShortcut.refresh.key.character,
                       AppShortcut.toggleShelf.key.character)
        XCTAssertNotEqual(AppShortcut.refresh.signature,
                          AppShortcut.toggleShelf.signature)
        XCTAssertEqual(AppShortcut.refresh.display, "⌘R")
        XCTAssertEqual(AppShortcut.toggleShelf.display, "⌥⌘R")
    }

    /// Same shape for `⌘T` (timer) and `⌥⌘T` (timeline Today).
    func testTimerAndTodayShareAKeyButNotAShortcut() {
        XCTAssertNotEqual(AppShortcut.toggleTimer.signature, AppShortcut.today.signature)
        XCTAssertEqual(AppShortcut.toggleTimer.display, "⌘T")
        XCTAssertEqual(AppShortcut.today.display, "⌥⌘T")
    }

    /// The rendered table, so a typo in a modifier set shows up as a diff
    /// rather than as a silent behaviour change.
    func testTheTableRendersAsDocumented() {
        let rendered = Dictionary(uniqueKeysWithValues:
            AppShortcut.allCases.map { ($0.rawValue, $0.display) })
        XCTAssertEqual(rendered, [
            "showBoard": "⌘1",
            "showTimeline": "⌘2",
            "showOverview": "⌘3",
            "refresh": "⌘R",
            "clearFilters": "⇧⌘K",
            "findFilter": "⌘F",
            "toggleInspector": "⌥⌘I",
            "toggleShelf": "⌥⌘R",
            "zoomIn": "⌘+",
            "zoomOut": "⌘-",
            "previousPeriod": "⌥←",
            "nextPeriod": "⌥→",
            "today": "⌥⌘T",
            "toggleTimer": "⌘T",
            "logTime": "⌘L",
            "openIssue": "⌘G",
            "syncSprints": "⇧⌘S",
            "markDone": "⇧⌘D",
            "moveCardLeft": "⌘←",
            "moveCardRight": "⌘→",
        ])
    }

    /// No entry may be nameless: `ShortcutHelpView` renders the table verbatim,
    /// so a blank title would ship as a blank row in Help.
    func testEveryShortcutHasATitle() {
        for shortcut in AppShortcut.allCases {
            XCTAssertFalse(shortcut.title.trimmingCharacters(in: .whitespaces).isEmpty,
                           "\(shortcut.rawValue) has no title")
            XCTAssertFalse(shortcut.display.isEmpty)
        }
    }

    /// Every menu owner is represented, so an item cannot be added to a menu
    /// that the Help sheet does not print.
    func testOwnersPartitionTheTable() {
        let owners: [AppShortcut.Owner] = [.viewMenu, .taskMenu, .boardKeyHandler]
        let counted = owners.reduce(0) { $0 + AppShortcut.table(for: $1).count }
        XCTAssertEqual(counted, AppShortcut.allCases.count)
    }
}
