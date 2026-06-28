# 05.5 — Offline replay harness

[phase](index.md) · [blueprint §10](../../research/memory-architecture-blueprint.md) · [acceptance: AC-O1..O4](../../acceptance-criteria.md)

**Goal:** measure whether memory actually helps — a memory-off vs memory-on baseline on historical tasks — and set the outcome targets. This is the gate for every future phase (V2/V3/V4).

## Scope

In: an offline harness that replays historical tasks on **fixed** models/prompts in three modes (memory-off, memory-on, memory-on-without-entity-cards); records the metric stack (blueprint §10.1); sets the AC-O\* targets from the baseline. Out: live production runs.

## Approach

- Deterministic replay over recorded tasks, with fixed models/prompts so memory's effect is isolated.
- Capture the metric stack: tokens / wall-clock for repeated-repo tasks (AC-O1); first-pass review/test success on repeated hotspots (AC-O2); stale-contradiction rate, secret-leak rate, external-only long-term promotions (AC-O3).
- The harness is the **gate** for the future phases: no vector/graph/SQLite infra ships without a measured recall/quality lift (AC-O4).
- Use the baseline to tune the provisional Q1 (cleanup budget) and Q5 (packet caps) integers — the approach is locked, only the integers move.

## Files

- New eval-harness module + recorded-task fixtures (under the test/eval tree).

## Outputs / Tests

- A recorded baseline (off vs on vs on-without-entity) with the full metric stack.
- AC-O1/O2/O3 numeric targets set from the baseline; the AC-O4 gate established.

## Done when

The baseline is recorded, the AC-O\* targets are set, and the AC-O4 future-phase gate is in place.
