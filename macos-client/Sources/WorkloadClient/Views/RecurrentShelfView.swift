import SwiftUI

/// The recurrent shelf: perpetual tasks, excluded from the Kanban columns and
/// shown in their own bottom pane (plan §9).
///
/// A native `Table` rather than cards, because these are a stable list you scan
/// rather than move.
///
/// **Phase 6 adds row actions, via a context menu and the Task menu — not via
/// inline controls.** That is a deliberate constraint as much as a style
/// choice: `naturalHeight(rows:)` computes the pane's height from a fixed
/// per-row figure, and a row that grew a button row would silently break the
/// sizing the previous phase established. A context menu occupies no space.
///
/// Three of the five actions write, and two of them (`Sync Sprints`,
/// `End Series`) can call `gh` irreversibly. Neither is reachable from this
/// view without passing through `Store.perform(_:on:)` and the sheet it opens —
/// the rules live in `ShelfAction`, not here.
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
    /// Still valid in Phase 6: row actions live in a context menu, so a row's
    /// content is unchanged and `row` is still 28pt.
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
        @Bindable var store = store
        VStack(alignment: .leading, spacing: 0) {
            titleBar
            Divider()

            if tasks.isEmpty {
                ContentUnavailableView("No recurrent tasks", systemImage: "tray.2")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                Table(tasks, selection: $store.shelfSelection) {
                    TableColumn("Title") { task in
                        Text(task.title).lineLimit(1)
                    }
                    TableColumn("Role") { task in
                        let color = RolePalette.color(forRoleID: task.roleId, in: store.roles)
                        RoleChip(label: roleLabel(task), color: color, compact: true)
                    }
                    .width(min: 90, ideal: 130)
                    TableColumn(sprintColumnTitle) { task in
                        Text(Duration.formatZeroed(minutes: thisSprint(task)))
                            .font(.body.monospacedDigit())
                    }
                    .width(min: 80, ideal: 105)
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
                    .width(min: 140, ideal: 220)
                    TableColumn("Series") { task in
                        Text(RecurrentSeries.displayName(for: task))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .help(seriesHelp)
                    }
                    .width(min: 80, ideal: 150)
                }
                .contextMenu(forSelectionType: TrackerTask.ID.self) { ids in
                    // `forSelectionType:` gives the right-clicked row even when
                    // it is not the selected one, which a plain `.contextMenu`
                    // on a `Table` does not.
                    if let id = ids.first, let task = tasks.first(where: { $0.id == id }) {
                        TaskActionMenu(task: task)
                    }
                }
            }
        }
    }

    // MARK: - Chrome

    private var titleBar: some View {
        HStack(spacing: 6) {
            Image(systemName: "arrow.trianglehead.2.clockwise.rotate.90")
                .symbolRenderingMode(.hierarchical)
            Text("Recurrent").font(.headline)
            Text("\(tasks.count)")
                .font(.caption.monospacedDigit())
                .padding(.horizontal, 6).padding(.vertical, 1)
                .background(.quaternary, in: .capsule)
            if store.isFiltering {
                // The shelf's row set ignores the Sprint facet (plan §9), so the
                // count here can exceed what the board is showing. Saying so
                // beats letting the two look inconsistent.
                Text(sprintExemptionNote)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
            Spacer()
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
    }

    /// The "This sprint" header names the sprint it is actually totalling, which
    /// is the selected one when the user picked exactly one.
    private var sprintColumnTitle: String {
        guard let sprint = store.shelfSprint else { return "This sprint" }
        return sprint.id == store.currentSprint?.id ? "This sprint" : sprint.displayName
    }

    private var sprintExemptionNote: String {
        store.filter.sprints.isEmpty
            ? "filtered"
            : "sprint filter not applied to the shelf"
    }

    private var seriesHelp: String {
        RecurrentSeries.unsupportedReason(for: tasks)
            ?? "The canonical recurring-series name."
    }

    private func roleLabel(_ task: TrackerTask) -> String {
        guard let id = task.roleId else { return "no role" }
        return store.roles.first { $0.id == id }?.displayName ?? id
    }

    /// Minutes this perpetual task logged in the shelf's sprint, from the
    /// timestamp-bucketed `sprints_with_time` — never a field lookup.
    private func thisSprint(_ task: TrackerTask) -> Double {
        guard let sprintID = store.shelfSprint?.id else { return 0 }
        return task.minutes(inSprint: sprintID)
    }
}

// MARK: - The row action menu

/// A task's actions, in `TaskAction.menu(for:)` order.
///
/// **One view, four surfaces**: the shelf's row context menu, the board card's
/// context menu, the inspector's button row and the menu bar's Task menu. They
/// cannot present different items, a different order or a different gate,
/// because they all read one table.
///
/// Phase 8 widened it from `ShelfAction` to `TaskAction` so board cards get the
/// same treatment; `End Series` is still last, separated, destructive-styled and
/// **without a keyboard shortcut** — asserted in `ShelfActionTests` and again in
/// `TaskActionTests` — and is still offered only on recurrent tasks.
struct TaskActionMenu: View {
    let task: TrackerTask
    @Environment(Store.self) private var store

    var body: some View {
        ForEach(TaskAction.menu(for: task)) { action in
            if action.isSeparatedInMenu { Divider() }
            item(action)
        }
    }

    @ViewBuilder
    private func item(_ action: TaskAction) -> some View {
        let availability = action.availability(
            for: task, isTimerRunning: store.isTimerRunning(on: task))
        Button(role: action.isDestructive ? .destructive : nil) {
            _Concurrency.Task { await store.perform(action, on: task) }
        } label: {
            Label(action.title, systemImage: action.systemImage)
        }
        .disabled(!availability.isAvailable)
        .help(availability.reason ?? helpText(action))
    }

    private func helpText(_ action: TaskAction) -> String {
        switch action {
        case .shelf(.startTimer): "Start the timer on this task"
        case .shelf(.logTime): "Add a time entry to this task"
        case .shelf(.openIssue): "Open \(task.currentIssue ?? "the issue") in your browser"
        case .shelf(.syncSprints):
            "Preview reconciling this task's per-sprint issues. "
            + "Shows a dry run before anything is sent."
        case .shelf(.endSeries):
            "Ends the recurrence and closes the live GitHub issue. "
            + "Asks you to type the series name first."
        case .markDone:
            "Close this task. Shows the plan — hours, issues, sprints — "
            + "before anything is sent."
        }
    }
}
