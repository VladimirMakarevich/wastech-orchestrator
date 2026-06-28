# 02.1 — Candidate-delta contract

[phase](index.md) · [design §2,§10](../../design.md) · [acceptance: AC-W2](../../acceptance-criteria.md)

**Goal:** lock the structured `candidate_memory_delta` schema (Q9) the supervisor emits on success, plus a tolerant parser/validator that turns raw model JSON into typed records.

## Scope

In: the typed schema (dataclasses) for `lessons` / `failures` / `entities`, each with `trust_hint` + `evidence[]`; the structured-output spec for the provider turn; a tolerant parse/validate function. Out: the emit wiring (02.2), the `apply_delta` consumption (02.4), tier persistence (02.5).

## Approach

- Define the contract from [questions.md](../../questions.md) Q9: `{ lessons: [{kind, subject, statement, rationale, scope{paths,symbols,nodes}, evidence[], trust_hint}], failures: [{signature, paths, remedy?, evidence[]}], entities: [{entity_id, type, paths, symbols, summary, relationships[], risk_notes[]}] }`. Lock the exact field names here (Q9 defers them to phase-02 detail).
- `trust_hint` is **advisory only** — `MemoryService` assigns the final trust deterministically (no self-certification to high trust). State this in the schema docstring so the contract is unambiguous.
- `evidence[]` entries are **pointers** (artifact path / commit / symbol), never raw content. The schema carries the field; `apply_delta` (02.4) enforces non-empty and rejects/quarantines on missing evidence (AC-W2). Keeping the validation in `apply_delta` (not the parser) keeps the parser purely structural.
- Provide a JSON-schema / structured-output spec compatible with the provider structured-output path the supervisor turn will use (today `supervisor.finalize()` is a **free-text** turn — converting it to also yield structured output is 02.2; this task only defines the shape and the parser).
- The parser is tolerant: valid JSON → typed records; absent / partial / malformed / extra-field → `None` (skip), never raises (Q9 best-effort). Pure, deterministic, model-free.

## Files

- New `src/wastech_orchestrator/.../memory/delta.py` — schema, structured-output spec, tolerant `parse_delta()`.

## Tests

- A valid delta parses to typed records; all fields round-trip.
- Malformed / partial / extra-field / empty input → `None`, never an exception (best-effort, Q9).
- `trust_hint` never maps directly to a durable trust level (assignment is `MemoryService`'s job — assert the parser does not set final trust).

## Done when

The schema + tolerant parser exist and are unit-tested without a model; missing-evidence is detectable downstream (AC-W2 groundwork); field names are final.
