import SwiftUI

/// The read-only Kanban board: three columns (To Do / In Progress / Done) with
/// the recurrent tasks in a separate collapsible bottom pane.
///
/// **Phase 3 renders only.** No drag and drop, no filter bar, no mutations —
/// those are Phases 4 and 5. The `VSplitView` and the column structure are laid
/// out now so those phases are additive rather than a rewrite.
struct BoardView: View {
    @Environment(Store.self) private var store
    /// Non-nil when the sidebar selected a single role, which scopes the board.
    let roleFilter: String?

    private var visibleTasks: [TrackerTask] {
        guard let roleFilter else { return store.tasks }
        return store.tasks.filter { $0.roleId == roleFilter }
    }

    private func column(_ status: TaskStatus) -> [TrackerTask] {
        let scoped = store.boardTasks(status)
        guard let roleFilter else { return scoped }
        return scoped.filter { $0.roleId == roleFilter }
    }

    private var recurrent: [TrackerTask] {
        guard let roleFilter else { return store.recurrentTasks }
        return store.recurrentTasks.filter { $0.roleId == roleFilter }
    }

    var body: some View {
        @Bindable var store = store
        VSplitView {
            HStack(alignment: .top, spacing: 0) {
                ForEach(Array(TaskStatus.boardColumns.enumerated()), id: \.offset) { index, status in
                    BoardColumn(status: status, tasks: column(status))
                    if index < TaskStatus.boardColumns.count - 1 {
                        Divider()
                    }
                }
            }
            .frame(minHeight: 260)

            if store.showsRecurrentShelf {
                RecurrentShelfView(tasks: recurrent)
                    .frame(minHeight: 120, idealHeight: 200)
            }
        }
        .toolbar {
            ToolbarItem(placement: .status) {
                BoardStatusLabel(taskCount: visibleTasks.count, roleFilter: roleFilter)
            }
            ToolbarItem {
                Toggle(isOn: $store.showsRecurrentShelf) {
                    Label("Recurrent Shelf", systemImage: "tray.2")
                }
                .help("Show or hide the recurrent task shelf")
                .keyboardShortcut("r", modifiers: [.command, .option])
            }
        }
    }
}

/// The count line in the toolbar, so the board's scope is never implicit.
private struct BoardStatusLabel: View {
    @Environment(Store.self) private var store
    let taskCount: Int
    let roleFilter: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(scopeText).font(.caption)
            if let updated = store.lastUpdated {
                Text("updated \(updated, style: .relative) ago")
                    .font(.caption2).foregroundStyle(.tertiary)
            }
        }
    }

    private var scopeText: String {
        let role = roleFilter.flatMap { id in
            store.roles.first { $0.id == id }?.displayName
        }
        let sprint = store.currentSprint?.displayName ?? "no current sprint"
        if let role { return "\(role) · \(taskCount) tasks · \(sprint)" }
        return "\(taskCount) tasks · \(sprint)"
    }
}

/// One Kanban column.
struct BoardColumn: View {
    let status: TaskStatus
    let tasks: [TrackerTask]

    @Environment(Store.self) private var store

    private var totalMins: Double { tasks.reduce(0) { $0 + $1.reportableMins } }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            if tasks.isEmpty {
                ContentUnavailableView("Nothing in \(status.displayName)",
                                       systemImage: "tray")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView {
                    LazyVStack(spacing: 8) {
                        ForEach(tasks) { task in
                            TaskCardView(
                                task: task,
                                roleLabel: roleLabel(task),
                                roleColor: RolePalette.color(forRoleID: task.roleId,
                                                             in: store.roles),
                                elapsed: elapsed(for: task),
                                currentSprint: store.currentSprint
                            )
                        }
                    }
                    .padding(10)
                }
            }
        }
        .frame(minWidth: 260, maxWidth: .infinity, maxHeight: .infinity)
    }

    private var header: some View {
        HStack {
            Text(status.displayName).font(.headline)
            Text("\(tasks.count)")
                .font(.caption.monospacedDigit())
                .padding(.horizontal, 6).padding(.vertical, 1)
                .background(.quaternary, in: .capsule)
            Spacer()
            Text(Duration.formatZeroed(minutes: totalMins))
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(status.displayName), \(tasks.count) tasks, "
                            + "\(Duration.formatZeroed(minutes: totalMins))")
    }

    private func roleLabel(_ task: TrackerTask) -> String {
        guard let id = task.roleId else { return "no role" }
        return store.roles.first { $0.id == id }?.displayName ?? id
    }

    private func elapsed(for task: TrackerTask) -> TimeInterval? {
        guard store.snapshot?.activeTimer?.taskId == task.id else { return nil }
        return store.activeTimerElapsed
    }
}
