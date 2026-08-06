import SwiftUI

/// The "daemon unreachable" pane.
///
/// This is the state that must **never** be confused with an empty board. A
/// board with zero cards means the tracker has no tasks; this means the client
/// could not reach the daemon at all, and it says so, names the URL it tried,
/// and gives the command that diagnoses it.
struct UnreachableView: View {
    let reason: String
    let baseURL: String
    let retry: () -> Void

    var body: some View {
        ContentUnavailableView {
            Label("Daemon unreachable", systemImage: "bolt.horizontal.circle")
                .symbolRenderingMode(.hierarchical)
        } description: {
            VStack(spacing: 8) {
                Text(reason)
                Text(baseURL)
                    .font(.caption.monospaced())
                    .foregroundStyle(.tertiary)
                    .textSelection(.enabled)
                Text("This is not an empty board — no snapshot could be fetched, "
                     + "so nothing is known about your tasks.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 420)
            }
        } actions: {
            VStack(spacing: 10) {
                Button("Try Again", action: retry)
                    .buttonStyle(.borderedProminent)
                Text("launchctl print gui/$(id -u)/com.carlossanabria.wtdaemon")
                    .font(.caption2.monospaced())
                    .foregroundStyle(.tertiary)
                    .textSelection(.enabled)
            }
        }
        .accessibilityLabel("Daemon unreachable. \(reason)")
    }
}

/// The daemon answered but with an error — a different state again from both
/// "unreachable" and "empty".
struct DaemonFailureView: View {
    let code: String?
    let message: String
    let retry: () -> Void

    var body: some View {
        ContentUnavailableView {
            Label("The daemon returned an error", systemImage: "exclamationmark.triangle")
                .symbolRenderingMode(.hierarchical)
        } description: {
            VStack(spacing: 6) {
                Text(message).multilineTextAlignment(.center).frame(maxWidth: 460)
                if let code {
                    Text(code)
                        .font(.caption.monospaced())
                        .foregroundStyle(.tertiary)
                        .textSelection(.enabled)
                }
            }
        } actions: {
            Button("Try Again", action: retry).buttonStyle(.borderedProminent)
        }
    }
}

/// Shown while the very first snapshot is in flight.
struct ConnectingView: View {
    var body: some View {
        VStack(spacing: 12) {
            ProgressView()
            Text("Connecting to the workload daemon…")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// Persistent banners over a *working* board: the TUI-clobber warning (plan
/// risk #1), the Full-Disk-Access state (risk #3/#9), and a degraded event
/// stream. None of these blank the board — they annotate it.
struct WarningBanners: View {
    @Environment(Store.self) private var store

    var body: some View {
        VStack(spacing: 0) {
            if let probe = store.dataFile, !probe.readable {
                Banner(
                    symbol: "lock.trianglebadge.exclamationmark",
                    tint: .red,
                    title: "The data file is not readable (\(probe.reason ?? "unknown"))",
                    detail: probe.advice ?? probe.path ?? ""
                )
            }
            if store.tuiIsRunning {
                Banner(
                    symbol: "exclamationmark.triangle",
                    tint: .orange,
                    title: "tracker.py is running",
                    detail: "The TUI holds the whole dataset in memory and saves "
                    + "wholesale, so its next save may overwrite changes made here."
                )
            }
            if case .degraded(let reason) = store.connection {
                Banner(
                    symbol: "antenna.radiowaves.left.and.right.slash",
                    tint: .yellow,
                    title: "Live updates paused",
                    detail: reason
                )
            }
        }
    }
}

private struct Banner: View {
    let symbol: String
    let tint: Color
    let title: String
    let detail: String

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: symbol)
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(tint)
            VStack(alignment: .leading, spacing: 1) {
                Text(title).font(.caption.weight(.semibold))
                if !detail.isEmpty {
                    Text(detail).font(.caption2).foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(tint.opacity(0.12))
        .overlay(alignment: .bottom) { Divider() }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(title). \(detail)")
    }
}

/// An honest placeholder for the views that arrive in later phases, naming the
/// phase rather than pretending to be under construction.
struct PhasePlaceholderView: View {
    let title: String
    let symbol: String
    let summary: String
    let phase: String

    var body: some View {
        ContentUnavailableView {
            Label(title, systemImage: symbol).symbolRenderingMode(.hierarchical)
        } description: {
            VStack(spacing: 8) {
                Text(summary).multilineTextAlignment(.center).frame(maxWidth: 460)
                Text(phase).font(.caption).foregroundStyle(.tertiary)
            }
        }
    }
}
