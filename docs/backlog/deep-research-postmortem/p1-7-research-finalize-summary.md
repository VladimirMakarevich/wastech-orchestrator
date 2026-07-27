# P1.7 — give `deep_research` its own finalize lens, and stop the summary fabricating

Priority: **P1** Status: **implemented** (2026-07-26) Date: 2026-07-25 Source: [postmortem.md](postmortem.md) DR-3

## Implemented

Items 1, 2 and 3, plus the `config.example.yaml` correction. Item 4 is answered rather than implemented — see below.

**The field is `flow.supervisor`, not `supervision`.** Both this document and the campaign README name a `supervision:` block; the flow schema has no such key and, since every flow mapping is a fail-closed allowlist, writing it would have been a fatal load error rather than a silently ignored one. The real key is `supervisor:` (`SupervisorBlock`: `role_file` / `finalize_role_file` / `handoff_role_file` / `emit_follow_ups`), which is what the flow now declares — `finalize_role_file: deep_research/summary.md` and nothing else. `emit_follow_ups` stays off: it is a code-oriented capability, and a research flow's evaluator findings already reach `summary.{json,md}` and the PR body through VF-18.

**Item 2, the no-fabrication rule, is two rules rather than one**, because the run produced two distinct failure shapes. "Claim nothing you did not do" bans the first-person verification claim (the turn is a read-only observer; it describes what the _pipeline_ did, in the third person, and attributes an independent check to the node that actually performed it). "No number, verdict or count you were not given" bans the other half — "the citation check independently passed all 41 entries" was written on a step with zero tool calls, and it was 39 verified plus 2 `uncheckable`. The prompt names both claim shapes explicitly and tells the turn to prefer a qualitative statement over invented precision.

**Item 3 is a generic orchestrator change, not a `deep_research` one.** `finalize()` now reads the `evaluations` rows once and renders every in-flow evaluator's **final** verdict plus its findings into the finalize prompt under `## Gate verdicts recorded for this task`, with the instruction that a gate which recorded findings "did **not** simply pass". Details worth recording:

- The last verdict per `(node_id, subtask_order)` wins, reusing exactly the keying VF-18 established — a superseded rework round is not shown, and a decomposed task does not let subtask N evict subtask N-1.
- `_last_verdict_per_node` and `_row_findings` are factored out of `_evaluator_finding_follow_ups`, so the digest and the follow-ups cannot drift apart on which verdict counts.
- One store read now feeds both (it used to happen after the turn, for follow-ups only).
- Finding reasons are cut at the same `_FINDING_TITLE_MAX` bound the observation digest and the follow-up titles use, so a chatty evaluator cannot inflate the turn.
- A flow with no in-flow evaluator renders no section at all, rather than an empty heading.

Every flow gains this, which is the point: "all gates passed" was writable in any flow with a non-blocking evaluator, not only this one.

**Item 4 — the model.** Not changed, and this is the honest answer rather than a deferral: `SupervisorConfig` carries one `model`/`reasoning` pair for both supervisor roles, so there is no way to upgrade the finalize turn without also upgrading every per-step observation — which is the expensive half and the one [DR-9](postmortem.md) wants to spend _less_ on. Splitting them is already designed in [supervisor-observation-cadence-p1](../token-optimization/supervisor-observation-cadence-p1.md) and belongs there. What this change does instead is remove the reason the weaker model was dangerous: the turn is no longer inventing the gate verdicts, it is being handed them.

The `config.example.yaml` note was correct and the example was inverted — it pinned `claude-opus-5` on the advisory supervisor while the primary provider (which runs every producer node) sat on `claude-sonnet-5`. Corrected to the cheaper tier with the reasoning written out: keep the oversight layer at or below the producers' tier, and note that the one knob also governs the finalize turn, so a flow whose summary matters constrains that turn through its lens rather than through a bigger model. The same guidance is now in the shipped `guide/config/reference.md` and `guide/flows/{README,roles}.md`, since those are what an operator reads after `install`.

Target-only remainder: a target's `.worc/flows/deep_research.yaml` needs the `supervisor:` block and the new `deep_research/summary.md`, and its `.worc/config.yaml` carries whatever supervisor model the operator chose — the packaged example is only the default for a fresh `install`.

## Problem

The operator's only view of a $12.93 Opus-xhigh audit is written by `claude-sonnet-5` at `medium` reasoning, from session memory, with one tool call — through a finalize lens designed for code changes. Two of its claims on `p9-09` are false, and a third re-presents as a virtue the exact fact the critic filed as a `medium` defect.

## Evidence

- `stages/supervisor/run-000000/1-claude/request.json`: `model = claude-sonnet-5`, `reasoning = medium`, tools `Read,Glob,Grep`. Source is `.worc/config.yaml` `supervisor:` → [`config/schema.py:501-518`](../../../src/wastech_orchestrator/config/schema.py), loaded at [`config/loader.py:707-726`](../../../src/wastech_orchestrator/config/loader.py). Producers ran `claude-opus-4-8` / `xhigh`.
- Fabrication 1, in `summary.md` and the PR body: _"At each stage **I independently re-opened and spot-checked** the cited code (`sec.ts`, `lint-files.ts`, `table.ts`, `regex.ts`, `grp.ts`, `content.ts`, plus the STR-001/TBL-004 guide pages)."_ Tool-call census across all 8 supervisor runs: **10 calls, 9 files**. It never opened `content.ts`, `STR-001.md`, or `TBL-004.md`.
- Fabrication 2, supervisor step run-000080, zero tool calls, never read `citation.json`: _"the automated citation check independently passed **all 41 entries**."_ It was 39 verified + 2 `uncheckable`.
- Third problem: _"No subsystem was silently skipped; areas only spot-checked at call sites … are explicitly named as such."_ — presented as a strength, while the critic had filed exactly that as the run's central `medium` weakness.
- And: _"three independent verification gates … **all of which passed**"_, with four unmentioned critic findings sitting in `state.db`.

Root cause: `deep_research.yaml` declares no `supervision:` block, so `_finalize_base()` falls through to `_BUILTIN_FINALIZE` ([`core/supervisor.py:176-181`](../../../src/wastech_orchestrator/core/supervisor.py)) — a code-flow lens ("grounded in the actual committed change") applied to a research deliverable whose "change" is three new Markdown files.

## Change

1. Add `supervision: { finalize_role_file: deep_research/summary.md }` to the flow, and write a research-specific lens: what question was asked, what the deliverable concludes, what confidence it claims and on what evidence base, what the gates said, and what remains open.
2. **Forbid first-person verification claims.** The finalize turn must describe what the pipeline did, not assert that it personally re-opened files. If a claim of independent verification is wanted, it has to be sourced from a node that actually performed it.
3. Feed it the evaluator findings ([P0.2](p0-2-evaluator-findings-surfacing.md)) so "all gates passed" becomes unwritable when a gate emitted findings.
4. Reconsider the model for the finalize turn specifically. The per-step layer is cheap advisory work and Sonnet is right for it; the finalize turn is the operator's only artifact and is worth the stronger model. This is separable from [VF-16](../issues/runtime-validation-findings.md)'s global model discussion because `SupervisorConfig` currently has one model for both roles — splitting observe/finalize models is already proposed in [supervisor-observation-cadence-p1](../token-optimization/supervisor-observation-cadence-p1.md).

## Acceptance

- A `deep_research` run's `summary.md` describes the research deliverable, not "the committed change".
- The summary contains no first-person claim of file inspection that the supervisor's own tool log does not support.
- A gate that emitted findings is never described as having simply "passed".
- The summary states the deliverable's coverage claim and its basis, so an operator can tell a thin audit from a thorough one without opening the artifacts.

## Test

Integration on a fixture research run: the rendered finalize prompt resolves to the new role file; the produced summary contains the gate findings section when findings exist and omits it when they do not.

## Scope / risk

Packaged flow + a new packaged role file, since every `deep_research` user hits this. Low risk — the finalize turn is advisory and cannot route.

Note the packaged `config.example.yaml:266-270` currently suggests the **inverse** of what this repo's operator configured (Opus on the supervisor, Sonnet as the primary provider). The operator's arrangement is the better one; the packaged example should be corrected in the same pass.

## Depends on

[P0.2](p0-2-evaluator-findings-surfacing.md) for item 3 — without it the finalize turn has no findings to render. Items 1, 2 and 4 are independent.
