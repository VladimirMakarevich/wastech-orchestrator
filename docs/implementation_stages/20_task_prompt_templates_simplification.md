# Backlog: Simplify prompt templates (deliver prompts-only, auto-detect resolution, drop `overrides`)

Status: **implemented 2026-06-14** (schema_version 6). Retained as the design record; the live behavior is documented in [configuration.md](../configuration.md#prompts), [cookbook.md](../cookbook.md#7a-customize-stage-prompts), and [operations.md](../operations.md#upgrading-the-orchestrator). Date: 2026-06-14 Owner: Vladimir Makarevich

This document captures an improvement to how the orchestrator **delivers** and **activates** stage-prompt templates. It revises the shipped `prompts:` block (originally "Prompt customization v1", see [follow_ups.md](follow_ups.md)) and narrows what [`install-templates`](../implementation_stages/17_task_install_templates_command.md) delivers. Nothing here overrides the canonical specification ([00_orchestrator_final_plan.md](../implementation_stages/00_orchestrator_final_plan.md)), [CLAUDE.md](../../CLAUDE.md), [AGENTS.md](../../AGENTS.md), or the hard invariants in [docs/rules/](../rules/) — in particular, prompt templates remain **prompt text only** and cannot change provider, `extra_args`, sandbox/approvals, denied commands/reads, or env.

## 1. Background

`init` and `install-templates` deliver the packaged `templates/` tree beside `config.yaml` in the operator's **install/control directory** — `prompts/<stage>.md`, `skills/<name>/SKILL.md`, `AGENTS.md`, `CLAUDE.md`, `task.md`. Two problems surfaced:

1. **Most of the tree is inert where it lands.** The only part the orchestrator consumes from the control dir is `prompts/`. The rest belong to (or are read from) the **target repository**, not the control dir:
   - `skills/` — the orchestrator scans `<repo.local_path>/.claude/skills` in the **target** repo clone ([`core/skills.py`](../../src/wastech_orchestrator/core/skills.py), [`config/schema.py`](../../src/wastech_orchestrator/config/schema.py) `SkillsConfig`), never `templates/skills/`.
   - `AGENTS.md` / `CLAUDE.md` — read by the coding agent (Codex / Claude Code) from the **target** repo root, never from `templates/`.
   - `task.md` — a task-authoring template that duplicates `worc/examples/task-minimal.md` and `worc/examples/task-rich.md`.

   So `skills/`, `AGENTS.md`, `CLAUDE.md`, and `task.md` sit beside `config.yaml` doing nothing and reading as "configured" when they are not — a real source of operator confusion.

2. **Prompt activation is needless friction.** The runtime always loads the packaged default per stage ([`core/prompts.py`](../../src/wastech_orchestrator/core/prompts.py) `_packaged_default`); the installed `templates/prompts/<stage>.md` is consulted **only** when the operator both edits it and lists it under `prompts.overrides` (empty `{}` by default), with `mode: append` as the default, plus a `strict` flag (an unreadable _listed_ override → fail-closed config error; else warn + fall back to packaged). But a freshly delivered template is **byte-identical** to the packaged default, so wiring an override before editing is a no-op, and the `overrides` map adds a config step with little benefit over "the file is present, so use it."

## 2. Goal

Two coupled prompt-template simplifications, plus a related operator-docs improvement:

- **(A) Deliver prompts-only.** Stop shipping `skills/`, `AGENTS.md`, `CLAUDE.md`, and `task.md` in the delivered `templates/` tree; deliver only `prompts/`.
- **(B) Resolve prompts by convention.** A `<stage>.md` present in `templates_dir` is used **automatically**; the packaged default becomes a per-stage **fallback**; `mode` defaults to **`replace`**; `prompts.overrides` **and** `strict` are **removed**.
- **(C) Improve & actualize the task-authoring docs (`worc/`).** A self-contained improvement to the operator docs delivered by `upgrade-docs`, folded in per operator request — see **Part C (§8)**. It touches no config/resolution: it rewrites the delivered `task-rich.md` to exercise every front-matter field and actualizes `README.md`/`best-practices.md`/`decision-guide.md` (mainly `pr_title` coverage and cross-consistency with the source of truth).

## 3. Non-goals and limits

- **Not** removing the skills feature. Planning-selected skill references (scanning the target repo's `.claude/skills`, surfacing `{skills_path}`) stay exactly as they are; only the delivery of _starter_ skill files from the control-dir templates tree is dropped.
- **Not** auto-seeding the target repo with `AGENTS.md` / `CLAUDE.md` / skills. If starter guardrails for a fresh target repo are ever wanted, that is a separate, explicit "seed target repo" step — not the control-dir templates tree.
- **Not** a change to the Git lifecycle, the security policy, or which process commits/pushes/PRs.
- **Not** auto-writing config: delivery still never mutates `config.yaml` (consistent with the `install-templates` contract).

## 4. Part A — deliver only `prompts/`

Remove `skills/`, `AGENTS.md`, `CLAUDE.md`, `task.md` from the delivered tree. Two implementation options:

| Option | Effect | Note |
| --- | --- | --- |
| Exclude in the copy walk | `_iter_template_files` / `_copy_templates_tree` skip them (as `config.example.yaml` already is) | keeps the files in the wheel (no current consumer) |
| Delete from packaged source | remove `src/wastech_orchestrator/templates/{skills/,AGENTS.md,CLAUDE.md,task.md}` | cleaner; recommended — the files have no other runtime consumer |

Recommendation: **delete from the packaged source**. Only the **template copies** under `src/wastech_orchestrator/templates/` are removed; the repository's own root `AGENTS.md` / `CLAUDE.md` are different files and stay. Task authors use `worc/examples/task-minimal.md` and `worc/examples/task-rich.md` instead of the removed `task.md`.

Downstream updates: the parity/contents tests ([`tests/test_cli_install_templates.py`](../../tests/test_cli_install_templates.py), incl. `test_init_and_install_templates_produce_same_tree`); spec §20.2 (template-tree listing) and §20.5; the `install-templates` design doc [§11 "Scope of the tree"](task_install_templates_command.md) (this **resolves** that open question → prompts-only, and makes the deferred `--only prompts|skills` selector moot) and §1; README command list; `CHANGELOG.md` `[Unreleased]`; [operations.md](../operations.md); and the `worc/` task-authoring guide.

## 5. Part B — prompt resolution by convention

### 5.1 Resolution

For each routable stage (`ROUTABLE_STAGES`):

| `templates_dir` | `<stage>.md` present? | Result |
| --- | --- | --- |
| empty (`""`) | — | packaged default (explicit opt-out: force built-ins) |
| set (default `./templates/prompts`) | **yes** | the file, per `mode` — `replace` = file only; `append` = packaged default + file |
| set | **no** | packaged default (per-stage fallback) |

The packaged default is therefore **only ever a fallback** — used when templates are not connected (`templates_dir` empty) or when a given stage has no file. The presence of `<stage>.md` in `templates_dir` is the **activation signal**; there is no separate opt-in.

### 5.2 Removed / changed

- **Remove `prompts.overrides`** entirely (convention over config).
- **Remove `strict`** too — its only trigger (a _listed_ override file that won't read) cannot occur once `overrides` is gone; a missing `<stage>.md` is now the normal **fallback**, not an error. A legacy `strict` key is tolerated (ignored) on load, like `overrides`.
- **`mode` default → `replace`** (was `append`). `append` (packaged default + file) stays available.
- `templates_dir` keeps its default `./templates/prompts`; empty string means "force packaged defaults".

### 5.3 Code

`core/prompts.py` `PromptTemplateStore`: at construction, scan `templates_dir` for `<stage>.md` per `ROUTABLE_STAGES` (bounded read, like today's override read) instead of iterating `config.overrides`; `resolved(stage)` applies `mode` when a file exists for that stage, else returns the packaged default. `_packaged_default` is unchanged (it becomes the fallback source). The old override-reading loop — and with it the `strict` fail-closed / warn branch — disappears; if `ConfigError` is left unused in the module afterwards, drop its import. `override_for(stage)` keeps its role for the §2.2 dedup, now returning the scanned `<stage>.md` content (or `None`); `compute_skill_dedup` is otherwise unchanged.

## 6. Config schema and migration

- `config/schema.py` `PromptsConfig`: drop the `overrides` **and** `strict` fields; flip the `mode` default to `replace`. **Bump `schema_version`** (config-shape change).
- `config/loader.py` / `config/validation.py`: stop parsing `overrides` and `strict`; shrink the `_check_keys` allowed set to `{templates_dir, mode}`; **tolerate** legacy `overrides`/`strict` keys (ignore, optionally warn) so old configs still load fail-open.
- `upgrade-config` ([`cli.cmd_upgrade_config`](../../src/wastech_orchestrator/cli.py), [`config/upgrade.py`](../../src/wastech_orchestrator/config/upgrade.py)): strip legacy `overrides` and `strict` keys on upgrade; decide whether to force-migrate an explicit `mode: append` (today's upgrade is add-only and does not transform values — see the "Schema migration runner" follow-up).
- `templates/config.example.yaml` `prompts:` block: rewrite to document auto-detect-by-file, the new `replace` default, and the `templates_dir`-empty opt-out; remove the `overrides` and `strict` lines.
- `config/loader.py`: resolve `templates_dir` **relative to the `config.yaml` directory** (not the CWD); absolute paths are still honored. Makes the file-presence activation switch CWD-independent (external-workspace footprint, unattended runs); rides the same `schema_version` bump and subsumes the deferred "config-relative `templates_dir`" follow-up.

## 7. Decisions (resolved open questions)

All open questions are settled (operator, 2026-06-14); the decisions are folded into the sections above.

- **`strict` — removed** (not repurposed). A missing stage file is the normal fallback, so there is no fail-closed-on-missing-prompt path. See §5.2 / §6.
- **§2.2 skill-dedup — kept, source-only change.** The resolution model is unchanged (`replace` = template only; `append` = packaged default + template). The dedup is an orthogonal `plan.md` annotation: `override_for(stage)` reads the scanned `<stage>.md` (or `None`) instead of an `overrides`-mapped file, and `compute_skill_dedup` is otherwise unchanged. No behavior change. See §5.3.
- **Drift — accepted, no auto-refresh.** Bulk delivery (Part A) stays. For a genuinely customized template, "drift vs. the packaged default" is meaningless — the operator wants their own prompt, which differs per project — so no refresh mechanism is added. The only residual is an _unedited_ delivered copy of a stage the operator never customized silently shadowing an updated packaged default after an upgrade: a known, **accepted** latent. `install-templates --force` (or the umbrella `upgrade`) remains the manual refresh. Documented, not engineered.
- **`templates_dir` resolution — relative to `config.yaml`.** Relative paths anchor to the config file's directory (where `init` / `install-templates` place `templates/`), not the CWD; absolute paths are still honored. This makes the file-presence activation switch CWD-independent (external- workspace footprint, unattended runs) and subsumes the deferred "config-relative `templates_dir`" follow-up. See §6.

## 8. Part C — improve & actualize the operator task-authoring docs (`worc/`)

Independent of Parts A/B (folded in per operator request): this improves the `worc/` task-authoring docs delivered by `upgrade-docs` (and `init`/`install`), packaged at `src/wastech_orchestrator/worc/examples/task-rich.md`. **No config-schema or resolution change** — it only makes the delivered example show the full manifest surface.

**Problem.** Today's `task-rich.md` shows a subset (`id`, `title`, `refined`, `decompose`, `agents` for 3 stages, `contacts`, `model`, `reasoning`, `stages` for 2 stages). It omits `pr_title` and `auto_merge`, doesn't route all agent-routed stages, never shows `stages.<stage>.enabled` (stage skip), and doesn't document the reasoning levels or the routable-vs-skippable rules — so an operator cannot see everything they may set and change.

**Goal.** Rewrite `task-rich.md` as a _maximal_ example that exercises **every** front-matter field and annotates the rules inline. `task-minimal.md` stays the minimal counterpart (id + title + body).

**Authoritative field set** — source of truth: `task/model.py` (`ALLOWED_TASK_KEYS`, `StageParams`, `NormalizedTask`), `task/validation_gate.py`, spec §5/§19.3:

| Field | Type / values | Default | Meaning |
| --- | --- | --- | --- |
| `id` | string `^[a-z0-9][a-z0-9._-]{0,63}$` | — (required) | normalized id (rejected, never sanitized) |
| `title` | string | — (required) | title; basis of the branch slug and the default PR title |
| `pr_title` | string | none | overrides the generated PR title |
| `refined` | bool | `false` | `true` skips the **refinement** stage |
| `decompose` | bool \| omit | config default | `true` forces decomposition; `false` disables |
| `auto_merge` | bool \| omit | config default | `true` requests auto-merge — **DANGER: bypasses human review**, honored only if `config.git.auto_merge_allow_per_task: true`; `false` opts out |
| `agents` | map stage→`codex`\|`claude` | none | per-stage provider override; **only** agent-routed stages, only providers in `agents.allowed` |
| `contacts` | list[string] | empty | handles surfaced for human-in-the-loop prompts/approvals |
| `model` | string | provider default | task-wide default model for agent-routed stages |
| `reasoning` | `low`\|`medium`\|`high`\|`xhigh`\|`max` | provider default | task-wide default reasoning |
| `stages.<stage>.model` | string | task-wide `model` | per-stage model; **only** agent-routed stages |
| `stages.<stage>.reasoning` | reasoning level | task-wide `reasoning` | per-stage reasoning; **only** agent-routed stages |
| `stages.<stage>.enabled` | bool | `true` | `false` skips the stage; **only** skippable stages; skipping `review` also needs `agents.allow_review_skip` |

**Stage roles (all 8):** `refinement` (agent; gated by `refined`, not skippable) · `planning` (agent; skippable) · `implementation` (agent; never skippable) · `testing` (no agent — runs checks; skippable; only `enabled` is valid, no model/reasoning) · `review` (agent; skippable + needs `allow_review_skip`) · `fixing` (agent; skippable) · `summary` (agent; skippable) · `publishing` (orchestrator/git; not configurable per task). **Agent-routed** = {refinement, planning, implementation, review, fixing, summary}; **skippable** = {planning, testing, review, fixing, summary}. Model/reasoning precedence: `stages.<stage>` → task-wide → provider default.

**Proposed maximal `task-rich.md`:**

```markdown
---
id: task-webhook-retry-budget
title: "Add a bounded retry budget to webhook delivery"
pr_title: "feat(webhooks): bounded retry budget for delivery" # overrides the auto-generated PR title (omit to auto-generate)
refined: false # true = skip the refinement stage (criteria below already make it complete)
decompose: false # true = force split into subtasks / false = disable / omit = config default
auto_merge: false # true = auto-merge (DANGER: skips human review; only if config git.auto_merge_allow_per_task) / false = opt out / omit = config default
contacts: # handles surfaced for human-in-the-loop prompts and approvals
  - "@team-lead"
  - "@webhooks-oncall"
agents: # per-stage provider override — only agent-routed stages; only providers in agents.allowed
  refinement: claude
  planning: claude
  implementation: codex
  review: claude
  fixing: codex
  summary: claude
model: claude-sonnet-4-6 # task-wide default model for agent-routed stages not overridden under `stages`
reasoning: medium # task-wide default reasoning: low | medium | high | xhigh | max
stages: # per-stage overrides; precedence: stages.<stage> -> task-wide -> provider default
  refinement:
    model: claude-opus-4-8
  planning:
    model: claude-opus-4-8
    reasoning: high
  implementation:
    model: claude-sonnet-4-6
    reasoning: medium
  review:
    reasoning: high # only reasoning overridden — model stays the task-wide default
  fixing:
    model: claude-sonnet-4-6
  testing:
    enabled: true # testing is skippable but runs no agent -> only `enabled` is valid here (no model/reasoning); false would skip checks (rarely wanted)
  summary:
    enabled:
      false # (illustrative) skip a stage; routing & `enabled` are independent. Skippable: planning, testing, review, fixing, summary.
      # skipping `review` also needs agents.allow_review_skip; implementation/refinement are never skippable; publishing is not per-task.
---

## Description

Webhook delivery currently retries forever on failure. Add a bounded retry budget: stop retrying after a fixed number of failed attempts and mark the delivery as exhausted. Store the attempt count on the existing delivery record and leave the successful-delivery path unchanged.

## Acceptance criteria

- [ ] A failed webhook delivery increments an attempt counter on the delivery record.
- [ ] Delivery stops retrying after 5 failed attempts and the record is marked `exhausted`.
- [ ] A successful delivery still marks the record as `delivered` and does not increment the counter.
- [ ] Add or update tests for retry exhaustion and the success path.

## Constraints

- Do not change the public webhook payload shape.
- Do not add a new queue or storage backend; reuse the existing delivery record.
- No new runtime dependencies without approval.
```

**Impl.** Rewrite the packaged `src/wastech_orchestrator/worc/examples/task-rich.md`; operators receive it via `upgrade-docs` (overwrite) or `init`/`install`. Optionally mirror the field table into `worc/README.md` / `worc/best-practices.md`. No code or schema change; the example must be valid YAML and parse cleanly through the §19 task gate.

**Actualize the supporting `worc/` docs.** Audit (2026-06-14): `README.md`, `best-practices.md`, and `decision-guide.md` are already comprehensive and accurate — `README.md`'s field table already lists all 11 keys (incl. `pr_title`/`auto_merge`), and the stage sets / reasoning levels match the source of truth. The concrete gaps to close so the docs and the new maximal example stay in lockstep:

- **`decision-guide.md`** — add a short "`pr_title` — override the PR title" section; it is the only manifest knob without a _when-to-use_ entry (today it appears only in `README.md`'s table).
- **`best-practices.md`** — add a one-line practice for `pr_title` (e.g. set it for a conventional-commit-style PR title that differs from the task `title`); confirm the authoring checklist and the canonical stage/provider names stay current.
- **`README.md`** — keep the field table and hard rules in lockstep with `task/model.py` (`ALLOWED_TASK_KEYS`) and `task/validation_gate.py`; optionally point readers at the maximal `examples/task-rich.md` as the "see every field" reference.
- **Cross-consistency** — the example, the `README.md` table, and `decision-guide.md` must agree on the stage sets (agent-routed / skippable), the reasoning levels, and the `review`-skip caveat.
- **No other staleness found** — Parts A/B touch operator _config_, not task authoring, so the `worc/` docs need no change for them; the removed `templates/task.md` is already superseded by these `examples/`.

## 9. Security requirements

- Templates remain **prompt-text-only**; removing files or changing how a present file is detected does not grant a template any new influence (provider/sandbox/approvals/env are untouched).
- The packaged defaults remain the trusted fallback source (read from the wheel); operator-edited files in `templates_dir` are operator-controlled text rendered through the existing safe renderer (allowlisted `{...}` variables only).
- No secrets are read or written; no behavior here weakens the sandbox or the denied paths.

## 10. Testing requirements

- **Delivery (Part A):** the delivered/`init`ed tree contains `prompts/` only; `skills/`, `AGENTS.md`, `CLAUDE.md`, `task.md` are absent; the `init` ↔ `install-templates` parity test still passes against the narrowed tree.
- **Resolution (Part B):** file present ⇒ used per `mode`; file absent ⇒ packaged default; `replace` is the default; `append` still concatenates packaged + file; `templates_dir: ""` forces packaged defaults for every stage.
- **Config/migration:** a config with legacy `overrides`/`strict` keys still loads (ignored/warned); `upgrade-config` strips them and bumps `schema_version`; `config.example.yaml` round-trips under the new schema.
- **No fail-closed path:** there is no longer any fail-closed-on-missing-prompt-file behavior (the removed `strict`); a missing `<stage>.md` falls back to the packaged default. The §2.2 dedup decision is covered by a test that pins the chosen behavior.

## 11. Acceptance criteria

- [ ] The delivered `templates/` tree contains **only** `prompts/`; `skills/`, `AGENTS.md`, `CLAUDE.md`, `task.md` are no longer delivered by `init` / `install-templates`, and the repo's own root `AGENTS.md` / `CLAUDE.md` are untouched.
- [ ] A `<stage>.md` present in `templates_dir` is used automatically (no `overrides` needed); a missing stage file falls back to the packaged default; an empty `templates_dir` forces packaged defaults.
- [ ] `mode` defaults to `replace`; `append` remains available and behaves as before.
- [ ] `prompts.overrides` **and** `strict` are removed from the schema; legacy `overrides`/`strict` keys do not break loading; `upgrade-config` strips them and `schema_version` is bumped.
- [ ] `strict` is fully removed (schema, the loader allowed-keys set, the `PromptTemplateStore` fail-closed branch, and the `config.example.yaml` line) with no fail-closed-on-missing-file path remaining; the §2.2 skill-dedup behavior is explicitly decided and covered by tests.
- [ ] Spec (§20.2/§20.5 + the `prompts:` section), the `install-templates` design doc §11, README, `CHANGELOG.md`, `operations.md`, `config.example.yaml`, and the `worc/` task-authoring guide are updated; the docs-sync gate passes.
- [ ] **(Part C)** `worc/examples/task-rich.md` is rewritten to exercise every front-matter field (`pr_title`, `auto_merge`, all agent-routed stages under `agents`, `stages.<stage>.model`/`reasoning`/`enabled`, `model`/`reasoning`, `decompose`, `refined`, `contacts`) with inline rule annotations and valid YAML; `task-minimal.md` stays minimal; delivered via `upgrade-docs`.
- [ ] **(Part C)** the supporting `worc/` docs are actualized: `decision-guide.md` gains a `pr_title` section, `best-practices.md` a `pr_title` note, and `README.md`'s field table / hard rules stay in lockstep with `task/model.py` + `task/validation_gate.py` (example, README, decision-guide agree on stage sets, reasoning levels, and the `review`-skip caveat).

## 12. References

- Delivery: [`cli.cmd_init`](../../src/wastech_orchestrator/cli.py), `cli._copy_templates_tree`, `cli._iter_template_files`, [`cli.cmd_install_templates`](../../src/wastech_orchestrator/cli.py); the design record [task_install_templates_command.md](task_install_templates_command.md) (§11 "Scope of the tree").
- Resolution: [`core/prompts.py`](../../src/wastech_orchestrator/core/prompts.py) `PromptTemplateStore` / `_packaged_default` / `override_for`; [`config/schema.py`](../../src/wastech_orchestrator/config/schema.py) `PromptsConfig`; `config/loader.py` / `config/validation.py`; the `prompts:` block in [config.example.yaml](../../src/wastech_orchestrator/templates/config.example.yaml).
- Skills (unchanged feature, for context): [`core/skills.py`](../../src/wastech_orchestrator/core/skills.py) (`SkillInventoryScanner`, `compute_skill_dedup`).
- Part C — task manifest fields (source of truth): [`task/model.py`](../../src/wastech_orchestrator/task/model.py) (`ALLOWED_TASK_KEYS`, `StageParams`, `NormalizedTask`), [`task/validation_gate.py`](../../src/wastech_orchestrator/task/validation_gate.py) (reasoning levels, routable/skippable rules), spec §5/§19.3; delivered example `src/wastech_orchestrator/worc/examples/task-rich.md` via `cli._copy_worc_docs` / `cmd_upgrade_docs`.
- Migration: [`cli.cmd_upgrade_config`](../../src/wastech_orchestrator/cli.py), [`config/upgrade.py`](../../src/wastech_orchestrator/config/upgrade.py).
- Tracking rows and related follow-ups (umbrella `upgrade`, schema migration runner, prompt customization v1): [follow_ups.md](follow_ups.md).
