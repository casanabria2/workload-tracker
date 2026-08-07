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

    /// Whether starting a timer opens the task's dedicated Safari window.
    ///
    /// Defaults to **on**, matching what the TUI's `t` key and the Stream Deck
    /// bridge already do (`_browser_on_task_started`), so the app behaves like
    /// the thing it replaces.
    ///
    /// It is a setting at all — rather than just passing `true` — because the
    /// daemon's `POST /v1/timer/start` defaults `browser` to `true` on its own,
    /// so the flag has to be sent explicitly to ever be `false`. Without a way
    /// to turn it off, every timer start from this app opens real Safari windows
    /// on the owner's desktop, including from a test run.
    ///
    /// `registerDefaults` seeds `true`, because `UserDefaults.bool` returns
    /// `false` for an absent key and the safe-looking default is the wrong one
    /// here.
    static var opensTaskWindow: Bool {
        get {
            UserDefaults.standard.register(defaults: [opensTaskWindowKey: true])
            return UserDefaults.standard.bool(forKey: opensTaskWindowKey)
        }
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
    /// When the app is packaged as a real `.app` (plan §12 / Phase 9) the scene
    /// value starts working and takes precedence; this stays as the seed for a
    /// brand-new window. Same failure shape as the Phase 4 `UTType` bug: a
    /// mechanism that compiles, runs, and silently does nothing.
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
