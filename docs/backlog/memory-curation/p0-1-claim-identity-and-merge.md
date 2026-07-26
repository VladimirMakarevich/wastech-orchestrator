# P0.1 — a scope-only claim id merges distinct claims and silently rewrites long-term memory

Priority: **P0** Status: **proposed** Date: 2026-07-26 Source: [memory audit](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/analysis/2026-07-24-wastimeapp-memory-audit.md) P0-1 · [curation analysis](2026-07-25-auto-dream-memory-curation-analysis.md) §2.1

## Problem

A long-term lesson's identity is `kind` + its normalized `scope.paths`. The `subject` — the thing that says _what the claim is about_ — is dropped from the key whenever the lesson names a path. Every later claim of the same kind about the same file is therefore treated as the same memory, and the merge that follows replaces only part of the record. The result is a record whose fields belong to different claims, and whose provenance is internally contradictory.

This is systematic data distortion, not a cosmetic duplicate: the original claims cease to exist as separate records and survive only in snapshots.

## Evidence

[`memory/service.py:647`](../../../src/wastech_orchestrator/memory/service.py):

```python
def derive_long_term_id(kind: LongTermKind, subject: str, paths: Sequence[str]) -> str:
    basis = _scope_key(paths) or normalize_subject(subject)
    digest = content_hash(f"{kind.value}:{basis}".encode())[:12]
    return f"ltm_{digest}"
```

`_merge_long_term` (`service.py:698`) is called with `(existing, statement, evidence, now, task_id)` — the signature cannot carry a new `subject`, `rationale`, `remedy` or `scope`, so those stay from the previous claim while `statement` is overwritten.

Four confirmed collision groups in the live WastimeApp store:

| Record | `subject` / `rationale` belong to | `statement` belongs to |
| --- | --- | --- |
| `ltm_6cf6a18427e7` | chapter 02 word count | clause repetition in bonus chapters (evidence unions four unrelated topics) |
| `ltm_32b5e9cd8721` | tone/length gates not rechecking a research overclaim | using a verified sibling text as the voice bar |
| `ltm_8926d5ec6018` | rephrasing drift while restructuring chapter 06 | a duplicate heading in chapter 13 |
| `ltm_280e9e58e76a` | product mentions | an essay's closing line (collided while still pending) |

The suite pins the behavior as intended: [`test_recurrence_dedups_across_drifting_subject_by_scope`](../../../tests/memory/test_apply_delta.py) (`tests/memory/test_apply_delta.py:100`) asserts that a drifting subject on the same scope is a dedup, which is exactly the collision.

**Why this blocks the whole campaign:** a curator proposing two clean records to replace one mixed record gets the same `memory_id` for both, so the first merge overwrites the statement and the second overwrites it again — producing a worse mixed record than the one it set out to fix, logged as a routine `merged into existing … (same kind+scope.paths)`.

## Change

1. Key a claim on `kind` + a normalized **claim fingerprint** (subject/statement) + normalized scope. Scope-only agreement becomes a _candidate search_, not identity.
2. Merge only on deterministic equivalence (an exact normalized claim key, or an explicit equivalence check). A different claim at the same scope creates a **new record** or an explicit conflict — never an overwrite.
3. Make the merge field-consistent: whatever is merged updates the record as a whole (subject, rationale, remedy, scope, evidence, freshness), so no record can hold two claims' fields.
4. Migrate the existing store. Snapshots + the audit chain carry the pre-collision versions for the four groups above; split them back into atomic claims.
5. Replace the test that pins the collision.

## Acceptance

- Two candidates of the same kind and path with different subject/statement produce two records, or one explicit conflict set — never one merged row.
- Re-proposing an equivalent claim keeps every field mutually consistent (a full-record invariant, not a count/id assertion).
- Migration restores the four listed ids as separate atomic claims; each migrated record's `subject`, `rationale` and `statement` describe the same thing.
- `mixed-record rate` among active long-term records: ≥3 of 19 known → 0.

## Test

A full-record equality fixture (not `len(rows)`): ingest claim A, then claim B at the same kind+scope with a different subject, assert two records with intact fields; then re-ingest an equivalent A and assert the record is updated coherently. Regression fixtures pinned from the four real collision groups, reconstructed from snapshots. The replaced `test_recurrence_dedups_across_drifting_subject_by_scope` becomes its inverse.

## Scope / risk

Changes recurrence/dedup semantics, so the promotion gate's `seen_task_ids` accounting shifts with it — a claim that used to recur by path will now recur only when it is really the same claim, which is the point but will lower promotion counts. Needs a migration; keep the migration reversible via the existing snapshot/restore path. Do not widen the key so far that legitimate rewording spawns duplicates — that is what the fingerprint + equivalence check must absorb, and it is the main thing to test.

## Depends on

Nothing. Everything else in the campaign depends on it: no writing pass over memory — curator or otherwise — is safe while identity collapses distinct claims.
