# Phase 01 — Foundations

Status: **implemented** (2026-06-30, branch `feat/memory-subsystem`) — [plan](../index.md) · [design](../../design.md) · [acceptance-criteria](../../acceptance-criteria.md)

**Goal:** stand up the canonical store, config, and the deterministic `MemoryService` skeleton — everything the write/read paths build on — with safety primitives (audit, snapshots) in place from the first commit. No LLM involvement in this phase.

**Exit criteria:** AC-S1..S5 and AC-SF3/SF4 (audit + snapshot/restore primitives) pass; enable/disable honored; suite green and cross-platform.

## Tasks

| # | Task | Touches |
| --- | --- | --- |
| 1 | [Store layout & gitignore](01-store-layout-and-gitignore.md) | `.worc/memory/` scaffolding, install gitignore seed |
| 2 | [MemoryConfig & schema bump](02-memory-config-and-schema-bump.md) | `config/schema.py`, `config/loader.py`, example yaml |
| 3 | [MemoryService skeleton](03-memory-service-skeleton.md) | new `MemoryService` module, records, atomic+redacted writes |
| 4 | [Audit log & snapshots](04-audit-log-and-snapshots.md) | `audit/log.jsonl`, `snapshots/`, restore primitive |

## Notes

- Build `MemoryService` as pure, deterministic logic — unit-tested without a model (no fake-CLI fixtures needed here).
- Reuse the existing redaction (`redact_text`/`redact_mapping`) and atomic-write (`_atomic_json`) discipline; do not invent new ones.
- Cross-platform from the first line: `pathlib` + `as_posix()` for any stored path string.
