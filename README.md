# wastech-orchestrator

**A lean orchestrator that turns a written task into a reviewed Pull Request** — using external
coding agents (**OpenAI Codex CLI** and **Anthropic Claude Code CLI**) to do the editing, while the
orchestrator owns the process and the Git lifecycle.

You drop a task file into a repo; the orchestrator parses it, creates a branch, and drives a
**deterministic stage pipeline** — refine → plan → implement → test → review → fix → summary →
publish — routing each stage to a coding agent behind a provider abstraction (with automatic
fallback to the other agent on infrastructure errors). It runs your project's checks, commits the
change, pushes, and opens a PR whose body is a plain-language summary of what was done and why. The
agents only edit files inside a dedicated clone; **only the orchestrator commits, pushes, and creates
PRs.** Every stage is checkpointed to SQLite, so a crash resumes from the last completed step and
publishing is idempotent.

> **Status: 0.x pre-release.** The full single-task pipeline, provider routing + fallback, the
> security/isolation gate, scoped Git footprints, SQLite checkpoints + crash recovery, the watch
> loop with periodic Git sync, and the `init`/`install` setup flows are implemented and covered by an
> extensive test suite. Telegram notifications and human-in-the-loop for clarification and
> deletion/dependency approvals are implemented; parallel `git worktree` execution remains on the
> roadmap (see [docs/backlog/](docs/backlog/)). APIs and config may still change before 1.0.

---

## Why use it

- **Tasks in, PRs out.** Author a task in Markdown; get a branch, the change, your checks run, and a
  PR with a written summary — no babysitting.
- **Two agents, one interface.** Codex and Claude Code are interchangeable per stage. If one fails
  for an *infrastructure* reason (binary missing, timeout, rate limit), the orchestrator falls back
  to the other; test/review failures instead loop through a bounded `fix` stage.
- **The orchestrator owns Git.** Agents never commit or push. Branch naming, scoped staging,
  commit, push, PR, and the safe return to the base branch are all the orchestrator's job.
- **Tasks and results live in the repo.** By default the task file and its summary are committed to
  the same repository as the code (in-repo audit footprint); working artifacts (plans, diffs, logs)
  stay local and never enter Git history. Other footprints are available.
- **Discovers work pushed to Git.** `watch` runs as a loop that periodically `fetch`/`pull`s the
  base branch, so a teammate can hand the orchestrator a task simply by committing it to
  `tasks/pending/` and pushing — no manual sync needed.
- **Crash-safe and idempotent.** State machine + per-stage checkpoints in SQLite; a restart resumes
  the one in-flight task and never double-commits, double-pushes, or re-opens a PR.
- **Optional Telegram HITL.** `refinement`/`planning` can ask one correlated question or approval,
  and deletions/dependency changes are fail-closed before tests. Waiting state is a recoverable
  artifact; ordinary diffs and routine publishing remain automatic.
- **Security can't be weakened by a task.** The sandbox/approval policy and the environment
  allowlist are config-level invariants; no task or `extra_args` can disable them, and no secrets
  are written to logs, SQLite, or artifacts.

---

## How it works

```text
tasks/pending/task-001.md
        │  watch loop (periodic git fetch/pull) picks it up
        ▼
   ┌──────────────────────── deterministic pipeline ─────────────────────────┐
   │ git fetch/pull → checkout base → create branch agent/<id>-<slug>          │
   │   refine → plan → implement → test → review → fix(loop)                   │
   │   → summary  (move task → tasks/done/, write <id>.summary.md)             │
   │   → commit (code)  +  commit (tasks/: task + summary)                     │
   │   → push  →  gh pr create   (PR body = the summary)                       │
   │   → return to base branch  →  fetch/pull refresh                          │
   └───────────────────────────────────────────────────────────────────────────┘
        │  auto_mode? → next pending task   |   otherwise idle (keep polling)
```

- **One task at a time.** A single processing slot; other tasks wait in `tasks/pending/`. Auto mode
  (off by default) controls whether the next pending task starts automatically after cleanup.
- **Footprint modes** decide where `tasks/` and `logs/` live and what is committed — see
  [Configuration](#configuration).
- The canonical, detailed contract (state machine, routing, recovery, security, footprints) is
  [docs/implementation_stages/00_orchestrator_final_plan.md](docs/implementation_stages/00_orchestrator_final_plan.md).

---

## Requirements

- **Python 3.12+** and **git**.
- The agent CLIs you intend to route to on `PATH`: **`codex`** and/or **`claude`**.
- **GitHub CLI (`gh`)** — only if you want PRs opened automatically (`git.create_pull_request: true`).

The orchestrator **never installs or authorizes** the CLIs and never stores credentials. Authorize
git push for your remote, `gh auth login`, and sign in to `codex` / `claude` yourself, once, in the
environment the orchestrator runs in. Only allowlisted environment variables are passed to child
processes. See [docs/operations.md §2](docs/operations.md).

---

## Quick start

Bind an existing repository and let the orchestrator process a task end to end.

```bash
# 1. Install the CLI (isolated, recommended)
pipx install "git+https://github.com/VladimirMakarevich/wastech-orchestrator.git"

# 2. Bind your repo: generates a validated config.yaml + records the binding.
#    The default in-repo footprint keeps the task & its summary in this repo; config + state
#    live in a sibling <repo-name>-orchestrator workspace, leaving your tracked files untouched.
cd /path/to/my-repo
wastech-orchestrator install .          # interactive wizard (detects origin, branch, agents, checks)

# 3. Confirm the agents and isolation policy are ready (read-only).
wastech-orchestrator preflight
```

Author a task in the repo's `tasks/pending/` directory:

```markdown
---
id: task-001
title: "Add email validation to the signup form"
# optional: refined: true | decompose: true | agents: {implementation: claude, review: codex}
---

## Description

The signup form accepts any string as an email. Validate the `email` field and show a clear error
for malformed addresses.

## Acceptance criteria

- Malformed emails are rejected with a user-facing message.
- A unit test covers valid and invalid cases.
```

> Only `id` and `title` are required; the gate **rejects unknown fields**. The full allow-list
> (`id`, `title`, `refined`, `decompose`, `agents`, `contacts`, `model`, `reasoning`) is in
> [docs/task-authoring.md](docs/task-authoring.md).

Then run it:

```bash
# Process exactly one task, end to end:
wastech-orchestrator run tasks/pending/task-001.md

# …or run the watch loop: it processes pending tasks and periodically fetch/pulls the base branch
# so tasks pushed to Git later are picked up automatically (Ctrl-C to stop).
wastech-orchestrator watch

# Inspect progress / the latest persisted task at any time:
wastech-orchestrator status
```

The orchestrator creates `agent/task-001-...`, runs the pipeline and your checks, commits the code
plus the task and its summary, pushes, and (with `gh` present) opens a PR whose body is the summary.
A failed attempt is also committed and pushed for inspection — without opening a PR.

> Prefer a fresh, self-contained layout instead of binding an existing repo? Use
> `wastech-orchestrator init .` to scaffold folders + `config.yaml` + editable prompt templates,
> then point `repo.url` / `repo.local_path` at a separate clone. See
> [docs/operations.md §1](docs/operations.md).

---

## Configuration

`install` writes a validated `config.yaml`; `init` seeds one from `config.example.yaml`. The full
reference (every field, default, and validation rule) is [docs/configuration.md](docs/configuration.md).
The knobs you'll touch most:

| Setting | What it controls |
|---|---|
| `repo.url` / `repo.local_path` / `repo.base_branch` | The target repository and the branch PRs target. |
| `git.footprint` | Where `tasks/`/`logs/` live and what is committed: **`in_repo` + `commit`** (default — task + summary committed, logs kept local), `in_repo` + `exclude_local` (artifacts in the clone but git-ignored), or `external` (artifacts outside the clone, zero footprint). |
| `orchestrator.auto_mode.enabled` | Process the next pending task automatically after cleanup (default `false`). |
| `orchestrator.poll_interval_seconds` | `watch` tick: fetch/pull + re-scan interval (default `300`; `0` = single pass). |
| `agents.allowed` / `agents.routing` | Which providers are enabled and the primary/fallback per stage. |
| `checks.commands` | Your project's test/lint commands, run as the `test` stage (argv list, no shell). |
| `git.create_pull_request` | Open a PR after push (needs `gh`); disabling it does not disable commit/push. |
| `telegram.*` | Optional terminal notifications and blocking HITL; credentials stay in environment variables. |
| `security.*` | Strict isolation, the environment allowlist, denied paths/commands — invariants a task cannot weaken. |

Config discovery order: explicit `--config` → `./config.yaml` → the current repo's binding → a hint
to run `install .`.

---

## Commands

```text
wastech-orchestrator init [path]        scaffold folders + config.yaml + templates (idempotent)
                          --git-mode in_repo_commit | in_repo_exclude | external   (default: in_repo_commit)
wastech-orchestrator install [repo]     bind an existing repo, generate config, record the binding
                          --non-interactive --provider codex|claude|both|auto --no-create-pr --reconfigure
wastech-orchestrator preflight          check both CLIs' health + the isolation policy (read-only)
wastech-orchestrator telegram-test      send a real correlated Telegram prompt and wait for reply
                          --timeout-seconds N       smoke-test deadline (default: 60)
wastech-orchestrator run <task-file>    process exactly one task end to end
wastech-orchestrator watch              process pending tasks; loop + periodic git sync
                          --poll-seconds N            override orchestrator.poll_interval_seconds
wastech-orchestrator stop               stop a running watch daemon (SIGTERM, then SIGKILL)
                          --timeout SECONDS           graceful-shutdown wait before SIGKILL (default: 30)
wastech-orchestrator restart            stop the running watch daemon, then start a fresh one
                          --timeout SECONDS  --poll-seconds N
wastech-orchestrator status [task-id]   show the active/latest persisted task (no work performed)
wastech-orchestrator --version          installed version
```

Every command is also available under the short alias **`worc`** (e.g. `worc watch`, `worc stop`);
`wastech-orchestrator` stays the canonical name.

Global options (before the subcommand): `--config PATH`, `--log-level`, `--log-format logfmt|json`,
`--log-file PATH`, `--heartbeat-seconds N`. Exit codes: `0` done, `1` failed, `2`
`manual_action_required`.

---

## Development

```bash
git clone https://github.com/VladimirMakarevich/wastech-orchestrator.git
cd wastech-orchestrator

python -m venv .venv
source .venv/bin/activate            # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

ruff check .
ruff format --check .
mypy src
pytest
```

Project layout:

```text
src/wastech_orchestrator/
  cli.py                  # init / install / preflight / telegram-test / run / watch / status
  core/                   # pipeline, HITL, dangerous-diff guardrails, recovery, decomposition
  notify/                 # Notifier contract + Telegram transport
  providers/              # AgentProvider contract + Codex / Claude adapters, redaction
  routing/                # per-stage routing + infrastructure-error fallback
  config/                 # schema, loader, fail-closed validator
  git_manager.py          # the only commit/push/PR owner; scoped staging + footprints
  state_store.py          # SQLite checkpoints
  task/                   # parser + §19 validation gate
  install/                # the install wizard, config writer, detection, registry
  templates/              # scaffolding copied by `init` (config example + per-stage prompts)
docs/                     # spec, operations, cookbook, configuration, task authoring, rules, backlog
tests/                    # unit / integration / e2e (see docs/rules/testing.md)
```

Coding agents working *in this repo* follow [CLAUDE.md](CLAUDE.md) (Claude Code) and
[AGENTS.md](AGENTS.md) (Codex), and the rules under [docs/rules/](docs/rules/).

---

## Documentation

| Document | Role |
|----------|------|
| [docs/implementation_stages/00_orchestrator_final_plan.md](docs/implementation_stages/00_orchestrator_final_plan.md) | **Canonical build spec**: contracts, state machine, routing, fallback, footprints, security, DoD. Source of truth on any discrepancy. |
| [docs/operations.md](docs/operations.md) | **Operator guide**: install, authorization, preflight, footprint modes, upgrading, diagnostics, and the `manual_action_required` recovery playbook. |
| [docs/cookbook.md](docs/cookbook.md) | Practical recipes: workspace setup, repo config, running tasks, routing, reading artifacts, recovery. |
| [docs/configuration.md](docs/configuration.md) | Full `config.yaml` reference with defaults, allowed values, and validation rules. |
| [docs/task-authoring.md](docs/task-authoring.md) | How to write valid task files and avoid validation rejects. |
| [docs/telegram.md](docs/telegram.md) | Bot/chat setup, environment config, preflight, live smoke test, and troubleshooting. |
| [docs/codex_git_orchestrator_architecture.md](docs/codex_git_orchestrator_architecture.md) | High-level architecture overview and the rationale behind the design. |
| [docs/rules/](docs/rules/) | Development rules: style, architectural invariants, security, git-flow, testing. |
| [docs/backlog/](docs/backlog/) | Deferred features and tracked follow-ups. |
| [CHANGELOG.md](CHANGELOG.md) | Release notes and the `config.yaml` / `state.db` / registry schema versions. |

---

## Design principles

1. **The core never knows a CLI's syntax** — only the `AgentProvider` interface; provider specifics
   live in `providers/`.
2. **A deterministic stage pipeline**, not free-form agent autonomy — predictability over emergence.
3. **Providers are interchangeable** with per-stage primary/fallback; **fallback is for
   infrastructure errors only**, never for test/review failures (those go to the bounded `fix` loop).
4. **Only the orchestrator does commit / push / PR.** Agents are forbidden from touching the Git
   lifecycle, and the code commit never contains orchestration/task files.
5. **Checkpoints at every stage** → crash recovery and idempotent publishing.
6. **The security policy cannot be weakened** through a task or `extra_args`; no secrets in logs,
   SQLite, or artifacts.
7. **Auto mode is opt-in** — by default one task is processed, the working copy returns to
   `repo.base_branch`, and further pending tasks are left for the operator.
