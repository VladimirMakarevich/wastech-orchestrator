# Phase 02 — Write path

Status: **planned** — [plan](../index.md) · [design §2,§5](../../design.md) · [acceptance: AC-W1..W4](../../acceptance-criteria.md)

**Goal:** memory gets written once per task at finalization, deterministically and safely, with zero new LLM calls. Depends on phase 01.

**Exit criteria:** AC-W1..W4 and AC-SF2/SF5 (trust enforcement, no low-trust auto-promotion) pass.

## Tasks

| # | Task | Touches |
| --- | --- | --- |
| 1 | [Candidate-delta contract](01-candidate-delta-contract.md) | new `memory/delta.py` — Q9 schema + tolerant parser |
| 2 | [Supervisor emit (success seam)](02-supervisor-emit-success-seam.md) | `core/supervisor.py` `finalize`, `core/orchestrator.py` `_engine_finalize` |
| 3 | [Failure / manual write seam](03-failure-manual-write-seam.md) | `core/orchestrator.py` `_fail` / `_go_terminal`, deterministic record |
| 4 | [`MemoryService.apply_delta`](04-apply-delta.md) | `memory/service.py` — validate→trust→merge→promote/quarantine→audit |
| 5 | [Tier persistence](05-tier-persistence.md) | episodic append, entity cards, gated long-term writes |
| 6 | [Audit marker (evaluations row)](06-audit-marker.md) | `memory/service.py` + existing `state_store` evaluations table |

## Notes

- All of `apply_delta` is deterministic and unit-tested without a model. The only LLM touch is the supervisor delta, which reuses the existing finalize turn (assert zero extra calls — AC-W1).
- **Seam reality:** `supervisor.finalize()` is today a **free-text** turn returning `Path | None` — task 02.2 converts it to also emit structured output on the _same_ turn (the AC-W1 "zero extra calls" constraint), not a second call.
- **Two write seams, one funnel:** success (publish, 02.2) and failure/manual (02.3) both feed `apply_delta` (02.4). External-context tasks → quarantine-unless-validated (AC-W4).
- Task 04's `DerivedIndex`-backed validation may stub path/symbol existence until [04.4](../04-curation/04-derived-index.md) lands, then tighten.
