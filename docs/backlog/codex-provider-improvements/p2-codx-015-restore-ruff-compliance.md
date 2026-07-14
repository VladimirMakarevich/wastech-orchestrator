# CODX-015 — Restore ruff line-length compliance

**Status:** open
**Priority:** P2
**Source finding:** CXP-13
**Dependencies:** none

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

- [ ] ruff check . exits 0.
- [ ] ruff format --check . exits 0.
- [ ] No noqa, per-file ignore or line-length increase is introduced.
- [ ] No executable code or test assertion behavior changes.
- [ ] The Windows false-success rationale remains complete and readable.
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
