# Definition of done

Status: **draft — to refine** Date: 2026-06-28 — [task hub](index.md)

V1 is done when **all** of the following hold. This is the merge gate; per-feature behavior is in [acceptance-criteria.md](acceptance-criteria.md).

## Functionality

- [ ] `.worc/memory/` canonical store with the three tiers, written at finalization via the supervisor's candidate delta (zero extra LLM calls).
- [ ] `PacketBuilder` produces deterministic, capped per-stage packets read via `memory_path`.
- [ ] `MemoryService` performs redact → validate → trust → merge/dedup → promote/quarantine → audit.
- [ ] `worc memory show | validate | compact | restore` with `--dry-run`.
- [ ] Bounded `CleanupJob` in the `watch_loop` idle gap.
- [ ] Global enable/disable in config; disabled = today's behavior exactly.

## Safety (no exceptions)

- [ ] Redaction + secret scan before every write; planted-secret test passes with **0** leaks.
- [ ] Trust levels assigned and enforced; poisoning drill shows no auto-promotion of low-trust memory.
- [ ] Append-only hash-chained audit log; pre-cleanup snapshots; `restore` verified by a rollback test.
- [ ] Bounded autonomy: no active-task writes, budget respected, no network, no long-term creation, no doc/code/skill edits.

## Quality gates

- [ ] `ruff check .` clean, `mypy src` clean, `pytest` green (the `/run-checks` gate).
- [ ] New deterministic services are unit-tested **without a model** (no fake-CLI needed for them); provider/pipeline paths covered where touched.
- [ ] Cross-platform: path handling via `pathlib`/`as_posix()`; suite green on Windows and POSIX (or CI matrix updated).

## Config & docs

- [ ] `MemoryConfig` added, wired, parsed, defaulted; `CONFIG_SCHEMA_VERSION` bumped; old configs load without a fatal error.
- [ ] `packaged/config.example.yaml` documents the memory block.
- [ ] Docs synced (`/sync-docs`): functional map / configuration / CLI reference updated; this task hub's status flipped to implemented; follow-ups recorded in [../follow_ups.md](../follow_ups.md). The Stop docs-sync gate passes.
- [ ] Install seeds `.worc/memory/` gitignore and any packaged role-prompt references to `{memory_path}`.

## Evaluation

- [ ] Offline replay harness exists and a memory-off vs memory-on baseline is recorded (even if targets in [acceptance-criteria.md](acceptance-criteria.md) are tuned afterward).
- [ ] No vector/graph/SQLite infra shipped in V1 (those are V2–V4, gated).
