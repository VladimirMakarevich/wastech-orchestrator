# Writing tasks for wastech-orchestrator

**You are an AI agent writing a task file for wastech-orchestrator.** This folder is your single source of truth: read it and you can produce a valid, well-scoped task without reading the rest of the repository. If you only have a moment, read this file — it is enough to write a correct task.

- **[best-practices.md](best-practices.md)** — how to write a _good_ task (testable criteria, scoping, constraints, the project's own working rules).
- **[decision-guide.md](decision-guide.md)** — _when to use what_ (run vs watch, disabling nodes, auto-merge, where task files live, Telegram).
- **[footprint.md](footprint.md)** — what the orchestrator leaves in the repository: every directory and file under `.worc/`, whether its presence is normal, what is safe to delete, and how retention works. Includes `follow-ups.md`, the accumulating list of what tasks noticed and did not fix — the one file you curate by hand.
- **[tasks/task-minimal.md](tasks/task-minimal.md)** — the smallest valid task (just `id`, `title`, and a body) and **[tasks/task-rich.md](tasks/task-rich.md)** — a rich task that exercises the common front-matter fields with inline rule notes. Copy one and fill it in.
- **[skills/worc-task/SKILL.md](skills/worc-task/SKILL.md)** — a copy-ready task-authoring skill that turns raw work into one valid orchestrator task file.
- **[skills/worc-deco-task/SKILL.md](skills/worc-deco-task/SKILL.md)** — a copy-ready task-authoring skill for operator-authored decomposition (root task + subtask specs).
- **[config/README.md](config/README.md)** — build or tune the orchestrator's own `config.yaml` for this repository.
- **[config/reference.md](config/reference.md)** — the **complete field reference** for `config.yaml`: every field, its allowed values, default, constraints, and when to use it. Read this to understand any config field.
- **[config/best-practices.md](config/best-practices.md)** — safe defaults, checks layout, and common config mistakes.
- **[skills/worc-config/SKILL.md](skills/worc-config/SKILL.md)** — a copy-ready skill that interviews the operator and assembles a project-specific config.
- **[flows/README.md](flows/README.md)** — author a _custom flow_ (a new `task_type`): the graph of steps, where the flow YAML + its prompts live, registration, and validation.
- **[flows/reference.md](flows/reference.md)** — the **complete field reference** for flows: every flow-level and node-level field (including `output_policy`, `publishing`, `permission_ceiling`, `network_policy`), plus edges and validation.
- **[flows/roles.md](flows/roles.md)** — author a flow's **role prompts**: the built-in evaluator roles, the per-node output contract, output slots, and the supervisor layer.
- **[flows/prompt-variables.md](flows/prompt-variables.md)** — the `{name}` variable allowlist role prompts may reference.
- **[skills/worc-flow/SKILL.md](skills/worc-flow/SKILL.md)** — a copy-ready skill that authors a new custom flow (graph, output kind, route) end-to-end.
- **[skills/worc-flow-role/SKILL.md](skills/worc-flow-role/SKILL.md)** — a copy-ready skill that writes or revises a single node's role prompt without a new flow.
- **[skills/worc-flow-tune/SKILL.md](skills/worc-flow-tune/SKILL.md)** — a copy-ready skill that tunes a flow's per-node provider/model/reasoning/budgets without changing the graph.

## What the orchestrator does with your task

wastech-orchestrator is not a chat agent. It takes one task file and drives it through a fixed pipeline of stages — **refinement → planning → implementation → testing → review → fixing → documentation → publishing** (the summary at the end is written by the supervisor layer, or deterministically from the run's recorded facts when that layer is switched off) — launching a coding-agent CLI (Codex or Claude Code) for the agent stages and running the repository's own checks for testing. When the pipeline succeeds, **the orchestrator** (never the agent) commits, pushes a branch `worc/<epoch>-<task-id>-<slug>` by default (`<epoch>` is a Unix timestamp, so a re-run never collides) or the task's validated `branch_name`, and opens a pull request.

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
| `id` | **yes** | string | Stable id. Must match `^[a-z0-9][a-z0-9._-]{0,63}$` (lowercase; no spaces, uppercase, or leading separator), have no trailing dot, and not be a Windows device name (`con`/`prn`/`aux`/`nul`/`com1`–`com9`/`lpt1`–`lpt9`). It becomes a directory/branch name, so the rule is host-independent. |
| `title` | **yes** | string | Short, non-empty human title. Used for the default branch slug, PR title, and reports. |
| `task_type` | no | string | Flow selector — which pipeline runs the task. Omit ⇒ `implementation` (the default coding pipeline). Built-ins seeded by `install` into `<repo>/.worc/flows/`: `implementation`, `deep_research`, `security_audit`, `blog_article`, `blog_article_revise`, `content_chapter`, `content_translate`; an operator may add or replace any of them as `<repo>/.worc/flows/<task_type>.yaml`. That directory is the only place flows resolve from, so an unknown `task_type` (no matching file there) fails the task before any branch is created. The task only _names_ the flow — it never edits the graph. See the decision guide. |
| `branch_name` | no | string \| null | Full branch-name override. Omit for `<repo.branch_prefix>/<epoch>-<id>-<slug(title)>`; set to match a project's branch convention. Ignored in `existing`/`current` branch mode. |
| `branch_mode` | no | `new` \| `existing` \| `current` | Where task git ops point. `new` (default) creates a fresh branch from base; `existing` works in `branch_ref` (required); `current` uses the working tree's current branch as-is. Overrides `repo.branch_mode`. See the decision guide. |
| `branch_ref` | no | string | The already-existing branch to check out — **required when** `branch_mode: existing`, ignored otherwise. Must exist locally or on the remote (never auto-created). |
| `publish` | no | `commit` \| `push` \| `pull_request` | Downgrade-only cap on where the publish node stops (`min(flow_policy, publish)`). Omit ⇒ the flow's own policy; a no-op on a flow with no publish node. See the decision guide. |
| `trust_level` | no | `strict` \| `auto` | Per-task override of `security.trust_level` — the approval threshold for the dangerous-diff gate. `strict` gates every deletion/manifest edit; `auto` (default) gates only `protected_paths`. Never lowers the hard ceiling. See the decision guide. |
| `auto_merge` | no | boolean | `true` requests auto-merge of the PR. **Dangerous**; the per-task value wins outright over the instance default. See the decision guide. |
| `prompt_audit` | no | boolean | `true`/`false` forces prompt-audit recording for this task; omit = config default. |
| `decomposition` | no | boolean | `true`/`false` permits/forbids decomposition for this task (task-wins over `agents.decomposition.enabled`); omit = config default. Only flips the gate — the flow + planning still decide whether a split happens. See the decision guide. |
| `contacts` | no | list of strings | Plain-text mentions in Telegram notifications and human-in-the-loop prompts. No access control. |
| `depends_on` | no | list of strings | Other **task ids** that must be **merged** before this task may start (non-blocking, merge-gated: the scheduler skips a dependent while a dependency is unmerged and runs other eligible tasks meanwhile). For _separate_ tasks that build on each other — not for splitting one task. Listing the task's own id rejects it (`invalid_depends_on`). |
| `subtasks` | no | list of strings | Operator-authored decomposition: ordered repository-relative paths to per-subtask spec files, run sequentially on one branch into one PR. The gate checks only the list shape; the paths, files, count, and linear ordering are checked at the pre-branch preflight. **Author it with the `worc-deco-task` skill**, not by hand. |
| `priority` | no | `low` \| `mid` \| `high` | Scheduling order under `watch`: eligible tasks run `high → mid → low`, ties by **natural (numeric-aware)** filename order (`p9` before `p10`, same on every OS). `depends_on` is always stronger. Read the effective order from `worc list` / `worc top`, not your file manager. Omit/unrecognised ⇒ `mid` (fail-open — never rejects). |
| `queue` | no | non-empty string | Routes the task to a worc instance whose `orchestrator.queue` selector matches (string equality) when several instances share one task pool. Omit ⇒ `"default"`. **Fail-closed**: a non-string or empty value rejects the task. Usually an operator concern — leave it off unless told otherwise. |
| `nodes` | no | mapping | Per-node front matter, keyed by flow node id. Valid sub-keys: `enabled` (the disable toggle) plus the best-effort `model` / `reasoning` / `provider` overrides. See the decision guide. |

Provider, model, and reasoning are the **flow node's** decision (the operator's flow + `config.yaml providers`); a task may only overlay them for one run via `nodes.<node-id>.{provider,model,reasoning}`. Those overlays are **best-effort**: the gate checks only that each is a non-empty string, and one the resolved flow or config cannot honor (a provider outside `agents.allowed`, a reasoning level the provider does not support) is warned and skipped at run time, falling back to the flow's value — never a task failure. The resolution chain is task node override → flow node declaration → provider config default.

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
5. `nodes` overrides carry **only** the sub-keys `enabled` / `model` / `reasoning` / `provider`, keyed by flow **node id**; any other sub-key rejects the task (`invalid_node_override`). Any node in the task's resolved flow may be disabled (the gate checks shape only); an id absent from the flow ends the task `failed` at flow resolution. Which nodes are safe to disable is the operator's flow-authoring call — there is no `agents.allow_review_skip` gate.
6. **No secrets** and **no CLI-flag-shaped values** in front matter — **every** value (including `title`/`contacts`) must be plain text: a leading `-`, or a `` ` ``/`;`/`|`/`$(`, is rejected as `injection_suspected` (e.g. a `title` of `"--dangerously-skip-permissions"`). The task body never builds CLI arguments and is not scanned, so put code/shell snippets there, not in front matter.
7. Keep it reasonably sized (the gate caps file size, line count, and per-line length).

Completeness (separate from rejection): if the task lacks acceptance criteria, it is **not** rejected — the refinement stage runs to enrich it. Provide acceptance criteria when you want refinement skipped (it is skipped automatically for a complete task; there is no flag).

## JSON tasks

For machine-generated input, a `.json` object works too. Every front-matter field is a JSON key, and `description` carries the body text:

```json
{
  "id": "task-050",
  "title": "Add retry budget to webhook delivery",
  "nodes": { "review": { "provider": "claude" } },
  "description": "## Description\n\nAdd a bounded retry budget.\n\n## Acceptance criteria\n\n- [ ] Stops after 5 failed attempts.\n"
}
```

## Where this fits

These docs are distilled for authoring, and they are complete for that purpose: you do not need anything outside this guide to write a task. The orchestrator's own repository carries the same material with extra contributor-facing detail, which matters only if you are working on the orchestrator itself.
