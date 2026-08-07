import SwiftUI

/// The Sync Sprints sheet (plan §9).
///
/// On a recurrent task a reconcile is not bookkeeping: it mints the new sprint's
/// issue and closes the ended one, both irreversible, both against the owner's
/// real GitHub org. So this follows the same shape as the §7.1 close sheet — a
/// write-free dry run rendered *before* anything happens, then an explicit
/// confirmation — and the action is **never** run automatically.
///
/// One thing it does that the close sheet does not: the "create missing issues"
/// toggle re-plans. `wt sync-sprints` treats issue creation as opt-in because a
/// blanket run over a long history can want to mint a couple of dozen issues,
/// and a preview that described a smaller run than the button would start would
/// be worse than no preview.
struct SyncSprintsSheetView: View {
    @Environment(Store.self) private var store
    let sheet: SyncSheetState

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            header
            Divider()
            content
                .frame(maxWidth: .infinity, alignment: .leading)
            Divider()
            footer
        }
        .padding(20)
        .frame(minWidth: 640, idealWidth: 720, minHeight: 340)
        .interactiveDismissDisabled(!sheet.isDismissable)
    }

    // MARK: - Header

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Sync sprints for “\(sheet.title)”")
                .font(.title3.weight(.semibold))
                .lineLimit(2)
            Text(subtitle).font(.callout).foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .combine)
    }

    private var subtitle: String {
        switch sheet.phase {
        case .planning: "Running a dry run — no changes are made."
        case .ready(let plan):
            plan.isNoOp
                ? "Every sprint binding already matches the logs. Nothing to do."
                : "Review the plan below. Nothing has been sent yet."
        case .planFailed: "The preview could not be produced."
        case .running: "Reconciling… do not quit."
        case .failed: "The reconcile failed."
        case .succeeded: "Done."
        }
    }

    // MARK: - Body

    @ViewBuilder
    private var content: some View {
        switch sheet.phase {
        case .planning:
            HStack(spacing: 10) {
                ProgressView().controlSize(.small)
                Text("Working out what this would do — no changes are made.")
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, minHeight: 140)

        case .ready(let plan):
            ReconcilePreviewView(plan: plan, createIssues: sheet.createIssues) { value in
                _Concurrency.Task { await store.setSyncCreatesIssues(value) }
            }

        case .planFailed(let message):
            failureBox(title: "Could not plan the reconcile", message: message, lines: [])

        case .running(_, let plan, let lines):
            VStack(alignment: .leading, spacing: 12) {
                OperationLog(lines: lines, running: true)
                Text(plan.impact.summary + " GitHub calls are in flight.")
                    .font(.caption).foregroundStyle(.secondary)
            }

        case .failed(let message, let code, let lines):
            failureBox(title: code.map { "The reconcile failed (\($0))" }
                       ?? "The reconcile failed",
                       message: message, lines: lines)

        case .succeeded(let lines, let outcome):
            // Same rule as the close sheet: a *completed* operation is not
            // automatically a successful one. `reconcile_task_sprints` reports
            // `success: false` with `errors[]` when an op raises.
            let summary: (message: String, isWarning: Bool) =
                outcome?.completionSummary
                ?? (message: "The sprint bindings and their issues are up to date.",
                    isWarning: false)
            VStack(alignment: .leading, spacing: 12) {
                Label(summary.message,
                      systemImage: summary.isWarning
                        ? "exclamationmark.triangle.fill" : "checkmark.circle")
                    .foregroundStyle(summary.isWarning ? .orange : .green)
                OperationLog(lines: lines, running: false)
            }
        }
    }

    private func failureBox(title: String, message: String, lines: [String]) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Label(title, systemImage: "exclamationmark.triangle")
                .font(.headline).foregroundStyle(.orange)
            Text(message).textSelection(.enabled).font(.callout)
            Text("Reconcile is idempotent by construction — it diffs against a "
                 + "derived target state — so re-running it after fixing the cause "
                 + "is safe and will not double-apply anything.")
                .font(.caption).foregroundStyle(.secondary)
            if !lines.isEmpty { OperationLog(lines: lines, running: false) }
        }
    }

    // MARK: - Footer

    @ViewBuilder
    private var footer: some View {
        HStack {
            warning
            Spacer(minLength: 16)
            buttons
        }
    }

    @ViewBuilder
    private var warning: some View {
        if case .ready(let plan) = sheet.phase, plan.impact.isIrreversible {
            Label {
                Text(plan.impact.summary) + Text(" This cannot be undone.").bold()
            } icon: {
                Image(systemName: "exclamationmark.triangle.fill")
            }
            .font(.callout)
            .foregroundStyle(.orange)
            .accessibilityLabel("Warning: \(plan.impact.summary) This cannot be undone.")
        } else if case .ready(let plan) = sheet.phase, !plan.isNoOp {
            Label(plan.impact.summary, systemImage: "info.circle")
                .font(.callout).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private var buttons: some View {
        switch sheet.phase {
        case .planning, .planFailed:
            Button("Cancel") { store.dismissSyncSheet() }
                .keyboardShortcut(.cancelAction)

        case .ready(let plan):
            Button(plan.isNoOp ? "Close" : "Cancel") { store.dismissSyncSheet() }
                .keyboardShortcut(.cancelAction)
            if !plan.isNoOp {
                Button(plan.impact.issuesCreated > 0 ? "Create Issues and Sync" : "Sync") {
                    _Concurrency.Task { await store.confirmSyncSprints() }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(!plan.isActionable)
                .help(plan.isActionable
                      ? "Runs the reconcile against GitHub"
                      : "The dry run failed, so the real run would fail too")
            }

        case .running:
            ProgressView().controlSize(.small)
            Button("Run in Background") { store.dismissSyncSheet() }
                .help("The reconcile keeps running; the board updates when it finishes")

        case .failed, .succeeded:
            Button("Done") { store.dismissSyncSheet() }
                .keyboardShortcut(.defaultAction)
        }
    }
}

// MARK: - The preview

/// The dry run, rendered with the **same row derivation the close sheet uses**
/// (`ClosePlanRowBuilder`), so one plan can never be described two ways.
private struct ReconcilePreviewView: View {
    let plan: ReconcileResponse
    let createIssues: Bool
    let setCreateIssues: (Bool) -> Void
    @State private var showsRawLines = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if plan.isNoOp {
                Label("Nothing to do — every sprint's hours already match its issue.",
                      systemImage: "checkmark.circle")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 100, alignment: .leading)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        ForEach(plan.rows) { row in
                            PlanRowView(row: row)
                            Divider()
                        }
                    }
                }
                .frame(minHeight: 110, maxHeight: 240)
            }

            if plan.untouchedSprintCount > 0 {
                Text("\(plan.untouchedSprintCount) other sprint"
                     + "\(plan.untouchedSprintCount == 1 ? "" : "s") already match and are "
                     + "not listed.")
                    .font(.caption).foregroundStyle(.tertiary)
            }

            Toggle(isOn: Binding(get: { createIssues }, set: setCreateIssues)) {
                VStack(alignment: .leading, spacing: 1) {
                    Text("Create issues for sprints that have time but none")
                    Text("Off by default — a run over a long history can want to mint "
                         + "a couple of dozen issues.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .toggleStyle(.checkbox)

            if let error = plan.result.error {
                Label(error, systemImage: "xmark.octagon")
                    .font(.callout).foregroundStyle(.red)
            }
            if plan.impact.unreportedSprints > 0 {
                Label("\(plan.impact.unreportedSprints) sprint"
                      + "\(plan.impact.unreportedSprints == 1 ? " has" : "s have") logged "
                      + "time and no issue. Their hours stay unreported until issue "
                      + "creation is turned on.",
                      systemImage: "exclamationmark.circle")
                    .font(.caption).foregroundStyle(.orange)
            }
            if plan.impact.withheldSprints > 0 {
                Label("Hours writes are withheld: some of this task's time has no issue "
                      + "to report on, and narrowing the others would delete it from the "
                      + "project.",
                      systemImage: "exclamationmark.circle")
                    .font(.caption).foregroundStyle(.orange)
            }

            DisclosureGroup("Plan as the CLI prints it", isExpanded: $showsRawLines) {
                // `wt._reconcile_plan_lines()` verbatim, so this sheet and
                // `wt sync-sprints` can never disagree about a plan.
                Text(plan.planLines.isEmpty ? "Nothing to do."
                     : plan.planLines.joined(separator: "\n"))
                    .font(.caption.monospaced())
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.top, 4)
            }
            .font(.caption)
        }
    }
}

/// The SSE-fed progress list, shared in shape with the close sheet's.
struct OperationLog: View {
    let lines: [String]
    let running: Bool

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 3) {
                    if lines.isEmpty {
                        Text(running ? "Waiting for the daemon…" : "No output.")
                            .foregroundStyle(.secondary)
                    }
                    ForEach(Array(lines.enumerated()), id: \.offset) { index, line in
                        Text(line)
                            .font(.caption.monospaced())
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .id(index)
                    }
                }
                .padding(8)
            }
            .frame(minHeight: 120, maxHeight: 240)
            .background(.quaternary.opacity(0.35), in: .rect(cornerRadius: 6))
            .onChange(of: lines.count) { _, count in
                withAnimation { proxy.scrollTo(count - 1, anchor: .bottom) }
            }
        }
    }
}
