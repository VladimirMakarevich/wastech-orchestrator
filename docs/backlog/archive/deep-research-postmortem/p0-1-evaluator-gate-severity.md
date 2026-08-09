# P0.1 — make a `medium` evaluator finding actually gate

Priority: **P0** Status: **implemented** (2026-07-26) Date: 2026-07-25 Source: [postmortem.md](postmortem.md) DR-1 Escalates: VF-18

## Implemented

Decision on step 2: `medium` on **every packaged flow whose evaluators are quality lenses** — `deep_research` (via `defaults.evaluator`, covering both), `security_audit`, `blog_article`, `blog_article_revise`, `content_chapter`, `content_translate`. The built-in `DEFAULT_GATE_SEVERITY` stays `high`, and `implementation`'s `review` stays `high`: it is a correctness lens on a `blocking: true` node, where a lowered gate changes the exhaustion landing to `manual_action_required`.

Two consequences to know about:

- **The four content critics are `blocking: true`**, so for them exhaustion parks the task rather than accepting with a warning. Their loop budgets are `style_fix: 10` / `style_fix: 10` / `critic_fix: 6` / `critic_fix: 3`. No budget was changed — `content_translate`'s `critic_fix: 3` is the thinnest and the one to raise if pages start parking.
- **Target-only remainder**: an operator's installed `.worc/flows/*.yaml` is a copy, so it needs `worc install` (or a hand edit) to pick up the new default and the restored header comment. Nothing in this repo can change an existing installation.

Step 4 went further than "reconcile the number": both `deep_research` prompts now state the _mechanism_ (the flow decides which severities gate; file everything at its true severity; a sub-threshold finding is carried to the operator, not discarded) instead of restating a threshold that drifts out of the YAML — the pattern the content critics already used. The "carry into the report's Open questions" clause is deleted. `security_audit/verifier.md` already claimed medium gates and is now correct without an edit.

## Problem

`critical_review` produced a correct, specific `medium` finding about the deliverable's central weakness and the engine discarded it. The verdict is not authored by the model — the evaluator's output schema has no `verdict` field — it is computed from `gate_severity`, which defaults to `high`. A `medium` finding can therefore never gate on any flow that does not set the field, and no packaged flow sets it.

The failure is silent in three compounding ways: `max_rework_per_stage: 3` is never consulted (the `accept` returns before the budget branch), `rework_exhausted` stays `False` so the operator warning does not fire, and the role prompt tells the model the opposite of the shipped behavior.

## Evidence

- [`core/flow/schema.py:31`](../../../../src/wastech_orchestrator/core/flow/schema.py) — `DEFAULT_GATE_SEVERITY = "high"`.
- [`core/flow/nodes/evaluator.py:461-468`](../../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py) — gates iff `_severity_rank(severity) <= gate_rank`; `medium` is rank 3, `high` is rank 2.
- [`core/flow/nodes/evaluator.py:265-267`](../../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py) — the early `return "accept", False` precedes the budget branch.
- `.worc/flows/deep_research/critic.md:25-27` — _"severity **medium** or high marks a substantive weakness that should be reworked … accept and let them carry into the report's Open questions."_ Both halves are false: medium does not gate, and nothing carries a critic finding into the report.
- The operator's installed `deep_research.yaml` deleted the three header-comment lines that documented `gate_severity`, so the one knob that would have caught this was undiscoverable.

## Change

1. Set `gate_severity: medium` for `critical_review` in `.worc/flows/deep_research.yaml` — or once via `flow.defaults.evaluator.gate_severity`, which also covers `fact_verification`.
2. Decide the same question for the packaged `packaged/flows/deep_research.yaml`. Argument for `medium` as the packaged default on quality evaluators: `high`/`critical` are correctness severities, and an evaluator whose job is "is this deliverable good enough" has no natural way to emit them.
3. Restore the deleted `gate_severity` line to the flow header comment (both the target copy and, if it drifted, the packaged one).
4. Reconcile `critic.md:25-27` with whatever default is chosen, and delete the "carry into the report's Open questions" clause — that mechanism does not exist.

Do **not** use `blocking: true` as the fix. A blocking evaluator ignores `max_rework_per_stage` and is bounded only by the edge budget, whose exhaustion parks the task in `manual_action_required`. `gate_severity` gives the same sensitivity with a safe landing (accept + warn).

## Acceptance

- A `medium` finding from a non-blocking evaluator routes to `rework` and increments the stage's rework count.
- The rework is bounded by `max_rework_per_stage` and terminates in `accept` with `rework_exhausted = True`, producing the console warning and the ⚠️ Telegram trace.
- No path reaches `manual_action_required` as a result of this change.
- `critic.md` states the same rubric the engine implements.

## Test

Unit: `_verdict()` with `gate_severity="medium"` and a single `medium` finding returns `("rework", False)`; with the budget already spent returns `("accept", True)`. Regression: the existing `gate_severity="high"` behavior is unchanged for `low` findings.

## Scope / risk

Target-only for the flow field; a packaged-default change is a behavior change for every repo and needs the deliberate decision in step 2. Cost risk is bounded and quantified: at most three extra `synthesis` rounds, ≈ +$7 on a run of this shape.

## Depends on

Nothing. This is the cheapest fix in the set and unblocks [P1.4](p1-4-audit-coverage-gate.md) (a coverage gate is pointless if its findings cannot gate) and [P3.10](p3-10-flow-and-config-hygiene.md)'s `resume_own_lineage` item (which only becomes live once a second round can happen).
