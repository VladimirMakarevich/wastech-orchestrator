# Task Authoring Guide

Task files are the input contract between a human requester and **wastech-orchestrator**. A good task is specific enough for an agent to plan, implement, test, review, and summarize without asking for hidden context.

Tasks can be Markdown (`.md`) or JSON (`.json`). Markdown is the normal operator format and is what this guide focuses on.

> **Writing tasks with an AI agent?** A compact, agent-facing version of this guide ships in [`docs/worc/`](worc/README.md) and is copied to `<repo>/.worc/guide/` at `install` time. Point an agent at that local `.worc/guide/` folder and ask it to "write a task for this orchestrator." This document remains the full operator reference.

Use the packaged `templates/task.md` as the editable runtime template. A completed example is kept at [`docs/examples/task-001.example.md`](examples/task-001.example.md). Live task files belong in the repo's own `tasks/pending/` directory at the repository root (committed and pushed there) — that is how a teammate hands the orchestrator work over git. The `tasks/` lifecycle directories are git-tracked and intentionally not ignored; only the orchestrator's own `.worc/` home is gitignored.

The canonical task rules are enforced by the validation gate in the code (`src/wastech_orchestrator/task/`); see the [Functional Map](functional/index.md).

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
| `title` | yes | string | Short human-readable title. Used for branch slugging and reports. |
| `task_type` | no | string | Selects the flow that runs the task. Omitted ⇒ `implementation` (the default coding pipeline). Built-ins: `implementation`, `deep_research`, `security_audit`; an operator flow in `<repo>/.worc/flows/<task_type>.yaml` may add others. An unknown `task_type` (no matching flow) fails the task before any branch is created. The task only _names_ the flow — it never edits the graph. |
| `pr_title` | no | string \| null | PR title override; when set, used verbatim as the pull-request title instead of `title`. |
| `auto_merge` | no | boolean | `true` requests auto-merge, `false` always opts out, omitted uses the instance default. A set per-task value wins outright over `git.auto_merge`. See [`auto_merge`](#auto_merge). |
| `prompt_audit` | no | boolean | `true` records each step's prompt + who for this task, `false` disables it, omitted uses config. Always overrides the global. See [`prompt_audit`](#prompt_audit). |
| `contacts` | no | list of strings | Plain-text mentions in Telegram notifications/HITL prompts. |
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

## Refinement (automatic)

Refinement-skip is deterministic — there is no task flag. The orchestrator skips refinement automatically when the task is **complete**: a non-empty `## Description` plus acceptance criteria. Provide acceptance criteria to skip refinement; omit them to let refinement enrich the task. Missing acceptance criteria never rejects the task — it makes refinement run.

When it runs, refinement is autonomous: it enriches the task with assumptions and acceptance criteria; it does not ask a human clarifying question.

## Decomposition (operator/flow-controlled)

Decomposition is not a task knob. Whether a large task is split is decided by the operator's `agents.decomposition.enabled`, the flow's `decomposition:` block, and the planning stage's proposal — not by a front-matter flag. Describe large scope in the `## Description` and let planning propose a split. When a split is accepted, subtasks run sequentially on one task branch and produce one PR for the parent task.

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

## Provider, model, reasoning (set on the flow, not the task)

A task **cannot** choose a provider, model, or reasoning level for any stage. Provider routing is node-based: each flow node declares its own `provider:` — or, when omitted, defaults to the operator's single global primary provider (the one with `primary: true` in `config.yaml` under `agents.providers`). Model and reasoning live on the flow node as well. A task has no `agents`, `model`, or `reasoning` field, and cannot repoint a stage's provider or change commands, `extra_args`, credentials, sandbox, or any security policy.

> **Tasks cannot supply or weaken checks.** The quality-gate commands are an operator/infrastructure concern resolved from `config.yaml` and the repository at install/preflight time (see [configuration.md](configuration.md#checks)). A task file has no field to add, replace, or relax a check, and cannot change the discovery policy — this keeps the quality gate independent of task content.

## `contacts`

`contacts` is a list of strings:

```yaml
contacts:
  - "@team-lead"
  - "frontend-team"
```

When Telegram is configured, the orchestrator renders these values as plain-text mentions in terminal notifications and HITL prompts. They do not choose the Telegram chat, grant access, alter routing, or change approval scope; the numeric chat id remains operator-controlled configuration.

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
