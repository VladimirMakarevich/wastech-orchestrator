# Implementation plan

Status: **detailed (all phases broken into task files); numeric defaults provisional** Date: 2026-06-29 — [task hub](../index.md)

Phased build order for V1, derived from the [design](../design.md) and the [blueprint roadmap](../research/memory-architecture-blueprint.md) (§9). Requirements and design are **locked** ([ADR-0001](../adr-0001-memory-subsystem-v1.md) accepted); **all five phases are now detailed into committed task files** (Goal / Scope / Approach / Files / Tests / Done-when each), grounded in the verified code seams (design §9). Only the provisional numeric defaults (Q1 cleanup budget, Q5 packet caps) remain to tune against the eval baseline. Each phase exits only when its slice of [acceptance-criteria.md](../acceptance-criteria.md) passes and `/run-checks` is green.

## Principle

Sequence by **shared seam** to minimize rework: storage + config first, then the write path, then the read path, then curation, then safety hardening + evaluation. The deterministic services (`MemoryService` / `PacketBuilder` / `CleanupJob` / `DerivedIndex`) are built and unit-tested **without a model**; only the supervisor candidate-delta touches an LLM, and it reuses the existing finalize turn.

## V1 phases

| Phase | Goal | Depends on |
| --- | --- | --- |
| [01 — Foundations](01-foundations/index.md) | `.worc/memory/` store, `MemoryConfig` + schema bump, gitignore, `MemoryService` skeleton, audit/snapshots | — |
| [02 — Write path](02-write-path/index.md) | Supervisor `candidate_memory_delta`; `MemoryService.apply_delta` (validate→trust→merge→promote/quarantine→audit); tier persistence | 01 |
| [03 — Read path](03-read-path/index.md) ✅ **done** | `PacketBuilder` deterministic per-stage packets; `memory_path` prompt var; role-prompt references; caps | 01 (02 for content) |
| [04 — Curation](04-curation/index.md) ✅ **done** | `worc memory show/validate/compact/restore`; bounded `CleanupJob` in the watch-loop idle gap | 01–03 |
| [05 — Safety & evaluation](05-safety-and-evaluation/index.md) | Redaction/poisoning/staleness/rollback drills; offline replay harness + baseline; docs sync | 01–04 |

## First-slice option (see [questions.md](../questions.md) Q8)

The leanest end-to-end loop is the **long-term tier only** through phases 01→02→03 (write a validated lesson at finalize, read it back in a packet), then add short-term + entity on the same seams, then 04–05. Leaning toward this; to be confirmed at phase 01/02 sign-off.

## Future phases (out of V1 — gated by [evaluation](../acceptance-criteria.md) §Outcome, not scheduled)

- **V2 — SQLite + FTS** when file-based dedup/merge/validation gets messy or slow (`.worc/memory/memory.sqlite`).
- **V3 — Embeddings** as a secondary recall layer, only after replay shows metadata-first retrieval misses relevant facts.
- **V4 — Entity graph** when multi-hop relational reasoning becomes routine.

Detail and triggers: [out-of-scope.md](../out-of-scope.md) and blueprint §9.
