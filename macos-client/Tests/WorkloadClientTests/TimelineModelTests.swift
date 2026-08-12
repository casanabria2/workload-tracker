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
                       now: Date = Date(timeIntervalSince1970: 1786075563)) -> TimelineData {
        TimelineModel.build(tasks: tasks ?? snapshot.tasks,
                            roles: snapshot.roles,
                            sprints: snapshot.sprints,
                            currentSprint: snapshot.currentSprint,
                            selectedSprintIDs: selected,
                            zoom: zoom,
                            activeTimer: activeTimer,
                            now: now)
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
    func testRoleTotalsCountLoggedMinutesInRangeNotBarWidths() throws {
        let snapshot = try loadSnapshot()
        let s103 = try sprint("Sprint 103", in: snapshot)
        for zoom in TimelineZoom.ordered {
            let data = build(snapshot, zoom: zoom, sprints: [s103.id])
            let expected = snapshot.tasks
                .flatMap { logsInRange($0, data.range) }
                .reduce(0.0) { $0 + $1.minutes }
            XCTAssertEqual(data.totalMinutes, expected, accuracy: 0.01, "\(zoom)")
        }

        // And the Sprint-zoom figure over one sprint is that sprint's own
        // total, which is the number the rest of the app reports for it.
        let sprintZoom = build(snapshot, zoom: .sprint, sprints: [s103.id])
        let fromBindings = snapshot.tasks
            .reduce(0.0) { $0 + $1.minutes(inSprint: s103.id) }
        XCTAssertEqual(sprintZoom.totalMinutes, fromBindings, accuracy: 1,
                       "the strip must agree with sprints_with_time, which is "
                       + "the timestamp-bucketed truth")
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
}
