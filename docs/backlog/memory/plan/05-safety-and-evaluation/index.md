# Phase 05 — Safety hardening & evaluation

Status: **planned** — [plan](../index.md) · [design §7](../../design.md) · [acceptance: AC-SF*, AC-O*](../../acceptance-criteria.md)

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

## Notes

The eval harness is what gates the future phases (V2 SQLite, V3 embeddings, V4 graph): none of them ship without a measured recall/quality lift (AC-O4).
