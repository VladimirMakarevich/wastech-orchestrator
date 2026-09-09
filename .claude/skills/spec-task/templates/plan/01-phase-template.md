<!-- One phase of the plan. Copy this file per phase and rename it NN-<kebab-name>.md. A phase is
     small, ordered by dependency, and leaves the repository green: gates pass, docs synced, one
     commit. Link it from the plan README so nothing is orphaned. -->

# Phase {{NN}} — {{PHASE_TITLE}}

- **Status:** ☐ <!-- ☐ todo · ◑ in progress · ☑ done -->
- **Depends on:** `<phase numbers, or none>`
- **Delivers:** `<the concrete outcome this phase produces>`

## Goal

<!-- What this phase achieves and which acceptance criteria it moves toward. -->

## Steps

1. `<ordered, concrete step — name the module and function to add or change>`
2. …

## Files touched

<!-- The critical few, create or modify, as repository-relative paths. -->

- `src/wastech_orchestrator/...`
- `tests/...`

## Invariants in play

<!-- The hard rules this phase must honor: provider syntax stays in `providers/`, no publication
     mandate, fallback only for provider infrastructure classes, the flag ceiling, allowlisted env
     only, argv as a list, cross-platform paths and line endings. -->

## Tests

<!-- What gets a test here and how it is driven: unit seams, fake-CLI integration coverage, the
     Windows and POSIX cases. -->

## Docs to sync in this phase

<!-- The documents on this branch that this phase's change makes stale — `.agents/rules/`,
     `README.md`, and the shipped copies under `src/wastech_orchestrator/packaged/`. -->

## Acceptance for this phase

- [ ] `<observable, checkable outcome — tie it to an AC where relevant>`
- [ ] `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports` and `pytest` are green.
