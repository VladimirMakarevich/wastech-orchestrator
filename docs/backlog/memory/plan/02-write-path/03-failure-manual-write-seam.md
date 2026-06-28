# 02.3 — Failure / manual deterministic write seam

[phase](index.md) · [design §2,§9](../../design.md) · [acceptance: AC-W3](../../acceptance-criteria.md)

**Goal:** on terminal failure / manual close — where there is **no** supervisor turn — build a short-term/failure record **deterministically (no LLM)**, never promoted to long-term.

## Scope

In: hook `_fail` and `_go_terminal` to build a failure record from the existing failure artifacts and hand it to `MemoryService`. Out: the success delta (02.2), promotion rules (02.4), tier persistence (02.5).

## Approach

- `_fail` (`src/wastech_orchestrator/core/orchestrator.py` ~line 2184) and `_go_terminal` (~line 2254) have **no** supervisor turn (FR3, design §2) — so the record is built **without a model** from artifacts already on disk (node logs, last failing node, check/review output).
- The record is **short-term / failure tier only**; `apply_delta` (02.4) must refuse long-term promotion for this source (AC-W3) — except by explicit operator signal.
- Best-effort and guarded by `memory.enabled`; the write **never** blocks or raises into the terminal transition.

## Files

- `src/wastech_orchestrator/core/orchestrator.py` (`_fail`, `_go_terminal`); `src/wastech_orchestrator/.../memory/service.py` (deterministic failure-record builder).

## Tests

- A failed / manually-closed task writes short-term/failure memory but **no** long-term entry (AC-W3).
- Disabled (Q10) → no write.
- A forced failure inside the memory write does not break the terminal path (best-effort).

## Done when

Failure / manual closes produce a deterministic failure record, never long-term; AC-W3 holds.
