# When to use what

A decision guide for the optional knobs. Defaults are almost always right — reach for these only when the task genuinely needs them. See [README.md](README.md) for the hard rules each setting must obey.

## `run` vs `watch` (how the operator runs your task)

You usually do not choose this — the operator does — but it affects where your task file goes:

- **`run <task-file>`** processes exactly one task file, end to end.
- **`watch`** polls the `tasks/pending/` folder and processes tasks dropped there, looping with periodic git sync.

A live task belongs in the repo's own `tasks/pending/` directory (committed and pushed there) — that is how a teammate hands work to a watching orchestrator.

## Decomposition — split a large task

Decomposition is **not** a per-task knob. Whether a large task is broken into sequential subtasks (on one branch, one PR) is decided by the operator's `agents.decomposition.enabled` setting plus the flow's `decomposition:` block and the planning stage's proposal. Keep a task one coherent unit; if the work is genuinely large, say so in the Description and let planning propose a split.

## `pr_title` — override the PR title

By default the orchestrator generates the PR title from the task `title`. Set `pr_title` when you want the published PR to read differently — most often a conventional-commit-style subject:

```yaml
title: "Add a bounded retry budget to webhook delivery"
pr_title: "feat(webhooks): bounded retry budget for delivery"
```

Omit it to auto-generate. It changes only the PR title text; it does not touch the branch name (still `agent/<task-id>-<slug>`), the commit messages, or any routing.

## Disabling nodes — `nodes.<node-id>.enabled: false`

Disable a node only when it adds no value for this task. Keys are flow **node ids**; any node in the task's resolved flow may be disabled. The ids below are the default `implementation` flow's; a custom flow exposes its own (e.g. `code_review`). `refinement` is skipped automatically when the task is already complete (see "Refinement" below).

```yaml
nodes:
  planning:
    enabled: false # write a stub plan and run as a single unit (no decomposition)
  testing:
    enabled: false # bypass the Check Runner — only for a repo with no meaningful test suite
```

What disabling the default-flow nodes does:

| Node | Effect |
| --- | --- |
| `planning` | Stub plan; runs as a single unit. |
| `testing` | Straight to review, no checks run. |
| `review` | Commit with **no agent review gate**. |
| `fixing` | A test/review failure spins the fix loop as a no-op to its cap, then `manual_action_required`. |

**Disabling `review` is high-risk** — it removes the only agent quality gate before commit/PR. There is no config gate for it (no `agents.allow_review_skip`): which nodes are safe to disable is the operator's flow-authoring responsibility. Node-disable is per-task only (`nodes.<node-id>.enabled: false`); `enabled` is the **only** valid per-node key. Naming an id absent from the task's flow ends the task `failed` (a controlled error at flow resolution).

## Provider / model / reasoning — not a task knob

Which provider runs a stage, and with which model and reasoning effort, is decided by the **flow** (each flow node declares its own `provider`/`model`/`reasoning`, or defaults to the operator's global primary provider) — never by the task. A task cannot repoint a stage's provider or set its model. If a stage needs a stronger model, that is an operator/flow change, not a task front-matter field.

## `auto_merge` — danger

`auto_merge: true` requests that the orchestrator merge the PR automatically after publishing, **bypassing human merge**. A per-task value wins outright over the instance default `git.auto_merge` — there is no separate operator gate, because the task author is the same trusted operator who owns `config.yaml`. Leave it unset unless you have an explicit reason and know auto-merge is safe for this repository; skipping the human PR review is your call.

## Refinement — skipped automatically when complete

You cannot flag a task to skip refinement. The orchestrator skips it automatically when the task looks complete — completeness needs a non-empty Description **plus** acceptance criteria. Provide acceptance criteria when you want to skip refinement; omit them to let the refinement stage enrich an under-specified task (missing criteria never rejects the task).

## Where task files live

There is a single canonical layout. Drop your task file in the repo's own `tasks/pending/` directory at the repo root, where it is git-tracked, committed, and pushed — that is how work reaches a watching orchestrator. Everything the orchestrator generates lives under a single gitignored `<repo>/.worc/` home; only the `tasks/` lifecycle directories stay at the repo root and are tracked. The orchestrator commits the task file and its `<id>.summary.md` as an audit trail; a rejected task is quarantined under `.worc/tasks/rejected`.

## `contacts` and Telegram

`contacts` is a list of plain-text strings rendered as mentions in Telegram notifications and human-in-the-loop prompts:

```yaml
contacts:
  - "@team-lead"
```

They are cosmetic mentions only. They do **not** choose the Telegram chat, grant access, change routing, or alter approval scope — the chat id stays operator-controlled configuration.
