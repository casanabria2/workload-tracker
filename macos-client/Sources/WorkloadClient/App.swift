import SwiftUI
import AppKit

/// The app scene.
///
/// Phase 3 of docs/plan-macos-app.md: a SwiftPM executable (no `.xcodeproj`, no
/// third-party dependencies) that attaches to `wt_daemon.py`, subscribes to its
/// SSE stream, and renders the snapshot **read-only**. Every write path — drag
/// to a column, the close sheet, the timer — is deliberately absent until Phase
/// 4, because every one of them touches the owner's real work history.
@main
struct WorkloadClientApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @State private var store = Store()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(store)
                .onAppear { delegate.store = store }
        }
        .defaultSize(width: 1240, height: 800)
        .windowResizability(.contentMinSize)
        .commands {
            CommandGroup(after: .toolbar) {
                Button("Refresh Snapshot") {
                    _Concurrency.Task { await store.refresh() }
                }
                .keyboardShortcut("r", modifiers: .command)

                // Plan §8.4. Owned by the menu bar rather than by the funnel
                // menu's own row, so the shortcut is registered exactly once.
                Button("Clear All Filters") { store.clearFilters() }
                    .keyboardShortcut("k", modifiers: [.shift, .command])
                    .disabled(!store.isFiltering)
            }
        }

        Settings {
            SettingsView().environment(store)
        }
    }
}

/// Bootstraps the AppKit side of a SwiftPM executable.
///
/// A bare executable has no bundle and therefore no `Info.plist`, so the
/// activation policy and window focus have to be set explicitly — otherwise
/// `swift run` puts up a window that never comes to the front. A packaged
/// `.app` (Phase 9) gets this from its plist instead, and this stays harmless.
final class AppDelegate: NSObject, NSApplicationDelegate {
    /// Set by the scene so termination can shut down a daemon *we* spawned.
    @MainActor var store: Store?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let store else { return .terminateNow }
        _Concurrency.Task { @MainActor in
            // Only ever terminates a daemon this process started; a launchd one
            // is left running for the menu-bar monitor.
            await store.shutDown()
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }
}
