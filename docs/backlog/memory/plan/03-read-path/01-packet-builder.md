# 03.1 — PacketBuilder

[phase](index.md) · [design §2,§6](../../design.md) · [acceptance: AC-R3](../../acceptance-criteria.md)

**Goal:** the deterministic, model-free retrieval core — given a node and its task context, select and rank the records that belong in that node's packet.

## Scope

In: `PacketBuilder.build(node/stage, task_context)` — the **stage-1 deterministic filter** (stage/role, touched paths/symbols, task type, recency, trust, entity links) and precision-first top-k ranking. Out: rendering + caps to file (03.2), the prompt variable (03.3), the optional semantic rerank (V3, **not** built here).

## Approach

- **Deterministic filter only** (design §6 stage 1). Stage 2 (semantic rerank) is V3, gated by the replay baseline — V1 ships the filter alone.
- Inputs from `task_context`: touched paths/symbols, task type, current node id. Filter the store down by these, then rank.
- **Precision-first / metadata-first** ranking, trust-weighted; entity-link expansion is bounded so one entity can't flood the packet.
- **Reproducible**: same inputs → same ordered set (AC-R3). Use a stable sort and inject any time input — no hidden clock or randomness in ranking.
- Pure and model-free (FR5) — unit-tested without a model.

## Files

- New `src/wastech_orchestrator/.../memory/packet.py` (`PacketBuilder`).

## Tests

- Same inputs → identical ordered record set (AC-R3).
- Filters honor stage/role, touched paths, task type, trust.
- No LLM call anywhere in the path.

## Done when

`PacketBuilder` returns a deterministic ranked record set per node; AC-R3 groundwork holds.
