# When to use what

A decision guide for the optional knobs. Defaults are almost always right — reach for these only when the task genuinely needs them. See [README.md](README.md) for the hard rules each setting must obey.

## `run` vs `watch` (how the operator runs your task)

You usually do not choose this — the operator does — but it affects where your task file goes:

- **`run <task-file>`** processes exactly one task file, end to end.
- **`watch`** polls the `tasks/pending/` folder and processes tasks dropped there, looping with periodic git sync.

A live task belongs in the repo's own `tasks/pending/` directory (committed and pushed there) — that is how a teammate hands work to a watching orchestrator.

## `decompose` — split a large task

| Value | Effect |
| --- | --- |
| `true` | Force the decomposition gate: the orchestrator breaks the task into subtasks that run sequentially on one branch and produce one PR. |
| `false` | Disable decomposition for this task. |
| omitted | Use the operator's `agents.decomposition.enabled` default. |

Use `true` only for genuinely large work. A small, coherent change should stay a single unit.

## `pr_title` — override the PR title

By default the orchestrator generates the PR title from the task `title`. Set `pr_title` when you want the published PR to read differently — most often a conventional-commit-style subject:

```yaml
title: "Add a bounded retry budget to webhook delivery"
pr_title: "feat(webhooks): bounded retry budget for delivery"
```

Omit it to auto-generate. It changes only the PR title text; it does not touch the branch name (still `agent/<task-id>-<slug>`), the commit messages, or any routing.

## Skipping stages — `stages.<stage>.enabled: false`

Skip a stage only when it adds no value for this task. Skippable: `planning`, `testing`, `review`, `fixing`, `summary`. (`implementation` and `publishing` are never skippable; for `refinement`, use the `refined` flag instead.)

```yaml
stages:
  planning:
    enabled: false # write a stub plan and run as a single unit (no decomposition)
  testing:
    enabled: false # bypass the Check Runner — only for a repo with no meaningful test suite
```

What each skip does:

| Skip | Effect |
| --- | --- |
| `planning` | Stub plan; runs as a single unit. |
| `testing` | Straight to review, no checks run. |
| `review` | Commit with **no agent review gate**. |
| `fixing` | The first test/review failure goes straight to `manual_action_required` (no recovery loop). |
| `summary` | A stub summary instead of a written one. |

**Skipping `review` is high-risk** — it removes the only agent quality gate before commit/PR. It is rejected (`review_skip_not_allowed`) unless the operator set `agents.allow_review_skip: true`. Do not skip review unless you know that flag is enabled. Stage-skip is per-task only (`stages.<stage>.enabled: false`); the global `agents.skip_stages` list was removed in config v10.

## Per-stage `model` / `reasoning`

Different stages have different needs: `planning` and `review` benefit from a stronger, higher-reasoning model; `implementation`, `fixing`, and `summary` are usually fine on a lighter one. Set a task-wide default and override per stage:

```yaml
model: claude-sonnet-4-6 # task-wide default for stages not listed below
reasoning: low
stages:
  planning:
    model: claude-opus-4-8
    reasoning: high
  review:
    reasoning: high # only reasoning overridden; model stays the task-wide one
```

Each field resolves most-specific-first:

```text
stages.<stage>.<field>  →  task-wide model/reasoning  →  provider default  →  unset
```

`reasoning` must be one of `low, medium, high, xhigh, max` (`xhigh` needs a capable model; `max` is Claude-only and clamps to `xhigh` on Codex). A `model` string is **not** checked against the stage's provider — if the provider does not know the model, the run fails there. Keep model names consistent with the provider you route each stage to.

## `auto_merge` — danger

`auto_merge: true` requests that the orchestrator merge the PR automatically after publishing, **bypassing human merge**. It is honored **only** when the operator set `git.auto_merge_allow_per_task: true`; otherwise it is ignored. Leave it unset unless you have an explicit reason and know auto-merge is enabled and safe for this repository.

## `refined` — skip refinement

Set `refined: true` only when the task already has enough detail to plan directly. When `refined` is unset, the orchestrator still skips refinement if the task looks complete — completeness needs a non-empty description **plus** acceptance criteria. Missing acceptance criteria does not reject the task; it just makes refinement run.

## Where task files live

There is a single canonical layout. Drop your task file in the repo's own `tasks/pending/` directory at the repo root, where it is git-tracked, committed, and pushed — that is how work reaches a watching orchestrator. Everything the orchestrator generates lives under a single gitignored `<repo>/.worc/` home; only the `tasks/` lifecycle directories stay at the repo root and are tracked. The orchestrator commits the task file and its `<id>.summary.md` as an audit trail; a rejected task is quarantined under `.worc/tasks/rejected`.

## `contacts` and Telegram

`contacts` is a list of plain-text strings rendered as mentions in Telegram notifications and human-in-the-loop prompts:

```yaml
contacts:
  - "@team-lead"
```

They are cosmetic mentions only. They do **not** choose the Telegram chat, grant access, change routing, or alter approval scope — the chat id stays operator-controlled configuration.
