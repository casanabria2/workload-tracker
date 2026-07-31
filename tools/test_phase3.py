#!/usr/bin/env python3
"""Verification harness for Phase 3 of docs/plan-sprint-bindings.md (wt.py/_wt).

There is no automated test suite in this repo, so this script *is* the test for
Phase 3. It runs fully offline:

  * every ``gh``-touching function in ``wt`` is monkeypatched (the ``Stubs``
    helper is reused from ``tools/test_reconcile.py``), and ``wt.subprocess``
    itself is swapped for a guard that raises on any attribute access, so a
    missed stub fails loudly instead of reaching real GitHub;
  * ``get_all_sprints`` is stubbed to the offline ``config.sprints_cache``;
  * every run happens on a fresh copy in a scratch dir, and the script refuses
    to start if ``WT_DATA_FILE`` resolves to the live data file.

Usage:

    WT_DATA_FILE=<scratch>/unused.json \\
    venv/bin/python tools/test_phase3.py <fixture.json> <migrated.json> \\
                                         <baseline.pristine.json> <scratch-dir>

Exit status 0 when every check passes.
"""
import copy
import io
import json
import os
import re
import shutil
import subprocess as real_subprocess
import sys
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

# Reuse Phase 2's stubbing machinery verbatim so both harnesses share one
# definition of "no GitHub call can escape".
from test_reconcile import Stubs, SubprocessGuard, load_copy, find, sig  # noqa: E402

FAILURES = []
CHECKS = 0

# The four recurrent per-sprint copies a blanket reconcile would mint issues for.
RECURRENT_WOULD_MINT = [
    "Time tracking - Sprint 104",
    "Stand Up Calls - casanabria - Sprint 104",
    "Ana 1:1 calls - casanabria - Sprint 104",
    "Ad-hoc Slack Questions - Sprint 104",
]


class CliStubs(Stubs):
    """``Stubs``, but ``get_all_sprints`` always answers from the offline cache.

    Phase 2's harness called reconcile directly and passed the sprint list in, so
    strict mode could treat ``get_all_sprints`` as a forbidden call. CLI commands
    fetch the list themselves and it is a pure read, so here it stays
    stubbed-but-allowed in both modes. Everything that writes to GitHub is still
    a hard failure under ``mode="strict"``.
    """

    def __enter__(self):
        super().__enter__()
        self.wt.get_all_sprints = lambda d, _s=self.sprints: copy.deepcopy(_s)
        return self


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


ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Answers:
    """Scripted stand-in for builtins.input(). Records every prompt."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError("no scripted answer left")
        return self.answers.pop(0)


def docstring_and_comment_lines(src: str) -> set[int]:
    """1-indexed line numbers of *src* that are comment or string-literal only.

    Used by the static checks so a mention of a deleted flag in prose isn't
    mistaken for live code. Tokenising beats regexes for multi-line docstrings.
    """
    import io as _io
    import tokenize
    out: set[int] = set()
    try:
        toks = list(tokenize.generate_tokens(_io.StringIO(src).readline))
    except tokenize.TokenError:                       # pragma: no cover
        return out
    for tok in toks:
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            for ln in range(tok.start[0], tok.end[0] + 1):
                out.add(ln)
    # A line is only "prose" if it holds nothing but comment/string tokens.
    code = {tok.start[0] for tok in toks
            if tok.type not in (tokenize.COMMENT, tokenize.STRING, tokenize.NL,
                                tokenize.NEWLINE, tokenize.INDENT,
                                tokenize.DEDENT, tokenize.ENDMARKER)}
    return out - code


def run_cmd(wt, fn, argv, answers=None):
    """Call a wt.cmd_* function, capturing stdout/stderr and SystemExit.

    Returns (plain_text_output, exit_code_or_None).
    """
    import builtins
    buf = io.StringIO()
    saved_input = builtins.input
    if answers is not None:
        builtins.input = answers
    code = None
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            fn(argv)
    except SystemExit as e:
        code = e.code if e.code is not None else 0
    finally:
        builtins.input = saved_input
    return ANSI.sub("", buf.getvalue()), code


class Env:
    """A fresh migrated copy + sprint stub, ready for CLI calls."""

    def __init__(self, wt, migrated, path):
        self.wt = wt
        self.path = Path(path)
        self.data = load_copy(wt, migrated, self.path)
        self.sprints = wt.get_cached_sprints(self.data)
        wt.get_all_sprints = lambda d, _s=self.sprints: copy.deepcopy(_s)

    def reload(self):
        self.data = self.wt.load()
        return self.data

    def on_disk(self):
        return json.loads(self.path.read_text())


# ------------------------------------------------------------------ 1. smoke --

def test_cli_smoke(wt, migrated, scratch):
    section("1. every touched CLI command runs end to end (all gh stubbed)")
    env = Env(wt, migrated, scratch / "smoke.json")
    before = env.path.read_text()

    multi = "Assist on Banco Galicia"
    cases = [
        ("list", wt.cmd_list, []),
        ("list --all", wt.cmd_list, ["--all"]),
        ("list --role demokit", wt.cmd_list, ["--role", "demokit"]),
        ("sprint", wt.cmd_sprint, []),
        ("status", wt.cmd_status, []),
        ("roles", wt.cmd_roles, []),
        ('report --sprint "Sprint 104"', wt.cmd_report, ["--sprint", "Sprint 104"]),
        ("report --last 14d", wt.cmd_report, ["--last", "14d"]),
        (f"logs {multi!r}", wt.cmd_logs, [multi]),
        ("sync-sprints --dry-run <task>", wt.cmd_sync_sprints, ["--dry-run", multi]),
        ("sync-sprints --all --dry-run", wt.cmd_sync_sprints, ["--all", "--dry-run"]),
        ("sync-sprints --help", wt.cmd_sync_sprints, ["--help"]),
    ]
    outs = {}
    with CliStubs(wt, mode="strict", sprints=env.sprints) as st:
        for label, fn, argv in cases:
            out, code = run_cmd(wt, fn, argv)
            outs[label] = out
            ok = code in (None, 0) and "Traceback" not in out
            check(ok, f"{label} exits cleanly",
                  f"code={code}\n{out[-600:]}")
        check(st.calls == [], "no GitHub call from any read-only command",
              str(st.names()[:6]))
    check(env.path.read_text() == before,
          "read-only commands did not touch the data file")

    # A couple of content assertions so "exits cleanly" isn't the whole story.
    check("Current sprint: Sprint 105" in outs["sprint"],
          "wt sprint names the current sprint", outs["sprint"][:300])
    check("Assist on Banco Galicia" in outs[f"logs {multi!r}"],
          "wt logs prints the task")
    check("Sprint 95" in outs["sync-sprints --dry-run <task>"]
          and "Dry run" in outs["sync-sprints --dry-run <task>"],
          "sync-sprints --dry-run shows the plan and says it changed nothing",
          outs["sync-sprints --dry-run <task>"][:800])
    print("\n--- wt sprint ---")
    print("\n".join("   " + l for l in outs["sprint"].strip().splitlines()[:14]))
    print("\n--- wt sync-sprints --dry-run 'Assist on Banco Galicia' ---")
    print("\n".join("   " + l for l in
                    outs["sync-sprints --dry-run <task>"].strip().splitlines()))


# -------------------------------------------------- 2. wt sprint offline fix --

def test_sprint_offline(wt, migrated, scratch):
    section("2. wt sprint works from the cache alone (the camelCase KeyError)")
    env = Env(wt, migrated, scratch / "sprintoff.json")

    # Pre-Phase-3 cmd_sprint read current["startDate"]/["duration"], which only
    # the live get_all_sprints() fetch emits. Prove a cache-shaped sprint dict
    # (exactly what get_cached_sprints returns) no longer raises.
    cached = wt.get_cached_sprints(env.data)
    check(all("startDate" not in s and "duration" not in s for s in cached),
          "cached sprint dicts really lack the camelCase keys")

    with CliStubs(wt, mode="strict", sprints=env.sprints):
        wt.get_all_sprints = lambda d: []          # simulate no network at all
        out, code = run_cmd(wt, wt.cmd_sprint, [])
    wt.get_all_sprints = lambda d, _s=env.sprints: copy.deepcopy(_s)
    check(code in (None, 0) and "KeyError" not in out,
          "wt sprint survives get_all_sprints() == [] by using the cache",
          f"code={code}\n{out[:400]}")
    check("offline" in out and "Sprint 105" in out,
          "…and says it is offline", out[:300])
    print("\n".join("   " + l for l in out.strip().splitlines()[:8]))


# ------------------------------------------------------- 3. requirement (a) --

def test_all_creates_nothing(wt, migrated, scratch):
    section("3. requirement (a): --all mints no issues without --create-issues")
    env = Env(wt, migrated, scratch / "all.json")
    answers = Answers("y")

    with CliStubs(wt, mode="record", sprints=env.sprints) as st:
        out, code = run_cmd(wt, wt.cmd_sync_sprints, ["--all", "--yes"], answers)
        creates = st.count("create_github_issue")
        closes = st.count("close_github_issue")
    check(code in (None, 0), f"sync-sprints --all exits cleanly (code={code})",
          out[-800:])
    check(creates == 0, "ZERO create_github_issue calls", f"got {creates}")
    hour_calls = st.count("update_project_hours")
    print(f"    stubbed gh calls: create_github_issue={creates}, "
          f"close_github_issue={closes}, update_project_hours={hour_calls}, "
          f"total={len(st.calls)}")
    # closes==0 is expected and correct here: on this data every close op the
    # Phase-2 blanket reconcile emitted was closing an issue it had *just*
    # minted (created=25, closed=25), so with create_issues=False there is
    # nothing new to close. The useful work --all still does is hours.
    check(hour_calls > 0, "…but hours were still synced (the point of --all)",
          f"got {hour_calls}")

    # An issue-less binding counts as "already bound", so a later --create-issues
    # run could never mint the issue that sprint needs. --all must therefore not
    # *add* any. Two already exist in the migrated fixture (Phase 1 seeded them
    # from tasks that have a sprint_id but were never linked to an issue), so the
    # assertion is "no new ones", measured against the input.
    def issueless(d):
        return {(t["id"], b.get("sprint_id")) for t in d["tasks"]
                for b in (t.get("sprint_issues") or []) if not b.get("issue")}

    pre = issueless(json.loads(Path(migrated).read_text()))
    post = issueless(env.reload())
    check(post <= pre,
          "--all added no issue-less 'placeholder' binding (would block a later "
          "--create-issues run)", str(sorted(post - pre)[:5]))
    print(f"    issue-less bindings: {len(pre)} before, {len(post)} after "
          f"(pre-existing, from the Phase 1 migration)")
    check("--create-issues" in out,
          "output tells the user how to mint the missing issues")
    unbilled = [l for l in out.splitlines() if "--create-issues" in l and "SKIP" in l]
    print(f"    {len(unbilled)} sprint(s) reported as needing an issue, e.g.:")
    for l in unbilled[:4]:
        print(f"      {l.strip()}")
    check(bool(unbilled), "the skipped past sprints are itemised, not silently dropped")

    # And with --create-issues it *does* mint them — same run, second pass.
    with CliStubs(wt, mode="record", sprints=env.sprints) as st2:
        out2, code2 = run_cmd(wt, wt.cmd_sync_sprints,
                              ["--all", "--create-issues", "--yes"], Answers("y"))
        creates2 = st2.count("create_github_issue")
    check(code2 in (None, 0), f"--all --create-issues exits cleanly (code={code2})",
          out2[-600:])
    check(creates2 > 0, "--create-issues does mint the previously-skipped issues",
          f"got {creates2}")
    print(f"    with --create-issues: create_github_issue={creates2}")

    # Third pass: nothing left.
    with CliStubs(wt, mode="strict", sprints=env.sprints) as st3:
        out3, code3 = run_cmd(wt, wt.cmd_sync_sprints,
                              ["--all", "--create-issues", "--dry-run"])
        check(st3.calls == [], "a follow-up dry run makes no GitHub call")
    check("Nothing to do" in out3,
          "after --create-issues the plan is empty", out3[-400:])


# ------------------------------------------------------- 4. requirement (b) --

def test_recurrent_reconciles(wt, migrated, scratch):
    section("4. Phase 5: recurrent series reconcile like any other task")
    env = Env(wt, migrated, scratch / "rec.json")
    recurrent = [t for t in env.data["tasks"] if t.get("status") == "recurrent"]
    print(f"    {len(recurrent)} recurrent task(s) after the merge:")
    for t in recurrent:
        n = len(t.get("sprint_issues") or [])
        print(f"      • {t['title']}  bindings={n}  start={t.get('start_sprint')}")

    # Phase 3 asserted the opposite: recurrent tasks were excluded because each
    # sprint was a separate cloned task, so reconciling would mint a past-sprint
    # issue per clone. Phase 5 merged the clones, so reconcile is now the right
    # thing to run — and it must NOT be skipped.
    check(recurrent, "the merge left at least one perpetual recurrent task")
    check(all(" - Sprint " not in t["title"] for t in recurrent),
          "no recurrent task still carries a '- Sprint N' suffix",
          str([t["title"] for t in recurrent if " - Sprint " in t["title"]]))

    with CliStubs(wt, mode="strict", sprints=env.sprints):
        out, _ = run_cmd(wt, wt.cmd_sync_sprints, ["--all", "--dry-run"])
    check("recurrent — use wt close-recurrent" not in out,
          "sync-sprints no longer reports recurrent tasks as skipped")

    # A perpetual series must never carry an issue forward: each sprint keeps its
    # own issue, so the sprint that just ended closes and the new one is minted.
    # Carrying forward would strand the ended sprint's hours (plan §6b/Phase 5).
    for t in recurrent:
        with CliStubs(wt, mode="strict", sprints=env.sprints):
            r = wt.reconcile_task_sprints(t, env.data, env.sprints, dry_run=True,
                                          create_issues=True)
        ops = [o["op"] for o in r["planned"]]
        check("repoint" not in ops,
              f"no carry-forward for perpetual {t['title'][:34]!r}", str(ops))
        check("create" in ops,
              f"mints the current sprint's issue for {t['title'][:34]!r}", str(ops))
        check(any(o["op"] == "close" for o in r["planned"]),
              f"closes the ended sprint for {t['title'][:34]!r}", str(ops))

    # That pair of ops is exactly what close-recurrent + new-recurrent did.
    with CliStubs(wt, mode="record", sprints=env.sprints) as st:
        run_cmd(wt, wt.cmd_sync_sprints,
                ["--all", "--create-issues", "--yes"], Answers("y"))
        made = [a[0].get("title") for n, a, k in st.calls if n == "create_github_issue"]
    for t in recurrent:
        want = t["title"]
        check(any(m == want or m.startswith(want) for m in made),
              f"a current-sprint issue was minted for {want[:34]!r}",
              str([m for m in made if want[:18] in (m or "")][:2]))
    check(not any(re.search(r" - Sprint \d+ \(Sprint \d+\)$", m or "") for m in made),
          "no double-suffixed clone-of-a-clone issue titles", str(made[:3]))


def test_split_sprint_alias(wt, migrated, scratch):
    section("5. `wt split-sprint` still works and warns")
    env = Env(wt, migrated, scratch / "alias.json")
    with CliStubs(wt, mode="strict", sprints=env.sprints) as st:
        out, code = run_cmd(wt, wt.cmd_split_sprint,
                            ["--dry-run", "Assist on Banco Galicia"])
        check(st.calls == [], "alias dry run makes no GitHub call")
    check(code in (None, 0), f"alias exits cleanly (code={code})", out[-400:])
    first = out.strip().splitlines()[0]
    check("deprecated" in first and "sync-sprints" in first,
          "prints a one-line deprecation notice first", repr(first))
    check("Sprint 95" in out, "and then renders the sync-sprints plan")
    check(wt.COMMANDS["split-sprint"] is wt.cmd_split_sprint
          and wt.COMMANDS["sync-sprints"] is wt.cmd_sync_sprints,
          "both names are wired into COMMANDS")
    print(f"    {first}")


# ------------------------------------------------------------ 6. idempotency --

def test_idempotent(wt, migrated, scratch):
    section("6. sync-sprints twice → the second run has nothing to do")
    env = Env(wt, migrated, scratch / "idem.json")
    task = "Assist on Banco Galicia"

    with CliStubs(wt, mode="record", sprints=env.sprints) as st:
        out1, code1 = run_cmd(wt, wt.cmd_sync_sprints, [task, "--yes"], Answers("y"))
        n1 = len(st.calls)
    check(code1 in (None, 0), f"run 1 exits cleanly (code={code1})", out1[-600:])
    print(f"    run 1: {n1} stubbed gh calls")
    print("\n".join("   " + l for l in out1.strip().splitlines()[-8:]))

    after1 = env.path.read_text()
    with CliStubs(wt, mode="strict", sprints=env.sprints) as st2:
        out2, code2 = run_cmd(wt, wt.cmd_sync_sprints, [task, "--yes"], Answers("y"))
        check(st2.calls == [], "run 2 makes zero GitHub calls", str(st2.names()))
    check("Nothing to do" in out2, "run 2 reports nothing to do", out2[-400:])
    check(env.path.read_text() == after1, "run 2 leaves the data file untouched")

    # Same for --all.
    with CliStubs(wt, mode="record", sprints=env.sprints):
        run_cmd(wt, wt.cmd_sync_sprints, ["--all", "--create-issues", "--yes"],
                Answers("y"))
    after_all = env.path.read_text()
    with CliStubs(wt, mode="strict", sprints=env.sprints) as st4:
        out4, _ = run_cmd(wt, wt.cmd_sync_sprints,
                          ["--all", "--create-issues", "--yes"], Answers("y"))
        check(st4.calls == [], "a repeated --all makes zero GitHub calls",
              str(st4.names()[:5]))
    check("Nothing to do" in out4, "repeated --all reports nothing to do",
          out4[-300:])
    check(env.path.read_text() == after_all, "…and changes nothing on disk")
    return env.path


# ------------------------------------------------------- 7. set-sprint / done --

def test_set_sprint(wt, migrated, scratch):
    section("7. wt set-sprint now corrects the START sprint")
    env = Env(wt, migrated, scratch / "setsprint.json")
    task = find(env.data, "Assist on Banco Galicia")
    tid = task["id"]
    before = {k: task.get(k) for k in ("sprint", "sprint_id",
                                       "start_sprint", "start_sprint_id")}
    print(f"    before: {before}")

    with CliStubs(wt, mode="strict", sprints=env.sprints) as st:
        out, code = run_cmd(wt, wt.cmd_set_sprint, [tid, "Sprint 99"])
        check(st.calls == [], "set-sprint makes no GitHub call")
    check(code in (None, 0), f"exits cleanly (code={code})", out)
    t = next(x for x in env.reload()["tasks"] if x["id"] == tid)
    check(t.get("start_sprint") == "Sprint 99", "start_sprint written",
          str(t.get("start_sprint")))
    check(t.get("start_sprint_id") == next(s["id"] for s in env.sprints
                                           if s["title"] == "Sprint 99"),
          "start_sprint_id written", str(t.get("start_sprint_id")))
    check(t.get("sprint") == before["sprint"] and t.get("sprint_id") == before["sprint_id"],
          "the reconcile-owned sprint/sprint_id pointer is left alone",
          f"{t.get('sprint')} / {t.get('sprint_id')}")
    check("sync-sprints" in out, "output points at sync-sprints for hours", out)
    print("\n".join("   " + l for l in out.strip().splitlines()))

    # `none` clears it.
    with CliStubs(wt, mode="strict", sprints=env.sprints):
        out2, code2 = run_cmd(wt, wt.cmd_set_sprint, [tid, "none"])
    t = next(x for x in env.reload()["tasks"] if x["id"] == tid)
    check(code2 in (None, 0) and "start_sprint" not in t and "start_sprint_id" not in t,
          "set-sprint <task> none clears both start-sprint keys",
          f"code={code2} keys={[k for k in t if 'start' in k]}\n{out2}")
    check("Cleared start sprint" in out2, "…and says so", out2)

    # wt sprint surfaces the carry-over marker. Needs an *open* task, since
    # cmd_sprint only lists non-done tasks.
    open_task = find(env.data, "CI Check to read Demo Blocks content and verify if "
                               "the change would break the demo block")
    otid = open_task["id"]
    with CliStubs(wt, mode="strict", sprints=env.sprints):
        run_cmd(wt, wt.cmd_set_sprint, [otid, "Sprint 90"])
        out3, _ = run_cmd(wt, wt.cmd_sprint, [])
    line = next((l for l in out3.splitlines() if "CI Check" in l), "")
    check("started Sprint 90" in line,
          "wt sprint shows 'started Sprint N' for a carry-over", repr(line))
    print("   " + line.strip())

    # A start sprint *later* than the group sprint is not a carry-over.
    with CliStubs(wt, mode="strict", sprints=env.sprints):
        run_cmd(wt, wt.cmd_set_sprint, [otid, "Sprint 111"])
        out4, _ = run_cmd(wt, wt.cmd_sprint, [])
    line4 = next((l for l in out4.splitlines() if "CI Check" in l), "")
    check("started" not in line4,
          "…and not for a start sprint that is later than the group sprint",
          repr(line4))
    print("   " + line4.strip())


def test_done(wt, migrated, scratch):
    section("8. wt done renders the reconcile result (fully stubbed)")
    env = Env(wt, migrated, scratch / "done.json")
    task = find(env.data, "CI Check to read Demo Blocks content and verify if the "
                          "change would break the demo block")
    tid, issue = task["id"], wt.task_current_issue(task, env.data)
    print(f"    task status={task['status']}  issue={issue}")
    print(f"    sprints with time: "
          + ", ".join(f"{e['sprint_title']}={e['total_mins']:.0f}m"
                      for e in wt.task_sprints_with_time(task, env.sprints)))

    with CliStubs(wt, mode="record", sprints=env.sprints) as st:
        out, code = run_cmd(wt, wt.cmd_done, [tid], Answers("y", ""))
        creates = st.count("create_github_issue")
        closes = st.count("close_github_issue")
    check(code in (None, 0), f"wt done exits cleanly (code={code})", out[-800:])
    print("\n".join("   " + l for l in out.strip().splitlines()))
    t = next(x for x in env.reload()["tasks"] if x["id"] == tid)
    check(t["status"] == "done", "task marked done")
    check("Closed:" in out, "prints the close line")
    check(any(l.strip().startswith(("+", "x", "=", "→", ".")) for l in out.splitlines()),
          "renders reconcile outcome lines instead of the old 'Split:' breakdown",
          out)
    check("Split:" not in out, "the old split breakdown wording is gone")
    check("Sprint:" in out, "reports which sprint the hours went to")
    print(f"    stubbed: create_github_issue={creates}, close_github_issue={closes}")
    check(closes >= 1, "the task's own issue was closed", str(closes))
    check(all("cross_sprint_parent" not in x for x in env.reload()["tasks"]),
          "no shadow task object created")
    check(len(env.reload()["tasks"]) == len(env.data["tasks"]),
          "no task added or removed",
          str(len(env.reload()["tasks"])))

    # close_task's documented return keys must survive.
    env2 = Env(wt, migrated, scratch / "done2.json")
    t2 = find(env2.data, "Move demo block scripts to the new field-eng-demo-blocks repo")
    with CliStubs(wt, mode="record", sprints=env2.sprints):
        res = wt.close_task(t2, env2.data, wt.save)
    required = {"success", "issue_created", "issue_closed", "project_updated",
                "skipped_github", "comment_added", "split_performed",
                "split_result", "error"}
    check(required <= set(res), "close_task keeps every documented return key",
          str(sorted(required - set(res))))
    check(res["success"] is True, "close_task succeeded", str(res["error"]))
    sr = res["split_result"] or {}
    check("sprint_tasks_created" in sr and "main_sprint" in sr,
          "split_result still carries the legacy render keys", str(sorted(sr))[:300])
    print(f"    split_performed={res['split_performed']}  "
          f"main_sprint={sr.get('main_sprint')}  "
          f"sprint_tasks_created={[(e.get('sprint'), e.get('issue_ref')) for e in sr.get('sprint_tasks_created', [])]}")

    # A task with no repo still short-circuits.
    env3 = Env(wt, migrated, scratch / "done3.json")
    norepo = next((t for t in env3.data["tasks"]
                   if not wt.get_task_repo(t) and t.get("status") != "done"), None)
    if norepo is None:
        # Every open task in this fixture has a repo, so synthesize the case.
        norepo = {"id": wt.uid(), "title": "Synthetic repo-less task",
                  "description": "", "role_id": "other", "status": "inprogress",
                  "created_at": 1, "logs": [], "sprint_issues": []}
        env3.data["tasks"].append(norepo)
        print("    (no repo-less open task in the fixture — synthesized one)")
    with CliStubs(wt, mode="strict", sprints=env3.sprints) as st3:
        res3 = wt.close_task(norepo, env3.data, wt.save)
        check(st3.calls == [], "repo-less close makes no GitHub call")
    check(res3["success"] and res3["skipped_github"] and norepo["status"] == "done",
          "repo-less task closes locally with skipped_github", str(res3))


# --------------------------------------------------------- 9. link/unlink/etc --

def test_issue_routing(wt, migrated, scratch):
    section("9. github_issue reads/writes go through the binding accessors")
    env = Env(wt, migrated, scratch / "route.json")

    # Static check: no read of task["github_issue"] outside the accessor layer.
    src = (REPO / "wt.py").read_text().splitlines()
    allowed_fns = {
        "_shadow_binding", "_legacy_binding_for_task", "task_current_issue",
        "task_issue_refs", "set_task_current_issue", "clear_task_current_issue",
        "_reconcile_plan", "reconcile_task_sprints",
    }
    cur = None
    offenders = []
    for i, line in enumerate(src, 1):
        m = re.match(r"def (\w+)", line)
        if m:
            cur = m.group(1)
        if re.search(r"""(?<!_)\b(task|t|shadow)(\.get\(["']github_issue|\[["']github_issue)""",
                     line) and not line.lstrip().startswith(("#", "*")):
            if cur not in allowed_fns:
                offenders.append(f"{i}: {cur}: {line.strip()}")
    check(not offenders,
          "no wt.py function outside the accessor layer touches "
          "task['github_issue'] directly", "\n      ".join(offenders))
    print(f"    accessor layer: {', '.join(sorted(allowed_fns))}")

    # link → binding + legacy mirror; unlink → both cleared. IRON Infusion has
    # five bindings, so it also exercises "past-sprint issues left behind".
    task = find(env.data, "IRON Infusion")
    tid = task["id"]
    n_bindings = len(task["sprint_issues"])
    check(n_bindings > 1, "the test task really has multiple bindings",
          str(n_bindings))
    with CliStubs(wt, mode="record", sprints=env.sprints) as st:
        out, code = run_cmd(wt, wt.cmd_unlink, [tid])
    t = next(x for x in env.reload()["tasks"] if x["id"] == tid)
    check(code in (None, 0), f"unlink exits cleanly (code={code})", out)
    check("github_issue" not in t, "legacy key removed")
    check(wt.task_current_issue(t, env.data) is None,
          "task_current_issue() now returns None",
          str(wt.task_current_issue(t, env.data)))
    check(len(t["sprint_issues"]) == n_bindings,
          "the binding itself is kept (bindings are never deleted)",
          f"{len(t['sprint_issues'])} vs {n_bindings}")
    check("Still bound to" in out, "unlink names the past-sprint issues left behind",
          out)
    print("\n".join("   " + l for l in out.strip().splitlines()))

    # relink
    with CliStubs(wt, mode="record", sprints=env.sprints) as st:
        wt.subprocess = FakeGhLink()          # cmd_link shells out directly
        out2, code2 = run_cmd(wt, wt.cmd_link, [tid, "grafana/field-eng#5238"])
    t = next(x for x in env.reload()["tasks"] if x["id"] == tid)
    check(code2 in (None, 0), f"link exits cleanly (code={code2})", out2)
    check(t.get("github_issue") == "grafana/field-eng#5238", "legacy mirror written",
          str(t.get("github_issue")))
    check(wt.task_current_issue(t, env.data) == "grafana/field-eng#5238",
          "and the binding carries it too")
    binding = wt._find_binding(t["sprint_issues"], issue="grafana/field-eng#5238")
    check(binding is not None and binding.get("sprint_id") == t.get("sprint_id"),
          "the issue landed on the binding for the task's own sprint",
          str(binding))

    # cmd_list's '#' marker follows the binding, not the legacy key.
    env2 = Env(wt, migrated, scratch / "route2.json")
    t2 = find(env2.data, "IRON Infusion")
    ref = t2.pop("github_issue")          # binding-only, as a future phase will be
    wt.save(env2.data)
    with CliStubs(wt, mode="strict", sprints=env2.sprints):
        out3, _ = run_cmd(wt, wt.cmd_list, ["--all"])
    line = next((l for l in out3.splitlines() if "IRON Infusion" in l), "")
    check("#" in line, "wt list still shows the '#' issue marker with no legacy key",
          repr(line))
    check(wt.task_current_issue(t2, env2.data) == ref,
          "task_current_issue reads it from the binding", str(ref))
    print(f"    binding-only task renders as: {line.strip()[:70]}")


class FakeGhLink:
    """Minimal `gh issue view --json number,title` stand-in for cmd_link."""

    def run(self, argv, **kw):
        class R:
            returncode = 0
            stdout = json.dumps({"number": 5238, "title": "IRON Infusion"})
            stderr = ""
        return R()

    def __getattr__(self, name):
        def boom(*a, **k):
            raise AssertionError(f"wt.subprocess.{name} called: {a!r}")
        return boom


# ------------------------------------------------------ 10. shadow plumbing --

def test_shadow_plumbing_gone(wt, migrated, scratch):
    section("10. shadow plumbing deleted, migration sweep intact")
    src = (REPO / "wt.py").read_text()

    # Every remaining cross_sprint_parent mention must be inside the migration
    # (the every-load shadow sweep, which is the iCloud defence and stays) — and
    # anywhere else it may only appear in prose, never in a live code line.
    lines = src.splitlines()
    mig_start = next(i for i, l in enumerate(lines)
                     if l.startswith("def _migrate_shadows_to_bindings"))
    mig_end = next(i for i, l in enumerate(lines)
                   if l.startswith("def resolve_task_by_id"))
    doc = docstring_and_comment_lines(src)
    outside = [f"{i + 1}: {lines[i].strip()}"
               for i in range(len(lines))
               if "cross_sprint_parent" in lines[i]
               and not (mig_start <= i < mig_end)
               and (i + 1) not in doc]
    check(not outside,
          "no live cross_sprint_parent filter/guard outside the migration",
          "\n      ".join(outside))
    print(f"    cross_sprint_parent survives only inside "
          f"_migrate_shadows_to_bindings (wt.py lines {mig_start + 1}-{mig_end})")
    check("existing_split_sprint_ids" not in src,
          "existing_split_sprint_ids is gone")
    check(not hasattr(wt, "existing_split_sprint_ids"),
          "…and is not importable")
    flag_lines = [f"{i + 1}: {lines[i].strip()}" for i in range(len(lines))
                  if "--shadows" in lines[i] and (i + 1) not in doc]
    check(not flag_lines,
          "the `wt list --shadows` flag is gone from live code",
          "\n      ".join(flag_lines))
    check("--shadows" not in (REPO / "_wt").read_text(),
          "_wt never had a --shadows entry and still doesn't")

    # `wt list --shadows` is now just an ignored arg, not a crash.
    env = Env(wt, migrated, scratch / "shadow.json")
    with CliStubs(wt, mode="strict", sprints=env.sprints):
        out_plain, _ = run_cmd(wt, wt.cmd_list, [])
        out_flag, code = run_cmd(wt, wt.cmd_list, ["--shadows"])
    check(code in (None, 0) and out_flag == out_plain,
          "`wt list --shadows` no longer changes anything", f"code={code}")

    # The every-load sweep still strips a re-introduced shadow (iCloud defence).
    raw = json.loads(Path(migrated).read_text())
    parent = next(t for t in raw["tasks"] if t["title"] == "IRON Infusion")
    raw["tasks"].append({
        "id": "99999999999999zzzz", "title": "IRON Infusion (Sprint 97)",
        "description": "", "role_id": parent["role_id"], "status": "done",
        "cross_sprint_parent": parent["id"],
        "sprint": "Sprint 97", "sprint_id": next(
            s["id"] for s in env.sprints if s["title"] == "Sprint 97"),
        "github_issue": "grafana/field-eng#99999",
        "created_at": 1, "logs": [{"id": "l1", "minutes": 120,
                                   "note": "Sprint split: 2h from IRON Infusion",
                                   "at": 1}],
    })
    inj = scratch / "reinjected.json"
    inj.write_text(json.dumps(raw))
    os.environ["WT_DATA_FILE"] = str(inj)
    wt.DATA_FILE = inj
    swept = wt.load()
    check(not any(t.get("cross_sprint_parent") for t in swept["tasks"]),
          "a re-introduced shadow is stripped on load")
    p = next(t for t in swept["tasks"] if t["title"] == "IRON Infusion")
    b = next((x for x in p["sprint_issues"] if x.get("sprint") == "Sprint 97"), None)
    check(b is not None and b["issue"] == "grafana/field-eng#99999"
          and b["state"] == "closed" and b["hours_synced"] == 2.0,
          "…and converted into a binding on its parent with the right hours",
          str(b))
    print(f"    swept shadow → binding {b}")


# ---------------------------------------------------------- 11. invariants --

def test_invariants(wt, work, baseline):
    section("11. tools/check_invariants.py after the CLI runs")
    proc = real_subprocess.run(
        [sys.executable, str(REPO / "tools" / "check_invariants.py"),
         str(work), str(baseline)],
        capture_output=True, text=True)
    print("\n".join("    " + l for l in proc.stdout.strip().splitlines()))
    if proc.stderr.strip():
        print("    stderr:", proc.stderr.strip()[:400])
    check(proc.returncode == 0, "check_invariants exits 0", f"rc={proc.returncode}")
    m = re.search(r"minutes=([\d.]+)\s+logs=(\d+)", proc.stdout)
    check(bool(m) and m.group(1) == "26620.71" and m.group(2) == "392",
          "minutes 26620.71 and 392 logs, unchanged",
          m.group(0) if m else proc.stdout[:200])


def test_creation_paths(wt, migrated, scratch):
    section("11b. task-creation paths write both the binding and the legacy key")
    env = Env(wt, migrated, scratch / "create.json")
    current = wt.find_sprint_for_date(env.sprints, __import__("datetime").date.today())

    def one_binding(t, label):
        b = t.get("sprint_issues") or []
        issue = wt.task_current_issue(t, env.data)
        check(len(b) == 1 and b[0].get("issue") == issue
              and b[0].get("sprint_id") == t.get("sprint_id")
              and t.get("github_issue") == issue,
              f"{label}: exactly one binding, issue on it and mirrored to the "
              f"legacy key", json.dumps(b, default=str) + f" legacy={t.get('github_issue')}")
        return issue

    # wt add --create-issue
    with CliStubs(wt, mode="record", sprints=env.sprints) as st:
        out, code = run_cmd(wt, wt.cmd_add,
                            ["Phase3 synthetic add", "--role", "other",
                             "--repo", "grafana/field-eng", "--create-issue"])
    check(code in (None, 0), f"wt add --create-issue exits cleanly (code={code})", out)
    t = next(x for x in env.reload()["tasks"] if x["title"] == "Phase3 synthetic add")
    ref = one_binding(t, "wt add --create-issue")
    check(t.get("sprint_id") == (current or {}).get("id"),
          "…on the current sprint's binding", str(t.get("sprint")))
    print(f"    wt add → {ref} on {t.get('sprint')}")

    # wt add-issue / create_task_from_issue
    env2 = Env(wt, migrated, scratch / "create2.json")
    with CliStubs(wt, mode="record", sprints=env2.sprints):
        wt.subprocess = FakeGhAddIssue()
        res = wt.create_task_from_issue(env2.data, "grafana/field-eng#77777",
                                        role_id="other")
    check(res["error"] is None and res["task"] is not None,
          "create_task_from_issue succeeded", str(res.get("error")))
    one_binding(res["task"], "create_task_from_issue")
    # …and it dedupes on the binding, not just the legacy key.
    res["task"].pop("github_issue")
    with CliStubs(wt, mode="record", sprints=env2.sprints):
        wt.subprocess = FakeGhAddIssue()
        again = wt.create_task_from_issue(env2.data, "grafana/field-eng#77777",
                                          role_id="other")
    check(again["existed"] is True and again["task"] is res["task"],
          "…and dedupes off the binding even with no legacy key", str(again))

    # new-recurrent
    env3 = Env(wt, migrated, scratch / "create3.json")
    with CliStubs(wt, mode="record", sprints=env3.sprints):
        summary = wt.create_current_sprint_recurrent_tasks(env3.data, wt.save)
    made = [r for r in summary["results"] if r.get("issue")]
    check(summary["error"] is None and made,
          "new-recurrent created tasks with issues", str(summary)[:300])
    for r in made[:3]:
        t = next(x for x in env3.data["tasks"] if x["title"] == r["title"])
        one_binding(t, f"new-recurrent {r['title'][:28]!r}")


class FakeGhAddIssue:
    """`gh issue view --json number,title,state,url` stand-in."""

    def run(self, argv, **kw):
        class R:
            returncode = 0
            stdout = json.dumps({"number": 77777, "title": "Phase3 synthetic issue",
                                 "state": "OPEN", "url": "https://example/77777"})
            stderr = ""
        return R()

    def __getattr__(self, name):
        def boom(*a, **k):
            raise AssertionError(f"wt.subprocess.{name} called: {a!r}")
        return boom


def test_close_task_github_diff(migrated, scratch):
    section("12. close_task's GitHub writes vs pre-Phase-3 (Option A assertion)")
    old = scratch / "wt_pre_phase3.py"
    proc = real_subprocess.run(["git", "show", "HEAD:wt.py"],
                               cwd=REPO, capture_output=True, text=True)
    if proc.returncode != 0:
        check(False, "could not extract HEAD:wt.py for the comparison",
              proc.stderr.strip()[:200])
        return
    old.write_text(proc.stdout)
    sub = scratch / "cmp"
    sub.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, WT_DATA_FILE=str(sub / "unused.json"))
    run = real_subprocess.run(
        [sys.executable, str(REPO / "tools" / "compare_close_task.py"),
         str(old), str(migrated), str(sub)],
        capture_output=True, text=True, env=env)
    print("\n".join("    " + l for l in run.stdout.strip().splitlines()))
    if run.stderr.strip():
        print("    stderr:", run.stderr.strip()[:400])
    same = len([l for l in run.stdout.splitlines() if l.startswith("  same ")])
    diff = len([l for l in run.stdout.splitlines() if l.startswith("  DIFF ")])
    err = len([l for l in run.stdout.splitlines() if l.startswith("  ERR ")])
    gone = len([l for l in run.stdout.splitlines() if l.startswith("  gone ")])
    check(err == 0, "no task errored under either version", f"{err} error(s)")
    # 'gone' = a title the Phase 5 merge absorbed, so it is no longer comparable.
    compared = same + diff
    check(compared >= 8, "enough tasks remain comparable to be meaningful",
          f"same={same} diff={diff} gone={gone}")
    check(same >= compared - 2,
          "all but at most two comparable tasks are byte-identical",
          f"same={same} of {compared} comparable (gone={gone})")

    # NOTE: this check used to assert "diffs are additions only", i.e. that the
    # refactor never changed a GitHub-visible value (Option A's premise). The
    # `closing=True` change to close_task deliberately breaks that, so the
    # assertion is now narrower: the only writes a close may *remove* are
    #   (a) Sprint-field writes pointing at the CURRENT sprint — the close no
    #       longer parks a task's issue on a sprint it was never worked in, and
    #   (b) the whole mint-a-past-sprint-issue block (NEW#n) for the newest
    #       sprint with time, because the carried-forward issue now covers it.
    # Anything else disappearing is a regression.
    removed = [l.strip() for l in run.stdout.splitlines()
               if l.strip().startswith("-") and "---" not in l]
    import wt as _wt
    current = _wt.find_sprint_for_date(
        _wt.get_cached_sprints(json.loads(Path(migrated).read_text())),
        __import__("datetime").date.today())
    cur_id = (current or {}).get("id") or "\0"
    def explained(line):
        # (a) a Sprint field write parking an issue on the current sprint
        if "update_project_sprint" in line and cur_id in line:
            return True
        # (b) any write against a newly-minted issue the new code doesn't need…
        if "NEW#" in line:
            return True
        # …including the mint itself, whose line names the title, not NEW#n
        if re.search(r"create_github_issue\('.*\(Sprint \d+\)'", line):
            return True
        # (c) the 0.0h report that this change exists to replace
        if "add_to_project_and_update" in line and ", 0.0," in line:
            return True
        return False

    unexplained = [l for l in removed if not explained(l)]
    check(not unexplained,
          "the only removed writes are current-sprint Sprint fields, absorbed "
          "past-sprint issue mints, and the 0.0h report",
          "\n      ".join(unexplained))
    # The two assertions below describe the `closing=True` change specifically.
    # They only hold while HEAD predates it — once it is merged, HEAD *is* the
    # new behaviour and the diff is empty of its signature. Assert them when the
    # comparison spans that change, and say so plainly when it doesn't, rather
    # than failing on a moving baseline.
    zero_before = [l for l in run.stdout.splitlines()
                   if l.strip().startswith("-")
                   and "add_to_project_and_update" in l and ", 0.0," in l]
    nonzero_after = [l for l in run.stdout.splitlines()
                     if l.strip().startswith("+")
                     and "add_to_project_and_update" in l and ", 0.0," not in l]
    spans_closing_change = bool(zero_before or any("NEW#" in l for l in removed))
    if spans_closing_change:
        check(any("NEW#" in l for l in removed),
              "at least one past-sprint issue is no longer minted "
              "(absorbed by the carried-forward issue)", str(len(removed)))
        check(zero_before and nonzero_after,
              "a close that used to report 0.0h now reports its real sprint hours",
              f"before={len(zero_before)} after={len(nonzero_after)}")
    else:
        print("    (HEAD already contains closing=True — its signature is not in "
              "this diff, so those two assertions are vacuous here)")
        check(diff == 0,
              "with closing=True already in HEAD, close_task's GitHub writes are "
              "unchanged by this branch", f"diff={diff}")


def test_reconcile_harness_still_passes(fixture, migrated, baseline, scratch):
    section("13. tools/test_reconcile.py (Phase 2) still passes")
    sub = scratch / "phase2"
    sub.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, WT_DATA_FILE=str(sub / "unused.json"))
    proc = real_subprocess.run(
        [sys.executable, str(REPO / "tools" / "test_reconcile.py"),
         str(fixture), str(migrated), str(baseline), str(sub)],
        capture_output=True, text=True, env=env)
    tail = proc.stdout.strip().splitlines()[-3:]
    print("\n".join("    " + l for l in tail))
    check(proc.returncode == 0, "test_reconcile.py exits 0", f"rc={proc.returncode}")


def main():
    if len(sys.argv) != 5:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    fixture, migrated, baseline, scratch = (Path(a).expanduser() for a in sys.argv[1:])
    scratch.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("WT_DATA_FILE", str(scratch / "unused.json"))
    import wt

    real = Path.home() / ".workload_tracker.json"
    if wt._resolve_data_file() == real or wt.DATA_FILE == real:
        print("REFUSING TO RUN: WT_DATA_FILE resolves to the live data file",
              file=sys.stderr)
        return 2

    test_cli_smoke(wt, migrated, scratch)
    test_sprint_offline(wt, migrated, scratch)
    test_all_creates_nothing(wt, migrated, scratch)
    test_recurrent_reconciles(wt, migrated, scratch)
    test_split_sprint_alias(wt, migrated, scratch)
    work = test_idempotent(wt, migrated, scratch)
    test_set_sprint(wt, migrated, scratch)
    test_done(wt, migrated, scratch)
    test_issue_routing(wt, migrated, scratch)
    test_shadow_plumbing_gone(wt, migrated, scratch)
    test_invariants(wt, work, baseline)
    test_creation_paths(wt, migrated, scratch)
    test_close_task_github_diff(migrated, scratch)
    test_reconcile_harness_still_passes(fixture, migrated, baseline, scratch)

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  x {f}")
        return 1
    print("All Phase 3 checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
