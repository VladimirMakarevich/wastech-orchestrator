# Hybrid agent testing

Status: **accepted** — not scheduled. Date: 2026-06-14 Updated: 2026-06-15 Owner: Vladimir Makarevich

## Goal

Add an optional **read-only test-quality evaluator**. It inspects the implementation **and the tests that the `implementation` agent authored** and returns a bounded `accept` / `rework` verdict on test coverage and quality, before the deterministic `CheckRunner` runs. It **never writes files** and never becomes the publish authority.

Two invariants frame the whole feature:

> Tests are authored by the `implementation` agent (in its editing lineage), never by this evaluator.
>
> Publishing is allowed only by the deterministic approved check profile and its exit codes.

`checks/discovery` is retained because it resolves which command argv the deterministic gate runs.

## Program context and sibling changes

This document is one independently implementable part of the [agent quality and continuity program](README.md#agent-quality-and-continuity-program):

- [Workflow execution foundation](workflow_execution_foundation.md) is the prerequisite and owns the read-only `evaluator` run kind (`role = test_quality`), session scope, immutable run-artifact allocation, the shared evaluator-loop primitive, and the quality-action/bounded-rework vocabulary;
- [Supervisor quality-gate](supervisor_quality_gate.md) owns the sibling evaluator instance (`role = supervisor`) and the rework-accounting helpers this feature reuses;
- **this document** owns the test-quality evaluator: its verdict schema, evaluation checkpoint, bounded rework loop, integration before `CheckRunner`, and the always-on Check Runner commit-candidate mutation guard;
- [Durable sessions and implementation/fixing affinity](durable_sessions_and_fixing_affinity.md) owns resumable editing lineage and provider-aware session handling.

Cross-change contracts:

1. The test-quality evaluator runs **read-only in its own session** and cannot read or update the implementation/fixing editing lineage. It writes nothing to the workspace.
2. Supervisor and the test-quality evaluator are both instances of the foundation's read-only `evaluator` primitive (`role = supervisor` vs `role = test_quality`) and may share low-level invocation, fallback, artifact, schema-validation, and audit plumbing. They remain separate domain components because they evaluate different things; **neither writes the workspace.**
3. The deterministic Check Runner remains the sole publish gate even when the evaluator disagrees.
4. Evaluator fallback remains infrastructure-only. A returned `rework` verdict is a quality result, not a reason to switch providers.
5. Database/config schema changes use the next available schema version (forward only; greenfield — no deployed data to migrate). This feature is last in the quality program, so increment from the version durable sessions shipped.

In the canonical order this feature lands **after** supervisor and durable sessions (foundation → supervisor → durable → hybrid). It must be after supervisor because the flow below assumes the mandatory supervisor evaluation already runs, and it reuses the supervisor's evaluator-loop and rework-accounting machinery.

## Current reality

- `testing` currently runs only `CheckRunner`.
- `checks/agent.py` performs advisory check-command discovery; it is not a testing-stage agent and must not be deleted or repurposed.
- `Stage.TESTING` exists but is not an agent-routable stage — and it stays that way: the evaluator runs through its own config route and a non-stage prompt path (like the supervisor), not by adding `Stage.TESTING` to `ROUTABLE_STAGES`.
- The claimed `only_allowed_paths` / `no_unexpected_files` output guards were never implemented. Under the read-only model they are unnecessary for the evaluator (it writes nothing); the relevant guard is the always-on Check Runner mutation detection below.
- A resolved profile with zero commands currently passes; that is unsafe — authored tests would never execute.

## Testing flow

The mandatory supervisor's fresh read-only evaluation and deterministic dangerous-change guard complete before the pipeline enters this flow. Supervisor rework returns to fixing without invoking the test-quality evaluator or Check Runner for the rejected edit.

```text
implementation / fixing edit   # the implementation agent wrote the code AND its tests
  -> supervisor evaluation (existing; rework -> fixing)
  -> status: testing
  -> if agents.testing.enabled and no applied test_quality verdict for this source edit run:
       run the read-only test-quality evaluator (own session)
         -> accept: continue
         -> rework (>= medium finding): enter fixing so the implementation agent improves the tests
              (bounded by the rework budget; on exhaustion continue with the gaps recorded)
         -> unavailable (all providers exhausted): record unavailable and continue (non-blocking)
  -> capture pre-check snapshot
  -> deterministic CheckRunner executes the approved resolved profile   # the sole publish gate
  -> classify any unexpected commit-candidate changes the checks produced (always-on guard)
  -> green: reviewing
  -> red quality result: fixing
  -> launch failure: existing bounded infrastructure re-resolution path
```

The evaluator re-runs whenever the pipeline reaches `testing` after a test-changing edit (like the supervisor evaluates each implementation/fixing), bounded by the rework budget — not "once per unit". It is **optional and non-blocking**: unlike the mandatory supervisor checkpoints, if the evaluator cannot produce a verdict (provider exhaustion) the pipeline still proceeds to the deterministic Check Runner.

## Test-quality evaluator component

A dedicated domain component — an instance of the foundation's read-only evaluator primitive (`role = test_quality`), not a renamed supervisor. It:

- uses a packaged `testing.md` prompt through a safe **non-stage template path** (as the supervisor uses `supervisor.md`), so `Stage.TESTING` need not become agent-routable;
- runs **read-only** (no `workspace-write`);
- receives task, plan, the current diff (including the authored tests), the active subtask, and the approved resolved-check-profile path;
- returns a strictly validated verdict (the supervisor's shape): `{ verdict: accept|rework, findings: [{ severity, reason, paths }] }`; `rework` requires at least one `medium`/`high` finding, low findings are advisory and cannot block;
- cannot mutate the approved profile or launch any command through the Core;
- never writes files, commits, pushes, or publishes.

Consume the foundation's shared low-level invocation, fallback, artifact-allocation, and audit contracts. Test-quality-specific verdict schema and the testing-stage integration stay in this feature.

## Checkpoint and recovery

Persist one immutable evaluation per source edit run (the same model as the supervisor), keyed at least by:

```text
task_id
evaluation_kind        # test_quality
target_stage           # testing
subtask_order
source_edit_run_id     # the implementation/fixing run whose tests are evaluated
input_checksum
outcome                # accept | rework | unavailable
feedback_path
applied
created_at
```

Evaluation and application are separate idempotent facts; applying a `rework` and consuming its budget is transactional so recovery cannot apply it twice. Recovery in `testing` consults this table keyed by `source_edit_run_id` before invoking the evaluator: a matching record resumes from its `outcome`/`applied` state; otherwise the evaluator runs. There are **no pre/post diff checksums** — the evaluator writes nothing, so there is nothing to apply twice.

Audit attempts under the foundation's `run_kind = evaluator` with `role = test_quality`. Use the next available `DB_SCHEMA_VERSION` (forward only; greenfield), incrementing from the version durable sessions shipped.

## Rework limits and termination

Reuse the supervisor's generic rework accounting (`record_rework(...)`), do not invent a parallel counter:

- every applied test-quality `rework` increments the task-wide `fix_iterations` budget exactly once, through `record_rework` — never also via a second path;
- the local limit is scoped by `(role=test_quality, target_stage=testing, subtask_order)` and is derived by counting applied test-quality verdicts (no separate mutable counter);
- on local-budget exhaustion the workflow does **not** fail or block: it continues to review/publish with the residual coverage gaps recorded in the evaluation artifact (the deterministic Check Runner still governs publish).

## No agent edit policy (the evaluator writes nothing)

There is **no testing-agent workspace delta to validate** — the evaluator is read-only. Tests are authored by the `implementation` agent and are governed by the **existing** implementation scoped-staging and dangerous-diff controls, not by a separate test-path guard. This removes the former test-path / deletion / dependency / partial-edit machinery entirely.

## Check Runner authority

- The evaluator may read and explain the approved resolved profile.
- Its findings are advisory with respect to _which checks run_: it can never launch a command, add a check, or trigger profile re-resolution.
- A failed test does not trigger profile re-resolution by itself. Existing proof-driven re-resolution and human-approval rules remain authoritative.
- Hybrid testing requires at least one approved command unless the operator explicitly configured `checks.discovery.mode: disabled`; that no-gate mode must be prominently audited. Enforced at **preflight/validation**: without a command, the tests the implementation agent authored would never execute, so the task fails closed before reaching the testing stage.
- Snapshot before and after Check Runner. Unexpected tracked/untracked commit-candidate mutations produced by checks must be classified before review or publish, even when exit code is zero. This guard is **always-on** — it ships with this feature and applies on every run, including when `agents.testing.enabled` is false (a check that auto-formats or regenerates files mutates the workspace regardless of the optional evaluator). It is the one intentional always-on behavior change; see the Definition of done.

## Infrastructure policy

If all providers for the evaluator are unavailable, record the evaluation `unavailable` and continue to the deterministic Check Runner — the evaluator is optional and non-blocking (this differs from the mandatory supervisor checkpoints, which stop in `manual_action_required`). A returned `rework` verdict is a quality result and never triggers provider fallback.

## Session isolation

The test-quality evaluator is an independent read-only reviewer:

- session policy is `fresh_each_pass` (consistent with the supervisor) — its own session, never resumed into a later stage, and **never** the implementation/fixing editing lineage;
- no implementation/fixing session ID is supplied; any returned evaluator session ID is audit-only or discarded per the shared redaction policy;
- each decomposed subtask receives an independent evaluation per source edit run.

See [durable sessions](durable_sessions_and_fixing_affinity.md) for the provider-level contract.

## Configuration and skip semantics

The evaluator runs through its own trusted route under `agents.testing` (like `agents.supervisor`); `Stage.TESTING` is **not** added to the agent-routable stage set.

```yaml
agents:
  testing:
    enabled: false
    primary: codex
    fallback: claude
    model: null
    reasoning: low
    timeout_seconds: 600
    max_rework_per_stage: 1
```

The feature defaults off. `agents.testing.enabled: false` disables only the optional test-quality evaluator. `stages.testing.enabled: false` or the existing global stage skip disables both the evaluator and the deterministic Check Runner, preserving current skip behavior and warnings.

Per-task model/reasoning overrides may still apply through the existing `stages.testing` fields, but task content cannot enable/reconfigure the evaluator's authority or replace the approved check profile. (There is no `allowed_test_paths` — the evaluator writes nothing.)

## Expected touchpoints

- dedicated read-only test-quality evaluator component (an evaluator-primitive instance);
- `core/orchestrator.py` testing flow and recovery;
- `core/loop_control.py` — reuse the supervisor's generic `record_rework` accounting;
- `state_store.py` immutable evaluations and the `run_kind = evaluator` / `role = test_quality` audit fields;
- `config/schema.py`, loader, validation, upgrade, and examples (`agents.testing` route + budget);
- a safe non-stage prompt path for `templates/prompts/testing.md` (like `supervisor.md`);
- provider-neutral `resolved_check_profile_path`;
- Check Runner before/after commit-candidate mutation detection (always-on);
- canonical plan, user docs, and `CHANGELOG.md`.

## Minimum tests

- the evaluator is read-only and writes nothing; any attempted workspace mutation fails closed;
- tests are authored by the `implementation` agent, not the evaluator;
- an applied test-quality `rework` increments `fix_iterations` exactly once (via `record_rework`, never also a second path) and re-enters fixing;
- the local test-quality limit is derived from applied verdicts for `(role=test_quality, target_stage=testing, subtask_order)`, independent of the global budget;
- on rework-budget exhaustion the task continues to review/publish with recorded gaps, never blocks;
- the evaluator is non-blocking: provider exhaustion records `unavailable` and proceeds to Check Runner (contrast: mandatory supervisor exhaustion stops in `manual_action_required`);
- recovery in `testing` consults the evaluations table keyed by `source_edit_run_id` before re-running; crash before/after application does not duplicate evaluation or budget use;
- decomposed subtasks receive independent evaluations;
- the evaluator starts in its own fresh session and cannot read or update editing lineage;
- the evaluator cannot launch commands, add checks, or trigger profile re-resolution;
- an empty profile is accepted only under explicit discovery-disabled mode;
- with the testing stage active and no approved command (discovery not disabled), preflight fails;
- green checks that mutate commit-candidate files do not proceed silently, including when `agents.testing.enabled` is false (the always-on Check Runner mutation guard);
- deterministic green/red results override any evaluator opinion;
- `run_kind = evaluator` / `role = test_quality` runs do not become `Stage` values;
- skip semantics distinguish the optional evaluator from the entire testing stage.

## Definition of done

- Deterministic checks remain the sole publish gate.
- Tests are authored by the `implementation` agent; the test-quality evaluator is read-only and writes nothing.
- The evaluator is an instance of the shared read-only evaluator primitive with bounded, restart-idempotent, session-isolated rework; on exhaustion it continues with recorded gaps.
- Existing testing/check pass-gate behavior is unchanged when `agents.testing.enabled` is false, except for the one intentional always-on addition: the Check Runner commit-candidate mutation guard runs on every task.
- `ruff`, `mypy`, and `pytest` pass.
- Canonical plan, configuration docs/examples, `how-it-works.md`, backlog/follow-ups, and `CHANGELOG.md` are updated in the same implementation change.
