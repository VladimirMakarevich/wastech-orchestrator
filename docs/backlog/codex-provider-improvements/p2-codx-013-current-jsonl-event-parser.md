# CODX-013 — Update the Codex JSONL event parser

**Status:** postponed
**Priority:** P2
**Source finding:** CXP-11
**Dependencies:** none
**Officially supported CLI versions:** `codex` **≥ 0.144.4**

## Problem

The parser recognizes several legacy flat event forms and relies on last-message for the happy path.
Current Codex emits thread.started, turn.started, nested item.completed and turn.completed/failed
events. Nested messages and structured error information are largely ignored, reducing diagnostics
and sometimes collapsing precise failures into process_crashed.

## Required outcome

Parse the supported current Codex JSONL contract explicitly while retaining tolerant handling of
known older fixtures. Terminal success/failure, message, usage, session and error data must be
derived deterministically.

## In scope

- Add typed/internal handling for current thread, turn and item events.
- Extract nested agent messages and supported structured output.
- Parse turn.failed and error events into the existing ErrorClass taxonomy.
- Validate session/thread identifiers before persistence.
- Preserve last-message as the canonical fallback where required by current CLI behavior.
- Define behavior for unknown and malformed events.
- Store representative sanitized fixtures from supported CLI versions.

## Acceptance criteria

- [ ] thread.started yields a validated durable session ID.
- [ ] item.completed agent_message is available when last-message is absent.
- [ ] turn.completed yields terminal success and usage from the current event shape.
- [ ] turn.failed/error events yield the most specific existing error class.
- [ ] Authentication, rate limit, invalid model/schema, session unavailable, permission and
      infrastructure failures are not collapsed unnecessarily.
- [ ] Model/test/review failures do not become fallback-eligible infrastructure errors.
- [ ] Malformed JSON lines do not crash parsing and remain diagnosable through redacted artifacts.
- [ ] Unknown future events are tolerated without manufacturing success.
- [ ] Legacy fixtures required by supported versions remain green.
- [ ] Structured-output parsing still uses last-message JSON when the CLI omits event output.

## Verification

- Sanitized contract fixtures captured from Codex CLI 0.142.5.
- Unit tests for every recognized event and ordering variant.
- Mixed valid/malformed/unknown event stream tests.
- Nonzero exit plus structured error classification tests.
- Fresh/resume and output-schema regressions.
- Full provider/routing test suites and quality gates.

## Out of scope

- Persisting every raw event indefinitely.
- Inventing semantics for undocumented event types.
- Replacing JSONL with an SDK/API provider.
- Changing router fallback policy.

## Likely implementation areas

- src/wastech_orchestrator/providers/codex.py
- src/wastech_orchestrator/providers/errors.py
- tests/providers/test_codex_parsing.py
- tests/providers/test_codex_run.py
- sanitized test fixtures
