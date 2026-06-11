---
name: implement-phase
description: Drive one implementation phase of wastech-orchestrator from docs/implementation_stages/ end to end — confirm prerequisites, turn the phase's logical blocks into a TDD plan, implement them respecting the invariants, and close the phase. Use when starting or continuing phase 1–6.
---

# implement-phase

Implement a single phase from [docs/implementation_stages/](../../../docs/implementation_stages/)
following the spec and the rules, block by block, test-first. Argument: the phase number or file
(e.g. `2` or `02_provider_layer.md`). If none is given, infer the current phase from git/code state
and confirm before proceeding.

## Before you start (gate)

1. Read [docs/implementation_stages/README.md](../../../docs/implementation_stages/README.md) for
   the phase order, the module map, and the conventions.
2. Read the target phase file in full — its logical blocks, tests, and DoD.
3. **Confirm the previous phase's DoD is complete** (run `/verify-dod` for it). Phases run *strictly
   in sequence* (spec §15) — do **not** start phase N+1 while phase N has an unchecked DoD item.
   If a prerequisite is unmet, stop and report what's missing instead of proceeding.
4. Read the spec sections the phase cites in [orchestrator_final_plan.md](../../../docs/orchestrator_final_plan.md)
   and the relevant files in [docs/rules/](../../../docs/rules/).

## Steps

1. **Plan from the blocks.** Turn the phase's numbered logical blocks into a `TodoWrite` list, in
   order. Each block is one or a few todos; later blocks depend on earlier ones.
2. **For each block, work test-first** (see [testing.md](../../../docs/rules/testing.md)):
   - write or adjust the unit tests for the behavior first (red);
   - implement the minimal code to satisfy them, in the module the README's map assigns to this
     phase — respect the layer boundaries (`core → router → provider(interface)`; the core never
     builds CLI commands);
   - add integration/e2e coverage when the phase calls for it (fake CLIs via `/fake-cli`, never the
     real Codex/Claude in tests).
3. **Hold the invariants on every block** (see [architecture.md](../../../docs/rules/architecture.md)
   and [security.md](../../../docs/rules/security.md)):
   - the core does not know CLI syntax; provider logic stays in `providers/`;
   - only the orchestrator (Git Manager) commits/pushes/PRs;
   - fallback is infrastructure-only; quality failures go to `fixing`;
   - no secrets in logs/SQLite/artifacts; CLIs launched as an argv list, no shell interpolation;
   - canonical provider/stage/status names only — use the enums, not string literals.
4. **Keep it green.** Run `/run-checks` after each block (or each red→green→refactor cycle); fix the
   cause, never silence a lint/type rule. Keep diffs minimal and in the style of the surrounding code.
5. **Close the phase.** When every block's todos are done, run `/verify-dod <phase>` and only then
   consider the phase complete. Optionally commit via `/commit-change` if that skill exists.

## Rules

- Never skip a phase's prerequisites or its tests to move faster.
- Do not implement behavior the phase defers to a later phase ("Not in this phase") — note it and
  move on.
- If the spec and a phase doc disagree, the spec
  ([orchestrator_final_plan.md](../../../docs/orchestrator_final_plan.md)) wins — flag the
  discrepancy.

## Definition of Done

- Every logical block of the phase is implemented with its tests.
- `/verify-dod <phase>` reports all DoD items satisfied with evidence.
- `ruff check .`, `ruff format --check .`, `mypy src`, `pytest` are green.
