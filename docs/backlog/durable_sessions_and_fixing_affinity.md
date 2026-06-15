# Durable sessions and implementation/fixing affinity

Status: **accepted** — not scheduled.
Date: 2026-06-14
Owner: Vladimir Makarevich

## Goal

Persist the editing conversation used by `implementation` and `fixing` so it survives orchestrator
restart, supports both Claude and Codex, and cannot be overwritten by evaluator stages. `fixing`
should first use the provider and session actually used by accepted implementation output.

The supported promise is affinity while the provider/session is available, not an absolute
availability guarantee. Existing infrastructure-only fallback remains authoritative unless the
operator explicitly chooses strict continuity and accepts `manual_action_required` on loss.

## Program context and sibling changes

This document is one independently implementable part of the
[agent quality and continuity program](README.md#agent-quality-and-continuity-program):

- [Workflow execution foundation](workflow_execution_foundation.md) is the prerequisite and owns
  execution roles, session-scope/lineage-key vocabulary, execution-unit identity, and persisted
  policy identity;
- [Supervisor quality-gate](supervisor_quality_gate.md) always uses fresh read-only sessions and
  cannot update editing lineage;
- [Hybrid agent testing](hybrid_agent_testing.md) is a read-only test-quality evaluator (it never
  writes the workspace); it uses its own evaluator session and cannot update editing lineage;
- **this document** owns provider resume, durable editing lineage, provider/session affinity,
  provider-aware fallback, recovery, and raw session-handle redaction.

Cross-change contracts:

1. Artifacts remain the primary source of truth. Session continuity is an optimization and context
   aid, never the only recovery mechanism.
2. Only `implementation` and `fixing` use the active editing lineage.
3. Mandatory supervisor quality-gate calls and an enabled final handoff pass, plus the test-quality
   evaluator, review, refinement, and planning cannot overwrite editing lineage even when routed to
   the same provider.
4. Provider-specific CLI syntax stays in adapters. Core/Router use only normalized session fields
   and session scope.
5. Database/config schema changes use the next available schema version (forward only; greenfield —
   no deployed data to migrate). Supervisor lands before this feature, so increment from the version
   it shipped rather than assuming v3 -> v4.

This feature lands **after** the supervisor quality-gate, not before it. Because the `implementation`
profile requires supervisor from the start (greenfield "fold"), supervisor ships immediately after
the foundation using ad-hoc fresh requests. This document then formalizes the fresh/disposable and
editing-lineage scopes the supervisor already relies on, and its implementation/fixing affinity is
deliberately ordered around the now-existing mandatory supervisor evaluation (see
[affinity](#implementationfixing-affinity)). It must precede hybrid testing, which also assumes the
mandatory supervisor.

## Current reality and defects

- `_Pipeline.session_ids` is an in-memory provider-to-last-session dictionary.
- Claude captures `session_id` and resumes through `--resume`.
- The current Codex adapter captures a synthetic `session_id` shape but ignores it when building
  argv.
- Current Codex CLI supports resume; the repository adapter and tests are outdated.
- Any later stage on the same provider can overwrite the implementation session before fixing.
- Router clones one primary request for fallback without replacing `session_id`, so a provider can
  receive another provider's ID.
- Session updates are not persisted atomically with stage completion.
- Raw session IDs currently appear in provider result artifacts and can appear in recorded argv.

## Official vendor contracts

- Claude supports `--resume` / `-r`, including non-interactive
  `claude -p --resume <session-id>`. See the official
  [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference) and
  [non-interactive resume example](https://code.claude.com/docs/en/hooks#defer-a-tool-call-for-later).
- Codex supports `codex exec resume <SESSION_ID> [PROMPT]`. See the official
  [Codex CLI reference](https://developers.openai.com/codex/cli/reference#codex-exec).
- Codex JSONL emits
  `{"type":"thread.started","thread_id":"..."}`. See the official
  [Codex non-interactive documentation](https://developers.openai.com/codex/noninteractive#make-output-machine-readable).
- The Codex contract was also verified locally on 2026-06-14 with `codex-cli 0.139.0`.

The entire affinity feature depends on `codex exec resume` existing and accepting the same global
security flags (sandbox/approval) as `codex exec`. The current adapter's `# no --resume equivalent`
comment is outdated and must be removed. Because the feature is unusable without resume, a documented
**minimum Codex version or a side-effect-free capability probe is a hard requirement, not optional**:
an installed Codex that cannot resume safely must fail or explicitly degrade (no silent loss of the
security flags on the resume path).

## Durable editing lineage

Do not persist the current provider-wide “last session” map as the source of truth. Persist one
active lineage per root task/subtask execution unit:

```text
EditingLineage
  task_id
  execution_unit       # the foundation's (task_id, subtask_order); NULL subtask_order = root task
  purpose              # implementation_fixing
  provider
  session_id           # raw value only in state.db
  effective_model
  origin_stage_run_id
  latest_stage_run_id
  status               # active | unavailable | broken | superseded
  break_reason
  created_at
  updated_at
```

A normalized table is preferred over `tasks.session_ids` JSON. Foreign keys and uniqueness should
enforce at most one active `implementation_fixing` lineage per execution unit.

For decomposed tasks, each subtask has an independent provider/session lineage. A fallback in one
subtask cannot alter completed lineage or preselect providers for another.

## Session scopes

Provider-neutral requests need an explicit scope/intent:

- `editing_lineage`: implementation/fixing (the stage authors) may load and update the active
  lineage;
- `fresh_disposable`: the implementation supervisor and the test-quality evaluator receive no prior
  session and cannot update editing lineage;
- an evaluator that holds a multi-round dialogue (e.g. a research `critic`) may instead
  `resume_own_lineage` — its **own** resumable session under a separate lineage key, still
  independent of the author and never sharing `implementation_fixing`.

This removes accidental behavior based on “latest session for provider”.

When enabled, the final supervisor handoff pass that replaces the old summary provider call uses
`fresh_disposable`. It receives complete artifact context and never resumes or rotates the
implementation/fixing lineage. A summary skip creates no supervisor session.

## Provider-aware routing and fallback

Build each Router attempt for the provider that will actually execute it. The per-attempt request
factory/context resolver selects:

- provider permission profile;
- matching provider session for the requested scope;
- provider-neutral context paths;
- partial diff for an eligible fallback.

Never send a Claude ID to Codex or a Codex thread ID to Claude. Core and Router still do not know CLI
syntax.

Fallback remains infrastructure-only. Returned quality failure never switches providers.

## Codex adapter parity

Fresh Codex run remains:

```text
codex [global security flags] exec [options] -
```

Resumed run becomes:

```text
codex [global security flags] exec resume [resume options] <SESSION_ID> -
```

The adapter must:

- parse `thread.started.thread_id` into normalized `AgentRunResult.session_id`;
- preserve sandbox, approval, argv-list, allowed environment, timeout, output-schema, and redaction
  rules;
- update fixtures/tests to documented JSONL rather than synthetic `session` events;
- reject or explicitly degrade unsupported installed versions;
- reject `--ephemeral` for editing stages while durable continuity is enabled.

Claude editing stages likewise reject `--no-session-persistence`.

## Implementation/fixing affinity

Capture affinity after a successful `implementation` provider result and validated session/thread
ID, before the mandatory supervisor evaluates the edit. Do not pin an attempted provider that
failed or returned an unusable result. This ordering is required because supervisor `rework`
transitions directly to fixing, which must be able to resume the implementation conversation that
produced the rejected edit. A later security/guardrail failure still stops fail-closed rather than
using affinity to continue.

Persist the provider actually used, including an eligible fallback provider that completed
implementation.

Before the first fixing attempt:

- resolve the implementation provider/session as fixing primary;
- record route source `session_affinity`;
- reject a conflicting task `agents.fixing` override while affinity is enabled;
- validate that the provider remains configured, allowed, and compatible with fixing permissions.

Recommended config:

```yaml
agents:
  session_affinity:
    implementation_fixing: true
    on_unavailable: fallback  # fallback | manual_action_required
```

- `fallback`: preserve availability, record `continuity_broken`, and use existing infra-only
  fallback;
- `manual_action_required`: strict continuity mode that deliberately sacrifices automatic
  fallback.

After a successful fixing provider result, validate and persist the returned session/thread ID
before mandatory supervisor evaluation and before transitioning to testing/review. Same-status
supervisor rework then resumes the just-updated fixing lineage.

If fixing succeeds through fallback, mark the original continuity broken and make the fallback
provider/session the active fixing lineage for subsequent cycles. Retain only a fingerprint of the
original ID for audit.

## Persistence, atomicity, and recovery

Completing the stage run, recording actual provider, and updating editing lineage share one
StateStore transaction.

Recovery behavior:

- ordinary restart: rehydrate lineage before provider invocation;
- `rerun --continue`: preserve and rehydrate lineage;
- fresh `rerun`: archive prior attempt and clear active lineage/affinity;
- valid redacted result artifact without DB lineage after a crash: reconcile stage/provider facts,
  mark continuity unavailable, and continue/re-run from artifacts;
- never attach an ambiguous or reconstructed ID.

Because raw IDs are intentionally excluded from artifacts, a crash after provider completion but
before the DB transaction can lose continuity. A zero-loss sidecar is possible but is not the
recommended default because it creates another sensitive state channel.

Both vendors resume locally persisted transcripts. Moving only `state.db` to a different host/user
is insufficient; missing vendor-local state follows the unavailable-session policy.

## Resume failure behavior

Classify missing, expired, deleted, or incompatible sessions as normalized
`session_unavailable`.

On that error:

1. record the failed resume attempt;
2. retry the same provider once without resume using complete artifact context;
3. if that provider remains unavailable, apply normal eligible infrastructure fallback;
4. do not consume a quality fix iteration (`fix_iterations` is untouched);
5. record whether continuity was unavailable or broken.

The retry-without-resume **is a normal Router `stage_attempt`** (it counts against
`agents.max_stage_attempts` like any other attempt) but it is **not** a quality fix iteration. This
keeps a single attempt-accounting model: continuity loss never gets a free, unbounded extra attempt,
and it never spends the fix budget. This avoids abandoning a healthy provider merely because its
local transcript is gone.

## Security and redaction

Session/thread IDs are not API credentials, but they are sensitive operational handles to local
conversation history.

- Store raw IDs only in `state.db`.
- Validate vendor ID format before persistence or use.
- Do not include raw IDs in regular logs, `request.json`, `result.json`, audit rows, terminal
  reports, or recorded argv.
- Parse raw provider output in memory, then redact `session_id` / `thread_id` before writing
  `stdout.log` and `events.jsonl`.
- Audit with a one-way fingerprint when correlation is needed.
- Audit requested affinity provider, actual provider, whether resume was attempted/succeeded, and
  normalized continuity-break reason without exposing the raw ID.
- Ensure the local state DB has restrictive filesystem permissions.

The existing result serializer and request argv representation require changes.

## Model and context policy

"Same agent" pins the effective model: persist implementation's `effective_model` on the lineage and
reuse it for fixing. A conflicting task-level fixing **model** override is rejected/audited while
affinity is enabled, exactly like the conflicting `agents.fixing` **provider** override — a resumed
conversation cannot silently switch models. (When affinity is disabled or already broken, the normal
per-stage model resolution applies.)

Unsupported resume/model combinations use the unavailable-session path.

Long sessions may increase cost. Measure resumed input/cached tokens and define an explicit rotation
policy. Rotation starts fresh from artifacts and records `continuity_broken`; it never happens
silently.

## Schema/versioning

Add the lineage table and any route/audit fields using the next available `DB_SCHEMA_VERSION`.
Because supervisor lands before this feature (greenfield "fold" ordering), increment from the
version supervisor shipped — do not assume v3 -> v4. This is forward schema versioning only; there
is no deployed production database to migrate (a developer's local `state.db` may be recreated).

Update:

- schema creation and idempotent version bump (forward only; no deployed-data migration);
- row dataclasses/mapping and store APIs;
- fresh-rerun clearing;
- continue/recovery preservation;
- schema-creation/version-bump tests;
- config schema/version if `agents.session_affinity` is added.

## Expected touchpoints

- `providers/codex.py` parser and resume argv;
- `providers/claude.py` persistence-disabling flag checks and redaction;
- provider artifacts and normalized error classification;
- `routing/router.py` provider-aware per-attempt requests;
- `core/orchestrator.py` lineage scope/load/update;
- `state_store.py` lineage schema and atomic completion API;
- rerun/recovery code;
- config schema/loader/validation/upgrade/examples;
- `how-it-works.md`, canonical plan, operations/security docs, and `CHANGELOG.md`.

## Minimum tests

- Claude fresh/resume argv and capture remain correct;
- Codex parses documented `thread.started.thread_id` and builds `codex exec resume`;
- resume preserves all sandbox/approval constraints;
- unsupported Codex capability follows documented fail/degrade behavior;
- restart and `rerun --continue` rehydrate exact per-unit lineage;
- fresh rerun clears lineage;
- stage completion and lineage update are atomic;
- evaluator/non-editing sessions cannot overwrite editing lineage;
- fallback receives only its own provider session;
- session-unavailable retries same provider once without resume before eligible fallback;
- accepted implementation fallback becomes fixing affinity;
- conflicting task fixing override is rejected/audited;
- successful fixing fallback records break and becomes active fixing lineage;
- decomposed subtasks are isolated;
- persistence-disabling flags are rejected for editing stages;
- invalid IDs are rejected;
- raw IDs do not appear in artifacts/logs/audit/terminal reports;
- missing local vendor transcript state degrades to artifact context;
- configured context rotation records a continuity break.

## Definition of done

- Claude and supported Codex versions resume through provider adapters.
- Editing lineage survives restart without contamination from evaluator stages.
- Affinity and its availability limits are explicit, deterministic, and audited.
- Artifact context remains sufficient when continuity is unavailable.
- Raw session handles are confined to protected state.
- `ruff`, `mypy`, and `pytest` pass.
- Canonical plan, configuration/security/operations docs, `how-it-works.md`, backlog/follow-ups,
  and `CHANGELOG.md` are updated in the same implementation change.
