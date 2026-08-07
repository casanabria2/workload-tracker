import SwiftUI

/// The Log Time sheet (plan §9).
///
/// Local-only: `POST /v1/tasks/{id}/logs` appends a log entry and touches no
/// GitHub issue. It still gets a sheet rather than acting on a menu click,
/// because it appends to the owner's irreplaceable work history and the amount
/// should be typed and read back rather than guessed at.
struct LogTimeSheetView: View {
    @Environment(Store.self) private var store
    let sheet: LogSheetState

    @State private var minutesText: String = "30"
    @State private var note: String = ""
    @FocusState private var minutesFocused: Bool

    /// The parsed amount, accepting either `90` or `1h 30m` / `1.5h`.
    private var minutes: Double? { Self.parse(minutesText) }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Log time to “\(sheet.title)”")
                    .font(.title3.weight(.semibold)).lineLimit(2)
                Text("Adds a time entry. This does not touch GitHub.")
                    .font(.callout).foregroundStyle(.secondary)
            }
            Divider()

            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 10) {
                GridRow {
                    Text("Amount").gridColumnAlignment(.trailing)
                    VStack(alignment: .leading, spacing: 3) {
                        TextField("30", text: $minutesText)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 140)
                            .focused($minutesFocused)
                        Text(minutes.map { "= \(Duration.format(minutes: $0))" }
                             ?? "Minutes, or “1h 30m”.")
                            .font(.caption)
                            .foregroundStyle(minutes == nil ? .secondary : .primary)
                    }
                }
                GridRow {
                    Text("Note").gridColumnAlignment(.trailing)
                    TextField("Manual entry", text: $note)
                        .textFieldStyle(.roundedBorder)
                        .frame(maxWidth: 380)
                }
            }

            if let error = sheet.error {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(.callout).foregroundStyle(.orange).textSelection(.enabled)
            }

            Divider()
            HStack {
                Spacer()
                Button("Cancel") { store.dismissLogSheet() }
                    .keyboardShortcut(.cancelAction)
                Button("Log Time") { submit() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(minutes == nil || sheet.isSubmitting)
            }
        }
        .padding(20)
        .frame(minWidth: 460)
        .onAppear { minutesFocused = true }
        .interactiveDismissDisabled(sheet.isSubmitting)
    }

    private func submit() {
        guard let minutes else { return }
        _Concurrency.Task { await store.confirmLogTime(minutes: minutes, note: note) }
    }

    /// `"90"` → 90, `"1.5h"` → 90, `"1h 30m"` → 90, `"45m"` → 45.
    ///
    /// `nil` for anything unparseable or non-positive, which is what disables
    /// the button — the store refuses a non-positive amount as well, so this is
    /// affordance rather than the guard.
    static func parse(_ text: String) -> Double? {
        let trimmed = text.trimmingCharacters(in: .whitespaces).lowercased()
        guard !trimmed.isEmpty else { return nil }
        if let plain = Double(trimmed) { return plain > 0 ? plain : nil }

        var total: Double = 0
        var matched = false
        var scanner = Substring(trimmed)
        while !scanner.isEmpty {
            scanner = scanner.drop(while: \.isWhitespace)
            let numberPart = scanner.prefix { $0.isNumber || $0 == "." }
            guard !numberPart.isEmpty, let value = Double(numberPart) else { break }
            scanner = scanner.dropFirst(numberPart.count).drop(while: \.isWhitespace)
            let unit = scanner.first
            if unit == "h" { total += value * 60; scanner = scanner.dropFirst(); matched = true }
            else if unit == "m" { total += value; scanner = scanner.dropFirst(); matched = true }
            else { total += value; matched = true }
        }
        guard matched, total > 0 else { return nil }
        return total
    }
}
