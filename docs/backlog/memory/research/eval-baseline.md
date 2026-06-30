# Memory eval baseline (offline replay harness)

Status: **harness shipped; baseline is SYNTHETIC pending real runs** Date: 2026-07-01 — [task hub](../index.md) · [plan 05.5](../plan/05-safety-and-evaluation/05-offline-replay-harness.md)

This is the recorded output of the offline replay harness (plan 05.5 / blueprint §10) — the gate for the V2/V3/V4 roadmap (AC-O4). The harness is `tests/eval/harness.py` (deterministic, model-free metric aggregation + the AC-O verdicts); it is fed per-task metrics recorded from replaying historical tasks on **fixed models/prompts** in three modes (memory-off / memory-on / memory-on-without-entity-cards). Running the models to produce those records is out of scope of the harness itself (blueprint §10.2).

## Important: this baseline is synthetic

The orchestrator is greenfield — there is **no corpus of real production tasks** to replay yet — so the numbers below come from the synthetic fixtures in `tests/eval/test_replay_baseline.py`, which exist to exercise the harness, the AC-O verdicts, and the report renderer. **They are an illustration of the report format, not a real measurement.** The real baseline replaces them once real runs accrue (record `TaskMetrics` per replayed task per mode, then `build_baseline(...)` → `render_baseline_markdown(...)`). What is locked here is the **approach and the thresholds**, not the integers.

## Illustrative report (from the synthetic fixtures)

| Mode | Tasks | Mean tokens | Mean wall-clock (s) | First-pass | Secret leaks |
| --- | --: | --: | --: | --: | --: |
| memory-off | 3 | 10667 | 260.0 | 67% | 0 |
| memory-on | 3 | 9267 | 228.3 | 100% | 0 |
| memory-on-without-entity | 3 | 9267 | 228.3 | 100% | 0 |

memory-off vs memory-on: token reduction (repeated-repo) 17%, wall-clock reduction 16%, first-pass improvement on hotspots +50pp → AC-O1 PASS, AC-O2 PASS, AC-O3 PASS, **AC-O4 measured-lift gate: YES**.

## The thresholds (locked; integers tuned against the real baseline)

- **AC-O1** — ≥ 10% reduction in tokens **or** wall-clock on repeated-repo tasks (`AC_O1_MIN_REDUCTION = 0.10`).
- **AC-O2** — ≥ 10 percentage-point improvement in first-pass review/test success on repeated hotspots (`AC_O2_MIN_IMPROVEMENT = 0.10`).
- **AC-O3** — stale-contradiction rate < 5% (`AC_O3_MAX_STALE_CONTRADICTION = 0.05`), secret-leak count 0, external-only long-term promotions 0 (a property of the memory-on run itself).
- **AC-O4** — the roadmap gate: **no vector/graph/SQLite infra (V2/V3/V4) ships without a measured recall/quality lift.** `Comparison.measured_lift` encodes it — a future phase opens only when the comparison clears AC-O1 or AC-O2. With memory-on identical to memory-off, the gate stays closed (regression-tested).

## How to record the real baseline

1. Replay a fixed set of historical tasks (same models/prompts) in each mode, recording one `TaskMetrics` per task (tokens, wall-clock, first-pass pass, the `repeated_repo`/`hotspot` subset flags, and the safety counters).
2. `build_baseline({MODE_OFF: [...], MODE_ON: [...], MODE_ON_NO_ENTITY: [...]})` → `render_baseline_markdown(...)`; replace the illustrative table above with the result.
3. Re-tune the provisional Q1 (cleanup budget) and Q5 (packet caps) integers against the measured numbers — the approach is fixed, only the integers move.
