# Task Authoring Guide

Task files are the input contract between a human requester and **wastech-orchestrator**. A good task is specific enough for an agent to plan, implement, test, review, and summarize without asking for hidden context.

Tasks can be Markdown (`.md`) or JSON (`.json`). Markdown is the normal operator format and is what this guide focuses on.

> **Writing tasks with an AI agent?** A compact, agent-facing version of this guide ships in [`packaged/guide/`](../src/wastech_orchestrator/packaged/guide/README.md) and is copied to `<repo>/.worc/guide/` at `install` time. Point an agent at that local `.worc/guide/` folder and ask it to "write a task for this orchestrator." This document remains the full operator reference.

Use the packaged `templates/task.md` as the editable runtime template. Live task files belong in the repo's own `tasks/pending/` directory at the repository root (committed and pushed there) — that is how a teammate hands the orchestrator work over git. The `tasks/` lifecycle directories are git-tracked and intentionally not ignored; only the orchestrator's own `.worc/` home is gitignored.

The canonical task rules are enforced by the validation gate in the code (`src/wastech_orchestrator/task/`); see the [Functional Map](functional/index.md). For the meaning of all task-file fields, task statuses, and related vocabulary, see the [Glossary](glossary.md).

## Markdown Shape

A Markdown task starts with YAML front matter and then carries the task body:

```markdown
---
id: task-001
title: "Add login form validation"
contacts:
  - "@team-lead"
---

## Description

Describe what should change and where the user-visible behavior should end up.

## Acceptance criteria

- [ ] First expected behavior.
- [ ] Second expected behavior.

## Constraints

- Do not touch unrelated modules.
- No new dependencies without approval.
```

The validation gate requires:

- a leading `---` front matter block;
- `id`;
- `title`;
- a non-empty `## Description` section or non-empty body.

The gate rejects structurally unsafe tasks before branch creation or provider execution.

## Front Matter Fields

Allowed fields:

| Field | Required | Type | Meaning |
| --- | --: | --- | --- |
| `id` | yes | string | Stable task id. Must match `^[a-z0-9][a-z0-9._-]{0,63}$`. |
| `title` | yes | string | Short human-readable title. Used for the default branch slug, PR title, commit messages, and reports. |
| `task_type` | no | string | Selects the flow that runs the task. Omitted ⇒ `implementation` (the default coding pipeline). Built-ins: `implementation`, `deep_research`, `security_audit`; an operator flow in `<repo>/.worc/flows/<task_type>.yaml` may add others. An unknown `task_type` (no matching flow) fails the task before any branch is created. The task only _names_ the flow — it never edits the graph. |
| `branch_name` | no | string \| null | Full task branch override. Omitted ⇒ `<repo.branch_prefix>/<id>-<slug(title)>`; set it to match a project's branch naming policy. See [`branch_name`](#branch_name). |
| `auto_merge` | no | boolean | `true` requests auto-merge, `false` always opts out, omitted uses the instance default. A set per-task value wins outright over `git.auto_merge`. See [`auto_merge`](#auto_merge). |
| `prompt_audit` | no | boolean | `true` records each step's prompt + who for this task, `false` disables it, omitted uses config. Always overrides the global. See [`prompt_audit`](#prompt_audit). |
| `decomposition` | no | boolean | `true` permits a split for this task, `false` forbids one, omitted uses the instance default `agents.decomposition.enabled`. The task value wins; it only flips the gate (the flow + planning still decide whether a split happens). See [`decomposition`](#decomposition). |
| `contacts` | no | list of strings | Plain-text mentions in Telegram notifications/HITL prompts. |
| `depends_on` | no | list of strings | Other task ids that must be **merged** before this task may start (non-blocking, merge-gated scheduling). See [`depends_on`](#depends_on). |
| `subtasks` | no | list of strings | Operator-authored decomposition: ordered references to per-subtask spec files. Presence ⇒ the task runs as a split (one branch, one PR). See [`subtasks`](#subtasks-operator-authored-decomposition). |
| `nodes` | no | mapping | Per-node disable toggle, keyed by flow node id: `nodes.<node-id>.enabled: false` disables a node. `enabled` is the only valid sub-key. See [`nodes`](#nodes). |

The current validation gate rejects unknown fields fail-closed (`unknown_top_level_field`). Keep task front matter limited to the fields above. Provider, model, and reasoning are **flow-node concerns, not task fields** — each flow node declares its own `provider`/`model`/`reasoning`, and a task cannot repoint or override them (see [Provider, model, reasoning](#provider-model-reasoning-set-on-the-flow-not-the-task)).

## `id`

Valid ids:

```yaml
id: task-001
id: frontend.login-2
id: api_pagination
```

Invalid ids:

```yaml
id: Task-001        # uppercase
id: "task 001"      # whitespace
id: "../task-001"   # path traversal shape
id: "-task-001"     # leading separator
```

The orchestrator rejects invalid ids; it does not sanitize them.

## `branch_name`

By default the orchestrator creates a task branch from `repo.branch_prefix`, the task `id`, and a slug of `title`, for example:

```text
worc/task-001-add-login-form-validation
```

Set `branch_name` when a project or customer requires a different branch naming convention:

```yaml
branch_name: "feature/ABC-123-add-login-validation"
branch_name: "customer/acme/ABC-123-login-validation"
```

The value is the **full branch name**, not only a suffix. It is validated before branch creation and provider execution. It must be a safe Git branch ref, must not start with `-` or `refs/`, must not contain whitespace/control characters or Git-ref metacharacters, and must not equal `repo.base_branch`.

The PR title still comes from `title`; `branch_name` changes only the Git branch/head used for push and PR creation.

## Refinement (automatic)

Refinement-skip is deterministic — there is no task flag. The orchestrator skips refinement automatically when the task is **complete**: a non-empty `## Description` plus acceptance criteria. Provide acceptance criteria to skip refinement; omit them to let refinement enrich the task. Missing acceptance criteria never rejects the task — it makes refinement run.

When it runs, refinement is autonomous: it enriches the task with assumptions and acceptance criteria; it does not ask a human clarifying question.

## Decomposition (operator/flow-controlled)

Decomposition has two sources, both running the same execution machinery (subtasks run sequentially on one task branch → one PR):

1. **Agent-proposed** (this section): whether a large task is split is decided by the flow's `decomposition:` block and the planning stage's proposal, gated on whether decomposition is permitted for the task (default `agents.decomposition.enabled`, overridable per task — see [`decomposition`](#decomposition) below). Describe large scope in the `## Description` and let planning propose a split.
2. **Operator-authored** (the [`subtasks`](#subtasks-operator-authored-decomposition) field): when you already know the ordered units, list references to per-subtask spec files in the root task's `subtasks:`. The Core validates them with the same gate as the agent split. (The `decomposition` gate does not apply here — an explicit `subtasks:` manifest always runs as a split.)

## prompt_audit

Use `prompt_audit` to record, for auditing, **who** (which agent) received **what prompt** at each step of this task:

```yaml
prompt_audit: true
```

Values:

| Value   | Meaning                                           |
| ------- | ------------------------------------------------- |
| `true`  | Record the prompt audit for this task.            |
| `false` | Disable the prompt audit for this task.           |
| omitted | Use the global `prompt_audit` from `config.yaml`. |

The per-task value **always overrides** the global one (in both directions — there is no operator gate). When enabled, each agent-routed stage run is written as a self-contained, redacted JSON record under `<repo>/.worc/logs/<task-id>/prompt-audit/`, in chronological order, plus a combined `timeline.jsonl`. See [configuration.md](configuration.md#prompt_audit) for the file layout.

## decomposition

Use `decomposition` to override, for this one task, whether decomposition is **permitted** — without touching the global `agents.decomposition.enabled` config:

```yaml
decomposition: true
```

Values:

| Value   | Meaning                                                           |
| ------- | ----------------------------------------------------------------- |
| `true`  | Permit a split for this task even if the global gate is off.      |
| `false` | Forbid a split for this task even if the global gate is on.       |
| omitted | Use the global `agents.decomposition.enabled` from `config.yaml`. |

The per-task value **always wins** (in both directions — there is no operator gate), mirroring [`auto_merge`](#auto_merge) and [`prompt_audit`](#prompt_audit). It only flips the _gate_: whether a split actually happens is still decided by the flow's `decomposition:` block and the planning stage's proposal (an operator [`subtasks`](#subtasks-operator-authored-decomposition) manifest ignores this gate and always splits). The field never edits the graph or forces a split — it cannot change `max_subtasks`, the provider, or any security setting. It is unrelated to the old `decompose` flag (removed in `schema_version` 11), which forced a split.

## Provider, model, reasoning (set on the flow, not the task)

A task **cannot** choose a provider, model, or reasoning level for any stage. Provider routing is node-based: each flow node declares its own `provider:` — or, when omitted, defaults to the operator's single global primary provider (the one with `primary: true` in `config.yaml` under `agents.providers`). Model and reasoning live on the flow node as well. A task has no `agents`, `model`, or `reasoning` field, and cannot repoint a stage's provider or change commands, `extra_args`, credentials, sandbox, or any security policy.

> **Tasks cannot supply or weaken checks.** The quality-gate commands are an operator/infrastructure concern, authored only in `config.yaml` under `checks.command_sets` (see [configuration.md](configuration.md#checks)). A task file has no field to add, replace, relax, or re-select a check — which sets run is a deterministic function of the task diff, not task content — keeping the quality gate independent of the task.

## `contacts`

`contacts` is a list of strings:

```yaml
contacts:
  - "@team-lead"
  - "frontend-team"
```

When Telegram is configured, the orchestrator renders these values as plain-text mentions in terminal notifications and HITL prompts. They do not choose the Telegram chat, grant access, alter routing, or change approval scope; the numeric chat id remains operator-controlled configuration.

## `depends_on`

`depends_on` lists other task ids that must be **merged** before this task may start:

```yaml
depends_on: [task-a, task-b] # this task may start only after task-a AND task-b have merged
```

This is for tasks that build on each other: each task branches from a freshly pulled `base_branch`, so a task that needs another's work must wait until that work is on `base_branch`. (Distinct from a decomposed task's per-subtask `depends_on`, which orders subtasks _within_ one task.)

Scheduling is **non-blocking and merge-gated**. Under `watch`, a pending task is **eligible** only when _every_ id in `depends_on` is merged; while a dependency is unmerged the scheduler **skips** the dependent and runs other eligible tasks instead — the single slot never idles on CI. The dependent is re-evaluated on each later tick, and once its dependencies merge it branches from a `base_branch` that now includes them.

"Merged" means, per dependency:

- it has a PR that is **MERGED** (probed read-only via `gh pr view`); or
- it ran in local-commit mode (no PR) and reached terminal `DONE` — its commits are already on `base_branch`.

An open/armed PR (e.g. GitHub-native auto-merge waiting on checks) counts as **not yet merged** — the dependent waits.

Rules and edge cases:

- **Deps must already exist.** A dependency id must resolve to a known task — pending in the queue, or a terminal record — when the dependent is evaluated. Add the dependency before (or alongside) the dependent. A `depends_on` id that matches no known task is treated as a typo and the dependent is **rejected** (fail-closed, `invalid_depends_on`, moved to `tasks/rejected/`).
- **Cycles and self-reference are rejected** the same way (`A → B → A`, or a task depending on itself).
- **An unsatisfiable dependency waits forever.** If a dependency failed, went `manual_action_required`, or had its PR closed unmerged, the dependent stays pending and is skipped every pass — _indefinitely_, until you remove or fix the dependency. The orchestrator never auto-fails a dependent (an advisory log line records the wait).
- **Explicit `run` is refused, not skipped.** `worc run <file>` of a task whose dependencies are not merged exits non-zero with a controlled message rather than building on a stale base. Use `watch` for dependency-gated scheduling.
- Shape: a list of non-empty strings (validated at the gate).

## `subtasks` (operator-authored decomposition)

When you already know how to carve a large change into ordered units, write **one root task** that carries the shared context and references the per-subtask spec files in `subtasks:`. The orchestrator runs them exactly like an accepted agent decomposition — sequentially, on one branch, into **one PR** — but the split is yours, not the planning agent's. (Distinct from `depends_on`, which links _separate_ tasks each producing their own PR.)

Root task:

```yaml
---
id: epic-checkout
title: "Rework checkout into a multi-step flow"
subtasks: # ordered references; presence ⇒ operator-authored decomposition
  - subtasks/01-cart-model.md
  - subtasks/02-payment-step.md
  - subtasks/03-confirmation.md
---
## Description

Shared context for the whole change — the once-per-task framing every subtask inherits.
```

Each referenced file is a **reduced spec, not a task** (no `id`, so it can never run standalone):

```yaml
---
title: "Add the cart line-item model"
depends_on: [] # optional; slugs of earlier subtasks (default: none)
---

## Acceptance criteria

- [ ] Concrete, testable unit-level criteria.
```

- `slug` defaults to `slugify(title)` (override with an explicit `slug:`); it names the immutable `NN-<slug>.md` spec. The file body is materialized **verbatim** and injected into the edit nodes — write the per-subtask instructions however you like.
- `depends_on` lists **slugs of earlier subtasks**; a forward, self, or unknown reference is rejected. The Core applies the same linear/`max_subtasks` gate as the agent split, so you cannot weaken it.
- **Where they live:** put subtask files in a **subfolder** (e.g. `tasks/pending/subtasks/…`). The scheduler scans only the top level of `tasks/pending/`, so subtask files there never run as standalone tasks; a path that lands beside the root is rejected.
- **Path rules (fail-closed):** each reference must be repo-relative with no `..`/absolute/traversal and resolve under the task directory.
- **Rejected fail-closed before any branch** (quarantined to `tasks/rejected/` with a `validation_report.json`): a malformed/missing subtask file, a bad path, fewer than 2 or more than `max_subtasks` units, a forward dependency, or a `task_type` whose flow declares no `decomposition:` block (`flow_cannot_decompose`).

## `auto_merge`

`auto_merge` is a publishing-policy choice — whether the orchestrator merges the PR without waiting for a human review:

```yaml
auto_merge: true
```

Values:

| Value   | Meaning                                           |
| ------- | ------------------------------------------------- |
| `true`  | Auto-merge this task's PR (skip human review).    |
| `false` | Always opt out, even if the instance defaults on. |
| omitted | Use the instance default `git.auto_merge`.        |

A set per-task `auto_merge` **wins outright** over the instance default `git.auto_merge` — there is no `git.auto_merge_allow_per_task` gate. Auto-merge skips the human PR review, and the task author owns that decision (the same trusted operator who owns `config.yaml`). It is a publishing-policy choice, not a security weakening.

## `nodes`

The `nodes` block carries the per-node **disable** toggle, `enabled: false` — the one surviving per-node knob. Keys are flow **node ids** (the ids in the task's resolved flow); `enabled: false` disables a node so the engine skips it and takes its forward edge:

```yaml
nodes:
  planning:
    enabled: false # write a stub plan and run as a single unit (no decomposition)
  testing:
    enabled: false # bypass the Check Runner (e.g. a repo with no test suite)
```

Any node present in the task's resolved flow may be disabled — there is no fixed allowlist. **Which nodes are safe to disable is the operator's responsibility** (they author the flow and run the tasks). The node ids above (`planning`, `testing`, `review`, `fixing`, …) are those of the default `implementation` flow; a custom flow exposes its own node ids (e.g. `code_review`). `refinement` is skipped automatically by completeness, not by a `nodes` entry. The whole-task **summary** is not a graph node — it is written by the constant supervisor layer at task close (see [configuration.md](configuration.md#supervisor)). Node-disable is **per-task only** — there is no global config knob (to drop a node everywhere, remove it from the flow).

What disabling the default-flow nodes does: `planning` → stub plan, single unit; `testing` → straight to review (no checks); `review` → commit with no agent quality gate; `fixing` → the test/review fix loop runs as a no-op to its cap, then `manual_action_required`. Every disable is recorded in `state.db` (`node_runs.skipped`) and listed in the PR body / summary.

**Failure mode (controlled).** If `nodes` names an id that is **not** in the task's resolved flow, the task ends `failed` (moved to `tasks/failed/`) with a clear message — checked at flow resolution, before any branch/PR side effect. The same controlled failure catches a disabled node whose skip cannot route to a forward edge in that flow.

Rules:

- `enabled` is the **only** valid sub-key under `nodes.<node-id>`. Any other sub-key (including `model`/`reasoning`, which are flow-node concerns) is rejected fail-closed (`invalid_node_override`);
- `enabled` must be a boolean; the `nodes` block must be a mapping and each value a mapping (or null);
- the gate validates **shape only** — it cannot see the flow, so node-id existence is checked later, at flow resolution (the failure mode above), not at the gate.

## Body Sections

Use these sections by default:

```markdown
## Description

What should be changed, why it matters, and what user/system behavior should exist afterward.

## Acceptance criteria

- [ ] Observable behavior that must work.
- [ ] Tests or checks that should pass.

## Constraints

- Areas that must not be touched.
- Compatibility, dependency, migration, or rollout constraints.
```

Good acceptance criteria are testable. Prefer:

```markdown
- [ ] `GET /users?page=2` returns the second page using existing pagination metadata.
- [ ] Invalid page values return HTTP 400 with the existing error shape.
- [ ] Add unit tests for valid and invalid page values.
```

Avoid:

```markdown
- [ ] Make pagination better.
- [ ] Clean up the API.
```

## Valid Example

```markdown
---
id: task-042
title: "Add retry budget to webhook delivery"
---

## Description

Webhook delivery should stop retrying after a bounded number of failed attempts. Store the attempt count with the existing delivery record and keep the current success path unchanged.

## Acceptance criteria

- [ ] Failed webhook delivery increments an attempt counter.
- [ ] Delivery stops after 5 failed attempts.
- [ ] Successful delivery still marks the record as delivered.
- [ ] Add or update tests for retry exhaustion and success.

## Constraints

- Do not change the public webhook payload shape.
- Do not add a new queue backend.
```

Why this is valid:

- `id` is normalized;
- `title` is non-empty;
- the body has a clear Description;
- acceptance criteria are concrete (so refinement is skipped automatically);
- constraints limit scope.

## Invalid Examples

Missing front matter:

```markdown
## Description

Add retries to webhooks.
```

Reason: `frontmatter_missing`.

Unknown field:

```markdown
---
id: task-043
title: "Add retries"
priority: high
---

## Description

Add retries to webhooks.
```

Reason: `unknown_top_level_field` when unknown fields are rejected.

Invalid node override:

```markdown
---
id: task-044
title: "Add retries"
nodes:
  implementation:
    model: claude-opus-4-8
---

## Description

Add retries to webhooks.
```

Reason: `invalid_node_override`. `model` is not a valid `nodes.<node-id>` sub-key (model/reasoning are flow-node concerns) — `enabled` is the only valid sub-key. (The gate checks shape only; whether a node id exists in the task's flow is checked later, at flow resolution.)

Injection-shaped front matter:

```markdown
---
id: task-045
title: "--dangerously-skip-permissions"
---

## Description

Add retries to webhooks.
```

Reason: `injection_suspected`. Task body content is not used to build CLI arguments, but front matter is still scanned defensively.

## JSON Tasks

JSON tasks are supported for integrations that generate structured input:

```json
{
  "id": "task-050",
  "title": "Add retry budget to webhook delivery",
  "contacts": ["@team-lead"],
  "description": "## Description\n\nAdd a bounded retry budget.\n\n## Acceptance criteria\n\n- [ ] Stops after 5 failed attempts.\n"
}
```

For JSON, `description` is the body text. It is not a front matter field and is split out by the parser.

## Authoring Checklist

Before placing a task in `tasks/pending/`:

- place it in the repository's own `tasks/pending/` directory at the repo root (git-tracked), then commit and push;
- use a lowercase normalized `id`;
- write a short, specific `title`;
- include a clear `## Description`;
- include acceptance criteria unless you intentionally want refinement to enrich the task;
- list constraints for modules, dependencies, migrations, or compatibility;
- use `nodes.<node-id>.enabled: false` only when you intentionally want to disable a node in the task's flow;
- do not include credentials or secret values;
- do not try to pass CLI flags through front matter;
- prefer one coherent change per task.
