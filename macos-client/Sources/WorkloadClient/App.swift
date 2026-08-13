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
    /// The sidebar selection, hoisted out of `RootView` so the View menu's
    /// `⌘1`/`⌘2`/`⌘3` can drive the same value the sidebar's `List` binds.
    @State private var selection: SidebarSelection? = .board

    var body: some Scene {
        WindowGroup {
            RootView(selection: $selection)
                .environment(store)
                .onAppear { delegate.store = store }
        }
        // Wider than Phase 3's 1240: the trailing inspector is a third column,
        // and a default size that cannot show all three makes it invisible
        // rather than merely cramped. 1440 leaves the board its three columns
        // *and* the inspector without the user resizing anything.
        .defaultSize(width: 1440, height: 860)
        .windowResizability(.contentMinSize)
        .commands {
            // Every shortcut below comes from `AppShortcut`, the one table the
            // collision check reads. See Design/AppShortcuts.swift.
            CommandGroup(after: .toolbar) {
                ViewCommands(selection: $selection).environment(store)
            }

            // `⌘F`. Placed *after* the standard text-editing group rather than
            // replacing it, so AppKit keeps Undo/Redo and Cut/Copy/Paste.
            CommandGroup(after: .textEditing) {
                FindCommands().environment(store)
            }

            // Plan §9: "Row actions via context menu **and the Task menu**."
            // Phase 8 widened it from the recurrent shelf to whichever card or
            // row was selected last.
            CommandMenu("Task") {
                TaskMenuCommands().environment(store)
            }

            CommandGroup(replacing: .help) {
                Button("Keyboard Shortcuts") { store.showsShortcutHelp = true }
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
