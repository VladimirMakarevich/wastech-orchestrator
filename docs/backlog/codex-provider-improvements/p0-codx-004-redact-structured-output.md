# CODX-004 — Redact structured provider output before persistence

**Status:** postponed
**Priority:** P0
**Source finding:** CXP-04
**Dependencies:** none
**Officially supported CLI versions:** `codex` **≥ 0.144.4**

## Problem

The shared adapter redacts final_message and usage but copies parsed.structured_output directly into
AgentRunResult. That mapping can then be written to result.json, evaluator findings, state and other
artifacts with literal secrets intact.

## Required outcome

All structured provider data must pass through the same recursive redaction boundary as text output
before it is returned to core or written to any durable sink.

## In scope

- Redact nested mappings, lists and scalar strings in structured_output.
- Reuse the complete extra-secret set, including denied-path values.
- Ensure the redacted object is the only object exposed through AgentRunResult.
- Audit downstream structured-output sinks for bypasses.
- Cover both Codex and Claude because the defect is in the shared adapter.

## Acceptance criteria

- [ ] Literal secrets are removed from nested dict/list structured output.
- [ ] Structural secret patterns and configured extra secrets use the existing canonical redaction
      marker.
- [ ] result.json, findings artifacts, prompt audit, logs and SQLite contain no original value.
- [ ] Redaction does not mutate the provider parser's input object in place.
- [ ] Non-secret JSON types and schema shape are preserved.
- [ ] Invalid or non-mapping provider structured output retains existing validation behavior.
- [ ] Session ID normalization continues to work after structured redaction.
- [ ] Tests cover Codex last-message schema output and Claude event-stream structured output.

## Verification

- End-to-end sink test with unique secrets at multiple nesting depths.
- Regression test using a value harvested from a denied_read_paths file.
- Tests for lists, null, booleans, numbers and repeated object references.
- Full provider, evaluator/state and artifact test suites.
- Full project quality gates.

## Out of scope

- Preventing the agent from reading denied files; tracked by CODX-003.
- Eliminating raw temporary output files; tracked by CODX-005.
- Changing output schemas or evaluator semantics.

## Likely implementation areas

- src/wastech_orchestrator/providers/_adapter_base.py
- src/wastech_orchestrator/providers/redaction.py
- src/wastech_orchestrator/providers/artifacts.py
- tests/providers/test_redaction_sinks.py
- downstream evaluator/state artifact tests
