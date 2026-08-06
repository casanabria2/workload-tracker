import SwiftUI

/// One board card. Draggable and selectable from Phase 4; the drag payload and
/// the drop rules live in `BoardDrop.swift`, not here.
struct TaskCardView: View {
    let task: TrackerTask
    let roleLabel: String
    let roleColor: Color
    /// Non-nil when this task's timer is running; drives the accent border and
    /// the live `m:ss` label.
    let elapsed: TimeInterval?
    /// The sprint the board is currently reading hours for, for the badge.
    let currentSprint: Sprint?
    /// The keyboard selection, which `⌘←`/`⌘→` act on.
    var isSelected: Bool = false
    /// An optimistic status change is in flight for this card. Drawn dimmed so
    /// "moved" and "moved and confirmed" are not the same picture — a rollback
    /// has to look like something reverting, not like a card teleporting.
    var isPending: Bool = false

    private var isRunning: Bool { elapsed != nil }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            header
            HStack(spacing: 8) {
                RoleChip(label: roleLabel, color: roleColor)
                Spacer(minLength: 4)
                hours
            }
            if !badges.isEmpty {
                FlowRow(spacing: 5) {
                    ForEach(badges, id: \.text) { badge in
                        Badge(text: badge.text, symbol: badge.symbol)
                    }
                }
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.background.secondary, in: .rect(cornerRadius: 8))
        .overlay {
            RoundedRectangle(cornerRadius: 8)
                .strokeBorder(borderColor, lineWidth: isRunning || isSelected ? 2 : 1)
        }
        // Bottom-trailing, not top: the top-right corner is where a running
        // timer's m:ss label lives, and the two collided.
        .overlay(alignment: .bottomTrailing) {
            if isPending {
                ProgressView()
                    .controlSize(.small)
                    .padding(6)
                    .accessibilityLabel("Move in progress")
            }
        }
        .opacity(isPending ? 0.6 : 1)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityDescription)
        .accessibilityAddTraits(isSelected ? [.isSelected] : [])
    }

    /// Running beats selected: a live timer is a fact about the data, selection
    /// is a fact about the cursor.
    private var borderColor: Color {
        if isRunning { return .accentColor }
        if isSelected { return .secondary }
        return .primary.opacity(0.08)
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(task.title)
                .font(.headline)
                .lineLimit(2)
                .multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
            if let elapsed {
                Text(Duration.formatElapsed(elapsed))
                    .font(.caption.monospacedDigit().weight(.semibold))
                    .foregroundStyle(Color.accentColor)
                    .accessibilityLabel("Running, \(Duration.formatElapsed(elapsed))")
            }
        }
    }

    private var hours: some View {
        // `reportable_mins` is the sprint-filtered figure — the one that gets
        // reported to GitHub. `logged_mins` is the task's lifetime total, shown
        // alongside only when the two differ (i.e. a cross-sprint task).
        HStack(spacing: 4) {
            // `formatZeroed`, not `format`: a task worked only in a past sprint
            // has 0 reportable minutes and a non-zero total, and "— of 10m"
            // reads like a bug where "0m of 10m" reads like the fact it is.
            Text(Duration.formatZeroed(minutes: task.reportableMins))
                .font(.caption.monospacedDigit())
            if task.loggedMins > task.reportableMins + 0.01 {
                Text("of \(Duration.format(minutes: task.loggedMins))")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .foregroundStyle(.secondary)
        .accessibilityLabel("\(Duration.format(minutes: task.reportableMins)) reportable, "
                            + "\(Duration.format(minutes: task.loggedMins)) total")
    }

    private struct BadgeSpec {
        let text: String
        let symbol: String
    }

    private var badges: [BadgeSpec] {
        var out: [BadgeSpec] = []
        if let issue = task.currentIssue {
            out.append(BadgeSpec(text: shortIssue(issue), symbol: "number.circle"))
        }
        if task.sprintIssues.count > 1 {
            out.append(BadgeSpec(text: "\(task.sprintIssues.count) sprints",
                                 symbol: "calendar.badge.clock"))
        } else if let sprint = task.sprintsWithTime.last?.sprintTitle {
            out.append(BadgeSpec(text: sprint, symbol: "calendar"))
        } else if let start = task.startSprint {
            out.append(BadgeSpec(text: start, symbol: "calendar"))
        }
        if let activity = task.activity, !activity.isEmpty {
            out.append(BadgeSpec(text: activity, symbol: "tag"))
        }
        if !task.tabs.isEmpty {
            out.append(BadgeSpec(text: "\(task.tabs.count) tabs", symbol: "safari"))
        }
        if task.localFolder != nil {
            out.append(BadgeSpec(text: "folder", symbol: "terminal"))
        }
        return out
    }

    /// `owner/some-repo#412` → `#412`, with the repo kept only when
    /// it differs from the task's own. Board cards are narrow; the full ref is
    /// in the accessibility label and (Phase 4) the inspector.
    private func shortIssue(_ ref: String) -> String {
        guard let hash = ref.lastIndex(of: "#") else { return ref }
        let number = String(ref[hash...])
        let repo = String(ref[ref.startIndex..<hash])
        if repo == task.githubRepo { return number }
        return "\(repo.split(separator: "/").last.map(String.init) ?? repo)\(number)"
    }

    private var accessibilityDescription: String {
        var parts = [task.title, "role \(roleLabel)",
                     "\(Duration.format(minutes: task.reportableMins)) reportable"]
        if let issue = task.currentIssue { parts.append("issue \(issue)") }
        if let elapsed { parts.append("timer running, \(Duration.formatElapsed(elapsed))") }
        if let sprint = currentSprint?.displayName { parts.append("current sprint \(sprint)") }
        return parts.joined(separator: ", ")
    }
}

/// A small labelled pill. Text always present — never a bare colored dot.
struct Badge: View {
    let text: String
    var symbol: String?

    var body: some View {
        HStack(spacing: 3) {
            if let symbol {
                Image(systemName: symbol).symbolRenderingMode(.hierarchical)
            }
            Text(text).lineLimit(1)
        }
        .font(.caption2)
        .foregroundStyle(.secondary)
        .padding(.horizontal, 5)
        .padding(.vertical, 2)
        .background(.quaternary.opacity(0.5), in: .capsule)
    }
}

/// A minimal wrapping HStack. Badges are variable-width and a card is narrow, so
/// they need to wrap; `Layout` does this without a third-party dependency.
struct FlowRow: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews,
                      cache: inout ()) -> CGSize {
        let maxWidth = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > 0, x + size.width > maxWidth {
                x = 0
                y += rowHeight + spacing
                rowHeight = 0
            }
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
        return CGSize(width: proposal.width ?? x, height: y + rowHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize,
                       subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, rowHeight: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > bounds.minX, x + size.width > bounds.maxX {
                x = bounds.minX
                y += rowHeight + spacing
                rowHeight = 0
            }
            subview.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }
}
