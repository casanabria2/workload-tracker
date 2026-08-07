import Foundation

// Plan §9 — the recurrent shelf's row actions, and the gates in front of the
// two that touch GitHub irreversibly.
//
// Everything in this file is pure and SwiftUI-free, for the same reason
// `BoardDrop.swift` is. Three of the five actions are writes and two of them
// call `gh` against the owner's real org, so "which action needs which
// confirmation" is the substance of this phase — and a rule that can only be
// exercised by driving a UI is a rule that does not get exercised.
//
// The board learned this the hard way in Phase 4: a drag bug survived 95 green
// tests because the tests covered pure functions and never the wiring. Here the
// pure table is the *gate*, not a description of one — `Store` cannot reach the
// irreversible call except through `ShelfAction.endSeries`'s satisfied
// confirmation, so the table is load-bearing rather than advisory.

// MARK: - The action table

/// One row action on the recurrent shelf.
///
/// Ordering is deliberate and asserted in tests: see `menuOrder`.
enum ShelfAction: String, CaseIterable, Sendable, Hashable, Identifiable {
    /// Start (or switch) the timer onto this perpetual task.
    case startTimer
    /// Add a manual log entry.
    case logTime
    /// Open the task's current-binding issue in the browser. Read-only.
    case openIssue
    /// `reconcile_task_sprints` — **mints the new sprint's issue and closes the
    /// ended one.** Gated by a dry-run preview.
    case syncSprints
    /// `close_task` on a perpetual task: **ends the recurrence and closes its
    /// live GitHub issue.** There is no reopen path. Gated by a typed
    /// confirmation.
    case endSeries

    var id: String { rawValue }

    /// The menu title. `…` marks the ones that open a sheet rather than acting.
    var title: String {
        switch self {
        case .startTimer: "Start Timer"
        case .logTime: "Log Time…"
        case .openIssue: "Open Issue"
        case .syncSprints: "Sync Sprints…"
        case .endSeries: "End Series…"
        }
    }

    var systemImage: String {
        switch self {
        case .startTimer: "play.circle"
        case .logTime: "clock.badge.checkmark"
        case .openIssue: "link"
        case .syncSprints: "arrow.triangle.2.circlepath"
        case .endSeries: "xmark.octagon"
        }
    }

    /// Whether the action mutates the tracker's data file.
    var isWrite: Bool {
        switch self {
        case .openIssue: false
        case .startTimer, .logTime, .syncSprints, .endSeries: true
        }
    }

    /// Whether the action can issue an **irreversible** `gh` call
    /// (`issue create` / `issue close`).
    ///
    /// `startTimer` and `logTime` write only the local data file — the daemon's
    /// post-stop hours sync is a `gh project` field update, not an issue
    /// mutation — so neither is in here.
    var touchesGitHubIrreversibly: Bool {
        switch self {
        case .syncSprints, .endSeries: true
        case .startTimer, .logTime, .openIssue: false
        }
    }

    /// How the action must be confirmed before anything is sent.
    var gate: ShelfActionGate {
        switch self {
        case .startTimer, .openIssue: .none
        // Not a GitHub write, but it does append to the owner's irreplaceable
        // work history, so the amount is typed and reviewed rather than guessed.
        case .logTime: .sheet
        case .syncSprints: .dryRunPreview
        case .endSeries: .typedConfirmation
        }
    }

    /// Whether a keyboard shortcut may be attached to this action.
    ///
    /// **`endSeries` must never have one.** A shortcut is a way to invoke an
    /// action without reading its name, which is precisely the accident this
    /// gate exists to prevent.
    var allowsKeyboardShortcut: Bool { self != .endSeries }

    /// Position in the context menu and the Task menu, low to high.
    ///
    /// `endSeries` is last **and** separated (`isSeparatedInMenu`), so it is
    /// never the item the pointer lands on when a menu opens and never adjacent
    /// to a benign action. `syncSprints` sits above it for the same reason: the
    /// two dangerous items are at the bottom, in ascending order of damage.
    var menuOrder: Int {
        switch self {
        case .startTimer: 0
        case .logTime: 1
        case .openIssue: 2
        case .syncSprints: 3
        case .endSeries: 4
        }
    }

    /// Whether a divider precedes this item.
    var isSeparatedInMenu: Bool {
        switch self {
        case .syncSprints, .endSeries: true
        default: false
        }
    }

    /// The actions in menu order.
    static var menu: [ShelfAction] {
        allCases.sorted { $0.menuOrder < $1.menuOrder }
    }

    /// Whether the action makes sense for `task` at all, and why not when it
    /// does not. Returning a reason rather than a `Bool` is what lets the menu
    /// disable an item *and* explain it in `.help`.
    func availability(for task: TrackerTask, isTimerRunning: Bool) -> ShelfActionAvailability {
        switch self {
        case .startTimer:
            return isTimerRunning
                ? .unavailable("The timer is already running on this task.")
                : .available
        case .logTime:
            return .available
        case .openIssue:
            return task.currentIssue == nil
                ? .unavailable("This task has no linked GitHub issue.")
                : .available
        case .syncSprints:
            return .available
        case .endSeries:
            return .available
        }
    }
}

/// Whether a row action applies, and why not.
enum ShelfActionAvailability: Equatable, Sendable {
    case available
    case unavailable(String)

    var isAvailable: Bool { self == .available }
    var reason: String? {
        if case .unavailable(let why) = self { return why }
        return nil
    }
}

/// How much ceremony stands between the user and the action running.
enum ShelfActionGate: Equatable, Sendable {
    /// Acts immediately. Only for reversible, local actions.
    case none
    /// Opens a sheet that collects input; the sheet's button is the commit.
    case sheet
    /// Opens a sheet showing a **write-free dry run** of exactly what would
    /// happen, and acts only on explicit confirmation.
    case dryRunPreview
    /// `dryRunPreview` **plus** the user typing the series name, with no default
    /// button. The strongest gate in the app.
    case typedConfirmation

    /// Whether the gate requires the user to read a preview of real consequences
    /// before the action can be confirmed.
    var requiresPreview: Bool {
        self == .dryRunPreview || self == .typedConfirmation
    }
}

// MARK: - The End Series gate

/// The typed confirmation in front of ending a recurrent series.
///
/// **This is the most dangerous action in the app.** Closing a recurrent task
/// ends the recurrence *and* closes its live GitHub issue, and there is no
/// reopen path — `wt.py` has no `gh issue reopen`, so the only recovery is
/// manual work on GitHub. CLAUDE.md warns about it explicitly.
///
/// Worse, the ordinary §7.1 close preview **understates it**. Measured against
/// the owner's real data: a `close/plan` for every one of the seven recurrent
/// tasks plans either nothing at all or a single `hours` update, because the
/// reconcile only emits a `close` op for a sprint that has *ended* — and a
/// perpetual task's current binding is, by definition, the current sprint. So
/// the plan table reads "no change" while the close would close
/// `grafana/field-eng#6299` and set the project's Status to Done. That gap is
/// exactly what this type exists to fill: it states the consequence in prose,
/// names the issue, and refuses to be satisfied by a click.
struct EndSeriesConfirmation: Equatable, Sendable {
    /// The series being ended, as the confirmation names it. This is what the
    /// user must type.
    let seriesName: String
    /// The live issue that will be closed, `owner/repo#n`, or `nil` when the
    /// task has none.
    let issue: String?
    /// How many per-sprint bindings the series has accumulated — the scale of
    /// what is being retired.
    let bindingCount: Int
    /// What the user has typed so far.
    var typed: String = ""

    init(seriesName: String, issue: String?, bindingCount: Int, typed: String = "") {
        self.seriesName = seriesName
        self.issue = issue
        self.bindingCount = bindingCount
        self.typed = typed
    }

    /// Builds the gate for a task.
    init(task: TrackerTask, seriesName: String? = nil) {
        self.init(seriesName: seriesName ?? task.title,
                  issue: task.currentIssue,
                  bindingCount: task.sprintIssues.count)
    }

    /// Whether the typed text matches the series name.
    ///
    /// Whitespace-insensitive at the ends and case-insensitive, because the
    /// point is to make the user *read and reproduce the name*, not to test
    /// their shift key. Interior whitespace is normalised the same way
    /// `wt.recurrent_series_for_title()` normalises a title, so a double space
    /// pasted from the table still matches.
    var isSatisfied: Bool {
        !seriesName.isEmpty && Self.normalize(typed) == Self.normalize(seriesName)
    }

    /// What to show under the field while it does not match.
    var validationHint: String? {
        guard !isSatisfied else { return nil }
        if Self.normalize(typed).isEmpty {
            return "Type the series name exactly to enable the button."
        }
        return "That does not match “\(seriesName)”."
    }

    /// Collapse runs of whitespace, trim, case-fold — the Swift equivalent of
    /// `" ".join(title.split()).lower()`, which is how `wt.py` normalises a
    /// series title.
    static func normalize(_ value: String) -> String {
        value.split(whereSeparator: \.isWhitespace)
            .joined(separator: " ")
            .lowercased()
    }

    /// The prose consequence, assembled once so the sheet and its accessibility
    /// label cannot drift apart.
    var consequenceLines: [String] {
        var lines = [
            "The recurrence ends. This series will stop getting a new issue each sprint."
        ]
        if let issue {
            lines.append("\(issue) will be closed on GitHub. There is no reopen path.")
        } else {
            lines.append("The task has no linked issue, so nothing is closed on GitHub.")
        }
        if bindingCount > 0 {
            lines.append(bindingCount == 1
                         ? "1 per-sprint binding stays on the task; its hours are "
                           + "not rewritten."
                         : "\(bindingCount) per-sprint bindings stay on the task; "
                           + "their hours are not rewritten.")
        }
        return lines
    }
}

// MARK: - Series resolution

/// How the shelf's Series column resolves a task to its canonical series.
///
/// **The snapshot does not carry the series name.** `wt_api.task_view()` emits
/// no `recurrent_series` key, so there is nothing for Swift to read — verified
/// against `wt_api.py` at `e11f45d` and against a live `/v1/snapshot`.
///
/// The alias table is deliberately **not** reimplemented here. CLAUDE.md is
/// explicit — *"Don't group recurring series by fuzzy title matching — use
/// `RECURRENT_SERIES_ALIASES` / `recurrent_series_for_title()`. Real titles
/// drifted three ways for one series"* — and a second copy of that table in
/// Swift would be a copy that silently goes stale. Two of the owner's seven
/// recurrent tasks (`1:1 with TomD`, `Alex KC 1:1 calls - casanabria`) are not
/// in the table at all, so even a faithful copy would resolve only five of
/// seven; guessing the other two is precisely the fuzzy matching the rule
/// forbids.
///
/// So this type reads a field the daemon *may* send and reports honestly when
/// it does not. When `task_view()` grows `"recurrent_series"`, the column
/// populates with no Swift change.
enum RecurrentSeries {

    /// The canonical series name for a task, or `nil` when the daemon did not
    /// supply one.
    static func canonicalName(for task: TrackerTask) -> String? {
        guard let raw = task.recurrentSeries?.trimmingCharacters(in: .whitespaces),
              !raw.isEmpty else { return nil }
        return raw
    }

    /// What the Series column shows.
    static func displayName(for task: TrackerTask) -> String {
        canonicalName(for: task) ?? "—"
    }

    /// Whether *any* task in the shelf carries a series name, i.e. whether the
    /// daemon supports the field at all. Drives the column's footnote.
    static func isSupported(by tasks: [TrackerTask]) -> Bool {
        tasks.contains { canonicalName(for: $0) != nil }
    }

    /// Why the column is empty, for the header tooltip. `nil` when supported.
    static func unsupportedReason(for tasks: [TrackerTask]) -> String? {
        guard !isSupported(by: tasks) else { return nil }
        return "The daemon's snapshot does not include a series name yet. "
            + "wt_api.task_view() would need to emit recurrent_series_for_title(); "
            + "the alias table is deliberately not duplicated in Swift."
    }

    /// Groups tasks by canonical series. Tasks with no series name each form
    /// their own group keyed by id, so nothing is silently merged.
    ///
    /// Post-`_migrate_recurrent_series_to_bindings` the owner's data has exactly
    /// one task per series, so every group here is a singleton — this exists so
    /// that a future daemon that *does* send the name cannot surprise the shelf
    /// with two rows of one series.
    static func groups(in tasks: [TrackerTask]) -> [String: [TrackerTask]] {
        Dictionary(grouping: tasks) { canonicalName(for: $0) ?? "\u{1}id:\($0.id)" }
    }
}
