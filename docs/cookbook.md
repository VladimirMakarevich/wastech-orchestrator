# Cookbook

This cookbook shows common ways to use **wastech-orchestrator**. It is written for operators who run the orchestrator and for developers who want a practical path from "empty workspace" to "task processed into a Pull Request".

The canonical product reference remains the [Functional Map](functional/index.md). The full CLI surface (`install`, `preflight`, `telegram-test`, `run`, `rerun`, `finalize`, `prs`, `merge-task`, `watch`, `stop`, `restart`, `status`, `top`, `shell`, `list`, `tasks`, `completion`, `logs`, `upgrade-config`, `upgrade-docs`) exists in the current codebase; this guide focuses on the everyday subset.

## 1. Install Into A Repository

Install the package in an environment that has Python 3.12+, git, and the provider CLIs you plan to use (`codex`, `claude`) on `PATH`.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

On Windows PowerShell, activate the environment with:

```powershell
.\.venv\Scripts\Activate.ps1
```

`install` is the single setup command. Run it from inside the target repository checkout — it detects settings, generates a validated `config.yaml`, scaffolds the runtime layout, and needs no later `--config`:

```powershell
pipx install "git+https://github.com/VladimirMakarevich/wastech-orchestrator.git"
cd C:\projects\my-repo
wastech-orchestrator install .                 # interactive wizard (same on macOS)
```

```bash
# non-interactive, e.g. for CI/automation:
wastech-orchestrator install . --non-interactive --provider codex --no-create-pr
```

Everything the orchestrator generates lives under a single gitignored `<repo>/.worc/` home: `config.yaml`, the agent task-authoring `guide/` (the packaged `worc/` docs copied to `.worc/guide/`), the editable `flows/` copies (built-in flows + their per-flow prompt dirs), SQLite `state.db` (with `-wal`/`-shm`), `orchestrator.pid`, `logs/`, and `workspace/`, plus the `tasks/rejected` quarantine. (Check logs are not a top-level directory — they live under `logs/<task-id>/checks/`.) `install` appends a single `.worc/` line to the repo's tracked `.gitignore`. The only things kept outside `.worc/` are the `tasks/` lifecycle dirs (`pending/`/`done/`/`failed/`) at the repo root — they are intentionally git-tracked, and the task file plus its `<id>.summary.md` (in `done/` or `failed/`) are the audit trail the orchestrator commits.

Re-running is idempotent (existing files are skipped and `config.yaml` is never overwritten); `--reconfigure` backs up and regenerates; `--dry-run` writes nothing. See [configuration.md](configuration.md) for the discovery order and [operations.md](operations.md) for the full wizard. The remaining recipes all run from inside the repo without `--config`.

## 2. Configure A Target Repository

Edit `config.yaml` and point the `repo` block at the repository the orchestrator should modify:

```yaml
repo:
  url: "git@github.com:OWNER/REPO.git"
  local_path: "./workspace/repo"
  base_branch: "main"
  branch_prefix: "worc"
```

The default branch name produced by the Git Manager follows:

```text
worc/<epoch>-<task-id>-<slug>
```

`<epoch>` is the unix timestamp captured when the branch is prepared, so re-submitting the same task never collides with a leftover branch. The full auto-generated name is capped at 50 characters: the slug is truncated to whatever fits (or dropped entirely if the prefix already fills the budget). A task may set `branch_name` to override the full branch name when a project or customer requires a different convention; an override longer than 50 characters logs a warning and falls back to the auto-generated name.

Credentials are configured outside the orchestrator:

- authenticate git and `gh` in the shell environment that runs the orchestrator;
- authenticate Codex through the Codex CLI;
- authenticate Claude Code through the Claude CLI;
- do not put token values in `config.yaml`, task files, or `extra_args`.

For all configuration fields, see [configuration.md](configuration.md).

### Self-host the orchestrator repository

Do not point `repo.local_path` at the source checkout open in your IDE. Use one checkout to run a known-good orchestrator build and a separate clone as the target repository:

```text
wastech-self/
  .venv/                 # control environment running the known-good orchestrator
  .worc/                 # orchestrator home (config.yaml, logs/, state.db, workspace/, ...)
  tasks/                 # git-tracked task lifecycle dirs at the repo root
  workspace/
    repo/                 # separate clone modified by coding agents
```

Recommended preparation:

1. Create the control checkout outside the source checkout and run `install .` there.
2. Clone `wastech-orchestrator` into the target clone configured in `repo.local_path`.
3. Confirm the target clone is clean and on `main`.
4. Confirm the target clone keeps no orchestrator artifacts of its own — the runtime home lives under the control checkout's `.worc/`.
5. Configure the real Python checks: `ruff check .`, `mypy src`, and `pytest`.
6. Keep `orchestrator.auto_mode.enabled: false` for the first run.
7. Use a unique task id and a small, fully specified task before attempting a larger backlog item.

Run the first experiment against a disposable fork or test remote. `create_pull_request: false` skips `gh pr create`, but the current publishing pipeline still commits and pushes the task branch. There is no no-push dry-run mode yet.

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

If preflight fails, fix the reported environment or configuration problem before running tasks. Do not work around failures by weakening sandbox or approval settings; the validator rejects known unsafe `extra_args`.

Run these diagnostics in the same shell that will start the orchestrator:

```bash
command -v codex
codex --version
codex exec --help
command -v claude
command -v gh
```

On Windows use `where codex`, `where claude`, and `where gh`. WSL, PowerShell, the IDE extension, and a global npm installation can resolve different Codex executables and report different versions. Configure `agents.providers.codex.command` for the executable visible to the actual orchestrator process; do not infer it from a different terminal.

Only providers named in `agents.allowed` are required. If Claude Code is not installed, use Codex-only routing for every agent-driven stage. If `gh` is not installed, set `git.create_pull_request: false` until GitHub CLI is installed and authenticated.

## 4. Create A First Task

Create `tasks/pending/task-001.md`:

```markdown
---
id: task-001
title: "Add login form validation"
---

## Description

Add client-side validation to the login form. Show a validation error when the email is missing, when the email format is invalid, or when the password is empty.

## Acceptance criteria

- [ ] Empty email shows a validation error.
- [ ] Invalid email format shows a validation error.
- [ ] Empty password shows a validation error.
- [ ] Existing login behavior still works for valid input.

## Constraints

- Do not touch billing code.
- Add or update focused tests for the login form.
```

The validation gate requires front matter, a valid `id`, a non-empty `title`, and a non-empty Description section. Acceptance criteria are not a structural reject, but they make the task complete enough to skip autonomous refinement (refinement-skip is deterministic — driven by completeness, with no task flag). Constraints are still strongly recommended because they keep the implementation scope clear.

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

| Exit | Meaning                                    |
| ---: | ------------------------------------------ |
|  `0` | The task reached `done`.                   |
|  `1` | The task reached `failed`.                 |
|  `2` | The task reached `manual_action_required`. |

A successful task runs through validation, branch preparation, optional refinement, planning, implementation, checks (testing), review, fixing if needed, then publishing — commit, push, and PR creation — followed by terminal cleanup back to `repo.base_branch`. The plain-language summary that becomes the PR body is **not** a separate stage: the constant supervisor layer writes it at task close (see [configuration.md](configuration.md#supervisor)).

### Find a task to act on (`list`) + Tab-completion

Before `rerun`/`finalize`/`status` you need the task id. `worc list` shows it without hand-scanning folders, and the completion script lets you Tab through ids instead of typing them:

```bash
worc list                              # active + pending queue + recent terminal tasks
worc list --format ids --scope rerun   # bare ids a rerun would accept
source <(worc completion zsh)          # then: worc rerun <Tab> completes rerun-eligible ids
```

### Re-attempt a task that ended `failed` / `manual_action_required`

A terminal task is frozen — `watch`/`resume` never pick it up again. `rerun` re-attempts it (stop the `watch` daemon first; it needs an idle slot):

```bash
worc rerun task-001 --dry-run        # show the plan; write nothing
worc rerun task-001 --yes            # fresh attempt from the current base_branch
worc rerun task-001 --continue --yes # infra failure you fixed: reuse the branch, re-enter at the failed stage
```

Use **fresh** (default) for a quality failure or a clean redo (the branch is reset to base and prior `.worc/logs/<id>/` is archived to `.worc/logs/<id>/attempt-<N>/`); use **`--continue`** when you fixed an environment/infra problem by hand (a missing tool, `PATH`, a dropped Telegram approval) and want to pick up where it stopped. Each re-attempt appends a ledger record linked to the prior one. See [operations.md](operations.md) "Re-attempting a terminal task" for the full rules.

### Record a task you handled by hand (`finalize`)

If you resolved a terminal task **yourself** (merged the PR, fixed it locally, or dropped it), `finalize` reconciles the bookkeeping — it records and tidies only, never running the pipeline or committing/pushing/PR-ing (daemon must be stopped):

```bash
worc finalize task-001 --as done --pr-url https://github.com/o/r/pull/42  # you merged it
worc finalize task-001 --as failed --note "superseded"                    # give up on it
worc finalize task-001 --as abandoned --note "obsolete"                   # drop it (audited)
```

It sets the terminal status, returns the working copy to `base_branch` (fail-closed on a dirty tree), moves the task file, closes any waiting HITL prompt, and appends a `manual` ledger record. See [operations.md](operations.md) "Finalize a task you handled by hand" for the full rules.

### Merge a reviewed PR (`prs` / `merge-task`)

With auto-merge off (the default), the orchestrator leaves its PR open for you to review on GitHub. After you approve it, let the orchestrator finish — pull base in, resolve any conflicts, and merge (daemon must be stopped):

```bash
worc prs                                 # which orchestrator PRs are open and awaiting merge?
worc merge-task task-001 --dry-run       # preview: PR, whether base merges cleanly
worc merge-task task-001                 # go-ahead: update branch w/ base, resolve conflicts, merge
worc prs --sync --yes                    # record PRs you merged directly on GitHub instead
```

A clean base-merge is mechanical; a conflicting one runs the operator-editable `merge` flow (`git.merge_flow`) — an agent resolves the markers, the checks re-run, then the orchestrator commits and merges. On any failure it aborts the merge and leaves the PR open. See [operations.md](operations.md) "Merging a reviewed PR" for flags and the safety-only gate.

## 6. Use `watch`

`watch` resumes an interrupted task first, then processes pending task files:

```bash
python -m wastech_orchestrator watch
python -m wastech_orchestrator watch --poll-seconds 0   # single pass (no loop), e.g. under cron
```

By default `watch` is a long-running loop: `orchestrator.poll_interval_seconds` (default `300`) is how often it runs `git fetch` + `pull --ff-only` on `base_branch` and re-scans, so tasks committed and pushed to the repo after `watch` started are picked up without a manual pull. Stop it with Ctrl-C; set `poll_interval_seconds: 0` (or `--poll-seconds 0`) for a single pass.

By default, `orchestrator.auto_mode.enabled` is `false`, so within each scan `watch` processes or resumes one task and then waits for the next tick:

```yaml
orchestrator:
  auto_mode:
    enabled: false
```

Enable sequential queue processing only when you want the orchestrator to pick the next pending task after a successful terminal cleanup:

```yaml
orchestrator:
  auto_mode:
    enabled: true
```

Auto mode does not introduce concurrency. There is a single active task slot, and checkout back to `repo.base_branch` must complete before the next task can start.

### Monitor a running task

For a long run, write the safe operator trace to a rotating file:

```bash
python -m wastech_orchestrator \
  --log-file ./logs/orchestrator.jsonl \
  --log-format json \
  --heartbeat-seconds 30 \
  watch
```

Global options must appear before `run`, `watch`, `preflight`, or `status`. The file rotates at 10 MB and keeps five backups. Use `--log-format logfmt` for a human-readable `key=value` file, or `json` for ingestion by tools. `--heartbeat-seconds 30` emits safe progress records while a provider, check, or Git command is still running; set it to `0` to disable heartbeats.

Follow the live trace from another terminal:

```bash
tail -f logs/orchestrator.jsonl
```

The trace includes start/completion/failure events and durations for stages, provider attempts, checks, branch preparation, commit, push, PR creation, and terminal cleanup. Heartbeats contain only safe metadata such as task, stage, provider, attempt, timeout, and elapsed time.

Read the persisted task snapshot without starting providers, checks, or Git operations:

```bash
python -m wastech_orchestrator --config ./config.yaml status
python -m wastech_orchestrator --config ./config.yaml status task-001
```

Without a task id, `status` shows active tasks or the most recently updated task. It reports the persisted status, current stage when applicable, configured primary provider, branch, subtask, fix counter, last update time, and elapsed time since that update. It opens `state.db` read-only.

For a single attended surface instead of re-running `status`, use `worc top` — a live, read-only monitor that auto-refreshes the active task + flow node, a parked/gate-pending marker, the queue (filtered to the served queue and priority-sorted, exactly as the daemon runs it), recent terminal tasks, and a tail of the daemon log. Point it at the daemon's log file and quit with `q`:

```bash
worc watch --log-file ./logs/daemon.jsonl &      # daemon writes its log here
worc top --log-file ./logs/daemon.jsonl          # live monitor; q (then Enter) to quit
```

`worc top` is stdlib-only (no extra). For an interactive console that also drives commands — `enqueue` a task, `ps`, `logs`, `down` the daemon — install the `[shell]` extra and run `worc shell`; it spawns or attaches to the daemon and streams its log above a prompt:

```bash
pip install wastech-orchestrator[shell]
worc shell                                       # spawns/attaches the daemon; enqueue / ps / down / quit
```

## 7. Choose Which Provider Runs a Node

Provider routing is **node-based** — it lives on the flow, not the task. Each agent/evaluator node in the flow YAML may declare its own `provider:` (`codex` | `claude`); a node with no `provider` runs on the **global primary** (the one `config.yaml` provider marked `primary: true`, which must be in `agents.allowed`). The global primary is also the sole infrastructure-fallback target.

```yaml
# in an operator flow (.worc/flows/<task_type>.yaml) or a packaged flow node:
- id: review
  kind: evaluator
  role: review
  role_file: implementation/review.md
  provider: codex # this node runs on codex; omit to use the global primary
```

A node's declared `provider`/`model`/`reasoning` are the **defaults**; a **task** may overlay them per run with `nodes.<node-id>.{model,reasoning,provider}` (best-effort — an invalid value is warned and skipped at run time, never fatal; see [task-authoring.md](task-authoring.md#provider-model-reasoning)). This lets one default flow cover several model/effort/provider variants without a separate flow file. `testing` and `publishing` run no agent (Check Runner / Git Manager). A `provider` (node-declared or task-overridden) must be in `agents.allowed`, and neither the flow nor a task can change provider commands, credentials, sandbox settings, `extra_args`, or any security policy.

## 7a. Customize a Node's Prompt

To add repository-specific engineering rules or a review rubric to a stage without editing Python, edit that node's **`role_file`** (see [configuration.md](configuration.md#prompt-templates-no-longer-a-config-block)). `install` delivers the built-in flows + their role files under `.worc/flows/` (each flow's prompts in its own `<task_type>/` subdir), and those copies override the packaged built-ins, so edit the delivered role file (a custom operator flow likewise keeps its role files under its own `.worc/flows/<task_type>/` subdir). The role file's content **is** the prompt template — edit it and the change takes effect on the next run.

For example, a review node's role file replaced with a security rubric:

```markdown
<!-- implementation/review.md -->

Review for security first. Reject the change unless:

- all new inputs are validated and outputs encoded;
- no secret, token, or credential appears in the diff at {diff_path};
- the plan at {plan_path} is fully implemented.
```

Notes:

- Variables are metadata/paths only — e.g. `{repo_path}`, `{diff_path}`, `{plan_path}`. Large content stays in the artifact files the agent reads by path. Unknown `{...}` and literal braces pass through unchanged.
- The exact text sent each run is saved (redacted) to `.worc/logs/<task-id>/stages/<node-id>/rendered-prompt.md` so you can verify what the agent received.
- A template is prompt text only: it cannot change the provider, sandbox/approvals, denied commands, or enable `git`/`gh` publishing.

## 8. Configure Checks

Checks are **operator-authored command sets** — never auto-detected. The Check Runner runs each selected command (argv list, no shell) with a timeout and records output as artifacts; a launched check that exits non-zero is a quality failure that goes to `fixing` (no provider fallback), while a **required toolchain that cannot launch** leaves the gate incomplete and sends the task to `manual_action_required` (the agent cannot install host toolchains). `install` writes `command_sets: {}` (no gate); you author the gate.

**Single-root repo.** One set with no `paths` always runs (on any non-empty diff):

```yaml
checks:
  timeout_seconds: 7200 # global per-command default
  command_sets:
    repo:
      paths: [] # no paths ⇒ always runs
      commands:
        - { name: lint, argv: ["ruff", "check", "."] }
        - { name: types, argv: ["mypy", "src"] }
        - { name: tests, argv: [".venv/bin/python", "-m", "pytest"] }
```

**Polyglot monorepo.** Sets are selected by the **union** of those whose `paths` match the task diff; a `cwd` runs the command in a subtree, and `skip_if_unavailable` skips (rather than fails) a set whose toolchain is absent off-host:

```yaml
checks:
  command_sets:
    backend:
      paths: ["backend/**"]
      commands:
        - { name: tests, argv: ["pytest"], cwd: "backend" }
    docs:
      paths: ["**/*.md"] # extension-anywhere: only when Markdown changes
      commands:
        - { name: prose, argv: ["npx", "prettier@3", "--check", "**/*.md"] }
    ios:
      paths: ["ios/**"]
      skip_if_unavailable: true # xcodebuild absent off-macOS ⇒ skip, don't block
      commands:
        - { name: build, argv: ["xcodebuild", "build"], cwd: "ios" }
```

An **empty diff** runs nothing (the checks node passes vacuously); a changed path claimed by **no** set runs no set on its account (cover shared/root files with a no-`paths` catch-all set). A skipped `skip_if_unavailable` set is recorded loudly and **blocks `git.auto_merge`** even when the node passes. See [configuration.md](configuration.md#checks) for every field and [operations.md](operations.md#command-set-diagnostics) for the `preflight`/`status` command-set summary.

## 9. Configure The Audit Commit

There is one canonical layout: the orchestrator's runtime home is the gitignored `<repo>/.worc/` (config, `logs/`, `state.db`, `workspace/`, ...), and the `tasks/` lifecycle dirs live git-tracked at the repo root. The _code_ commit uses scoped staging and excludes `.worc/`; a separate task-scoped **audit commit** records `tasks/` — the task file moved to `done/`/`failed/` plus its `<id>.summary.md`. Everything under `.worc/` (plan, review, diffs, `summary.json`) stays local and is never committed.

`git.footprint` has two settings:

```yaml
git:
  footprint:
    audit_commit_message: "chore: archive task {task_id}"
    audit_on_branch: task # task (default) | sibling
```

`audit_on_branch` controls where the audit commit lands:

| Value | Best for |
| --- | --- |
| `task` (default) | The task + its summary committed onto the task branch, beside the code change. |
| `sibling` | The audit commit goes onto a separate `<branch>-audit` branch, keeping the task branch limited to the code change. |

The audit commit exists to answer a different question than the code commit:

- the code commit says **what changed in the source tree**;
- the audit commit says **which task was completed and what its outcome was**.

Typical history for a successful task:

```text
feat(task-123): add rate limiting
chore(orchestrator): audit trail for task-123
```

The first commit carries the source diff. The second carries only the task trail under `tasks/`: the moved `task-123.md` plus `task-123.summary.md`. Everything under `.worc/` stays local and is never committed.

## 10. Inspect Logs And Artifacts

The `--log-file` operator trace is the best live view. Start artifact inspection with:

```bash
ls .worc/logs
```

The most useful files are:

```text
.worc/logs/
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

Use `completed.jsonl` as the index of terminal tasks. Use `stuck.md` and `failure_report.json` when the result is `manual_action_required`. Provider stdout/stderr/event files are redacted; the full process environment and secrets must not be stored.

Do not live-tail provider `stdout.log` or `stderr.log`: provider output is finalized and redacted after the subprocess exits. Use the operator log and `status` while work is in progress, then inspect provider artifacts after the attempt completes.

## 11. Recover From `manual_action_required`

`manual_action_required` means the orchestrator stopped safely and needs a human decision. Common causes:

| Cause | Action |
| --- | --- |
| Fix budget exhausted | Read `stuck.md`, inspect the final diff, and decide whether to fix manually or refine the task. |
| Terminal cleanup unsafe | Inspect `repo.local_path` with `git status`, reconcile the branch, and return to `repo.base_branch`. |
| More than one active task on restart | Decide which task is authoritative, then repair state before rerunning. |

After resolving the problem, run:

```bash
python -m wastech_orchestrator watch
```

Recovery is idempotent: reruns reconcile state and do not duplicate commit, push, or PR operations.

## 12. Common Setups

Codex-only:

```yaml
agents:
  allowed:
    - codex
  providers:
    codex:
      command: "codex"
      model: ""
      timeout_seconds: 7200
      sandbox: "workspace-write"
      permission_profile: "workspace-write"
      extra_args: []
      primary: true # the global primary (exactly one; must be in agents.allowed)
```

No automatic PR creation:

```yaml
git:
  create_pull_request: false
  pr_base: "main"
```

This disables PR creation only. A successful run still commits and pushes the task branch.

Long-running checks (a per-set `timeout_seconds` overrides the global default):

```yaml
checks:
  command_sets:
    repo:
      timeout_seconds: 3600
      commands:
        - { name: tests, argv: ["npm", "test"] }
        - { name: lint, argv: ["npm", "run", "lint"] }
        - { name: types, argv: ["npm", "run", "typecheck"] }
```

Smaller fix budget for conservative runs:

```yaml
agents:
  max_stage_attempts: 3
  max_fix_cycles: 15
  max_total_fix_iterations: 30
```
