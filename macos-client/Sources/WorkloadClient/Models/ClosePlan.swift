import Foundation

// A Codable mirror of `POST /v1/tasks/{id}/close/plan` (wt_api.plan_close), plus
// the pure derivation that turns it into the rows the §7.1 sheet renders.
//
// `plan_close` is a `reconcile_task_sprints(dry_run=True, closing=True)` — the
// planner is a separate pass from the executor in `wt.py`, so it is **write-free
// by construction** and makes no GitHub calls. It is the only thing a Done drop
// is allowed to send before the user confirms.
//
// The row derivation lives here, not in the view, for one reason: "how many
// GitHub issues will this create" is the number the confirmation is built
// around, and it has to be unit-testable without a UI.

// MARK: - Wire types

/// The whole `close/plan` response.
struct ClosePlanResponse: Decodable, Sendable, Equatable {
    let title: String?
    /// The task's `github_repo`. `nil` means the close touches GitHub not at all.
    let repo: String?
    /// True when the task has a repo but no issue yet, so the close workflow
    /// itself must mint the task's first issue. This is **not** counted by
    /// `willCreateIssues`, which only covers the reconcile's per-sprint issues —
    /// see `issuesToCreate`.
    let needsIssue: Bool
    let currentIssue: String?
    /// `wt_api.plan_close`'s count of reconcile-planned issue creations.
    let willCreateIssues: Int
    /// `wt._reconcile_plan_lines()` — the same itemised text `wt sync-sprints`
    /// prints. Shown as the sheet's "details" disclosure so the app and the CLI
    /// never disagree about what a plan says.
    let planLines: [String]
    let plan: ReconcilePlan

    enum CodingKeys: String, CodingKey {
        case title, repo, plan
        case needsIssue = "needs_issue"
        case currentIssue = "current_issue"
        case willCreateIssues = "will_create_issues"
        case planLines = "plan_lines"
    }

    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        title = try c.decodeIfPresent(String.self, forKey: .title)
        repo = try c.decodeIfPresent(String.self, forKey: .repo)
        needsIssue = try c.decodeIfPresent(Bool.self, forKey: .needsIssue) ?? false
        currentIssue = try c.decodeIfPresent(String.self, forKey: .currentIssue)
        willCreateIssues = try c.decodeIfPresent(Int.self, forKey: .willCreateIssues) ?? 0
        planLines = try c.decodeIfPresent([String].self, forKey: .planLines) ?? []
        plan = try c.decodeIfPresent(ReconcilePlan.self, forKey: .plan) ?? ReconcilePlan()
    }
}

/// `reconcile_task_sprints()`'s dry-run result.
struct ReconcilePlan: Decodable, Sendable, Equatable {
    let success: Bool
    let error: String?
    let dryRun: Bool
    let currentSprint: String?
    /// The target state: one entry per sprint the task has time in (plus the
    /// current sprint when the task stays open — dropped here because
    /// `plan_close` passes `closing=True`).
    let target: [PlanTarget]
    let planned: [PlanOperation]
    let skipped: [PlanSkip]
    let bindings: [SprintBinding]
    /// Sprints whose time has no issue to report on. Non-empty means every
    /// hours write for this task is withheld — narrowing the other issues would
    /// delete that time from the project's reporting.
    let unbillable: [PlanUnbillable]
    let unassignedMinutes: Double

    init() {
        success = false; error = nil; dryRun = true; currentSprint = nil
        target = []; planned = []; skipped = []; bindings = []
        unbillable = []; unassignedMinutes = 0
    }

    enum CodingKeys: String, CodingKey {
        case success, error, target, planned, skipped, bindings, unbillable
        case dryRun = "dry_run"
        case currentSprint = "current_sprint"
        case unassignedMinutes = "unassigned_minutes"
    }

    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        success = try c.decodeIfPresent(Bool.self, forKey: .success) ?? false
        error = try c.decodeIfPresent(String.self, forKey: .error)
        dryRun = try c.decodeIfPresent(Bool.self, forKey: .dryRun) ?? false
        currentSprint = try c.decodeIfPresent(String.self, forKey: .currentSprint)
        target = try c.decodeIfPresent([PlanTarget].self, forKey: .target) ?? []
        planned = try c.decodeIfPresent([PlanOperation].self, forKey: .planned) ?? []
        skipped = try c.decodeIfPresent([PlanSkip].self, forKey: .skipped) ?? []
        bindings = try c.decodeIfPresent([SprintBinding].self, forKey: .bindings) ?? []
        unbillable = try c.decodeIfPresent([PlanUnbillable].self, forKey: .unbillable) ?? []
        unassignedMinutes = try c.decodeIfPresent(Double.self, forKey: .unassignedMinutes) ?? 0
    }
}

/// One `plan.target[]` entry: a sprint the close must account for.
struct PlanTarget: Decodable, Sendable, Hashable {
    let sprintId: String?
    let sprint: String?
    let minutes: Double
    let hours: Double

    enum CodingKeys: String, CodingKey {
        case sprint, minutes, hours
        case sprintId = "sprint_id"
    }

    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sprintId = try c.decodeIfPresent(String.self, forKey: .sprintId)
        sprint = try c.decodeIfPresent(String.self, forKey: .sprint)
        minutes = try c.decodeIfPresent(Double.self, forKey: .minutes) ?? 0
        hours = try c.decodeIfPresent(Double.self, forKey: .hours) ?? 0
    }
}

/// One `plan.planned[]` operation. The `op` discriminator is
/// `create` / `repoint` / `hours` / `close` / `supersede` / `relabel`.
struct PlanOperation: Decodable, Sendable, Hashable {
    let op: String
    let sprintId: String?
    let sprint: String?
    let issue: String?
    let minutes: Double?
    let hours: Double?
    let fromHours: Double?
    let fromSprint: String?
    /// `create` only: whether the op actually mints a GitHub issue, as opposed
    /// to writing a local binding because the task has no repo.
    let createIssue: Bool
    let issueTitle: String?
    let repo: String?
    /// `create` only: the sprint has already ended, so the new issue is closed
    /// immediately after it is made.
    let willClose: Bool
    /// `create` only: why GitHub was skipped (`no repo`, `create_issues=False`).
    let skippedGitHub: String?
    /// `supersede` only: the issue that keeps this sprint's hours.
    let primary: String?
    let reason: String?

    enum CodingKeys: String, CodingKey {
        case op, sprint, issue, minutes, hours, repo, primary, reason
        case sprintId = "sprint_id"
        case fromHours = "from_hours"
        case fromSprint = "from_sprint"
        case createIssue = "create_issue"
        case issueTitle = "issue_title"
        case willClose = "will_close"
        case skippedGitHub = "skipped_github"
    }

    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        op = try c.decodeIfPresent(String.self, forKey: .op) ?? ""
        sprintId = try c.decodeIfPresent(String.self, forKey: .sprintId)
        sprint = try c.decodeIfPresent(String.self, forKey: .sprint)
        issue = try c.decodeIfPresent(String.self, forKey: .issue)
        minutes = try c.decodeIfPresent(Double.self, forKey: .minutes)
        hours = try c.decodeIfPresent(Double.self, forKey: .hours)
        fromHours = try c.decodeIfPresent(Double.self, forKey: .fromHours)
        fromSprint = try c.decodeIfPresent(String.self, forKey: .fromSprint)
        createIssue = try c.decodeIfPresent(Bool.self, forKey: .createIssue) ?? false
        issueTitle = try c.decodeIfPresent(String.self, forKey: .issueTitle)
        repo = try c.decodeIfPresent(String.self, forKey: .repo)
        willClose = try c.decodeIfPresent(Bool.self, forKey: .willClose) ?? false
        skippedGitHub = try c.decodeIfPresent(String.self, forKey: .skippedGitHub)
        primary = try c.decodeIfPresent(String.self, forKey: .primary)
        reason = try c.decodeIfPresent(String.self, forKey: .reason)
    }
}

/// One `plan.skipped[]` entry — a sprint the plan deliberately does nothing to.
struct PlanSkip: Decodable, Sendable, Hashable {
    let sprintId: String?
    let sprint: String?
    let issue: String?
    let minutes: Double?
    let hours: Double?
    let fromHours: Double?
    /// The sprint has time but no issue and `create_issues=False` would leave it
    /// unreported.
    let needsIssue: Bool
    /// The hours write was withheld because some of the task's time has no issue
    /// to land on.
    let withheldHours: Bool
    let reason: String?

    enum CodingKeys: String, CodingKey {
        case sprint, issue, minutes, hours, reason
        case sprintId = "sprint_id"
        case fromHours = "from_hours"
        case needsIssue = "needs_issue"
        case withheldHours = "withheld_hours"
    }

    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sprintId = try c.decodeIfPresent(String.self, forKey: .sprintId)
        sprint = try c.decodeIfPresent(String.self, forKey: .sprint)
        issue = try c.decodeIfPresent(String.self, forKey: .issue)
        minutes = try c.decodeIfPresent(Double.self, forKey: .minutes)
        hours = try c.decodeIfPresent(Double.self, forKey: .hours)
        fromHours = try c.decodeIfPresent(Double.self, forKey: .fromHours)
        needsIssue = try c.decodeIfPresent(Bool.self, forKey: .needsIssue) ?? false
        withheldHours = try c.decodeIfPresent(Bool.self, forKey: .withheldHours) ?? false
        reason = try c.decodeIfPresent(String.self, forKey: .reason)
    }
}

/// One `plan.unbillable[]` entry.
struct PlanUnbillable: Decodable, Sendable, Hashable {
    let sprintId: String?
    let sprint: String?
    let minutes: Double

    enum CodingKeys: String, CodingKey {
        case sprint, minutes
        case sprintId = "sprint_id"
    }

    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sprintId = try c.decodeIfPresent(String.self, forKey: .sprintId)
        sprint = try c.decodeIfPresent(String.self, forKey: .sprint)
        minutes = try c.decodeIfPresent(Double.self, forKey: .minutes) ?? 0
    }
}

// MARK: - Derived sheet rows

/// One line of the §7.1 preview:
///
/// ```
/// Sprint 105   3h 30m   →  (no issue)   CREATE issue, set hours, close
/// ```
struct ClosePlanRow: Identifiable, Sendable, Equatable {
    let sprintId: String?
    let sprintTitle: String
    /// Minutes of logged time this sprint contributes. `nil` when the row is a
    /// pure bookkeeping op (a `close` on a sprint with no target time).
    let minutes: Double?
    /// The issue this sprint's hours land on, or `nil` for "(no issue)".
    let issue: String?
    /// Whether this row mints a GitHub issue. Drives the warning count.
    let createsIssue: Bool
    /// Whether the issue is closed as part of this operation.
    let closesIssue: Bool
    /// An hours write is planned and withheld (see `ReconcilePlan.unbillable`).
    let hoursWithheld: Bool
    /// Ordered, user-facing verbs: `"CREATE issue"`, `"set hours to 12.5h"`, …
    let actions: [String]

    var id: String { sprintId ?? sprintTitle }

    /// True when nothing at all happens to this sprint — rendered greyed so the
    /// sheet still accounts for every sprint the task has time in.
    var isNoOp: Bool { actions.isEmpty }
}

extension ClosePlanResponse {

    /// Total GitHub issues this close creates: the reconcile's per-sprint ones
    /// **plus** the task's very first issue when it has none.
    ///
    /// `will_create_issues` alone under-counts, because `close_task` mints the
    /// first issue itself through its `prompt_callback` rather than through the
    /// reconcile plan. Under-counting here is exactly the failure the §7.1 sheet
    /// exists to prevent, so the two sources are added, not conflated.
    var issuesToCreate: Int { willCreateIssues + (needsIssue ? 1 : 0) }

    /// What to pass as `create_issue` on `POST /close`. `close()` refuses rather
    /// than mints when this is false, so it must be true whenever the preview
    /// told the user an issue would be created.
    var createIssueOnConfirm: Bool { needsIssue }

    /// Whether confirming is meaningful. A failed dry run means the executor
    /// would fail too, and a failed reconcile *aborts* the close (the task stays
    /// open) — so offering the button would just produce a guaranteed error.
    var isActionable: Bool { plan.success && plan.error == nil }

    /// Whether this close touches GitHub at all.
    var touchesGitHub: Bool {
        (repo?.isEmpty == false) || currentIssue != nil || !plan.planned.isEmpty
    }

    /// The preview rows, one per sprint, in plan order.
    ///
    /// Built from the union of `target` (sprints with time), `planned` and
    /// `skipped`, so a `close` op on an old binding whose sprint has no time
    /// still gets a line rather than happening invisibly.
    var rows: [ClosePlanRow] {
        var order: [String] = []
        var seen: Set<String> = []
        func note(_ key: String?) {
            let key = key ?? "—"
            if seen.insert(key).inserted { order.append(key) }
        }
        plan.target.forEach { note($0.sprintId) }
        plan.planned.forEach { note($0.sprintId) }
        plan.skipped.filter { $0.needsIssue || $0.withheldHours }
            .forEach { note($0.sprintId) }

        let bindingsBySprint = Dictionary(
            plan.bindings.map { ($0.sprintId ?? "—", $0) }, uniquingKeysWith: { first, _ in first })

        let built: [ClosePlanRow] = order.map { key in
            let target = plan.target.first { ($0.sprintId ?? "—") == key }
            let ops = plan.planned.filter { ($0.sprintId ?? "—") == key }
            let skips = plan.skipped.filter { ($0.sprintId ?? "—") == key }
            let binding = bindingsBySprint[key]

            let title = target?.sprint ?? ops.first?.sprint ?? skips.first?.sprint
                ?? binding?.sprint ?? "unknown sprint"

            let createOp = ops.first { $0.op == "create" }
            let repointOp = ops.first { $0.op == "repoint" }
            let hoursOp = ops.first { $0.op == "hours" }
            let closeOp = ops.first { $0.op == "close" }
            let supersedeOp = ops.first { $0.op == "supersede" }
            let withheld = skips.first { $0.withheldHours }
            let needsIssueSkip = skips.first { $0.needsIssue }

            // The issue this sprint's hours land on. A `create` op has none yet;
            // a `repoint` carries the task's live issue forward.
            let issue = createOp != nil ? nil
                : (repointOp?.issue ?? hoursOp?.issue ?? closeOp?.issue
                   ?? binding?.issue ?? needsIssueSkip?.issue)

            var actions: [String] = []
            if let createOp {
                if createOp.createIssue {
                    actions.append("CREATE issue in \(createOp.repo ?? "its repo")")
                    if let hours = createOp.hours, hours > 0 {
                        actions.append("set hours to \(Self.hours(hours))")
                    }
                } else {
                    actions.append("add local binding only "
                                   + "(\(createOp.skippedGitHub ?? "no repo"))")
                }
            }
            if let repointOp {
                actions.append("carry \(repointOp.issue ?? "the issue") forward from "
                               + "\(repointOp.fromSprint ?? "no sprint")")
                if let hours = repointOp.hours, hours > 0 {
                    actions.append("set hours to \(Self.hours(hours))")
                }
            }
            if let hoursOp, let hours = hoursOp.hours {
                let was = hoursOp.fromHours.map { Self.hours($0) } ?? "unknown"
                actions.append("update hours \(was) → \(Self.hours(hours))")
            }
            if let supersedeOp {
                actions.append("zero and close \(supersedeOp.issue ?? "duplicate issue") "
                               + "(superseded by \(supersedeOp.primary ?? "the primary"))")
            }
            if closeOp != nil || createOp?.willClose == true {
                actions.append("close issue — sprint has ended")
            }
            if let needsIssueSkip {
                actions.append("NO ISSUE — \(Duration.format(minutes: needsIssueSkip.minutes ?? 0)) "
                               + "would go unreported")
            }
            if let withheld {
                let was = withheld.fromHours.map { Self.hours($0) } ?? "unknown"
                let want = withheld.hours.map { Self.hours($0) } ?? "?"
                actions.append("hours withheld (\(was) → \(want)) — other sprints "
                               + "have unreported time")
            }

            return ClosePlanRow(
                sprintId: target?.sprintId ?? ops.first?.sprintId ?? skips.first?.sprintId,
                sprintTitle: title,
                minutes: target?.minutes ?? ops.first?.minutes ?? skips.first?.minutes,
                issue: issue,
                createsIssue: createOp?.createIssue ?? false,
                closesIssue: closeOp != nil || createOp?.willClose == true
                    || supersedeOp != nil,
                hoursWithheld: withheld != nil,
                actions: actions)
        }
        return withFirstIssueRow(built)
    }

    /// Attaches the task's **first** issue creation to a row.
    ///
    /// `close_task` mints that issue itself, before the reconcile and outside
    /// `planned[]`, so nothing in the plan mentions it — a real task on the
    /// owner's data planned *no* operations at all yet would still have an
    /// issue created on close. Without this the sheet would warn "1 issue will
    /// be created" over a table that says "no change", which is exactly the
    /// kind of quiet mismatch the preview exists to prevent.
    ///
    /// It lands on the current binding, so: the last sprint whose binding has
    /// no issue, else the last row, else a row of its own.
    private func withFirstIssueRow(_ rows: [ClosePlanRow]) -> [ClosePlanRow] {
        guard needsIssue else { return rows }
        let action = "CREATE the task’s first issue in \(repo ?? "its repo")"
        guard !rows.isEmpty else {
            return [ClosePlanRow(sprintId: nil, sprintTitle: plan.currentSprint ?? "—",
                                 minutes: nil, issue: nil, createsIssue: true,
                                 closesIssue: false, hoursWithheld: false,
                                 actions: [action])]
        }
        let index = rows.lastIndex { $0.issue == nil && !$0.createsIssue } ?? rows.count - 1
        var rows = rows
        let row = rows[index]
        rows[index] = ClosePlanRow(sprintId: row.sprintId, sprintTitle: row.sprintTitle,
                                   minutes: row.minutes, issue: row.issue,
                                   createsIssue: true, closesIssue: row.closesIssue,
                                   hoursWithheld: row.hoursWithheld,
                                   actions: [action] + row.actions)
        return rows
    }

    /// `21.0` → `"21h"`, `12.5` → `"12.5h"`, `12.25` → `"12.25h"`.
    ///
    /// GitHub's Hours field is quarter hours (`mins_to_quarter_hours`), so two
    /// decimals is always enough and a trailing zero is noise.
    static func hours(_ value: Double) -> String {
        if value == value.rounded() { return "\(Int(value))h" }
        var text = String(format: "%.2f", value)
        while text.hasSuffix("0") { text.removeLast() }
        if text.hasSuffix(".") { text.removeLast() }
        return text + "h"
    }
}

// MARK: - Operations

/// `POST /v1/tasks/{id}/close`'s `202` body, and `GET /v1/operations/{id}`.
///
/// The close is long (reconcile + `gh`), so the daemon returns immediately and
/// streams `progress` events. This record is also the reconnect path: a client
/// that missed the stream re-reads the terminal state here.
struct OperationRecord: Decodable, Sendable, Equatable {
    let operationId: String
    let op: String?
    let taskId: String?
    /// `running` / `completed` / `failed`.
    let state: String
    let startedAt: TimeInterval?
    let finishedAt: TimeInterval?
    let progress: [String]
    let error: DaemonErrorBody?

    var isTerminal: Bool { state == "completed" || state == "failed" }
    var didFail: Bool { state == "failed" }

    enum CodingKeys: String, CodingKey {
        case op, state, progress, error
        case operationId = "operation_id"
        case taskId = "task_id"
        case startedAt = "started_at"
        case finishedAt = "finished_at"
    }

    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        operationId = try c.decodeIfPresent(String.self, forKey: .operationId) ?? ""
        op = try c.decodeIfPresent(String.self, forKey: .op)
        taskId = try c.decodeIfPresent(String.self, forKey: .taskId)
        state = try c.decodeIfPresent(String.self, forKey: .state) ?? "running"
        startedAt = try c.decodeIfPresent(TimeInterval.self, forKey: .startedAt)
        finishedAt = try c.decodeIfPresent(TimeInterval.self, forKey: .finishedAt)
        progress = try c.decodeIfPresent([String].self, forKey: .progress) ?? []
        // `DaemonErrorBody` decodes the envelope `{"error": {code, message}}`,
        // and an operation record *is* that shape at its top level — so it
        // decodes straight from the record's own decoder. `try?` covers the
        // running case, where `error` is `null`.
        error = try? DaemonErrorBody(from: decoder)
    }
}
