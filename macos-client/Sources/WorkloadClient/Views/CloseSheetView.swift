import SwiftUI

/// The close-confirmation sheet (plan §7.1).
///
/// This exists so that no route to `close_task` — drag, keyboard, menu — can be
/// silent. It renders the `close/plan` dry run **before anything happens**, in
/// the plan's own shape:
///
/// ```
/// Sprint 104   6h 15m   →  grafana/field-eng#412   update hours, close issue
/// Sprint 105   3h 30m   →  (no issue)              CREATE issue, set hours, close
/// Sprint 106   1h 45m   →  grafana/field-eng#488   update hours  (stays open)
///
/// ⚠ 1 GitHub issue will be created. This cannot be undone.
/// ```
///
/// Confirming issues `POST /close` and the sheet becomes a progress list fed by
/// SSE `progress` events. On failure the task stays open, which is
/// `close_task`'s own contract: a failed reconcile aborts the close so hours
/// cannot be mis-reported.
struct CloseSheetView: View {
    @Environment(Store.self) private var store
    let sheet: CloseSheetState

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
        .frame(minWidth: 620, idealWidth: 700, minHeight: 320)
        .interactiveDismissDisabled(!sheet.isDismissable)
    }

    // MARK: - Header

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                if sheet.endSeries != nil {
                    Image(systemName: "exclamationmark.octagon.fill")
                        .foregroundStyle(.red)
                        .accessibilityHidden(true)
                }
                Text(sheet.endSeries == nil
                     ? "Close “\(sheet.title)”"
                     : "End the series “\(sheet.endSeries?.seriesName ?? sheet.title)”")
                    .font(.title3.weight(.semibold))
                    .lineLimit(2)
            }
            Text(subtitle)
                .font(.callout)
                .foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .combine)
    }

    private var subtitle: String {
        switch sheet.phase {
        case .planning: return "Working out what this would do…"
        case .ready(let plan):
            if sheet.endSeries != nil {
                return "This is not reversible. Read what happens, then type the "
                    + "series name to enable the button."
            }
            return plan.touchesGitHub
                ? "Review the plan below. Nothing has been sent yet."
                : "This task has no GitHub repository, so closing it is local only."
        case .planFailed: return "The preview could not be produced."
        case .closing: return "Closing… do not quit."
        case .failed: return sheet.endSeries == nil
            ? "The close failed. The task is still open."
            : "It failed. The series is still running."
        case .succeeded: return sheet.endSeries == nil ? "Closed." : "The series has ended."
        }
    }

    // MARK: - Body

    @ViewBuilder
    private var content: some View {
        switch sheet.phase {
        case .planning:
            HStack(spacing: 10) {
                ProgressView().controlSize(.small)
                Text("Running a dry run — no changes are made.")
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, minHeight: 120)

        case .ready(let plan):
            if let gate = sheet.endSeries {
                // The consequence prose comes *first*, above the plan, because
                // on a recurrent task the plan is the thing that understates it.
                VStack(alignment: .leading, spacing: 14) {
                    EndSeriesConsequences(gate: gate)
                    PlanPreview(plan: plan, planIsMisleading: true)
                    EndSeriesConfirmationField(gate: gate)
                }
            } else {
                PlanPreview(plan: plan)
            }

        case .planFailed(let message):
            failureBox(title: "Could not plan the close", message: message, lines: [])

        case .closing(_, let plan, let lines):
            VStack(alignment: .leading, spacing: 12) {
                ProgressLog(lines: lines, running: true)
                Text("\(plan.rows.count) sprint\(plan.rows.count == 1 ? "" : "s") being "
                     + "reconciled. GitHub calls are in flight.")
                    .font(.caption).foregroundStyle(.secondary)
            }

        case .failed(let message, let code, let lines):
            failureBox(title: code.map { "The close failed (\($0))" } ?? "The close failed",
                       message: message, lines: lines)

        case .succeeded(let lines, let outcome):
            // **Not an unconditional success message.** `wt.close_task` marks the
            // task done even when the GitHub half fails, and the daemon still
            // reports the operation `completed`. Rendering "its issues are up to
            // date" over that was a real, observed lie: an End Series run whose
            // `gh issue close` failed left the binding `state: "open"` and said
            // everything was fine.
            let summary = (outcome ?? .init(success: true))
                .summary(expectedIssueClose: sheet.expectsIssueClose)
            VStack(alignment: .leading, spacing: 12) {
                Label(summary.message,
                      systemImage: summary.isWarning
                        ? "exclamationmark.triangle.fill" : "checkmark.circle")
                    .foregroundStyle(summary.isWarning ? .orange : .green)
                if summary.isWarning {
                    Text("The tracker and GitHub now disagree. Re-run Sync Sprints, "
                         + "or close the issue on GitHub by hand.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                ProgressLog(lines: lines, running: false)
            }
        }
    }

    private func failureBox(title: String, message: String, lines: [String]) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Label(title, systemImage: "exclamationmark.triangle")
                .font(.headline)
                .foregroundStyle(.orange)
            Text(message)
                .textSelection(.enabled)
                .font(.callout)
            Text("The task has not been marked done. A failed reconcile aborts the "
                 + "close on purpose, so its hours cannot be mis-reported.")
                .font(.caption)
                .foregroundStyle(.secondary)
            if !lines.isEmpty { ProgressLog(lines: lines, running: false) }
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
        if case .ready(let plan) = sheet.phase, plan.issuesToCreate > 0 {
            Label {
                // Concatenated `Text`, not markdown in a string: `Text`'s
                // markdown parsing only applies to *literals*, so an
                // interpolated "**…**" renders the asterisks. Caught by
                // rendering the sheet with `ImageRenderer`.
                Text("\(plan.issuesToCreate) GitHub issue"
                     + "\(plan.issuesToCreate == 1 ? " will be" : "s will be") created. ")
                + Text("This cannot be undone.").bold()
            } icon: {
                Image(systemName: "exclamationmark.triangle.fill")
            }
            .font(.callout)
            .foregroundStyle(.orange)
            .accessibilityLabel("Warning: \(plan.issuesToCreate) GitHub issues will be "
                                + "created. This cannot be undone.")
        } else if case .ready(let plan) = sheet.phase, plan.touchesGitHub {
            Label("Existing GitHub issues will be updated and closed. This cannot be undone.",
                  systemImage: "exclamationmark.triangle")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private var buttons: some View {
        switch sheet.phase {
        case .planning:
            Button("Cancel") { store.dismissCloseSheet() }
                .keyboardShortcut(.cancelAction)

        case .ready(let plan):
            Button("Cancel") { store.dismissCloseSheet() }
                .keyboardShortcut(.cancelAction)
            if let gate = sheet.endSeries {
                // **No `.keyboardShortcut(.defaultAction)`.** A default button
                // fires on Return, which is exactly how an irreversible action
                // gets triggered by someone dismissing a dialog they did not
                // read. Cancel keeps Escape; this one has to be clicked.
                Button("End Series", role: .destructive) {
                    _Concurrency.Task { await store.confirmClose() }
                }
                .disabled(!sheet.allowsConfirmation(plan))
                .help(gate.isSatisfied
                      ? "Ends the recurrence and closes \(gate.issue ?? "no issue")"
                      : "Type the series name above to enable this")
            } else {
                Button(plan.issuesToCreate > 0 ? "Create Issues and Close" : "Close Task") {
                    _Concurrency.Task { await store.confirmClose() }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(!plan.isActionable)
                .help(plan.isActionable
                      ? "Runs the reconcile and closes the GitHub issues"
                      : "The dry run failed, so the real close would fail too")
            }

        case .planFailed:
            Button("Cancel") { store.dismissCloseSheet() }
                .keyboardShortcut(.cancelAction)

        case .closing:
            ProgressView().controlSize(.small)
            Button("Close in Background") { store.dismissCloseSheet() }
                .help("The close keeps running; the board updates when it finishes")

        case .failed, .succeeded:
            Button("Done") { store.dismissCloseSheet() }
                .keyboardShortcut(.defaultAction)
        }
    }
}

// MARK: - Plan preview

/// The per-sprint table. One row per sprint the close must account for, so a
/// sprint whose hours are *not* touched still gets a line — the sheet's job is
/// to leave nothing invisible.
///
/// Internal rather than private so a row can be rendered on its own with
/// `ImageRenderer`; the sheet's `ScrollView` is AppKit-backed and renders
/// blank, which is how this table was reviewed without Screen Recording.
struct PlanPreview: View {
    let plan: ClosePlanResponse
    /// True on the End Series path, where the plan routinely says "no change"
    /// while the close still closes the live issue — so the table needs a
    /// caveat rather than being read as the whole story.
    var planIsMisleading = false
    @State private var showsRawLines = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if planIsMisleading {
                // Two `Text`s rather than one string with `*bindings*` in it.
                // `Text`'s markdown parsing only applies to a *literal*, and
                // `"a" + "b"` resolves to `Text(String)` — so the asterisks
                // rendered verbatim. Same trap the warning row hit in Phase 4,
                // caught here by looking at the sheet on screen.
                (Text("Per-sprint reconcile plan. This table covers the ")
                 + Text("bindings").italic()
                 + Text(" only — the issue close above is not one of its rows."))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if plan.rows.isEmpty {
                Label(planIsMisleading
                      ? "No binding changes — but the series still ends and its "
                        + "issue still closes."
                      : "Nothing to reconcile — the task is closed locally only.",
                      systemImage: "info.circle")
                    .foregroundStyle(planIsMisleading ? .orange : .secondary)
                    .font(.callout)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        ForEach(plan.rows) { row in
                            PlanRowView(row: row)
                            Divider()
                        }
                    }
                }
                .frame(minHeight: 120, maxHeight: 260)
            }

            if let error = plan.plan.error {
                Label(error, systemImage: "xmark.octagon")
                    .font(.callout).foregroundStyle(.red)
            }
            if !plan.plan.unbillable.isEmpty {
                Label("Hours are withheld: this task has time in "
                      + plan.plan.unbillable.map {
                          "\($0.sprint ?? "a sprint") (\(Duration.format(minutes: $0.minutes)))"
                      }.joined(separator: ", ")
                      + " with no issue to report on. Narrowing the other issues "
                      + "would delete that time from the project.",
                      systemImage: "exclamationmark.circle")
                    .font(.caption).foregroundStyle(.orange)
            }
            if plan.plan.unassignedMinutes > 0 {
                Text("\(Duration.format(minutes: plan.plan.unassignedMinutes)) of logs fall "
                     + "outside every known sprint and will not be reported.")
                    .font(.caption).foregroundStyle(.secondary)
            }

            DisclosureGroup("Plan as the CLI prints it", isExpanded: $showsRawLines) {
                // The daemon sends `wt._reconcile_plan_lines()` verbatim, so the
                // sheet and `wt sync-sprints` can never disagree about a plan.
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

// MARK: - The End Series gate (plan §9)

/// What ending a series actually does, in prose, above the plan table.
///
/// This block exists because the plan table **understates the danger**. A
/// `close/plan` on a perpetual task plans nothing (or one hours update): the
/// reconcile emits a `close` op only for a sprint that has *ended*, and a
/// perpetual task's current binding is by definition the current sprint. So the
/// table reads "no change" while the close ends the recurrence and closes the
/// live issue. Measured against all seven of the owner's recurrent tasks.
private struct EndSeriesConsequences: View {
    let gate: EndSeriesConfirmation

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("This cannot be undone. There is no reopen path.",
                  systemImage: "exclamationmark.triangle.fill")
                .font(.callout.weight(.semibold))
                .foregroundStyle(.red)
            VStack(alignment: .leading, spacing: 5) {
                ForEach(gate.consequenceLines, id: \.self) { line in
                    HStack(alignment: .firstTextBaseline, spacing: 7) {
                        Image(systemName: "arrow.right")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                        Text(line).font(.callout)
                    }
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.red.opacity(0.08), in: .rect(cornerRadius: 8))
        .overlay {
            RoundedRectangle(cornerRadius: 8).strokeBorder(.red.opacity(0.35), lineWidth: 1)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Warning, this cannot be undone. "
                            + gate.consequenceLines.joined(separator: ". "))
    }
}

/// The typed confirmation field.
///
/// A checkbox or a second click would both be satisfiable without reading; this
/// requires reproducing the series name, so the name has to be looked at. The
/// gate itself lives in `CloseSheetState.allowsConfirmation` — this view only
/// collects the text.
private struct EndSeriesConfirmationField: View {
    let gate: EndSeriesConfirmation
    @Environment(Store.self) private var store
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("To confirm, type ") + Text(gate.seriesName).bold().monospaced()
            TextField("Series name", text: Binding(
                get: { gate.typed },
                set: { store.updateEndSeriesConfirmation($0) }))
                .textFieldStyle(.roundedBorder)
                .focused($focused)
                // Deliberately **not** `.onSubmit { confirm() }`: Return in this
                // field must not run the action either.
                .frame(maxWidth: 420)
            if let hint = gate.validationHint {
                Text(hint).font(.caption).foregroundStyle(.secondary)
            } else {
                Label("Matches. The End Series button is now enabled.",
                      systemImage: "checkmark.circle.fill")
                    .font(.caption).foregroundStyle(.orange)
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Type the series name \(gate.seriesName) to confirm")
    }
}

struct PlanRowView: View {
    let row: ClosePlanRow

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Text(row.sprintTitle)
                .font(.callout.weight(.medium))
                .frame(width: 96, alignment: .leading)
            Text(row.minutes.map { Duration.format(minutes: $0) } ?? "—")
                .font(.callout.monospacedDigit())
                .frame(width: 72, alignment: .trailing)
            Image(systemName: "arrow.right")
                .font(.caption2).foregroundStyle(.tertiary)
            Text(row.issue ?? "(no issue)")
                .font(.callout.monospaced())
                .foregroundStyle(row.issue == nil ? .secondary : .primary)
                .frame(width: 240, alignment: .leading)
                .lineLimit(1).truncationMode(.head)
            VStack(alignment: .leading, spacing: 2) {
                if row.isNoOp {
                    Text("no change").foregroundStyle(.tertiary)
                }
                ForEach(row.actions, id: \.self) { action in
                    Text(action)
                        .foregroundStyle(colorFor(action))
                }
            }
            .font(.callout)
            Spacer(minLength: 0)
        }
        .padding(.vertical, 6)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityText)
    }

    private func colorFor(_ action: String) -> Color {
        if action.hasPrefix("CREATE") { return .orange }
        if action.hasPrefix("NO ISSUE") || action.hasPrefix("hours withheld") { return .orange }
        return .primary
    }

    private var accessibilityText: String {
        var parts = [row.sprintTitle]
        if let minutes = row.minutes { parts.append(Duration.format(minutes: minutes)) }
        parts.append(row.issue ?? "no issue")
        parts.append(row.actions.isEmpty ? "no change" : row.actions.joined(separator: ", "))
        return parts.joined(separator: ", ")
    }
}

/// The SSE-fed progress list. The daemon streams
/// `wt._reconcile_outcome_lines()`, one `progress` event per line.
private struct ProgressLog: View {
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
