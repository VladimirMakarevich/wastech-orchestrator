# 02.6 — Audit marker (evaluations row)

[phase](index.md) · [design §7](../../design.md) · [acceptance: AC-SF3](../../acceptance-criteria.md)

**Goal:** mirror every memory mutation into the orchestrator's existing decision trail as a **best-effort** `evaluations` marker row (Q6), complementing the primary hash-chained `audit/log.jsonl` (01.4).

## Scope

In: write a lightweight marker into the existing `evaluations` table for each memory mutation; lock the minimal row shape (Q6 defers the shape to phase-02 detail). Out: the primary audit row (01.4), the `MemoryService` decision logic (02.4).

## Approach

- The **primary** audit row (id, timestamp, actor, source artifact ids, affected ids, action, pre/post hashes, rationale, hash-chained) is written by the 01.4 audit primitive. **This task** adds the **secondary** marker.
- Reuse the existing append-only `evaluations` table (`src/wastech_orchestrator/state_store.py` ~line 264; `INSERT INTO evaluations` ~line 1158; `EvaluationRow` / `get_evaluations`). It already exists (added at DB schema v8) — **no `state.db` schema bump** is needed, which keeps C2 intact (memory data itself never lives in `state.db`; this is only a decision-trail marker that already has a home).
- The marker is **best-effort**: a failure to write it must never fail the memory write or the task (FR8 "best-effort", NFR9).
- Decide the minimal row content (task_id, actor, action, affected memory ids, post-hash) by mapping onto existing `EvaluationRow` columns — no new column.

## Files

- `src/wastech_orchestrator/.../memory/service.py` (call site); reuse the existing `state_store` evaluations-insert helper.

## Tests

- A memory write produces **both** an `audit/log.jsonl` row and an `evaluations` marker.
- A forced `evaluations`-write failure does **not** break the memory write (best-effort).
- No new `state.db` table or column is introduced (C2).

## Done when

Dual audit (primary jsonl + best-effort `evaluations` marker) works; AC-SF3 and FR8 hold; C2 respected.
