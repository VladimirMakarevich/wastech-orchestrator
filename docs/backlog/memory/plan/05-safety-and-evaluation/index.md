# Phase 05 — Safety hardening & evaluation

Status: **done** (2026-07-01, on `feat/memory-subsystem`) — [plan](../index.md) · [design §7](../../design.md) · [acceptance: AC-SF*, AC-O*](../../acceptance-criteria.md)

**Goal:** prove the safety properties under adversarial conditions and measure whether memory actually helps, before declaring V1 done. Spans/after phases 01–04.

**Exit criteria:** the safety drills pass; an offline replay baseline (memory-off vs memory-on) is recorded; [definition-of-done.md](../../definition-of-done.md) is satisfied.

## Tasks

| # | Task | Touches |
| --- | --- | --- |
| 1 | [Redaction drill](01-redaction-drill.md) | planted secrets never reach `.worc/memory/`; leak count 0 (AC-SF1) |
| 2 | [Poisoning drill](02-poisoning-drill.md) | low-trust never auto-promotes or outranks trusted (AC-SF2/SF5) |
| 3 | [Staleness drill](03-staleness-drill.md) | removed/renamed targets detected → quarantine (AC-C4) |
| 4 | [Rollback drill](04-rollback-drill.md) | snapshot → bad cleanup → `restore` returns pre-state (AC-SF4) |
| 5 | [Offline replay harness](05-offline-replay-harness.md) | memory-off vs on baseline; set AC-O\* targets; gate V2/V3/V4 |
| 6 | [Docs sync](06-docs-sync.md) | `/sync-docs`; flip task hub to implemented; record follow-ups |

## Outcome (2026-07-01)

Built on `feat/memory-subsystem`: four adversarial drills under `tests/memory/` — `test_memory_redaction_drill.py` (planted secrets → 0 leaks across every tier + audit + quarantine + a rendered packet, AC-SF1), `test_memory_poisoning_drill.py` (external-untrusted/agent-inferred → quarantined, never durable, never reach a packet, never out-rank trusted, never silently overwrite on contradiction — AC-SF2/SF5/NFR2), `test_memory_staleness_drill.py` (removed → quarantine, renamed → remap, ambiguous → quarantine, never silent delete, lesson never dropped on judgment — AC-C4), `test_memory_rollback_drill.py` (snapshot → bad cleanup → `restore` byte-identical pre-state + a rollback audit row, chain intact — AC-SF4). Plus the offline-replay harness `tests/eval/harness.py` (deterministic, model-free: `TaskMetrics` → `summarize_mode`/`compare_modes`/`build_baseline`/`render_baseline_markdown`; the AC-O1..O3 verdicts + the `measured_lift` AC-O4 gate) with `tests/eval/test_replay_baseline.py` and the recorded [eval baseline](../../research/eval-baseline.md) (synthetic, greenfield). The [definition-of-done](../../definition-of-done.md) holds; suite + ruff + mypy green.

## Notes

The eval harness is what gates the future phases (V2 SQLite, V3 embeddings, V4 graph): none of them ship without a measured recall/quality lift (AC-O4). The baseline numbers are synthetic until a real task corpus exists — the approach and thresholds are what is locked.
