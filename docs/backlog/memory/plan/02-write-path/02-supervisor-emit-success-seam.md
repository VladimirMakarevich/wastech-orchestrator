# 02.2 — Supervisor emit (success seam)

[phase](index.md) · [design §2,§9](../../design.md) · [acceptance: AC-W1](../../acceptance-criteria.md)

**Goal:** extend `supervisor.finalize()` to also return a `candidate_memory_delta` from its **existing** turn — with **zero additional LLM calls** (AC-W1) — and wire the publish-node hook to feed it to `MemoryService`.

## Scope

In: augment the finalize turn to yield both `summary.md` and the structured delta; wire `_engine_finalize` to pass the delta to `MemoryService.apply_delta()` when enabled. Out: the delta schema (02.1), `apply_delta` internals (02.4), the failure seam (02.3).

## Approach

- `supervisor.finalize()` today (`src/wastech_orchestrator/core/supervisor.py`, ~line 190) returns `Path | None` from a **free-text** `_run_result()` turn. Extend it to request the `candidate_memory_delta` via **structured output on the same turn** — not a second model call. AC-W1 asserts zero extra calls, so the delta must ride the turn that already happens.
- Best-effort: an absent / malformed / schema-invalid delta → skip + log, finalize/publish is **never** blocked (Q9). Parse via 02.1's tolerant parser.
- Reuse the durable `__supervisor__` lineage (`_SUPERVISOR_LINEAGE_NODE_ID`, `supervisor.py` ~line 52) — no new session.
- The publish-node hook `_engine_finalize` (`src/wastech_orchestrator/core/orchestrator.py`, calls `supervisor.finalize()` ~line 1809) receives the delta and calls `MemoryService.apply_delta()` **only when `memory.enabled`**. Disabled → no delta requested at all, finalize unchanged (Q10).

## Files

- `src/wastech_orchestrator/core/supervisor.py` (`finalize`), `src/wastech_orchestrator/core/orchestrator.py` (`_engine_finalize`).

## Tests

- LLM-call count is identical with memory on vs off (AC-W1) — assert the same number of provider turns.
- A malformed delta → publish still succeeds (best-effort).
- Disabled (Q10) → no delta requested; finalize behavior byte-for-byte today's (AC-S4 alignment).

## Done when

`finalize()` emits a delta on its existing turn with zero added LLM calls, best-effort; AC-W1 holds.
