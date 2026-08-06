import Foundation

/// Duration formatting shared by cards, column headers and the sidebar.
///
/// Mirrors `wt.fmt_mins()`: whole hours and minutes, never decimal hours, so a
/// number here reads the same as the same number in the CLI or the TUI.
enum Duration {
    /// `"6h 15m"`, `"45m"`, `"—"` for zero.
    static func format(minutes: Double) -> String {
        guard minutes > 0 else { return "—" }
        let total = Int(minutes.rounded())
        let hours = total / 60
        let mins = total % 60
        if hours > 0 && mins > 0 { return "\(hours)h \(mins)m" }
        if hours > 0 { return "\(hours)h" }
        return "\(mins)m"
    }

    /// `"6h 15m"` but `"0m"` rather than an em dash, for column totals where a
    /// literal zero is more informative than "nothing".
    static func formatZeroed(minutes: Double) -> String {
        minutes > 0 ? format(minutes: minutes) : "0m"
    }

    /// A running timer, as `m:ss` under an hour and `h:mm:ss` above.
    /// Byte-identical to `formatElapsed()` in workload-macos-monitor.
    static func formatElapsed(_ seconds: TimeInterval) -> String {
        let total = Int(seconds.rounded(.down))
        let hours = total / 3600
        let minutes = (total % 3600) / 60
        let secs = total % 60
        if hours > 0 {
            return String(format: "%d:%02d:%02d", hours, minutes, secs)
        }
        return String(format: "%d:%02d", minutes, secs)
    }

    /// `"2 days ago"` style, for a task's last-logged timestamp.
    static func relative(since epoch: TimeInterval, now: Date = Date()) -> String {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        return formatter.localizedString(for: Date(timeIntervalSince1970: epoch), relativeTo: now)
    }
}
