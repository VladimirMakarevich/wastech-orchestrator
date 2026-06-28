# 01.3 — MemoryService skeleton

[phase](index.md) · [design §1,§4,§7](../../design.md) · [acceptance: AC-SF1/SF5](../../acceptance-criteria.md)

**Goal:** the deterministic core that owns the canonical store — record types and safe write primitives — without yet wiring the supervisor (that is phase 02).

## Scope

In: record dataclasses for the three tiers (schemas per design §4 / blueprint §5.3), trust-level enum, redacted + atomic append/update primitives, a read API the `PacketBuilder` will use later. Out: `apply_delta` promotion logic (phase 02), retrieval/packets (phase 03).

## Approach

- Define typed records: short-term episode, long-term (semantic/procedural/reviewer/failure), entity card — each carrying provenance + `trust_level`.
- Trust levels: `repo-observed | human-curated | review-verified | artifact-backed | agent-inferred | external-untrusted` (design §7).
- Every write: `redact_text`/`redact_mapping` first, then the atomic temp-file-then-rename pattern (mirror `_atomic_json`). No write bypasses redaction.
- Pure, deterministic, model-free — unit-tested without a model.

## Files

- New `src/wastech_orchestrator/.../memory/service.py` (+ `records.py`/`trust.py` as needed).

## Tests

- Planted-secret string is redacted before it reaches disk (AC-SF1) — table-driven.
- Trust level is required on every record; promotion gate (added in phase 02) can read it (AC-SF5 groundwork).
- Concurrent-safe atomic writes (no partial files on interrupt).

## Done when

Records + redacted atomic writes exist and are unit-tested without a model; no write path skips redaction.
