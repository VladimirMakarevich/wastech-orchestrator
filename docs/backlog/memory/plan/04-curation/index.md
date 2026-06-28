# Phase 04 — Curation

Status: **outline** — [plan](../index.md) · [design §5,§7](../../design.md) · [acceptance: AC-C1..C4](../../acceptance-criteria.md)

**Goal:** the operator can inspect and repair memory, and a bounded background job keeps it from rotting — without ever touching an active task. Depends on phases 01–03.

**Exit criteria:** AC-C1..C4 pass; cleanup is bounded, audited, and never delays the next task.

## Tasks (split into files at scope-lock)

- **`worc memory` CLI** — first nested subparser (own `add_subparsers`), modeled on `cmd_upgrade_config`: `show`, `validate`, `compact`, `restore`, each with a `--dry-run` plan before execute (verbs decided — Q4; no `defrag` alias).
- **`CleanupJob.run_once`** — TTL expiry, path/symbol existence checks (via `DerivedIndex`), duplicate-merge candidates, stale marking, quarantine of uncertain cases; snapshot-before-batch; bounded scan/edit/wall-clock budget; promotions-per-pass default 0.
- **Idle hook** — call `CleanupJob.run_once()` in the `watch_loop` idle gap (after `watch_once`, before the poll sleep); short, interruptible, no-network, no active-task writes.
- **`DerivedIndex` (minimal)** — just enough to answer "does this path/symbol still exist?" for stale detection (see Q2); full index is a separate concern.

## Notes

Cleanup may demote / expire / quarantine / merge — it must **never** create a new long-term lesson or edit code/docs/skills. Cadence + budget are open ([questions.md](../../questions.md) Q1).
