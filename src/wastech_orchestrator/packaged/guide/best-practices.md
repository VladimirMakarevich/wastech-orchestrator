# Best practices for writing tasks

Read [README.md](README.md) first for the contract and hard rules. This file is about writing a _good_ task — one the orchestrator can carry to a clean, reviewable pull request without guesswork.

## 1. Write testable acceptance criteria

Acceptance criteria are the contract for "done." Make each one observable and checkable. Name the behavior, the input, and the expected output.

**Good:**

```markdown
- [ ] `GET /users?page=2` returns the second page using the existing pagination metadata.
- [ ] Invalid page values return HTTP 400 with the existing error shape.
- [ ] Add unit tests for valid and invalid page values.
```

**Avoid (vague, untestable):**

```markdown
- [ ] Make pagination better.
- [ ] Clean up the API.
```

If you genuinely want the orchestrator to enrich an under-specified task, omit acceptance criteria — the refinement stage will add assumptions and criteria. A complete task (description + acceptance criteria) skips refinement automatically; a task you have thought through should state its criteria.

## 2. Scope to one coherent change

One task = one branch = one PR. Keep it to a single, reviewable change:

- Prefer "add a retry budget to webhook delivery" over "improve webhook reliability."
- If the work spans several independent changes, split it into separate tasks. (Whether one large task is auto-decomposed into sequential subtasks is a flow/planning decision; the gate defaults to the operator's `agents.decomposition.enabled` but a task may override it with `decomposition: true|false` — describe the scope and let planning propose a split.)
- A tightly scoped task plans better, reviews faster, and is far less likely to need a fixing cycle.
- If your repo expects a customer-specific branch convention, set `branch_name` to the full target branch (e.g. `branch_name: "feature/ABC-123-webhook-retry-budget"`); otherwise omit it and use the orchestrator's default branch naming.

## 3. Pick the flow with `task_type` (default: `implementation`)

A task runs a **flow** — a fixed pipeline of stages. Omit `task_type` and you get the default `implementation` flow (`planning → implementation → testing → review → fixing → documentation → publish`), which is what almost every coding task wants. Set `task_type` to run a different flow:

```yaml
task_type: deep_research # omit ⇒ implementation
```

- **Built-ins:** `implementation` (default), `deep_research`, `security_audit`.
- **Custom flows:** an operator can add `<repo>/.worc/flows/<task_type>.yaml`, and you select it by naming it in `task_type`. A `task_type` with no matching flow fails the task before any branch is created.

The task only _names_ the flow; it never edits the graph or a stage's provider/model — those live in the flow YAML (an operator concern). The only per-task pipeline knob is disabling a node (`nodes.<node-id>.enabled: false`); to reshape the pipeline or retune models, author/edit the flow under `.worc/flows/`.

## 4. State constraints explicitly

The `## Constraints` section is how you fence the agent in. Use it for:

- **Do-not-touch areas** — "do not change the public webhook payload shape," "leave the billing module alone."
- **Dependency limits** — "no new runtime dependencies without approval."
- **Compatibility / migration limits** — "keep the existing DB schema; no destructive migrations."

Constraints are cheap to write and prevent the most expensive review failures.

## 5. Respect the project's own working rules

A task you author should ask for work that fits how this project (and most well-run repos) operate. State these in the task body when relevant so the agent honors them:

- **Minimal, focused changes** that match the style of the surrounding code.
- **Tests updated with behavior** — new or changed behavior comes with new or updated tests.
- **Docs kept in sync** — when the change touches user-facing behavior, CLI, config, or architecture, the docs are updated in the same change.
- **Canonical names** — refer to stages and providers by their real names: stages `refinement, planning, implementation, testing, review, fixing, summary, publishing`; providers `codex`, `claude`.

## 6. Honor the security invariants

These are non-negotiable for this orchestrator; never write a task that tries to work around them:

- **No secrets** in the task file (no tokens, keys, or passwords). Credentials are configured in the environment by the operator, never carried in a task.
- **No flags that weaken the sandbox or approvals.** A task cannot pass CLI flags to the agent, cannot change commands, `extra_args`, credentials, or the security policy. Front matter is scanned for flag-shaped values and rejected.
- **A task cannot add, replace, or relax checks.** The quality-gate commands are an operator concern resolved from config at install time; task content cannot change them.

## Authoring checklist

Before handing over a task:

- [ ] `id` is lowercase, matches `^[a-z0-9][a-z0-9._-]{0,63}$`, has no trailing dot, and is not a Windows device name (`con`, `nul`, `com1`–`com9`, `lpt1`–`lpt9`).
- [ ] `title` is short, specific, and non-empty.
- [ ] `task_type` is omitted (⇒ `implementation`) or names a flow that exists — a built-in (`deep_research`, `security_audit`) or an operator flow in `.worc/flows/`.
- [ ] `## Description` is concrete and non-empty.
- [ ] Acceptance criteria are present and testable (unless you intend refinement to add them).
- [ ] `## Constraints` lists do-not-touch areas and dependency/compatibility limits.
- [ ] Front matter uses only allowed fields; any `nodes.<node-id>.enabled` disable names a node in the task's flow.
- [ ] No secrets; no CLI-flag-shaped values; no attempt to change checks or security policy.
- [ ] The change is one coherent unit (describe the scope; large work may be auto-decomposed by the flow).
