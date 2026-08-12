import SwiftUI
import Charts

/// The Gantt (plan §10).
///
/// **Swift Charts, `BarMark(xStart:xEnd:y:)`** — that mark *is* a Gantt bar, and
/// it brings axes, hit testing and accessibility descriptors for free. Zero
/// third-party dependencies, as everywhere else in this client.
///
/// **Read-only.** The chart displays logged time; there is no write path in this
/// view, and clicking a bar selects a task rather than editing anything.
///
/// Four things are worth knowing before changing this file:
///
/// - **Zoom changes what a bar means**, not just the axis: one bar per log entry
///   at Day/Week, one per task at Sprint/Quarter. That lives in `TimelineModel`,
///   which is pure and tested — a chart is exactly the sort of code that renders
///   wrong while every test passes, so as little as possible is decided here.
/// - **The Sprint facet sets the x-range** (§8.5), so filtering to Sprint 103
///   scrolls the axis there rather than merely subtracting bars.
/// - **The hatched bars are not decoration.** They are the 29 logs with no wall
///   clock, drawn at their effective date with a width of `minutes`. The legend
///   says so in words.
/// - **The empty state is a real state.** The default filter is the current
///   sprint, and on the morning a sprint opens it has no logged time at all —
///   which is what today looks like. The axis still draws; the overlay explains.
struct TimelineView: View {
    @Environment(Store.self) private var store

    /// The bar under the pointer, for the hover tooltip.
    @State private var hovered: TimelineBar?
    /// The bar the user clicked. Keeps the tooltip up after the pointer leaves,
    /// and is what makes the tooltip's content verifiable without a hover.
    @State private var pinned: TimelineBar?

    /// One row's height.
    ///
    /// A Gantt row is a fixed size, not a share of the window: with
    /// `chartYVisibleDomain` left to fill the plot, ten rows in an 800pt window
    /// came out 65pt tall each and the bars looked like blocks. The visible
    /// domain is therefore derived from the *available height* divided by this,
    /// so rows keep their size and the plot scrolls once there are too many.
    private static let rowHeight: CGFloat = 34
    /// Never show fewer than this many row slots, so two rows in a tall window
    /// do not stretch to fill it either.
    private static let minimumVisibleRows = 6

    private var focus: TimelineBar? { hovered ?? pinned }

    var body: some View {
        @Bindable var store = store
        let data = store.timeline

        VStack(spacing: 0) {
            // Navigating releases the Sprint facet, which the Board reads too.
            // A shared filter must never change silently, so the Timeline shows
            // the same bar the Board does rather than its own thing.
            if let feedback = store.feedback {
                FeedbackBar(feedback: feedback) { store.clearFeedback() }
            }
            TimelineSummaryStrip(data: data)
            Divider()
            content(data)
        }
        .toolbar {
            ToolbarItem(placement: .status) {
                TimelineStatusLabel(data: data)
            }
            ToolbarItem { TimelineNavigationControls() }
            ToolbarItem {
                Picker("Zoom", selection: $store.timelineZoom) {
                    ForEach(TimelineZoom.ordered) { zoom in
                        Text(zoom.displayName).tag(zoom)
                    }
                }
                .pickerStyle(.segmented)
                .help("How much time the axis covers, and whether a bar is one "
                      + "session or one task")
            }
            ToolbarItem { FilterMenu() }
        }
        .filterSearchField(store)
        // A filter change can pull the pinned bar's task out of the view.
        .onChange(of: store.filter) { _, _ in pinned = nil; hovered = nil }
        .onChange(of: store.timelineZoom) { _, _ in pinned = nil; hovered = nil }
        // Stepping the range does the same: the pinned bar was clipped to the
        // old window, so keeping its tooltip up would caption the new one with
        // a period that is no longer on screen.
        .onChange(of: store.timelineAnchor) { _, _ in pinned = nil; hovered = nil }
    }

    // MARK: - Body states

    @ViewBuilder
    private func content(_ data: TimelineData) -> some View {
        if store.isFiltering && store.filteredTasks.isEmpty && !store.tasks.isEmpty {
            // Nothing passes the filter at all — the same empty state the board
            // shows, for the same reason and with the same one-click way out.
            FilteredEmptyView()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            GeometryReader { geometry in
                let slots = rowSlots(for: data, in: geometry.size.height)
                chart(data, visibleRows: slots)
                    .frame(height: min(geometry.size.height,
                                       CGFloat(slots) * Self.rowHeight + Self.chrome),
                           alignment: .top)
                    .overlay(alignment: .center) {
                        if data.isEmpty { TimelineEmptyOverlay(data: data) }
                    }
                    .frame(maxHeight: .infinity, alignment: .top)
            }
        }
    }

    /// The plot's chrome: the x axis and its labels below, and the room the
    /// "Now" and sprint-boundary annotations need above.
    private static let chrome: CGFloat = 96

    /// The id of the invisible placeholder row that keeps the categorical y
    /// domain non-empty.
    static let placeholderRowID = "\u{1}no-rows"

    /// The chart's categorical y domain — **never empty**.
    ///
    /// Found by crashing the app: stepping the range onto a period with no
    /// logged time took the domain from six categories to zero and Swift Charts
    /// trapped with
    /// *"CGFloat value cannot be converted to Int because it is outside the
    /// representable range"* — its vertical-scroll path divides the plot height
    /// by the category count, and 0 categories makes that infinite. The empty
    /// state renders fine on a *fresh* build of an empty range; it is the
    /// transition into one that traps, which is why the Gantt's own empty state
    /// never hit it before there was a way to navigate into an empty period.
    ///
    /// One placeholder category costs nothing: it has no bar, and the y-axis
    /// label looks its row up by id and finds nothing, so it draws nothing.
    static func yDomain(for data: TimelineData) -> [String] {
        data.rows.isEmpty ? [placeholderRowID] : data.rows.map(\.id)
    }

    /// How many row slots the plot gets.
    ///
    /// Sized to the rows when they fit, so the x axis sits directly under the
    /// last one instead of at the bottom of a pane of empty space; capped by
    /// what the window can hold, after which the plot scrolls vertically with
    /// the axis pinned.
    private func rowSlots(for data: TimelineData, in height: CGFloat) -> Int {
        let capacity = Int(((height - Self.chrome) / Self.rowHeight).rounded(.down))
        return max(Self.minimumVisibleRows, min(data.rows.count, max(1, capacity)))
    }

    // MARK: - The chart

    private func chart(_ data: TimelineData, visibleRows: Int) -> some View {
        Chart {
            // Sprint boundaries. The **labels** are a second x axis rather than
            // annotations on these marks: with `chartScrollableAxes(.vertical)`
            // on, a mark's `.top` annotation scrolls with the plot and is
            // clipped away, which is exactly what the first Sprint-zoom
            // screenshot showed — dashed lines with nothing naming them.
            ForEach(data.sprintBoundaries, id: \.date) { boundary in
                RuleMark(x: .value("Sprint start", boundary.date))
                    .lineStyle(StrokeStyle(lineWidth: 1, dash: [3, 3]))
                    .foregroundStyle(.secondary.opacity(0.5))
            }

            ForEach(data.bars) { bar in
                BarMark(xStart: .value("Start", bar.start),
                        xEnd: .value("End", bar.end),
                        y: .value("Task", bar.rowId),
                        height: .ratio(0.62))
                    .foregroundStyle(style(for: bar))
                    .cornerRadius(3)
                    .opacity(focus == nil || focus?.id == bar.id ? 1 : 0.45)
                    .accessibilityLabel(bar.accessibilityDescription)
                    .accessibilityValue(Duration.format(minutes: bar.minutes))
            }

            // Now. Drawn last so it sits over the bars. Its label, like the
            // sprint ones, is an axis mark rather than an annotation — for the
            // same clipping reason.
            if data.range.contains(store.now) {
                RuleMark(x: .value("Now", store.now))
                    .lineStyle(StrokeStyle(lineWidth: 1.5, dash: [4, 3]))
                    .foregroundStyle(.red.opacity(0.75))
            }
        }
        .chartXScale(domain: data.range)
        .chartYScale(domain: Self.yDomain(for: data))
        .chartYAxis {
            AxisMarks(preset: .aligned, position: .leading) { value in
                AxisValueLabel {
                    if let id = value.as(String.self),
                       let row = data.rows.first(where: { $0.id == id }) {
                        TimelineRowLabel(
                            row: row,
                            color: RolePalette.color(forRoleID: row.roleId,
                                                     in: store.roles))
                    }
                }
                // Only the first row of each role gets a gridline, which is what
                // draws the role sections without a second axis.
                AxisGridLine()
                    .foregroundStyle(startsSection(value, in: data)
                                     ? AnyShapeStyle(.tertiary)
                                     : AnyShapeStyle(.clear))
            }
        }
        .chartXAxis {
            AxisMarks(values: axisValues(for: data.range)) { value in
                AxisGridLine()
                AxisTick()
                AxisValueLabel(format: axisFormat(for: data.range), centered: false)
            }
            // The sprint header strip along the top — axis chrome, so it stays
            // put while the rows scroll.
            AxisMarks(position: .top, values: data.sprintBoundaries.map(\.date)) { value in
                AxisValueLabel(anchor: .bottomLeading) {
                    if let date = value.as(Date.self),
                       let title = data.sprintBoundaries
                        .first(where: { $0.date == date })?.title {
                        Text(title)
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(.secondary)
                    }
                }
            }
            if data.range.contains(store.now) {
                AxisMarks(position: .top, values: [store.now]) { _ in
                    AxisValueLabel(anchor: .bottom) {
                        Text("Now")
                            .font(.caption2.weight(.semibold))
                            .foregroundStyle(.red)
                    }
                }
            }
        }
        .chartScrollableAxes(.vertical)
        .chartYVisibleDomain(length: visibleRows)
        .chartLegend(.hidden)
        .chartOverlay { proxy in
            GeometryReader { geometry in
                Rectangle()
                    .fill(.clear)
                    .contentShape(Rectangle())
                    .onContinuousHover { phase in
                        switch phase {
                        case .active(let point):
                            hovered = bar(at: point, proxy: proxy,
                                          geometry: geometry, data: data)
                        case .ended:
                            hovered = nil
                        }
                    }
                    .onTapGesture { point in
                        let hit = bar(at: point, proxy: proxy,
                                      geometry: geometry, data: data)
                        pinned = hit
                        // Plan §10: clicking a bar selects the task, and that
                        // selection is the Board's.
                        store.selectTask(hit?.taskId)
                    }
            }
        }
        .overlay(alignment: .topTrailing) {
            if let focus { TimelineTooltip(bar: focus).padding(8) }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            TimelineLegend(data: data)
        }
        .padding(.leading, 14)
        // Wider on the trailing edge: the last x-axis label is anchored to the
        // tick rather than centred on it, so 14pt clipped it to `1…`.
        .padding(.trailing, 30)
        .padding(.top, 18)
        .frame(minHeight: 260)
    }

    /// A bar's fill. Three cases, and the middle one is the point of the phase.
    private func style(for bar: TimelineBar) -> AnyShapeStyle {
        let color = RolePalette.color(forRoleID: bar.roleId, in: store.roles)
        switch bar.kind {
        case .running:
            // The growing bar reads as live rather than logged: the role colour
            // would make it look like history.
            return AnyShapeStyle(Color.accentColor.gradient)
        case .approximate:
            return AnyShapeStyle(HatchPattern.paint(color))
        case .session, .taskSpan:
            // A span whose entries are partly approximate is hatched too: its
            // start or end may be one of them, so the geometry is not exact.
            if bar.isApproximate { return AnyShapeStyle(HatchPattern.paint(color)) }
            return AnyShapeStyle(color)
        }
    }

    private func startsSection(_ value: AxisValue, in data: TimelineData) -> Bool {
        guard let id = value.as(String.self) else { return false }
        return data.rows.first { $0.id == id }?.startsRoleSection ?? false
    }

    /// Where the x-axis ticks go, chosen from **the range's length rather than
    /// the zoom's name**.
    ///
    /// The two come apart whenever the window is clamped to a sprint: Week zoom
    /// two days into Sprint 106 is a three-day axis. `.automatic(desiredCount:)`
    /// then put seven ticks across three days and, with a day-resolution label
    /// format, printed `Mon 10  Mon 10  Tue 11  Tue 11  Wed 12  Wed 12` — twice.
    /// Striding on the calendar unit the labels actually name is what makes a
    /// duplicate impossible rather than unlikely.
    private func axisValues(for range: ClosedRange<Date>) -> AxisMarkValues {
        let days = range.upperBound.timeIntervalSince(range.lowerBound) / 86_400
        if days <= 2.5 { return .automatic(desiredCount: 7) }
        if days <= 16 { return .stride(by: .day) }
        return .stride(by: .weekOfYear)
    }

    private func axisFormat(for range: ClosedRange<Date>) -> Date.FormatStyle {
        let days = range.upperBound.timeIntervalSince(range.lowerBound) / 86_400
        // Hour-of-day only, not weekday+hour: over one day the weekday repeats
        // on every tick, and `Tue, 19` reads as a date rather than 19:00 —
        // which is how the first Day-zoom screenshot looked.
        if days <= 2.5 { return .dateTime.hour().minute() }
        if days <= 16 { return .dateTime.weekday(.abbreviated).day() }
        return .dateTime.month(.abbreviated).day()
    }

    /// Which bar is under a point in the plot.
    ///
    /// `chartXSelection` alone gives a date and no row, so the hit test reads
    /// both axes off the proxy: the categorical y value names the row, and the x
    /// value picks the bar on it whose span contains the instant. Bars on one
    /// row cannot overlap in time, so the first containing bar is the answer.
    private func bar(at point: CGPoint,
                     proxy: ChartProxy,
                     geometry: GeometryProxy,
                     data: TimelineData) -> TimelineBar? {
        guard let plotFrame = proxy.plotFrame else { return nil }
        let origin = geometry[plotFrame].origin
        let x = point.x - origin.x
        let y = point.y - origin.y
        guard let date: Date = proxy.value(atX: x),
              let rowId: String = proxy.value(atY: y) else { return nil }
        let onRow = data.bars.filter { $0.rowId == rowId }
        if let exact = onRow.first(where: { $0.start <= date && date <= $0.end }) {
            return exact
        }
        // A one-log bar can be narrower than the pointer is precise. Fall back
        // to the nearest bar on the row, within a small slice of the range.
        let tolerance = data.range.upperBound
            .timeIntervalSince(data.range.lowerBound) / 120
        return onRow.min {
            abs($0.start.timeIntervalSince(date)) < abs($1.start.timeIntervalSince(date))
        }.flatMap { candidate in
            abs(candidate.start.timeIntervalSince(date)) <= tolerance ? candidate : nil
        }
    }
}

// MARK: - Timeframe navigation

/// Previous · Today · Next.
///
/// The buttons are here **and** in the menu bar (`App.swift`), where the
/// shortcuts live: a `keyboardShortcut` attached to a view only fires while that
/// view is on screen *and* focused, and the chart loses focus to the zoom picker
/// — the same reason `⌘+`/`⌘-` are menu items. The menu items are gated on
/// `store.selection == .timeline`, so `⌥←`/`⌥→` cannot shadow the Board's
/// `⌘←`/`⌘→` (different modifier) *or* fire while the Board is showing.
///
/// Disabled at the ends of the data rather than scrolling into empty infinity:
/// `Store.canStepTimeline` asks the same pure `TimelineNavigation.step` the
/// button would call, so what is greyed out and what would no-op cannot drift.
private struct TimelineNavigationControls: View {
    @Environment(Store.self) private var store

    var body: some View {
        ControlGroup {
            Button {
                store.stepTimeline(.previous)
            } label: {
                Label("Previous", systemImage: "chevron.left")
            }
            .disabled(!store.canStepTimeline(.previous))
            .help("Show the previous \(unit) (⌥←)")

            Button("Today") { store.timelineToToday() }
                .disabled(!store.canReturnToToday)
                .help("Return to the present, and put back any Sprint filter "
                      + "navigation released (⌥⌘T)")

            Button {
                store.stepTimeline(.next)
            } label: {
                Label("Next", systemImage: "chevron.right")
            }
            .disabled(!store.canStepTimeline(.next))
            .help("Show the next \(unit) (⌥→)")
        }
    }

    /// What one press moves by, named for the current zoom.
    private var unit: String {
        switch store.timelineZoom {
        case .day: "day"
        case .week: "week"
        case .sprint: "sprint"
        case .quarter: "quarter"
        }
    }
}

// MARK: - Row label

/// A y-axis row: the role's colour dot and the task's title.
///
/// The dot never travels alone — the role's name is in the accessibility label
/// and in the legend, and the summary strip spells out every role in text.
private struct TimelineRowLabel: View {
    let row: TimelineRow
    let color: Color

    var body: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(color)
                .frame(width: 6, height: 6)
            Text(row.title)
                .font(.caption)
                .lineLimit(1)
                .truncationMode(.tail)
        }
        .frame(width: 190, alignment: .leading)
        .help("\(row.title) · \(row.roleLabel) · "
              + Duration.formatZeroed(minutes: row.minutes))
        .accessibilityLabel("\(row.title), \(row.roleLabel), "
                            + Duration.formatZeroed(minutes: row.minutes))
    }
}

// MARK: - Summary strip

/// Plan §10's summary strip: the **filtered** hours by role, for the visible
/// range. Every colour carries its label and its figure.
private struct TimelineSummaryStrip: View {
    @Environment(Store.self) private var store
    let data: TimelineData

    var body: some View {
        ScrollView(.horizontal) {
            HStack(spacing: 14) {
                VStack(alignment: .leading, spacing: 1) {
                    Text(Duration.formatZeroed(minutes: data.totalMinutes))
                        .font(.title3.monospacedDigit().weight(.semibold))
                    Text(rangeCaption)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                if !data.roleTotals.isEmpty { Divider().frame(height: 30) }
                ForEach(data.roleTotals) { total in
                    RoleTotalChip(
                        total: total,
                        color: RolePalette.color(forRoleID: total.roleId, in: store.roles))
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
        }
        .scrollIndicators(.never)
    }

    /// The dates on the axis, why they are those dates, and — when it applies —
    /// what is *not* in the total sitting right above this line.
    ///
    /// The last clause matters because excluding recurrent tasks removed about a
    /// third of the plotted hours. A total that quietly shrank by 123.8h with
    /// nothing on screen accounting for it would read as a bug.
    private var rangeCaption: String {
        let start = data.range.lowerBound.formatted(.dateTime.month(.abbreviated).day())
        let end = data.range.upperBound.formatted(.dateTime.month(.abbreviated).day())
        var caption = "\(start) – \(end) · \(data.rangeSource.explanation)"
        if data.excludedRecurrentTaskCount > 0 {
            caption += " · \(data.excludedRecurrentTaskCount) recurrent "
                + (data.excludedRecurrentTaskCount == 1 ? "task" : "tasks")
                + " on the shelf, not plotted"
        }
        return caption
    }
}

private struct RoleTotalChip: View {
    let total: TimelineRoleTotal
    let color: Color

    var body: some View {
        HStack(spacing: 6) {
            RoundedRectangle(cornerRadius: 2)
                .fill(color)
                .frame(width: 4, height: 26)
            VStack(alignment: .leading, spacing: 1) {
                Text(total.label)
                    .font(.caption)
                    .lineLimit(1)
                Text("\(Duration.formatZeroed(minutes: total.minutes)) · "
                     + "\(total.taskCount) task\(total.taskCount == 1 ? "" : "s")")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(total.label), "
                            + "\(Duration.formatZeroed(minutes: total.minutes)), "
                            + "\(total.taskCount) tasks")
    }
}

// MARK: - Legend

/// A hand-built legend rather than Swift Charts'.
///
/// The chart's own legend is driven by a foreground-style *scale*, and the
/// hatch is applied per mark rather than through one — so an automatic legend
/// would show the roles and silently omit the one entry that matters most.
///
/// **The roles are deliberately not repeated here.** The summary strip above
/// the chart already names every role in the range with its colour and its
/// hours; a second copy at nine roles wrapped onto two lines and collided with
/// itself, which is what the Quarter-zoom screenshot showed. So this legend
/// carries only the two channels the strip cannot: the hatch and the timer.
private struct TimelineLegend: View {
    @Environment(Store.self) private var store
    let data: TimelineData

    var body: some View {
        HStack(spacing: 14) {
            if data.hasApproximateBars {
                Label {
                    Text("Approximate time of day").font(.caption2)
                } icon: {
                    HatchSwatch(color: .secondary).frame(width: 20, height: 10)
                }
                .labelStyle(.titleAndIcon)
                .help("These entries record how long the work took, but not when "
                      + "in the day it happened. The bar sits at the date it was "
                      + "logged; its position within the day is not real.")
            }
            if store.activeTimerTask != nil {
                Label {
                    Text("Running").font(.caption2)
                } icon: {
                    RoundedRectangle(cornerRadius: 2)
                        .fill(Color.accentColor)
                        .frame(width: 20, height: 10)
                }
                .labelStyle(.titleAndIcon)
            }
            if !data.sprintBoundaries.isEmpty {
                Label {
                    Text("Sprint boundary").font(.caption2)
                } icon: {
                    Rectangle()
                        .fill(.secondary.opacity(0.5))
                        .frame(width: 1, height: 11)
                        .frame(width: 20)
                }
                .labelStyle(.titleAndIcon)
            }
            Spacer(minLength: 0)
        }
        .foregroundStyle(.secondary)
        .padding(.horizontal, 14)
        .padding(.vertical, 6)
    }
}

// MARK: - Tooltip

/// Plan §10's hover tooltip: task, note, duration, sprint.
///
/// Shown for the hovered bar, and kept up for a clicked one — which is also the
/// only way its content can be verified, since synthesised mouse drags and
/// hovers do not drive SwiftUI but clicks do.
private struct TimelineTooltip: View {
    let bar: TimelineBar

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(bar.taskTitle)
                .font(.callout.weight(.semibold))
                .lineLimit(2)
            if !bar.detail.isEmpty {
                Text(bar.detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            HStack(spacing: 6) {
                Text(Duration.format(minutes: bar.minutes))
                    .font(.caption.monospacedDigit().weight(.medium))
                if let sprint = bar.sprintTitle {
                    Text("·").foregroundStyle(.tertiary)
                    Text(sprint).font(.caption).foregroundStyle(.secondary)
                }
            }
            Text(when)
                .font(.caption2)
                .foregroundStyle(.tertiary)
            if bar.isApproximate {
                Label(approximateNote, systemImage: "questionmark.circle")
                    .font(.caption2)
                    .foregroundStyle(.orange)
                    .labelStyle(.titleAndIcon)
            }
        }
        .padding(9)
        .frame(maxWidth: 300, alignment: .leading)
        .background(.regularMaterial, in: .rect(cornerRadius: 8))
        .overlay {
            RoundedRectangle(cornerRadius: 8).strokeBorder(.quaternary, lineWidth: 1)
        }
        .shadow(radius: 6, y: 2)
        .allowsHitTesting(false)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(bar.accessibilityDescription)
    }

    private var when: String {
        let start = bar.start.formatted(.dateTime.month(.abbreviated).day()
            .hour().minute())
        let end = bar.end.formatted(.dateTime.hour().minute())
        return bar.kind == .taskSpan
            ? bar.start.formatted(.dateTime.month(.abbreviated).day())
              + " – " + bar.end.formatted(.dateTime.month(.abbreviated).day())
            : "\(start) – \(end)"
    }

    private var approximateNote: String {
        bar.entryCount <= 1
            ? "Approximate time of day — this entry records only the date."
            : "\(bar.approximateCount) of \(bar.entryCount) entries have an "
              + "approximate time of day."
    }
}

// MARK: - Empty state

/// What the Gantt shows when the filter admits tasks but none of them has
/// logged time in the visible range.
///
/// **This is today's real state**, not an error: Sprint 106 opened on
/// 2026-08-10 with nothing logged against it, and the Sprint facet defaults to
/// the current sprint. The axis, the sprint boundaries and the "Now" rule stay
/// drawn behind this — an empty fortnight you can see the shape of is more
/// useful than a blank pane.
private struct TimelineEmptyOverlay: View {
    @Environment(Store.self) private var store
    let data: TimelineData

    var body: some View {
        VStack(spacing: 8) {
            Image(systemName: "chart.bar.xaxis")
                .font(.largeTitle)
                .foregroundStyle(.tertiary)
            Text(headline)
                .font(.headline)
            Text(detail)
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            HStack(spacing: 8) {
                // Offered first when the user navigated here: the likeliest way
                // out of an empty period is back to the present, not a filter
                // change they did not make.
                if store.canReturnToToday {
                    Button("Today") { store.timelineToToday() }
                        .keyboardShortcut(.defaultAction)
                }
                if !store.filter.sprints.isEmpty {
                    let sprints = Button("Show All Sprints") { store.clear(.sprint) }
                    // Only one default button, and Today takes it when it is
                    // offered.
                    if store.canReturnToToday { sprints } else {
                        sprints.keyboardShortcut(.defaultAction)
                    }
                }
                if store.isFiltering {
                    Button("Clear All Filters") { store.clearFilters() }
                }
            }
            .padding(.top, 2)
        }
        .padding(20)
        .frame(maxWidth: 460)
        .background(.regularMaterial, in: .rect(cornerRadius: 12))
        .overlay {
            RoundedRectangle(cornerRadius: 12).strokeBorder(.quaternary, lineWidth: 1)
        }
        .shadow(radius: 10, y: 3)
    }

    private var headline: String {
        switch data.rangeSource {
        case .navigated(let label):
            "No logged time in \(label)"
        case .sprintFacet(let titles) where titles.count == 1:
            "No logged time in \(titles[0]) yet"
        case .sprintFacet:
            "No logged time in the selected sprints"
        default:
            "No logged time in this range"
        }
    }

    /// Counts **plotted** tasks, not everything the filter admits: recurrent
    /// tasks pass the filter and are never drawn here, so including them would
    /// promise bars that stepping the range can never produce.
    private var detail: String {
        let plotted = store.timelineTasks.count
        var lines = ["The range is drawn, the tasks are just not in it — "
                     + "\(plotted) task" + (plotted == 1 ? "" : "s")
                     + " pass the filter with no time logged here."]
        if data.hiddenEntryCount > 0 {
            lines.append("\(data.hiddenEntryCount) entr"
                         + (data.hiddenEntryCount == 1 ? "y falls" : "ies fall")
                         + " outside it.")
        }
        if data.excludedRecurrentTaskCount > 0 {
            lines.append("\(data.excludedRecurrentTaskCount) recurrent task"
                         + (data.excludedRecurrentTaskCount == 1 ? " is" : "s are")
                         + " on the shelf and never plotted here.")
        }
        return lines.joined(separator: " ")
    }
}

// MARK: - Toolbar status

/// The scope line, so the axis is never implicit — the same job
/// `BoardStatusLabel` does for the board.
private struct TimelineStatusLabel: View {
    let data: TimelineData

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(scope).font(.caption)
            Text(detail).font(.caption2).foregroundStyle(.tertiary)
        }
    }

    /// Kept short: the toolbar's `.status` slot is narrow, and the first draft's
    /// two long lines were clipped mid-word.
    /// Counts **logged** bars only. The running timer draws a bar but is not a
    /// log entry, and reporting "1 entry" over a range whose logged total is 0m
    /// contradicted the summary strip right next to it.
    private var scope: String {
        let bars = data.bars.count { $0.kind != .running }
        let unit = data.zoom.granularity == .logEntry
            ? (bars == 1 ? "entry" : "entries")
            : (bars == 1 ? "task" : "tasks")
        return "\(bars) \(unit)"
    }

    /// **What is not on screen, and why.**
    ///
    /// Two different absences, kept apart on purpose. "Off-range" entries are
    /// reachable — step or widen the range and they appear. Recurrent tasks are
    /// not: they are never plotted here at any range. Reporting them as
    /// off-range would send the user stepping through months looking for 123.8h
    /// that lives on the shelf.
    private var detail: String {
        var parts: [String] = []
        if data.hiddenEntryCount > 0 { parts.append("\(data.hiddenEntryCount) off-range") }
        if data.excludedRecurrentTaskCount > 0 {
            parts.append("\(data.excludedRecurrentTaskCount) recurrent on shelf")
        }
        return parts.isEmpty ? "all shown" : parts.joined(separator: " · ")
    }
}
