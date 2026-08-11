---
name: worc-deco-task
description: Convert a set of related steps into wastech-orchestrator's operator-authored decomposition — one root task with a subtasks: list plus per-subtask spec files, run sequentially on one branch / one PR. Use when a change is one coherent unit you already know how to split into ordered steps that should land together.
---

# worc-deco-task

Turn a change you already know how to split into **operator-authored decomposition**: one **root task** that carries the shared context, plus an ordered set of **subtask spec files**. The orchestrator runs the subtasks **sequentially, on one branch, into one PR** — the same machinery as an agent-proposed split, but the carving is yours.

Speak in the user's language (default to the language they wrote in).

## When to use

- The user has **one coherent change** plus a clear, ordered list of steps to get there, and wants them done together as a single PR.
- **Guardrail:** if the parts are **independent** and each should get its **own** PR, those are **separate tasks** — author each with `worc-task`, not this skill. Decomposition is for steps that share one branch and one review.

## How to run

1. **Separate root from steps.** The **root** holds the shared context (what the whole change is and why). The **subtasks** are the ordered units to implement it.
2. **Check the count.** You need `2 ≤ n ≤ max_subtasks`, where `max_subtasks` is the operator's `agents.decomposition.max_subtasks` (default **8**). Fewer than 2 isn't a decomposition; more than the max is rejected — group steps together or split the work into separate tasks instead.
3. **Write the root task** at `tasks/preparing/<root-id>.md` — the staging folder the watch daemon never scans (so an in-progress batch is never picked up mid-write) — a normal task file (same front-matter and body rules as a single task: leading `---`, valid `id`, non-empty `## Description`, only allowed keys, no secrets, no flag-shaped values) **plus** a `subtasks:` list of repo-relative paths pointing into a **subfolder**, in execution order.
4. **Write each subtask spec** at `tasks/preparing/subtasks/NN-<slug>.md` (`NN` = zero-padded order):
   - Front matter: `title` (**required**); `slug` (optional — defaults to `slugify(title)`; it names the `NN-<slug>.md` file); `depends_on` (optional — a list of **slugs of EARLIER subtasks only**).
   - **No `id`.** A spec file is a reduced spec, not a standalone task — that is what keeps it from ever running on its own.
   - Body: `## Acceptance criteria` (and any per-step instructions). **Required — an empty body is a reject.** The body is materialized **verbatim** into an immutable `NN-<slug>.md` spec that the edit steps (`implementation`, `fixing`) read as `{subtask_spec_path}`, so write it however the step needs.
5. **Promote the batch** when it is complete: `worc promote <root-id>` moves the root **and the subtask specs it references** together into `tasks/pending/` (subfolder preserved), atomically — so the root never reaches the queue without its specs. (`worc promote --all` promotes everything staged, including the whole `subtasks/` subfolder.) The subtask paths are relative to the root, so they stay valid after the move; no rewriting.
6. **Self-check** against the hard rules below before finishing.

## The decomposition contract

**Root task** — note the `subtasks:` list of ordered, repo-relative paths into a subfolder:

```markdown
---
id: epic-checkout
title: "Rework checkout into a multi-step flow"
subtasks:
  - subtasks/01-cart-model.md
  - subtasks/02-payment-step.md
  - subtasks/03-confirmation.md
---

## Description

Shared context for the whole change — the once-per-task framing every subtask inherits.

## Acceptance criteria

- [ ] The whole checkout flow works end to end after all subtasks land.
```

**Subtask spec** (`tasks/preparing/subtasks/02-payment-step.md`, promoted to `tasks/pending/subtasks/…`) — a reduced spec, **no `id`**:

```markdown
---
title: "Add the payment step"
slug: "payment-step" # optional; defaults to slugify(title)
depends_on: ["cart-model"] # optional; slugs of EARLIER subtasks only
---

## Acceptance criteria

- [ ] Concrete, testable unit-level criteria for this step.
```

## Hard rules / fail-closed reasons

The whole split is validated **before any branch** and quarantined (to `.worc/tasks/rejected/` — the runtime quarantine, deliberately outside the git-tracked `tasks/` tree) on any violation:

- **Count out of range** — fewer than 2 or more than `max_subtasks` (`subtask_count_out_of_range`).
- **Forward / self / unknown dependency** — a subtask's `depends_on` may reference only the slugs of subtasks **before** it; the gate enforces a linear order (`subtask_depends_forward`).
- **Missing or malformed spec file** — a referenced file that doesn't exist, has no front matter, lacks a `title`, or has an empty body (`subtask_file_missing`, `subtask_malformed`).
- **Duplicate slug** — two specs that resolve to the same slug (easy to hit with near-identical titles, since `slug` defaults to `slugify(title)`) reject the batch (`subtask_malformed`). Set an explicit `slug` when titles collide.
- **Bad path** — every `subtasks:` entry must be repo-relative with no `..`, no absolute path, no traversal, and must resolve under the task directory (`invalid_subtask_path`). Spec files **must** live in a **subfolder** (e.g. `tasks/preparing/subtasks/…`, promoted to `tasks/pending/subtasks/…`); a path beside the root task is rejected (the scheduler scans only the top level, so a subfolder also keeps specs from running as standalone tasks).
- **Flow can't decompose** — the task's `task_type` does not resolve at all, or selects a flow with no `decomposition:` block (`flow_cannot_decompose`). The default `implementation` flow supports it.

The root task itself must still obey every single-task rule (valid `id`, only allowed front-matter keys, no secrets, no flag-shaped values, etc.).

## What not to do

- Don't give a subtask spec an `id` — it is a spec, not a task; an `id` would make it look standalone.
- Don't reference a later subtask's slug (or its own) in `depends_on` — dependencies are linear and backward-only.
- Don't place spec files at the top level of `tasks/preparing/` or `tasks/pending/` — keep them in a `subtasks/` subfolder.
- Don't exceed `max_subtasks` (default 8) — group steps or split into separate tasks.
- Don't use decomposition for independent changes that each deserve their own PR — those are **separate tasks** (`worc-task`), optionally chained with `depends_on`.
