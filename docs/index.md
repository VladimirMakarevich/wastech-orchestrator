# WORC

A lean orchestrator that turns Markdown tasks into reviewed Pull Requests — driving **Codex** and **Claude Code** agents through a deterministic **flow** (a validated graph of typed steps) while keeping the Git lifecycle, security policy, crash recovery, and publication under a single owner.

> **Status: 0.x pre-release.** The flow engine and its packaged flows (`implementation`, `deep_research`, `security_audit`), node-based provider routing and infrastructure fallback, the constant advisory supervisor layer, the security/isolation gate, scoped Git audit commits, SQLite checkpoints and crash recovery, the watch loop with periodic Git sync, and the `install` setup flow are implemented and covered by an extensive test suite. Telegram HITL is implemented; parallel `git worktree` execution remains on the roadmap.

---

## Getting started

|  |  |
| --- | --- |
| [How it works](how-it-works.md) | The pipeline from a task file to a Pull Request: stages, routing, and recovery |
| [Operations](operations.md) | Install, authorization, upgrades, running, recovery, and diagnostics |
| [Cookbook](cookbook.md) | Practical recipes: workspace setup, repo config, routing, artifacts, and recovery |
| [How-To](how-to.md) | Problem-first troubleshooting recipes for situations you run into operating the orchestrator |

## Reference

|  |  |
| --- | --- |
| [Configuration](configuration.md) | Every `config.yaml` field, default, and validation rule |
| [Glossary](glossary.md) | Canonical vocabulary for commands, configs, states, flows, artifacts, and legacy terms |
| [Task authoring](task-authoring.md) | How to write valid task files accepted by the validation gate |
| [Flow authoring](flow-authoring.md) | How to author, register, validate, and debug a custom flow (`task_type`) |
| [Telegram](telegram.md) | Bot and chat setup, environment config, preflight, smoke test, and troubleshooting |

## Architecture

|  |  |
| --- | --- |
| [Architecture overview](worc_architecture.md) | High-level design rationale and the reasoning behind the key decisions |
| [Functional map](functional/index.md) | Code-derived reference: contracts, state machine, routing, fallback, security, invariants |
| [System flows](functional/system-flows.md) | Stage-by-stage pipeline, state transitions, and error paths |

---

## Core promises

- **Agents edit; the orchestrator owns Git.** Branch creation, commit, push, and PR are never delegated to an agent.
- **The pipeline is data, not code.** A task's `task_type` resolves to a validated **flow** graph of typed nodes; a constant **advisory supervisor** watches every step read-only and writes the PR summary, but it never changes the route, reworks a step, or overrides a gate.
- **Node-based routing, fallback for infrastructure errors only.** Each flow node runs on its declared provider (else the global primary); test and review failures enter a bounded `fix` loop instead of switching providers.
- **Security policy is a config-level invariant.** No task or `extra_args` can weaken the sandbox or the environment allowlist; flow-wide ceilings (`permission_ceiling`/`output_policy`/`network_policy`) are validated fail-closed before any task runs.
- **Crash-safe and idempotent.** SQLite checkpoints at every step; a restart resumes in-flight work without double-commits or duplicate PRs.
- **Tasks and results live in the repo.** The task file and its summary are committed to `tasks/`; orchestration state, config, and logs live in the gitignored `.worc/` home and never enter Git history.
- **Optional Telegram HITL.** Planning can request a single correlated question or approval (refinement, a question only); a `trust_level`-gated dangerous-diff approval (deletions / dependency changes, or operator-marked `protected_paths`) is fail-closed before tests.
