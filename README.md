# wastech-orchestrator

A console **lean orchestrator** for automatically carrying out development tasks via external coding agents (**OpenAI Codex CLI** and **Anthropic Claude Code CLI**), publishing the result to a dedicated Git branch / Pull Request.

The orchestrator owns the process: it accepts a task → parses it → creates a branch → runs a deterministic pipeline of stages through interchangeable CLI agents → runs checks → commits and pushes. After a task reaches a terminal state, it safely switches the working copy back to the configured base branch before any next task can start. The agents work only with the repository contents and do not manage the Git lifecycle.

> Status: **design / pre-MVP**. The architecture is fixed; the codebase is in an early stage. The spec below is the source of truth for the implementation.

---

## Documents (sources of truth)

| Document | Role |
|----------|------|
| [docs/orchestrator_final_plan.md](docs/orchestrator_final_plan.md) | **Canonical build spec**: contracts, state machine, routing, fallback, security, DoD, implementation stages. Takes priority in case of discrepancies. |
| [docs/codex_git_orchestrator_architecture.md](docs/codex_git_orchestrator_architecture.md) | Architectural overview and rationale for the decisions (high-level). The original requirements and their mapping are in §11. |
| [docs/operations.md](docs/operations.md) | **Operator guide**: install, authorization, preflight for both CLIs, footprint modes, diagnostics, and the `manual_action_required` recovery playbook. |
| [docs/cookbook.md](docs/cookbook.md) | Practical recipes for initializing a workspace, configuring a repo, running tasks, routing providers, reading artifacts, and recovery. |
| [docs/configuration.md](docs/configuration.md) | Detailed `config.yaml` reference with defaults, allowed values, validation rules, and safe examples. |
| [docs/task-authoring.md](docs/task-authoring.md) | How to write valid task files, use front matter, choose refinement/decomposition flags, and avoid validation rejects. |
| [docs/backlog/](docs/backlog/) | Aggregated backlog, including deferred v2 features, prompt customization, and token optimization. |
| [docs/rules/](docs/rules/) | Development rules: style, architectural invariants, security, git-flow, tests. |

For coding agents: [CLAUDE.md](CLAUDE.md) (Claude Code) and [AGENTS.md](AGENTS.md) (Codex).

---

## Key principles

1. **The core does not know the syntax of any specific CLI** — only the `AgentProvider` interface.
2. **A deterministic pipeline of stages**, rather than free-form agent autonomy.
3. **The coding agent sits behind an abstraction** — Codex and Claude Code are interchangeable, with per-stage primary/fallback.
4. **Fallback only for infrastructure errors** of the provider, not for quality errors (tests/review).
5. **Only the orchestrator does commit / push / PR** — agents are forbidden from doing so.
6. **Checkpoints at every stage** → recovery after a crash, idempotent publishing.
7. **The security policy cannot be weakened** through a task or `extra_args`.
8. **Auto mode is opt-in** — by default the orchestrator handles one task, returns to `repo.base_branch`, and leaves the next pending task untouched.

---

## Technologies

- **Python 3.12+**
- `watchdog` — watching the task folder
- `PyYAML` — config and templates
- `python-telegram-bot` — human-in-the-loop and notifications
- `sqlite3` (stdlib) — state store and checkpoints
- subprocess — running `git` / `codex` / `claude` / checks
- dev: `ruff`, `mypy`, `pytest`

---

## Project structure

```text
wastech-orchestrator/
  README.md
  CLAUDE.md / AGENTS.md          # instructions for coding agents in THIS repo
  pyproject.toml
  config.example.yaml            # configuration example (copy to config.yaml)
  docs/
    cookbook.md                   # practical user recipes
    configuration.md              # config.yaml reference
    task-authoring.md             # task input guide
    operations.md                 # operator guide
    orchestrator_final_plan.md    # canonical build spec
    backlog/                      # aggregated future work and detailed backlog items
    rules/                       # development rules (source of truth for agents)
  .claude/
    skills/                      # reusable skills for development
  src/
    wastech_orchestrator/
      cli.py                     # entry point (run / watch / init)
      providers/
        base.py                  # AgentProvider contract (§4.3 of the spec)
      templates/                 # scaffolding copied out by `init` (task + per-stage prompts)
  tasks/                         # pending / processing / done / failed / rejected
  tests/                         # unit / integration / e2e (see docs/rules/testing.md)
```

---

## Quick start (development)

```bash
# 1. virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1      # Windows PowerShell
# source .venv/bin/activate       # macOS/Linux

# 2. install in editable mode with dev dependencies
pip install -e ".[dev]"

# 3. config
copy config.example.yaml config.yaml   # Windows
# cp config.example.yaml config.yaml    # macOS/Linux

# 4. checks
ruff check .
mypy src
pytest
```

Initialize a project layout (folders + `config.yaml` + templates; idempotent):

```bash
python -m wastech_orchestrator init .                              # external git footprint (default)
python -m wastech_orchestrator init . --git-mode in_repo_exclude   # keep artifacts out of the target repo
```

Running (as the pipeline is implemented):

```bash
python -m wastech_orchestrator preflight                    # check both CLIs + isolation (read-only)
python -m wastech_orchestrator run tasks/pending/task-001.md
python -m wastech_orchestrator watch
```

See [docs/cookbook.md](docs/cookbook.md) for practical recipes, [docs/configuration.md](docs/configuration.md)
for every `config.yaml` field, [docs/task-authoring.md](docs/task-authoring.md) for task files, and
[docs/operations.md](docs/operations.md) for production operations.

`watch` respects `orchestrator.auto_mode.enabled` in `config.yaml`: when `false` (default), it does not automatically take another pending task after terminal cleanup; when `true`, it processes pending tasks sequentially, returning to `repo.base_branch` between tasks.

---

## Implementation roadmap

The stages are executed strictly in sequence (see [docs/orchestrator_final_plan.md §15](docs/orchestrator_final_plan.md)):

1. Contracts and configuration
2. Provider layer and the Codex adapter
3. Claude Code adapter
4. Routing and fallback
5. Pipeline and recovery
6. Security and observability

Moving to the next stage is only allowed after the DoD of the previous one has been met.
