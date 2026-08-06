// swift-tools-version:6.2
//
// 6.2 rather than 6.0 because `.macOS(.v26)` was only introduced in
// PackageDescription 6.2 — an earlier tools version rejects the manifest.
import PackageDescription

// Phase 3 of docs/plan-macos-app.md. SwiftPM executable, no .xcodeproj, zero
// third-party dependencies — the same conventions as ~/dev/carlos/workload-macos-monitor.
//
// Deployment target is macOS 26 unconditionally: both of the owner's Macs run
// 26.x, so current APIs are adopted directly with no `#available` gating.
let package = Package(
    name: "WorkloadClient",
    platforms: [
        .macOS(.v26)
    ],
    targets: [
        .executableTarget(
            name: "WorkloadClient",
            path: "Sources/WorkloadClient"
        ),
        .testTarget(
            name: "WorkloadClientTests",
            dependencies: ["WorkloadClient"],
            path: "Tests/WorkloadClientTests",
            resources: [.copy("Fixtures")]
        )
    ]
)
