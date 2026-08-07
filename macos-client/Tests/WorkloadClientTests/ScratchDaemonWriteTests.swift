import XCTest
@testable import WorkloadClient

/// The write paths, end to end, against a **scratch** daemon.
///
/// `LiveDaemonTests` is read-only by charter. This one is not, so it defends
/// itself rather than trusting the operator:
///
/// * it is skipped unless `WT_SCRATCH_DAEMON=1`;
/// * it refuses to run unless `/v1/health` reports a data file under `/tmp`,
///   so pointing it at the launchd daemon serving `~/.workload_tracker.json`
///   aborts instead of closing a real task;
/// * `WT_SCRATCH_DAEMON_URL` / `_TOKEN` must be given explicitly — there is no
///   default that could accidentally be the real one.
///
/// The daemon it talks to should itself have the harnesses' `gh` stubs
/// installed (`tools/test_reconcile.Stubs`) with a logging fake `gh` first on
/// `PATH`, so `gh issue create` / `gh issue close` cannot escape even if a stub
/// is missed.
///
///     mkdir -p /tmp/wt-p4 && cp ~/.workload_tracker.json /tmp/wt-p4/copy.json
///     venv/bin/python wt_daemon.py --port 17374 \
///         --token-file /tmp/wt-p4/token --data-file /tmp/wt-p4/copy.json
///     WT_SCRATCH_DAEMON=1 \
///     WT_SCRATCH_DAEMON_URL=http://127.0.0.1:17374 \
///     WT_SCRATCH_DAEMON_TOKEN=/tmp/wt-p4/token \
///         swift test --filter ScratchDaemonWriteTests
final class ScratchDaemonWriteTests: XCTestCase {

    private func makeClient() throws -> DaemonClient {
        let env = ProcessInfo.processInfo.environment
        try XCTSkipUnless(env["WT_SCRATCH_DAEMON"] == "1",
                          "set WT_SCRATCH_DAEMON=1 to run the write paths")
        let url = try XCTUnwrap(URL(string: try XCTUnwrap(
            env["WT_SCRATCH_DAEMON_URL"],
            "WT_SCRATCH_DAEMON_URL is required — there is no safe default")))
        let token = URL(fileURLWithPath: try XCTUnwrap(
            env["WT_SCRATCH_DAEMON_TOKEN"],
            "WT_SCRATCH_DAEMON_TOKEN is required"))
        return DaemonClient(configuration: .init(baseURL: url, tokenFileURL: token,
                                                 timeout: 60))
    }

    /// The guard. Everything else in this file calls it first.
    private func assertScratch(_ client: DaemonClient) async throws {
        let health = try await client.health()
        let path = try XCTUnwrap(health.dataFile?.path)
        XCTAssertTrue(path.hasPrefix("/tmp/"),
                      "REFUSING to write: the daemon is serving \(path), not a scratch copy")
        guard path.hasPrefix("/tmp/") else {
            throw XCTSkip("not a scratch daemon")
        }
        XCTAssertEqual(health.dataFile?.readable, true)
    }

    /// Picks the subject from the data at runtime rather than hardcoding a task
    /// id — the same discipline `tools/README.md` imposes on the Python
    /// harnesses, and for the same reason: the data keeps moving.
    private func pickCrossSprintTask(_ snapshot: Snapshot) throws -> TrackerTask {
        let candidates = snapshot.tasks
            .filter { $0.status == .inProgress && $0.sprintsWithTime.count >= 2 }
            .sorted { $0.sprintsWithTime.count > $1.sprintsWithTime.count }
        return try XCTUnwrap(candidates.first,
                             "no cross-sprint in-progress task in the scratch copy")
    }

    /// `close/plan` must change nothing. Asserted by running it twice and
    /// comparing the data file's mtime across both.
    func testClosePlanIsWriteFree() async throws {
        let client = try makeClient()
        try await assertScratch(client)
        let task = try pickCrossSprintTask(try await client.snapshot())

        let before = try await client.health().dataFile?.mtime
        let first = try await client.planClose(taskId: task.id)
        let second = try await client.planClose(taskId: task.id)
        let after = try await client.health().dataFile?.mtime

        XCTAssertEqual(before, after, "close/plan wrote to the data file")
        XCTAssertEqual(first, second, "the dry run is not idempotent")
        XCTAssertTrue(first.plan.dryRun)
        print("[scratch] plan for \(task.id): \(first.plan.target.count) sprints, "
              + "\(first.issuesToCreate) issue(s) would be created")
        for line in first.planLines { print("[scratch]   \(line)") }
    }

    /// The full drop → sheet → confirm → operation → snapshot round trip.
    ///
    /// Runs only with `WT_SCRATCH_DAEMON_CLOSE=1` on top of the other gates,
    /// because it really does close the task in the scratch copy.
    func testCloseEndToEnd() async throws {
        let client = try makeClient()
        try XCTSkipUnless(
            ProcessInfo.processInfo.environment["WT_SCRATCH_DAEMON_CLOSE"] == "1",
            "set WT_SCRATCH_DAEMON_CLOSE=1 to actually close a task")
        try await assertScratch(client)

        let snapshot = try await client.snapshot()
        let task = try pickCrossSprintTask(snapshot)
        let store = await Store(client: client, snapshot: snapshot)

        // 1. The drop. Opens the sheet; sends the dry run only.
        await store.perform(drop: TaskDragPayload(taskId: task.id, sourceStatus: .inProgress),
                            on: .done)
        let opened = await store.closeSheet
        let sheet = try XCTUnwrap(opened)
        guard case .ready(let plan) = sheet.phase else {
            return XCTFail("expected a plan, got \(sheet.phase)")
        }
        print("[scratch] sheet for “\(plan.title ?? "?")”: \(plan.rows.count) rows, "
              + "\(plan.issuesToCreate) issue(s) to create")
        for row in plan.rows {
            print("[scratch]   \(row.sprintTitle)  "
                  + "\(row.minutes.map { Duration.format(minutes: $0) } ?? "—")  → "
                  + "\(row.issue ?? "(no issue)")  "
                  + "\(row.actions.joined(separator: ", "))")
        }
        let statusStillOpen = await store.effectiveStatus(of: task)
        XCTAssertNotEqual(statusStillOpen, .done, "the card must not move before the confirm")

        // 2. The confirm.
        await store.confirmClose()

        // 3. Wait for the operation to reach a terminal state.
        var terminal = false
        for _ in 0..<120 {
            let phase = await store.closeSheet?.phase
            if case .succeeded(let lines, let outcome) = phase {
                terminal = true
                print("[scratch] close succeeded:")
                for line in lines { print("[scratch]   \(line)") }
                // Phase 6: "completed" is not "GitHub took it" — report both.
                print("[scratch]   outcome: issue_closed="
                      + "\(outcome?.issueClosed.description ?? "n/a") "
                      + "error=\(outcome?.error ?? "none")")
                break
            }
            if case .failed(let message, let code, _) = phase {
                return XCTFail("close failed: \(code ?? "?") \(message)")
            }
            try await _Concurrency.Task.sleep(for: .milliseconds(500))
        }
        XCTAssertTrue(terminal, "the close never reached a terminal state")

        // 4. The daemon's own snapshot agrees.
        let after = try await client.snapshot()
        let closed = try XCTUnwrap(after.tasks.first { $0.id == task.id })
        XCTAssertEqual(closed.status, .done)
        print("[scratch] \(task.id) is now \(closed.status.rawValue), "
              + "current issue \(closed.currentIssue ?? "none"), "
              + "\(closed.sprintIssues.count) bindings")
    }
}
