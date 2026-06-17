# WORC

A lean orchestrator that turns Markdown tasks into reviewed Pull Requests — driving **Codex** and **Claude Code** agents through a deterministic stage pipeline while keeping the Git lifecycle, security policy, crash recovery, and publication under a single owner.

> **Status: 0.x pre-release.** The full single-task pipeline, provider routing and fallback, the security/isolation gate, scoped Git audit commits, SQLite checkpoints and crash recovery, the watch loop with periodic Git sync, and the `install` setup flow are implemented and covered by an extensive test suite. Telegram HITL is implemented; parallel `git worktree` execution remains on the roadmap.

---

## Getting started

|  |  |
| --- | --- |
| [How it works](how-it-works.md) | The pipeline from a task file to a Pull Request: stages, routing, and recovery |
| [Operations](operations.md) | Install, authorization, upgrades, running, recovery, and diagnostics |
| [Cookbook](cookbook.md) | Practical recipes: workspace setup, repo config, routing, artifacts, and recovery |

## Reference

|  |  |
| --- | --- |
| [Configuration](configuration.md) | Every `config.yaml` field, default, and validation rule |
| [Task authoring](task-authoring.md) | How to write valid task files accepted by the validation gate |
| [Telegram](telegram.md) | Bot and chat setup, environment config, preflight, smoke test, and troubleshooting |

## Architecture

|  |  |
| --- | --- |
| [Architecture overview](worc_architecture.md) | High-level design rationale and the reasoning behind the key decisions |
| [Functional map](functional/index.md) | Code-derived reference: contracts, state machine, routing, fallback, security, invariants |
| [System flows](functional/system-flows.md) | Stage-by-stage pipeline, state transitions, and error paths |
| [LikeC4 diagrams](likec4/README.md) | Interactive C4-model architecture diagrams |

---

## Core promises

- **Agents edit; the orchestrator owns Git.** Branch creation, commit, push, and PR are never delegated to an agent.
- **Provider fallback for infrastructure errors only.** Test and review failures enter a bounded `fix` loop instead of switching providers.
- **Security policy is a config-level invariant.** No task or `extra_args` can weaken the sandbox or the environment allowlist.
- **Crash-safe and idempotent.** SQLite checkpoints at every stage; a restart resumes in-flight work without double-commits or duplicate PRs.
- **Tasks and results live in the repo.** The task file and its summary are committed to `tasks/`; orchestration state, config, and logs live in the gitignored `.worc/` home and never enter Git history.
- **Optional Telegram HITL.** Refinement and planning stages can request a single correlated question or approval; deletions and dependency changes are fail-closed before tests.
