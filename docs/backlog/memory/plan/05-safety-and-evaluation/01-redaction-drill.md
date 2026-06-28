# 05.1 — Redaction drill

[phase](index.md) · [design §7](../../design.md) · [acceptance: AC-SF1](../../acceptance-criteria.md)

**Goal:** prove that secret-like strings planted in task artifacts never reach `.worc/memory/` — leak count **0**.

## Scope

In: an adversarial end-to-end test that plants secrets in task artifacts and asserts none appear in any memory file. Out: the redaction code itself (01.3 reuses `redact_text` / `redact_mapping`).

## Approach

- Fixture: task artifacts seeded with planted secret-like strings → run the write path (phase 02) → scan **every** `.worc/memory/` file (all tiers + `audit/` + `quarantine/`) **and** any rendered packet → assert **0** leaks (C1/NFR5).
- Table-driven over the existing redaction patterns; V1 ships no new scanner — this confirms the reused redaction closes the re-surfacing path before write.

## Files

- `tests/.../test_memory_redaction_drill.py`.

## Tests

- Planted secrets across tiers, audit rows, and packets → 0 leaks (AC-SF1).

## Done when

AC-SF1 holds; leak count is 0.
