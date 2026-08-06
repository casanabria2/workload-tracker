import XCTest
import SwiftUI
@testable import WorkloadClient

/// `RolePalette` must be **stable** (same role, same color, every launch) and
/// must not collapse the three `white` roles onto one chip.
final class RolePaletteTests: XCTestCase {

    /// The owner's real role list, colors only — no labels, so nothing private
    /// is committed. Three `white`, two `blue`.
    private let liveColors: [String?] = [
        "blue", "green", "yellow", "white", "white", "white",
        "red", "cyan", "blue", "magenta",
    ]

    private func liveRoles() -> [Role] {
        liveColors.enumerated().map { index, color in
            Role(id: "role-\(index)", label: "Role \(index)", color: color)
        }
    }

    func testNamedColorsMapToSystemColors() {
        XCTAssertEqual(RolePalette.color(named: "blue", index: 0), .blue)
        XCTAssertEqual(RolePalette.color(named: "green", index: 1), .green)
        XCTAssertEqual(RolePalette.color(named: "yellow", index: 2), .yellow)
        XCTAssertEqual(RolePalette.color(named: "red", index: 6), .red)
        XCTAssertEqual(RolePalette.color(named: "cyan", index: 7), .cyan)
        // No system "magenta"; pink is the documented nearest.
        XCTAssertEqual(RolePalette.color(named: "magenta", index: 9), .pink)
    }

    func testColorNameIsCaseAndWhitespaceInsensitive() {
        XCTAssertEqual(RolePalette.color(named: "  BLUE ", index: 0), .blue)
    }

    /// The headline requirement: three `white` roles must not render as three
    /// identical chips.
    func testWhiteRolesGetDistinctColors() {
        let colors = RolePalette.colors(for: liveRoles())
        let whiteIndices = liveColors.enumerated()
            .filter { $0.element == "white" }.map(\.offset)
        XCTAssertEqual(whiteIndices, [3, 4, 5])

        let whiteColors = whiteIndices.map { colors[$0] }
        XCTAssertEqual(Set(whiteColors).count, 3,
                       "the three white roles collapsed onto \(Set(whiteColors).count) color(s)")
        // And none of them is literally white.
        XCTAssertFalse(whiteColors.contains(.white))
    }

    /// Same input, same output — no hashing of mutable state, no randomness.
    func testAssignmentIsStableAcrossCalls() {
        let first = RolePalette.colors(for: liveRoles())
        let second = RolePalette.colors(for: liveRoles())
        let third = RolePalette.colors(for: liveRoles())
        XCTAssertEqual(first, second)
        XCTAssertEqual(second, third)
    }

    /// A role keeps its color when a role is appended after it — only insertion
    /// *before* it would shift the index, which is a rename-level event.
    func testAppendingARoleDoesNotRecolorTheExistingOnes() {
        let before = RolePalette.colors(for: liveRoles())
        var roles = liveRoles()
        roles.append(Role(id: "role-new", label: "New", color: "white"))
        let after = RolePalette.colors(for: roles)
        XCTAssertEqual(Array(after.prefix(before.count)), before)
    }

    func testBlankAndUnknownColorsFallBackRatherThanFailing() {
        // `default`, `none`, empty and nil all mean "unspecified".
        for name in ["white", "default", "none", "", nil] as [String?] {
            let color = RolePalette.color(named: name, index: 3)
            XCTAssertEqual(color, RolePalette.color(named: "white", index: 3),
                           "\(name ?? "nil") should use the same fallback slot")
        }
        // An unrecognised name also falls back, deterministically.
        XCTAssertEqual(RolePalette.color(named: "chartreuse", index: 4),
                       RolePalette.color(named: "white", index: 4))
    }

    func testFallbackWrapsForLargeIndicesWithoutCrashing() {
        let wide = (0..<40).map { Role(id: "r\($0)", label: nil, color: "white") }
        let colors = RolePalette.colors(for: wide)
        XCTAssertEqual(colors.count, 40)
        // Eight fallback slots, so the cycle repeats every 8.
        XCTAssertEqual(colors[0], colors[8])
        XCTAssertEqual(colors[3], colors[11])
        XCTAssertEqual(Set(colors).count, 8)
    }

    func testLookupByRoleID() {
        let roles = liveRoles()
        XCTAssertEqual(RolePalette.color(forRoleID: "role-1", in: roles), .green)
        XCTAssertEqual(RolePalette.color(forRoleID: "role-3", in: roles),
                       RolePalette.color(named: "white", index: 3))
        // An unknown id must not borrow another role's identity.
        XCTAssertEqual(RolePalette.color(forRoleID: "nope", in: roles), .secondary)
        XCTAssertEqual(RolePalette.color(forRoleID: nil, in: roles), .secondary)
    }

    /// Two roles legitimately share `blue` in the real data. That is accepted —
    /// the chip always carries a text label, so color is never the only channel
    /// — but it should be a conscious fact, not a surprise.
    func testDuplicateNamedColorsAreAllowedAndLabelled() {
        let colors = RolePalette.colors(for: liveRoles())
        XCTAssertEqual(colors[0], .blue)
        XCTAssertEqual(colors[8], .blue)
    }
}
