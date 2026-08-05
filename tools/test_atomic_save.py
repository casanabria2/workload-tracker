#!/usr/bin/env python3
"""Verification harness for Phase 0 of docs/plan-macos-app.md §3.

There is no pytest suite in this repo, so this script *is* the test for the
hardened write path: ``wt.save()`` must be **atomic** (a concurrent reader never
sees a partial file) and ``wt.data_lock()`` must make a read-modify-write
**mutually exclusive** (no writer's mutation is lost).

It runs fully offline. ``wt.subprocess`` is replaced by the same guard the other
harnesses use, so a stray ``gh`` call fails loudly instead of reaching GitHub —
and concurrency is produced with ``os.fork()`` rather than ``subprocess``, so
nothing in this file can spawn a process at all.

Usage:

    WT_DATA_FILE=<scratch>/unused.json \\
    venv/bin/python tools/test_atomic_save.py <data.json> <scratch-dir> [writers]

  data.json     a *copy* of the data file (never the live one)
  scratch-dir   writable directory for working copies
  writers       concurrent writer processes (default 8)

Exit status 0 when every check passes. Against the pre-Phase-0 code (plain
``DATA_FILE.write_text``) section 3 fails: mutations are lost and/or a reader
observes a truncated file.
"""
import json
import os
import shutil
import subprocess as real_subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

LIVE = Path.home() / ".workload_tracker.json"

FAILURES = []
CHECKS = 0

WRITER_MINUTES = 7.0        # each writer appends exactly this much time
WRITER_STAGGER = 0.05       # held inside the lock, to force real contention


def check(ok, label, detail=""):
    global CHECKS
    CHECKS += 1
    if ok:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}  {detail}")
        FAILURES.append(f"{label}  {detail}")
    return ok


def section(title):
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- gh stubbing --

class SubprocessGuard:
    """Stand-in for wt.subprocess: any use is a bug in the test setup."""

    def __getattr__(self, name):
        def boom(*a, **k):
            raise AssertionError(
                f"wt.subprocess.{name} called with {a!r} — nothing here may shell out"
            )
        return boom


# ------------------------------------------------------------------ utilities --

def point_at(wt, src, dst):
    """Copy *src* to *dst* and point wt at it.

    wt resolves DATA_FILE once at import time, so the module constant has to be
    rebound (not just the env var). Refuses anything that isn't a fresh copy, so
    no test can reach the live data file.
    """
    dst = Path(dst)
    assert dst.name.endswith(".json"), dst
    assert dst.resolve() != LIVE.resolve() if LIVE.exists() else True
    shutil.copyfile(src, dst)
    os.environ["WT_DATA_FILE"] = str(dst)
    wt.DATA_FILE = dst
    lock = getattr(wt, "_resolve_lock_file", None)
    if lock is not None:
        with_suppress_unlink(lock(dst))
    return dst


def with_suppress_unlink(path):
    try:
        Path(path).unlink()
    except OSError:
        pass


def noop_lock(*a, **k):
    """Stand-in for wt.data_lock on pre-Phase-0 code, so the 'before' run works."""
    import contextlib

    @contextlib.contextmanager
    def _cm():
        yield False
    return _cm()


def get_lock(wt):
    return getattr(wt, "data_lock", None) or noop_lock


def total_minutes(data):
    return round(sum(l.get("minutes", 0) for t in data.get("tasks", [])
                     for l in t.get("logs", [])), 6)


def log_ids(data):
    return {l.get("id") for t in data.get("tasks", []) for l in t.get("logs", [])}


def stray_tmp(path: Path):
    real = Path(path).resolve()
    return sorted(p.name for p in real.parent.glob(real.name + ".*")
                  if p.name.endswith(".tmp"))


# ------------------------------------------------------------------- 1. basics --

def test_write_mechanics(wt, src, scratch):
    section("1. atomic write mechanics")
    work = point_at(wt, src, scratch / "mech.json")
    data = wt.load()
    before_mode = os.stat(work).st_mode & 0o7777

    # Byte-for-byte format parity: the file is hand-diffed and iCloud-synced, so
    # a reformat would show up as a spurious whole-file change.
    wt.save(data)
    check(work.read_text() == json.dumps(data, indent=2),
          "payload is exactly json.dumps(data, indent=2)")
    check(json.loads(work.read_text())["tasks"], "file is valid JSON with tasks")
    check(os.stat(work).st_mode & 0o7777 == before_mode,
          "file mode preserved across the replace",
          f"{oct(before_mode)} -> {oct(os.stat(work).st_mode & 0o7777)}")
    check(stray_tmp(work) == [], "no temp file left behind", str(stray_tmp(work)))

    # A save must not clobber the target's inode wholesale when it is a symlink:
    # the live ~/.workload_tracker.json is a symlink chain into iCloud Drive and
    # os.replace() does not follow symlinks, so replacing the link path directly
    # would detach the data from iCloud sync on both Macs.
    realdir = scratch / "linked"
    realdir.mkdir(exist_ok=True)
    real = realdir / "real.json"
    shutil.copyfile(src, real)
    link = scratch / "link.json"
    with_suppress_unlink(link)
    link.symlink_to(real)
    saved_target = wt.DATA_FILE
    try:
        wt.DATA_FILE = link
        os.environ["WT_DATA_FILE"] = str(link)
        d = wt.load()
        d["config"]["_atomic_probe"] = "sentinel"
        wt.save(d)
        check(link.is_symlink(), "a symlinked data file is still a symlink after save")
        check(json.loads(real.read_text())["config"].get("_atomic_probe") == "sentinel",
              "and the write landed in the real file behind the link")
        check(stray_tmp(link) == [] and stray_tmp(real) == [],
              "no temp file left beside either path")
    finally:
        wt.DATA_FILE = saved_target
        os.environ["WT_DATA_FILE"] = str(saved_target)


# ------------------------------------------------------- 2. lock semantics ----

def test_lock_semantics(wt, src, scratch):
    section("2. data_lock() semantics")
    work = point_at(wt, src, scratch / "lock.json")
    data_lock = get_lock(wt)
    if data_lock is noop_lock:
        check(False, "wt.data_lock exists", "missing on this revision")
        return

    lock_path = wt._resolve_lock_file(work)
    check(str(lock_path).startswith(str(scratch)),
          "lock is a sidecar of the WT_DATA_FILE copy, not the live lock",
          str(lock_path))
    check("Mobile Documents" not in str(wt._resolve_lock_file(LIVE)),
          "the live lock is outside ~/Library/Mobile Documents",
          str(wt._resolve_lock_file(LIVE)))
    check(wt._resolve_lock_file(LIVE) == Path.home() / ".workload_tracker.lock",
          "the live lock is ~/.workload_tracker.lock",
          str(wt._resolve_lock_file(LIVE)))

    # Re-entrancy: this is the contract Phase 2 depends on — hold the lock across
    # a transaction and call save() inside it.
    ready = scratch / "reentry.ready"
    with_suppress_unlink(ready)
    deadlocked = True
    try:
        with data_lock(work):
            d = wt.load()
            wt.save(d)          # must re-enter, not hang
            with data_lock(work):
                wt.save(d)      # and nest arbitrarily
            # ...and the flock must still be held out here, despite those exits.
            still_held = child_lock_is_busy(wt, work)
        deadlocked = False
    except Exception as exc:            # noqa: BLE001 - reported, not raised
        check(False, "save() inside data_lock() does not deadlock or raise", repr(exc))
        return
    check(not deadlocked, "save() inside data_lock() does not deadlock")
    check(still_held,
          "a nested save()/lock exit does NOT release the outer lock early")
    check(not child_lock_is_busy(wt, work),
          "and the lock is released once the outermost block exits")

    # A timeout must be bounded (the TUI's 1 s tick can reach save()).
    check(isinstance(wt.DATA_LOCK_TIMEOUT_SECONDS, (int, float))
          and 0 < wt.DATA_LOCK_TIMEOUT_SECONDS <= 30,
          "DATA_LOCK_TIMEOUT_SECONDS is bounded", str(wt.DATA_LOCK_TIMEOUT_SECONDS))

    held = scratch / "held.flag"
    with_suppress_unlink(held)
    pid = os.fork()
    if pid == 0:
        try:
            with data_lock(work):
                held.write_text("1")
                time.sleep(1.5)
        finally:
            os._exit(0)
    for _ in range(200):
        if held.exists():
            break
        time.sleep(0.01)
    t0 = time.monotonic()
    raised = None
    try:
        with data_lock(work, timeout=0.2, required=True):
            pass
    except Exception as exc:            # noqa: BLE001
        raised = exc
    waited = time.monotonic() - t0
    check(isinstance(raised, wt.DataLockTimeout),
          "required=True raises DataLockTimeout when another process holds it",
          repr(raised))
    check(waited < 1.0, "and gives up inside the timeout rather than blocking",
          f"{waited:.2f}s")

    t0 = time.monotonic()
    with data_lock(work, timeout=0.2, required=False) as got:
        pass
    check(got is False and time.monotonic() - t0 < 1.0,
          "required=False degrades to unlocked instead of hanging the caller",
          f"got={got}")
    os.waitpid(pid, 0)
    check(not child_lock_is_busy(wt, work), "lock released when the holder exits")


def child_lock_is_busy(wt, work) -> bool:
    """True when a *separate process* cannot take the lock right now.

    Forks, because flock is per-open-file: an in-process check would see the
    parent's own fd and prove nothing.
    """
    pid = os.fork()
    if pid == 0:
        rc = 0
        try:
            # The fork inherited the parent's re-entrancy depth (and its held
            # RLock); a nested acquisition would be a no-op and prove nothing.
            # Reset both so the child really attempts the flock — the inherited
            # fd keeps holding it, which is exactly what we want to detect.
            import threading
            wt._DATA_LOCK_STATE = {"depth": 0, "fh": None}
            wt._DATA_LOCK_MUTEX = threading.RLock()
            with wt.data_lock(work, timeout=0.05, required=True):
                rc = 0
        except Exception:                # noqa: BLE001
            rc = 3
        os._exit(rc)
    return os.waitpid(pid, 0)[1] >> 8 == 3


# ------------------------------------------------- 3. concurrent writers ------

def writer_child(wt, work, idx, task_id):
    """One competing writer: lock, load, append a distinct log, save."""
    data_lock = get_lock(wt)
    try:
        with data_lock(work):
            data = wt.load()
            task = next(t for t in data["tasks"] if t["id"] == task_id)
            # Sleep *inside* the transaction: a correct lock still serialises,
            # while an absent one reliably loses updates.
            time.sleep(WRITER_STAGGER)
            task.setdefault("logs", []).append({
                "id": f"concurrent{idx:04d}",
                "minutes": WRITER_MINUTES,
                "note": f"atomic-save harness writer {idx}",
                "at": time.time(),
            })
            wt.save(data)
    except Exception as exc:             # noqa: BLE001
        print(f"    writer {idx} raised: {exc!r}")
        os._exit(4)
    os._exit(0)


def reader_child(wt, work, stop_flag, result_path):
    """Loop-read the file; record anything that is not a whole document."""
    tears = 0
    empties = 0
    reads = 0
    sizes = set()
    while not stop_flag.exists():
        reads += 1
        try:
            raw = Path(work).read_text()
        except OSError:
            tears += 1
            continue
        sizes.add(len(raw))
        try:
            parsed = json.loads(raw)
        except Exception:                # noqa: BLE001
            tears += 1
            continue
        if not parsed.get("tasks"):
            # wt.load() masks a parse error as {} -> zero tasks, so an empty
            # task list from a non-empty fixture is a tear the loader swallowed.
            empties += 1
    Path(result_path).write_text(json.dumps(
        {"reads": reads, "tears": tears, "empties": empties,
         "distinct_sizes": len(sizes)}))
    os._exit(0)


def test_concurrent_writers(wt, src, scratch, n_writers):
    section(f"3. {n_writers} concurrent writers + a looping reader")
    work = point_at(wt, src, scratch / "concurrent.json")
    data = wt.load()
    wt.save(data)                       # normalise before measuring
    before = json.loads(work.read_text())
    mins_before = total_minutes(before)
    ids_before = log_ids(before)
    tasks = [t["id"] for t in before["tasks"]][:n_writers]
    check(len(tasks) == n_writers, "fixture has enough tasks for one each",
          f"{len(tasks)} tasks")

    stop = scratch / "readers.stop"
    with_suppress_unlink(stop)
    reader_results = []
    reader_pids = []
    for r in range(2):
        rp = scratch / f"reader{r}.json"
        with_suppress_unlink(rp)
        reader_results.append(rp)
        pid = os.fork()
        if pid == 0:
            reader_child(wt, work, stop, rp)
        reader_pids.append(pid)

    t0 = time.monotonic()
    writer_pids = []
    for idx, task_id in enumerate(tasks):
        pid = os.fork()
        if pid == 0:
            writer_child(wt, work, idx, task_id)
        writer_pids.append(pid)
    bad = [p for p in writer_pids if os.waitpid(p, 0)[1] != 0]
    elapsed = time.monotonic() - t0
    stop.write_text("1")
    for pid in reader_pids:
        os.waitpid(pid, 0)

    check(not bad, "every writer exited cleanly", f"{len(bad)} failed")
    print(f"    {n_writers} writers finished in {elapsed:.2f}s "
          f"(each holds the lock ~{WRITER_STAGGER}s)")

    after_raw = work.read_text()
    parsed = None
    try:
        parsed = json.loads(after_raw)
    except Exception as exc:             # noqa: BLE001
        check(False, "the data file is still valid JSON", repr(exc))
        return work
    check(True, "the data file is still valid JSON")

    ids_after = log_ids(parsed)
    expected = {f"concurrent{i:04d}" for i in range(n_writers)}
    survived = expected & ids_after
    check(survived == expected,
          f"all {n_writers} writers' mutations survived",
          f"{len(survived)}/{n_writers} present, lost="
          f"{sorted(expected - survived)}")
    check(ids_before <= ids_after, "no pre-existing log was dropped",
          f"lost={sorted(ids_before - ids_after)[:4]}")
    want = round(mins_before + n_writers * WRITER_MINUTES, 6)
    check(abs(total_minutes(parsed) - want) < 1e-6,
          "total minutes == before + every writer's contribution",
          f"{total_minutes(parsed)} vs {want}")
    check(len(parsed["tasks"]) == len(before["tasks"]), "task count unchanged")
    check(stray_tmp(work) == [], "no temp files left behind", str(stray_tmp(work)))

    total_reads = 0
    total_tears = 0
    total_empty = 0
    for rp in reader_results:
        res = json.loads(rp.read_text())
        total_reads += res["reads"]
        total_tears += res["tears"]
        total_empty += res["empties"]
        print(f"    reader: {res['reads']} reads, {res['tears']} unparseable, "
              f"{res['empties']} empty, {res['distinct_sizes']} distinct sizes")
    check(total_reads > 50, "the readers really did spin during the writes",
          f"{total_reads} reads")
    check(total_tears == 0,
          "a concurrent reader never observed a torn/partial file",
          f"{total_tears} of {total_reads} reads were unparseable")
    check(total_empty == 0,
          "and never observed an empty document (which wt.load() would mask)",
          f"{total_empty} of {total_reads} reads had no tasks")
    return work


# -------------------------------------------- 4. delegation of write paths ----

def test_delegation(wt, src, scratch):
    section("4. tracker.py and mcp_server.py delegate to wt.save()")
    import inspect

    tracker_src = (REPO / "tracker.py").read_text()
    mcp_src = (REPO / "mcp_server.py").read_text()
    check("DATA_FILE.write_text" not in tracker_src,
          "tracker.py has no raw DATA_FILE.write_text left")
    check("DATA_FILE.write_text" not in mcp_src,
          "mcp_server.py has no raw DATA_FILE.write_text left")
    check("DATA_FILE.write_text" not in (REPO / "wt.py").read_text(),
          "wt.py has no raw DATA_FILE.write_text left")

    # mcp_server imports mcp/fastmcp; skip gracefully if the venv lacks it.
    try:
        import mcp_server            # noqa: F401
    except Exception as exc:         # noqa: BLE001
        print(f"    (mcp_server not importable here: {exc!r})")
    else:
        work = point_at(wt, src, scratch / "mcp.json")
        mcp_server.DATA_FILE = work
        body = inspect.getsource(mcp_server.save)
        check("wt_save" in body, "mcp_server.save delegates to wt.save", body.strip())
        d = mcp_server.load()
        d["config"]["_mcp_probe"] = "x"
        mcp_server.save(d)
        check(json.loads(work.read_text())["config"].get("_mcp_probe") == "x",
              "mcp_server.save() still writes its own DATA_FILE")
        check(stray_tmp(work) == [], "atomically", str(stray_tmp(work)))
        check(len(inspect.signature(mcp_server.save).parameters) == 1,
              "and keeps its one-argument signature")


# --------------------------------------------------------------------- main ----

def main():
    if len(sys.argv) not in (3, 4):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    src = Path(sys.argv[1]).expanduser()
    scratch = Path(sys.argv[2]).expanduser()
    n_writers = int(sys.argv[3]) if len(sys.argv) == 4 else 8
    scratch.mkdir(parents=True, exist_ok=True)

    if src.resolve() == LIVE.resolve():
        print("REFUSING TO RUN: <data.json> is the live data file", file=sys.stderr)
        return 2

    os.environ.setdefault("WT_DATA_FILE", str(scratch / "unused.json"))
    import wt

    if wt._resolve_data_file() == LIVE or wt.DATA_FILE == LIVE:
        print("REFUSING TO RUN: WT_DATA_FILE resolves to the live data file",
              file=sys.stderr)
        return 2
    wt.subprocess = SubprocessGuard()

    if not hasattr(wt, "data_lock"):
        print("NOTE: wt.data_lock() is absent — this is the pre-Phase-0 code; "
              "writers run unlocked.")

    test_write_mechanics(wt, src, scratch)
    test_lock_semantics(wt, src, scratch)
    work = test_concurrent_writers(wt, src, scratch, n_writers)
    test_delegation(wt, src, scratch)

    section("5. tools/check_invariants.py on the post-concurrency file")
    proc = real_subprocess.run(
        [sys.executable, str(REPO / "tools" / "check_invariants.py"), str(work)],
        capture_output=True, text=True)
    print("\n".join("    " + l for l in proc.stdout.strip().splitlines()))
    if proc.stderr.strip():
        print("    stderr:", proc.stderr.strip()[:500])
    check(proc.returncode == 0, "check_invariants exits 0", f"rc={proc.returncode}")

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  x {f}")
        return 1
    print("All atomic-save checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
