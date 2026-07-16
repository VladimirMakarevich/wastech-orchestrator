# CODX-011 — Restore cross-platform mypy compliance

**Status:** open
**Priority:** P1
**Source finding:** CXP-13
**Dependencies:** none
**Officially supported CLI versions:** `codex` **≥ 0.144.4**

## Problem

Running mypy from the repository's Windows virtual environment reports that os.killpg and
signal.SIGKILL do not exist in Windows platform stubs. Runtime platform guards are present, but the
implementation is not statically narrowed in a cross-platform-safe way.

## Required outcome

The process-tree termination implementation passes mypy on Windows and POSIX without weakening
typing rules or changing stop semantics.

## In scope

- Refactor POSIX-only symbol access behind a statically valid platform-specific seam.
- Preserve the injected/testable process-control boundary.
- Keep Windows taskkill behavior and POSIX process-group/descendant behavior unchanged.
- Add tests that exercise platform dispatch without relying on the host platform.

## Acceptance criteria

- [ ] mypy src exits 0 from the supported Windows development environment.
- [ ] mypy src remains green on Linux/macOS CI.
- [ ] No global mypy exclusion, weakened strictness or broad type-ignore is introduced.
- [ ] POSIX still sends SIGKILL to the process group and required descendants.
- [ ] Windows still uses the injected tree killer/taskkill path.
- [ ] Unsupported-platform behavior remains explicit and tested.
- [ ] Existing timeout, cancellation and orphan-prevention tests remain green.

## Verification

- Unit tests with mocked platform dispatch and signal/process functions.
- Windows and POSIX CI/type-check jobs where available.
- Existing process.py and process_control stop-ladder test suites.
- Full Definition of Done gates.

## Out of scope

- Redesigning the stop ladder.
- Adding a new terminal cancelled task state.
- Changing timeout values or escalation order.
- Silencing mypy errors through configuration.

## Likely implementation areas

- src/wastech_orchestrator/providers/process.py
- src/wastech_orchestrator/process_control.py if the shared seam is reused
- tests/providers and tests/process-control
- CI platform matrix if required
