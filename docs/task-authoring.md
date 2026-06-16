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
refined: false
decompose: false
agents:
  planning: claude
  implementation: claude
  review: codex
contacts:
  - "@team-lead"
model: null # optional: override provider model for this task
reasoning: null # optional: low | medium | high | xhigh | max
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
| `refined` | no | boolean | Set `true` when the task is already complete enough to skip refinement. |
| `decompose` | no | boolean | `true` forces the decomposition gate, `false` disables it, omitted uses config. |
| `agents` | no | mapping | Per-stage provider override. |
| `contacts` | no | list of strings | Plain-text mentions in Telegram notifications/HITL prompts. |
| `model` | no | string or null | Override the provider model for every stage of this task (e.g. `claude-opus-4-8`). |
| `reasoning` | no | string or null | Override the reasoning effort level for this task: `low`, `medium`, `high`, `xhigh`, or `max`. |
| `stages` | no | mapping | Per-stage overrides: `model`/`reasoning` (precedence over the task-wide values) and `enabled: false` to skip a stage. See [`stages`](#stages). |
| `pr_title` | no | string \| null | PR title override; when set, used verbatim as the pull-request title instead of `title`. |

The current validation gate rejects unknown fields fail-closed. Keep task front matter limited to the fields above.

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

## `refined`

Use `refined: true` only when the task already has enough detail for planning:

```yaml
refined: true
```

When `refined` is omitted or `false`, the Core still skips refinement if the task is classified as complete. In the current implementation, completeness requires a non-empty description plus acceptance criteria. Missing acceptance criteria does not reject the task; it makes refinement run.

Planned v1 refinement is autonomous. It enriches the task with assumptions and acceptance criteria; it does not ask a human clarifying question.

## `decompose`

Use `decompose` for large tasks:

```yaml
decompose: true
```

Values:

| Value   | Meaning                                                |
| ------- | ------------------------------------------------------ |
| `true`  | Force the decomposition gate for this task.            |
| `false` | Disable decomposition for this task.                   |
| omitted | Use `agents.decomposition.enabled` from `config.yaml`. |

Planned v1 decomposition is still sequential: accepted subtasks run one after another on one task branch and produce one PR for the parent task.

## `agents`

Use `agents` to override the provider for specific agent-routed stages:

```yaml
agents:
  refinement: claude
  planning: claude
  implementation: codex
  review: codex
  fixing: claude
  summary: claude
```

Allowed stage keys:

```text
refinement, planning, implementation, review, fixing, summary
```

Allowed provider values:

```text
codex, claude
```

Rules:

- the provider must be listed in `agents.allowed`;
- the provider must have an `agents.providers.<provider>` config entry;
- `testing` and `publishing` cannot be overridden here;
- task overrides cannot change commands, `extra_args`, credentials, sandbox, or any security policy.

> **Tasks cannot supply or weaken checks.** The quality-gate commands are an operator/infrastructure concern resolved from `config.yaml` and the repository at install/preflight time (see [configuration.md](configuration.md#checks)). A task file has no field to add, replace, or relax a check, and cannot change the discovery policy — this keeps the quality gate independent of task content.

## `contacts`

`contacts` is a list of strings:

```yaml
contacts:
  - "@team-lead"
  - "frontend-team"
```

When Telegram is configured, the orchestrator renders these values as plain-text mentions in terminal notifications and HITL prompts. They do not choose the Telegram chat, grant access, alter routing, or change approval scope; the numeric chat id remains operator-controlled configuration.

## `model`

Override the provider model for every agent stage of this specific task:

```yaml
model: "claude-opus-4-8"
```

When set, this replaces whatever model is configured under `agents.providers.<provider>.model` for all stages of this task. Use `null` or omit the field to use the globally configured model.

## `reasoning`

Override the reasoning effort level for this specific task:

```yaml
reasoning: "xhigh"
```

Valid values: `low`, `medium`, `high`, `xhigh`, `max`.

- For **Claude Code** (CLI v2.1+), this maps to `--effort <level>`, which implicitly enables adaptive thinking. `xhigh` requires Opus 4.7+ or Fable 5; using it on an incompatible model exits non-zero → `unsupported_version` → infrastructure fallback.
- For **Codex**, this maps to `--reasoning-effort`; Codex supports up to `xhigh` natively, and `max` (Claude-only) is clamped to `xhigh`.

When omitted, the global `agents.providers.<provider>.reasoning` value from `config.yaml` is used. When that is also absent, no reasoning flag is passed to the CLI.

## `stages`

Different stages have different cognitive demands: `planning` and `review` benefit from a capable, high-reasoning model, while `implementation`, `fixing`, and `summary` are usually fine on a lighter/cheaper one. Use `stages` to set `model` and/or `reasoning` per stage instead of one task-wide value:

```yaml
model: claude-sonnet-4-6 # task-wide fallback for stages not listed below
reasoning: low
stages:
  planning:
    model: claude-opus-4-8
    reasoning: high
  review:
    reasoning: high # only reasoning overridden — model stays the task-wide claude-sonnet-4-6
  fixing:
    model: claude-sonnet-4-6
    reasoning: medium
```

Each field resolves independently, most-specific first:

```text
stages.<stage>.<field>  →  task-wide model/reasoning  →  agents.providers.<provider>.<field>  →  unset
```

So a stage can override only `reasoning` and keep the task-wide (or provider-default) `model`, and vice versa. Both sub-fields are optional; a stage block of `{}` or `null` means "inherit", which is useful for scaffolding a block before filling it in.

The `stages` block also carries the per-stage **skip** toggle, `enabled: false`:

```yaml
stages:
  planning:
    enabled: false # write a stub plan and run as a single unit (no decomposition)
  testing:
    enabled: false # bypass the Check Runner (e.g. a repo with no test suite)
  review:
    enabled: false # DANGER: no agent review gate — requires agents.allow_review_skip: true
```

Skippable stages: `planning`, `testing`, `review`, `fixing`, `summary`. `implementation` (the core work), `publishing` (the output), and `refinement` (use the `refined` flag) can never be skipped here. The effective skip set is the union of `agents.skip_stages` (global) and a task's `enabled: false` overrides — a stage skipped globally cannot be re-enabled per task.

What each skip does: `planning` → stub plan, single unit; `testing` → straight to review (no checks); `review` → commit with no agent gate; `fixing` → first test/review failure goes to `manual_action_required` (no recovery loop); `summary` → a stub summary. Every skip is logged at WARNING and recorded in `state.db` (`stage_runs.skipped`), and the skipped set is listed in the PR body.

Allowed stage keys and their valid sub-keys:

```text
refinement      model, reasoning
planning        model, reasoning, enabled
implementation  model, reasoning
testing         enabled
review          model, reasoning, enabled
fixing          model, reasoning, enabled
summary         model, reasoning, enabled
```

Rules:

- `model`/`reasoning` apply only to the agent-routed stages; `testing` (the Check Runner) and `publishing` (the Git Manager) run no agent, so a `model`/`reasoning` override there is meaningless and is rejected fail-closed (`invalid_stage_override`). `publishing` is not a valid key at all;
- `enabled` applies only to the skippable stages above; `enabled` on `implementation`/`refinement` is rejected. `enabled` must be a boolean;
- disabling `review` (`enabled: false`) is rejected unless `agents.allow_review_skip: true` (`review_skip_not_allowed`) — it removes the only agent quality gate before commit/PR;
- unknown sub-keys, non-mapping stage values, and invalid `reasoning` levels are likewise rejected;
- a `model` string is **not** validated against the stage's provider — if a stage routes to a provider that does not recognize the model name, the run fails at that provider, the same as a task-wide `model`. Keep model names consistent with the provider routing for that stage.

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
refined: false
decompose: false
agents:
  planning: claude
  implementation: claude
  review: codex
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
- acceptance criteria are concrete;
- constraints limit scope;
- provider overrides name known stages and providers.

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

Invalid route override:

```markdown
---
id: task-044
title: "Add retries"
agents:
  testing: codex
---

## Description

Add retries to webhooks.
```

Reason: `testing` is not an agent-routed stage.

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
  "refined": false,
  "decompose": false,
  "agents": {
    "planning": "claude",
    "review": "codex"
  },
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
- keep provider overrides minimal;
- use `model` and `reasoning` only when the task demands a specific model tier or reasoning depth;
- do not include credentials or secret values;
- do not try to pass CLI flags through front matter;
- prefer one coherent change per task.
