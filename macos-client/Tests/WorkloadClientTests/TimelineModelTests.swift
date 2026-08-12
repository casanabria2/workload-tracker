import XCTest
@testable import WorkloadClient

/// Plan §10 — the Gantt, asserted at the model.
///
/// A chart is the sort of code that renders wrong while every unit test passes,
/// so the tests here are deliberately about the things a screenshot cannot
/// cheaply prove: that the zoom levels really do change *what a bar is*, that
/// the 29 timestamp-less logs are marked rather than silently placed, that the
/// Sprint facet drives the x-range, and that the empty current sprint produces a
/// non-degenerate range rather than a division by zero.
///
/// The fixture is `facets.json` — synthetic titles and issue refs over the
/// owner's **real measured distribution**: 55 tasks, 419 logs of which 29 carry
/// no wall clock, 72 cached sprints, 11 of them with logged time.
@MainActor
final class TimelineModelTests: XCTestCase {

    // MARK: - Fixtures

    private func loadSnapshot(currentSprintTitle: String? = nil) throws -> Snapshot {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: "facets", withExtension: "json",
                              subdirectory: "Fixtures"))
        var raw = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        if let title = currentSprintTitle {
            // Re-point `current_sprint` at a sprint from the same cache. This is
            // how the Sprint 106 case is built: no second fixture, and the
            // sprint calendar stays the one the rest of the suite uses.
            let sprints = try XCTUnwrap(raw["sprints"] as? [[String: Any]])
            raw["current_sprint"] = try XCTUnwrap(
                sprints.first { $0["title"] as? String == title })
        }
        let data = try JSONSerialization.data(withJSONObject: raw)
        return try JSONDecoder().decode(Snapshot.self, from: data)
    }

    /// A store with the fixture and no networking, so `Store.timeline` — the
    /// accessor the view actually reads — is what gets exercised.
    private func makeStore(currentSprintTitle: String? = nil,
                           now: Date = Date(timeIntervalSince1970: 1786075563)) throws -> Store {
        Store(previewSnapshot: try loadSnapshot(currentSprintTitle: currentSprintTitle),
              now: now)
    }

    private func sprint(_ title: String, in snapshot: Snapshot) throws -> Sprint {
        try XCTUnwrap(snapshot.sprints.first { $0.title == title })
    }

    /// The logs of `task` that intersect `range`.
    ///
    /// Tests are written against this rather than against the task's whole log
    /// array because **no zoom level shows everything**: the fixture's logged
    /// time spans about 150 days and the widest window is a quarter. Comparing
    /// a bar to the task's lifetime totals passes or fails by accident
    /// depending on where the window happens to land.
    private func logsInRange(_ task: TrackerTask,
                             _ range: ClosedRange<Date>) -> [LogEntry] {
        task.logs.filter { log in
            guard let span = TimelineModel.span(of: log) else { return false }
            return span.end >= range.lowerBound && span.start <= range.upperBound
        }
    }

    private func build(_ snapshot: Snapshot,
                       zoom: TimelineZoom,
                       sprints selected: Set<String> = [],
                       tasks: [TrackerTask]? = nil,
                       activeTimer: ActiveTimer? = nil,
                       anchor: TimelineAnchor? = nil,
                       now: Date = Date(timeIntervalSince1970: 1786075563)) -> TimelineData {
        TimelineModel.build(tasks: tasks ?? snapshot.tasks,
                            roles: snapshot.roles,
                            sprints: snapshot.sprints,
                            currentSprint: snapshot.currentSprint,
                            selectedSprintIDs: selected,
                            zoom: zoom,
                            activeTimer: activeTimer,
                            now: now,
                            anchor: anchor)
    }

    // MARK: - Zoom changes what a bar is

    /// The load-bearing distinction of the phase: Day/Week draw **one bar per
    /// log entry**, Sprint/Quarter **one bar per task**. Asserted over the whole
    /// fixture rather than a toy, so a regression that quietly reused one
    /// representation everywhere shows up as a count mismatch.
    func testDayAndWeekDrawOneBarPerLogEntryAndSprintQuarterOnePerTask() throws {
        let snapshot = try loadSnapshot()
        // A range wide enough to hold every log, so the counts are about
        // granularity and not about clipping.
        let all = Set(snapshot.sprints.map(\.id))

        for zoom in [TimelineZoom.day, .week] {
            XCTAssertEqual(zoom.granularity, .logEntry, "\(zoom) must be per-entry")
        }
        for zoom in [TimelineZoom.sprint, .quarter] {
            XCTAssertEqual(zoom.granularity, .taskSpan, "\(zoom) must be per-task")
        }

        let week = build(snapshot, zoom: .week, sprints: all)
        let quarter = build(snapshot, zoom: .quarter, sprints: all)

        // Per-entry bars outnumber per-task bars, and every per-task bar has a
        // distinct task.
        XCTAssertEqual(Set(quarter.bars.map(\.taskId)).count, quarter.bars.count,
                       "Sprint/Quarter must draw at most one bar per task")
        XCTAssertGreaterThan(week.bars.count, week.rows.count,
                             "Day/Week must draw more bars than rows — several "
                             + "sessions land on one task")
        XCTAssertTrue(quarter.bars.allSatisfy { $0.kind == .taskSpan })
        XCTAssertTrue(week.bars.allSatisfy { $0.kind == .session || $0.kind == .approximate })
    }

    /// A per-task span really is `min(log date)` → `max(log date)` (plan §10),
    /// not the sum of its sessions.
    func testTaskSpanRunsFromFirstLogToLast() throws {
        let snapshot = try loadSnapshot()
        let all = Set(snapshot.sprints.map(\.id))
        let data = build(snapshot, zoom: .quarter, sprints: all)

        let multi = try XCTUnwrap(data.bars.first { $0.entryCount > 3 })
        let task = try XCTUnwrap(snapshot.tasks.first { $0.id == multi.taskId })
        let spans = logsInRange(task, data.range).compactMap { TimelineModel.span(of: $0) }
        let firstStart = try XCTUnwrap(spans.map(\.start).min())
        let lastEnd = try XCTUnwrap(spans.map(\.end).max())

        XCTAssertEqual(multi.start.timeIntervalSince1970,
                       max(firstStart, data.range.lowerBound).timeIntervalSince1970,
                       accuracy: 1)
        XCTAssertEqual(multi.end.timeIntervalSince1970,
                       min(lastEnd, data.range.upperBound).timeIntervalSince1970,
                       accuracy: 1)
    }

    // MARK: - The 29 timestamp-less logs

    /// Every log with no wall clock is drawn at `log_effective_date` with a
    /// width of `minutes`, and **marked**. This is the plan's one place where
    /// visible imprecision beats invisible fabrication, so the marking is the
    /// assertion — not the position.
    func testTimestamplessLogsArePlacedAtTheirEffectiveDateAndMarkedApproximate() throws {
        let snapshot = try loadSnapshot()
        let all = Set(snapshot.sprints.map(\.id))
        let data = build(snapshot, zoom: .week, sprints: all)

        let approximate = data.bars.filter { $0.kind == .approximate }
        XCTAssertFalse(approximate.isEmpty,
                       "the fixture carries 29 clock-less logs; none was marked")
        XCTAssertTrue(data.hasApproximateBars)

        for bar in approximate {
            let task = try XCTUnwrap(snapshot.tasks.first { $0.id == bar.taskId })
            let log = try XCTUnwrap(task.logs.first { bar.id.hasSuffix($0.id) })
            XCTAssertFalse(log.hasWallClock,
                           "a log with a real span was drawn as approximate")
            let effective = try XCTUnwrap(log.effectiveDate)
            XCTAssertEqual(bar.start.timeIntervalSince1970, effective, accuracy: 1,
                           "an approximate bar must sit at log_effective_date, "
                           + "not at an invented time of day")
            XCTAssertEqual(bar.end.timeIntervalSince(bar.start),
                           max(log.minutes, 1) * 60, accuracy: 1,
                           "its width must be the logged minutes")
            XCTAssertTrue(bar.isApproximate)
            XCTAssertTrue(bar.accessibilityDescription
                .localizedCaseInsensitiveContains("approximate"))
        }
    }

    /// A session bar with a real clock is never marked, so the hatch means
    /// something.
    func testSessionBarsAreNotMarkedApproximate() throws {
        let snapshot = try loadSnapshot()
        let all = Set(snapshot.sprints.map(\.id))
        let data = build(snapshot, zoom: .week, sprints: all)
        for bar in data.bars where bar.kind == .session {
            XCTAssertFalse(bar.isApproximate)
            XCTAssertEqual(bar.approximateCount, 0)
        }
    }

    /// The count of clock-less logs the model sees matches the fixture's, which
    /// matches the owner's measured 29. A silent change to `hasWallClock` or to
    /// `span(of:)` would move this.
    func testTheFixtureCarriesTheMeasuredNumberOfClocklessLogs() throws {
        let snapshot = try loadSnapshot()
        let clockless = snapshot.tasks.flatMap(\.logs).count { !$0.hasWallClock }
        XCTAssertEqual(clockless, 29)
        XCTAssertEqual(snapshot.tasks.flatMap(\.logs).count, 419)
    }

    /// At Sprint/Quarter a span that folds in a clock-less entry is still
    /// marked, because one of its endpoints may be the approximate one.
    func testTaskSpansInheritApproximationFromTheirEntries() throws {
        let snapshot = try loadSnapshot()
        let all = Set(snapshot.sprints.map(\.id))
        let data = build(snapshot, zoom: .quarter, sprints: all)

        for bar in data.bars {
            let task = try XCTUnwrap(snapshot.tasks.first { $0.id == bar.taskId })
            let expected = logsInRange(task, data.range).count { !$0.hasWallClock }
            XCTAssertEqual(bar.approximateCount, expected, "task \(bar.taskId)")
            XCTAssertEqual(bar.isApproximate, expected > 0)
        }
        XCTAssertTrue(data.bars.contains(where: \.isApproximate),
                      "no span in the visible quarter folded in a clock-less log, "
                      + "so this test asserted nothing")
    }

    // MARK: - The Sprint facet drives the x-range (§8.5)

    func testSprintFacetSetsTheVisibleRangeToThatSprintsCachedDates() throws {
        let snapshot = try loadSnapshot()
        let s103 = try sprint("Sprint 103", in: snapshot)
        let data = build(snapshot, zoom: .sprint, sprints: [s103.id])

        XCTAssertEqual(data.range.lowerBound, try XCTUnwrap(s103.start))
        XCTAssertEqual(data.range.upperBound, try XCTUnwrap(s103.end))
        XCTAssertEqual(data.rangeSource, .sprintFacet(titles: ["Sprint 103"]))
    }

    func testSelectingSeveralSprintsSpansThem() throws {
        let snapshot = try loadSnapshot()
        let s100 = try sprint("Sprint 100", in: snapshot)
        let s103 = try sprint("Sprint 103", in: snapshot)
        let data = build(snapshot, zoom: .quarter, sprints: [s100.id, s103.id])

        XCTAssertEqual(data.range.lowerBound, try XCTUnwrap(s100.start))
        XCTAssertEqual(data.range.upperBound, try XCTUnwrap(s103.end))
        XCTAssertEqual(data.rangeSource,
                       .sprintFacet(titles: ["Sprint 100", "Sprint 103"]))
    }

    /// With no Sprint facet the range comes from the data, not from a nominal
    /// window — otherwise the default view could show an axis with nothing on it
    /// while months of logged time sit off screen.
    func testWithoutASprintFacetTheRangeComesFromTheLoggedTime() throws {
        let snapshot = try loadSnapshot()
        let data = build(snapshot, zoom: .quarter)
        XCTAssertEqual(data.rangeSource, .loggedTime)
        XCTAssertFalse(data.bars.isEmpty)
    }

    /// The narrow zooms still sit *inside* the selected sprint. Selecting a
    /// sprint and then zooming to Day must not jump the axis out of it.
    func testNarrowZoomsStayInsideTheSelectedSprint() throws {
        let snapshot = try loadSnapshot()
        let s103 = try sprint("Sprint 103", in: snapshot)
        let start = try XCTUnwrap(s103.start)
        let end = try XCTUnwrap(s103.end)

        for zoom in [TimelineZoom.day, .week] {
            let data = build(snapshot, zoom: zoom, sprints: [s103.id])
            XCTAssertGreaterThanOrEqual(data.range.lowerBound, start, "\(zoom)")
            XCTAssertLessThanOrEqual(data.range.upperBound, end, "\(zoom)")
            XCTAssertLessThan(data.range.lowerBound, data.range.upperBound)
        }
    }

    /// Day and Week really do show a day and a week.
    func testZoomWindowLengths() throws {
        let snapshot = try loadSnapshot()
        let day = build(snapshot, zoom: .day)
        let week = build(snapshot, zoom: .week)
        XCTAssertEqual(day.range.upperBound.timeIntervalSince(day.range.lowerBound),
                       24 * 3600, accuracy: 1)
        XCTAssertEqual(week.range.upperBound.timeIntervalSince(week.range.lowerBound),
                       7 * 24 * 3600, accuracy: 1)
    }

    // MARK: - Sprint 106: the empty current sprint

    /// **The state the app is actually in today.** Sprint 106 opened
    /// 2026-08-10 with no logged time, and the filter's default seed is the
    /// current sprint — so the default timeline is an empty range.
    ///
    /// It must render as an empty *range*, not as a collapsed axis: a
    /// zero-width domain is where a chart divides by zero.
    func testTheEmptyCurrentSprintProducesARealRangeAndNoBars() throws {
        let snapshot = try loadSnapshot(currentSprintTitle: "Sprint 106")
        let s106 = try sprint("Sprint 106", in: snapshot)
        XCTAssertEqual(snapshot.currentSprint?.title, "Sprint 106")
        XCTAssertFalse(snapshot.tasks.contains {
            $0.sprintsWithTime.contains { $0.sprintId == s106.id }
        }, "Sprint 106 must have no logged time for this test to mean anything")

        for zoom in TimelineZoom.ordered {
            let data = build(snapshot, zoom: zoom, sprints: [s106.id])
            XCTAssertTrue(data.bars.isEmpty, "\(zoom): nothing is logged in 106")
            XCTAssertTrue(data.rows.isEmpty, "\(zoom)")
            XCTAssertEqual(data.totalMinutes, 0, "\(zoom)")
            XCTAssertGreaterThanOrEqual(
                data.range.upperBound.timeIntervalSince(data.range.lowerBound),
                TimelineModel.minimumSpan,
                "\(zoom): the range collapsed, which is the divide-by-zero case")
            XCTAssertGreaterThan(data.hiddenEntryCount, 0,
                                 "\(zoom): the entries outside the range must be "
                                 + "counted so the empty state can say so")
            XCTAssertEqual(data.rangeSource, .sprintFacet(titles: ["Sprint 106"]))
        }
    }

    /// At Day zoom on the empty sprint the window sits on **today**, not on the
    /// sprint's far end a fortnight in the future.
    func testDayZoomInsideTheEmptySprintAnchorsOnToday() throws {
        let snapshot = try loadSnapshot(currentSprintTitle: "Sprint 106")
        let s106 = try sprint("Sprint 106", in: snapshot)
        // 2026-08-12 noon: two days into Sprint 106, twelve days before it ends.
        // Anchoring on the domain's end would open Day zoom on 2026-08-23.
        let now = Date(timeIntervalSince1970: 1786561200)
        let data = build(snapshot, zoom: .day, sprints: [s106.id], now: now)
        XCTAssertTrue(data.range.contains(now),
                      "Day zoom opened on \(data.range), which does not contain now")
    }

    /// The whole store path, not just the model: the default seed really is the
    /// current sprint, and `Store.timeline` really is empty because of it.
    func testStoreDefaultsToTheCurrentSprintAndYieldsAnEmptyTimeline() throws {
        let store = try makeStore(currentSprintTitle: "Sprint 106")
        let s106 = try sprint("Sprint 106", in: try XCTUnwrap(store.snapshot))
        XCTAssertEqual(store.filter.sprints, [s106.id],
                       "the filter must seed with the current sprint")
        XCTAssertTrue(store.timeline.bars.isEmpty)
        XCTAssertLessThan(store.timeline.range.lowerBound,
                          store.timeline.range.upperBound)
        // The open-work exemption still admits tasks; they simply have no time
        // in the range. That distinction is what the empty state explains.
        XCTAssertFalse(store.filteredTasks.isEmpty)
    }

    /// A snapshot with no sprints and no logs at all still yields a drawable
    /// range rather than crashing on an empty min/max.
    func testAnEmptyTrackerStillProducesADrawableRange() {
        let now = Date(timeIntervalSince1970: 1786075563)
        let data = TimelineModel.build(tasks: [], roles: [], sprints: [],
                                       currentSprint: nil, selectedSprintIDs: [],
                                       zoom: .week, activeTimer: nil, now: now)
        XCTAssertEqual(data.rangeSource, .aroundNow)
        XCTAssertLessThan(data.range.lowerBound, data.range.upperBound)
        XCTAssertTrue(data.bars.isEmpty)
    }

    /// `widened` is the guard the whole no-divide-by-zero property rests on.
    func testWidenedNeverReturnsADegenerateRange() {
        let instant = Date(timeIntervalSince1970: 1786075563)
        let range = TimelineModel.widened(instant, instant)
        XCTAssertEqual(range.upperBound.timeIntervalSince(range.lowerBound),
                       TimelineModel.minimumSpan, accuracy: 0.001)
        let backwards = TimelineModel.widened(instant, instant.addingTimeInterval(-500))
        XCTAssertLessThan(backwards.lowerBound, backwards.upperBound)
    }

    // MARK: - Filtering, rows and totals

    /// Plan §8.1: one filter state, read by the timeline. Filtering by role must
    /// subtract bars, and the summary strip must total only what is left.
    func testTheRoleFacetSubtractsBarsAndRetotalsTheSummaryStrip() throws {
        let store = try makeStore()
        store.clearFilters()
        let unfiltered = store.timeline
        XCTAssertGreaterThan(unfiltered.roleTotals.count, 1)

        let role = try XCTUnwrap(unfiltered.roleTotals.first)
        store.toggle(try XCTUnwrap(role.roleId), in: .role)
        let filtered = store.timeline

        XCTAssertEqual(filtered.roleTotals.count, 1)
        XCTAssertEqual(filtered.roleTotals.first?.roleId, role.roleId)
        XCTAssertLessThan(filtered.bars.count, unfiltered.bars.count)
        XCTAssertTrue(filtered.bars.allSatisfy { $0.roleId == role.roleId })
    }

    /// The strip totals **logged minutes**, not bar widths — at Sprint/Quarter
    /// a bar's width is calendar time, so measuring the drawing would be wrong.
    ///
    /// Checked per zoom against the logs in that zoom's own range, because the
    /// window length differs by zoom and there is no shared range to compare
    /// across.
    ///
    /// Both expectations are over the **non-recurrent** tasks, which is the
    /// population the Gantt plots: the strip has to total what the chart draws,
    /// and a strip that reported the recurrent tasks' hours next to a chart with
    /// no recurrent bars would be the exact dishonesty the summary exists to
    /// prevent.
    func testRoleTotalsCountLoggedMinutesInRangeNotBarWidths() throws {
        let snapshot = try loadSnapshot()
        let plotted = snapshot.tasks.filter { $0.status != .recurrent }
        let s103 = try sprint("Sprint 103", in: snapshot)
        for zoom in TimelineZoom.ordered {
            let data = build(snapshot, zoom: zoom, sprints: [s103.id])
            let expected = plotted
                .flatMap { logsInRange($0, data.range) }
                .reduce(0.0) { $0 + $1.minutes }
            XCTAssertEqual(data.totalMinutes, expected, accuracy: 0.01, "\(zoom)")
        }

        // And the Sprint-zoom figure over one sprint is that sprint's own
        // total, which is the number the rest of the app reports for it.
        let sprintZoom = build(snapshot, zoom: .sprint, sprints: [s103.id])
        let fromBindings = plotted
            .reduce(0.0) { $0 + $1.minutes(inSprint: s103.id) }
        XCTAssertEqual(sprintZoom.totalMinutes, fromBindings, accuracy: 1,
                       "the strip must agree with sprints_with_time, which is "
                       + "the timestamp-bucketed truth")

        // The exclusion is not free, and the test says how much it costs: the
        // recurrent tasks really do carry hours in this sprint, and they are
        // deliberately not in the figure above.
        let recurrentInSprint = snapshot.tasks
            .filter { $0.status == .recurrent }
            .reduce(0.0) { $0 + $1.minutes(inSprint: s103.id) }
        XCTAssertGreaterThan(recurrentInSprint, 0,
                             "the fixture's recurrent tasks must have time in "
                             + "Sprint 103, or this assertion is vacuous")
    }

    /// Rows are grouped into role sections in the sidebar's role order, and each
    /// section is flagged exactly once so the view can draw the break.
    func testRowsAreSectionedByRoleInSnapshotOrder() throws {
        let snapshot = try loadSnapshot()
        let all = Set(snapshot.sprints.map(\.id))
        let rows = build(snapshot, zoom: .quarter, sprints: all).rows
        XCTAssertGreaterThan(rows.count, 5)

        var seen: [Int] = []
        for row in rows where row.startsRoleSection { seen.append(row.roleOrder) }
        XCTAssertEqual(seen, seen.sorted(), "role sections are out of order")
        XCTAssertEqual(seen.count, Set(rows.map(\.roleOrder)).count,
                       "each role must start exactly one section")
        XCTAssertTrue(rows.first?.startsRoleSection ?? false)
    }

    /// A row exists for every task with a bar, and for no task without one — the
    /// 5 log-less tasks must not produce empty rows.
    func testTasksWithNoLogsGetNoRows() throws {
        let snapshot = try loadSnapshot()
        let all = Set(snapshot.sprints.map(\.id))
        let data = build(snapshot, zoom: .quarter, sprints: all)
        let logless = snapshot.tasks.filter { $0.logs.isEmpty }
        XCTAssertFalse(logless.isEmpty, "the fixture must keep its log-less tasks")
        for task in logless {
            XCTAssertNil(data.rows.first { $0.id == task.id },
                         "\(task.id) has no logs and must not get a row")
        }
        XCTAssertEqual(Set(data.rows.map(\.id)), Set(data.bars.map(\.rowId)))
    }

    // MARK: - The running timer

    func testTheActiveTimerRendersAsAGrowingBar() throws {
        let snapshot = try loadSnapshot()
        let task = try XCTUnwrap(snapshot.tasks.first { !$0.logs.isEmpty })
        let now = Date(timeIntervalSince1970: 1786075563)
        let timer = try JSONDecoder().decode(
            ActiveTimer.self,
            from: Data(#"{"task_id":"\#(task.id)","started_at":\#(now.timeIntervalSince1970 - 1800)}"#.utf8))

        let early = build(snapshot, zoom: .day, activeTimer: timer, now: now)
        let running = try XCTUnwrap(early.bars.first { $0.kind == .running })
        XCTAssertEqual(running.taskId, task.id)
        XCTAssertEqual(running.minutes, 30, accuracy: 0.1)
        XCTAssertEqual(running.end.timeIntervalSince1970, now.timeIntervalSince1970,
                       accuracy: 1)

        // It grows: the same timer a minute later is a minute longer.
        let later = build(snapshot, zoom: .day, activeTimer: timer,
                          now: now.addingTimeInterval(60))
        let grown = try XCTUnwrap(later.bars.first { $0.kind == .running })
        XCTAssertEqual(grown.minutes, 31, accuracy: 0.1)

        // And it is never counted as logged time.
        XCTAssertFalse(running.isApproximate)
        let row = try XCTUnwrap(early.rows.first { $0.id == task.id })
        XCTAssertEqual(row.minutes,
                       early.bars.filter { $0.taskId == task.id && $0.kind != .running }
                           .reduce(0) { $0 + $1.minutes },
                       accuracy: 0.01)
    }

    // MARK: - Zoom stepping and selection

    func testZoomStepsAndStopsAtTheEnds() {
        XCTAssertEqual(TimelineZoom.day.zoomedIn, nil)
        XCTAssertEqual(TimelineZoom.day.zoomedOut, .week)
        XCTAssertEqual(TimelineZoom.week.zoomedOut, .sprint)
        XCTAssertEqual(TimelineZoom.sprint.zoomedOut, .quarter)
        XCTAssertEqual(TimelineZoom.quarter.zoomedOut, nil)
        XCTAssertEqual(TimelineZoom.quarter.zoomedIn, .sprint)
    }

    func testStoreZoomCommandsWalkTheLevelsWithoutWrapping() throws {
        let store = try makeStore()
        store.timelineZoom = .day
        XCTAssertFalse(store.canZoomTimelineIn)
        store.zoomTimeline(in: true)
        XCTAssertEqual(store.timelineZoom, .day, "⌘+ must not wrap round to Quarter")

        store.zoomTimeline(in: false)
        XCTAssertEqual(store.timelineZoom, .week)
        store.timelineZoom = .quarter
        XCTAssertFalse(store.canZoomTimelineOut)
        store.zoomTimeline(in: false)
        XCTAssertEqual(store.timelineZoom, .quarter)
    }

    /// Clicking a bar selects the task, and that is **the Board's selection** —
    /// one property, not two (plan §10).
    func testSelectingABarWritesTheBoardSelection() throws {
        let store = try makeStore()
        store.clearFilters()
        let bar = try XCTUnwrap(store.timeline.bars.first)
        store.selectTask(bar.taskId)
        XCTAssertEqual(store.boardSelection, bar.taskId)
        XCTAssertEqual(BoardView.cursor(for: store).revalidated(store.boardSelection)
                       != nil || store.tasks.first { $0.id == bar.taskId }?.status == .recurrent,
                       true,
                       "a selected bar's task should be reachable on the board, "
                       + "unless it is a recurrent task that lives on the shelf")
    }

    // MARK: - Range clipping

    /// A log that straddles the range's edge is clipped to it rather than
    /// dropped, and one entirely outside is counted as hidden.
    func testBarsAreClippedToTheRangeAndOutsidersAreCounted() throws {
        let snapshot = try loadSnapshot()
        let s100 = try sprint("Sprint 100", in: snapshot)
        let data = build(snapshot, zoom: .sprint, sprints: [s100.id])

        XCTAssertGreaterThan(data.hiddenEntryCount, 0)
        for bar in data.bars {
            XCTAssertGreaterThanOrEqual(bar.start, data.range.lowerBound)
            XCTAssertLessThanOrEqual(bar.end, data.range.upperBound)
        }
    }

    /// Sprint boundary rules are drawn only at the two zooms wide enough to hold
    /// more than one, and only for sprints inside the range.
    func testSprintBoundariesAppearOnlyAtSprintAndQuarterZoom() throws {
        let snapshot = try loadSnapshot()
        let all = Set(snapshot.sprints.map(\.id))
        XCTAssertTrue(build(snapshot, zoom: .day, sprints: all).sprintBoundaries.isEmpty)
        XCTAssertTrue(build(snapshot, zoom: .week, sprints: all).sprintBoundaries.isEmpty)

        let quarter = build(snapshot, zoom: .quarter, sprints: all)
        XCTAssertFalse(quarter.sprintBoundaries.isEmpty)
        for boundary in quarter.sprintBoundaries {
            XCTAssertTrue(quarter.range.contains(boundary.date))
        }
        XCTAssertEqual(quarter.sprintBoundaries.map(\.date),
                       quarter.sprintBoundaries.map(\.date).sorted())
    }

    /// Sprint attribution on a bar comes from the cached sprint calendar, using
    /// the same half-open `[start, end)` rule as `wt.find_sprint_for_date`.
    func testBarsCarryTheSprintTheirTimeFallsIn() throws {
        let snapshot = try loadSnapshot()
        let s103 = try sprint("Sprint 103", in: snapshot)
        let data = build(snapshot, zoom: .week, sprints: [s103.id])
        for bar in data.bars {
            XCTAssertEqual(bar.sprintTitle, "Sprint 103", "bar \(bar.id)")
        }
    }

    /// A task span that crosses a sprint boundary says so. Labelling it with the
    /// sprint of its first log misattributes the rest — measured on a real bar
    /// captioned "Sprint 104" while half of it lay in Sprint 105.
    func testACrossSprintSpanIsLabelledWithBothSprints() throws {
        let snapshot = try loadSnapshot()
        let s104 = try sprint("Sprint 104", in: snapshot)
        let s105 = try sprint("Sprint 105", in: snapshot)
        let data = build(snapshot, zoom: .sprint, sprints: [s104.id, s105.id])

        let crossing = data.bars.first {
            ($0.sprintTitle ?? "").contains("–")
        }
        XCTAssertNotNil(crossing, "no span crossed the 104/105 boundary")
        XCTAssertEqual(crossing?.sprintTitle, "Sprint 104 – Sprint 105")

        // A span wholly inside one sprint still gets the single title.
        let inside = data.bars.filter { !($0.sprintTitle ?? "").contains("–") }
        XCTAssertFalse(inside.isEmpty)
        for bar in inside {
            XCTAssertTrue(bar.sprintTitle == "Sprint 104" || bar.sprintTitle == "Sprint 105",
                          "\(bar.sprintTitle ?? "nil")")
        }
    }

    // MARK: - Recurrent tasks live on the shelf, not the Gantt

    /// A perpetual task has no meaningful start or end, so it gets no bar — at
    /// **any** zoom, and selected on `status`, never on the title.
    func testRecurrentTasksAreNeverPlottedAtAnyZoom() throws {
        let snapshot = try loadSnapshot()
        let recurrent = Set(snapshot.tasks.filter { $0.status == .recurrent }.map(\.id))
        XCTAssertEqual(recurrent.count, 7, "the fixture's measured recurrent count")

        let all = Set(snapshot.sprints.map(\.id))
        for zoom in TimelineZoom.ordered {
            let data = build(snapshot, zoom: zoom, sprints: all)
            XCTAssertTrue(data.bars.allSatisfy { !recurrent.contains($0.taskId) },
                          "\(zoom): a recurrent task got a bar")
            XCTAssertTrue(data.rows.allSatisfy { !recurrent.contains($0.id) },
                          "\(zoom): a recurrent task got a row")
        }
    }

    /// The exclusion is **said out loud**, with the measured numbers, so the
    /// summary strip's total dropping by about a third is accounted for on
    /// screen rather than looking like a bug.
    func testTheExclusionIsCountedRatherThanSilent() throws {
        let snapshot = try loadSnapshot()
        let data = build(snapshot, zoom: .quarter, sprints: Set(snapshot.sprints.map(\.id)))
        XCTAssertEqual(data.excludedRecurrentTaskCount, 7)
        XCTAssertEqual(data.excludedRecurrentEntryCount, 146,
                       "the measured 146 recurrent log entries")

        let recurrentMinutes = snapshot.tasks
            .filter { $0.status == .recurrent }
            .flatMap(\.logs)
            .reduce(0.0) { $0 + $1.minutes }
        XCTAssertEqual(recurrentMinutes / 60, 123.8, accuracy: 0.1,
                       "the measured 123.8 hours this change removes")
    }

    /// "M off-range" must mean *reachable by moving the range*. Recurrent
    /// entries are not off-range, they are off this chart entirely, so folding
    /// them into that figure would send the user stepping through months looking
    /// for hours that live on the shelf.
    func testOffRangeCountExcludesRecurrentEntries() throws {
        let snapshot = try loadSnapshot()
        let s103 = try sprint("Sprint 103", in: snapshot)
        let data = build(snapshot, zoom: .sprint, sprints: [s103.id])

        let expected = snapshot.tasks
            .filter { $0.status != .recurrent }
            .flatMap(\.logs)
            .count { log in
                guard let span = TimelineModel.span(of: log) else { return false }
                return span.end < data.range.lowerBound || span.start > data.range.upperBound
            }
        XCTAssertEqual(data.hiddenEntryCount, expected)
        XCTAssertGreaterThan(data.hiddenEntryCount, 0, "otherwise this asserts nothing")
    }

    /// Excluding them inside the model is the same as never handing them over —
    /// which is what makes `Store.timelineTasks` (used by the empty state) and
    /// the builder agree by construction rather than by coincidence.
    func testExcludingRecurrentIsEquivalentToNotPassingThemIn() throws {
        let snapshot = try loadSnapshot()
        let all = Set(snapshot.sprints.map(\.id))
        let withRecurrent = build(snapshot, zoom: .week, sprints: all)
        let without = build(snapshot, zoom: .week, sprints: all,
                            tasks: snapshot.tasks.filter { $0.status != .recurrent })
        XCTAssertEqual(withRecurrent.bars.map(\.id), without.bars.map(\.id))
        XCTAssertEqual(withRecurrent.totalMinutes, without.totalMinutes, accuracy: 0.01)
        XCTAssertEqual(withRecurrent.hiddenEntryCount, without.hiddenEntryCount)
        XCTAssertEqual(withRecurrent.range, without.range,
                       "the domain must be derived from the plotted tasks only")
    }

    // MARK: - Timeframe navigation

    /// Every zoom steps, in both directions, and says so.
    func testEveryZoomStepsBackwardsAndForwards() throws {
        let store = try makeStore()
        store.clearFilters()
        for zoom in TimelineZoom.ordered {
            store.timelineToToday()
            store.timelineZoom = zoom
            let derived = store.timeline.range
            XCTAssertFalse(store.timeline.rangeSource.isNavigated, "\(zoom)")

            XCTAssertTrue(store.canStepTimeline(.previous), "\(zoom): cannot go back")
            store.stepTimeline(.previous)
            let back = store.timeline.range
            XCTAssertTrue(store.timeline.rangeSource.isNavigated, "\(zoom)")
            XCTAssertLessThan(back.lowerBound, derived.lowerBound, "\(zoom)")

            XCTAssertTrue(store.canStepTimeline(.next), "\(zoom): cannot come back")
            store.stepTimeline(.next)
            XCTAssertGreaterThan(store.timeline.range.lowerBound, back.lowerBound, "\(zoom)")
        }
    }

    /// **A step lands on the period it names**, at Day and at Week alike.
    ///
    /// Both cases were found by looking. Shifting the rolling 24-hour window
    /// gave a Day range running 14:18 → 14:18 captioned "showing Mon, Aug 3";
    /// shifting the current sprint's clamped window gave a **three-day** range
    /// captioned "showing week of Aug 3". A caption that names a period has to
    /// be that period, so every step snaps.
    func testStepsLandOnWholeCalendarPeriods() throws {
        let calendar = Calendar.current
        let store = try makeStore()
        store.clearFilters()

        store.timelineZoom = .day
        store.stepTimeline(.previous)
        var range = store.timeline.range
        XCTAssertEqual(range.lowerBound, calendar.startOfDay(for: range.lowerBound),
                       "a Day window must start at midnight")
        XCTAssertEqual(range.upperBound.timeIntervalSince(range.lowerBound),
                       86_400, accuracy: 3_600, "and last one day")
        XCTAssertEqual(store.timeline.rangeSource,
                       .navigated(label: TimelineNavigation.dayLabel(range.lowerBound)))
        // From there each further press is exactly one day.
        store.stepTimeline(.previous)
        XCTAssertEqual(range.lowerBound.timeIntervalSince(store.timeline.range.lowerBound),
                       86_400, accuracy: 3_600)

        store.timelineToToday()
        store.timelineZoom = .week
        store.stepTimeline(.previous)
        range = store.timeline.range
        let week = try XCTUnwrap(calendar.dateInterval(of: .weekOfYear,
                                                       for: range.lowerBound))
        XCTAssertEqual(range.lowerBound, week.start,
                       "a Week window must start on the week's first day")
        XCTAssertEqual(range.upperBound, week.end,
                       "and end on its last")
        XCTAssertEqual(store.timeline.rangeSource,
                       .navigated(label: TimelineNavigation.weekLabel(range.lowerBound)))
        store.stepTimeline(.previous)
        XCTAssertEqual(range.lowerBound.timeIntervalSince(store.timeline.range.lowerBound),
                       7 * 86_400, accuracy: 3_600)
    }

    /// Sprint stepping walks the **cached sprint calendar**, landing on real
    /// boundaries rather than on a nominal fortnight.
    func testSprintSteppingWalksTheCachedSprintCalendar() throws {
        let store = try makeStore()
        let snapshot = try XCTUnwrap(store.snapshot)
        let s103 = try sprint("Sprint 103", in: snapshot)
        let s102 = try sprint("Sprint 102", in: snapshot)
        store.clearFilters()
        store.toggle(s103.id, in: .sprint)
        store.timelineZoom = .sprint
        XCTAssertEqual(store.timeline.range.lowerBound, try XCTUnwrap(s103.start))

        store.stepTimeline(.previous)
        XCTAssertEqual(store.timeline.range.lowerBound, try XCTUnwrap(s102.start))
        XCTAssertEqual(store.timeline.range.upperBound, try XCTUnwrap(s102.end))
        XCTAssertEqual(store.timeline.rangeSource, .navigated(label: "Sprint 102"))
    }

    /// Quarter stepping moves whole calendar quarters and names them.
    func testQuarterSteppingMovesWholeCalendarQuarters() throws {
        let calendar = Calendar.current
        let july = try XCTUnwrap(calendar.date(from: DateComponents(year: 2026, month: 7, day: 15)))
        let bounds = TimelineModel.widened(
            try XCTUnwrap(calendar.date(from: DateComponents(year: 2025, month: 1, day: 1))),
            try XCTUnwrap(calendar.date(from: DateComponents(year: 2027, month: 1, day: 1))))
        let current = TimelineNavigation.period(of: .quarter, containing: july,
                                                sprints: [], calendar: calendar)
        XCTAssertEqual(current.label, "Q3 2026")

        let previous = try XCTUnwrap(
            TimelineNavigation.step(.previous, from: current.range, zoom: .quarter,
                                    sprints: [], bounds: bounds, calendar: calendar))
        XCTAssertEqual(previous.label, "Q2 2026")
        XCTAssertEqual(calendar.component(.month, from: previous.start), 4)

        let backAgain = try XCTUnwrap(
            TimelineNavigation.step(.next, from: previous.range, zoom: .quarter,
                                    sprints: [], bounds: bounds, calendar: calendar))
        XCTAssertEqual(backAgain.label, "Q3 2026")
        XCTAssertEqual(backAgain.start, current.start)
    }

    /// **No scrolling into empty infinity.** Stepping past the data returns
    /// `nil`, which is the same call the button's `disabled` reads — so what is
    /// greyed out and what would no-op cannot drift apart.
    func testSteppingStopsAtTheEndsOfTheDataRatherThanScrollingForever() throws {
        let calendar = Calendar.current
        let start = try XCTUnwrap(calendar.date(from: DateComponents(year: 2026, month: 3, day: 1)))
        let end = try XCTUnwrap(calendar.date(from: DateComponents(year: 2026, month: 4, day: 1)))
        let bounds = TimelineModel.widened(start, end)

        var window = start...start.addingTimeInterval(7 * 86_400)
        var steps = 0
        while let next = TimelineNavigation.step(.previous, from: window, zoom: .week,
                                                 sprints: [], bounds: bounds,
                                                 calendar: calendar) {
            window = next.range
            steps += 1
            XCTAssertLessThan(steps, 20, "stepping past the data never terminated")
        }
        XCTAssertGreaterThan(steps, 0, "the first step back should have been allowed")

        // And the same property through the store, over the real fixture.
        let store = try makeStore()
        store.clearFilters()
        store.timelineZoom = .week
        var walked = 0
        while store.canStepTimeline(.previous) && walked < 200 {
            store.stepTimeline(.previous)
            walked += 1
        }
        XCTAssertLessThan(walked, 200, "the store walked off the end of the data")
        XCTAssertFalse(store.canStepTimeline(.previous))
        XCTAssertTrue(store.canStepTimeline(.next), "the way back must stay open")
    }

    /// Zooming while navigated keeps the **period you were looking at** rather
    /// than snapping back to today: the anchor is re-derived for the new zoom.
    func testZoomingWhileNavigatedKeepsThePeriodYouWereLookingAt() throws {
        let store = try makeStore()
        let snapshot = try XCTUnwrap(store.snapshot)
        let s103 = try sprint("Sprint 103", in: snapshot)
        let s102 = try sprint("Sprint 102", in: snapshot)
        store.clearFilters()
        store.toggle(s103.id, in: .sprint)
        store.timelineZoom = .sprint
        store.stepTimeline(.previous)          // → Sprint 102

        store.timelineZoom = .day
        let day = store.timeline
        XCTAssertTrue(day.rangeSource.isNavigated)
        XCTAssertGreaterThanOrEqual(day.range.lowerBound, try XCTUnwrap(s102.start))
        XCTAssertLessThanOrEqual(day.range.upperBound, try XCTUnwrap(s102.end))
    }

    // MARK: The Sprint facet ↔ navigation rule (one range, last touched wins)

    /// Navigating releases the Sprint facet — otherwise the axis would step onto
    /// a period whose work the facet had just filtered out — and **Today gives
    /// it back**. The two controls are exact inverses.
    func testNavigatingReleasesTheSprintFacetAndTodayRestoresIt() throws {
        let store = try makeStore()
        let snapshot = try XCTUnwrap(store.snapshot)
        let s103 = try sprint("Sprint 103", in: snapshot)
        store.clearFilters()
        store.toggle(s103.id, in: .sprint)
        store.timelineZoom = .sprint
        XCTAssertEqual(store.timeline.rangeSource, .sprintFacet(titles: ["Sprint 103"]))
        XCTAssertFalse(store.canReturnToToday, "nothing to undo before navigating")

        store.stepTimeline(.previous)
        XCTAssertTrue(store.filter.sprints.isEmpty, "the facet must be released")
        XCTAssertTrue(store.timeline.rangeSource.isNavigated)
        XCTAssertTrue(store.canReturnToToday)

        store.timelineToToday()
        XCTAssertEqual(store.filter.sprints, [s103.id], "Today must hand the facet back")
        XCTAssertEqual(store.timeline.rangeSource, .sprintFacet(titles: ["Sprint 103"]))
        XCTAssertFalse(store.canReturnToToday)
    }

    /// Touching the Sprint facet makes it authoritative again: the anchor goes.
    /// This is the other half of "last touched wins", and it is what stops the
    /// range having two owners.
    func testTouchingTheSprintFacetClearsTheAnchor() throws {
        let store = try makeStore()
        let snapshot = try XCTUnwrap(store.snapshot)
        let s103 = try sprint("Sprint 103", in: snapshot)
        store.clearFilters()
        store.timelineZoom = .week
        store.stepTimeline(.previous)
        XCTAssertTrue(store.timeline.rangeSource.isNavigated)

        store.toggle(s103.id, in: .sprint)
        XCTAssertEqual(store.timeline.rangeSource, .sprintFacet(titles: ["Sprint 103"]))
        XCTAssertNil(store.timelineAnchor)
        XCTAssertFalse(store.canReturnToToday,
                       "a facet the user set is not something Today should undo")
    }

    /// A role filter is **not** a range source, so it must not disturb the
    /// anchor — only the Sprint facet competes for the viewport.
    func testANonSprintFilterLeavesTheAnchorAlone() throws {
        let store = try makeStore()
        store.clearFilters()
        store.timelineZoom = .week
        store.stepTimeline(.previous)
        let navigated = store.timeline.range

        let role = try XCTUnwrap(store.timeline.roleTotals.first?.roleId)
        store.toggle(role, in: .role)
        XCTAssertTrue(store.timeline.rangeSource.isNavigated)
        XCTAssertEqual(store.timeline.range, navigated)
    }

    /// **The chart's y domain is never empty**, because Swift Charts traps when
    /// a categorical domain goes from non-empty to empty:
    /// *"CGFloat value cannot be converted to Int because it is outside the
    /// representable range"*, thrown from its vertical-scroll path, which
    /// divides the plot height by the category count.
    ///
    /// This is a real crash, found by stepping the range onto a period with no
    /// logged time — a period the Gantt had no way to reach before this change.
    /// The assertion lives here rather than in the view because it is a
    /// property of a value, and a crash in a chart is not something a test can
    /// otherwise catch.
    func testTheChartsYDomainIsNeverEmpty() throws {
        let empty = TimelineData.empty(zoom: .week, now: .now)
        XCTAssertTrue(empty.rows.isEmpty)
        XCTAssertEqual(TimelineView.yDomain(for: empty).count, 1,
                       "an empty categorical domain crashes Swift Charts")

        // And when there are rows it is exactly them, in their order.
        let snapshot = try loadSnapshot()
        let data = build(snapshot, zoom: .week, sprints: Set(snapshot.sprints.map(\.id)))
        XCTAssertFalse(data.rows.isEmpty)
        XCTAssertEqual(TimelineView.yDomain(for: data), data.rows.map(\.id))
        XCTAssertFalse(TimelineView.yDomain(for: data).contains(TimelineView.placeholderRowID))
    }

    /// **The default view must be navigable.** Sprint 106 opens with no logged
    /// time, so bounds computed from the sprint-filtered tasks would be a single
    /// instant and both buttons would be dead on exactly the screen the feature
    /// exists for. Stepping back must reach the work.
    func testTheEmptyCurrentSprintCanStillBeSteppedAwayFrom() throws {
        let store = try makeStore(currentSprintTitle: "Sprint 106")
        XCTAssertTrue(store.timeline.isEmpty, "precondition: the default view is empty")
        XCTAssertTrue(store.canStepTimeline(.previous),
                      "Previous was dead on the empty current sprint")

        var steps = 0
        while store.timeline.isEmpty && steps < 6 {
            store.stepTimeline(.previous)
            steps += 1
        }
        XCTAssertFalse(store.timeline.isEmpty,
                       "stepping back from the empty sprint never reached any work")
        XCTAssertTrue(store.timeline.rangeSource.isNavigated)
    }
}
