import Foundation

/// Thin wrapper over `UserDefaults` for the small amount of persisted config.
///
/// Kept as a namespace of static accessors so any layer (client, process
/// manager, UI) can read the current settings without dependency wiring — the
/// same shape as `AppSettings` in workload-macos-monitor.
enum AppSettings {

    // MARK: - Keys

    private static let baseURLKey = "daemonBaseURL"
    private static let tokenPathKey = "daemonTokenPath"
    private static let repositoryPathKey = "trackerRepositoryPath"
    private static let autoStartKey = "autoStartDaemon"
    private static let opensTaskWindowKey = "opensTaskWindow"
    private static let lastFilterKey = "lastFilterState"

    // MARK: - Defaults

    /// The v1 API port. `:7373` is the TUI's in-process bridge and `:7375` the
    /// daemon's unauthenticated legacy contract for the menu-bar monitor —
    /// neither speaks `/v1`.
    static let defaultPort = 7374
    static let defaultBaseURL = "http://127.0.0.1:7374"

    static var defaultTokenFileURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appending(path: ".workload_tracker_daemon_token")
    }

    static var defaultRepositoryPath: String {
        FileManager.default.homeDirectoryForCurrentUser
            .appending(path: "dev/carlos/workload-tracker").path
    }

    // MARK: - Accessors

    /// Base URL of the daemon's authenticated v1 API.
    static var baseURLString: String {
        get { nonBlank(baseURLKey) ?? defaultBaseURL }
        set { UserDefaults.standard.set(newValue, forKey: baseURLKey) }
    }

    /// The port component of `baseURLString`, for spawning a matching child.
    static var daemonPort: Int {
        URL(string: baseURLString)?.port ?? defaultPort
    }

    /// Path to the bearer token file the daemon writes at mode 0600.
    static var tokenFileURL: URL {
        get {
            if let path = nonBlank(tokenPathKey) {
                return URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
            }
            return defaultTokenFileURL
        }
        set { UserDefaults.standard.set(newValue.path, forKey: tokenPathKey) }
    }

    /// Where `wt_daemon.py` and `venv/bin/python3` live, for the spawn path.
    static var repositoryPath: String {
        get { nonBlank(repositoryPathKey).map { ($0 as NSString).expandingTildeInPath }
            ?? defaultRepositoryPath }
        set { UserDefaults.standard.set(newValue, forKey: repositoryPathKey) }
    }

    /// Whether the app may spawn its own daemon when none is answering.
    ///
    /// Defaults to **off**: the owner runs one under launchd, and a second
    /// instance racing it on the data file is worse than a visible
    /// "unreachable" state.
    static var autoStartDaemon: Bool {
        get { UserDefaults.standard.bool(forKey: autoStartKey) }
        set { UserDefaults.standard.set(newValue, forKey: autoStartKey) }
    }

    /// Whether starting or stopping a timer opens/snapshots the task's
    /// dedicated Safari window.
    ///
    /// Defaults to **off**, matching `wt_daemon`'s own default since `0fdf2d7`:
    /// *"a v1 client starting a timer should not reach out and rearrange the
    /// user's desktop"*. The plan's §13.5 now lists the Safari integration as a
    /// removal target rather than a deprecation, so this must not be the thing
    /// that keeps it alive.
    ///
    /// The flag is still **sent explicitly** on every call rather than omitted.
    /// The daemon's default has flipped once already; a client that relies on it
    /// changes behaviour when the server does, silently. Sending it also keeps
    /// the capability reachable — the daemon still opens the window for a client
    /// that asks — without making it the default.
    static var opensTaskWindow: Bool {
        get { UserDefaults.standard.bool(forKey: opensTaskWindowKey) }
        set { UserDefaults.standard.set(newValue, forKey: opensTaskWindowKey) }
    }

    /// The last `FilterState`, JSON-encoded — the **fallback** behind
    /// `@SceneStorage("filterState")` (plan §8.1).
    ///
    /// `@SceneStorage` is the right home for a per-window filter, but it rides
    /// on AppKit state restoration, which a bare SwiftPM executable with no
    /// bundle and no bundle identifier does not get. Measured: after a filter
    /// was set and the app relaunched, `defaults read WorkloadClient` held the
    /// window frame and **no** `filterState` key, and the board came back on the
    /// default filter. So the scene value is mirrored here, where it survives.
    ///
    /// **Phase 9 update — measured, and the mirror is being kept.** With
    /// `WorkloadTracker.app` (plan §12) the scene value does now persist: the
    /// whole `com.carlossanabria.workloadtracker` defaults domain was deleted,
    /// the app relaunched, and both the sidebar selection *and* a role filter
    /// came back — so the restore did not come from here. `@SceneStorage` takes
    /// precedence in `RootView.onAppear`, and this is the fallback behind it.
    ///
    /// It is not merely belt and braces. AppKit state restoration is switched
    /// off by **System Settings → Desktop & Dock → "Close windows when quitting
    /// an application"** (`NSQuitAlwaysKeepsWindows = 0`), and discarded by
    /// `open --fresh`. In either case `@SceneStorage` silently reverts to its
    /// default and this key is the only thing that remembers the filter. Same
    /// failure shape as the Phase 4 `UTType` bug — a mechanism that compiles,
    /// runs and silently does nothing — except here the trigger is a checkbox in
    /// System Settings.
    ///
    /// `""` means "never written", which must stay distinguishable from an
    /// explicitly cleared filter — see `FilterStateCodec`.
    static var lastFilterState: String {
        get { UserDefaults.standard.string(forKey: lastFilterKey) ?? "" }
        set { UserDefaults.standard.set(newValue, forKey: lastFilterKey) }
    }

    private static func nonBlank(_ key: String) -> String? {
        let stored = UserDefaults.standard.string(forKey: key)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard let stored, !stored.isEmpty else { return nil }
        return stored
    }
}
