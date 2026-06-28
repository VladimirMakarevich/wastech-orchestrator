# 05.4 — Rollback drill

[phase](index.md) · [design §7](../../design.md) · [acceptance: AC-SF4](../../acceptance-criteria.md)

**Goal:** prove that snapshot → bad cleanup → `restore` returns memory to its pre-cleanup state.

## Scope

In: a test that snapshots, applies a destructive cleanup batch, then restores and asserts byte-identical pre-state. Out: the snapshot/restore primitives (01.4), the CLI verb (04.1).

## Approach

- Populate memory → take the automatic pre-batch snapshot → run a cleanup that demotes/merges/quarantines → `worc memory restore` (or the 01.4 restore primitive) → assert **byte-identical** pre-cleanup state (AC-SF4/FR8).
- Rely on UTF-8 + explicit `\n` (NFR8) so content hashes — and thus the byte-identity check — are stable cross-platform.

## Files

- `tests/.../test_memory_rollback_drill.py`.

## Tests

- `restore` returns byte-identical pre-state (AC-SF4).
- The rollback itself is recorded as an audit row.

## Done when

AC-SF4 holds under the drill.
