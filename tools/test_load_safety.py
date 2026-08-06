#!/usr/bin/env python3
"""Verification harness: ``wt.load()`` must never destroy an unreadable file.

``wt.load()`` is not a read — it is a read-modify-*write*. It runs four
migrations and calls ``save()`` when any of them mutates. Combined with its
``except Exception: data = {}`` fallback, that means a data file it cannot
*parse* or cannot *read* is silently replaced by an empty document.

Phase 0 made the worst case worse. The old write path was
``DATA_FILE.write_text()``, which needs write permission on the **file**, so a
mode-000 file raised ``PermissionError`` and survived. The atomic path is
``os.replace()`` onto the resolved target, which needs write permission on the
**directory** — so it succeeds, and 210 KB of history becomes a 520-byte stub
with the original mode still on it, looking untouched.

The trigger is not hypothetical: the documented second-Mac failure is exactly
"the data file is there but this process cannot read it" (Full Disk Access /
TCC), and it fires on a plain read — no write command needed. Anything that
polls (the menu-bar monitor hitting ``/status``) would trip it unattended.

Against the pre-fix code, sections 2 and 3 fail.

Usage:

    WT_DATA_FILE=<scratch>/unused.json \\
    venv/bin/python tools/test_load_safety.py <data.json> <scratch-dir>

  data.json     a *copy* of the data file (never the live one)
  scratch-dir   writable directory for working copies

Exit status 0 when every check passes.
"""
import json
import os
import shutil
import stat
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

FAILURES = []
CHECKS = 0
LIVE = Path.home() / ".workload_tracker.json"


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


class SubprocessGuard:
    """Raise on any attribute access, so a stray `gh` call fails loudly."""

    def __getattr__(self, item):
        raise AssertionError(f"subprocess.{item} used — a GitHub call escaped")


def fresh(scratch: Path, src: Path, name: str) -> Path:
    dst = scratch / name
    shutil.copyfile(src, dst)
    os.chmod(dst, 0o600)
    return dst


def size_of(p: Path) -> int:
    return p.stat().st_size


def restore_mode(p: Path):
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    src, scratch = (Path(a).expanduser() for a in sys.argv[1:3])
    scratch.mkdir(parents=True, exist_ok=True)

    if src.resolve() == LIVE.resolve():
        print("REFUSING TO RUN: that is the live data file")
        return 2

    import wt
    wt.subprocess = SubprocessGuard()  # type: ignore[assignment]

    original = src.read_bytes()
    original_tasks = len(json.loads(original).get("tasks", []))
    print(f"fixture: {src}  ({len(original)} bytes, {original_tasks} tasks)")
    if original_tasks == 0:
        print("fixture has no tasks — cannot prove non-destruction")
        return 2

    # ── 1. the healthy path is untouched ────────────────────────────────────
    section("1. a healthy file still loads, and is not rewritten needlessly")
    good = fresh(scratch, src, "good.json")
    wt.DATA_FILE = good
    before = good.read_bytes()
    data = wt.load()
    check(len(data.get("tasks", [])) == original_tasks,
          f"load() returns all {original_tasks} tasks", str(len(data.get("tasks", []))))
    check(good.read_bytes() == before,
          "an already-migrated file is left byte-identical", f"{len(before)} -> {size_of(good)}")

    # ── 2. unreadable file: the catastrophic case ───────────────────────────
    section("2. an unreadable file is never overwritten (the Phase 0 regression)")
    unreadable = fresh(scratch, src, "unreadable.json")
    wt.DATA_FILE = unreadable
    before_size = size_of(unreadable)
    os.chmod(unreadable, 0o000)
    raised = None
    try:
        got = wt.load()
    except Exception as exc:                       # noqa: BLE001 - that's the point
        raised = exc
        got = None
    restore_mode(unreadable)
    after_size = size_of(unreadable)

    check(after_size == before_size,
          "the file still holds every byte after a failed load",
          f"{before_size} -> {after_size}")
    check(json.loads(unreadable.read_text()).get("tasks"),
          "and its tasks are still there")
    check(raised is not None,
          "load() fails loudly instead of returning an empty dataset",
          f"returned {len(got.get('tasks', [])) if got is not None else 'n/a'} tasks")
    check(isinstance(raised, wt.DataFileUnreadable),
          "the failure is a typed DataFileUnreadable", type(raised).__name__)

    # ── 3. corrupt-but-readable file ────────────────────────────────────────
    section("3. a corrupt file is preserved for recovery, not stubbed out")
    corrupt = scratch / "corrupt.json"
    corrupt.write_text('{"tasks": [{"id": "half-writ')
    wt.DATA_FILE = corrupt
    before = corrupt.read_bytes()
    raised = None
    try:
        wt.load()
    except Exception as exc:                       # noqa: BLE001
        raised = exc
    check(corrupt.read_bytes() == before,
          "the corrupt bytes survive, so the file can be repaired by hand",
          f"{len(before)} -> {size_of(corrupt)}")
    check(isinstance(raised, wt.DataFileUnreadable),
          "and load() raises DataFileUnreadable", type(raised).__name__)

    # ── 4. a genuinely absent file is still a fresh install ─────────────────
    section("4. a missing file is still treated as a fresh install")
    missing = scratch / "does-not-exist.json"
    if missing.exists():
        missing.unlink()
    wt.DATA_FILE = missing
    data = wt.load()
    check(data.get("tasks") == [], "load() returns empty defaults")
    check(data.get("roles"), "with the default roles seeded")
    # load() seeding the file on a fresh install is long-standing behaviour and
    # destroys nothing — but it must only ever happen when there was no file.
    if missing.exists():
        check(json.loads(missing.read_text()).get("tasks") == [],
              "if it seeds the file, the seed is an empty task list")

    # ── 5. save() refuses to stub out a populated file ──────────────────────
    section("5. save() will not write an empty dataset over a populated file")
    target = fresh(scratch, src, "guarded.json")
    wt.DATA_FILE = target
    before = target.read_bytes()
    empty = {"tasks": [], "active_timer": None, "roles": wt.DEFAULT_ROLES.copy()}
    raised = None
    try:
        wt.save(empty)
    except Exception as exc:                       # noqa: BLE001
        raised = exc
    check(target.read_bytes() == before,
          "the populated file is untouched", f"{len(before)} -> {size_of(target)}")
    check(isinstance(raised, wt.RefusingToEmptyDataFile),
          "save() raises RefusingToEmptyDataFile", type(raised).__name__)

    # the escape hatch must still work, or a real "delete my last task" is stuck
    raised = None
    try:
        wt.save(empty, allow_empty=True)
    except Exception as exc:                       # noqa: BLE001
        raised = exc
    check(raised is None, "but allow_empty=True is honoured", str(raised))
    check(json.loads(target.read_text()).get("tasks") == [],
          "and actually writes the empty document")

    # a fresh install must still be able to create its first file
    new = scratch / "brand-new.json"
    if new.exists():
        new.unlink()
    raised = None
    try:
        wt.save(empty, path=new)
    except Exception as exc:                       # noqa: BLE001
        raised = exc
    check(raised is None and new.exists(),
          "a first save to a non-existent path is allowed", str(raised))

    # ── 6. the directory-level denial (the real TCC shape) ──────────────────
    section("6. an unwritable directory fails safely rather than half-writing")
    locked_dir = scratch / "locked"
    if locked_dir.exists():
        os.chmod(locked_dir, 0o700)
        shutil.rmtree(locked_dir)
    locked_dir.mkdir()
    victim = locked_dir / "data.json"
    shutil.copyfile(src, victim)
    before = victim.read_bytes()
    os.chmod(locked_dir, stat.S_IRUSR | stat.S_IXUSR)   # r-x: read, no create
    wt.DATA_FILE = victim
    raised = None
    try:
        d = json.loads(victim.read_text())
        d["tasks"][0]["title"] = "mutated"
        wt.save(d)
    except Exception as exc:                       # noqa: BLE001
        raised = exc
    os.chmod(locked_dir, 0o700)
    check(victim.read_bytes() == before,
          "the file is unchanged when the temp file cannot be created",
          f"{len(before)} -> {size_of(victim)}")
    check(raised is not None, "and the failure surfaces rather than passing silently",
          "no exception raised")

    # ── 7. symlink preservation still holds (Phase 0 invariant) ─────────────
    section("7. the Phase 0 symlink invariant still holds")
    real = fresh(scratch, src, "real-target.json")
    link = scratch / "link.json"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(real)
    wt.DATA_FILE = link
    d = json.loads(link.read_text())
    d.setdefault("config", {})["_probe"] = 1
    wt.save(d)
    check(link.is_symlink(), "the data file is still a symlink after save()")
    check(json.loads(real.read_text()).get("config", {}).get("_probe") == 1,
          "and the write landed on the symlink's target")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} of {CHECKS} checks FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"{CHECKS}/{CHECKS} checks passed")
    print("All load-safety checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
