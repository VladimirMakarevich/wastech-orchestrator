# CODX-012 — Strengthen the Codex CLI contract preflight

**Status:** open
**Priority:** P2
**Source finding:** CXP-10
**Dependencies:** CODX-002, CODX-006, CODX-008

## Problem

Codex preflight primarily checks command --version and the presence of a small subset of help
options. It does not prove that the installed CLI supports the complete argv/config contract used by
fresh, resume, structured-output, sandbox, reasoning and capability paths.

## Required outcome

Preflight must detect an incompatible Codex CLI before task admission by probing every required
command, flag, grammar and controlled config capability without performing a model request.

## In scope

- Define a version-independent required CLI contract.
- Probe top-level, exec and exec-resume help surfaces.
- Verify every mandatory flag used by the provider.
- Verify controlled config keys and strict-config behavior.
- Report optional features separately from baseline incompatibility.
- Produce actionable ProviderHealth diagnostics.
- Cache probes only within a process lifetime and invalidate by resolved executable/version.

## Acceptance criteria

- [ ] Baseline probe covers approval policy, exec, cd, sandbox, JSONL and last-message output.
- [ ] Structured-output support is checked before a schema-requiring node can run.
- [ ] Resume command grammar and all resume-compatible provider options are checked.
- [ ] Controlled offline config/rules support from CODX-002/CODX-003 is verified.
- [ ] Reasoning/capability controls from CODX-006/CODX-008 are represented in the capability report.
- [ ] Unknown controlled config keys fail through strict config rather than being ignored.
- [ ] Missing baseline behavior sets supports_required_features=false before task admission.
- [ ] Optional unsupported features do not block nodes that did not request them.
- [ ] Probe uses scratch output, mandatory timeouts and redaction.
- [ ] No paid model request is made.

## Verification

- Fake CLI help fixtures for supported, old, partial and malformed versions.
- One-missing-flag/key tests for every required contract element.
- Fresh/resume request-specific capability tests.
- Cache invalidation tests for command path/version changes.
- Opt-in real Codex 0.142.5 contract smoke.
- Full provider/preflight and project quality gates.

## Out of scope

- Authentication status; CODX-010.
- Model entitlement or remaining account capacity.
- Pinning one exact Codex CLI version when feature probing is sufficient.
- Automatically installing or upgrading Codex.

## Likely implementation areas

- src/wastech_orchestrator/providers/_adapter_base.py
- src/wastech_orchestrator/providers/codex.py
- src/wastech_orchestrator/providers/base.py
- tests/providers/test_codex_run.py
- tests/providers/test_codex_windows_helper.py
- docs/operations.md
