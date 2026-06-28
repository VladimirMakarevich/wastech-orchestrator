# 04.4 — `DerivedIndex` (minimal)

[phase](index.md) · [design §1](../../design.md) · [acceptance: AC-C4, AC-X1](../../acceptance-criteria.md)

**Goal:** just enough repo introspection to answer "does this path/symbol still exist?" for staleness detection (Q2) — a rebuildable cache, not memory truth.

## Scope

In: a minimal `DerivedIndex` — path existence (`git ls-files` + stat) and symbol existence (symbol grep). Out: FTS / embeddings / a full repo map (V2+); those are a separate concern (NFR3).

## Approach

- Q2 source-of-truth = the **live repo**: `git ls-files` + stat for paths, symbol grep for symbols.
- A **rebuildable cache plane** (NFR3): lives under `derived/`, is deletable and recomputable from the current tree, and carries **no audit / no snapshots** (distinct from durable memory).
- Consumed by `apply_delta` validation (02.4) and `CleanupJob` staleness (04.2). Minimal only — a richer index is out of scope.
- Cross-platform: stored/compared paths normalized with `as_posix()` (NFR8/AC-X1).

## Files

- New `src/wastech_orchestrator/.../memory/derived.py` (`DerivedIndex`).

## Tests

- An existing path/symbol → present; a removed one → absent (feeds AC-C4).
- The index is rebuildable from a clean tree (recompute matches).
- Path strings round-trip identically on Windows and POSIX (AC-X1).

## Done when

Existence queries are correct and rebuildable; staleness detection (AC-C4) is unblocked.
