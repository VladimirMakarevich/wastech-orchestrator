# Writing tasks for wastech-orchestrator

**You are an AI agent writing a task file for wastech-orchestrator.** This folder is your single source of truth: read it and you can produce a valid, well-scoped task without reading the rest of the repository. If you only have a moment, read this file — it is enough to write a correct task.

- **[best-practices.md](best-practices.md)** — how to write a _good_ task (testable criteria, scoping, constraints, the project's own working rules).
- **[decision-guide.md](decision-guide.md)** — _when to use what_ (run vs watch, disabling nodes, auto-merge, where task files live, Telegram).
- **[examples/task-minimal.md](examples/task-minimal.md)** — the smallest valid task (just `id`, `title`, and a body) and **[examples/task-rich.md](examples/task-rich.md)** — a maximal task that exercises _every_ front-matter field with inline rule notes. Copy one and fill it in.

## What the orchestrator does with your task

wastech-orchestrator is not a chat agent. It takes one task file and drives it through a fixed pipeline of stages — **refinement → planning → implementation → testing → review → fixing → summary → publishing** — launching a coding-agent CLI (Codex or Claude Code) for the agent stages and running the repository's own checks for testing. When the pipeline succeeds, **the orchestrator** (never the agent) commits, pushes a branch `agent/<task-id>-<slug>`, and opens a pull request.

Your task file is the entire contract. Write it so an agent can plan, implement, test, review, and summarize the change **without asking for hidden context**.

## The task contract

A task is a **Markdown** file: YAML front matter, then a Markdown body. (JSON is also accepted for machine-generated input — see the end of this file.) Use this shape:

```markdown
---
id: task-001
title: "Short imperative title"
---

## Description

What should change and what the user-visible behavior should be afterward.

## Acceptance criteria

- [ ] First observable behavior that must work.
- [ ] Tests that must pass.

## Constraints

- Areas that must not be touched.
- No new dependencies without approval.
```

### Front-matter fields

Only the fields below are allowed. **Any other key makes the task rejected** (`unknown_top_level_field`).

| Field | Required | Type | Meaning |
| --- | --: | --- | --- |
| `id` | **yes** | string | Stable id. Must match `^[a-z0-9][a-z0-9._-]{0,63}$` (lowercase; no spaces, uppercase, or leading separator). |
| `title` | **yes** | string | Short, non-empty human title. Used for the branch slug and reports. |
| `auto_merge` | no | boolean | `true` requests auto-merge of the PR. **Dangerous**; the per-task value wins outright over the instance default. See the decision guide. |
| `prompt_audit` | no | boolean | `true`/`false` forces prompt-audit recording for this task; omit = config default. |
| `contacts` | no | list of strings | Plain-text mentions in Telegram notifications. No access control. |
| `nodes` | no | mapping | Per-node `enabled: false` disable toggle, keyed by flow node id (the only per-node knob). See the decision guide. |
| `pr_title` | no | string \| null | PR title override; when set, used verbatim as the pull-request title instead of `title`. |

Provider, model, and reasoning are **not** task fields — they live on the flow node (the operator's flow + `config.yaml providers`). A task cannot repoint a stage's provider or set its model.

### Body sections

- `## Description` — **required and non-empty.** What changes, why, and the end-state behavior.
- `## Acceptance criteria` — a testable checklist. Strongly recommended (see below).
- `## Constraints` — do-not-touch areas, dependency/compatibility limits.

If there are no `##` sections at all, the whole body is treated as the description.

## Hard rules — never emit a task that breaks these

The validation gate rejects a task **before** any branch or agent runs. To always pass:

1. The file **starts** with a `---` front-matter fence (no blank lines before it).
2. `id` and `title` are present; `id` matches the regex above.
3. `## Description` (or the body) is **non-empty**.
4. **Only** the allowed front-matter keys appear; types match the table.
5. `nodes` overrides carry **only** `enabled: false`, keyed by flow **node id**. Any node in the task's resolved flow may be disabled (the gate checks shape only); an id absent from the flow ends the task `failed` at flow resolution. Which nodes are safe to disable is the operator's flow-authoring call — there is no `agents.allow_review_skip` gate.
6. **No secrets** and **no CLI-flag-shaped values** in front matter (e.g. a `title` of `"--dangerously-skip-permissions"` is rejected as `injection_suspected`). The task body never builds CLI arguments, but front matter is scanned defensively.
7. Keep it reasonably sized (the gate caps file size, line count, and per-line length).

Completeness (separate from rejection): if the task lacks acceptance criteria, it is **not** rejected — the refinement stage runs to enrich it. Provide acceptance criteria when you want refinement skipped (it is skipped automatically for a complete task; there is no flag).

## JSON tasks

For machine-generated input, a `.json` object works too. Every front-matter field is a JSON key, and `description` carries the body text:

```json
{
  "id": "task-050",
  "title": "Add retry budget to webhook delivery",
  "agents": { "planning": "claude", "review": "codex" },
  "description": "## Description\n\nAdd a bounded retry budget.\n\n## Acceptance criteria\n\n- [ ] Stops after 5 failed attempts.\n"
}
```

## Where this fits

These docs are distilled for authoring. The operator-facing references with full detail are `docs/task-authoring.md` and `docs/configuration.md` in the orchestrator's own repository; you do not need them to write a task.
