import SwiftUI

/// The trailing `.inspector` (plan §11).
///
/// **A panel, not a modal** — it inspects a selection, and a selection changes
/// under you. Sheets in this app are reserved for confirmation and destructive
/// flows, which is why nothing here writes: every action offered is the same
/// `TaskAction` the menu bar and the context menus offer, and each one goes
/// through `Store.perform(_:on:)` and its gate.
///
/// Everything it shows comes from `TaskInspectorModel`, which is pure and
/// tested. This file is layout.
struct InspectorView: View {
    @Environment(Store.self) private var store

    var body: some View {
        Group {
            if let task = store.menuTask {
                content(for: task)
            } else {
                ContentUnavailableView {
                    Label("No task selected", systemImage: "sidebar.right")
                        .symbolRenderingMode(.hierarchical)
                } description: {
                    Text("Select a card on the board or a row on the recurrent "
                         + "shelf to inspect its logs and sprint bindings.")
                }
            }
        }
        // No width modifier here: the pane's width is set by its owner
        // (`RootView.inspectorWidth`), which is what makes it deterministic.
        // See the note on the `HStack` in `RootView` for why this is a plain
        // trailing pane rather than `.inspector`.
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    @ViewBuilder
    private func content(for task: TrackerTask) -> some View {
        let model = TaskInspectorModel(task: task,
                                       roleLabel: roleLabel(task),
                                       sprints: store.snapshot?.sprints ?? [])
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                header(model, task: task)
                Divider()
                details(model)
                if !model.bindings.isEmpty {
                    Divider()
                    bindings(model)
                }
                Divider()
                logs(model)
                if !model.notes.isEmpty {
                    Divider()
                    notes(model)
                }
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .accessibilityLabel("Inspector. " + model.accessibilityDescription)
    }

    // MARK: - Header

    @ViewBuilder
    private func header(_ model: TaskInspectorModel, task: TrackerTask) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(model.title)
                .font(.headline)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                RoleChip(label: model.roleLabel,
                         color: RolePalette.color(forRoleID: task.roleId, in: store.roles))
                Spacer(minLength: 8)
                Text(Duration.formatZeroed(minutes: model.reportableMins))
                    .font(.body.monospacedDigit().weight(.medium))
                if model.loggedMins > model.reportableMins + 0.01 {
                    Text("of \(Duration.format(minutes: model.loggedMins))")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
            }
            // The same action set as the Task menu and the context menus, so
            // the inspector adds a surface without adding a code path.
            TaskActionRow(task: task)
        }
    }

    // MARK: - Sections

    private func details(_ model: TaskInspectorModel) -> some View {
        section("Details", symbol: "info.circle") {
            Grid(alignment: .leadingFirstTextBaseline,
                 horizontalSpacing: 12, verticalSpacing: 8) {
                ForEach(model.details) { row in
                    GridRow {
                        Text(row.label)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .gridColumnAlignment(.leading)
                        Text(row.value)
                            .font(row.isMonospaced ? .caption.monospaced() : .caption)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("\(row.label): \(row.value)")
                }
            }
        }
    }

    private func bindings(_ model: TaskInspectorModel) -> some View {
        section("Sprint bindings", symbol: "calendar.badge.clock") {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(model.bindings) { binding in
                    BindingRow(binding: binding)
                }
                if !model.outOfSyncBindings.isEmpty {
                    Label {
                        Text(Self.outOfSyncNote(count: model.outOfSyncBindings.count))
                            .font(.caption)
                    } icon: {
                        Image(systemName: "exclamationmark.triangle")
                            .symbolRenderingMode(.hierarchical)
                    }
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    /// Broken out of the `Label` because the inline string concatenation was
    /// enough to make the type checker give up on the whole `section` body.
    static func outOfSyncNote(count: Int) -> String {
        let noun = count == 1 ? "binding carries" : "bindings carry"
        return "\(count) \(noun) hours GitHub has not been told. "
            + "Sync Sprints previews the fix."
    }

    private func logs(_ model: TaskInspectorModel) -> some View {
        section("Logs", symbol: "list.bullet.rectangle") {
            if model.logs.isEmpty {
                Text("No time logged yet.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(model.logs.prefix(Self.visibleLogs)) { log in
                        LogRow(log: log)
                    }
                    if model.logs.count > Self.visibleLogs {
                        Text("\(model.logs.count - Self.visibleLogs) older "
                             + "entr\(model.logs.count - Self.visibleLogs == 1 ? "y" : "ies") "
                             + "not shown.")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }
            }
        }
    }

    /// The inspector is a panel beside a board, not a log editor — and the
    /// client has no log-editing endpoint anyway (`wt logs`/`edit-log` are CLI
    /// and MCP only). Showing the recent slice keeps it scannable; the full
    /// history lives where it can be edited.
    private static let visibleLogs = 12

    private func notes(_ model: TaskInspectorModel) -> some View {
        section("Notes", symbol: "note.text") {
            Text(model.notes)
                .font(.caption)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    @ViewBuilder
    private func section<Content: View>(_ title: String, symbol: String,
                                        @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(title, systemImage: symbol)
                .symbolRenderingMode(.hierarchical)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.secondary)
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func roleLabel(_ task: TrackerTask) -> String {
        guard let id = task.roleId else { return "no role" }
        return store.roles.first { $0.id == id }?.displayName ?? id
    }
}

// MARK: - Rows

/// One sprint binding: which sprint, which issue, open or closed, and whether
/// GitHub's figure matches the logs.
private struct BindingRow: View {
    let binding: TaskInspectorModel.Binding

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 8) {
                Text(binding.sprint).font(.caption.weight(.medium))
                // Closed/open is a text label first; the symbol and the colour
                // are the second and third channels, never the only one.
                Label(binding.isClosed ? "closed" : "open",
                      systemImage: binding.isClosed ? "lock" : "circle")
                    .symbolRenderingMode(.hierarchical)
                    .labelStyle(.titleAndIcon)
                    .font(.caption2)
                    .foregroundStyle(binding.isClosed
                                     ? AnyShapeStyle(.secondary) : AnyShapeStyle(Color.green))
                Spacer(minLength: 4)
                Text(Duration.formatZeroed(minutes: binding.loggedMins))
                    .font(.caption.monospacedDigit())
            }
            HStack(spacing: 6) {
                Text(binding.issue ?? "no issue")
                    .font(.caption2.monospaced())
                    .foregroundStyle(binding.issue == nil ? .tertiary : .secondary)
                    .lineLimit(1)
                if binding.isOutOfSync {
                    Label(syncedText, systemImage: "exclamationmark.triangle")
                        .symbolRenderingMode(.hierarchical)
                        .labelStyle(.titleAndIcon)
                        .font(.caption2)
                        .foregroundStyle(.orange)
                }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityLabel)
    }

    private var syncedText: String {
        guard let hours = binding.hoursSynced else { return "never synced" }
        return "GitHub has \(Duration.formatZeroed(minutes: hours * 60))"
    }

    private var accessibilityLabel: String {
        var parts = [binding.sprint,
                     binding.isClosed ? "issue closed" : "issue open",
                     binding.issue ?? "no issue",
                     "\(Duration.formatZeroed(minutes: binding.loggedMins)) logged"]
        if binding.isOutOfSync { parts.append("out of sync: \(syncedText)") }
        return parts.joined(separator: ", ")
    }
}

/// One log entry: when, how long, and the note.
private struct LogRow: View {
    let log: LogEntry

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(Duration.format(minutes: log.minutes))
                .font(.caption.monospacedDigit().weight(.medium))
                .frame(width: 56, alignment: .leading)
            VStack(alignment: .leading, spacing: 1) {
                Text(log.note.isEmpty ? "—" : log.note)
                    .font(.caption)
                    .lineLimit(2)
                HStack(spacing: 4) {
                    Text(when)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                    // The 29 logs with no wall clock are marked, exactly as the
                    // Gantt hatches their bars — a date without a time of day
                    // must not read as a time of day of midnight.
                    if !log.hasWallClock {
                        Image(systemName: "questionmark.circle")
                            .symbolRenderingMode(.hierarchical)
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                            .help("This entry records the date but not the time of day.")
                    }
                }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(Duration.format(minutes: log.minutes)), "
                            + "\(log.note.isEmpty ? "no note" : log.note), \(when)"
                            + (log.hasWallClock ? "" : ", approximate time of day"))
    }

    private var when: String {
        guard let epoch = log.effectiveDate else { return "no date" }
        return Date(timeIntervalSince1970: epoch)
            .formatted(.dateTime.month(.abbreviated).day().hour().minute())
    }
}

// MARK: - Actions

/// The task's actions as buttons, mirroring the Task menu exactly.
///
/// `TaskAction.menu(for:)` decides the item set, so the inspector cannot offer
/// `End Series` on a board card or `Mark Done` on a recurrent one — and both
/// still route through the gates in `Store.perform(_:on:)`.
struct TaskActionRow: View {
    let task: TrackerTask
    @Environment(Store.self) private var store

    var body: some View {
        FlowRow(spacing: 8) {
            ForEach(TaskAction.menu(for: task)) { action in
                let availability = action.availability(
                    for: task, isTimerRunning: store.isTimerRunning(on: task))
                Button {
                    _Concurrency.Task { await store.perform(action, on: task) }
                } label: {
                    Label(action.title, systemImage: action.systemImage)
                        .symbolRenderingMode(.hierarchical)
                        .font(.caption)
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .tint(action.isDestructive ? .red : nil)
                .disabled(!availability.isAvailable)
                .help(availability.reason ?? action.title)
            }
        }
    }
}
