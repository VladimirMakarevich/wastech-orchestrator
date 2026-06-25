# When to use what

A decision guide for the optional knobs. Defaults are almost always right — reach for these only when the task genuinely needs them. See [README.md](README.md) for the hard rules each setting must obey.

## `run` vs `watch` (how the operator runs your task)

You usually do not choose this — the operator does — but it affects where your task file goes:

- **`run <task-file>`** processes exactly one task file, end to end.
- **`watch`** polls the `tasks/pending/` folder and processes tasks dropped there, looping with periodic git sync.

A live task belongs in the repo's own `tasks/pending/` directory (committed and pushed there) — that is how a teammate hands work to a watching orchestrator.

## `task_type` — choose the flow

`task_type` selects which **flow** (the fixed pipeline of stages) carries the task. Omit it and the task runs the default `implementation` flow (`planning → implementation → testing → review → fixing → documentation → publish`, with `refinement` skipped automatically when the task is complete) — what almost every coding task wants. Set it to run a different flow:

```yaml
task_type: deep_research # or: security_audit, implementation (default), or a custom operator flow
```

Built-in flows: `implementation` (default coding pipeline), `deep_research`, `security_audit`. An operator can add more by dropping a `<task_type>.yaml` in the repo's `<repo>/.worc/flows/` directory (the file's own `flow.task_type` must match its name); an operator flow there takes priority over a built-in of the same name. A `task_type` that resolves to no flow fails the task at flow resolution, before any branch is created.

The task only **names** the flow — it never edits the graph, its nodes, or their providers/models. Picking a different built-in is the one task-side choice. To change _which_ stages run for a single task, the only per-task knob is disabling a node (see below); to reshape the pipeline or retune a stage's provider/model, edit the flow YAML under `.worc/flows/` (an operator/flow-authoring change, not a task field).

## Decomposition — split a large task

Whether a large task is broken into sequential subtasks (on one branch, one PR) is decided by the flow's `decomposition:` block and the planning stage's proposal, gated on whether decomposition is _permitted_ for the task. The gate defaults to the operator's `agents.decomposition.enabled` setting, but a task may override it with the optional `decomposition` field: `true` permits a split even when the global setting is off, `false` forbids one even when it is on, omitted defers to the global. The field only flips the gate — it never edits the graph or forces a split: the flow + planning (or an operator `subtasks:` manifest) still decide whether a split actually happens. Keep a task one coherent unit; if the work is genuinely large, say so in the Description and let planning propose a split (set `decomposition: true` if the global gate is off and you want this one task considered for splitting).

## `branch_name` — override the task branch

By default the orchestrator creates `<repo.branch_prefix>/<task-id>-<slug(title)>`, usually `worc/<task-id>-<slug>`. Set `branch_name` when the target project or customer requires a different branch convention:

```yaml
title: "Add a bounded retry budget to webhook delivery"
branch_name: "feature/ABC-123-webhook-retry-budget"
```

Omit it to use the default. The value is the full branch name, not a suffix. It must be a valid Git branch name, must not equal the base branch, and is validated before any branch/provider side effect. It changes only the branch/head used for push and PR creation; the PR title still comes from `title`.

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
