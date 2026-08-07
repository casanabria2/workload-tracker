import SwiftUI

/// The recurrent shelf: perpetual tasks, excluded from the Kanban columns and
/// shown in their own bottom pane (plan §9).
///
/// A native `Table` rather than cards, because these are a stable list you scan
/// rather than move. **Read-only in Phase 3** — no row actions, and in
/// particular no way to close a series, which is irreversible on GitHub.
struct RecurrentShelfView: View {
    let tasks: [TrackerTask]
    @Environment(Store.self) private var store

    // MARK: - Sizing

    /// The height this shelf actually needs, so the split view gives it that and
    /// no more.
    ///
    /// The pane used to take a fixed `idealHeight: 200` regardless of content,
    /// which left a large band of empty table under seven rows. These are the
    /// measured metrics of the parts above and inside the `Table`; a `Table`
    /// will not size itself to its rows, so the height has to be computed.
    ///
    /// Capped, because the shelf is the *secondary* pane: a long series list
    /// scrolls internally rather than crowding out the board.
    static func naturalHeight(rows: Int) -> CGFloat {
        let titleBar: CGFloat = 30      // icon + "Recurrent" + count, 6pt padding
        let divider: CGFloat = 1
        let tableHeader: CGFloat = 28
        let row: CGFloat = 28           // a row carries a RoleChip, so not the 24pt default
        let bottomInset: CGFloat = 8
        let content = CGFloat(max(rows, 1)) * row
        return min(titleBar + divider + tableHeader + content + bottomInset, maximumHeight)
    }

    /// Beyond this the shelf scrolls instead of growing.
    static let maximumHeight: CGFloat = 420

    /// What an empty shelf needs for its `ContentUnavailableView`.
    static let emptyHeight: CGFloat = 140

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 6) {
                Image(systemName: "arrow.trianglehead.2.clockwise.rotate.90")
                    .symbolRenderingMode(.hierarchical)
                Text("Recurrent").font(.headline)
                Text("\(tasks.count)")
                    .font(.caption.monospacedDigit())
                    .padding(.horizontal, 6).padding(.vertical, 1)
                    .background(.quaternary, in: .capsule)
                Spacer()
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            Divider()

            if tasks.isEmpty {
                ContentUnavailableView("No recurrent tasks", systemImage: "tray.2")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                Table(tasks) {
                    TableColumn("Title") { task in
                        Text(task.title).lineLimit(1)
                    }
                    TableColumn("Role") { task in
                        let color = RolePalette.color(forRoleID: task.roleId, in: store.roles)
                        RoleChip(label: roleLabel(task), color: color, compact: true)
                    }
                    .width(min: 90, ideal: 130)
                    TableColumn("This sprint") { task in
                        Text(Duration.formatZeroed(minutes: thisSprint(task)))
                            .font(.body.monospacedDigit())
                    }
                    .width(min: 80, ideal: 95)
                    TableColumn("Total") { task in
                        Text(Duration.formatZeroed(minutes: task.loggedMins))
                            .font(.body.monospacedDigit())
                    }
                    .width(min: 70, ideal: 85)
                    TableColumn("Sprints") { task in
                        Text("\(task.sprintIssues.count)")
                            .font(.body.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                    .width(min: 55, ideal: 65)
                    TableColumn("Current issue") { task in
                        Text(task.currentIssue ?? "—")
                            .font(.caption.monospaced())
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    .width(min: 140, ideal: 240)
                }
            }
        }
    }

    private func roleLabel(_ task: TrackerTask) -> String {
        guard let id = task.roleId else { return "no role" }
        return store.roles.first { $0.id == id }?.displayName ?? id
    }

    /// Minutes this perpetual task logged in the current sprint, from the
    /// timestamp-bucketed `sprints_with_time` — never a field lookup.
    private func thisSprint(_ task: TrackerTask) -> Double {
        guard let sprintID = store.currentSprint?.id else { return 0 }
        return task.minutes(inSprint: sprintID)
    }
}
