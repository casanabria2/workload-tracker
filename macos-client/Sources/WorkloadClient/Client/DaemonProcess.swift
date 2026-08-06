import Foundation

/// Attach-first lifecycle management for `wt_daemon.py` (plan §5.5).
///
/// **The rule this type exists to enforce: never terminate a daemon we did not
/// start.** On the owner's Macs the daemon runs under launchd
/// (`com.carlossanabria.wtdaemon`) and the menu-bar monitor depends on it, so
/// quitting this app must leave that process alone. `spawnedProcess` is `nil`
/// in the attached case and `terminate()` is therefore a no-op.
actor DaemonProcess {

    /// How the daemon is being reached.
    enum Mode: Sendable, Equatable {
        /// A daemon was already answering `/v1/health`; we attached to it.
        case attached
        /// We started one, and we own it.
        case spawned(pid: Int32)
        /// Nothing is answering and we did not (or could not) start one.
        case absent(reason: String)

        var isRunning: Bool {
            switch self {
            case .attached, .spawned: true
            case .absent: false
            }
        }
    }

    private(set) var mode: Mode = .absent(reason: "not started")
    /// Non-nil only for a daemon this process launched.
    private var spawnedProcess: Process?

    /// Probes health; spawns a child only if nothing answers **and**
    /// `allowSpawn` is set (Settings → auto-start).
    ///
    /// Returns the resulting mode. A failure to spawn is reported as
    /// `.absent`, never thrown: the UI's job is then to render the
    /// "daemon unreachable" state, which it must do correctly regardless.
    @discardableResult
    func ensureRunning(client: DaemonClient, allowSpawn: Bool) async -> Mode {
        if await client.isReachable() {
            // Attached. If we previously spawned one and something else now
            // answers, keep ours recorded — terminate() still only kills ours.
            if spawnedProcess?.isRunning != true {
                spawnedProcess = nil
                mode = .attached
            }
            return mode
        }

        guard allowSpawn else {
            mode = .absent(reason: "no daemon is answering and auto-start is off")
            return mode
        }
        return await spawn(client: client)
    }

    private func spawn(client: DaemonClient) async -> Mode {
        let repo = URL(fileURLWithPath: AppSettings.repositoryPath, isDirectory: true)
        let interpreter = repo.appending(path: "venv/bin/python3")
        let script = repo.appending(path: "wt_daemon.py")

        let fm = FileManager.default
        guard fm.isExecutableFile(atPath: interpreter.path) else {
            mode = .absent(reason: "no venv interpreter at \(interpreter.path)")
            return mode
        }
        guard fm.fileExists(atPath: script.path) else {
            mode = .absent(reason: "no wt_daemon.py at \(script.path)")
            return mode
        }

        let process = Process()
        process.executableURL = interpreter
        process.arguments = [script.path, "--port", String(AppSettings.daemonPort)]
        process.currentDirectoryURL = repo
        // launchd hands a minimal PATH and the daemon shells out to `gh`; the
        // LaunchAgent plist works around the same thing (launchd/README.md).
        var environment = ProcessInfo.processInfo.environment
        let path = environment["PATH"] ?? "/usr/bin:/bin"
        if !path.contains("/opt/homebrew/bin") {
            environment["PATH"] = "/opt/homebrew/bin:" + path
        }
        process.environment = environment

        do {
            try process.run()
        } catch {
            mode = .absent(reason: "could not launch the daemon: \(error.localizedDescription)")
            return mode
        }
        spawnedProcess = process

        // The daemon binds and writes its token before serving; poll health
        // rather than sleeping a fixed interval.
        for _ in 0..<40 {
            if await client.isReachable() {
                mode = .spawned(pid: process.processIdentifier)
                return mode
            }
            if !process.isRunning { break }
            try? await _Concurrency.Task.sleep(for: .milliseconds(250))
        }

        // It never came up. Clean up the child we made — this is ours to kill.
        if process.isRunning { process.terminate() }
        spawnedProcess = nil
        mode = .absent(reason: "the daemon did not answer /v1/health within 10s")
        return mode
    }

    /// Terminates the daemon **only if this process started it**. Called on app
    /// quit. A launchd- or terminal-started daemon is deliberately untouched.
    func terminateIfSpawned() {
        guard let process = spawnedProcess, process.isRunning else {
            spawnedProcess = nil
            return
        }
        process.terminate()
        spawnedProcess = nil
        mode = .absent(reason: "terminated on quit")
    }
}
