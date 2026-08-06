import SwiftUI

/// The `NavigationSplitView` sidebar: a **Views** section for navigation and a
/// **Roles** section listing each role with its logged time — the Calendar.app
/// pattern (plan §11).
///
/// In Phase 3 selecting a role scopes the board to that role. Phase 5 replaces
/// that with multi-select toggles writing the shared `FilterState`, at which
/// point the Roles rows stop being navigation and become a second view of the
/// filter. The section is laid out now so that change is additive.
struct SidebarView: View {
    @Environment(Store.self) private var store
    @Binding var selection: SidebarSelection?

    var body: some View {
        List(selection: $selection) {
            Section("Views") {
                Label("Board", systemImage: "rectangle.split.3x1")
                    .tag(SidebarSelection.board)
                Label("Timeline", systemImage: "chart.bar.xaxis")
                    .tag(SidebarSelection.timeline)
                Label("Overview", systemImage: "chart.pie")
                    .tag(SidebarSelection.overview)
            }

            Section("Roles") {
                ForEach(store.roleSummaries) { summary in
                    RoleRow(summary: summary)
                        .tag(SidebarSelection.role(summary.role.id))
                }
            }
        }
        .listStyle(.sidebar)
        .navigationSplitViewColumnWidth(min: 220, ideal: 250, max: 340)
        .safeAreaInset(edge: .bottom, spacing: 0) {
            SidebarFooter()
        }
    }
}

/// One Roles row: color chip, name, task count, logged time.
private struct RoleRow: View {
    let summary: RoleSummary

    var body: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(summary.color)
                .frame(width: 9, height: 9)
                .overlay(Circle().strokeBorder(.primary.opacity(0.15), lineWidth: 0.5))
            VStack(alignment: .leading, spacing: 1) {
                Text(summary.role.displayName)
                    .lineLimit(1)
                Text("\(summary.taskCount) task\(summary.taskCount == 1 ? "" : "s")")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
            Spacer(minLength: 8)
            Text(Duration.format(minutes: summary.loggedMins))
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(
            "\(summary.role.displayName), \(summary.taskCount) tasks, "
            + "\(Duration.format(minutes: summary.loggedMins)) logged")
    }
}

/// Mirrors the TUI's ACTIVE TIMER block. Read-only in Phase 3 — the start/stop
/// control lands with the write paths in Phase 4.
private struct SidebarFooter: View {
    @Environment(Store.self) private var store

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Divider()
            if let task = store.activeTimerTask, let elapsed = store.activeTimerElapsed {
                HStack(spacing: 6) {
                    Image(systemName: "record.circle")
                        .symbolRenderingMode(.hierarchical)
                        .foregroundStyle(.red)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(task.title).font(.caption).lineLimit(1)
                        Text(Duration.formatElapsed(elapsed))
                            .font(.caption.monospacedDigit().weight(.semibold))
                    }
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("Timer running on \(task.title), "
                                    + "\(Duration.formatElapsed(elapsed)) elapsed")
            } else {
                Label("No timer running", systemImage: "pause.circle")
                    .symbolRenderingMode(.hierarchical)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            if let sprint = store.currentSprint {
                Text("\(sprint.displayName) · "
                     + Duration.formatZeroed(minutes: store.currentSprintMinutes))
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
