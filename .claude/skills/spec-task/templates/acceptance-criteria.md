<!-- Testable pass/fail criteria. Each maps back to a requirement (FR-/NFR-). Written so a reviewer —
     or a test — can objectively say "met" or "not met". Prefer Given/When/Then. -->

# Acceptance criteria — {{TASK_TITLE}}

Verifies [requirements.md](requirements.md).

## Scenarios

### AC-1 — maps to FR-1

- **Given** `<initial state: a task file, a config, a flow, a repository state>`
- **When** `<the operator runs …>`
- **Then** `<observable outcome: the status, the artifact, the log line, the branch>`

### AC-2 — maps to FR-2

- **Given** …
- **When** …
- **Then** …

## Edge cases & error states

<!-- The ones this repository actually hits: a provider infrastructure failure, an exhausted account,
     a timeout, a dirty or diverged worktree, an invalid task or config, a rejected flag, an
     interrupted run resumed later, an empty or first-run state. -->

- **AC-E1** — given `<the failure>` → `<the expected fail-closed behavior and the status it routes to>`.

## Non-functional checks

<!-- One line per NFR that can be observed: the platform matrix, no secret in the log, the argv built
     as a list, the cost or token bound, the audit row that must exist. -->

- **AC-N1 (from NFR-1)** — `<how it is verified on Windows and on POSIX>`.

## Verification method

<!-- For each AC: a unit test, an integration test driven by deterministic fake CLIs, a gate
     (`ruff` / `mypy` / `lint-imports` / `pytest` / `python tools/mdlint.py`), or a real run named
     explicitly. Tests are mandatory for new or changed behavior — see `.agents/rules/testing.md`. -->

| AC   | How it is verified | Where |
| ---- | ------------------ | ----- |
| AC-1 |                    |       |
