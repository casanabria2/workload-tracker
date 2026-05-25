---
name: new-task-with-issue
description: Create a new workload-tracker task and its linked GitHub issue. Uses the `wt` CLI exclusively (no direct `gh` calls). Triggered when the user asks to "create a task with an issue", "add a task and open a GitHub issue", "create issue for new task", etc.
---

# Create a workload-tracker task with a GitHub issue

This skill creates a task in the workload tracker and a linked GitHub issue
in one step, using the `wt add --create-issue` CLI subcommand. **Always use
`wt`; never call `gh` directly** — the CLI also adds the issue to the
configured GitHub Project and sets the Status, Activity, Sprint, and Hours
fields, which a raw `gh issue create` does not.

## When to use this skill

Use this skill whenever the user wants to create a task that should have a
corresponding GitHub issue from the start. Examples:

- "Create a task `Refactor login flow` and open a GH issue for it"
- "Add a `Sprint 95` copy of the Ana 1:1 task with an issue"
- "New task in the `demokit` role with an issue"

If the user only wants a local task (no GitHub issue), use plain `wt add`
without this skill.

## Inputs to gather

Before invoking the CLI, make sure you have:

1. **Title** (required). The exact text the user wants.
2. **Role** (required). One of the configured roles. Resolve fuzzy names
   against `data["roles"]` if needed:
   ```bash
   python3 -c "import json,pathlib; print([r['id'] for r in json.loads((pathlib.Path.home()/'.workload_tracker.json').read_text())['roles']])"
   ```
   If the user gives an ambiguous name, ask them to disambiguate.
3. **Status** (optional, default `todo`). One of `todo`, `inprogress`,
   `recurrent`, `done`.
4. **Sprint** (optional). One of:
   - omitted → auto-assigned to the current sprint
   - explicit sprint title (e.g. `"Sprint 95"`) → must match an existing
     sprint title from GitHub Projects
   - `none` → no sprint assignment
5. **Description** (optional). Goes into the issue body as well, since the
   issue body is read from the task's local notes.

## Preconditions to verify

The CLI fails fast if these are not met — verify them up-front so you can
guide the user before running anything:

- **The chosen role must have a `github_repo`** in its config. Check with
  `wt roles` (or read `data["roles"][i]["github_repo"]`). If the role has no
  repo, ask the user whether to set one with
  `wt roles set-repo <role> owner/repo` first, or pick a different role.
- The user must be logged into `gh` (`gh auth status`). The CLI uses `gh`
  internally; if auth is missing, the issue creation will fail with a clear
  error.

## Running it

The one-liner:

```bash
python3 wt.py add "Task title here" \
    --role <role> \
    --status <status> \
    --sprint "<sprint>" \
    --create-issue
```

Notes:
- Always quote the title.
- Omit `--status` to default to `todo`.
- Omit `--sprint` to auto-assign to the current sprint.
- `--create-issue` is the only flag that triggers GitHub work.

On success the CLI prints:
```
✓ Added: <title>  [Role]  [Status]  [Sprint NN]
  id: <task-id>
  ✓ Created issue: owner/repo#NNNN
  ✓ Added to project (Status/Activity/Sprint/Hours)
```

## What the flag does under the hood

`wt add --create-issue` (in `wt.py:cmd_add`) does, in order:

1. Validates that the role has a `github_repo` (errors out otherwise).
2. Creates the task in `~/.workload_tracker.json` with the usual sprint
   auto-assignment.
3. Calls `create_github_issue(task, repo)` which uses `gh issue create`
   internally and assigns it to `@me`. Body comes from local notes if any.
4. Calls `setup_issue_in_project(issue_ref, task, data)` to add the issue
   to the configured GitHub Project and set its Status, Activity, Sprint,
   and Hours fields.
5. Writes the resulting `github_issue` reference back to the task.

This is the **same path** the TUI uses when a user enables GH integration on
task creation, so behaviour stays consistent.

## What NOT to do

- **Don't** call `gh issue create` directly. It bypasses the project setup
  and leaves the task without a `github_issue` field.
- **Don't** call `wt.create_github_issue()` from an ad-hoc Python script
  when you can use `wt add --create-issue` instead. The CLI is the
  supported entry point.
- **Don't** create the task first and then "add an issue later" via raw
  `gh` — use this flag as part of the initial creation, or use
  `wt link <task> owner/repo#NN` if the issue already exists.

## Examples

### Standard new task with issue in the current sprint
```bash
python3 wt.py add "Refactor login flow" --role demokit --create-issue
```

### Recurrent task pinned to a specific past sprint (the Ana 1:1 pattern)
```bash
python3 wt.py add "Ana 1:1 calls - casanabria - Sprint 95" \
    --role other --status recurrent --sprint "Sprint 95" --create-issue
```

### Task in progress with a description
```bash
python3 wt.py add "Investigate dashboard timeout" \
    --role strategic --status inprogress \
    --desc "Customer reports 30s+ load times on demo dashboard" \
    --create-issue
```

### Bulk: create one per sprint (loop in a shell, not a single command)
For multi-sprint backfills, loop in shell. **Do not parallelize** — the JSON
data file is read-modify-written by each invocation:
```bash
for sprint in "Sprint 95" "Sprint 96" "Sprint 97"; do
    python3 wt.py add "Ana 1:1 calls - casanabria - $sprint" \
        --role other --status recurrent --sprint "$sprint" --create-issue
done
```

## Failure modes and how to react

| Symptom | Likely cause | Fix |
|---|---|---|
| `--create-issue requires role 'X' to have a github_repo set.` | Role missing repo | `wt roles set-repo <role> owner/repo`, then retry |
| `Failed to create issue: ...` | `gh` auth / network / repo permission | Run `gh auth status`; check the user has issue-create perms on the repo |
| `! project setup: <error>` | Project lookup failed but task and issue exist | Don't re-run — re-running creates a duplicate issue. Use `wt link`/`wt unlink` and the TUI's "sync project" path to recover |
| `Sprint 'X' not found.` | Sprint title typo or non-existent | Look up exact titles with `python3 -c "import wt; [print(s['title']) for s in wt.get_all_sprints(wt.load())]"` |

## Verifying success

After the command, confirm with:
```bash
wt list                          # task appears in the list
gh issue view <issue-ref>        # issue exists, assigned to user
```

The task's stored `github_issue` field will be `owner/repo#NN` — use
`python3 -c "import wt; t = wt.resolve_task(wt.load(), 'partial title'); print(t.get('github_issue'))"`
to read it back.
