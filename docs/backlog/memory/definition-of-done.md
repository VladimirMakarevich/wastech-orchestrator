# Definition of done

Status: **satisfied — V1 implemented** Date: 2026-07-01 — [task hub](index.md)

V1 is done when **all** of the following hold. This is the merge gate; per-feature behavior is in [acceptance-criteria.md](acceptance-criteria.md).

## Functionality

- [x] `.worc/memory/` canonical store with the three tiers, written at finalization via the supervisor's candidate delta (zero extra LLM calls).
- [x] `PacketBuilder` produces deterministic, capped per-stage packets read via `memory_path`.
- [x] `MemoryService` performs redact → validate → trust → merge/dedup → promote/quarantine → audit.
- [x] `worc memory show | validate | compact | restore` with `--dry-run`.
- [x] Bounded `CleanupJob` in the `watch_loop` idle gap.
- [x] Global enable/disable in config; disabled = today's behavior exactly.

## Safety (no exceptions)

- [x] Redaction + secret scan before every write; planted-secret test passes with **0** leaks (redaction drill 05.1).
- [x] Trust levels assigned and enforced; poisoning drill shows no auto-promotion of low-trust memory (05.2).
- [x] Append-only hash-chained audit log; pre-cleanup snapshots; `restore` verified by a rollback test (05.4).
- [x] Bounded autonomy: no active-task writes, budget respected, no network, no long-term creation, no doc/code/skill edits.

## Quality gates

- [x] `ruff check .` clean, `mypy src` clean, `pytest` green (the `/run-checks` gate).
- [x] New deterministic services are unit-tested **without a model** (no fake-CLI needed for them); provider/pipeline paths covered where touched.
- [x] Cross-platform: path handling via `pathlib`/`as_posix()` (suite green on POSIX; the Windows CI matrix remains the standing cross-platform follow-up).

## Config & docs

- [x] `MemoryConfig` added, wired, parsed, defaulted; `CONFIG_SCHEMA_VERSION` bumped (→ 24); old configs load without a fatal error.
- [x] `packaged/config.example.yaml` documents the memory block.
- [x] Docs synced: configuration + CLI reference (operations) updated; this task hub flipped to implemented; follow-ups recorded in [../follow_ups.md](../follow_ups.md). A dedicated **functional-map** memory block rides the already-deferred functional-map re-sync row. The Stop docs-sync gate passes.
- [x] Install seeds `.worc/memory/` (the whole `.worc/` home is gitignored) and the packaged role-prompt `{memory_path}` references.

## Evaluation

- [x] Offline replay harness exists (`tests/eval/harness.py`) and a memory-off vs memory-on baseline is recorded ([eval-baseline.md](research/eval-baseline.md)) — **synthetic** for now (greenfield: no real task corpus), the approach + thresholds locked, integers tuned against a real baseline afterward.
- [x] No vector/graph/SQLite infra shipped in V1 (those are V2–V4, gated by the AC-O4 measured-lift gate).
