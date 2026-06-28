# 05.3 — Staleness drill

[phase](index.md) · [design §5](../../design.md) · [acceptance: AC-C4](../../acceptance-criteria.md)

**Goal:** prove that outdated commands and renamed/removed modules are detected and quarantined/marked.

## Scope

In: a test that removes or renames a referenced path/symbol, runs `validate` / `CleanupJob`, and asserts stale detection → quarantine (Q2). Out: `DerivedIndex` (04.4), `CleanupJob` (04.2).

## Approach

- Seed an entity/lesson referencing a path/symbol → remove or rename the target → run `CleanupJob.run_once()` (or `worc memory validate`) → assert: a **rename-remap** attempt first, else **mark stale → quarantine**, **never** a silent delete (Q2).
- Cover the auto-drop boundary: a lesson auto-drops only on existence failure or a 2× explicit contradiction — never on judgment.

## Files

- `tests/.../test_memory_staleness_drill.py`.

## Tests

- A removed target → quarantined (AC-C4).
- A renamed target → remapped, or quarantined if no confident remap.
- Never a silent delete.

## Done when

AC-C4 holds under the drill.
