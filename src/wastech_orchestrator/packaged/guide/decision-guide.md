# When to use what

A decision guide for the optional knobs. Defaults are almost always right — reach for these only when the task genuinely needs them. See [README.md](README.md) for the hard rules each setting must obey.

## `run` vs `watch` (how the operator runs your task)

You usually do not choose this — the operator does — but it affects where your task file goes:

- **`run <task-file>`** processes exactly one task file, end to end. The argument is a **path** to the file (e.g. `tasks/pending/my-task.md`), not a task id.
- **`watch`** polls the `tasks/pending/` folder and processes tasks promoted there, looping with periodic git sync.

A live task belongs in the repo's own `tasks/pending/` directory (committed and pushed there) — that is how a teammate hands work to a watching orchestrator. Compose the file in the `tasks/preparing/` staging folder first (the watcher never scans it), then `worc promote <id>` moves it into `tasks/pending/` once it is complete, so a half-written draft is never picked up mid-edit.

## `task_type` — choose the flow

`task_type` selects which **flow** (the fixed pipeline of stages) carries the task. Omit it and the task runs the default `implementation` flow (`refinement → planning → implementation → testing → review → fixing → documentation → publish`, with `refinement` skipped automatically when the task is complete) — what almost every coding task wants. Set it to run a different flow:

```yaml
task_type: deep_research # or: security_audit, implementation (default), or a custom operator flow
```

Built-in flows: `implementation` (default coding pipeline), `deep_research`, `security_audit`, `merge`, `blog_article`, `blog_article_revise`, `content_chapter`, `content_translate` — `install` seeds editable copies into `<repo>/.worc/flows/`. An operator can add more by dropping a `<task_type>.yaml` there (the file's own `flow.task_type` must match its name), or replace a built-in by editing its seeded copy. `.worc/flows/` is the only place flows resolve from, so a `task_type` with no file there fails the task at flow resolution, before any branch is created.

The task only **names** the flow — it never edits the graph: the stages, their edges, and their gating are the flow's. Picking a different built-in is the main task-side choice. Two narrow per-task knobs act on the resolved flow, both keyed by node id: disabling a node and overriding a node's provider/model/reasoning (see below). To reshape the pipeline itself, or to retune a stage durably, edit the flow YAML under `.worc/flows/` (an operator/flow-authoring change, not a task field).

## Decomposition — split a large task

Whether a large task is broken into sequential subtasks (on one branch, one PR) is decided by the flow's `decomposition:` block and the planning stage's proposal, gated on whether decomposition is _permitted_ for the task. The gate defaults to the operator's `agents.decomposition.enabled` setting, but a task may override it with the optional `decomposition` field: `true` permits a split even when the global setting is off, `false` forbids one even when it is on, omitted defers to the global. The field only flips the gate — it never edits the graph or forces a split: the flow + planning (or an operator `subtasks:` manifest) still decide whether a split actually happens. Keep a task one coherent unit; if the work is genuinely large, say so in the Description and let planning propose a split (set `decomposition: true` if the global gate is off and you want this one task considered for splitting).

## `branch_name` — override the task branch

By default the orchestrator creates `<repo.branch_prefix>/<epoch>-<task-id>-<slug(title)>`, usually `worc/<epoch>-<task-id>-<slug>` — the epoch is the attempt's Unix timestamp, so a fresh rerun never collides with the previous attempt's branch (the slug is truncated, or dropped entirely, to keep the name within the branch-name length budget). Set `branch_name` when the target project or customer requires a different branch convention:

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

**Disabling `review` is high-risk** — it removes the only agent quality gate before commit/PR. There is no config gate for it (no `agents.allow_review_skip`): which nodes are safe to disable is the operator's flow-authoring responsibility. Node-disable is per-task only (`nodes.<node-id>.enabled: false`); the valid per-node keys are `enabled`, `model`, `reasoning`, and `provider` — any other sub-key rejects the task (`invalid_node_override`). Naming an id absent from the task's flow ends the task `failed` (a controlled error at flow resolution).

## Provider / model / reasoning — a per-run overlay, not a redesign

Which provider runs a stage, and with which model and reasoning effort, is the **flow's** decision: each node declares its own `provider`/`model`/`reasoning`, falling back to the operator's global provider defaults. A task may overlay that for one run, per node:

```yaml
nodes:
  review:
    provider: claude # this run only; the flow's declaration is unchanged
    reasoning: high
```

The resolution chain is task node override → flow node declaration → provider config default. The overrides are deliberately **best-effort**: the validation gate checks only that each is a non-empty string, and one the resolved flow or config cannot honor (a `provider` outside `agents.allowed`, a `reasoning` the provider does not support) is warned and skipped at run time, falling back to the flow's value — the task is never aborted for it. `model` is passed through unchecked, because model names have no reliable tier ordering. Use this to run one task at a different effort or on the other agent; when a stage needs a stronger model _every_ time, change the flow YAML instead — that is the durable fix.

## `auto_merge` — danger

`auto_merge: true` requests that the orchestrator merge the PR automatically after publishing, **bypassing human merge**. A per-task value wins outright over the instance default `git.auto_merge` — there is no separate operator gate, because the task author is the same trusted operator who owns `config.yaml`. Leave it unset unless you have an explicit reason and know auto-merge is safe for this repository; skipping the human PR review is your call.

## Refinement — skipped automatically when complete

You cannot flag a task to skip refinement. The orchestrator skips it automatically when the task looks complete — completeness needs a non-empty Description **plus** acceptance criteria. Provide acceptance criteria when you want to skip refinement; omit them to let the refinement stage enrich an under-specified task (missing criteria never rejects the task).

## Where task files live

There is a single canonical layout. Compose your task file in the repo's `tasks/preparing/` staging folder — the watcher never scans it, so an in-progress draft is invisible to the daemon — then run `worc promote <id>` (or the `promote` verb inside `worc shell`) to move it atomically into `tasks/pending/`, where it is git-tracked, committed, and pushed and a watching orchestrator picks it up. (`enqueue <file>` in the shell is a fast path for an already-complete external file: it lands straight in `tasks/pending/`, atomically.) Everything the orchestrator generates lives under a single gitignored `<repo>/.worc/` home; only the `tasks/` lifecycle directories (`preparing`/`pending`/`done`/`failed`) stay at the repo root and are tracked. On a terminal outcome the orchestrator commits the task file and its `<id>.summary.md` as an audit trail: `done` moves it to `tasks/done/`, `failed` to `tasks/failed/`, and a rejected task is quarantined under `.worc/tasks/rejected`.

One-shot verbs such as `rerun` execute on a worker when invoked from `worc shell`, so the console's log tail remains live and synchronous transports such as Telegram behave exactly as they do when the verb is run directly. The console forwards `--non-interactive` to `rerun`, `down`, and `restart` (a confirmation prompt would fight the REPL's own stdin reader), so a verb that would have asked instead refuses with instructions: answer `rerun` up front with `-y`, and a busy daemon's `down`/`restart` with `--force` or `--force-full`.

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
