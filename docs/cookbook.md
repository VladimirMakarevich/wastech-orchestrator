# Cookbook

This cookbook shows common ways to use **wastech-orchestrator**. It is written for operators who
run the orchestrator and for developers who want a practical path from "empty workspace" to "task
processed into a Pull Request".

The canonical product contract remains
[00_orchestrator_final_plan.md](implementation_stages/00_orchestrator_final_plan.md).
Where this guide mentions planned v1 behavior, it is labeled explicitly. The CLI surface described
here (`init`, `install`, `preflight`, `run`, `watch`, and `status`) exists in the current codebase.

## 1. Initialize A Workspace

Install the package in an environment that has Python 3.12+, git, and the provider CLIs you plan to
use (`codex`, `claude`) on `PATH`.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

On Windows PowerShell, activate the environment with:

```powershell
.\.venv\Scripts\Activate.ps1
```

Create the runtime layout:

```bash
python -m wastech_orchestrator init .
```

This creates `config.yaml`, `tasks/`, `logs/`, `workspace/`, and editable copies of task and prompt
templates under `templates/`. The command is idempotent: a second run skips existing files and never
overwrites `config.yaml`.

The generated task template is `templates/task.md`. Under the default in-repo audit footprint, live
task files belong in the repo's `tasks/pending/` (committed and pushed there). Under the
`external`/`exclude_local` footprints, copy examples into the external workspace's `tasks/pending/`
instead and do not commit them under a target repo's `tasks/`/`logs/` paths — there the footprint
preflight treats tracked paths with those names as a collision.

Choose a footprint mode at initialization when you already know where artifacts should live (the
default is `in_repo_commit`):

```bash
python -m wastech_orchestrator init . --git-mode in_repo_commit   # default: tasks + artifacts in the repo
python -m wastech_orchestrator init . --git-mode in_repo_exclude
python -m wastech_orchestrator init . --git-mode external
```

Use `--dry-run` to inspect the created/skipped plan without writing files.

### Bind An Existing Repository Instead (`install`)

If you already have the target repository checked out, skip `init` + hand-editing and use `install`,
which detects settings, generates a validated `config.yaml` in a sibling control workspace, and
records a binding so later commands need no `--config`:

```powershell
pipx install "git+https://github.com/VladimirMakarevich/wastech-orchestrator.git"
cd C:\projects\my-repo
wastech-orchestrator install .                 # interactive wizard (same on macOS)
```

```bash
# non-interactive, e.g. for CI/automation:
wastech-orchestrator install . --non-interactive --provider codex --no-create-pr
```

`install` binds the current checkout as `repo.local_path` and keeps `config.yaml`, `tasks/`, `logs/`,
and SQLite state only in the `<repo-name>-orchestrator` sibling — it never modifies the target repo.
Re-running is idempotent; `--reconfigure` backs up and regenerates; `--dry-run` writes nothing. See
[configuration.md](configuration.md) for the discovery order and [operations.md](operations.md) for
the full wizard. The remaining recipes also apply to an `install`-bound project — its commands just
run from inside the repo without `--config`.

## 2. Configure A Target Repository

Edit `config.yaml` and point the `repo` block at the repository the orchestrator should modify:

```yaml
repo:
  url: "git@github.com:OWNER/REPO.git"
  local_path: "./workspace/repo"
  base_branch: "main"
  branch_prefix: "agent"
```

The branch name produced by the Git Manager follows:

```text
agent/<task-id>-<slug>
```

Credentials are configured outside the orchestrator:

- authenticate git and `gh` in the shell environment that runs the orchestrator;
- authenticate Codex through the Codex CLI;
- authenticate Claude Code through the Claude CLI;
- do not put token values in `config.yaml`, task files, or `extra_args`.

For all configuration fields, see [configuration.md](configuration.md).

### Self-host the orchestrator repository

Do not point `repo.local_path` at the source checkout open in your IDE. Use one directory to run a
known-good orchestrator build and a separate clone as the target repository:

```text
wastech-self/
  .venv/                 # control environment running the known-good orchestrator
  config.yaml
  tasks/
  logs/
  workspace/
    repo/                 # separate clone modified by coding agents
```

Recommended preparation:

1. Create the control directory outside the source checkout and run `init` there with the external
   footprint.
2. Clone `wastech-orchestrator` into `workspace/repo`.
3. Confirm the target clone is clean and on `main`.
4. Confirm `git ls-files -- tasks logs` prints nothing. Runtime task files belong to the control
   directory, not the target clone.
5. Configure the real Python checks: `ruff check .`, `mypy src`, and `pytest`.
6. Keep `orchestrator.auto_mode.enabled: false` for the first run.
7. Use a unique task id and a small, fully specified task before attempting a larger backlog item.

Run the first experiment against a disposable fork or test remote. `create_pull_request: false`
skips `gh pr create`, but the current publishing pipeline still commits and pushes the task branch.
There is no no-push dry-run mode yet.

## 3. Run Preflight

Before processing tasks, run:

```bash
python -m wastech_orchestrator preflight
```

Preflight checks each allowed provider and the configured isolation policy. A healthy run looks like:

```text
claude: OK - claude 1.2.3 available (version=1.2.3, authenticated=True)
codex: OK - codex 0.9.0 available (version=0.9.0, authenticated=True)
isolation: OK (enforced)
preflight: ready
```

If preflight fails, fix the reported environment or configuration problem before running tasks. Do
not work around failures by weakening sandbox or approval settings; the validator rejects known
unsafe `extra_args`.

Run these diagnostics in the same shell that will start the orchestrator:

```bash
command -v codex
codex --version
codex exec --help
command -v claude
command -v gh
```

On Windows use `where codex`, `where claude`, and `where gh`. WSL, PowerShell, the IDE extension,
and a global npm installation can resolve different Codex executables and report different
versions. Configure `agents.providers.codex.command` for the executable visible to the actual
orchestrator process; do not infer it from a different terminal.

Only providers named in `agents.allowed` are required. If Claude Code is not installed, use
Codex-only routing for every agent-driven stage. If `gh` is not installed, set
`git.create_pull_request: false` until GitHub CLI is installed and authenticated.

## 4. Create A First Task

Create `tasks/pending/task-001.md`:

```markdown
---
id: task-001
title: "Add login form validation"
refined: false
decompose: false
---

## Description

Add client-side validation to the login form. Show a validation error when the email is missing,
when the email format is invalid, or when the password is empty.

## Acceptance criteria

- [ ] Empty email shows a validation error.
- [ ] Invalid email format shows a validation error.
- [ ] Empty password shows a validation error.
- [ ] Existing login behavior still works for valid input.

## Constraints

- Do not touch billing code.
- Add or update focused tests for the login form.
```

The validation gate requires front matter, a valid `id`, a non-empty `title`, and a non-empty
Description section. Acceptance criteria are not a structural reject, but in the current
implementation they make the task complete enough to skip autonomous refinement when
`refined: true` is not set. Constraints are still strongly recommended because they keep the
implementation scope clear.

Task authoring details are in [task-authoring.md](task-authoring.md).

## 5. Run One Task

Process a single task end to end:

```bash
python -m wastech_orchestrator run tasks/pending/task-001.md
```

Useful variants:

```bash
python -m wastech_orchestrator --config ./config.yaml run tasks/pending/task-001.md
python -m wastech_orchestrator --log-level debug run tasks/pending/task-001.md
python -m wastech_orchestrator --log-file ./logs/orchestrator.log run tasks/pending/task-001.md
```

Exit codes:

| Exit | Meaning |
|---:|---|
| `0` | The task reached `done`. |
| `1` | The task reached `failed`. |
| `2` | The task reached `manual_action_required`. |

Planned v1 behavior: a successful task runs through validation, preparation, optional refinement,
planning, implementation, checks, review, fixing if needed, summary, commit, push, PR creation, and
terminal cleanup back to `repo.base_branch`.

### Re-attempt a task that ended `failed` / `manual_action_required`

A terminal task is frozen — `watch`/`resume` never pick it up again. `rerun` re-attempts it (stop the
`watch` daemon first; it needs an idle slot):

```bash
worc rerun task-001 --dry-run        # show the plan; write nothing
worc rerun task-001 --yes            # fresh attempt from the current base_branch
worc rerun task-001 --continue --yes # infra failure you fixed: reuse the branch, re-enter at the failed stage
```

Use **fresh** (default) for a quality failure or a clean redo (the branch is reset to base and prior
`logs/<id>/` is archived to `logs/<id>/attempt-<N>/`); use **`--continue`** when you fixed an
environment/infra problem by hand (a missing tool, `PATH`, a dropped Telegram approval) and want to
pick up where it stopped. Each re-attempt appends a ledger record linked to the prior one. See
[operations.md](operations.md) "Re-attempting a terminal task" for the full rules.

### Record a task you handled by hand (`finalize`)

If you resolved a terminal task **yourself** (merged the PR, fixed it locally, or dropped it),
`finalize` reconciles the bookkeeping — it records and tidies only, never running the pipeline or
committing/pushing/PR-ing (daemon must be stopped):

```bash
worc finalize task-001 --as done --pr-url https://github.com/o/r/pull/42  # you merged it
worc finalize task-001 --as failed --note "superseded"                    # give up on it
worc finalize task-001 --as abandoned --note "obsolete"                   # drop it (audited)
```

It sets the terminal status, returns the working copy to `base_branch` (fail-closed on a dirty tree),
moves the task file, closes any waiting HITL prompt, and appends a `manual` ledger record. See
[operations.md](operations.md) "Finalize a task you handled by hand" for the full rules.

## 6. Use `watch`

`watch` resumes an interrupted task first, then processes pending task files:

```bash
python -m wastech_orchestrator watch
python -m wastech_orchestrator watch --poll-seconds 0   # single pass (no loop), e.g. under cron
```

By default `watch` is a long-running loop: `orchestrator.poll_interval_seconds` (default `300`) is
how often it runs `git fetch` + `pull --ff-only` on `base_branch` and re-scans, so tasks committed
and pushed to the repo after `watch` started are picked up without a manual pull. Stop it with
Ctrl-C; set `poll_interval_seconds: 0` (or `--poll-seconds 0`) for a single pass.

By default, `orchestrator.auto_mode.enabled` is `false`, so within each scan `watch` processes or
resumes one task and then waits for the next tick:

```yaml
orchestrator:
  auto_mode:
    enabled: false
```

Enable sequential queue processing only when you want the orchestrator to pick the next pending task
after a successful terminal cleanup:

```yaml
orchestrator:
  auto_mode:
    enabled: true
```

Auto mode does not introduce concurrency. Planned v1 behavior keeps a single active task slot and
requires checkout back to `repo.base_branch` before the next task can start.

### Monitor a running task

For a long run, write the safe operator trace to a rotating file:

```bash
python -m wastech_orchestrator \
  --log-file ./logs/orchestrator.jsonl \
  --log-format json \
  --heartbeat-seconds 30 \
  watch
```

Global options must appear before `run`, `watch`, `preflight`, or `status`. The file rotates at
10 MB and keeps five backups. Use `--log-format logfmt` for a human-readable `key=value` file, or
`json` for ingestion by tools. `--heartbeat-seconds 30` emits safe progress records while a
provider, check, or Git command is still running; set it to `0` to disable heartbeats.

Follow the live trace from another terminal:

```bash
tail -f logs/orchestrator.jsonl
```

The trace includes start/completion/failure events and durations for stages, provider attempts,
checks, branch preparation, commit, push, PR creation, and terminal cleanup. Heartbeats contain
only safe metadata such as task, stage, provider, attempt, timeout, and elapsed time.

Read the persisted task snapshot without starting providers, checks, or Git operations:

```bash
python -m wastech_orchestrator --config ./config.yaml status
python -m wastech_orchestrator --config ./config.yaml status task-001
```

Without a task id, `status` shows active tasks or the most recently updated task. It reports the
persisted status, current stage when applicable, configured primary provider, branch, subtask,
fix counter, last update time, and elapsed time since that update. It opens `state.db` read-only.

## 7. Override Providers Per Stage

Global routing lives in `config.yaml` under `agents.routing`. A task may override only the provider
for known agent stages, and only to a provider listed in `agents.allowed`.

```yaml
---
id: task-002
title: "Update API pagination"
agents:
  planning: codex
  implementation: claude
  review: codex
---
```

Allowed stage keys are:

```text
refinement, planning, implementation, review, fixing, summary
```

`testing` and `publishing` are not provider-routed stages. They are executed by the Check Runner and
Git Manager.

Task overrides cannot change provider commands, credentials, sandbox settings, `extra_args`, or any
security policy.

## 7a. Customize Stage Prompts

To add repository-specific engineering rules or a review rubric to a stage without editing Python,
use the optional `prompts:` block (see [configuration.md](configuration.md#prompts)). `init` already
scaffolds `templates/prompts/` with the packaged defaults; edit a copy and point an override at it.

Add house implementation rules on top of the default instruction:

```yaml
# config.yaml
prompts:
  mode: append            # keep the packaged default, then add your text
  overrides:
    implementation: "implementation.md"
```

```markdown
<!-- templates/prompts/implementation.md -->
Follow the repository conventions:
- keep functions under 40 lines; extract helpers otherwise;
- never add a runtime dependency without a note in the summary;
- match the logging style in {repo_path}.
```

Replace the review prompt entirely with a security rubric:

```yaml
prompts:
  mode: replace           # your template is the whole review prompt
  overrides:
    review: "review.md"
```

Notes:

- Variables are metadata/paths only — e.g. `{repo_path}`, `{diff_path}`, `{plan_path}`. Large
  content stays in the artifact files the agent reads by path. Unknown `{...}` and literal braces
  pass through unchanged.
- `strict: true` turns a missing override file into a startup error; the default `false` logs a
  warning and falls back to the packaged default.
- The exact text sent each run is saved (redacted) to
  `logs/<task-id>/stages/<stage>/rendered-prompt.md` so you can verify what the agent received.
- A template is prompt text only: it cannot change the provider, sandbox/approvals, denied
  commands, or enable `git`/`gh` publishing.

## 8. Configure Checks

Checks are configured or discovered — never hardcoded. The Check Runner runs each resolved command
with a timeout and records output as artifacts; a launched check that exits non-zero is a quality
failure that goes to `fixing` (no provider fallback), while a check that **cannot be launched** stops
the task before any branch and never burns a fixing iteration.

**Zero-config onboarding.** `install` detects the repository's ecosystem and writes
`checks.discovery.mode: auto` when it can't pin explicit commands. On the next `preflight`/run the
orchestrator discovers the launchable profile — for a Python repo with a local virtualenv it resolves
`.venv/bin/python -m pytest` (and ruff/mypy) even when `pytest` is not on `PATH`:

```bash
python -m wastech_orchestrator install        # detects ecosystem, writes mode: auto
python -m wastech_orchestrator preflight       # reports the resolved, launchable checks
```

```yaml
checks:
  discovery:
    mode: auto
  commands: []           # discovery resolves a launchable profile; preflight shows it
  timeout_seconds: 7200
```

**Explicit override.** A non-empty `commands` list is authoritative in any mode. Use legacy strings,
structured `{name, argv}`, or both:

```yaml
checks:
  discovery:
    mode: configured
  commands:
    - "ruff check ."
    - name: tests
      argv: [".venv/bin/python", "-m", "pytest"]
```

See [configuration.md](configuration.md#checks) for every field and
[operations.md](operations.md#check-discovery-diagnostics) for the `preflight`/`status` diagnostics.

## 9. Choose A Git Footprint Mode

The footprint controls where `tasks/` and `logs/` live relative to the target clone.

| Mode | Config | Best for |
|---|---|---|
| `in_repo_commit` (default) | `location: in_repo`, `tracking: commit` | The task + its summary stored in git, in the same repo as the code, via a separate orchestrator-made `tasks/` commit (`logs/` stays local). |
| `in_repo_exclude` | `location: in_repo`, `tracking: exclude_local` | Artifacts beside the code, excluded through `.git/info/exclude`. |
| `external` | `location: external`, `tracking: none` | Zero artifact footprint in the target repo. |

Examples:

```yaml
git:
  footprint:
    location: in_repo
    tracking: commit
```

```yaml
git:
  footprint:
    location: external
    tracking: none
    external_root: "./"
```

In every mode the *code* commit uses scoped staging and excludes `tasks/`/`logs/`/`workspace/` (and,
under in-repo, the root runtime files `state.db`/`config.yaml`); audit mode stores **`tasks/`** (the
task moved to `done/`/`failed/` + its `<id>.summary.md`) in a *separate* commit, while `logs/`
(plan, review, diffs, `summary.json`) stays local and is never committed.

## 10. Inspect Logs And Artifacts

The `--log-file` operator trace is the best live view. Start artifact inspection with:

```bash
ls logs
```

The most useful files are:

```text
logs/
  completed.jsonl
  <task-id>/
    validation_report.json
    task.normalized.json
    task.enriched.md
    plan.md
    current.diff
    review/findings.json
    checks/
    stages/
    summary.md
    failure_report.json
    stuck.md
    publish/
```

Use `completed.jsonl` as the index of terminal tasks. Use `stuck.md` and `failure_report.json` when
the result is `manual_action_required`. Provider stdout/stderr/event files are redacted; the full
process environment and secrets must not be stored.

Do not live-tail provider `stdout.log` or `stderr.log`: provider output is finalized and redacted
after the subprocess exits. Use the operator log and `status` while work is in progress, then inspect
provider artifacts after the attempt completes.

## 11. Recover From `manual_action_required`

`manual_action_required` means the orchestrator stopped safely and needs a human decision. Common
causes:

| Cause | Action |
|---|---|
| Fix budget exhausted | Read `stuck.md`, inspect the final diff, and decide whether to fix manually or refine the task. |
| Terminal cleanup unsafe | Inspect `repo.local_path` with `git status`, reconcile the branch, and return to `repo.base_branch`. |
| More than one active task on restart | Decide which task is authoritative, then repair state before rerunning. |
| Footprint conflict | Only under `external`/`exclude_local`: remove or rename tracked `tasks/`/`logs/` paths (the preflight rejects that collision). Under the default `in_repo_commit` those paths are the expected audit trail and the preflight is skipped. |

After resolving the problem, run:

```bash
python -m wastech_orchestrator watch
```

Planned v1 behavior is idempotent: reruns reconcile state and do not duplicate commit, push, or PR
operations.

## 12. Common Setups

Codex-only:

```yaml
agents:
  allowed:
    - codex
  routing:
    refinement: {primary: codex, fallback: null}
    planning: {primary: codex, fallback: null}
    implementation: {primary: codex, fallback: null}
    review: {primary: codex, fallback: null}
    fixing: {primary: codex, fallback: null}
    summary: {primary: codex, fallback: null}
  providers:
    codex:
      command: "codex"
      model: ""
      timeout_seconds: 7200
      sandbox: "workspace-write"
      permission_profile: "workspace-write"
      extra_args: []
```

No automatic PR creation:

```yaml
git:
  create_pull_request: false
  pr_base: "main"
```

This disables PR creation only. A successful run still commits and pushes the task branch.

Long-running checks:

```yaml
checks:
  commands:
    - "npm test"
    - "npm run lint"
    - "npm run typecheck"
  timeout_seconds: 3600
```

Smaller fix budget for conservative runs:

```yaml
agents:
  max_stage_attempts: 3
  max_fix_cycles: 15
  max_total_fix_iterations: 30
```
