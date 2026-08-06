import SwiftUI

/// Maps a role's stored color *name* onto a **system** color (plan §11).
///
/// Two rules, both deliberate:
///
/// 1. **System colors, never raw hex.** Dark Mode, Increase Contrast and the
///    accessibility display settings are then handled by the OS rather than by
///    us guessing two sets of hex values.
/// 2. **`white` is not a color here.** Three of the owner's ten roles are
///    literally `"white"` in the data file (`other`, `testing`,
///    `iron infusion`) — the value `wt roles add` seeds, since there is no
///    `set-color` subcommand. Rendering three identical chips would make the
///    channel useless, so a role whose color is `white`, blank or unrecognised
///    gets a **stable distinct** color assigned from its index in the role list.
///
/// Stability matters more than prettiness: the same role must get the same color
/// on every launch, so the assignment is a pure function of `(color, index)`
/// with no hashing of mutable strings and no randomness.
///
/// Color is never the only channel. Every element `RolePalette` colors also
/// carries a text label — see `RoleChip`.
enum RolePalette {

    /// Colors reachable by name from the data file. These are the values
    /// actually present (`blue`, `green`, `yellow`, `red`, `cyan`, `magenta`)
    /// plus the rest of the Textual/ANSI-ish names `wt roles` could produce.
    private static let named: [String: Color] = [
        "blue": .blue,
        "green": .green,
        "yellow": .yellow,
        "red": .red,
        "cyan": .cyan,
        "magenta": .pink,      // No system "magenta"; pink is its nearest.
        "purple": .purple,
        "orange": .orange,
        "teal": .teal,
        "pink": .pink,
        "indigo": .indigo,
        "mint": .mint,
        "brown": .brown,
        "gray": .gray,
        "grey": .gray,
    ]

    /// Assigned to roles with no usable color, indexed by the role's position in
    /// `snapshot.roles`. Eight entries, so the owner's three `white` roles (at
    /// indices 3, 4 and 5) land on three different colors.
    ///
    /// Deliberately disjoint from the low-index named colors so a fallback role
    /// is unlikely to collide with a named neighbour.
    private static let fallback: [Color] = [
        .teal, .purple, .orange, .indigo, .mint, .brown, .pink, .gray,
    ]

    /// Names that mean "no color was chosen".
    private static let unspecified: Set<String> = ["white", "default", "none", ""]

    /// The color for a role at `index` in the snapshot's role list.
    static func color(named colorName: String?, index: Int) -> Color {
        let key = (colorName ?? "").trimmingCharacters(in: .whitespaces).lowercased()
        if !unspecified.contains(key), let color = named[key] { return color }
        // `index` can exceed the fallback count; wrap, and clamp negatives.
        return fallback[((index % fallback.count) + fallback.count) % fallback.count]
    }

    /// Convenience for a `Role` at a known index.
    static func color(for role: Role, index: Int) -> Color {
        color(named: role.color, index: index)
    }

    /// Looks a role id up in `roles` and returns its color, using the role's own
    /// position as the fallback index. Unknown ids get a neutral secondary
    /// color rather than borrowing some other role's identity.
    static func color(forRoleID roleID: String?, in roles: [Role]) -> Color {
        guard let roleID,
              let index = roles.firstIndex(where: { $0.id == roleID }) else {
            return .secondary
        }
        return color(for: roles[index], index: index)
    }

    /// The colors every role in `roles` resolves to, in order. Used by the
    /// stability test and handy for previews.
    static func colors(for roles: [Role]) -> [Color] {
        roles.enumerated().map { color(for: $1, index: $0) }
    }
}

// MARK: - Chip

/// A role's color and name together. The label is not optional — color alone is
/// never the only channel (plan §11, accessibility).
struct RoleChip: View {
    let label: String
    let color: Color
    var compact: Bool = false

    var body: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(color)
                .frame(width: 7, height: 7)
                .overlay(Circle().strokeBorder(.primary.opacity(0.15), lineWidth: 0.5))
            Text(label)
                .font(compact ? .caption2 : .caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Role: \(label)")
    }
}
