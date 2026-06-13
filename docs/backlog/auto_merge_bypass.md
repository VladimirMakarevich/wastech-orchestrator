# Backlog: Auto-merge bypass flags (global and per-task)

Status: **implemented** (shipped; see CHANGELOG `[Unreleased]`)
Date: 2026-06-13
Owner: Vladimir Makarevich

> **Status update (shipped).** This feature is now implemented. The design below is retained for
> context; where the code diverges from it, **the code wins**:
>
> - Config lives under **`git:`** (not a new `publishing:` block): `git.auto_merge`,
>   `git.auto_merge_strategy`, `git.auto_merge_allow_per_task`, `git.auto_merge_wait_for_checks`.
> - There is **no `MANUAL_REVIEW` state**. The normal flow is `creating_pr → done` (DONE already means
>   "PR created, human merges later"); auto-merge adds a merge step on that same edge. A **blocked**
>   merge ends `manual_action_required` with the PR left open — never `failed`.
> - A per-task `auto_merge: true` is honored **only** when `git.auto_merge_allow_per_task: true`
>   (operator opt-in); a per-task `false` always opts out. This closes the "task authorship == merge
>   rights" surface and intentionally diverges from §3.2 below.
> - Two merge modes via `git.auto_merge_wait_for_checks`: immediate `gh pr merge` (default) or
>   GitHub-native `gh pr merge --auto`. The merge **never** uses `--admin`; idempotent via a
>   `pr_merge` publish op.
> - Operator guide: [docs/operations.md](../operations.md#auto-merge-to-the-base-branch-danger-bypasses-human-review).

> ⚠️ **Security-sensitive feature.** Both flags described here bypass the human review gate that
> is the primary safeguard against shipping broken or malicious code to the main branch. Implement
> with explicit, multi-layered warnings and keep both flags `false` by default forever.

This document captures the design for opt-in auto-merge to the main repository, at two
granularities: a global setting in `config.yaml` and a per-task metadata override. It is a backlog
item, not part of the currently implemented runtime behavior. Nothing here overrides
[00_orchestrator_final_plan.md](../implementation_stages/00_orchestrator_final_plan.md), [CLAUDE.md](../../CLAUDE.md), or the hard
invariants in [docs/rules/](../rules/).

---

## 1. Goal

Allow operators who understand the risk to skip the manual PR-approval gate after a successful
publish stage, so that a merged commit lands on the target branch without human intervention.

Two distinct opt-in surfaces are needed:

| Surface | When to use |
|---|---|
| **Global flag** (`config.yaml`) | Operator-level policy: every qualifying task in this instance auto-merges by default. |
| **Per-task flag** (task metadata) | Task-level override: a single task is known-trivial and the author explicitly wants it to bypass the queue. |

Both are **disabled by default** and are independent — enabling the global flag does not force
all tasks to auto-merge, and enabling the per-task flag works even when the global flag is off.

### Typical use cases

- **Global flag**: CI/CD pipelines or fully automated deployments where the PR review is
  performed by an upstream process (e.g. required CI checks, protected branches with status
  checks), making the human-approval gate redundant.
- **Per-task flag**: A developer submits a single, clearly scoped cosmetic task (rename a label,
  change a colour, fix a typo) and is confident the diff will be trivially reviewable. They do
  not want to sit through the approval round-trip for an obviously safe change.

---

## 2. Why this is dangerous

The manual approval gate exists because:

1. **Agents make mistakes.** LLM-generated code can be functionally wrong, introduce subtle bugs,
   or regress unrelated behaviour even when all checks pass.
2. **Checks are not exhaustive.** CI may not cover every edge case; a passing test suite is a
   necessary but not sufficient condition for a correct change.
3. **Security surface.** A compromised task definition, a prompt-injection in the repository, or
   a misconfigured provider could produce a harmful diff. Human review is the last line of defence.
4. **Irreversibility.** Once merged to the main branch and pushed, undoing a change requires
   explicit revert commits and coordination.

Auto-merging removes this last defence. It should be treated the same way as granting write access
to the main branch without review — acceptable only in tightly controlled, well-understood contexts.

---

## 3. Proposed design

### 3.1 Global flag in `config.yaml`

Add a new top-level key under `publishing` (or a new `merge` section):

```yaml
# config.yaml
publishing:
  # ⚠️  DANGER: when true, every successfully published PR is merged to the main branch
  #    automatically, bypassing human review.  Enable only if CI status checks and
  #    protected-branch rules are already enforcing the quality gate you need.
  #    Default: false
  auto_merge: false

  # Strategy passed to the GitHub merge API when auto_merge is true.
  # Allowed: "merge" | "squash" | "rebase"
  auto_merge_strategy: "squash"
```

**Schema version** must be bumped when this key is added (see config `schema_version` gating in
`config/loader.py`); old configs that do not include the key default to `false`.

### 3.2 Per-task flag in task metadata

Add an optional field to the task definition (`.yaml` or `.md` front-matter):

```yaml
# tasks/pending/TASK-042.yaml
id: TASK-042
title: "Rename 'Submit' button to 'Save'"
# ⚠️  DANGER: setting this to true merges the PR without human review.
#    Use only for changes you are certain are trivially safe.
#    Default: false
auto_merge: false
```

The per-task flag applies **only to that task**. It does not change the global default.

**Priority / resolution order**:

```
task.auto_merge (explicit) > global publishing.auto_merge > false (hard default)
```

A task may also explicitly set `auto_merge: false` to opt out even when the global flag is `true`.

### 3.3 Effective auto-merge logic (publishing stage)

```
if task.auto_merge is explicitly set:
    effective = task.auto_merge
elif config.publishing.auto_merge:
    effective = true
else:
    effective = false

if effective:
    log WARNING "Auto-merge enabled for task <id>; skipping human review gate"
    git_manager.merge_pr(pr_url, strategy=config.publishing.auto_merge_strategy)
    state = DONE
else:
    state = MANUAL_REVIEW  # existing flow
```

### 3.4 Audit trail

Whenever auto-merge fires, the orchestrator must:

- Write an explicit `[AUTO-MERGE]` line to the task's run log (persisted, auditable).
- Record the merge commit SHA in `state.db` alongside the PR URL.
- Emit a `WARNING`-level log line (so it surfaces in `--verbose` output and monitoring).
- Optionally trigger a Telegram/notifier alert (hook into the existing `Notifier` interface from
  `notify/interface.py`) so an operator can react if the merge was unexpected.

### 3.5 Safety guardrails (non-negotiable)

- Both flags are **`false` by default** — no migration or upgrade changes this.
- The security policy (no flag can weaken sandbox/approval rules per `docs/rules/security.md`)
  continues to apply. `auto_merge` only affects the *publish* stage, not the agent execution
  sandbox.
- `extra_args` in a task **cannot** set `auto_merge: true`. Task-level override comes from
  the parsed task metadata, not from free-form agent arguments.
- The `manual_review` state remains reachable at any time; auto-merge is purely additive.
- If the merge API call fails (e.g. branch protection rules block it), the task falls back to
  `MANUAL_REVIEW` with a warning — it does **not** force-push or retry destructively.

---

## 4. Implementation checklist (when scheduled)

- [ ] Bump `config.schema_version`; add `publishing.auto_merge` and `publishing.auto_merge_strategy`
      to `config/loader.py` with validation and schema migration note.
- [ ] Add `auto_merge: bool = False` to the task metadata parser (`core/task_parser.py` or
      equivalent).
- [ ] Add effective-flag resolution logic to the publishing stage in `core/orchestrator.py`.
- [ ] Wire `git_manager.py` — add `merge_pr(url, strategy)` method that calls the GitHub API
      (likely via `gh pr merge`); handle branch-protection failures gracefully.
- [ ] Write audit log entry and `state.db` record on merge.
- [ ] Hook into `Notifier` for auto-merge events (see [[session-persistence]] for restart context).
- [ ] Add `auto_merge_strategy` validation (reject unknown strategies early).
- [ ] Unit tests: flag resolution logic, config default, per-task override, opt-out override.
- [ ] Integration test: fake-CLI publish stage with `auto_merge: true` → assert merge called;
      with `false` → assert `MANUAL_REVIEW`.
- [ ] Update `docs/operations.md` with a prominent warning section on auto-merge.
- [ ] Update `CHANGELOG.md` `[Unreleased]`.

---

## 5. Related items

- [[product_backlog]] — "Automatic PR merge" row: this item supersedes that row and adds the
  per-task granularity.
- [[session-persistence]] — restart recovery must not re-trigger an auto-merge that already fired.
- `notify/interface.py`, `notify/telegram.py` — Notifier interface for merge alerts.
- `docs/rules/security.md` — security policy that constrains what this feature may touch.
- `config/loader.py` `_check_schema_version` — schema gating that must be updated.
