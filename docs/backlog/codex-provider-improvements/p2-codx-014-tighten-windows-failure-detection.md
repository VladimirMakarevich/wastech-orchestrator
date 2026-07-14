# CODX-014 — Tighten Windows sandbox failure detection

**Status:** open
**Priority:** P2
**Source finding:** CXP-12
**Dependencies:** none

## Problem

The post-success Windows guard correctly catches the real sandbox false-success incident, but its
regular expression also matches a bare helper executable name and a broad windows-sandbox prefix.
A benign diagnostic can therefore turn a valid success into permission_denied and trigger fallback.

## Required outcome

Detect only evidence that proves a fatal Windows sandbox setup/runtime failure while preserving all
known incident signatures on both zero and nonzero exit paths.

## In scope

- Replace broad text fragments with contextual/error-specific signatures.
- Keep setup-helper, CreateProcessWithLogonW and filesystem-helper failures covered.
- Share signature logic between normal error classification and post-success validation.
- Add explicit negative fixtures for benign diagnostics.
- Preserve redaction and operator-facing error messages.

## Acceptance criteria

- [ ] The exact 2026-07-14 incident stderr still converts exit 0 success into permission_denied.
- [ ] Known nonzero helper/runtime failures retain their current classification.
- [ ] Mentioning codex-windows-sandbox-setup.exe without a failure does not change success.
- [ ] Informational windows sandbox diagnostics do not trigger fallback.
- [ ] Matching requires an error verb/code/context, not only a component name.
- [ ] Matching is case-insensitive only where the real CLI output requires it.
- [ ] A false-success correction remains observable in result/router artifacts.
- [ ] Fresh and resume paths use the same guard.

## Verification

- Regression fixtures copied from the existing post-mortem and tests.
- Negative table of helper path, version, discovery and successful setup messages.
- Near-miss tests around every retained signature.
- Router test proving benign stderr stays on Codex without fallback.
- Windows opt-in smoke where available.
- Full provider/routing tests and project gates.

## Out of scope

- Fixing the host seclogon/PowerShell installation.
- Removing the false-success guard.
- Redesigning general error taxonomy.
- Changing stop-tree behavior.

## Likely implementation areas

- src/wastech_orchestrator/providers/codex.py
- tests/providers/test_codex_run.py
- tests/providers/test_codex_windows_helper.py
- docs/analysis/2026-07-14-codex-windows-sandbox-false-success.md if resolution is appended
