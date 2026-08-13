import Foundation

/// What the trailing inspector shows for one task (plan §11: "logs table,
/// sprint bindings, notes").
///
/// A pure model rather than logic inside the view, for the reason Phase 7 wrote
/// down about the chart: the ordering rules here are exactly the sort of thing
/// that renders wrong while every test stays green. Three of them matter —
///
/// * **Logs newest-first, on `log_effective_date`.** `wt.log_effective_date()`
///   prefers `started_at` over `at`, and 29 of the owner's 416 logs have no
///   `started_at` at all. Sorting on `at` alone would interleave an imported
///   calendar entry with the session it duplicates.
/// * **Bindings newest-sprint-first, on the sprint's start date** — never on
///   the sprint *title*, which sorts `Sprint 99` after `Sprint 100`.
/// * **Hours come from the snapshot, never re-derived.** `reportableMins` is
///   the sprint-filtered figure the daemon computed; CLAUDE.md is explicit that
///   Swift must not recompute per-sprint attribution.
struct TaskInspectorModel: Equatable, Sendable {

    /// One `label: value` row in the detail grid.
    struct Row: Equatable, Sendable, Identifiable {
        let label: String
        let value: String
        /// Rendered monospaced — issue refs, repos and ids.
        var isMonospaced: Bool = false
        var id: String { label }
    }

    /// One row of the sprint-bindings table.
    struct Binding: Equatable, Sendable, Identifiable {
        let sprintId: String?
        let sprint: String
        let issue: String?
        let isClosed: Bool
        /// What GitHub was last told, from `hours_synced`. `nil` when never
        /// synced — which is different from "synced zero".
        let hoursSynced: Double?
        /// The minutes the logs actually put in this sprint, from
        /// `sprints_with_time`.
        let loggedMins: Double
        /// The sprint's ISO start date, for ordering only.
        let startDate: String?

        var id: String { (sprintId ?? "") + "|" + (issue ?? "") + "|" + sprint }

        /// True when GitHub carries a different figure from the logs — the
        /// thing a Sync Sprints run would fix.
        var isOutOfSync: Bool {
            guard let hoursSynced else { return loggedMins > 0 }
            return abs(hoursSynced * 60 - loggedMins) > 1
        }
    }

    let taskId: String
    let title: String
    let statusLabel: String
    let roleLabel: String
    let isRecurrent: Bool
    /// The task's description, i.e. its local notes. Empty when it has none.
    let notes: String
    let details: [Row]
    let bindings: [Binding]
    /// Newest first.
    let logs: [LogEntry]
    let loggedMins: Double
    let reportableMins: Double

    /// Builds the model. `roleLabel` is resolved by the caller because roles
    /// live on the snapshot, not on the task.
    init(task: TrackerTask, roleLabel: String, sprints: [Sprint]) {
        taskId = task.id
        title = task.title
        statusLabel = task.statusLabel ?? task.status.displayName
        self.roleLabel = roleLabel
        isRecurrent = task.status == .recurrent
        notes = task.description.trimmingCharacters(in: .whitespacesAndNewlines)
        loggedMins = task.loggedMins
        reportableMins = task.reportableMins

        var rows: [Row] = [
            Row(label: "Status", value: statusLabel),
            Row(label: "Role", value: roleLabel),
        ]
        if let repo = task.githubRepo, !repo.isEmpty {
            rows.append(Row(label: "Repository", value: repo, isMonospaced: true))
        }
        // `current_issue`, never the legacy `github_issue` mirror — the daemon
        // resolves it through `task_current_issue()`.
        if let issue = task.currentIssue {
            rows.append(Row(label: "Current issue", value: issue, isMonospaced: true))
        }
        if let activity = task.activity, !activity.isEmpty {
            rows.append(Row(label: "Activity", value: activity))
        }
        if let type = task.type, !type.isEmpty {
            rows.append(Row(label: "Type", value: type))
        }
        if let start = task.startSprint, !start.isEmpty {
            rows.append(Row(label: "Start sprint", value: start))
        }
        if let last = task.lastLoggedAt {
            rows.append(Row(label: "Last logged", value: Duration.relative(since: last)))
        }
        if let folder = task.localFolder, !folder.isEmpty {
            rows.append(Row(label: "Local folder", value: folder, isMonospaced: true))
        }
        details = rows

        // A sprint the task has time in but no binding for is still a row: it
        // is precisely the gap `wt sync-sprints` exists to close, and hiding it
        // would make the inspector agree with GitHub rather than with the logs.
        let minutesBySprint = Dictionary(
            task.sprintsWithTime.compactMap { time in time.sprintId.map { ($0, time.totalMins) } },
            uniquingKeysWith: +)
        let startBySprint: [String: String] = Dictionary(
            sprints.compactMap { sprint in sprint.startDate.map { (sprint.id, $0) } },
            uniquingKeysWith: { first, _ in first })
        let titleBySprint = Dictionary(
            sprints.map { ($0.id, $0.displayName) }, uniquingKeysWith: { first, _ in first })

        var built: [Binding] = task.sprintIssues.map { binding in
            let id = binding.sprintId
            return Binding(
                sprintId: id,
                sprint: binding.sprint
                    ?? id.flatMap { titleBySprint[$0] }
                    ?? "unknown sprint",
                issue: binding.issue,
                isClosed: binding.isClosed,
                hoursSynced: binding.hoursSynced,
                loggedMins: id.map { minutesBySprint[$0] ?? 0 } ?? 0,
                startDate: id.flatMap { startBySprint[$0] })
        }
        let bound = Set(task.sprintIssues.compactMap(\.sprintId))
        for time in task.sprintsWithTime where !bound.contains(time.sprintId ?? "") {
            built.append(Binding(
                sprintId: time.sprintId,
                sprint: time.sprintTitle ?? "unknown sprint",
                issue: nil,
                isClosed: false,
                hoursSynced: nil,
                loggedMins: time.totalMins,
                startDate: time.startDate
                    ?? time.sprintId.flatMap { startBySprint[$0] }))
        }
        // Newest sprint first, on the ISO start date. Falling back to the title
        // only when a date is missing, and even then reversed, so the ordering
        // degrades to "most recently added" rather than to lexicographic.
        bindings = built.sorted { lhs, rhs in
            switch (lhs.startDate, rhs.startDate) {
            case let (l?, r?) where l != r: return l > r
            case (nil, _?): return false
            case (_?, nil): return true
            default: return lhs.sprint > rhs.sprint
            }
        }

        logs = task.logs.sorted {
            ($0.effectiveDate ?? 0, $0.id) > ($1.effectiveDate ?? 0, $1.id)
        }
    }

    /// The bindings whose GitHub hours disagree with the logs. The inspector
    /// says so rather than leaving the reader to compare two columns.
    var outOfSyncBindings: [Binding] { bindings.filter(\.isOutOfSync) }

    /// A one-line spoken summary, used as the inspector's accessibility label.
    var accessibilityDescription: String {
        var parts = ["\(title), \(statusLabel), role \(roleLabel)",
                     "\(Duration.format(minutes: reportableMins)) reportable of "
                     + "\(Duration.format(minutes: loggedMins)) logged",
                     "\(logs.count) log entr\(logs.count == 1 ? "y" : "ies")"]
        if !bindings.isEmpty {
            parts.append("\(bindings.count) sprint binding"
                         + (bindings.count == 1 ? "" : "s"))
        }
        return parts.joined(separator: ", ")
    }
}
