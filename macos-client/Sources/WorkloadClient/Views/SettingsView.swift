import SwiftUI

/// The `Settings` scene (⌘,). A stub in Phase 3: enough to point the client at
/// a different daemon (which is how the "unreachable" state gets exercised) and
/// to show what the client currently believes about the daemon.
struct SettingsView: View {
    @Environment(Store.self) private var store

    @Environment(\.colorSchemeContrast) private var contrast
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.accessibilityDifferentiateWithoutColor)
    private var differentiateWithoutColor

    @State private var baseURL: String = AppSettings.baseURLString
    @State private var tokenPath: String = AppSettings.tokenFileURL.path
    @State private var repositoryPath: String = AppSettings.repositoryPath
    @State private var autoStart: Bool = AppSettings.autoStartDaemon

    var body: some View {
        TabView {
            general.tabItem {
                Label("General", systemImage: "gearshape")
                    .symbolRenderingMode(.hierarchical)
            }
            appearance.tabItem {
                Label("Appearance", systemImage: "paintpalette")
                    .symbolRenderingMode(.hierarchical)
            }
            advanced.tabItem {
                Label("Advanced", systemImage: "wrench.and.screwdriver")
                    .symbolRenderingMode(.hierarchical)
            }
        }
        // A height as well as a width: Appearance's role-colour list and
        // General's status block both scrolled inside a ~250pt viewport at the
        // window's natural size, which reads as a truncated pane rather than a
        // scrollable one.
        .frame(width: 520, height: 560)
        .padding(20)
    }

    /// **Appearance** (plan §11's third Settings pane).
    ///
    /// Deliberately short, and deliberately *not* a theme picker. The app has no
    /// hardcoded backgrounds and no palette of its own: role colours map onto
    /// system colours (`RolePalette`), the accent follows the system accent, and
    /// light/dark, Increase Contrast and Differentiate Without Colour are all
    /// read from the environment. Offering an in-app override would mean owning
    /// a second set of colours that the OS settings could no longer correct.
    ///
    /// So this pane holds the two appearance choices that are genuinely the
    /// app's own — what the board shows on launch — and *reports* the
    /// accessibility settings in force, because "why does my board look like
    /// this" is a question the OS pane cannot answer about this window.
    private var appearance: some View {
        Form {
            Section("On launch") {
                Toggle("Show the recurrent shelf", isOn: Binding(
                    get: { store.showsRecurrentShelf },
                    set: { store.showsRecurrentShelf = $0 }))
                Toggle("Show the inspector", isOn: Binding(
                    get: { store.showsInspector },
                    set: { store.showsInspector = $0 }))
                Text("Both are restored per window from the last session; "
                     + "changing them here changes this window now.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("System settings in force") {
                LabeledContent("Increase contrast",
                               value: contrast == .increased ? "on" : "off")
                LabeledContent("Reduce motion", value: reduceMotion ? "on" : "off")
                LabeledContent("Differentiate without colour",
                               value: differentiateWithoutColor ? "on" : "off")
                Text("Read from macOS, not set here. Role colours come from the "
                     + "system palette so Dark Mode and Increase Contrast are "
                     + "handled by the OS; every colour-coded element also "
                     + "carries a text label.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Role colours") {
                ForEach(Array(store.roles.enumerated()), id: \.element.id) { index, role in
                    HStack(spacing: 8) {
                        RoleChip(label: role.displayName,
                                 color: RolePalette.color(for: role, index: index))
                        Spacer()
                        Text(RolePalette.isAssigned(role.color) ? "assigned" : role.color ?? "—")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }
                Text("Roles stored as “white” carry no colour of their own — the "
                     + "value `wt roles add` seeds — so they are assigned a stable "
                     + "distinct system colour by position rather than rendered "
                     + "as identical chips.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
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
