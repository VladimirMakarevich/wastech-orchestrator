---
name: verify-dod
description: Verify that an implementation phase's Definition of Done is actually met before advancing to the next phase or opening a PR (spec §15 gate). Checks each DoD item against real code/tests with evidence and runs all quality checks. Use before closing a phase, before a commit, or before a PR.
---

# verify-dod

Gate a phase against its Definition of Done. The spec (§15) forbids starting the next phase until
every DoD item of the current one is *documented as complete* — this skill produces that evidence.
Argument: the phase number or file (e.g. `4` or `04_routing_and_fallback.md`). If none is given,
infer the phase under review and confirm.

## Steps

1. Read the phase file in [docs/implementation_stages/](../../../docs/implementation_stages/) and
   extract its **Definition of Done** checklist (and the final §16 DoD for phase 6).
2. For **each** DoD item, verify it against the actual codebase — do **not** tick from memory:
   - locate the implementing module/function and the test(s) that exercise it;
   - cite concrete evidence as `file:line` (e.g. the validator rejecting an illegal footprint pair,
     the router's fallback table, the scoped-staging pathspec, the redaction test);
   - mark the item **PASS** (with evidence) or **FAIL** (with what's missing).
3. Cross-check the hard invariants in
   [architecture.md](../../../docs/rules/architecture.md) ("What must not be done") and
   [security.md](../../../docs/rules/security.md) — these are implicit DoD for every phase
   (core ↛ CLI syntax; only the orchestrator commits/pushes/PRs; fallback infra-only; no secrets in
   logs/SQLite/artifacts; argv-list launches; policy not weakenable by task/`extra_args`).
4. Run the full quality suite (or invoke `/run-checks`):
   ```bash
   ruff check .
   ruff format --check .
   mypy src
   pytest
   ```
5. Report a single checklist: every DoD item with PASS/FAIL + evidence, the invariant cross-check,
   and the check results.

## Rules

- A phase is complete **only** when every DoD box is PASS with evidence **and** all checks are green.
- If any item is FAIL or any check is red, state plainly that the phase is **not** done and list the
  exact gaps — never declare "all green" or advance the phase.
- Evidence must point at real code/tests, not intentions. Missing tests for a behavior = FAIL.

## Output

A concise PASS/FAIL table per DoD item (with `file:line` evidence), the invariant cross-check result,
the check-suite result, and a one-line verdict: **phase N complete** or **phase N blocked: <gaps>**.
