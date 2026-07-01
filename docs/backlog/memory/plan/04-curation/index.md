# Phase 04 — Curation

Status: **done** (2026-07-01, on `feat/memory-subsystem`) — [plan](../index.md) · [design §5,§7](../../design.md) · [acceptance: AC-C1..C4](../../acceptance-criteria.md)

**Goal:** the operator can inspect and repair memory, and a bounded background job keeps it from rotting — without ever touching an active task. Depends on phases 01–03.

**Exit criteria:** AC-C1..C4 pass; cleanup is bounded, audited, and never delays the next task.

## Tasks

| # | Task | Touches |
| --- | --- | --- |
| 1 | [`worc memory` CLI](01-worc-memory-cli.md) | `cli.py` nested subparser — `show`/`validate`/`compact`/`restore` + `--dry-run` |
| 2 | [`CleanupJob.run_once`](02-cleanup-job.md) | new `memory/cleanup.py` — bounded, snapshotted, audited pass |
| 3 | [Idle hook](03-idle-hook.md) | `cli.py` `watch_loop` idle gap, work-gated + rate-limited |
| 4 | [`DerivedIndex` (minimal)](04-derived-index.md) | new `memory/derived.py` — path/symbol existence for staleness |

## Outcome (2026-07-01)

Built on `feat/memory-subsystem`: `memory/derived.py` (`DerivedIndex` — path/symbol existence via an injectable `git ls-files` provider routed through the safe `run_process` runner + filesystem stat; `find_by_basename` for rename-remap; a rebuildable `derived/repo_map.json` cache); `memory/cleanup.py` (`CleanupJob.run_once` — snapshot-first, budget-bounded `_Budget`, episode TTL expiry + entity staleness (basename remap → else quarantine) + duplicate long-term merge; `full=True` for the foreground pass, `dry_run=True` for the plan; never promotes, never edits code — AC-C3); four redacted+audited tier-rewrite methods on `MemoryService` (`replace_long_term`/`replace_entities`/`replace_episodes`/`replace_quarantine`); the `worc memory show|validate|compact|restore` nested subparser with `--dry-run` and an active-task gate on the mutating verbs (AC-C1); and the rate-limited, idle-gated `_build_cleanup_hook` threaded into `watch_loop`'s idle gap (AC-C2, no `os.kill`/signal). AC-C1..C4 + AC-SF4 groundwork hold; suite + ruff + mypy green.

## Notes

- Cleanup may demote / expire / quarantine / merge — it must **never** create a new long-term lesson or edit code/docs/skills (AC-C3). Cadence + budget are the provisional Q1 integers (tunable).
- **Sequencing wrinkle:** `DerivedIndex` (04.4) is consumed by both `CleanupJob` (04.2) staleness and `apply_delta` (02.4) validation — build it early in the phase; phase 02 may stub against it until it lands.
- **Seam reality:** model the `memory` subparser on the existing nested `logs` subparser and `cmd_upgrade_config` in `cli.py`; the idle gap is between `watch_once` and the poll sleep in `watch_loop`.
