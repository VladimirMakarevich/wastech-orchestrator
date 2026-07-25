# P2.8 — let a node's real output cross the edge, not just a pointer to its sign-off

Priority: **P2** Status: **accepted** Date: 2026-07-25 Source: [postmortem.md](postmortem.md) DR-4

## Problem

Every agent node in `deep_research` runs `fresh_disposable`, so a node knows only what its rendered prompt gives it — and what the prompt gives it is a **file path and nothing else**. The whole pipeline is carried by three sentences hand-written into role files. The nodes were disciplined and opened what they were pointed at, so nothing failed; but the pointers were systematically thinner than the artifacts, and 79% of the structuring node's output never reached the writer.

## Evidence

| Edge | What crossed | Opened? |
| --- | --- | --- |
| `repository_analysis` → `external_research` | path to `.out.md` (10 139 B) | yes |
| `architecture_design` → `synthesis` | path to `.out.md` (**4 042 B**) | yes |
| — the 19 821 B `report-structure.md` it actually wrote | **not referenced** | **no** |
| `citation_check` → `fact_verification` | nothing (`checks_path` set only on failure) | impossible |
| `fact_verification` → `critical_review` | `{"findings": []}` — 21 bytes | yes |
| `critical_review` → `publish` | nothing — 5 461 B of findings die at the node | — |

`architecture_design` wrote a 295-line blueprint and its `.out.md` says so: _"The full blueprint with every citation and recommended direction is in the written file."_ `synthesis` was pointed at the 4 KB chat sign-off, ran `ls`, saw the file, and wrote _"`report-structure.md` in that directory is a prior stage's artifact — I left it untouched."_ The only node that opened the blueprint was the supervisor, which cannot act on it.

Measurable loss: the blueprint cited `sec.ts:196` as BL-1 evidence; the final report dropped it. The critic separately observed that the draft's BL-1 wording is **more precise** than the shipped report's — the handoff degraded the deliverable.

Cost of the weak channel: `repository_analysis` read 400 450 B across 60 files and emitted 10 139 B (39.5× condensation, 2.5% survives). The four downstream nodes then re-read **407 071 B**, essentially the same twelve files each time. Within-session caching absorbs most of the token cost, so the real price is wall clock and divergence — and the divergence was real: coverage evidence never travelled, so the report could only relay a label.

Structural causes:

- [`core/flow/prompt_vars.py:26-36`](../../../src/wastech_orchestrator/core/flow/prompt_vars.py) — `node_output_vars()` is documented as "a path to a Core-written, redacted artifact, **never inlined content**"; enforced at [`core/prompts.py:55-90`](../../../src/wastech_orchestrator/core/prompts.py).
- [`providers/base.py:215-235`](../../../src/wastech_orchestrator/providers/base.py) — `build_context_footer` has a fixed six-slot shape (`task / plan / diff / checks / review / human_input`) with **no upstream-output slot**, so handoff depends on a prompt author remembering to hand-write `{<node_id>_path}` into prose.
- [`core/flow/nodes/evaluator.py:378-386`](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py) — `_prompt_variables()` never calls `_node_output_paths` (the agent path does, at `agent.py:606-610` / `:669`), so evaluators **structurally cannot** reference an upstream node's output. That is why `verifier.md` and `critic.md` hardcode `{repo}/docs/research/{task_id}/report.md`.
- [`core/flow/postprocess.py:183-189`](../../../src/wastech_orchestrator/core/flow/postprocess.py) — `_slot_content` publishes `structured_output["content"]` or `final_message`, i.e. the chat sign-off. A node whose real product is a written file publishes only its summary.

## Change

Three separable pieces, in increasing cost:

1. **Publish the produced file, not just the sign-off.** Let a node declare a produced-file path in its structured output and publish _that_ to the exchange. `exchange_publish.py:158-185` (`publish_node_run_file`) already accepts arbitrary content — this is a `_slot_content` change, not new machinery. Fixes the blueprint loss directly and is the cheapest of the three.
2. **Give evaluators the node-output channel.** Mirror `agent.py:606-610` / `:669` into `evaluator.py`, so `{<node_id>_path}` resolves for evaluators and the hardcoded repo paths in `verifier.md` / `critic.md` can go away.
3. **Add an upstream slot to the footer**, fed from `_node_output_paths`, so handoff stops depending on prose. Optionally a size-bounded, redacted `{<node_id>_content}` companion for small outputs — the redaction path already exists at `postprocess.py:154-157`, but see [P0.3](p0-3-redaction-false-positive.md): do not inline content through a redactor that currently corrupts benign identifiers.

The "never inline content" doctrine is deliberate and documented in the shipped guide; piece 3's content variant is the only part that touches it, and it should be argued on its own merits rather than smuggled in with pieces 1–2.

## Acceptance

- A node whose deliverable is a written file makes that file addressable downstream, and the downstream node opens it.
- `{<node_id>_path}` resolves inside an evaluator role file; the hardcoded `{repo}/docs/research/{task_id}/report.md` strings are removed from the packaged verifier and critic prompts.
- Redundant re-reads across nodes measurably drop on the next `deep_research` run (baseline: 407 071 B).

## Test

Unit: `_slot_content` prefers a declared produced-file path when present and falls back to `final_message`. Unit: an evaluator's prompt variables include upstream node-output paths. Integration: a two-node fixture flow where node B's prompt resolves node A's produced file.

## Scope / risk

Orchestrator default, all flows. Risk is contained for pieces 1–2 (they add a channel, they do not change what an existing prompt resolves). Piece 3 changes the shape of every rendered prompt and should ship separately, behind its own review.

## Depends on

[P0.3](p0-3-redaction-false-positive.md) before any content-inlining variant of piece 3 — inlining through a redactor that mangles `tokens:` would push the corruption into every downstream prompt rather than only into logs.
