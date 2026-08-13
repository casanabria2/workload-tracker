import XCTest
@testable import WorkloadClient

/// Plan §11's accessibility items, asserted where they can be.
///
/// **What this cannot do:** VoiceOver cannot be driven headlessly, so nothing
/// here proves that anything is *spoken*. What it proves is that the strings
/// VoiceOver would read are correct, present, and derived from the same values
/// the screen is drawn from — which is the half that silently rots. The other
/// half (that the modifiers are attached at all) is inspected in the views and
/// reported as inspected, not as tested.
@MainActor
final class AccessibilityTests: XCTestCase {

    // MARK: - Fixtures

    private func loadSnapshot(_ name: String = "snapshot") throws -> Snapshot {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: name, withExtension: "json",
                              subdirectory: "Fixtures"))
        return try JSONDecoder().decode(Snapshot.self, from: Data(contentsOf: url))
    }

    private func makeStore() throws -> Store {
        let transport = StubTransport()
        return Store(client: transport.makeClient(), snapshot: try loadSnapshot())
    }

    // MARK: - Filter announcements

    /// The count is the part that is otherwise invisible. "Filtering by Role"
    /// describes a filter that removes nothing and one that removes everything
    /// identically.
    func testAnnouncementCarriesTheResultingCount() throws {
        let store = try makeStore()
        store.applyFilter(FilterState())
        let total = store.tasks.count
        XCTAssertEqual(store.filterAnnouncement,
                       "Filters cleared. \(total) tasks shown.")
    }

    func testAnnouncementNamesTheFacetsInForce() throws {
        let store = try makeStore()
        let role = try XCTUnwrap(store.roles.first)
        store.applyFilter(FilterState(facet: .role, value: role.id))
        let text = store.filterAnnouncement
        XCTAssertTrue(text.hasPrefix("Filtering by Role: "), text)
        XCTAssertTrue(text.contains(role.displayName), text)
        XCTAssertTrue(text.contains(" of \(store.tasks.count) tasks shown."), text)
    }

    /// Facet **labels**, not raw ids: a role announced as `iron infusion` is
    /// the data file's key, not the name on screen.
    func testAnnouncementUsesLabelsNotIdentifiers() throws {
        let store = try makeStore()
        let role = try XCTUnwrap(store.roles.first { $0.displayName != $0.id })
        store.applyFilter(FilterState(facet: .role, value: role.id))
        XCTAssertTrue(store.filterAnnouncement.contains(role.displayName))
        XCTAssertFalse(store.filterAnnouncement.contains("Role: \(role.id)"))
    }

    func testAnnouncementIncludesTheSearchText() throws {
        let store = try makeStore()
        store.applyFilter(FilterState(text: "widget"))
        XCTAssertTrue(store.filterAnnouncement.contains("search “widget”"),
                      store.filterAnnouncement)
    }

    /// A filter that admits nothing still announces a number, so a VoiceOver
    /// user hears the board empty rather than discovering it silently.
    func testAnnouncementSaysZeroWhenNothingPasses() throws {
        let store = try makeStore()
        store.applyFilter(FilterState(text: "\u{1}nothing matches this"))
        XCTAssertTrue(store.filterAnnouncement.contains("0 of "),
                      store.filterAnnouncement)
    }

    // MARK: - The Gantt's chart descriptor

    func testChartDescriptorSummaryDescribesAPopulatedRange() throws {
        let store = try makeStore()
        store.clearFilters()
        store.timelineZoom = .quarter
        let data = store.timeline
        try XCTSkipIf(data.bars.isEmpty, "the fixture plots nothing at quarter zoom")
        let summary = TimelineChartDescriptor(data: data).summary
        XCTAssertTrue(summary.contains("totalling"), summary)
        XCTAssertTrue(summary.contains("task"), summary)
    }

    /// The empty range is a real state — the morning a sprint opens — and it
    /// must describe itself rather than producing an empty summary.
    func testChartDescriptorSummaryDescribesAnEmptyRange() {
        let data = TimelineData.empty(zoom: .week, now: Date(timeIntervalSince1970: 1_775_000_000))
        let summary = TimelineChartDescriptor(data: data).summary
        XCTAssertTrue(summary.hasPrefix("No logged time between"), summary)
        XCTAssertFalse(summary.isEmpty)
    }

    /// `AXChartDescriptor` requires at least one series, and an empty chart is
    /// reachable, so the empty case must still produce a well-formed descriptor
    /// rather than trapping.
    func testChartDescriptorIsWellFormedWhenEmpty() {
        let data = TimelineData.empty(zoom: .week, now: Date(timeIntervalSince1970: 1_775_000_000))
        let descriptor = TimelineChartDescriptor(data: data).makeChartDescriptor()
        XCTAssertFalse(descriptor.series.isEmpty)
        XCTAssertEqual(descriptor.series.first?.dataPoints.count, 0)
        XCTAssertFalse(descriptor.additionalAxes.isEmpty)
    }

    /// A data point's value is its **minutes**, never its bar width. At Sprint
    /// and Quarter zoom a bar is a task span — the calendar time work was spread
    /// over — and sonifying that would tell a VoiceOver user a fortnight of
    /// neglect was a fortnight of work.
    func testChartDataPointsCarryMinutesNotSpanWidth() {
        // A synthetic span rather than one from the fixture: the property is
        // about the *gap* between minutes and width, and it must hold for a bar
        // where the two differ by orders of magnitude — 90 minutes of work
        // spread over a fortnight.
        let start = Date(timeIntervalSince1970: 1_775_000_000)
        let span = TimelineBar(
            id: "bar", taskId: "t", taskTitle: "Spread thin", roleId: "r",
            roleLabel: "Role", kind: .taskSpan,
            start: start, end: start.addingTimeInterval(14 * 86_400),
            minutes: 90, detail: "", sprintTitle: "Sprint 101",
            entryCount: 3, approximateCount: 0)

        // `AXDataPointValue` is a bridged ObjC class whose value cannot be read
        // back out in Swift, so the assertion goes at the function that
        // produces it rather than at the finished descriptor.
        XCTAssertEqual(TimelineChartDescriptor.yValue(for: span), 90)
        let widthMinutes = span.end.timeIntervalSince(span.start) / 60
        XCTAssertEqual(widthMinutes, 20_160, "the fixture's span is not wide")
        XCTAssertNotEqual(TimelineChartDescriptor.yValue(for: span), widthMinutes)
        XCTAssertEqual(TimelineChartDescriptor.xValue(for: span),
                       span.start.timeIntervalSince1970)
    }

    /// Every bar reaches a series — including the running-timer bar, whose role
    /// contributes no *logged* minutes and can therefore be missing from the
    /// summary strip the series used to be built from.
    func testTheRunningTimerBarIsNotDroppedFromTheDescriptor() throws {
        let store = try makeStore()
        store.clearFilters()
        store.timelineZoom = .quarter
        let data = store.timeline
        try XCTSkipIf(!data.bars.contains { $0.kind == .running },
                      "the fixture plots no running bar at quarter zoom")
        let descriptor = TimelineChartDescriptor(data: data).makeChartDescriptor()
        XCTAssertEqual(descriptor.series.flatMap(\.dataPoints).count, data.bars.count)
    }

    /// The descriptor is built from the same `TimelineData` the marks are, so
    /// the two cannot describe different charts.
    func testChartDescriptorCountsMatchTheData() throws {
        let store = try makeStore()
        store.clearFilters()
        store.timelineZoom = .quarter
        let data = store.timeline
        try XCTSkipIf(data.bars.isEmpty)
        let descriptor = TimelineChartDescriptor(data: data).makeChartDescriptor()
        XCTAssertEqual(descriptor.series.flatMap(\.dataPoints).count, data.bars.count)
    }

    // MARK: - Card actions

    /// The card's custom action set has to *be* the context menu, not a subset
    /// chosen by hand — that is the difference between "accessible" and
    /// "accessible to a smaller app".
    func testTheCardActionSetMatchesTheContextMenu() throws {
        let snapshot = try loadSnapshot()
        let card = try XCTUnwrap(snapshot.tasks.first { $0.status == .inProgress })
        let menu = TaskAction.menu(for: card)
        XCTAssertEqual(menu.count, TaskAction.boardMenu.count)
        // Every non-destructive item is reachable; the destructive one is not
        // on a board card at all.
        XCTAssertFalse(menu.contains(.shelf(.endSeries)))
    }
}
