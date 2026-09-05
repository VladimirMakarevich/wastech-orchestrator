# WORC

A lean orchestrator that turns Markdown tasks into reviewed Pull Requests — driving **Codex** and **Claude Code** agents through a deterministic **flow** (a validated graph of typed steps) while keeping the Git lifecycle, security policy, crash recovery, and publication under a single owner.

> **Status: 0.x pre-release.** The flow engine and its packaged flows (`implementation`, `deep_research`, `security_audit`, `merge`, and the content/blog authoring flows), node-based provider routing and infrastructure fallback, the advisory supervisor layer (on by default, removable), the deterministic PR-body report, deterministic decomposition into per-subtask commits, custom `tool` nodes, the security/isolation gate, scoped Git audit commits, SQLite checkpoints and crash recovery, the watch loop with periodic Git sync, the `install` setup flow, and the operator's **advanced mode** (`security.strict_isolation: false` — what a fresh install writes) are implemented and covered by an extensive test suite that runs on Linux, macOS, and Windows. Persistent repo-scoped memory runs but is **experimental** — off by default, unaudited store, no redaction guarantee. Telegram HITL is implemented; parallel `git worktree` execution remains on the roadmap — for now a single task holds the processing slot at a time.

---

## Getting started

|  |  |
| --- | --- |
| [How it works](how-it-works.md) | The mental model: the fixed line of steps, who runs each one, the fix loops, decomposition, and how a task ends |
| [Operations](operations.md) | Install, upgrades, authorization, preflight, running, the Git footprint, diagnostics, and the recovery playbook |
| [Cookbook](cookbook.md) | A from-zero walkthrough: install, repo config, preflight, a first task, `watch`, per-node routing and prompts, checks, and the audit commit |
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
| [Architecture overview](worc_architecture.md) | Design rationale, the core components, the state machine, the `.worc/` footprint, and the reasoning behind the key decisions |
| [Architecture as code](likec4/README.md) | The C4 model of the system in `workspace.likec4` — `landscape`, `containers`, `components`, `crosscutting`, and `isolation` views |

---

## Core promises

- **Agents edit; the orchestrator owns Git.** Branch creation, commit, push, and PR are never delegated to an agent. Publishing **recovers** rather than infers provenance: an existing branch, a moved branch, or an open PR on the task head is ordinary working state, so a diverged remote is merged in locally, the quality gate re-runs over the combination, and only then does anything go out.
- **The pipeline is data, not code.** A task's `task_type` resolves to a validated **flow** graph of typed nodes; an **advisory supervisor** watches the run read-only at a configured cadence and writes the PR summary, but it never changes the route, reworks a step, or overrides a gate. Switch it off and the PR body is rendered deterministically from the run's own recorded facts.
- **Node-based routing, fallback for infrastructure errors only.** Each flow node runs on its declared provider (else the global primary); test and review failures enter a bounded `fix` loop instead of switching providers.
- **Security policy is a config-level invariant.** No task or `extra_args` can weaken the sandbox or the environment allowlist; flow-wide ceilings (`permission_ceiling`/`output_policy`/`network_policy`) are validated fail-closed before any task runs. Both provider full-access modes are refused at **every** value of `security.strict_isolation`, by three independent layers. Setting that key `false` — the posture a fresh install writes — is the **advanced mode**: full freedom for the agent under the operator's responsibility, except a four-level floor, and only the first level of it is mechanical — mechanical on Codex, which keeps its generated permission profile at either value of the key, while Claude gets **no OS sandbox at all** in that mode, on any host. Preflight and the run log say so on their own `isolation-floor: NONE` line. Read it before the first real run.
- **Nothing secret crosses the boundary.** Every artifact, log, and SQLite write passes a single redaction seam first, and a CLI is launched from an argv list, never a shell string, with task content reaching a provider only as a file path. Under strict isolation a child process receives only the allowlisted variables that exist in the parent environment, never the parent's environment. Advanced mode trades that gate away for the _agent's_ children and widens redaction to compensate — scrubbing a secret-named variable by name alone — while the orchestrator's own `git` and `gh` keep the allowlist, because a `GH_REPO` from your shell would otherwise retarget a pull request.
- **Crash-safe and idempotent.** SQLite checkpoints at every step; a restart resumes in-flight work without double-commits or duplicate PRs.
- **Tasks and results live in the repo.** The task file and its `<id>.summary.md` are committed under `tasks/` by a separate scoped audit commit; orchestration state, config, and logs live in the gitignored `.worc/` home and the agent-facing exchange in the gitignored `.worc-io/` — neither ever enters Git history.
- **Optional Telegram HITL.** Planning can request a single correlated question or approval (refinement, a question only); a `trust_level`-gated dangerous-diff approval (deletions / renames / dependency changes, or operator-marked `protected_paths`) is fail-closed and fires in three places — after the edit step that produced the diff, at that node's own round-trip, and once more immediately before the publishing commit, measured from the last commit the orchestrator itself made rather than from `HEAD`.
