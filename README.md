# WORC → Wastech Orchestrator

**A lean orchestrator that turns a written task into a reviewed Pull Request** — using external coding agents (**OpenAI Codex CLI** and **Anthropic Claude Code CLI**) to do the editing, while the orchestrator owns the process and the Git lifecycle.

You drop a task file into a repo; the orchestrator parses it, creates a branch, and drives a **deterministic flow** — a validated graph of typed steps (refine → plan → implement → test → review → fix → publish for the default flow) — routing each node to a coding agent behind a provider abstraction (with automatic fallback to the other agent on infrastructure errors). A constant, **read-only supervisor** watches every step and writes the plain-language summary that becomes the PR body. It runs your project's checks, commits the change, pushes, and opens a PR. The agents only edit files inside a dedicated clone; **only the orchestrator commits, pushes, and creates PRs.** Every step is checkpointed to SQLite, so a crash resumes from the last completed node and publishing is idempotent.

> **Status: 0.x pre-release.** The flow engine and its packaged flows (`implementation`, `deep_research`, `security_audit`), node-based provider routing + infrastructure fallback, the constant advisory supervisor layer, the security/isolation gate, the scoped Git audit commit, SQLite checkpoints + crash recovery, the watch loop with periodic Git sync, and the `install` setup flow are implemented and covered by an extensive test suite. Telegram notifications and human-in-the-loop for clarification and deletion/dependency approvals are implemented; parallel `git worktree` execution remains on the roadmap. APIs and config may still change before 1.0.

---

## Why use it

- **Tasks in, PRs out.** Author a task in Markdown; get a branch, the change, your checks run, and a PR with a written summary — no babysitting.
- **Two agents, one interface.** Codex and Claude Code are interchangeable per flow node — a node runs on its declared `provider`, else the global primary. If a node fails for an _infrastructure_ reason (binary missing, timeout, rate limit), the orchestrator falls back to the global primary; test/review failures instead loop through a bounded `fix` stage.
- **The orchestrator owns Git.** Agents never commit or push. Branch naming, scoped staging, commit, push, PR, and the safe return to the base branch are all the orchestrator's job.
- **Tasks and results live in the repo.** The task file and its summary are committed to the same repository as the code (the audit trail under the repo-root `tasks/`); everything else the orchestrator generates lives under a single gitignored `<repo>/.worc/` home and never enters Git history.
- **Discovers work pushed to Git.** `watch` runs as a loop that periodically `fetch`/`pull`s the base branch, so a teammate can hand the orchestrator a task simply by committing it to `tasks/pending/` and pushing — no manual sync needed.
- **Crash-safe and idempotent.** State machine + per-stage checkpoints in SQLite; a restart resumes the one in-flight task and never double-commits, double-pushes, or re-opens a PR.
- **Optional Telegram HITL.** `refinement`/`planning` can ask one correlated question or approval, and deletions/dependency changes are fail-closed before tests. Waiting state is a recoverable artifact; ordinary diffs and routine publishing remain automatic.
- **Security can't be weakened by a task.** The sandbox/approval policy and the environment allowlist are config-level invariants; no task or `extra_args` can disable them, and no secrets are written to logs, SQLite, or artifacts.

---

## How it works

```text
tasks/pending/task-001.md
        │  watch loop (periodic git fetch/pull) picks it up
        ▼
   ┌──────────────────────────── deterministic flow ───────────────────────────┐
   │ git fetch/pull → checkout base → create task branch                       │
   │   refine → plan → implement → test → review → fix(loop) → publish         │
   │   (supervisor watches every step; writes <id>.summary.md at close)        │
   │   → move task → tasks/done/  →  commit (code) + commit (task+summary)     │
   │   → push  →  gh pr create   (PR body = the supervisor summary)            │
   │   → return to base branch  →  fetch/pull refresh                          │
   └───────────────────────────────────────────────────────────────────────────┘
        │  auto_mode? → next pending task   |   otherwise idle (keep polling)
```

- **One task at a time.** A single processing slot; other tasks wait in `tasks/pending/`. Auto mode (off by default) controls whether the next pending task starts automatically after cleanup.
- **One canonical layout.** The task lifecycle dirs (`tasks/preparing|pending|done|failed`) sit at the repo root and are git-tracked; everything else — `config.yaml`, `state.db`, `logs/`, and the installed guide bundle (task docs, task skills, config helper) — lives under the gitignored `<repo>/.worc/` home. See [Configuration](#configuration).
- The detailed, code-derived reference (state machine, routing, recovery, security, the audit footprint) is the [Functional Map](docs/functional/index.md); the design rationale is in [docs/worc_architecture.md](docs/worc_architecture.md). The canonical project vocabulary (commands, config keys, flow nodes, statuses, artifacts, legacy terms) is in [docs/glossary.md](docs/glossary.md).

---

## Requirements

- **Python 3.12+** and **git**.
- The agent CLIs you intend to route to on `PATH`: **`codex`** and/or **`claude`**.
- **GitHub CLI (`gh`)** — only if you want PRs opened automatically (`git.create_pull_request: true`).
- For the interactive console (`worc shell`): install the optional extra — `pip install wastech-orchestrator[shell]`. `worc top` and every other command need nothing extra; the daemon never imports it.

The orchestrator **never installs or authorizes** the CLIs and never stores credentials. Authorize git push for your remote, `gh auth login`, and sign in to `codex` / `claude` yourself, once, in the environment the orchestrator runs in. Only allowlisted environment variables are passed to child processes. See [docs/operations.md §2](docs/operations.md).

---

## Quick start

Bind an existing repository and let the orchestrator process a task end to end.

```bash
# 1. Install the CLI (isolated, recommended)
pipx install "git+https://github.com/VladimirMakarevich/wastech-orchestrator.git"

# 2. Set up your repo: generates a validated config.yaml under <repo>/.worc/.
#    The task & its summary are committed to this repo; config, state.db, logs/, the
#    installed guide bundle (`guide/` task docs + task skills + config helper), and editable copies of the
#    built-in flows + node prompts all live under the gitignored .worc/ home, leaving tracked
#    files clean.
cd /path/to/my-repo
worc install .          # interactive wizard (detects origin, branch, agents, checks)

# 3. Confirm the agents and isolation policy are ready (read-only).
worc preflight
```

Author a task in the repo's `tasks/preparing/` staging directory, then promote it into the queue:

```markdown
---
id: task-001
title: "Add email validation to the signup form"
# optional: task_type: deep_research | branch_name: feature/ABC-123-email-validation
# optional: auto_merge: true | nodes: { review: { enabled: false } }
---

## Description

The signup form accepts any string as an email. Validate the `email` field and show a clear error for malformed addresses.

## Acceptance criteria

- Malformed emails are rejected with a user-facing message.
- A unit test covers valid and invalid cases.
```

> Only `id` and `title` are required; the gate **rejects unknown fields**. The full allow-list (`id`, `title`, `task_type`, `branch_name`, `auto_merge`, `prompt_audit`, `decomposition`, `contacts`, `depends_on`, `priority`, `queue`, `subtasks`, `nodes`) is in [docs/task-authoring.md](docs/task-authoring.md). Provider/model/reasoning **default** to the flow node; a task may overlay them per node (best-effort) via the `nodes` block, but it names a flow and never patches the graph.

Then run it:

```bash
# Process exactly one task, end to end:
worc run tasks/pending/task-001.md

# …or run the watch loop: it processes pending tasks and periodically fetch/pulls the base branch
# so tasks pushed to Git later are picked up automatically (Ctrl-C to stop).
worc watch

# Inspect progress / the latest persisted task at any time:
worc status
```

The orchestrator creates `worc/task-001-...` by default (or the task's validated `branch_name`), runs the pipeline and your checks, makes a scoped code commit plus a separate audit commit for the task file and its summary, pushes, and (with `gh` present) opens a PR whose body is the summary. A failed attempt is also committed and pushed for inspection — without opening a PR.

---

## Configuration

`install` writes a validated `config.yaml` under `<repo>/.worc/` (seeded from `config.example.yaml`). The full reference (every field, default, and validation rule) is [docs/configuration.md](docs/configuration.md). The knobs you'll touch most:

| Setting | What it controls |
| --- | --- |
| `repo.url` / `repo.local_path` / `repo.base_branch` | The target repository and the branch PRs target. |
| `git.footprint` | The audit trail: `audit_commit_message` (the message for the orchestrator's task+summary commit) and `audit_on_branch` (`task` — keep that audit commit on the task branch, the default; `sibling` — put it on an `…-audit` branch). |
| `orchestrator.auto_mode.enabled` | Process the next pending task automatically after cleanup (default `false`). |
| `orchestrator.poll_interval_seconds` | `watch` tick: fetch/pull + re-scan interval (default `300`; `0` = single pass). |
| `agents.allowed` / `agents.providers.<id>.primary` | Which providers are enabled, and which one is the global primary (runs any flow node with no `provider`, and is the sole infrastructure-fallback target). Per-node routing lives on the flow, not in config. |
| `checks.command_sets` | Operator-authored, diff-selected test/lint commands (argv list, no shell) run as the `testing` stage. Empty (`{}`) = no gate. |
| `supervisor` | The constant read-only supervisor layer that watches every step and writes the PR summary (model/reasoning/role_file). |
| `git.create_pull_request` | Open a PR after push (needs `gh`); disabling it does not disable commit/push. |
| `telegram.*` | Optional terminal notifications and blocking HITL; credentials stay in environment variables. |
| `security.*` | Strict isolation, the environment allowlist, denied paths/commands — invariants a task cannot weaken. |

Config discovery order: explicit `--config` → `<repo-root>/.worc/config.yaml` (found by walking up from the cwd to the Git root) → a hint to run `install .`.

Secrets (e.g. the Telegram token/chat id) are read from the environment. Keep them in `<repo>/.worc/.env` (auto-loaded at startup, gitignored; `install` drops a `.worc/.env.example` to copy) or `export` them — an exported variable always wins over the file. Override the path with `--env-file PATH`. See [docs/operations.md §2](docs/operations.md).

---

## Commands

```text
worc install [repo]     set up the orchestrator under <repo>/.worc/: generate config + guide + flows, gitignore .worc/
                          --non-interactive --provider codex|claude|both|auto --no-create-pr --reconfigure
worc preflight          check both CLIs' health + the isolation policy (read-only)
worc telegram-test      send a real correlated Telegram prompt and wait for reply
                          --timeout-seconds N       smoke-test deadline (default: 60)
worc run <task-file>    process exactly one task end to end
worc rerun <task-id>    re-attempt a terminal (failed/manual) task; daemon must be idle
                          --continue                  reuse the branch + re-enter at the failed stage
                          --force-reset-remote        delete the prior remote branch (closes its PR)
                          --dry-run  --yes            preview the plan / skip the confirmation
worc finalize <task-id> record + tidy a task you handled by hand (no pipeline/commit/PR)
                          --as done|failed|abandoned  the operator-declared terminal outcome (required)
                          --pr-url URL  --note TEXT    merged-PR URL / ledger note (for --as done)
                          --delete-branch  --no-verify-pr  --dry-run  --yes
worc prs                open, un-merged orchestrator PRs awaiting merge (read-only)
                          --check                     enrich each row with live GitHub state (gh)
                          --sync [--yes]              record PRs merged externally (dry-run unless --yes)
worc merge-task <id>    go-ahead to merge a reviewed PR: update branch w/ base, resolve conflicts, merge
                          --strategy merge|squash|rebase   default: git.auto_merge_strategy
                          --wait-for-checks/--no-wait-for-checks  --no-resolve  --dry-run  --yes
worc tasks              list every known task with status + branch (read-only); --status filters
worc logs clean         reclaim disk: remove per-task dirs under .worc/logs/ (keeps the ledger)
                          --keep N                    keep the N most recently modified task dirs
                          --all                       also remove the ledger (completed.jsonl)
                          --yes                       skip the confirmation prompt
worc watch              process pending tasks; loop + periodic git sync
                          --poll-seconds N            override orchestrator.poll_interval_seconds
                          --queue NAME                serve only this queue (override orchestrator.queue)
worc stop               stop a running watch daemon (stop ladder: idle stops, busy confirms/forces)
                          --timeout SECONDS           graceful-shutdown wait before SIGKILL (default: 30)
                          --force                     stop a busy daemon softly (finish the current step)
                          --force-full                hard-stop now: kill the daemon + agent (POSIX group / Windows tree)
                          --non-interactive           never prompt; refuse a busy daemon unless --force/--force-full
worc restart            stop the running watch daemon (same stop ladder), then start a fresh one
                          --timeout SECONDS  --poll-seconds N  --queue NAME  --force  --force-full  --non-interactive
worc status [task-id]   show the active/latest persisted task (no work performed)
worc top                live read-only monitor: active task + node, queue, recent, daemon log (q quits)
                          --poll-seconds N  --queue NAME  --log-file PATH  --recent N
worc shell              interactive operator console over the watch daemon (needs the [shell] extra)
                          attaches to a live daemon or opens idle; 'up'/'watch' starts serving (verified),
                          'enqueue <file>' queues, 'promote <id>' stages→pending, 'down' stops, 'quit' detaches (daemon keeps running)
                          --queue NAME  --log-file PATH
worc list               enumerate active + pending + recent tasks (read-only)
                          --pending | --recent [N] | --all   focus one section
                          --format table|ids|json    human / bare ids / structured
                          --scope rerun|status|finalize   ids a given command accepts (completion-facing)
worc completion bash|zsh print a shell completion script; wire with: source <(worc completion zsh)
worc upgrade-config     add config keys from a new version, keeping existing values
                          --dry-run                   preview the keys that would be added
worc upgrade-docs       refresh the installed worc/ task-authoring docs to the packaged version
                          --dry-run                   preview added/updated/removed files
worc --version          installed version
```

Every command is also available under the short alias **`worc`** (e.g. `worc watch`, `worc stop`); `wastech-orchestrator` stays the canonical name.

Global options (before the subcommand): `--config PATH`, `--env-file PATH`, `--log-level`, `--log-format logfmt|json`, `--log-file PATH`, `--heartbeat-seconds N`. Exit codes: `0` done, `1` failed, `2` `manual_action_required`.

---

## Development

```bash
git clone https://github.com/VladimirMakarevich/wastech-orchestrator.git
cd wastech-orchestrator

python -m venv .venv
source .venv/bin/activate            # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pre-commit install                   # local gate; also: pre-commit install --hook-type pre-push

ruff check .
ruff format --check .
mypy src
lint-imports                         # architectural import-boundary contracts (.importlinter)
pytest
```

Project layout:

```text
src/wastech_orchestrator/
  cli.py                  # install / preflight / telegram-test / run / rerun / finalize / prs / merge-task / watch / stop / restart / status / top / shell / list / tasks / completion / logs / upgrade-config / upgrade-docs
  core/                   # the orchestrator wrapper (spine), HITL, dangerous-diff guardrail, recovery, decomposition, the constant supervisor layer
    flow/                 # the flow engine + graph traversal, node runners, validation, checkers
  notify/                 # Notifier contract + Telegram transport
  providers/              # AgentProvider contract + Codex / Claude adapters, durable sessions, redaction
  routing/                # node-based provider routing + global-primary infrastructure fallback
  config/                 # schema, loader, fail-closed validator, upgrade-config
  checks/                 # command-set model, resolution, and diff-based selection
  check_runner.py         # runs the selected command sets as bounded subprocesses
  git_manager.py          # the only commit/push/PR owner; scoped staging + the audit commit
  state_store.py          # SQLite checkpoints (schema v13)
  ledger.py               # the append-only completed-task ledger + failure reports
  task/                   # parser + §19 validation gate
  install/                # the install wizard, config writer, detection
  packaged/               # all shipped package data (one home): config.example.yaml (source for `install`/`upgrade-config`); flows/ = built-in flows + role prompts (-> .worc/flows/); guide/ = installable docs bundle (task authoring + config helper -> .worc/guide/)
docs/                     # operations, cookbook, configuration, glossary, task authoring, telegram, architecture, rules, backlog
  functional/             # code-derived block + flow reference (the source of truth on any discrepancy)
  worc/                   # authored source for the packaged guide/ (kept in sync by a test)
tests/                    # unit / integration / e2e (see .agents/rules/testing.md)
```

Coding agents working _in this repo_ follow [CLAUDE.md](CLAUDE.md) (Claude Code) and [AGENTS.md](AGENTS.md) (Codex), and the rules under [.agents/rules/](.agents/rules/).

---

## Documentation

| Document | Role |
| --- | --- |
| [docs/functional/index.md](docs/functional/index.md) | **Functional map** (code-derived): contracts, blocks, state machine, routing, fallback, the `.worc/` layout, security, invariants. The code is the source of truth on any discrepancy. |
| [docs/operations.md](docs/operations.md) | **Operator guide**: install, the `.worc/` layout, authorization, preflight, upgrading, diagnostics, and the `manual_action_required` recovery playbook. |
| [docs/cookbook.md](docs/cookbook.md) | Practical recipes: workspace setup, repo config, running tasks, routing, reading artifacts, recovery. |
| [docs/configuration.md](docs/configuration.md) | Full `config.yaml` reference with defaults, allowed values, and validation rules. |
| [docs/task-authoring.md](docs/task-authoring.md) | How to write valid task files and avoid validation rejects. |
| [docs/telegram.md](docs/telegram.md) | Bot/chat setup, environment config, preflight, live smoke test, and troubleshooting. |
| [docs/worc_architecture.md](docs/worc_architecture.md) | High-level architecture overview and the rationale behind the design. |
| [.agents/rules/](.agents/rules/) | Development rules: style, architectural invariants, security, git-flow, testing. |
| [docs/backlog/](docs/backlog/) | Deferred features and tracked follow-ups. |

The full documentation is published at **[vladimirmakarevich.github.io/wastech-orchestrator](https://vladimirmakarevich.github.io/wastech-orchestrator/)**.

---

## Design principles

1. **The core never knows a CLI's syntax** — only the `AgentProvider` interface; provider specifics live in `providers/`.
2. **The pipeline is data, not code** — a task's `task_type` resolves to a deterministic **flow** (a validated graph of typed nodes), driven by the flow engine; predictability over emergence.
3. **An advisory supervisor, never a decision-maker** — a constant read-only layer watches every step and writes the PR summary, but it can never rework, reopen, or route; blocking is the job of the in-flow `review`/evaluator nodes.
4. **Providers are interchangeable per node** — a node runs on its declared `provider`, else the global primary; **fallback is for infrastructure errors only** (targeting the global primary), never for test/review failures (those go to the bounded `fix` loop).
5. **Only the orchestrator does commit / push / PR.** Agents are forbidden from touching the Git lifecycle, and the code commit never contains orchestration/task files.
6. **Checkpoints at every step** → crash recovery and idempotent publishing.
7. **The security policy cannot be weakened** through a task or `extra_args`; flow-wide ceilings (`permission_ceiling`/`output_policy`/`network_policy`) are validated fail-closed before any task runs; no secrets in logs, SQLite, or artifacts.
8. **Auto mode is opt-in** — by default one task is processed, the working copy returns to `repo.base_branch` (unless the branch mode / `repo.checkout_base_on_cleanup` keeps it on the working branch), and further pending tasks are left for the operator.
