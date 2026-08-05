# Cookbook

This cookbook shows common ways to use **wastech-orchestrator**. It is written for operators who run the orchestrator and for developers who want a practical path from "empty workspace" to "task processed into a Pull Request".

The canonical product reference remains the code and [worc_architecture.md](worc_architecture.md). The full CLI surface (`install`, `preflight`, `validate-flow`, `telegram-test`, `run`, `promote`, `rerun`, `finalize`, `prs`, `merge-task`, `watch`, `stop`, `restart`, `status`, `top`, `shell`, `list`, `tasks`, `completion`, `clear`, `logs`, `runs`, `memory`, `upgrade-config`, `upgrade-docs`) exists in the current codebase; this guide focuses on the everyday subset.

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
worc install .                 # interactive wizard (same on macOS)
```

```bash
# non-interactive, e.g. for CI/automation:
worc install . --non-interactive --provider codex --no-create-pr
```

Everything the orchestrator generates lives under a single gitignored `<repo>/.worc/` home: `config.yaml`, the agent task-authoring `guide/` (the packaged `worc/` docs copied to `.worc/guide/`), the editable `flows/` copies (built-in flows + their per-flow prompt dirs), the `tools/` copies (executables the packaged `tool` nodes resolve against), SQLite `state.db` (with `-wal`/`-shm`), `orchestrator.pid`, `logs/`, `runs/` (per-task frozen bundles + sealed exchanges), `memory/`, and `workspace/`, plus the `tasks/rejected` quarantine. (Check logs are not a top-level directory — they live under `logs/<task-id>/checks/`.) `install` appends two lines — `.worc/` and the sibling gitignored `.worc-io/` exchange — to the repo's tracked `.gitignore`. The only things kept tracked outside `.worc/`/`.worc-io/` are the `tasks/` lifecycle dirs (`pending/`/`done/`/`failed/`) at the repo root — they are intentionally git-tracked, and the task file plus its `<id>.summary.md` (in `done/` or `failed/`) are the audit trail the orchestrator commits.

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

This fresh-branch behavior is `branch_mode: new` — the default, and the only mode in which the orchestrator owns (and may reset/delete) the branch. Set `repo.branch_mode` (or a per-task `branch_mode`) to `existing` to work in a named already-existing branch (`branch_ref`), or `current` to work in the current checkout as-is — see [task-authoring.md](task-authoring.md#branch_mode). A per-task `publish: commit|push|pull_request` caps how far publishing goes for one task (e.g. stop at a local commit) without switching flows.

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
env: OK - loaded 2 variable(s) from .worc/.env
claude: OK - claude 2.1.210 available (version=2.1.210, authenticated=True)
codex: OK - codex 0.144.4 available (version=0.144.4, authenticated=True)
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

On Windows use `where codex`, `where claude`, and `where gh`. WSL, PowerShell, the IDE extension, and a global npm installation can resolve different Codex executables and report different versions. Configure `agents.providers.codex.command` for the executable visible to the actual orchestrator process; do not infer it from a different terminal. If `codex --version` and `worc preflight` disagree, follow [How-To §5](how-to.md#5-fix-conflicting-codex-installations-on-windows) to pin the intended native executable.

Only providers named in `agents.allowed` are required. If Claude Code is not installed, use Codex-only routing for every agent-driven stage. If `gh` is not installed, set `git.create_pull_request: false` until GitHub CLI is installed and authenticated.

## 4. Create A First Task

Create `tasks/preparing/task-001.md`, then promote it into the queue with `worc promote task-001`:

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

A successful task runs through validation, branch preparation, optional refinement, planning, implementation, checks (testing), review, fixing if needed, the whole-task `documentation` pass, then publishing — commit, push, and PR creation — followed by terminal cleanup back to `repo.base_branch`. The plain-language summary that becomes the PR body is **not** a separate stage: the supervisor layer writes it at task close (see [configuration.md](configuration.md#supervisor)) — and with that layer switched off, the deterministic report is the body instead.

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
worc rerun task-001 --continue --reset-fix-budget --yes   # fix loop exhausted: grant a fresh fix budget
worc rerun task-001 --continue --from implementation --yes # re-enter at a chosen node
```

Use **fresh** (default) for a quality failure or a clean redo (the branch is reset to base and prior `.worc/logs/<id>/` is archived to `.worc/logs/<id>/attempt-<N>/`); use **`--continue`** when you fixed an environment/infra problem by hand (a missing tool, `PATH`, a dropped Telegram approval) and want to pick up where it stopped — on `--continue` the task's own uncommitted work is tolerated once it reached review/fixing/publish. Add **`--reset-fix-budget`** when the fix loop was exhausted (`max_fix_cycles`) — it resets the consecutive fix counters while keeping the global `max_total_fix_iterations` backstop, so termination stays bounded; if you resume into an already-exhausted budget without passing `--reset-fix-budget`/`--no-reset-fix-budget`, `rerun` asks interactively (never skipped by `--yes`). Add **`--from <node>`** to re-enter at a chosen node of the checkpoint's flow — it resumes through any flow drift since the checkpoint (the node just needs to exist today). Each re-attempt appends a ledger record linked to the prior one. See [operations.md](operations.md) "Re-attempting a terminal task" for the full rules.

### Record a task you handled by hand (`finalize`)

If you resolved a terminal task **yourself** (merged the PR, fixed it locally, or dropped it), `finalize` reconciles the bookkeeping — it records and tidies only, never running the pipeline or committing/pushing/PR-ing (daemon must be stopped):

```bash
worc finalize task-001 --as done --pr-url https://github.com/o/r/pull/42  # you merged it
worc finalize task-001 --as failed --note "superseded"                    # give up on it
worc finalize task-001 --as abandoned --note "obsolete"                   # drop it (audited)
```

It sets the terminal status, runs terminal cleanup (returns the working copy to `base_branch`, fail-closed on a dirty tree — unless the branch mode / `repo.checkout_base_on_cleanup` keeps it on the working branch), moves the task file, closes any waiting HITL prompt, and appends a `manual` ledger record. See [operations.md](operations.md) "Finalize a task you handled by hand" for the full rules.

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

Auto mode does not introduce concurrency. There is a single active task slot, and terminal cleanup (checkout back to `repo.base_branch`, or staying on the working branch per `repo.checkout_base_on_cleanup` / the branch mode) must complete before the next task can start.

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

`worc top` is stdlib-only (no extra). For an interactive console that also drives commands — `enqueue` a task, `ps`, `logs`, `down` the daemon — install the `[shell]` extra and run `worc shell`. Entry is passive: it attaches to a live daemon or opens idle (the queue is not served until you type `up`). `up` spawns the daemon and verifies it came up (surfacing the real error if it does not); `quit` detaches and leaves the daemon running:

```bash
pip install wastech-orchestrator[shell]
worc shell                                       # attach-or-idle; up / enqueue / promote / ps / clear / down / quit
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

To add repository-specific engineering rules or a review rubric to a stage without editing Python, edit that node's **`role_file`** (see [configuration.md](configuration.md#prompt-templates-no-longer-a-config-block)). `install` delivers the built-in flows + their role files under `.worc/flows/` (each flow's prompts in its own `<task_type>/` subdir), and `.worc/flows/` is the only copy the orchestrator reads, so edit the delivered role file (a custom operator flow likewise keeps its role files under its own `.worc/flows/<task_type>/` subdir). The role file's content **is** the prompt template — edit it and the change takes effect on the next run.

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
- The exact text sent each run is saved (redacted) to `.worc/logs/<task-id>/stages/<node-id>/run-<node-run-id>/rendered-prompt.md` — one per node run, so a re-running node keeps every pass.
- A template is prompt text only: it cannot change the provider, sandbox/approvals, denied commands, or enable `git`/`gh` publishing.

## 7b. Run A Different Flow (`deep_research`)

A task selects its pipeline with one front-matter field. Nothing else changes — same engine, same gates, same supervisor layer:

```markdown
---
id: task-042
title: "How does provider fallback actually decide?"
task_type: deep_research
---
```

`deep_research` is worth knowing in outline because its shape differs from the coding flow in three ways an operator notices:

- **The repository analysis is three sequential passes, not one**, each with a fresh context window and a narrow mandatory remit (`analysis_core` → `analysis_surfaces` → `analysis_docs_tests`). One node asked to walk everything self-triages: it goes deep on what it recognises and labels the rest "no findings". The remit is what forces even depth. For a narrow question, disable the passes you do not need **per task** (`nodes.analysis_surfaces: { enabled: false }`) rather than editing the graph.
- **A `coverage_gate` measures the audit instead of reading it.** Every subsystem the task declares must show a traced property, not a bare "no findings" label; its rework edge re-enters at `analysis_core`, because a gap can sit in any of the three remits. That catches a thin pass before `synthesis` writes a conclusion on top of it.
- **Two `checks` nodes run before the expensive evaluators**: the deterministic `citation` checker over the sources manifest (its report records the `manifest_path` it validated, so the fact-verification evaluator can open the same file), and a `command_profile` node running _your_ `checks.command_sets` over the Markdown about to be committed. That second one costs nothing until you configure a matching set — and once you do, it changes how the flow can fail (see the catch-all note in §8).

`refinement` here is a **scoping** pass and runs on every task: it decomposes the question into sub-questions and anchors each to where its evidence lives. (It used to be gated on `derived.needs_refinement`, which is only true for an ill-formed task file — so on any well-formed task the scoping pass never ran. A complete task file and a scoped question are different things.) Skip it per task when the question is narrow enough.

**What the deliverable directory contains.** `deep_research` declares `output_policy: repository_document`, so every write is confined to `docs/research/<task-id>/` and the bundle must produce `report.md` + `sources.json`. The organizing pass before the writer (`architecture_design`) deliberately writes **no file** — it returns its blueprint as its output and the writer consumes it. An intermediate blueprint written into the report directory ships in the pull request next to the deliverable, and the run that proved this shipped two documents that disagreed about coverage. So the report directory holds the deliverable and nothing else.

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

Two things about that catch-all worth knowing before you rely on it:

- **It fires on a Markdown-only diff too.** A no-`paths` set runs on _any_ non-empty diff, so once you also run a document-producing flow (`deep_research`, or the content/blog flows), that research run pays for the whole code gate — and a command that **rewrites** files rather than checking them parks the run on the green-but-dirtying guard. Either scope the catch-all's `paths` to code, or keep it and add a documents set running a format **check**:

  ```yaml
  checks:
    command_sets:
      code:
        paths: ["src/**", "tests/**", "pyproject.toml"] # scoped, not catch-all
        commands:
          - { name: lint, argv: ["ruff", "check", "."] }
      docs:
        paths: ["**/*.md"]
        commands: # a CHECK, never a formatter
          - { name: prose, argv: ["npx", "prettier@3", "--check", "**/*.md"] }
  ```

- **`skip_if_unavailable` is not an escape hatch.** It converts a launch failure into a loud skip, nothing more — and a set that was the _only_ one the diff selected and is then skipped leaves the gate with nothing run, which parks the task at `manual_action_required` exactly where the launch failure would have. When you actually want a gate not to run, disable the node per task: `nodes.testing: { enabled: false }`.

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
  completed.jsonl                 # the ledger: one record per terminal task
  daemon.log                      # the watch daemon's live stream (+ rotated backups)
  daemon-startup.log              # only the daemon's pre-configuration output
  <task-id>/
    validation_report.json
    task.normalized.json
    task.enriched.md
    plan.md
    current.diff
    checks/
    hitl/
    memory/
    stages/<node-id>/
      history.jsonl               # one line per run of this node
      run-<node-run-id>/          # every re-run kept: rendered-prompt.md, findings.json, <id>.out.md, …
    summary.md / summary.json
    failure_report.json
    stuck.md
    publish/
```

Use `completed.jsonl` as the index of terminal tasks. Use `stuck.md` and `failure_report.json` when the result is `manual_action_required`. Provider stdout/stderr/event files are redacted; the full process environment and secrets must not be stored.

Note that a node's per-run artifacts are **not** at a fixed path — an evaluator's findings live under `stages/<node-id>/run-<node-run-id>/findings.json`, one directory per run, so a `review → fixing → testing → review` loop keeps every pass instead of clobbering the last. Read `stages/<node-id>/history.jsonl` for the chronological index.

Per-task **runtime** state (frozen control + instruction bundles, sealed exchanges) is a separate tree under `.worc/runs/`, cleaned automatically for a successful task and on demand with `worc runs clean`; the log tree above is cleaned with `worc logs clean` (which also sweeps the daemon logs).

Do not live-tail provider `stdout.log` or `stderr.log`: provider output is finalized and redacted after the subprocess exits. Use the operator log and `status` while work is in progress, then inspect provider artifacts after the attempt completes.

## 11. Recover From `manual_action_required`

`manual_action_required` means the orchestrator stopped safely and needs a human decision. Common causes:

| Cause | Action |
| --- | --- |
| Fix budget exhausted | Read `stuck.md`, inspect the final diff, and decide whether to fix manually or refine the task. |
| Checks gate incomplete (required toolchain absent, or every selected set skipped) | Install the toolchain, narrow the selecting `paths`, or disable that checks node for the task. A fix loop cannot install toolchains, so this never routes to `fixing`. |
| Terminal cleanup unsafe | Inspect `repo.local_path` with `git status`, reconcile the branch, and return to `repo.base_branch`. |
| More than one active task on restart | Decide which task is authoritative, then repair state before rerunning. |
| Git control state drifted across an attempt (WRI-009) | Inspect `git status` plus the repo-local config/hooks, reconcile, then `rerun --continue`. On a `read-only` node holding the git-evidence grant this only warns and continues. |

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
      permission_profile: "workspace-write" # the access level — there is no `sandbox` key here
      extra_args: []
      primary: true # the global primary (exactly one; must be in agents.allowed)
```

`sandbox: read-only` / `sandbox: workspace-write` is **rejected at config load** — the access level lives on `permission_profile`, and `sandbox` now only accepts the full-access escape `danger-full-access` (`upgrade-config` folds a legacy value across for you).

Run without the oversight layer (cheapest possible run; the PR body becomes the deterministic report):

```yaml
supervisor:
  enabled: false
memory:
  enabled: false # otherwise it is forced false for the run, with a warning
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
