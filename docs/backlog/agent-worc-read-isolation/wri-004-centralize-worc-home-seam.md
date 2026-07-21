# WRI-004 — Centralize the `.worc` home literal into one injectable seam

**Status:** open **Phase:** 2 (hard isolation) — prerequisite **Source:** [decision record](README.md), [follow_ups.md](follow_ups.md) **Dependencies:** WRI-001

## Problem

The `.worc` home is a hardcoded literal duplicated across the codebase — `WORC_HOME` in `cli.py` and `_WORC_HOME` in `core/orchestrator.py` (already tracked as tech debt in [follow_ups.md](follow_ups.md)). Phase-2 relocation (WRI-005) needs a single, injectable private-home path; scattered literals make that unsafe.

## Required outcome

One source of truth for the private-home location, resolved through the composition/config seam, so the home can later be pointed outside the working tree without editing call sites. No behavior change in this task — the home stays at `<repo>/.worc/`; this only builds the seam.

## In scope

- Collapse the duplicated `.worc` literal into a single constant/config-resolved value.
- Thread the private-home path through composition so no consumer assumes `<repo>/.worc`.
- Leave the default path exactly as today (`<repo>/.worc/`).

## Acceptance criteria

- [ ] A single source resolves the private-home path; the `WORC_HOME` / `_WORC_HOME` duplication is removed.
- [ ] All consumers obtain the home through the seam; no new hardcoded `.worc` literal is introduced (guard with grep / an import-linter or unit check).
- [ ] No functional change — the resolved default path is unchanged; the full suite is green.
- [ ] The corresponding [follow_ups.md](follow_ups.md) entry is closed/updated.

## Verification

- A test or lint asserting there is one definition of the home literal.
- Full suite green with the default path unchanged.

## Out of scope

- Actually moving the home outside the tree (WRI-005).
- Any read-deny mechanism.

## Likely implementation areas

- src/wastech_orchestrator/cli.py
- src/wastech_orchestrator/core/orchestrator.py
- src/wastech_orchestrator/composition.py
- src/wastech_orchestrator/config
- docs/backlog/follow_ups.md (close the duplicate-literal entry)
