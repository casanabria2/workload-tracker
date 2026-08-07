import SwiftUI

/// The `NavigationSplitView` sidebar: a **Views** section for navigation and a
/// **Roles** section listing each role with its logged time — the Calendar.app
/// pattern (plan §11).
///
/// Phase 5 made the Roles rows **multi-select filter toggles, not navigation**
/// (§8.4). Each row writes `Store.filter.roles` through `Store.toggle`, which is
/// the very same call the toolbar's Filter menu makes — one state, two views of
/// it. Checking a role here makes its token appear in the search field;
/// deleting that token unchecks the row.
///
/// Role gets this privileged always-visible position because it is the facet
/// with per-role time totals worth seeing even when you are not filtering.
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

            Section {
                ForEach(store.roleSummaries) { summary in
                    RoleRow(summary: summary,
                            isOn: store.isSelected(summary.role.id, in: .role)) {
                        store.toggle(summary.role.id, in: .role)
                    }
                }
            } header: {
                HStack {
                    Text("Roles")
                    Spacer()
                    if !store.filter.roles.isEmpty {
                        Button("Clear") { store.clear(.role) }
                            .buttonStyle(.plain)
                            .font(.caption)
                            .foregroundStyle(Color.accentColor)
                    }
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

/// One Roles row: a checkbox over color chip, name, task count and logged time.
///
/// A role with no tasks stays listed — the sidebar is a directory of what exists
/// — but is not togglable, because selecting it could only ever produce an empty
/// board. Same reasoning as the facet self-hide rule in §8.3.
private struct RoleRow: View {
    let summary: RoleSummary
    let isOn: Bool
    let toggle: () -> Void

    var body: some View {
        Toggle(isOn: Binding(get: { isOn }, set: { _ in toggle() })) {
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
        }
        .toggleStyle(.checkbox)
        .disabled(summary.taskCount == 0)
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
