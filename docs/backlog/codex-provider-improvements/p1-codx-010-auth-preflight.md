# CODX-010 — Verify Codex authentication during preflight

**Status:** open
**Priority:** P1
**Source finding:** CXP-09
**Dependencies:** none
**Officially supported CLI versions:** `codex` **≥ 0.144.4**

## Problem

The shared preflight marks authenticated=true after a successful version probe without checking
Codex authentication. An installed but logged-out CLI can therefore be reported healthy and fail
only after the orchestrator admits and starts a task.

## Required outcome

Codex preflight performs a safe, bounded authentication-status probe and reports installed,
compatible and authenticated as independent facts.

## In scope

- Add a provider-specific auth probe using the supported Codex login-status command.
- Parse logged-in, logged-out and indeterminate outcomes without exposing credentials.
- Apply the same allowlisted environment and timeout discipline as other probes.
- Return an actionable ProviderHealth message.
- Decide readiness from authentication without performing a model request.

## Acceptance criteria

- [ ] Logged-in Codex reports authenticated=true.
- [ ] Logged-out Codex reports authenticated=false and not ready.
- [ ] Unsupported login-status command reports authenticated unknown/false with a compatibility
      explanation; it is never reported true by assumption.
- [ ] Timeout, spawn failure and malformed output are classified separately.
- [ ] Probe stdout/stderr are redacted and written only to scratch storage.
- [ ] No token, account secret or auth file content appears in logs/artifacts.
- [ ] Preflight performs no paid model turn and does not mutate login state.
- [ ] Claude preflight behavior changes only if the shared health type requires an explicit unknown.

## Verification

- Fake-runner tests for logged-in, logged-out, old CLI, timeout and malformed output.
- Redaction test with a synthetic credential in probe output.
- Opt-in real CLI smoke for the logged-in path.
- CLI/operator output snapshot tests.
- Full project quality gates.

## Out of scope

- Running codex login or refreshing credentials.
- Storing or migrating credentials.
- Checking subscription quota/capacity.
- Validating model entitlement.

## Likely implementation areas

- src/wastech_orchestrator/providers/_adapter_base.py
- src/wastech_orchestrator/providers/codex.py
- src/wastech_orchestrator/providers/base.py
- tests/providers/test_codex_run.py
- preflight CLI/reporting tests
- docs/operations.md
