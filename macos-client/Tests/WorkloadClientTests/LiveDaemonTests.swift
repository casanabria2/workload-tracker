import XCTest
@testable import WorkloadClient

/// A manual integration check against a **running** daemon.
///
/// Skipped unless `WT_LIVE_DAEMON=1`, so `swift test` stays hermetic and CI-safe.
/// Enable it to prove the models decode the *real* snapshot rather than only the
/// synthetic fixture — the failure mode a hand-written fixture cannot catch.
///
/// **Strictly read-only.** Only `GET /v1/health` and `GET /v1/snapshot` are
/// issued. Nothing here may ever POST, PATCH or DELETE: the daemon writes to the
/// owner's live, irreplaceable work history, and `gh issue create`/`close` are
/// irreversible. Point it at a scratch daemon via `WT_LIVE_DAEMON_URL` /
/// `WT_LIVE_DAEMON_TOKEN` if you need anything more.
///
///     WT_LIVE_DAEMON=1 swift test --filter LiveDaemonTests
final class LiveDaemonTests: XCTestCase {

    private func makeClient() throws -> DaemonClient {
        try XCTSkipUnless(ProcessInfo.processInfo.environment["WT_LIVE_DAEMON"] == "1",
                          "set WT_LIVE_DAEMON=1 to run against a running daemon")
        let env = ProcessInfo.processInfo.environment
        let url = URL(string: env["WT_LIVE_DAEMON_URL"] ?? AppSettings.defaultBaseURL)!
        let token = env["WT_LIVE_DAEMON_TOKEN"].map(URL.init(fileURLWithPath:))
            ?? AppSettings.defaultTokenFileURL
        return DaemonClient(configuration: .init(baseURL: url, tokenFileURL: token,
                                                 timeout: 15))
    }

    func testLiveHealth() async throws {
        let health = try await makeClient().health()
        XCTAssertTrue(health.ok)
        XCTAssertNotNil(health.version)
        XCTAssertEqual(health.dataFile?.readable, true,
                       "the daemon cannot read the data file — check Full Disk Access")
        print("[live] daemon \(health.version ?? "?") pid \(health.pid ?? -1) "
              + "port \(health.port ?? -1), tracker.py running: "
              + "\(health.tuiBridge?.running ?? false)")
    }

    /// Decodes the real snapshot and prints the board partition — the same
    /// numbers the UI renders, so a mismatch with what is on screen is visible.
    func testLiveSnapshotDecodesAndPartitions() async throws {
        let snapshot = try await makeClient().snapshot()
        XCTAssertFalse(snapshot.tasks.isEmpty, "a live snapshot with no tasks is a red flag")
        XCTAssertFalse(snapshot.roles.isEmpty)

        let store = await Store(previewSnapshot: snapshot)
        let columns = await MainActor.run {
            TaskStatus.boardColumns.map { ($0, store.boardTasks($0)) }
        }
        let recurrent = await store.recurrentTasks
        let summaries = await store.roleSummaries
        let sprint = await store.currentSprint

        print("[live] \(snapshot.tasks.count) tasks, \(snapshot.roles.count) roles, "
              + "\(snapshot.sprints.count) cached sprints, "
              + "current \(sprint?.displayName ?? "none")")
        for (status, tasks) in columns {
            let mins = tasks.reduce(0) { $0 + $1.reportableMins }
            print("[live] column \(status.displayName): \(tasks.count) cards, "
                  + Duration.formatZeroed(minutes: mins))
        }
        print("[live] recurrent shelf: \(recurrent.count) rows")
        for summary in summaries {
            print("[live] role \(summary.role.displayName): \(summary.taskCount) tasks, "
                  + Duration.format(minutes: summary.loggedMins))
        }

        // Every board card must be classifiable, and recurrent must never leak
        // into a column.
        let columnCount = columns.reduce(0) { $0 + $1.1.count }
        let unclassified = await store.unclassifiedTasks.count
        XCTAssertEqual(columnCount + recurrent.count + unclassified, snapshot.tasks.count)
        XCTAssertTrue(recurrent.allSatisfy { $0.status == .recurrent })
    }

    /// The proof the plan asks for: a dead port must surface as
    /// `.unreachable`, not as a decode failure and not as an empty snapshot.
    func testDeadPortIsUnreachableNotEmpty() async throws {
        let client = DaemonClient(configuration: .init(
            baseURL: URL(string: "http://127.0.0.1:59999")!,
            tokenFileURL: AppSettings.defaultTokenFileURL,
            timeout: 3))
        do {
            _ = try await client.snapshot()
            XCTFail("a dead port should not return a snapshot")
        } catch let error as DaemonClientError {
            XCTAssertTrue(error.isUnreachable, "got \(error) instead of .unreachable")
            XCTAssertNil(error.code)
            print("[live] dead port → \(error.errorDescription ?? "?")")
        }
    }
}
