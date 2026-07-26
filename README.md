<div align="center">

# Wastech Orchestrator (WORC)

**Task in → reviewed Pull Request out.** — _You write the task. It ships the change._

`Two agents, one interface` · `Owns the Git lifecycle` · `Crash-safe & idempotent`

[![Docs](https://img.shields.io/badge/docs-github.io-2563eb?style=flat-square)](https://vladimirmakarevich.github.io/wastech-orchestrator/) [![Platform](https://img.shields.io/badge/platform-macOS%20·%20Linux%20·%20Windows-555?style=flat-square)](#requirements) [![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-lightgrey?style=flat-square)](LICENSE) ![Python](https://img.shields.io/badge/python-3.12+-3776ab?style=flat-square) ![Agents](https://img.shields.io/badge/agents-Codex%20CLI%20·%20Claude%20Code%20CLI-6f42c1?style=flat-square)

</div>

---

Wastech Orchestrator turns a written task into a reviewed Pull Request. You drop a task file into your repository; it creates a branch, drives a coding agent (**OpenAI Codex** or **Anthropic Claude Code**) through the work, runs your project's checks, commits the result, pushes, and opens a PR with a plain-language summary.

The agents do the editing. The orchestrator owns the process and the Git lifecycle — so the result is predictable, auditable, and safe to leave running.

## Why use it

- **Tasks in, PRs out.** Author a task in Markdown; get a branch, the change, your checks run, and a PR with a written summary — no babysitting.
- **Two agents, one interface.** Codex and Claude Code are interchangeable. If one fails for an infrastructure reason (missing binary, timeout, rate limit), the orchestrator automatically falls back to the other.
- **The orchestrator owns Git.** Agents never commit or push. Branch naming, staging, commit, push, PR, and the safe return to your base branch are all handled for you.
- **Your work stays in your repo.** The task file and its summary are committed alongside your code as an audit trail; everything else lives in a single gitignored home and never touches Git history.
- **Runs unattended.** A watch loop periodically syncs your base branch, so a teammate can hand off a task just by committing it and pushing.
- **Crash-safe and idempotent.** Every step is checkpointed. A restart resumes the in-flight task and never double-commits, double-pushes, or re-opens a PR.
- **Secure by default.** The sandbox policy and environment allowlist are locked at the config level — no task can weaken them, and no secrets are ever written to logs or artifacts.
- **Optional human-in-the-loop.** With Telegram configured, the orchestrator can ask a clarifying question or request approval before risky changes; routine work stays fully automatic.

## How it works

You start by defining a **flow** — the sequence of steps a task should go through, and which agent runs each one. Pick a built-in flow or shape your own to match how you work: a full pipeline for feature work, a lighter one for quick fixes, a research-only flow with no code changes at all. This is where you decide _what you actually need_.

Only then do you write tasks. Each task names a flow, and the orchestrator drives it through those steps end to end:

```text
   1. Define your flow      →   refine → plan → implement → test → review → fix → publish
                                (choose a built-in one or tailor your own)

   2. Write a task          →   tasks/pending/task-001.md   (names the flow to run)
                                     │
                                     ▼
   3. The orchestrator runs it   ┌──────────────────────────────────────┐
                                 │  branch → run each step of the flow   │
                                 └──────────────────────────────────────┘
                                     │
                                     ▼
   4. You get the result     →   scoped commit → push → Pull Request (summary as the body)
```

One task at a time, end to end. A read-only supervisor watches every step and writes the summary that becomes your PR description.

## Requirements

- **Python 3.12+** and **git**.
- The agent CLIs you want to use on your `PATH`: **`codex`** and/or **`claude`**.
- **GitHub CLI (`gh`)** — only if you want PRs opened automatically.

**Officially supported CLI versions:** `claude` **≥ 2.1.210** and `codex` **≥ 0.144.4**. Older versions may work but are not guaranteed — the orchestrator is developed and tested against these.

You authorize the tools yourself, once, in the environment the orchestrator runs in (`git push` for your remote, `gh auth login`, and signing in to `codex` / `claude`). The orchestrator never installs the CLIs or stores credentials, and passes only allowlisted environment variables to the agents.

## Quick start

```bash
# 1. Install
pipx install "git+https://github.com/VladimirMakarevich/wastech-orchestrator.git"

# 2. Set up your repo (interactive wizard: detects origin, branch, agents, checks)
cd /path/to/my-repo
worc install .

# 3. Confirm the agents and isolation policy are ready (read-only)
worc preflight
```

Write a task in the repo's `tasks/preparing/` staging directory:

```markdown
---
id: task-001
title: "Add email validation to the signup form"
---

## Description

The signup form accepts any string as an email. Validate the `email` field and show a clear error for malformed addresses.

## Acceptance criteria

- Malformed emails are rejected with a user-facing message.
- A unit test covers valid and invalid cases.
```

Promote it to `tasks/pending/` and run it:

```bash
# Process exactly one task, end to end:
worc run tasks/pending/task-001.md

# …or run the watch loop: it processes pending tasks and picks up new ones
# pushed to Git automatically (Ctrl-C to stop):
worc watch

# Check progress at any time:
worc status
```

The orchestrator creates a branch, runs the pipeline and your checks, commits the change plus an audit commit for the task and its summary, pushes, and (with `gh` present) opens a PR whose body is the summary.

## Configuration

`worc install` writes a validated `config.yaml` for you. The knobs you'll touch most:

| Setting | What it controls |
| --- | --- |
| `repo.url` / `repo.base_branch` | The target repository and the branch PRs target. |
| `agents.allowed` / providers | Which agents are enabled and which is the default. |
| `checks.command_sets` | Your test/lint commands, run as the testing stage. |
| `orchestrator.auto_mode.enabled` | Whether the next pending task starts automatically (default off). |
| `git.create_pull_request` | Open a PR after push (needs `gh`). |
| `telegram.*` | Optional notifications and human-in-the-loop approvals. |

Secrets are read from the environment (keep them in a gitignored `.env` or `export` them). See the [full configuration reference](https://vladimirmakarevich.github.io/wastech-orchestrator/).

## Common commands

```text
worc install [repo]     set up the orchestrator in a repository
worc preflight          check the agent CLIs and isolation policy (read-only)
worc run <task-file>    process exactly one task end to end
worc watch              process pending tasks in a loop, with periodic git sync
worc status             show the active / latest task
worc top                live read-only monitor of the active task and queue
worc shell              interactive daemon console; command errors keep the session open
worc stop / restart     stop or restart the watch daemon
worc rerun <task-id>    re-attempt a failed task
worc merge-task <id>    update, resolve conflicts, and merge a reviewed PR
worc --version          installed version
```

Run `worc --help` for the full command set and options.

## Documentation

Full documentation — installation, operations, configuration, task authoring, and Telegram setup — is published at **[vladimirmakarevich.github.io/wastech-orchestrator](https://vladimirmakarevich.github.io/wastech-orchestrator/)**.

## License

Released under the [Apache License 2.0](LICENSE).
