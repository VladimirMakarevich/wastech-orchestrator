# Hybrid agent testing

Status: **accepted** — not scheduled.
Date: 2026-06-14
Owner: Vladimir Makarevich

## Goal

Add an optional testing agent that can inspect the implementation and author or repair test files
before the existing deterministic `CheckRunner` executes. The agent improves test coverage and
interpretation; it never becomes the publish authority.

The invariant remains:

> Publishing is allowed only by the deterministic approved check profile and its exit codes.

`checks/discovery` is retained because it resolves which command argv the deterministic gate runs.

## Program context and sibling changes

This document is one independently implementable part of the
[agent quality and continuity program](README.md#agent-quality-and-continuity-program):

- [Workflow execution foundation](workflow_execution_foundation.md) is the prerequisite and owns
  the `testing_agent` execution role, fresh-session scope, immutable run-artifact allocation, and
  reusable exact-delta/path-containment primitives;
- [Supervisor quality-gate](supervisor_quality_gate.md) owns read-only AI evaluation and rework
  verdicts;
- **this document** owns the optional workspace-writing testing agent, its checkpoint, diff policy,
  and integration before `CheckRunner`;
- [Durable sessions and implementation/fixing affinity](durable_sessions_and_fixing_affinity.md)
  owns resumable editing lineage and provider-aware session handling.

Cross-change contracts:

1. The testing agent always starts a fresh session and cannot read or update implementation/fixing
   editing lineage.
2. Supervisor and testing can share low-level invocation, fallback, artifact, schema-validation,
   and audit plumbing, but not a common domain policy: their permissions and outputs differ.
3. The deterministic Check Runner remains authoritative even when the testing agent disagrees.
4. Testing-agent fallback remains infrastructure-only. A returned quality failure is not a reason
   to switch providers.
5. Database/config migrations use the next available schema version at implementation time.

This feature may land before durable sessions by explicitly sending no session ID and discarding
any returned testing-agent session handle. Once the session feature lands, the same behavior must
be represented as an explicit fresh/disposable session scope.

## Current reality

- `testing` currently runs only `CheckRunner`.
- `checks/agent.py` performs advisory check-command discovery; it is not a testing-stage agent and
  must not be deleted or repurposed.
- `Stage.TESTING` exists but is not an agent-routable stage.
- The code does not currently implement the claimed `only_allowed_paths` /
  `no_unexpected_files` output guards.
- A resolved profile with zero commands currently passes; that is unsafe as an implicit hybrid
  testing gate.

## Corrected testing flow

The mandatory supervisor's fresh read-only evaluation and deterministic dangerous-change guard
complete before the pipeline enters the flow below. Supervisor rework returns to fixing without
invoking the testing agent or Check Runner for the rejected edit.

```text
implementation (or first entry for a subtask)
  -> status: testing
  -> if no completed testing-agent checkpoint for this execution unit:
       capture pre-agent snapshot
       run testing agent in a fresh workspace-write session
       validate exact testing-agent diff delta
       persist completed/unavailable checkpoint
       refresh current.diff
  -> capture pre-check snapshot
  -> deterministic CheckRunner executes the approved resolved profile
  -> validate that checks left no unexpected commit-candidate changes
  -> green: reviewing
  -> red quality result: fixing
  -> launch failure: existing bounded infrastructure re-resolution path

fixing
  -> testing
  -> reuse completed testing-agent checkpoint; do not run the agent again
  -> deterministic CheckRunner
```

The testing agent runs once per root execution unit or decomposed subtask, not on every fixing
cycle. A fresh operator `rerun` may create a new checkpoint; automatic fixing cycles may not.

## Testing-agent component

Create a dedicated domain component, not a renamed supervisor. It:

- uses a packaged `testing.md` prompt;
- runs with `workspace-write`;
- receives task, plan, current diff, active subtask, and the approved resolved-check-profile path;
- may add or modify test-owned files;
- returns bounded advisory output such as `{ran, notes, suggested_checks}`;
- cannot mutate the approved profile or launch arbitrary suggested commands through the Core;
- never commits, pushes, or publishes.

Consume the foundation's shared low-level invocation contracts. Any additional shared types should
remain limited to explicit context paths, provider fallback, artifact allocation, and audit
metadata; testing-specific checkpoint and edit policy stay in this feature.

## Checkpoint and recovery

Persist an idempotent checkpoint containing at least:

```text
task_id
subtask_order
source_edit_run_id
pre_agent_diff_checksum
testing_agent_run_id
outcome                  # completed | unavailable
post_agent_diff_checksum
created_at
```

Recovery from `testing` consults this checkpoint before invoking the agent. The checkpoint and
accepted diff-delta result must be durable before the deterministic checks start, so a crash cannot
apply the workspace-writing agent twice.

Use a generic auxiliary run role compatible with the supervisor design, such as
`run_kind = testing_agent`, without pretending that the testing agent is equivalent to a read-only
supervisor.

If another feature migrates `state.db` first, use the next available schema version.

## Deterministic edit policy

Validate the exact workspace delta created by the testing agent, not the entire task diff.
Pre-existing implementation changes are legitimate input and must not be falsely rejected.

Default allowed changes:

- add or modify files under deterministically recognized test roots;
- add or modify recognized test filenames according to trusted repository policy.

Default forbidden changes:

- production source;
- CI workflows;
- check/quality-gate configuration;
- dependency manifests or lockfiles;
- orchestration/task artifacts;
- deletion or rename of existing tests;
- unrelated tracked or untracked files.

Repository-specific extra test paths belong in trusted operator config, never task content or agent
output.

After the test-path guard, run dangerous-diff classification over the testing-agent delta as a
second layer. An out-of-policy production edit is already a violation and cannot become acceptable
merely because it is not a deletion/dependency change.

## Check Runner authority

- The testing agent may read and explain the approved resolved profile.
- `suggested_checks` are advisory only and are never launched automatically.
- A failed test does not trigger profile re-resolution by itself.
- Existing proof-driven re-resolution and human approval rules remain authoritative.
- Hybrid testing requires at least one approved command unless the operator explicitly configured
  `checks.discovery.mode: disabled`; that no-gate mode must be prominently audited.
- Snapshot before and after Check Runner. Unexpected tracked/untracked commit-candidate mutations
  produced by checks must be classified before review or publish, even when exit code is zero.

## Infrastructure and partial-edit policy

If all providers are unavailable and no workspace delta exists, record the testing-agent phase as
`unavailable` and continue to deterministic checks.

If an infrastructure failure leaves partial edits:

- calculate the exact delta;
- apply the same test-path, deletion, dependency, and dangerous-change guardrails;
- pass only a valid partial diff to infrastructure fallback;
- stop fail-closed on invalid or ambiguous changes.

Agent quality failure remains a returned result and does not trigger provider fallback.

## Session isolation

The testing agent is an independent author/reviewer:

- request session scope is fresh/disposable;
- no implementation/fixing session ID is supplied;
- any returned testing-agent session ID is audit-only or discarded according to the shared
  redaction policy;
- it never becomes active editing lineage;
- each decomposed subtask receives an independent testing-agent run/checkpoint.

See [durable sessions](durable_sessions_and_fixing_affinity.md) for the provider-level contract.

## Configuration and skip semantics

Add `Stage.TESTING` to agent routing while preserving its existing user-visible stage:

```yaml
agents:
  testing:
    enabled: false
    model: null
    reasoning: low
    timeout_seconds: 600
    allowed_test_paths: []
  routing:
    testing:
      primary: codex
      fallback: claude
```

The feature defaults off. `agents.testing.enabled: false` disables only the optional testing-agent
phase. `stages.testing.enabled: false` or the existing global stage skip disables both the optional
agent and deterministic Check Runner, preserving current skip behavior and warnings.

Per-task model/reasoning overrides may still apply through the existing `stages.testing` fields,
but task content cannot expand allowed test paths or replace the approved check profile.

## Expected touchpoints

- dedicated testing-agent domain component;
- `core/orchestrator.py` testing flow and recovery;
- deterministic before/after diff-delta guard;
- `state_store.py` checkpoint and generic auxiliary run role;
- `config/schema.py`, loader, validation, upgrade, and examples;
- routing support for `Stage.TESTING`;
- provider-neutral `resolved_check_profile_path`;
- `templates/prompts/testing.md`;
- Check Runner workspace-mutation detection;
- canonical plan, user docs, and `CHANGELOG.md`.

## Minimum tests

- testing agent runs once per execution unit across multiple fixing cycles;
- decomposed subtasks receive independent checkpoints;
- restart before/after checkpoint persistence cannot duplicate invocation or edits;
- only the testing-agent delta is validated;
- test additions/modifications pass under trusted path policy;
- production/config/CI/dependency edits and test deletion/rename fail closed;
- testing starts fresh and cannot update editing lineage;
- infrastructure exhaustion with no edits continues to Check Runner;
- invalid partial edits stop and valid partial edits follow infra-only fallback;
- `suggested_checks` cannot change or execute outside the approved profile;
- an empty profile is accepted only under explicit discovery-disabled mode;
- green checks that mutate commit-candidate files do not proceed silently;
- deterministic green/red results override any testing-agent opinion;
- skip semantics distinguish the optional agent from the entire testing stage.

## Definition of done

- Deterministic checks remain the sole publish gate.
- Testing-agent execution is once-per-unit, restart-idempotent, and session-isolated.
- Agent edits are constrained by an implemented deterministic delta policy.
- Existing behavior is unchanged when `agents.testing.enabled` is false.
- `ruff`, `mypy`, and `pytest` pass.
- Canonical plan, configuration docs/examples, `how-it-works.md`, backlog/follow-ups, and
  `CHANGELOG.md` are updated in the same implementation change.
