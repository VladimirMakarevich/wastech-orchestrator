# The audit commit never stages the task file's removal from `tasks/preparing/`

Status: **proposed** Date: 2026-09-10 Owner: Vladimir Makarevich

## Problem

`worc promote` moves a staged task file `tasks/preparing/<id>.md` → `tasks/pending/<id>.md` as a plain rename on disk, and the run's terminal cleanup then moves it on to `tasks/done/<id>.md`. When the staged file was **committed** while it sat in `preparing/` — the documented operator workflow: compose it in the staging area the scanner never scans, commit it, then promote — the audit commit at the end of the run records only half of that journey. It stages the *appearance* of `tasks/done/<id>.md`, and never the *removal* of `tasks/preparing/<id>.md`.

Two consequences, once per task, on every run of this shape:

- **The task file is committed twice.** The `preparing/` copy is still in HEAD, and nothing ever deletes it, so the branch carries both `tasks/done/<id>.md` and `tasks/preparing/<id>.md`. Merge the PR and the stale staging copy lands on the base branch.
- **A dangling `D` is left in the working tree** after terminal cleanup — precisely the condition the surrounding code comments already name as the bug they were written to prevent ("leaving the base working tree dirty (a dangling `D`) after terminal cleanup").

## Current behavior (verified)

The staging area is a *tracked* repo directory, created by `install` alongside the other lifecycle folders — [`cli.py` → `REPO_TASK_DIRS`](../../src/wastech_orchestrator/cli.py):

```python
REPO_TASK_DIRS: tuple[str, ...] = (
    "tasks/preparing",
    "tasks/pending",
    "tasks/done",
    "tasks/failed",
)
```

`promote` is a filesystem rename and nothing else — no index, no commit ([`cli.py` → `_promote_one`](../../src/wastech_orchestrator/cli.py)): `src.replace(dest)`. So after a promote, git sees `tasks/preparing/<id>.md` as tracked-but-deleted and `tasks/pending/<id>.md` as untracked.

The audit commit builds its staging pathspec from a hardcoded tuple of three lifecycle states ([`git_manager.py` → `commit_audit`](../../src/wastech_orchestrator/git_manager.py)):

```python
audit_files = [
    f"{self._tasks_dir}/{state}/{task_id}{suffix}"
    # Destination states (``done``/``failed``) stage the file's *appearance*; the source
    # state (``pending``) stages its *removal* on a lifecycle move — without the source
    # path a ``pending→failed`` / ``pending→done`` move of a base-tracked task file leaves
    # a dangling ``D`` on the base branch after terminal cleanup.
    for state in ("done", "failed", "pending")
    for suffix in (".md", ".summary.md")
]
```

`preparing` is absent. The comment states the intent exactly — the source state whose removal is staged is assumed to be `pending` — and that assumption holds only when the file was base-tracked *in* `pending`. A file base-tracked in `preparing` is moved out from under git with nothing staged: `git add -A` over the three candidate pathspecs cannot match a path that is not in the list, so the tracked deletion is never staged and the `preparing/` blob is never removed from the tree.

## Reproduction

### Preconditions

The one that matters: **the task file must be committed to the base branch while it is still in `tasks/preparing/`.** Everything else about the run is incidental. If the staged file is untracked when `promote` runs, git has nothing to lose track of — no duplicate, no dangling `D`, no bug.

That precondition is reachable by design rather than by misuse: `tasks/preparing/` is one of the four tracked lifecycle folders `install` creates at the repo root ([`REPO_TASK_DIRS`](../../src/wastech_orchestrator/cli.py)), and the guide describes the whole set as "deliberately **not** in `.worc/`" and tracked (`packaged/guide/footprint.md`). The documented happy path does commit *later* — "`worc promote <id>` moves it into `tasks/pending/`, where it is git-tracked, committed, and pushed" (`packaged/guide/decision-guide.md`) — so committing while the file still sits in `preparing/` is not the path the guide walks you down. It is, however, exactly what an operator does when a batch of phase task files is authored ahead of time and committed for review before any of them runs, which is how the eight tasks below were prepared (`4e0d3f8`, one commit, all eight files). And it is the same operator behavior the current code already anticipates one folder over: the comment on the existing `pending` entry says "committed to base by hand".

### Steps

1. `worc install` (default layout, so `paths.tasks_dir: tasks` and the four lifecycle folders are tracked).
2. Author `tasks/preparing/<id>.md`, **commit it** on the base branch, push.
3. `worc promote <id>` — atomic rename to `tasks/pending/<id>.md`. Nothing is staged; `git status` now shows ` D tasks/preparing/<id>.md` plus an untracked `tasks/pending/<id>.md`.
4. `worc run tasks/pending/<id>.md`, and let it reach a terminal `done`.
5. Inspect the result:
   - `git show --name-status <audit-sha> -- tasks/` → two additions under `tasks/done/`, no deletion.
   - `git ls-tree --name-only <branch> -- tasks/preparing/` → the file is still there, alongside its `tasks/done/` copy.
   - `git status --porcelain` → ` D tasks/preparing/<id>.md` on the base branch that terminal cleanup checked out.

Repeat for a second task on the same branch and the dirty tree from step 5 is the starting state of the next run's branch preparation.

### Orchestrator settings

Verified reproducing under the settings on the left; the rest of the config is listed because it is *not* implicated, so nobody has to re-run the matrix to find that out.

| Setting | Value in the reproducing run | Relevance |
| --- | --- | --- |
| `paths.tasks_dir` | `tasks` (default) | **Load-bearing** — the pathspec is built from it. A custom dir reproduces the same way as long as its `preparing/` subfolder is tracked. |
| `git.footprint.audit_on_branch` | `task` | **Load-bearing for the observed shape.** The audit commit rides the task branch, so the duplicate is visible on that branch and in the PR. `sibling` puts the audit commit on `<branch>-audit`; the staging pathspec is the same code path, so the miss should be identical — *not verified*. |
| `git.footprint.audit_commit_message` | `chore(orchestrator): audit trail for {task_id}` | Cosmetic; only how the commits below are named. |
| task front matter `branch_mode` | `new` (task 1), `existing` (tasks 2–8) | **Not implicated** — reproduced identically on both. |
| terminal status | `done` (8/8) | `done` is what was observed. `failed` is the same code path with `tasks/failed/` as the destination, so a `preparing → failed` run should miss the same way — *not verified*. |
| `git.create_pull_request` / `pr_base` / `auto_merge` | `true` / `main` / `false` | **Not implicated.** The duplicate and the dangling `D` are created by the audit commit; publishing only decides who sees them. |
| flow / `task_type` | `implementation` (packaged), `output_policy: code_change`, `publishing: pull_request` | **Not implicated** — `commit_audit` runs for any flow that reaches a terminal state. |
| `checks.command_sets.default` | `npm run lint`, `npm run build` | Not implicated. |
| `agents.providers` | claude `claude-opus-5` primary, codex `gpt-5.5` review | Not implicated. |
| `security.strict_isolation` / `disable_read_isolation` / `allow_git_evidence` | `false` / `true` / `true` | **Not implicated** — flagged explicitly because an advanced-mode config invites the assumption that a sandbox escape wrote the tree. It did not: the missing deletion is in `commit_audit`'s own pathspec. |
| `logging.artifacts` / `logging.level` / `clean_runs_on_success` | `full` / `debug` / `false` | Not implicated; this is only why the evidence below survived the run. |
| `orchestrator.auto_mode.enabled` | `false` (tasks launched one at a time by the operator) | **Not implicated, but it set the blast radius.** An operator between tasks restored the deleted path each time. Under `watch` nobody does. |

Orchestrator version: `wastech-orchestrator 0.12.0a1` (pipx). The pathspec tuple in this repository's `src/` is byte-identical to the installed one, so the defect is current on `main`, not an artifact of an old install.

## Evidence and logs

The run that surfaced this is on the operator's machine, in a **private** repository — so there are no public links to point at; everything below is a local path or a git ref you resolve inside that clone.

```
repo   /Users/a1234/Documents/GitHub/wastechlab-mobile-template   (private)
branch worc/003-004-storage-startup-and-compliance                (base: main)
run    2026-09-10, 01:30-05:19 local, 8 tasks 003-02 … 004-05, all done,
       one reused PR (#9), zero retries, zero HITL prompts
```

The eight task files were committed **while in `tasks/preparing/`** by `4e0d3f8` — one commit, all eight files, before any of them ran. That commit is the precondition.

Three commands inside that clone reproduce the whole finding:

```bash
# 1. no commit in the run ever deleted anything from the staging folder -> empty output
git log --oneline --diff-filter=D main..worc/003-004-storage-startup-and-compliance -- tasks/preparing/

# 2. the eight task files are still in preparing/ on the branch head -> 8
git ls-tree --name-only worc/003-004-storage-startup-and-compliance -- tasks/preparing/ | wc -l

# 3. an audit commit's tasks/ footprint: two additions, no deletion
git show --name-status 43b4652 -- tasks/
```

The eight audit commits on that branch, each adding `tasks/done/<id>.md` + `<id>.summary.md` and deleting nothing:

| Task | Audit commit | Code commit |
| --- | --- | --- |
| `003-02-fetchitems-soft-delete-contract` | `43b4652` | `2816971` |
| `003-03-startup-watchdog-lifetime` | `f014c31` | `c281d47` |
| `003-04-verification-and-docs` | `5e65eca` | `4784bb2` |
| `004-01-english-comments-sweep` | `2cfb6a7` | `8d83085` |
| `004-02-todo-and-commented-code` | `555e739` | `58aa3bc` |
| `004-03-delete-dead-files` | `399dc2e` | `7f6c638` |
| `004-04-rename-greeting-timer` | `020d54d` | `a8ec120` |
| `004-05-docs-english-and-verification` | `daa6866` | `024028f` |

### What is verified, and what is not

Verified on the pushed branch, once per task, eight times out of eight:

- Every audit commit's `tasks/` footprint is two additions and nothing else. The eight `feat` code commits touch `tasks/` not at all.
- No commit in the run deleted anything from `tasks/preparing/`.
- The branch head carries all eight task files twice — eight paths under `tasks/preparing/`, sixteen under `tasks/done/` (`<id>.md` + `<id>.summary.md`).
- After each task went terminal, the working tree held ` D tasks/preparing/<id>.md` on the branch cleanup had checked out.

**Not observed**, and deliberately not claimed: whether `prepare_branch` would abort on that dirty tree or carry the deletion forward silently. The operator restored the path before every next task, so the run never entered a branch preparation with the deletion still pending. Only the dangling `D` itself is verified. This is the open question a fix should settle, and it is what decides the severity under `watch`, where no operator is between two tasks.

**Inferred from reading the code, not observed:** `commit_merge_resolution` stages the whole tree (`git add -A -- :/ …`), so a dangling `D tasks/preparing/<id>.md` still present when a base merge is finalized would be swept into that merge commit — the deletion would land, attributed to an unrelated merge resolution rather than to the task that caused it.

### Run logs

`.worc/` is gitignored, so none of this is in git history — it exists only in that working copy, and it is the part worth copying out before it is cleaned (`logging.clean_runs_on_success: false` is why it all survived the run). Paths are relative to `/Users/a1234/Documents/GitHub/wastechlab-mobile-template/`:

| Path | What it holds |
| --- | --- |
| `.worc/logs/<task-id>/` | Per-task run artifacts for all eight ids: `stages/` (per-node transcripts: `planning`, `implementation`, `testing`, `review`, `fixing`, `documentation`, `publish`, `supervisor`), `checks/NNN.log`, `current.diff`, `plan.md`, `summary.md`, `summary.json`, `task.normalized.json`, `prompt-audit/`. |
| `.worc/logs/<task-id>/publish/terminal-cleanup.json` | **The most direct log evidence.** For every task it records `{"target_branch": "main", "completed": true, "safe": true, "error": null}` — terminal cleanup checked out the base branch and reported itself *safe* while leaving ` D tasks/preparing/<id>.md` behind in it. That is the failure mode the two earlier fixes were written against, still open for this folder, and the cleanup's own safety verdict does not see it. |
| `.worc/logs/completed.jsonl` | One terminal record per task. |
| `.worc/runs/control-bundles/<task-id>/` | Frozen control-plane + instruction bundles per task. |
| `.worc/overnight/<task-id>.log` | Full stdout/stderr of each `worc run` (operator's wrapper, `--log-level debug`), including the `msg=terminal` line with `final_status=done pr_url=… cleanup_safe=true`. |
| `.worc/overnight/watch-<task-id>.txt` | Operator's `worc status` polling trace per task (node-by-node progression with timestamps). |
| `.worc/overnight-report.md` | The operator's run report: per-task start/finish, terminal state, commit sha, lint/build result, PR link, and the section describing this defect as it was hit and worked around eight times. |
| `.worc/overnight/final-verify.txt` | Post-run whole-branch verification: `npm run lint` clean, `npm run build` green, `npm run test:ci` **942/942**. Included to establish that the eight tasks' *code* is sound and this item is purely about the orchestrator's git bookkeeping. |

### How it presented to the operator

Once per task, immediately after the task went terminal, on whichever branch cleanup had checked out:

```
$ git status --short --branch
## main...origin/main
 D tasks/preparing/003-02-fetchitems-soft-delete-contract.md
```

The operator restored that single path (`git checkout -- tasks/preparing/<id>.md`, working tree only — no rebase, reset or force-push) before launching the next task, eight times. That is the whole workaround, and it is only available to a human sitting between two tasks.

## This is the third time the same enumeration came up short

The pathspec has already been widened twice, each time by appending one more source state, each time after the same symptom was found in production:

1. `failed → done` — the task-023 regression ([`tests/git/test_git_manager.py` → `test_audit_commit_stages_lifecycle_move_deletion`](../../tests/git/test_git_manager.py)).
2. `pending → failed/done` — the ion-list regression, whose test comment records the shape of the miss: *"the prior fix covered only failed->done, not pending->failed/done"* ([`test_audit_commit_stages_pending_to_failed_move_deletion`](../../tests/git/test_git_manager.py)).
3. `preparing → done` — this item. The last tracked lifecycle folder still missing from the list.

That is the argument for not fixing it by appending a fourth string: the list is a hand-maintained restatement of `REPO_TASK_DIRS`, and it has drifted from it three times.

## Proposed minimal design

Derive the audit pathspec's source states from the tracked lifecycle directories rather than restating them. Every folder in `REPO_TASK_DIRS` is a possible base-tracked source for a lifecycle move, and every one of them belongs in the pathspec; `tasks/rejected` correctly stays out because it lives under `.worc/` and is not tracked at all.

The staging semantics need no change — `git add -A` over the candidate pathspecs already stages adds and deletes, and the existing `stageable` filter (path exists on disk **or** is tracked) already keeps an unmatched pathspec from aborting the add. Widening the list is sufficient; nothing about the guards has to be relaxed:

- `assert_staged_allowed(set(stageable))` keeps its contract — the new path is a member of `stageable`, so "only this task's lifecycle files may be in the index" still holds.
- `_assert_lifecycle_matches_packet` is unaffected: it only ever digests the *destination* states (`done`, `failed`), and a source path being staged for deletion has no content to verify.

A one-line alternative (append `"preparing"` to the tuple) fixes the observed defect and would be a defensible hotfix, but it leaves the drift that produced all three regressions in place.

## Touch points

| Path | Change |
| --- | --- |
| [`src/wastech_orchestrator/git_manager.py`](../../src/wastech_orchestrator/git_manager.py) | `commit_audit`: source states for the audit pathspec derived from the tracked lifecycle dirs instead of the hardcoded `("done", "failed", "pending")`; update the comment that asserts `pending` is *the* source state. |
| [`src/wastech_orchestrator/cli.py`](../../src/wastech_orchestrator/cli.py) | Only if the derivation reads `REPO_TASK_DIRS` — it is the install-time default layout, while the runtime honors `config.paths.tasks_dir`, so the lifecycle *state names* must come across without the hardcoded `tasks/` prefix. |

## Tests

- A `preparing → done` regression test beside the two existing ones in [`tests/git/test_git_manager.py`](../../tests/git/test_git_manager.py), in their shape: commit `tasks/preparing/<id>.md` to base, move it to `done/` on disk, `commit_audit`, then assert the old path is gone from `ls-files`, the new path is tracked, and `status --porcelain` is free of the dangling `D`.
- The `preparing → failed` counterpart, so a failed run is covered on the same axis.
- A guard against the drift itself: assert that every tracked lifecycle state is present in the audit pathspec, so adding a fifth folder to `REPO_TASK_DIRS` without teaching `commit_audit` about it fails a test rather than a production run.

## Constraints

- Not a `promote` change. Making `promote` stage or commit the rename would put the orchestrator's hands on the operator's index before a run is authorized; the staging area is deliberately inert (`src.replace`, one syscall, no partial-write window).
- The audit commit must keep staging **only this task's** lifecycle files — never the whole `tasks/` tree — so a concurrently pending task is never swept in.
- `tasks/rejected` stays out of the pathspec: it is the quarantine, lives under `.worc/`, and is never committed.
- Existing behavior for the two already-covered moves must not regress; this widens the source set, it does not redefine the destinations.
