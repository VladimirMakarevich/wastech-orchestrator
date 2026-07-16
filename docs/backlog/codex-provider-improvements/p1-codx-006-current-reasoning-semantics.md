# CODX-006 — Implement current Codex reasoning semantics

**Status:** open
**Priority:** P1
**Source finding:** CXP-06
**Dependencies:** none
**Officially supported CLI versions:** `codex` **≥ 0.144.4**

## Problem

The provider accepts a provider-wide scalar reasoning value. Light is rejected, Max is silently
normalized to xhigh, and Ultra is rejected. Current Codex semantics distinguish aliases/scalar
effort from Max compute behavior and Ultra multi-agent execution.

## Required outcome

The configuration and runtime must represent the user's requested Codex reasoning mode exactly.
Unsupported modes must fail before model execution; they must never be silently lowered or encoded
using an unrelated config key.

## In scope

- Add documented aliases: light → low and extra-high/extra_high → xhigh.
- Remove the max → xhigh normalization.
- Separate scalar reasoning effort, Max compute mode and Ultra/multi-agent mode in the typed request.
- Validate modes against the selected model and installed CLI capability.
- Project each supported mode through the documented non-interactive Codex mechanism.
- Preserve the effective mode in request/result observability.
- Define cross-provider fallback behavior for all new provider-specific fields.
- Update config schema/version, loader, validation, examples and task-authoring documentation.

## Acceptance criteria

- [ ] Light produces the documented CLI low setting.
- [ ] Low, medium, high and xhigh retain exact current behavior.
- [ ] Extra-high and extra_high normalize explicitly to xhigh.
- [ ] Max is never sent as xhigh and never silently downgraded.
- [ ] Ultra is never encoded as model_reasoning_effort.
- [ ] When the CLI/model supports native Max or Ultra, the generated invocation selects it exactly.
- [ ] When the CLI/model does not support the requested mode, preflight/config validation reports
      capability unavailable before a paid model turn.
- [ ] Ultra execution has bounded concurrency, timeout/cancellation propagation and auditable child
      activity when the native CLI exposes those controls.
- [ ] Resume retains the requested/effective mode or rejects an incompatible continuation clearly.
- [ ] Cross-provider fallback drops Codex-specific compute/agent modes rather than leaking them to
      Claude.
- [ ] Tests prove that no code path contains max → xhigh fallback behavior.

## Verification

- Table-driven normalization and model-capability tests.
- Fresh/resume argv snapshots for every supported mode.
- Unsupported CLI/model tests with no process/model run.
- Ultra cancellation and observability tests when native Ultra is available.
- Opt-in contract smoke against the supported Codex CLI baseline.
- Full config/provider/routing tests and project quality gates.

## Out of scope

- Implementing a custom orchestrator fan-out engine to imitate Ultra when Codex lacks native Ultra.
- Dynamically choosing a reasoning level based on task content.
- Cross-vendor session transfer.
- Unbounded subagent concurrency or task-controlled budget increases.

## Likely implementation areas

- src/wastech_orchestrator/providers/capabilities.py
- src/wastech_orchestrator/providers/base.py
- src/wastech_orchestrator/providers/codex.py
- src/wastech_orchestrator/config
- src/wastech_orchestrator/routing/router.py
- tests/config, tests/providers and tests/routing
- docs/configuration.md and docs/task-authoring.md
