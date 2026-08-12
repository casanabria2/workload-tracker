import Foundation

// Plan §10 — the Gantt's data model.
//
// SwiftUI-free and pure, for the same reason `TaskFilter.swift` and
// `BoardDrop.swift` are: the substance of this phase is *what a bar is* at each
// zoom level, where the x-range comes from, and how the 29 timestamp-less logs
// are represented. A chart is exactly the kind of code that renders wrong while
// every test passes, so as much of it as possible is made assertable without
// drawing anything.
//
// **Read-only.** Nothing in this file or in `TimelineView` writes; the Gantt
// displays logged time and never edits it.

// MARK: - Zoom

/// The four zoom levels (plan §10). Two things vary with zoom, and they are not
/// the same thing:
///
/// 1. **What a bar is** (`granularity`) — one bar per *log entry* at Day/Week,
///    one bar per *task* at Sprint/Quarter.
/// 2. **How much time is on screen** (`windowLength`).
enum TimelineZoom: String, CaseIterable, Codable, Sendable, Identifiable {
    case day
    case week
    case sprint
    case quarter

    var id: String { rawValue }

    /// Ordered widest-last, which is what `zoomedIn`/`zoomedOut` walk.
    static let ordered: [TimelineZoom] = [.day, .week, .sprint, .quarter]

    var displayName: String {
        switch self {
        case .day: "Day"
        case .week: "Week"
        case .sprint: "Sprint"
        case .quarter: "Quarter"
        }
    }

    /// What one bar stands for.
    enum Granularity: Sendable, Equatable {
        /// One bar per log entry, at its real `started_at`→`ended_at`.
        case logEntry
        /// One bar per task, `min(log date)` → `max(log date)`.
        case taskSpan
    }

    var granularity: Granularity {
        switch self {
        case .day, .week: .logEntry
        case .sprint, .quarter: .taskSpan
        }
    }

    /// Sprint boundaries are drawn at the two zooms wide enough to contain more
    /// than one (plan §10).
    var showsSprintBoundaries: Bool { granularity == .taskSpan }

    /// Seconds of time the window covers, before it is clamped to the domain.
    ///
    /// `.sprint` and `.quarter` defer to the domain when the Sprint facet set it
    /// (§8.5): selecting Sprint 105 and zooming to Sprint should show that
    /// sprint, whatever its length, not a nominal fortnight starting elsewhere.
    func windowLength(domainLength: TimeInterval, isSprintScoped: Bool) -> TimeInterval {
        switch self {
        case .day: 24 * 3600
        case .week: 7 * 24 * 3600
        case .sprint: isSprintScoped ? domainLength : 14 * 24 * 3600
        case .quarter: max(91 * 24 * 3600, isSprintScoped ? domainLength : 0)
        }
    }

    var zoomedIn: TimelineZoom? {
        guard let i = Self.ordered.firstIndex(of: self), i > 0 else { return nil }
        return Self.ordered[i - 1]
    }

    var zoomedOut: TimelineZoom? {
        guard let i = Self.ordered.firstIndex(of: self),
              i < Self.ordered.count - 1 else { return nil }
        return Self.ordered[i + 1]
    }
}

// MARK: - Bars

/// One drawn bar.
///
/// `kind` is the honesty channel: `.approximate` is the case the plan singles
/// out, and it must never be collapsed into `.session`.
struct TimelineBar: Identifiable, Hashable, Sendable {
    /// What the bar stands for.
    enum Kind: Sendable, Hashable {
        /// A log entry with a real `started_at`/`ended_at` span.
        case session
        /// A log entry with **no wall clock** for the work — 29 of the owner's
        /// 419. It carries `at` (when it was recorded), so the bar is drawn at
        /// `log_effective_date` with a width of `minutes` and is **hatched**.
        /// Inventing a plausible time of day would be a lie the chart tells
        /// silently (plan §10).
        case approximate
        /// A whole task's span at Sprint/Quarter zoom.
        case taskSpan
        /// The running timer, growing against `now`.
        case running

        /// Whether the bar's horizontal position is exact.
        var isExactlyPlaced: Bool { self != .approximate }
    }

    let id: String
    let taskId: String
    let taskTitle: String
    let roleId: String?
    let roleLabel: String
    let kind: Kind
    let start: Date
    let end: Date
    /// Logged minutes the bar represents. For `.taskSpan` this is the task's
    /// in-range total, which is **not** the bar's width — a span covers the
    /// calendar time work was spread over, not the time worked.
    let minutes: Double
    /// The log's note, or a summary for a span.
    let detail: String
    let sprintTitle: String?
    /// Log entries folded into this bar (1 for a session).
    let entryCount: Int
    /// How many of those had no wall clock.
    let approximateCount: Int

    /// The row this bar sits on. One row per task at every zoom level.
    var rowId: String { taskId }

    /// True when the bar's drawn position is approximate in whole or in part.
    var isApproximate: Bool { approximateCount > 0 }

    /// What VoiceOver reads, and the substance of the tooltip.
    var accessibilityDescription: String {
        var parts = ["\(taskTitle), \(Duration.format(minutes: minutes))"]
        if let sprintTitle { parts.append(sprintTitle) }
        if isApproximate {
            parts.append(entryCount == 1
                         ? "approximate time of day"
                         : "\(approximateCount) of \(entryCount) entries "
                           + "have an approximate time of day")
        }
        if kind == .running { parts.append("timer running") }
        return parts.joined(separator: ", ")
    }
}

// MARK: - Rows

/// One y-axis row: a task, sectioned under its role.
struct TimelineRow: Identifiable, Hashable, Sendable {
    let id: String
    let title: String
    let roleId: String?
    let roleLabel: String
    /// The role's index in `snapshot.roles`, so rows sort into role sections in
    /// the sidebar's order rather than alphabetically by role name.
    let roleOrder: Int
    let minutes: Double
    let approximateMinutes: Double
    /// True when this row is the first of its role, so the view can draw the
    /// section break. Rows are ordered, so this is a property of the sequence.
    var startsRoleSection: Bool
}

/// One entry in the summary strip above the chart.
struct TimelineRoleTotal: Identifiable, Hashable, Sendable {
    let roleId: String?
    let label: String
    let order: Int
    let minutes: Double
    let taskCount: Int

    var id: String { roleId ?? "\u{1}norole" }
}

// MARK: - Range

/// Where the visible x-range came from, so the view can say so rather than
/// leaving the axis unexplained.
enum TimelineRangeSource: Equatable, Sendable {
    /// Plan §8.5: the Sprint facet also sets the viewport.
    case sprintFacet(titles: [String])
    /// The span of the filtered tasks' logged time.
    case loggedTime
    /// Nothing is logged and no sprint is selected, so the current sprint frames
    /// an otherwise undefined axis.
    case currentSprint(title: String)
    /// Not even a current sprint — a window around now.
    case aroundNow

    var explanation: String {
        switch self {
        case .sprintFacet(let titles):
            titles.isEmpty ? "sprint filter"
                : "showing " + ListFormatter.localizedString(byJoining: titles)
        case .loggedTime: "showing the range with logged time"
        case .currentSprint(let title): "showing \(title)"
        case .aroundNow: "showing the last two weeks"
        }
    }
}

// MARK: - The built chart

/// Everything `TimelineView` draws, computed in one pass.
struct TimelineData: Sendable {
    let zoom: TimelineZoom
    /// Always non-degenerate: `upperBound > lowerBound` by construction, so no
    /// axis can divide by zero. The empty Sprint 106 default depends on this.
    let range: ClosedRange<Date>
    let rangeSource: TimelineRangeSource
    let rows: [TimelineRow]
    let bars: [TimelineBar]
    let roleTotals: [TimelineRoleTotal]
    /// Sprint starts inside the range, for the boundary rules.
    let sprintBoundaries: [(date: Date, title: String)]
    /// Log entries the filter admits that fall **outside** the visible range, so
    /// the summary strip can say the axis is not the whole story.
    let hiddenEntryCount: Int
    /// Whether any drawn bar is approximately placed — drives the legend entry.
    let hasApproximateBars: Bool

    var totalMinutes: Double { roleTotals.reduce(0) { $0 + $1.minutes } }
    var isEmpty: Bool { bars.isEmpty }

    static func empty(zoom: TimelineZoom, now: Date) -> TimelineData {
        TimelineData(zoom: zoom,
                     range: now.addingTimeInterval(-7 * 24 * 3600)...now,
                     rangeSource: .aroundNow,
                     rows: [], bars: [], roleTotals: [], sprintBoundaries: [],
                     hiddenEntryCount: 0, hasApproximateBars: false)
    }
}

// MARK: - The builder

enum TimelineModel {

    /// Never let a range collapse. A zero-width domain makes Swift Charts draw
    /// an axis with no ticks and, worse, invites a divide-by-zero anywhere a
    /// position is computed as a fraction of the span.
    static let minimumSpan: TimeInterval = 3600

    /// A bar this thin is invisible and unclickable. Applied only to
    /// `.taskSpan`, where a task with a single log genuinely has a zero-length
    /// calendar span; the *drawn* width is then a floor, and the tooltip carries
    /// the real figures.
    static let minimumBarFraction: Double = 1.0 / 240.0

    /// Axis headroom past `now` while a timer runs.
    static let nowHeadroom: TimeInterval = 30 * 60

    /// Builds everything the chart draws.
    ///
    /// `tasks` is expected to be **already filtered** (`Store.filteredTasks`) —
    /// plan §8.1 is explicit that there is one filter state and the timeline
    /// reads it rather than owning a second one.
    static func build(tasks: [TrackerTask],
                      roles: [Role],
                      sprints: [Sprint],
                      currentSprint: Sprint?,
                      selectedSprintIDs: Set<String>,
                      zoom: TimelineZoom,
                      activeTimer: ActiveTimer?,
                      now: Date) -> TimelineData {

        let roleOrder = Dictionary(uniqueKeysWithValues:
            roles.enumerated().map { ($0.element.id, $0.offset) })
        let roleLabels = Dictionary(roles.map { ($0.id, $0.displayName) },
                                    uniquingKeysWith: { first, _ in first })

        // 1. The domain — every instant the view could show.
        let (domain, source) = self.domain(tasks: tasks, sprints: sprints,
                                           currentSprint: currentSprint,
                                           selectedSprintIDs: selectedSprintIDs,
                                           activeTimer: activeTimer,
                                           now: now)

        // 2. The window — the zoom's slice of it.
        let range = window(in: domain, zoom: zoom,
                           isSprintScoped: source.isSprintScoped, now: now)

        // 3. The logs that land in it.
        var placed: [PlacedLog] = []
        var hidden = 0
        for task in tasks {
            for log in task.logs {
                guard let span = self.span(of: log) else { continue }
                if span.end < range.lowerBound || span.start > range.upperBound {
                    hidden += 1
                    continue
                }
                placed.append(PlacedLog(task: task, log: log,
                                        start: span.start, end: span.end,
                                        roleLabel: label(of: task, in: roleLabels)))
            }
        }

        // 4. Bars, per the zoom's granularity.
        var bars: [TimelineBar]
        switch zoom.granularity {
        case .logEntry:
            bars = placed.map { entry -> TimelineBar in
                let approximate = !entry.log.hasWallClock
                return TimelineBar(
                    id: "log-\(entry.task.id)-\(entry.log.id)",
                    taskId: entry.task.id,
                    taskTitle: entry.task.title,
                    roleId: entry.task.roleId,
                    roleLabel: entry.roleLabel,
                    kind: approximate ? .approximate : .session,
                    start: clamp(entry.start, to: range),
                    end: clamp(entry.end, to: range),
                    minutes: entry.log.minutes,
                    detail: entry.log.note,
                    sprintTitle: sprintTitle(for: entry.start, in: sprints),
                    entryCount: 1,
                    approximateCount: approximate ? 1 : 0)
            }
        case .taskSpan:
            let floor = (range.upperBound.timeIntervalSince(range.lowerBound))
                * minimumBarFraction
            let grouped: [String: [PlacedLog]] = Dictionary(grouping: placed,
                                                            by: { $0.task.id })
            bars = grouped
                .compactMap { (_, entries) -> TimelineBar? in
                    guard let first = entries.first else { return nil }
                    let task: TrackerTask = first.task
                    let start: Date = entries.map(\.start).min() ?? first.start
                    let rawEnd: Date = entries.map(\.end).max() ?? first.end
                    let end: Date = max(rawEnd, start.addingTimeInterval(floor))
                    let approximate: Int = entries.count { !$0.log.hasWallClock }
                    let minutes: Double = entries.reduce(0.0) { $0 + $1.log.minutes }
                    return TimelineBar(
                        id: "task-\(task.id)",
                        taskId: task.id,
                        taskTitle: task.title,
                        roleId: task.roleId,
                        roleLabel: first.roleLabel,
                        kind: .taskSpan,
                        start: clamp(start, to: range),
                        end: clamp(end, to: range),
                        minutes: minutes,
                        detail: "\(entries.count) "
                            + (entries.count == 1 ? "entry" : "entries"),
                        sprintTitle: sprintTitles(from: start, to: rawEnd, in: sprints),
                        entryCount: entries.count,
                        approximateCount: approximate)
                }
        }

        // 5. The running timer, as a growing bar. Added at every zoom: a timer
        //    you are watching should not vanish because you widened the axis.
        if let timer = activeTimer, let taskId = timer.taskId,
           let startedAt = timer.startedAt,
           let task = tasks.first(where: { $0.id == taskId }) {
            let start = Date(timeIntervalSince1970: startedAt)
            if start <= range.upperBound && now >= range.lowerBound {
                bars.append(TimelineBar(
                    id: "running-\(task.id)",
                    taskId: task.id,
                    taskTitle: task.title,
                    roleId: task.roleId,
                    roleLabel: label(of: task, in: roleLabels),
                    kind: .running,
                    start: clamp(start, to: range),
                    end: clamp(now, to: range),
                    minutes: max(0, now.timeIntervalSince(start)) / 60,
                    detail: "Timer running",
                    sprintTitle: sprintTitle(for: start, in: sprints),
                    entryCount: 0,
                    approximateCount: 0))
            }
        }

        bars.sort { $0.start < $1.start }

        // 6. Rows — one per task with a bar, grouped into role sections.
        var rows = Dictionary(grouping: bars, by: \.taskId)
            .compactMap { taskId, taskBars -> TimelineRow? in
                guard let first = taskBars.first else { return nil }
                // The running bar's minutes are live, not logged, so they are
                // deliberately excluded from the row's logged total.
                let logged = taskBars.filter { $0.kind != .running }
                return TimelineRow(
                    id: taskId,
                    title: first.taskTitle,
                    roleId: first.roleId,
                    roleLabel: first.roleLabel,
                    roleOrder: first.roleId.flatMap { roleOrder[$0] } ?? Int.max,
                    minutes: logged.reduce(0) { $0 + $1.minutes },
                    approximateMinutes: logged.filter(\.isApproximate)
                        .reduce(0) { $0 + $1.minutes },
                    startsRoleSection: false)
            }
        rows.sort {
            if $0.roleOrder != $1.roleOrder { return $0.roleOrder < $1.roleOrder }
            if $0.roleLabel != $1.roleLabel { return $0.roleLabel < $1.roleLabel }
            return $0.title.localizedCaseInsensitiveCompare($1.title) == .orderedAscending
        }
        var previousRole: String??
        for index in rows.indices {
            let role = rows[index].roleId
            if previousRole == nil || previousRole! != role {
                rows[index].startsRoleSection = true
            }
            previousRole = .some(role)
        }

        // 7. Role totals for the summary strip.
        let roleTotals: [TimelineRoleTotal] = self.roleTotals(for: placed,
                                                              roleOrder: roleOrder)

        // 8. Sprint boundaries inside the range.
        let boundaries: [(date: Date, title: String)] = zoom.showsSprintBoundaries
            ? sprints.compactMap { sprint in
                guard let start = sprint.start,
                      start > range.lowerBound, start < range.upperBound else { return nil }
                return (start, sprint.displayName)
            }.sorted { $0.date < $1.date }
            : []

        return TimelineData(zoom: zoom,
                            range: range,
                            rangeSource: source,
                            rows: rows,
                            bars: bars,
                            roleTotals: roleTotals,
                            sprintBoundaries: boundaries,
                            hiddenEntryCount: hidden,
                            hasApproximateBars: bars.contains(where: \.isApproximate))
    }

    /// One log that falls inside the visible range, with its resolved geometry.
    ///
    /// A named struct rather than a tuple: the tuple version made the builder's
    /// body slow enough that the type checker gave up on it.
    private struct PlacedLog {
        let task: TrackerTask
        let log: LogEntry
        let start: Date
        let end: Date
        let roleLabel: String
    }

    private struct RoleAccumulator {
        var label: String
        var order: Int
        var minutes: Double
        var tasks: Set<String>
    }

    /// The summary strip's per-role hours.
    ///
    /// Computed from **log minutes in range**, never from bar geometry: a
    /// `.taskSpan` bar's width is the calendar time work was spread over, not
    /// the time worked, so measuring the drawing would report the wrong number
    /// at Sprint and Quarter zoom.
    private static func roleTotals(for placed: [PlacedLog],
                                   roleOrder: [String: Int]) -> [TimelineRoleTotal] {
        let noRoleKey = "\u{1}norole"
        var totals: [String: RoleAccumulator] = [:]
        for entry in placed {
            let key: String = entry.task.roleId ?? noRoleKey
            let order: Int = entry.task.roleId.flatMap { roleOrder[$0] } ?? Int.max
            var bucket = totals[key] ?? RoleAccumulator(label: entry.roleLabel,
                                                        order: order,
                                                        minutes: 0, tasks: [])
            bucket.minutes += entry.log.minutes
            bucket.tasks.insert(entry.task.id)
            totals[key] = bucket
        }
        let mapped: [TimelineRoleTotal] = totals.map { key, value in
            TimelineRoleTotal(roleId: key == noRoleKey ? nil : key,
                              label: value.label,
                              order: value.order,
                              minutes: value.minutes,
                              taskCount: value.tasks.count)
        }
        return mapped.sorted {
            $0.order == $1.order ? $0.label < $1.label : $0.order < $1.order
        }
    }

    // MARK: - Range resolution

    /// Every instant the view *could* show, and why.
    ///
    /// **Plan §8.5 lives here**: a non-empty Sprint facet sets the domain to the
    /// selected sprints' cached dates, which is what makes filtering by sprint
    /// useful on a time axis rather than merely subtractive.
    static func domain(tasks: [TrackerTask],
                       sprints: [Sprint],
                       currentSprint: Sprint?,
                       selectedSprintIDs: Set<String>,
                       activeTimer: ActiveTimer? = nil,
                       now: Date) -> (ClosedRange<Date>, TimelineRangeSource) {
        if !selectedSprintIDs.isEmpty {
            let picked = sprints.filter { selectedSprintIDs.contains($0.id) }
            let starts = picked.compactMap(\.start)
            let ends = picked.compactMap(\.end)
            if let lower = starts.min(), let upper = ends.max() {
                let titles = picked
                    .sorted { ($0.start ?? .distantPast) < ($1.start ?? .distantPast) }
                    .map(\.displayName)
                return (widened(lower, upper), .sprintFacet(titles: titles))
            }
        }

        var lower: Date?
        var upper: Date?
        for task in tasks {
            for log in task.logs {
                guard let span = self.span(of: log) else { continue }
                lower = min(lower ?? span.start, span.start)
                upper = max(upper ?? span.end, span.end)
            }
        }
        if var lower, var upper {
            // A running timer is real work happening *now*, so the axis has to
            // reach it. Without this the growing bar was off screen whenever the
            // last log predated today — which is the normal case first thing in
            // the morning, and was how this was found.
            //
            // Only for a *running* timer: with nothing running, stretching the
            // axis to now would open the chart on a stretch of empty days.
            if activeTimer?.taskId != nil, let startedAt = activeTimer?.startedAt {
                lower = min(lower, Date(timeIntervalSince1970: startedAt))
                // A little headroom past now, so the "Now" rule draws as a line
                // inside the plot rather than as a half-clipped edge and the
                // growing bar has somewhere to grow into.
                upper = max(upper, now.addingTimeInterval(nowHeadroom))
            }
            return (widened(lower, upper), .loggedTime)
        }

        // Nothing logged. Frame the axis on the current sprint rather than on
        // nothing — this is the shape of the Sprint 106 morning, when the
        // default filter is a sprint that has no logged time yet.
        if let currentSprint, let start = currentSprint.start, let end = currentSprint.end {
            return (widened(start, end), .currentSprint(title: currentSprint.displayName))
        }
        return (widened(now.addingTimeInterval(-14 * 24 * 3600), now), .aroundNow)
    }

    /// The zoom's slice of the domain, anchored to *now* when now is inside it
    /// and to the domain's end otherwise.
    ///
    /// Anchoring matters on the empty current sprint: Sprint 106 runs to
    /// 2026-08-24, so anchoring Day zoom at the domain's end would open the
    /// chart on a day two weeks in the future.
    static func window(in domain: ClosedRange<Date>,
                       zoom: TimelineZoom,
                       isSprintScoped: Bool,
                       now: Date) -> ClosedRange<Date> {
        let domainLength = domain.upperBound.timeIntervalSince(domain.lowerBound)
        let length = zoom.windowLength(domainLength: domainLength,
                                       isSprintScoped: isSprintScoped)
        guard length < domainLength else { return domain }

        var anchorEnd = domain.upperBound
        if domain.contains(now) {
            let endOfToday = Calendar.current.startOfDay(for: now)
                .addingTimeInterval(24 * 3600)
            anchorEnd = min(domain.upperBound, max(endOfToday, now))
        }
        let lower = max(domain.lowerBound, anchorEnd.addingTimeInterval(-length))
        return widened(lower, anchorEnd)
    }

    /// A range that can never be degenerate.
    static func widened(_ lower: Date, _ upper: Date) -> ClosedRange<Date> {
        guard upper.timeIntervalSince(lower) >= minimumSpan else {
            return lower...lower.addingTimeInterval(minimumSpan)
        }
        return lower...upper
    }

    // MARK: - Log geometry

    /// A log's position on the axis.
    ///
    /// A log with a wall clock uses it. One without — 29 of the owner's 419 —
    /// is placed at `log_effective_date` with a width of `minutes`, and is
    /// marked `.approximate` so the drawing can say the time of day is a guess
    /// at the *date* level only. Nothing here invents a plausible hour.
    static func span(of log: LogEntry) -> (start: Date, end: Date)? {
        if let startedAt = log.startedAt, let endedAt = log.endedAt, endedAt > startedAt {
            return (Date(timeIntervalSince1970: startedAt),
                    Date(timeIntervalSince1970: endedAt))
        }
        guard let effective = log.effectiveDate else { return nil }
        let start = Date(timeIntervalSince1970: effective)
        return (start, start.addingTimeInterval(max(log.minutes, 1) * 60))
    }

    static func clamp(_ date: Date, to range: ClosedRange<Date>) -> Date {
        min(max(date, range.lowerBound), range.upperBound)
    }

    /// The sprint label for a task span.
    ///
    /// A span can cross a sprint boundary — the owner has one task with time in
    /// eleven sprints — and labelling it with the sprint its *first* log fell in
    /// silently misattributes the rest. Measured on the real data: a bar drawn
    /// Jul 23 – Aug 5 was captioned "Sprint 104" although half of it is Sprint
    /// 105.
    static func sprintTitles(from start: Date, to end: Date,
                             in sprints: [Sprint]) -> String? {
        let first = sprintTitle(for: start, in: sprints)
        let last = sprintTitle(for: end, in: sprints)
        switch (first, last) {
        case let (a?, b?) where a != b: return "\(a) – \(b)"
        case let (a?, _): return a
        case let (_, b?): return b
        default: return nil
        }
    }

    static func sprintTitle(for date: Date, in sprints: [Sprint]) -> String? {
        for sprint in sprints {
            guard let start = sprint.start, let end = sprint.end else { continue }
            // Half-open, matching `wt.find_sprint_for_date`.
            if date >= start && date < end { return sprint.displayName }
        }
        return nil
    }

    private static func label(of task: TrackerTask, in labels: [String: String]) -> String {
        guard let id = task.roleId, !id.isEmpty else { return "No role" }
        return labels[id] ?? id
    }
}

extension TimelineRangeSource {
    /// Whether the Sprint facet is what set the domain, which changes how the
    /// Sprint and Quarter windows size themselves.
    var isSprintScoped: Bool {
        if case .sprintFacet = self { return true }
        return false
    }
}
