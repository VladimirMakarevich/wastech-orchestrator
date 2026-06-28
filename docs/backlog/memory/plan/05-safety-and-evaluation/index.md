# Phase 05 — Safety hardening & evaluation

Status: **outline** — [plan](../index.md) · [design §7](../../design.md) · [acceptance: AC-SF*, AC-O*](../../acceptance-criteria.md)

**Goal:** prove the safety properties under adversarial conditions and measure whether memory actually helps, before declaring V1 done. Spans/after phases 01–04.

**Exit criteria:** the safety drills pass; an offline replay baseline (memory-off vs memory-on) is recorded; [definition-of-done.md](../../definition-of-done.md) is satisfied.

## Tasks (split into files at scope-lock)

- **Redaction drill** — planted secrets in artifacts never reach `.worc/memory/`; leak count 0 (AC-SF1).
- **Poisoning drill** — `external-untrusted` / `agent-inferred` candidates never auto-promote or outrank trusted repo-backed memory (AC-SF2).
- **Staleness drill** — outdated commands + renamed modules are detected and quarantined/marked (AC-C4).
- **Rollback drill** — snapshot → bad cleanup → `restore` returns pre-state (AC-SF4).
- **Offline replay harness** — run historical tasks memory-off vs memory-on (and without entity cards) on fixed models/prompts; record the metric stack (blueprint §10.1); set the AC-O\* targets from the baseline.
- **Docs sync** — `/sync-docs`: functional map / configuration / CLI reference; flip the [task hub](../../index.md) status to implemented; record follow-ups in [../../../follow_ups.md](../../../follow_ups.md). The Stop docs-sync gate must pass.

## Notes

The eval harness is what gates the future phases (V2 SQLite, V3 embeddings, V4 graph): none of them ship without a measured recall/quality lift (AC-O4).
