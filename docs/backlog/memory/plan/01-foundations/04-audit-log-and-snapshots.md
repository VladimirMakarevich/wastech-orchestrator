# 01.4 — Audit log & snapshots

[phase](index.md) · [design §7](../../design.md) · [acceptance: AC-SF3/SF4](../../acceptance-criteria.md)

**Goal:** make every mutation auditable and every batch reversible from day one — before any promotion or cleanup logic can do damage.

## Scope

In: append-only `audit/log.jsonl`, content hashing, snapshot-before-batch, and a `restore` primitive. Out: the CLI surface that exposes them (phase 04) and the cleanup that uses snapshots (phase 04).

## Approach

- `audit/log.jsonl`: append-only, hash-chained rows — id, timestamp, actor (`finalizer`/`cleanup`/`operator`), source artifact ids, affected memory ids, action (`append`/`promote`/`merge`/`quarantine`/`prune`/`rollback`), pre/post content hashes, rationale.
- `snapshots/<ts>/`: cheap copy of the affected tier files taken before a batch mutation.
- `restore`: a primitive (API now; CLI verb in phase 04) that returns memory to a chosen snapshot.
- Timestamps are passed in / injected (deterministic for tests), not read from a hidden clock in pure logic.

## Files

- New `src/wastech_orchestrator/.../memory/audit.py` (+ snapshot helpers).

## Tests

- Every mutation through `MemoryService` writes exactly one audit row with pre/post hashes (AC-SF3).
- Snapshot → mutate → `restore` returns byte-identical pre-state (AC-SF4).
- The audit log is append-only (no in-place rewrite).

## Done when

Audit rows + snapshots + restore work and are tested; nothing can mutate memory without an audit row.
