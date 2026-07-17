# CODX-015 — Restore ruff line-length compliance

**Status:** completed
**Priority:** P2
**Source finding:** CXP-13
**Dependencies:** none
**Officially supported CLI versions:** `codex` **≥ 0.144.4**

## Problem

The repository fails ruff check because seven comment lines exceed the configured 100-character
limit: four in providers/codex.py and three in tests/providers/test_codex_run.py.

## Required outcome

Restore a green ruff check without changing provider behavior, test meaning or lint configuration.

## In scope

- Reflow the seven reported comments to the configured line length.
- Keep incident explanations understandable.
- Run formatter and lint checks over the repository.

## Acceptance criteria

- [x] ruff check . exits 0.
- [x] ruff format --check . exits 0.
- [x] No noqa, per-file ignore or line-length increase is introduced.
- [x] No executable code or test assertion behavior changes.
- [x] The Windows false-success rationale remains complete and readable.
- [ ] Full pytest remains green.

## Verification

- ruff check .
- ruff format --check .
- pytest for Codex provider tests.
- Full Definition of Done gates.

## Out of scope

- Refactoring Windows error detection; CODX-014.
- Changing lint thresholds.
- Unrelated formatting cleanup.
- Fixing mypy platform errors; CODX-011.

## Likely implementation areas

- src/wastech_orchestrator/providers/codex.py
- tests/providers/test_codex_run.py
