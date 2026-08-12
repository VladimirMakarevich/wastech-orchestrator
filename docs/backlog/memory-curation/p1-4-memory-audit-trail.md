# P1.4 — the audit trail cannot explain a removal, and snapshots grow without information

Priority: **P1** Status: **proposed** Date: 2026-07-26 Source: [memory audit](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/analysis/2026-07-24-wastimeapp-memory-audit.md) P1 (audit/snapshots) · [curation analysis](2026-07-25-auto-dream-memory-curation-analysis.md) §2.3

## Problem

The audit log is tied to a **file rewrite**, not to a logical transition. A snapshot is taken before it is known whether the pass will mutate anything. An event carries no tier or file, a whole-file replace can name the rows that stayed instead of the row that left, and reads are never audited. The hash chain is intact and verifiable — but an operator cannot answer "which record disappeared and why" from the trail alone, and an automated forensic or migration procedure cannot rely on it.

This becomes blocking the moment a model is allowed to propose rewrites: a curator's edit and a data corruption would look the same in the log.

## Evidence

`CleanupJob.run_once` (`memory/cleanup.py:67`) snapshots first and computes changes afterwards:

```python
label = f"cleanup-{audit.timestamp}"
snapshot_dir = None if dry_run else self._service.snapshot(tier_files, label=label)
```

Measured on the WastimeApp store:

| Fact | Number |
| --- | --- |
| Snapshot directories | 398 (2 407 files, ~19,6 MB payload) |
| Unique canonical tier states behind them | 25 |
| Snapshots repeating the previous state | 373 (93,7%) |
| Snapshots created by a pass with **no** memory mutation | 393 (98,7%) |
| Cleanup timestamps that actually mutated memory | 5 |
| Audit events | 113 (append 64, merge 18, prune 3, quarantine 28) |
| Events with an empty task id | 20 (all cleanup) |
| Events with an empty rationale | 23 |
| Events with `source_artifacts` populated | 0 |

Two concrete misattributions: `audit_000024` names `ltm_9ecf2cf19618` as quarantined while the state transition shows it among the kept rows — the actually removed row is visible only by diffing the snapshot against the file; `audit_000063` does the same for entities. `AuditAction.PROMOTE` (`memory/audit.py:48`) exists and is never used, so a promotion is indistinguishable from an append.

Growth is not correlated with information: 126 snapshots on a single idle day (Jul 23) with zero audit mutations.

## Change

1. **Plan first, then snapshot.** Compute an immutable cleanup plan/diff; if the plan is empty, do nothing at all — no snapshot, no event, no file rewrite. Take exactly one snapshot before a non-empty plan is applied.
2. **Make the event describe the logical transition:** `tier`, the relative file, added / updated / removed / moved ids, a reason **per id**, before/after record hashes, and the snapshot id.
3. **Use `AuditAction.PROMOTE`** so a promotion is not an append, and add an actor for the curator when P2.6 lands (`AuditActor` at `memory/audit.py:40` currently knows only `finalizer` / `cleanup` / `operator`).
4. **Snapshot retention + dedup:** content-address identical states, keep N / daily, so storage tracks distinct states rather than pass count.
5. **A retrieval-decision trace** (separate from the mutation log): the query signals, candidates, scores, selected and dropped ids, and the token cost of the rendered packet. Without it, "did curation improve retrieval?" is unanswerable, and P2.6's precision cannot be computed offline.

## Acceptance

- A no-op cleanup pass creates 0 snapshots and 0 audit events.
- One stale move produces one event naming the exact source tier, destination, id and reason.
- Replaying snapshot + events reproduces the subsequent state and passes a hash comparison.
- Every packet can be explained after the fact: which records were selected, which were dropped, and why.
- `no-op snapshots`: 393 of 398 → 0. `cleanup events with the exact removed id`: incomplete → 100%.

## Test

A no-op pass asserts zero writes (snapshot dir count unchanged, audit length unchanged). A single-move pass asserts one event with exact ids and reasons. A replay test applies recorded events on top of the snapshot and compares the canonical state hash. Keep the existing audit-chain, atomic-IO and snapshot-restore tests as regression guards — they are the good primitives here and must survive the event-model change.

## Scope / risk

Touches the seam every memory mutation passes through, so the risk is losing the currently-working guarantees: the chain must stay verifiable and `restore` must stay green. Retention deletes history — make the policy explicit, configurable, and never able to remove the snapshot a still-referenced event points at. The retrieval trace must not become a new secret channel: it records ids and scores, and goes through the same redaction chokepoint as everything else.

## Depends on

[P0.1](p0-1-claim-identity-and-merge.md) — ids are the thing the trail names, so pinning the event model before identity settles would encode the collision into the audit format as well.
