# 02.5 — Tier persistence

[phase](index.md) · [design §3,§4,§5](../../design.md) · [acceptance: AC-W3, AC-X1](../../acceptance-criteria.md)

**Goal:** persist records into the correct tier files — append episodic, update entity cards, and write long-term only when promotion passes.

## Scope

In: the tier-write layer `apply_delta` (02.4) calls: short-term (`short_term/recent.jsonl` + `runs/<task-id>/episode.json`), long-term (`long_term/semantic|procedural|reviewer|failures.jsonl`), entity (`entities/entities.jsonl` + `index.md` + `aliases.json`). Out: the promotion decision (02.4), retrieval (phase 03), cleanup (phase 04).

## Approach

- **Short-term** stores a **distilled episode + `artifact_paths` pointers only** (Q7) — never raw transcripts / full diffs / large logs (C3); resume/debug-grade detail stays in `logs/<task-id>/` + `state.db`. Append with a TTL stamp (14–45d, design §4).
- **Long-term** appends to the matching jsonl **only after** promotion passes (02.4); one short, repo-specific sentence per lesson.
- **Entity cards** carry name / aliases / paths / symbols / summary / relationships / risk_notes / last-validated commit (design §4). Entity is the **last** tier landed in the staged order (FR2/Q8) because its staleness checks need `DerivedIndex` (04.4).
- All writes are **redacted + atomic** (01.3 primitives), **UTF-8 with explicit `\n`** (NFR8, so audit hashes are stable), and any stored path string normalized with `as_posix()` (NFR8/AC-X1).

## Files

- `src/wastech_orchestrator/.../memory/service.py` (or a `memory/tiers.py` helper).

## Tests

- An episode stores pointers, not raw detail (Q7/C3).
- Long-term writes happen only on promotion (gated by 02.4).
- An entity card round-trips; aliases resolve.
- Path strings round-trip identically on Windows and POSIX (AC-X1); files are UTF-8 + `\n`.

## Done when

The three tiers persist correctly with promotion gating; cross-platform round-trip holds (AC-X1).
