# Operations guide

How to install, authorize, run, and diagnose **wastech-orchestrator** in production. The orchestrator drives coding agents (OpenAI Codex CLI, Anthropic Claude Code CLI) through a deterministic pipeline and publishes the result to a Pull Request. It **owns** git (commit/push/PR); the agents only edit files in a dedicated clone. This guide is for the operator who runs it; the architecture reference is [worc_architecture.md](worc_architecture.md) and the security policy is [security.md](../.agents/rules/security.md).

> The orchestrator never installs or authorizes the CLIs and never stores credentials. Authorization for git and for each agent is configured **outside** the orchestrator (see [2. Authorization](operations-install.md#2-authorization-configured-outside-the-orchestrator)).

Related guides:

- [cookbook.md](cookbook.md) - practical recipes from workspace setup to recovery.
- [configuration.md](configuration.md) - every `config.yaml` field, default, and validation rule, indexed there across four topic pages.
- [task-authoring.md](task-authoring.md) - task front matter, examples, and validation behavior.

---

## The chapters

The guide is six pages. Read them in order the first time; after that this page is the map.

| Chapter | Page |
| --- | --- |
| [1. Installation](operations-install.md#1-installation), [Upgrading the orchestrator](operations-install.md#upgrading-the-orchestrator), [2. Authorization](operations-install.md#2-authorization-configured-outside-the-orchestrator) | [Install, upgrade, authorize](operations-install.md) |
| [3. Preflight](operations-preflight.md#3-preflight-both-clis), [command-set diagnostics](operations-preflight.md#command-set-diagnostics), [validating flows](operations-preflight.md#validating-flows-validate-flow) | [Preflight and validation](operations-preflight.md) |
| [4. Running](operations-running.md#4-running) — `run` / `watch`, [provider outages](operations-running.md#provider-outage-behavior), [`rerun`](operations-running.md#re-attempting-a-terminal-task-rerun), [`finalize`](operations-running.md#finalize-a-task-you-handled-by-hand-finalize), [the stop ladder](operations-running.md#stopping-the-daemon-safely-the-stop-ladder) | [Running tasks](operations-running.md) |
| [Structured logs](operations-logs.md#structured-logs), [`logs clean`](operations-logs.md#cleaning-up-logs-logs-clean), [`runs clean`](operations-logs.md#reclaiming-per-task-runtime-state-runs-clean), [`worc memory`](operations-logs.md#managing-the-memory-store-worc-memory), [Telegram](operations-logs.md#telegram-hitl-and-notifications) | [Logs, state, notifications](operations-logs.md) |
| [5. Git footprint and the audit commit](operations-publishing.md#5-git-footprint-and-the-audit-commit), [the PR body](operations-publishing.md#the-pr-body-and-how-a-reused-chain-pr-is-trimmed), [auto-merge](operations-publishing.md#auto-merge-to-the-base-branch-danger-bypasses-human-review) | [Git footprint and publishing](operations-publishing.md) |
| 6. Diagnostics — reading what a run produced, 7. Recovery playbook — `manual_action_required` | [Diagnostics and recovery](operations-diagnostics.md) |
