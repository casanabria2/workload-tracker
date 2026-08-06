import SwiftUI

/// The `Settings` scene (⌘,). A stub in Phase 3: enough to point the client at
/// a different daemon (which is how the "unreachable" state gets exercised) and
/// to show what the client currently believes about the daemon.
struct SettingsView: View {
    @Environment(Store.self) private var store

    @State private var baseURL: String = AppSettings.baseURLString
    @State private var tokenPath: String = AppSettings.tokenFileURL.path
    @State private var repositoryPath: String = AppSettings.repositoryPath
    @State private var autoStart: Bool = AppSettings.autoStartDaemon

    var body: some View {
        TabView {
            general.tabItem { Label("General", systemImage: "gearshape") }
            advanced.tabItem { Label("Advanced", systemImage: "wrench.and.screwdriver") }
        }
        .frame(width: 520)
        .padding(20)
    }

    private var general: some View {
        Form {
            Section("Daemon") {
                TextField("Base URL", text: $baseURL)
                    .onSubmit(apply)
                Toggle("Start a daemon if none is running", isOn: $autoStart)
                    .onChange(of: autoStart) { _, new in
                        AppSettings.autoStartDaemon = new
                    }
                Text("Off by default. A daemon usually runs under launchd "
                     + "(com.carlossanabria.wtdaemon) and the menu-bar monitor "
                     + "depends on it — this app never terminates a daemon it "
                     + "did not start.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Status") {
                LabeledContent("Connection", value: connectionText)
                LabeledContent("Daemon version", value: store.health?.version ?? "—")
                LabeledContent("Data file",
                               value: store.dataFile?.path ?? "—")
                LabeledContent("Data file readable",
                               value: store.dataFile.map { $0.readable ? "yes" : "no (\($0.reason ?? "?"))" } ?? "—")
                LabeledContent("tracker.py on :7373",
                               value: store.tuiIsRunning ? "running" : "not running")
            }
            Section {
                Button("Apply and Reconnect", action: apply)
                    .buttonStyle(.borderedProminent)
            }
        }
        .formStyle(.grouped)
    }

    private var advanced: some View {
        Form {
            Section("Paths") {
                TextField("Token file", text: $tokenPath).onSubmit(apply)
                TextField("Tracker repository", text: $repositoryPath)
                    .onSubmit(apply)
                Text("The repository path is only used to spawn a daemon; it must "
                     + "contain venv/bin/python3 and wt_daemon.py.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section {
                Button("Reset to Defaults") {
                    baseURL = AppSettings.defaultBaseURL
                    tokenPath = AppSettings.defaultTokenFileURL.path
                    repositoryPath = AppSettings.defaultRepositoryPath
                    apply()
                }
            }
        }
        .formStyle(.grouped)
    }

    private var connectionText: String {
        switch store.connection {
        case .connecting: "connecting"
        case .live: "live"
        case .degraded(let reason): "degraded — \(reason)"
        case .unreachable(let reason): "unreachable — \(reason)"
        case .failed(let code, let message): "error \(code ?? "") — \(message)"
        }
    }

    private func apply() {
        AppSettings.baseURLString = baseURL
        AppSettings.tokenFileURL = URL(fileURLWithPath: (tokenPath as NSString).expandingTildeInPath)
        AppSettings.repositoryPath = repositoryPath
        store.reconfigureAndReconnect()
    }
}
