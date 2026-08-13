import Accessibility
import Foundation
import SwiftUI

/// `.accessibilityChartDescriptor` for the Gantt (plan §11).
///
/// Swift Charts gives every mark an accessibility element for free, so VoiceOver
/// can already walk the bars one by one. What it cannot do without a descriptor
/// is give the chart a **shape**: the Audio Graph rotor, the x/y axis names and
/// units, the summary that says how many tasks over what span. On a chart whose
/// row count changes with the filter and whose bar *meaning* changes with the
/// zoom, that summary is the difference between a readable chart and 200
/// unlabelled elements.
///
/// Two decisions worth recording:
///
/// * **The x axis is a numeric axis of dates**, described in the range's own
///   units — `AXNumericDataAxisDescriptor` takes `Double`s, so the values are
///   seconds since the epoch and the formatter turns each back into a date at
///   the resolution the visible range warrants (times within a day, dates
///   across weeks). A categorical axis would lose ordering, which is the one
///   thing a timeline must keep.
/// * **A data point's value is its minutes, not its bar width.** At Sprint and
///   Quarter zoom a bar is a task *span* — the calendar time work was spread
///   over — which is not the time worked. Sonifying the width would tell a
///   VoiceOver user that a task nobody touched for a fortnight was a fortnight
///   of work.
///
/// The descriptor is built from `TimelineData`, the same value the chart
/// renders, so the two cannot describe different charts.
struct TimelineChartDescriptor: AXChartDescriptorRepresentable {
    let data: TimelineData

    func makeChartDescriptor() -> AXChartDescriptor {
        let lower = data.range.lowerBound.timeIntervalSince1970
        let upper = data.range.upperBound.timeIntervalSince1970
        let spansOneDay = (upper - lower) <= 2.5 * 86_400

        let xAxis = AXNumericDataAxisDescriptor(
            title: "Time",
            range: lower...upper,
            gridlinePositions: data.sprintBoundaries.map { $0.date.timeIntervalSince1970 },
            valueDescriptionProvider: { value in
                let date = Date(timeIntervalSince1970: value)
                return spansOneDay
                    ? date.formatted(.dateTime.hour().minute())
                    : date.formatted(.dateTime.month(.abbreviated).day())
            })

        // Minutes, so the audio graph's pitch tracks time worked.
        let maxMinutes = max(data.bars.map(\.minutes).max() ?? 0, 1)
        let yAxis = AXNumericDataAxisDescriptor(
            title: "Logged time",
            range: 0...maxMinutes,
            gridlinePositions: [],
            valueDescriptionProvider: { Duration.format(minutes: $0) })

        // The task each bar belongs to, as a third axis — this is what lets the
        // rotor move between tasks rather than only along time.
        let taskAxis = AXCategoricalDataAxisDescriptor(
            title: "Task",
            categoryOrder: data.rows.map(\.title))

        // One series per **role**, matching the chart's own sectioning and the
        // summary strip above it. One series per task would produce as many
        // series as rows, which the rotor turns into an unusable list.
        //
        // **Grouped from the bars, not from `roleTotals`.** The first cut
        // iterated the totals and looked bars up by role, which silently
        // dropped any bar whose role has no total — and the running-timer bar
        // is exactly that: it is not a log entry, so it contributes no logged
        // minutes and its role can be absent from the strip entirely. Caught by
        // `testChartDescriptorCountsMatchTheData`, which asserts every bar
        // reaches a series.
        let byRole = Dictionary(grouping: data.bars) { $0.roleLabel }
        let order = Dictionary(data.roleTotals.enumerated().map { ($1.label, $0) },
                               uniquingKeysWith: { first, _ in first })
        let totals = Dictionary(data.roleTotals.map { ($0.label, $0.minutes) },
                                uniquingKeysWith: +)
        let series: [AXDataSeriesDescriptor] = byRole.keys
            // The strip's order first, so the rotor reads roles in the order
            // the eye does; anything the strip does not know about follows,
            // alphabetically, rather than in `Dictionary` order.
            .sorted { (order[$0] ?? .max, $0) < (order[$1] ?? .max, $1) }
            .map { label in
                let bars = byRole[label] ?? []
                let name = totals[label]
                    .map { "\(label), \(Duration.formatZeroed(minutes: $0))" } ?? label
                return AXDataSeriesDescriptor(name: name, isContinuous: false,
                                              dataPoints: bars.map(Self.point(for:)))
            }

        return AXChartDescriptor(
            title: "Logged time by task",
            summary: summary,
            xAxis: xAxis,
            yAxis: yAxis,
            additionalAxes: [taskAxis],
            series: series.isEmpty ? [Self.emptySeries] : series)
    }

    func updateChartDescriptor(_ descriptor: AXChartDescriptor) {
        // Rebuilt wholesale rather than patched: every field depends on the
        // range, the zoom and the filter, and a partial update is how a
        // descriptor comes to describe a chart that is no longer on screen.
        let fresh = makeChartDescriptor()
        descriptor.title = fresh.title
        descriptor.summary = fresh.summary
        descriptor.xAxis = fresh.xAxis
        descriptor.yAxis = fresh.yAxis
        descriptor.additionalAxes = fresh.additionalAxes
        descriptor.series = fresh.series
    }

    /// The spoken overview. Pure and `internal` so it can be asserted without a
    /// screen reader — VoiceOver cannot be driven headlessly, so the string is
    /// what is provable.
    var summary: String {
        let start = data.range.lowerBound.formatted(.dateTime.month(.abbreviated).day())
        let end = data.range.upperBound.formatted(.dateTime.month(.abbreviated).day())
        guard !data.bars.isEmpty else {
            return "No logged time between \(start) and \(end). "
                + data.rangeSource.explanation + "."
        }
        let unit = data.zoom.granularity == .logEntry ? "session" : "task span"
        let count = data.bars.count
        var text = "\(count) \(unit)\(count == 1 ? "" : "s") across "
            + "\(data.rows.count) task\(data.rows.count == 1 ? "" : "s"), "
            + "\(start) to \(end), totalling "
            + Duration.formatZeroed(minutes: data.totalMinutes) + "."
        if data.hasApproximateBars {
            text += " Some entries record the date but not the time of day; "
                + "their position within a day is not meaningful."
        }
        if data.excludedRecurrentTaskCount > 0 {
            text += " \(data.excludedRecurrentTaskCount) recurrent task"
                + (data.excludedRecurrentTaskCount == 1 ? " is" : "s are")
                + " on the shelf and not plotted here."
        }
        return text
    }

    private static func point(for bar: TimelineBar) -> AXDataPoint {
        AXDataPoint(x: xValue(for: bar),
                    y: yValue(for: bar),
                    additionalValues: [.category(bar.taskTitle)],
                    label: bar.accessibilityDescription)
    }

    /// A point's x, in seconds since the epoch.
    ///
    /// Split out of `point(for:)` and `internal` so the axis decisions are
    /// assertable: `AXDataPointValue` is a bridged ObjC class whose value
    /// cannot be read back out in Swift, so a test can only reach these
    /// through the functions that produce them.
    static func xValue(for bar: TimelineBar) -> Double {
        bar.start.timeIntervalSince1970
    }

    /// A point's y: **minutes worked, never the bar's width in time.**
    ///
    /// At Sprint and Quarter zoom a bar is a task span — the calendar time work
    /// was spread over — so the two differ by orders of magnitude. Sonifying
    /// the width would tell a VoiceOver user that a task nobody touched for a
    /// fortnight was a fortnight of work.
    static func yValue(for bar: TimelineBar) -> Double {
        bar.minutes
    }

    /// `AXChartDescriptor` requires at least one series; an empty range is a
    /// real state here (the morning a sprint opens), so it gets a named empty
    /// one rather than a crash or a lie.
    ///
    /// A function, not a `static let`: `AXDataSeriesDescriptor` is a
    /// non-`Sendable` reference type, so a shared instance is a data race the
    /// compiler is right to refuse — and handing every descriptor the same
    /// mutable object would be wrong even if it compiled.
    private static var emptySeries: AXDataSeriesDescriptor {
        AXDataSeriesDescriptor(name: "No logged time",
                               isContinuous: false, dataPoints: [])
    }
}
