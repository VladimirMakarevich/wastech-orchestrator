# When to use what

A decision guide for the optional knobs. Defaults are almost always right — reach for these only when the task genuinely needs them. See [README.md](README.md) for the hard rules each setting must obey.

## `run` vs `watch` (how the operator runs your task)

You usually do not choose this — the operator does — but it affects where your task file goes:

- **`run <task-file>`** processes exactly one task file, end to end. The argument is a **path** to the file (e.g. `tasks/pending/my-task.md`), not a task id.
- **`watch`** polls the `tasks/pending/` folder and processes tasks promoted there, looping with periodic git sync.

A live task belongs in the repo's own `tasks/pending/` directory (committed and pushed there) — that is how a teammate hands work to a watching orchestrator. Compose the file in the `tasks/preparing/` staging folder first (the watcher never scans it), then `worc promote <id>` moves it into `tasks/pending/` once it is complete, so a half-written draft is never picked up mid-edit.

## `task_type` — choose the flow

`task_type` selects which **flow** (the fixed pipeline of stages) carries the task. Omit it and the task runs the default `implementation` flow (`planning → implementation → testing → review → fixing → documentation → publish`, with `refinement` skipped automatically when the task is complete) — what almost every coding task wants. Set it to run a different flow:

```yaml
task_type: deep_research # or: security_audit, implementation (default), or a custom operator flow
```

Built-in flows: `implementation` (default coding pipeline), `deep_research`, `security_audit`, and the content-authoring set `content_chapter` / `content_translate` / `blog_article` / `blog_article_revise` — `install` seeds editable copies of all of them into `<repo>/.worc/flows/`, plus the `tool` executables they reference into `.worc/tools/`. (`merge` ships too but is never task-dispatched: the orchestrator selects it via `git.merge_flow` when merging the base branch into a finished task branch conflicts.) An operator can add more by dropping a `<task_type>.yaml` there (the file's own `flow.task_type` must match its name), or replace a built-in by editing its seeded copy. `.worc/flows/` is the only place flows resolve from, so a `task_type` with no file there fails the task at flow resolution, before any branch is created.

The task only **names** the flow — it never edits the graph. Picking a different built-in is the main task-side choice. To change _which_ stages run for a single task, disable a node (see below); to overlay one node's model/reasoning/provider for a single run, use the same `nodes` block (see below); to reshape the pipeline or retune a stage for every task, edit the flow YAML under `.worc/flows/` (an operator/flow-authoring change, not a task field).

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

The per-task value overrides the global `repo.branch_mode` default. **Safety:** in `existing`/`current` the branch belongs to you, so the orchestrator never deletes, resets, or force-checkouts away from it — and by default terminal cleanup leaves the tree on that branch rather than switching back to base (`repo.checkout_base_on_cleanup` overrides this); a branch-resetting fresh rerun in these modes is refused once the run produced work (use `rerun --continue`). A plain `rerun` of a run that failed **before any work** (no checkpoint — e.g. a transient pickup failure) instead restarts it **in place** on the branch, resetting nothing. `rerun --continue` tolerates the task's own uncommitted work once it has reached review/fixing/publish, and takes two recovery controls: `--reset-fix-budget` (grant a fresh fix budget when the fix loop hit `max_fix_cycles`, keeping the global backstop) and `--from <node>` (re-enter at a chosen node). Branch mode only governs _where_ git operations point; whether a `publish` node runs at all is still the flow's decision.

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

Disable a node only when it adds no value for this task. Keys are flow **node ids**; any node in the task's resolved flow may be disabled. The ids below are the default `implementation` flow's; a custom flow exposes its own (e.g. `code_review`). `refinement` is skipped automatically when the task is already complete (see "Refinement" below) — with one flow-specific exception: `deep_research`'s `refinement` is a *scoping* pass that carries no completeness predicate and runs on every task, so disabling it here is the only way to skip it.

Disabling a `checks` node is also the sanctioned way to make a quality gate not run for one task. Do not reach for a set's `skip_if_unavailable` instead: that only turns a missing toolchain into a loud skip, and a set that was the *only* one the diff selected then leaves the gate with nothing run — which parks the task at `manual_action_required`, the same place the launch failure would have.

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

**Disabling `review` is high-risk** — it removes the only agent quality gate before commit/PR. There is no config gate for it (no `agents.allow_review_skip`): which nodes are safe to disable is the operator's flow-authoring responsibility. Node-disable is per-task only. Naming an id absent from the task's flow ends the task `failed` (a controlled error at flow resolution).

Also note what disabling **does not** reach: the whole-task summary is not a graph node, so no `nodes` entry removes it. Removing that oversight layer is the operator's config switch `supervisor.enabled: false`, after which the pull-request body is rendered deterministically from the run's own recorded facts.

## Provider / model / reasoning — an overlay, not a redesign

A node's **defaults** are decided by the **flow** (each flow node declares its own `provider`/`model`/`reasoning`, or falls back to the operator's global primary). A task may **overlay** them for one run, inside the same `nodes` block:

```yaml
nodes:
  implementation: { model: claude-opus-5, reasoning: high }
  review: { provider: codex }
```

This exists so one default flow can cover several model/effort/provider variants without a separate flow file per combination — useful for an experiment or a one-off run. Three things bound it:

- **Best-effort, never fatal.** A `provider` must be in `agents.allowed` and a `reasoning` must be supported by the resolved provider; an invalid value — or an overlay on a node that runs no agent, such as `testing` or `publish` — is logged as a warning and **skipped**, and the node runs on the flow's declared value. `model` is passed through unchecked. That is deliberate: an unattended `watch` queue must never be blocked by a typo in one task.
- **It changes the executor, nothing else.** A task can never change provider commands, credentials, sandbox or approval settings, `extra_args`, or any security policy.
- **It does not edit the graph.** Reshaping the pipeline, or retuning a node for every task, is still an operator/flow change under `.worc/flows/`.

The effective post-override model, reasoning, and provider appear in the prompt audit (`logs/<task-id>/prompt-audit/`).

## `auto_merge` — danger

`auto_merge: true` requests that the orchestrator merge the PR automatically after publishing, **bypassing human merge**. A per-task value wins outright over the instance default `git.auto_merge` — there is no separate operator gate, because the task author is the same trusted operator who owns `config.yaml`. Leave it unset unless you have an explicit reason and know auto-merge is safe for this repository; skipping the human PR review is your call.

## Refinement — skipped automatically when complete

You cannot flag a task to skip refinement. The orchestrator skips it automatically when the task looks complete — completeness needs a non-empty Description **plus** acceptance criteria. Provide acceptance criteria when you want to skip refinement; omit them to let the refinement stage enrich an under-specified task (missing criteria never rejects the task).

## Where task files live

There is a single canonical layout. Compose your task file in the repo's `tasks/preparing/` staging folder — the watcher never scans it, so an in-progress draft is invisible to the daemon — then run `worc promote <id>` (or the `promote` verb inside `worc shell`) to move it atomically into `tasks/pending/`, where it is git-tracked, committed, and pushed and a watching orchestrator picks it up. (`enqueue <file>` in the shell is a fast path for an already-complete external file: it lands straight in `tasks/pending/`, atomically.) Everything the orchestrator generates lives under a single gitignored `<repo>/.worc/` home; only the `tasks/` lifecycle directories (`preparing`/`pending`/`done`/`failed`) stay at the repo root and are tracked. On a terminal outcome the orchestrator commits the task file and its `<id>.summary.md` as an audit trail: `done` moves it to `tasks/done/`, `failed` to `tasks/failed/`, and a rejected task is quarantined under `.worc/tasks/rejected`.

One-shot verbs such as `rerun` execute on a worker when invoked from `worc shell`, so the console's log tail remains live and synchronous transports such as Telegram behave exactly as they do when the verb is run directly.

## When a task needs manual action

A task that ends in **`manual_action_required`** (a stuck fix loop, an evaluator that could not run, a blocked merge) is the one terminal that keeps its file **in `tasks/pending/`** — its branch is preserved for you to review and publish, not discarded. The watcher deliberately leaves it there and does **not** re-pick it: it never re-runs an id that has already reached a terminal state, so the task does not churn into a spurious `failed` on the next tick. You resolve it yourself, on your schedule:

- `worc rerun <id> --continue` — re-enter from the saved checkpoint (see `branch_mode` above for the `--reset-fix-budget` / `--from` recovery controls).
- `worc finalize <id> --as done|failed|abandoned` — close it out: `done`/`failed` move the file to the matching lifecycle folder, `abandoned` leaves it in place (still `manual_action_required` in the ledger).

## `contacts` and Telegram

`contacts` is a list of plain-text strings rendered as mentions in Telegram notifications and human-in-the-loop prompts:

```yaml
contacts:
  - "@team-lead"
```

They are cosmetic mentions only. They do **not** choose the Telegram chat, grant access, change routing, or alter approval scope — the chat id stays operator-controlled configuration.
