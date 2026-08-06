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

    private static func nonBlank(_ key: String) -> String? {
        let stored = UserDefaults.standard.string(forKey: key)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard let stored, !stored.isEmpty else { return nil }
        return stored
    }
}
