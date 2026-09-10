# Problem — Tracker connector

## The problem, as stated

The operator's idea, in their own framing (translated from the conversation of 2026-09-09):

> Add a connector project for the orchestrator — possibly a separate repository — that acts as a bridge to one concrete repository on GitHub. It runs periodically (every five minutes, configurable), watches every open issue, pulls in the new ones, analyses them, possibly reproduces them, then creates a task for the worc orchestrator, and worc picks that task up and drives it to an open pull request — or, if the user configures it so, all the way to a merge into `main`.
>
> The same thing will be needed for Azure DevOps, GitLab, or any other tracking system. The workflow of all such connectors should be identical, so that adding one costs a minimum of change, and users should install the connector they need themselves rather than getting all of them by default with the orchestrator. Hence a separate repository.

Two decisions were taken in that conversation and are inputs here, not open questions: the analysis-and-reproduction step (triage) is an **optional** mechanic, off in v1, so the connector mechanics can be proven quickly and cheaply; and the connector is a **separate package** whose core does not know any tracker's API.

## Whose problem it is

- **The operator running `worc`** — today they are the only bridge between the tracker and the task queue: they read each issue, decide, author a task file by hand, and promote it. This is the party the connector serves.
- **A maintainer of this repository** — they own the contract worc exposes to any external producer of tasks (the task-file grammar, the staging folder, `worc promote`, `worc list --format json`, the PR body). The two worc-side items in this spec are theirs.
- **The issue author** — indirectly: they get no feedback today that an issue was picked up, is being worked, or shipped.

## Current situation (the pain)

worc's intake is a Markdown task file. The operator writes it in `tasks/preparing/`, runs `worc promote <id>`, and `worc watch` claims it from `tasks/pending/` — or a teammate commits the file to the base branch and the watch tick's fetch/pull discovers it (`src/wastech_orchestrator/cli.py`, `cmd_watch` and `promote_tasks`). Nothing reads an issue tracker. The orchestrator otherwise runs unattended end to end — branch, agent, checks, review, commit, push, PR, optional auto-merge — so the one manual step left in the loop is the one at the very front: turning an issue into a task.

Concretely, for every issue the operator today:

1. reads it and decides whether it is actionable;
2. writes a task file with a valid `id`, a `title`, a `## Description`, and any dispatch fields (`priority`, `queue`, `commit_type`, `auto_merge`, …);
3. promotes it;
4. later finds the PR themselves and links or closes the issue by hand.

The backlog already names the gap twice, as one-line items with no design behind them: "GitHub Issues integration — link tasks to issues, update status, optionally close on PR creation/merge" and "Richer task parsing — candidate fields: … labels, issue links" (`docs/backlog/README.md`).

## Why it matters now

- The orchestrator is at the point where the intake is the bottleneck: everything after the task file is automated and checkpointed, the step before it is a human copying text between two windows.
- The operator wants to **test the connector mechanics quickly**. A v1 that needs no worc change and no agent call in its polling loop can be built and observed in days, and the same skeleton then carries triage and the other trackers.
- Left alone, each new tracker request would arrive as a bespoke script glued to worc's internals, and the internals it would reach for (`state.db`, `.worc/` layout) are not a contract.

## Signals & evidence

- Ingress contract that already exists: `preparing_dir` / `pending_dir` / `promote_tasks` in `src/wastech_orchestrator/cli.py`; the fail-closed task gate in `src/wastech_orchestrator/task/validation_gate.py` (required `id` / `title` / `## Description`, unknown front-matter key → reject, injection scan over front-matter values); the allowed key set in `src/wastech_orchestrator/task/model.py` (`ALLOWED_TASK_KEYS`, including `branch_name`, `priority`, `queue`, `commit_type`, `auto_merge`, `publish`).
- Egress that already exists: `worc list --format json` (`cmd_list` in `cli.py`) returns `task_id`, `status`, `title`, `branch` per known task and a file-derived entry per pending task — but no PR URL; the PR body is the committed `<id>.summary.md`, with an optional notice prepended by `GitManager._body_with_notice` (`src/wastech_orchestrator/git_manager.py`).
- GitHub is already the only code host worc speaks to, through `gh` with a `--repo` pin resolved from `repo.url` (`GitManager._gh`, `gh_repo_pin`). No other host appears anywhere in `src/`.
- The audit commit stages **only** the task's own lifecycle file and summary — never the whole `tasks/` tree — so a file the connector leaves in `tasks/preparing/` can never be swept into a task's commit (`GitManager.commit_audit`).
- Backlog rows "GitHub Issues integration", "Richer task parsing", "Multi-repo/project binding" in `docs/backlog/README.md`.

## Constraints given up front

- **Separate repository, optional install.** Nothing tracker-specific ships with the orchestrator; users install the connector, and the adapter, they need.
- **One workflow for every tracker.** Adding a tracker must be an adapter, not a fork of the pipeline.
- **v1 must need no change to worc.** The worc-side items in this spec are conveniences that follow, not prerequisites.
- **Issue text is untrusted input from strangers.** It will end up in a prompt to an agent with write access to the repository; the design has to treat it that way from the first line.
- **The orchestrator's hard invariants stand:** the agent never publishes; the core knows no CLI syntax; the connector must not become a second, weaker agent runtime beside worc.
- **Cross-platform** (Windows / Linux / macOS) for the connector as for worc, and greenfield: no migration or back-compat machinery for anything here.
