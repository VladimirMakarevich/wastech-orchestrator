# P0.2 — an accepting evaluator's findings must reach the operator

Priority: **P0** Status: **proposal** Date: 2026-07-25 Source: [postmortem.md](postmortem.md) DR-2 Escalates: [VF-18](../issues/runtime-validation-findings.md)

## Problem

When an evaluator returns `accept` with findings, those findings terminate at the node. They are written to `findings.json` and the `evaluations` table and go nowhere else — not the `artifacts` table, not `summary.json`, not `summary.md`, not the PR body. The operator's only view of the run then states the opposite of what happened.

This is the generalisation of VF-18 (which observed the same loss on `review`'s `low` findings). On `p9-09` it lost a `medium` finding, and the PR body told the operator that all three gates _"passed"_.

## Evidence

Three independent missing wires, each verifiable at a single call site:

1. [`core/flow/nodes/evaluator.py:210-214`](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py) builds `NodeResult(... NodeOutcome(kind, findings=..., rework_exhausted=...))` with **no `final_message=`**. `agent.py:866` is the only site in the codebase that passes `final_message` into a `NodeOutcome`. The provider did return the findings as its final message — verbatim in `stages/critical_review/run-000082/summary.md` — and it is dropped one layer up.
2. [`core/orchestrator.py:3111-3117`](../../../src/wastech_orchestrator/core/orchestrator.py) calls `observe(..., final_message=outcome.final_message)`. `outcome.findings` is populated and in scope at that exact line, and is not passed.
3. [`core/supervisor.py:1047-1053`](../../../src/wastech_orchestrator/core/supervisor.py) (`_step_prompt`) has no slot for findings, so the supervisor's prompt for the critic step was 1 563 bytes ending in `## Step observed / Node: critical_review / Outcome: accept`. It made zero tool calls on that step — as on the `citation_check` and `fact_verification` steps — because there was nothing to react to.

Grep proof of the loss: `grep -c "Uneven audit depth" pr_body_appended.md` → 0.

## Change

Minimum viable: wire 1 alone. Pass the evaluator's provider-authored final message into `NodeOutcome.final_message`, which immediately makes it visible to the summariser and therefore to the PR body.

Complete: additionally forward `outcome.findings` at wire 2 and render a findings digest into `_step_prompt` at wire 3, so the supervisor sees what it is acknowledging rather than a bare outcome label.

Prefer a structured merge over raw prose for the operator surface: accepted-with-findings entries should land in `summary.json`'s follow-ups list (deduped against the supervisor's own list) so they reach the PR body in the existing `## Technical debt / follow-ups` section — the mechanism already exists and other tasks in the same PR use it.

## Acceptance

- An evaluator that returns `accept` with N ≥ 1 findings produces N entries on the operator surface (summary + PR body), each carrying severity, path and the finding text.
- The summariser can no longer describe a gate as having "passed" when it emitted findings.
- Zero findings still produces no section (no empty heading).
- Dedup against supervisor-authored follow-ups is exact-match safe (no duplicated bullet when both produce the same item).

## Test

Unit: an evaluator run with `accept` + one `medium` finding yields a non-empty `NodeOutcome.final_message` and a follow-up entry in the built summary payload. Integration: a flow whose evaluator accepts with findings produces a PR body containing them.

## Scope / risk

Orchestrator default — affects every flow and every repo, which is the point. Risk is summary bloat on flows with chatty evaluators; bound it by carrying severity + one-line `what`, not the full `fix` prose.

## Depends on

Independent of [P0.1](p0-1-evaluator-gate-severity.md), but they are complements: P0.1 makes substantive findings gate, P0.2 makes the ones that legitimately do not gate still visible. Shipping only P0.1 would hide sub-threshold findings exactly as today.
