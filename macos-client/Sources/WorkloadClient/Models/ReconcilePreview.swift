import Foundation

// A Codable mirror of `POST /v1/tasks/{id}/reconcile` with `{"dry_run": true}`
// (wt_api.reconcile), plus the pure derivation the Sync Sprints sheet renders.
//
// Why this needs its own preview at all: on a **recurrent** task a reconcile is
// not bookkeeping. It mints the new sprint's issue and closes the ended one —
// `gh issue create` and `gh issue close`, both irreversible, both against the
// owner's real org. Plan §9 and the phase brief are explicit that it must show a
// dry run first and never auto-run.
//
// The dry run is write-free *by construction* on the Python side: the planner in
// `reconcile_task_sprints` is a separate pass from the executor, and
// `wt_api.reconcile` passes `save_callback=None` when `dry_run` is set. The
// daemon serves it synchronously with `Daemon.read(...)` rather than as a
// `202` operation, which is the second, independent reason it cannot write.

// MARK: - Wire type

/// The whole `reconcile` response. `dryRun` distinguishes a preview from the
/// record of a real run.
struct ReconcileResponse: Decodable, Sendable, Equatable {
    let dryRun: Bool
    let success: Bool
    /// `wt._reconcile_plan_lines()` — the same itemised text `wt sync-sprints`
    /// prints, so the app and the CLI can never disagree about a plan.
    let planLines: [String]
    /// `wt._reconcile_outcome_lines()`. Empty on a dry run by construction.
    let outcomeLines: [String]
    let result: ReconcilePlan

    enum CodingKeys: String, CodingKey {
        case success, result
        case dryRun = "dry_run"
        case planLines = "plan_lines"
        case outcomeLines = "outcome_lines"
    }

    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        dryRun = try c.decodeIfPresent(Bool.self, forKey: .dryRun) ?? false
        success = try c.decodeIfPresent(Bool.self, forKey: .success) ?? false
        planLines = try c.decodeIfPresent([String].self, forKey: .planLines) ?? []
        outcomeLines = try c.decodeIfPresent([String].self, forKey: .outcomeLines) ?? []
        result = try c.decodeIfPresent(ReconcilePlan.self, forKey: .result) ?? ReconcilePlan()
    }

    /// What to tell the user after a **real** run.
    ///
    /// `reconcile_task_sprints` sets `success: false` and fills `errors[]` when
    /// an operation raises, so a completed operation is not automatically a
    /// successful one — the same trap the close sheet fell into.
    ///
    /// ⚠️ This does **not** catch the silent case: `sync_project_hours` swallows
    /// a `gh` failure and returns `false` without raising, so a run whose only
    /// op was `hours` comes back `success: true` having written nothing.
    /// Observed on the scratch daemon. `hours_synced` is left unmoved in that
    /// case, so a re-run retries — but the sheet cannot tell, and this is
    /// reported as a daemon/`wt.py` contract gap rather than guessed at here.
    var completionSummary: (message: String, isWarning: Bool) {
        if !success {
            let detail = result.error ?? "some operations failed"
            return ("The reconcile did not fully succeed: \(detail)", true)
        }
        return ("The sprint bindings and their issues are up to date.", false)
    }
}

// MARK: - Derived preview

extension ReconcileResponse {

    /// The operations that actually reach GitHub, with the two irreversible
    /// kinds counted separately. This is the number the confirmation is built
    /// around, so it lives here and is unit-tested without a UI.
    var impact: ReconcileImpact {
        var impact = ReconcileImpact()
        for op in result.planned {
            switch op.op {
            case "create":
                if op.createIssue {
                    impact.issuesCreated += 1
                    if op.willClose { impact.issuesClosed += 1 }
                } else {
                    impact.localBindingsOnly += 1
                }
            case "close":
                impact.issuesClosed += 1
            case "supersede":
                impact.issuesClosed += 1
                impact.hoursUpdated += 1
            case "hours":
                impact.hoursUpdated += 1
            case "repoint":
                impact.repointed += 1
            case "relabel":
                impact.relabelled += 1
            default:
                impact.other += 1
            }
        }
        impact.withheldSprints = result.skipped.filter(\.withheldHours).count
        impact.unreportedSprints = result.skipped.filter(\.needsIssue).count
        return impact
    }

    /// Whether the plan would change anything at all. A no-op plan still gets a
    /// sheet — "nothing to do" is a useful answer — but the confirm button is
    /// hidden rather than offered.
    var isNoOp: Bool { result.planned.isEmpty }

    /// Whether confirming is meaningful. A failed dry run means the executor
    /// would fail the same way.
    var isActionable: Bool {
        result.success && result.error == nil && !isNoOp
    }

    /// The per-sprint rows. Deliberately the **same** derivation the close sheet
    /// uses, so the two previews cannot describe one plan two ways.
    ///
    /// Only sprints the plan actually touches are shown: a recurrent task can
    /// carry twenty-one untouched bindings (measured on the owner's data), and
    /// listing them would bury the two lines that matter.
    var rows: [ClosePlanRow] {
        ClosePlanRowBuilder.rows(from: result, includeUntouchedTargets: false,
                                 firstIssue: nil)
    }

    /// How many bindings the plan leaves entirely alone, for the "and N
    /// unchanged" footnote — so nothing is hidden without being counted.
    var untouchedSprintCount: Int {
        let touched = Set(result.planned.map { $0.sprintId ?? "—" })
        let all = Set(result.target.map { $0.sprintId ?? "—" })
            .union(result.bindings.map { $0.sprintId ?? "—" })
        return all.subtracting(touched).count
    }
}

/// What a reconcile would do to GitHub, counted by kind.
struct ReconcileImpact: Equatable, Sendable {
    var issuesCreated = 0
    var issuesClosed = 0
    var hoursUpdated = 0
    var repointed = 0
    var relabelled = 0
    var localBindingsOnly = 0
    var other = 0
    /// Sprints whose hours write is withheld because other time has no issue.
    var withheldSprints = 0
    /// Sprints with time and no issue, which `create_issues=false` would leave
    /// unreported.
    var unreportedSprints = 0

    /// Whether any irreversible GitHub call is planned.
    var isIrreversible: Bool { issuesCreated > 0 || issuesClosed > 0 }

    /// One-line summary for the sheet's warning row.
    var summary: String {
        var parts: [String] = []
        if issuesCreated > 0 {
            parts.append("\(issuesCreated) issue\(issuesCreated == 1 ? "" : "s") created")
        }
        if issuesClosed > 0 {
            parts.append("\(issuesClosed) issue\(issuesClosed == 1 ? "" : "s") closed")
        }
        if hoursUpdated > 0 {
            parts.append("\(hoursUpdated) hours field\(hoursUpdated == 1 ? "" : "s") updated")
        }
        if repointed > 0 { parts.append("\(repointed) carried forward") }
        if relabelled > 0 { parts.append("\(relabelled) relabelled") }
        if localBindingsOnly > 0 {
            parts.append("\(localBindingsOnly) local binding"
                         + (localBindingsOnly == 1 ? "" : "s"))
        }
        return parts.isEmpty ? "No changes." : parts.joined(separator: ", ") + "."
    }
}

// MARK: - Shared row derivation

/// Turns a `ReconcilePlan` into `ClosePlanRow`s.
///
/// Extracted from `ClosePlanResponse.rows` so the close sheet and the sync sheet
/// render one plan the same way. The close sheet keeps its extra behaviours —
/// showing untouched target sprints, and attaching the task's first issue — via
/// the two parameters.
enum ClosePlanRowBuilder {

    static func rows(from plan: ReconcilePlan,
                     includeUntouchedTargets: Bool,
                     firstIssue: (repo: String?, needed: Bool)?) -> [ClosePlanRow] {
        var order: [String] = []
        var seen: Set<String> = []
        func note(_ key: String?) {
            let key = key ?? "—"
            if seen.insert(key).inserted { order.append(key) }
        }
        if includeUntouchedTargets { plan.target.forEach { note($0.sprintId) } }
        plan.planned.forEach { note($0.sprintId) }
        plan.skipped.filter { $0.needsIssue || $0.withheldHours }
            .forEach { note($0.sprintId) }

        let bindingsBySprint = Dictionary(
            plan.bindings.map { ($0.sprintId ?? "—", $0) },
            uniquingKeysWith: { first, _ in first })

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

            let issue = createOp != nil ? nil
                : (repointOp?.issue ?? hoursOp?.issue ?? closeOp?.issue
                   ?? binding?.issue ?? needsIssueSkip?.issue)

            var actions: [String] = []
            if let createOp {
                if createOp.createIssue {
                    actions.append("CREATE issue in \(createOp.repo ?? "its repo")")
                    if let hours = createOp.hours, hours > 0 {
                        actions.append("set hours to \(ClosePlanResponse.hours(hours))")
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
                    actions.append("set hours to \(ClosePlanResponse.hours(hours))")
                }
            }
            if let hoursOp, let hours = hoursOp.hours {
                let was = hoursOp.fromHours.map { ClosePlanResponse.hours($0) } ?? "unknown"
                actions.append("update hours \(was) → \(ClosePlanResponse.hours(hours))")
            }
            if let supersedeOp {
                actions.append("zero and close \(supersedeOp.issue ?? "duplicate issue") "
                               + "(superseded by \(supersedeOp.primary ?? "the primary"))")
            }
            if closeOp != nil || createOp?.willClose == true {
                actions.append("close issue — sprint has ended")
            }
            if let needsIssueSkip {
                actions.append("NO ISSUE — "
                               + "\(Duration.format(minutes: needsIssueSkip.minutes ?? 0)) "
                               + "would go unreported")
            }
            if let withheld {
                let was = withheld.fromHours.map { ClosePlanResponse.hours($0) } ?? "unknown"
                let want = withheld.hours.map { ClosePlanResponse.hours($0) } ?? "?"
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

        guard let firstIssue, firstIssue.needed else { return built }
        return attachFirstIssue(built, repo: firstIssue.repo,
                                currentSprint: plan.currentSprint)
    }

    /// Attaches the task's **first** issue creation to a row.
    ///
    /// `close_task` mints that issue itself, before the reconcile and outside
    /// `planned[]`, so nothing in the plan mentions it.
    private static func attachFirstIssue(_ rows: [ClosePlanRow],
                                         repo: String?,
                                         currentSprint: String?) -> [ClosePlanRow] {
        let action = "CREATE the task’s first issue in \(repo ?? "its repo")"
        guard !rows.isEmpty else {
            return [ClosePlanRow(sprintId: nil, sprintTitle: currentSprint ?? "—",
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
}
