# 04.2 — `CleanupJob.run_once`

[phase](index.md) · [design §5,§7](../../design.md) · [acceptance: AC-C3/C4, AC-SF4](../../acceptance-criteria.md)

**Goal:** the bounded, model-free maintenance pass that keeps memory from rotting — and never touches an active task.

## Scope

In: `run_once()` — TTL expiry, path/symbol existence checks (via `DerivedIndex`), duplicate-merge candidates, stale marking, quarantine of uncertain cases; snapshot-before-batch; bounded scan/edit/wall-clock budget; `promotions_per_pass` default 0. Out: the idle scheduling (04.3), the index (04.4).

## Approach

- Implements the design §5 cleanup decisions and §7 bounded autonomy. **Snapshot first** (01.4), then operate within the Q1 budget (`MemoryConfig`, tunable): `min_interval 300s`, `max_scanned 200`/pass, `max_edits 50`/pass, `max_wall_clock 5s`, `promotions_per_pass 0`.
- **Staleness** via `DerivedIndex` (04.4 / Q2): a missing path/symbol → attempt a rename-remap (same basename / content signature) → else mark stale → **quarantine, never silent delete**. A lesson auto-drops only on existence failure or a 2× explicit contradiction.
- **Never** creates a new long-term lesson and **never** edits code/docs/skills (AC-C3); fail-closed; **no network** (NFR6).
- Every action is audited via the 01.4 primitive.

## Files

- New `src/wastech_orchestrator/.../memory/cleanup.py` (`CleanupJob`).

## Tests

- Respects every budget cap (`max_scanned` / `max_edits` / `max_wall_clock`); `promotions_per_pass` is 0.
- Never creates a long-term lesson and never edits code/docs/skills (AC-C3).
- A stale entity (removed path/symbol) is detected and quarantined/marked (AC-C4).
- A snapshot precedes the batch (AC-SF4 groundwork).

## Done when

`run_once` is bounded, snapshotted, and audited; AC-C3/C4 hold.
