# When to use what

A decision guide for the optional knobs. Defaults are almost always right — reach for these only when the task genuinely needs them. See [README.md](README.md) for the hard rules each setting must obey.

## `run` vs `watch` (how the operator runs your task)

You usually do not choose this — the operator does — but it affects where your task file goes:

- **`run <task-file>`** processes exactly one task file, end to end. The argument is a **path** to the file (e.g. `tasks/pending/my-task.md`), not a task id.
- **`watch`** polls the `tasks/pending/` folder and processes tasks dropped there, looping with periodic git sync.

A live task belongs in the repo's own `tasks/pending/` directory (committed and pushed there) — that is how a teammate hands work to a watching orchestrator.

## `task_type` — choose the flow

`task_type` selects which **flow** (the fixed pipeline of stages) carries the task. Omit it and the task runs the default `implementation` flow (`planning → implementation → testing → review → fixing → documentation → publish`, with `refinement` skipped automatically when the task is complete) — what almost every coding task wants. Set it to run a different flow:

```yaml
task_type: deep_research # or: security_audit, implementation (default), or a custom operator flow
```

Built-in flows: `implementation` (default coding pipeline), `deep_research`, `security_audit` — `install` seeds editable copies into `<repo>/.worc/flows/`. An operator can add more by dropping a `<task_type>.yaml` there (the file's own `flow.task_type` must match its name), or replace a built-in by editing its seeded copy. `.worc/flows/` is the only place flows resolve from, so a `task_type` with no file there fails the task at flow resolution, before any branch is created.

The task only **names** the flow — it never edits the graph, its nodes, or their providers/models. Picking a different built-in is the one task-side choice. To change _which_ stages run for a single task, the only per-task knob is disabling a node (see below); to reshape the pipeline or retune a stage's provider/model, edit the flow YAML under `.worc/flows/` (an operator/flow-authoring change, not a task field).

## Decomposition — split a large task

Whether a large task is broken into sequential subtasks (on one branch, one PR) is decided by the flow's `decomposition:` block and the planning stage's proposal, gated on whether decomposition is _permitted_ for the task. The gate defaults to the operator's `agents.decomposition.enabled` setting, but a task may override it with the optional `decomposition` field: `true` permits a split even when the global setting is off, `false` forbids one even when it is on, omitted defers to the global. The field only flips the gate — it never edits the graph or forces a split: the flow + planning (or an operator `subtasks:` manifest) still decide whether a split actually happens. Keep a task one coherent unit; if the work is genuinely large, say so in the Description and let planning propose a split (set `decomposition: true` if the global gate is off and you want this one task considered for splitting).

## `branch_name` — override the task branch

By default the orchestrator creates `<repo.branch_prefix>/<task-id>-<slug(title)>`, usually `worc/<task-id>-<slug>`. Set `branch_name` when the target project or customer requires a different branch convention:

```yaml
title: "Add a bounded retry budget to webhook delivery"
branch_name: "feature/ABC-123-webhook-retry-budget"
```

Omit it to use the default. The value is the full branch name, not a suffix. It must be a valid Git branch name, must not equal the base branch, and is validated before any branch/provider side effect. It changes only the branch/head used for push and PR creation; the PR title still comes from `title`. In `existing`/`current` branch mode (below) `branch_name` is ignored (there is nothing to name) — setting it there is a validation warning.

## `branch_mode` — run in an existing or current branch

By default the orchestrator creates a fresh task branch from the base branch (`branch_mode: new`). Two other modes let a task target a branch you already care about:

```yaml
branch_mode: existing # work in an already-existing branch
branch_ref: "feature/big-feature" # required for `existing`; the branch to check out
```

- `new` _(default)_ — create `<repo.branch_prefix>/<id>-<slug>` from the base branch (today's behavior).
- `existing` — check out and work in the branch named by `branch_ref` (required). It must already exist locally or on the remote — the orchestrator never auto-creates it. Use this to continue/refine a branch or chain several tasks onto one feature branch (they converge on a single reused PR).
- `current` — work in whatever branch the working tree is on, without creating, switching, requiring a clean tree, or pulling. A low-ceremony local experiment; a detached HEAD is rejected. Poor fit for unattended `watch` (it depends on your live checkout) — it warns there.

The per-task value overrides the global `repo.branch_mode` default. **Safety:** in `existing`/`current` the branch belongs to you, so the orchestrator never deletes, resets, or force-checkouts away from it; a branch-resetting fresh rerun in these modes is refused once the run produced work (use `rerun --continue`). A plain `rerun` of a run that failed **before any work** (no checkpoint — e.g. a transient pickup failure) instead restarts it **in place** on the branch, resetting nothing. `rerun --continue` tolerates the task's own uncommitted work once it has reached review/fixing/publish, and takes two recovery controls: `--reset-fix-budget` (grant a fresh fix budget when the fix loop hit `max_fix_cycles`, keeping the global backstop) and `--from <node>` (re-enter at a chosen node). Branch mode only governs _where_ git operations point; whether a `publish` node runs at all is still the flow's decision.

## `publish` — cap where a task stops (commit / push / PR)

`publish` is a **downgrade-only** cap on the flow's publish node, for a single task that should stop short of a PR without switching flows:

```yaml
publish: commit # stop after the local commit (no push, no PR)
```

- `commit` — commit locally, no push, no PR.
- `push` — commit and push the branch, no PR.
- `pull_request` — full publish (commit → push → PR).

It is a **cap, never an escalation**: the effective scope is `min(flow_policy, publish)`. On a flow whose graph has no publish node it is a no-op — it cannot manufacture publishing. Omit it to use the flow's own policy. (Edge case: when the working branch resolves to the PR base — e.g. `current` on `main` — the push runs but the PR is skipped, since a `main→main` PR is impossible.)

## `trust_level` — approval policy for the dangerous-diff gate

`trust_level` moves the threshold at which the mid-task dangerous-diff gate asks for approval. It is a per-task override of the global `security.trust_level`:

```yaml
trust_level: strict # gate on every deletion / dependency-manifest edit
```

- `strict` — gate on **any** tracked-file deletion/rename or dependency manifest/lock edit (ask before continuing).
- `auto` _(default)_ — routine in-repo deletions/renames/edits do **not** gate; only a `security.protected_paths` match asks.

It never lowers the hard ceiling (env-allowlist, the `--dangerously-*`/bypass ban, `cwd` containment) — it only changes _which_ diffs raise the gate. `protected_paths` is an operator-only config floor that always asks regardless of `trust_level`; there is no per-task equivalent. Leave `trust_level` unset unless a task genuinely needs a stricter (or looser) bar than the instance default.

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
