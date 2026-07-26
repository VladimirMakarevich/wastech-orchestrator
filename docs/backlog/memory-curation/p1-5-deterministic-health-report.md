# P1.5 — `worc memory validate` should be a real health report (no model, and it is most of "auto-dream")

Priority: **P1** Status: **proposed** Date: 2026-07-26 Source: [curation analysis](2026-07-25-auto-dream-memory-curation-analysis.md) §4.11 stage 0 · [memory audit](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/analysis/2026-07-24-wastimeapp-memory-audit.md) §8.3

## Problem

Every P0 defect in this campaign was found by hand — by a person reading JSONL files and recomputing ids. The existing operator verb reports only entity-card staleness (remappable / stale) and always exits 0, so a store violating a hard invariant looks identical to a healthy one from the CLI. Meanwhile most of what the operator wants from a periodic "auto-dream" pass — what is stale, what is duplicated, what evidence does not resolve, what references are dangling, how long quarantine has been sitting — is **computable deterministically**, for free, with no model and no non-determinism.

Doing this first is also what makes the curator (P2.6) cheap and honest: the model should spend its context on judgments, not on rediscovering what a `for` loop can prove.

## Evidence

`_cmd_memory_validate` (`cli.py:2392`) prints remappable/stale entity counts and returns 0. The audit's findings, by contrast, were produced by: recomputing `derive_long_term_id` over all rows, resolving evidence refs against the tree, resolving relationship targets against entity ids, comparing quarantine scopes against cleanup semantics, and diffing snapshots. All of those are pure functions over the store plus `DerivedIndex` (`memory/derived.py`), which already answers _does this path/symbol exist_ and is already built for exactly this consumer.

What such a report would have surfaced on day one, per the audit's own counts:

| Signal | Current value on the WastimeApp store |
| --- | --- |
| Mixed-field / claim-consistency violations | ≥3 of 19 active long-term |
| Stale records that are still retrieval-eligible | 7 |
| Unresolved entity relationships | 8 of 19 (42%) |
| Semantic near-duplicate pairs (conservative, manual) | 3 pairs touching 6 of 19 |
| Evidence refs that do not resolve | not measured before the audit |
| Terminal failures represented in failure memory | 0 of 3 |
| Records with empty `first_seen_commit` / `last_verified_commit` / `supersedes` | 19 of 19 |
| Snapshots per mutating cleanup pass | 398 / 5 |

## Change

Extend the read-only verb (keep `validate`, or add `worc memory doctor` and make `validate` an alias) to report, per class, with exact ids:

1. **Integrity** — id recomputation mismatches; rows failing schema validation on read; duplicate ids within a tier; malformed rows isolated rather than silently skipped.
2. **Claim consistency** — records whose `subject` / `rationale` / `statement` provably disagree (the P0.1 signature), and exact-duplicate subjects across tiers.
3. **Freshness** — stale scope paths, remappable vs unresolvable, quarantine age (median + oldest), records never re-verified.
4. **Evidence** — refs that do not resolve to a tracked path; refs of ephemeral type (`task` / `commit` / `diff`) that will rot; records whose only evidence is unresolvable.
5. **Graph** — relationship targets that do not resolve to a current entity id; entity cards sharing a canonical path; cards with empty symbols.
6. **Retrieval hygiene** — how many quarantined records are currently retrieval-eligible and why (the P0.3 signal), and the rendered size of the largest packet against the token/line budget.
7. **Growth** — records / bytes / snapshots per task, so churn is visible before it becomes 23 MB.

Exit code: non-zero when a **P0 invariant** is violated (a retrieval-eligible stale record, an unsupported `repo-observed`, a claim-consistency violation), zero with warnings otherwise. Machine-readable output (`--json`) so the numbers can be diffed before/after and consumed by P2.6 as its input.

## Acceptance

- The command reproduces the audit's headline counts on the WastimeApp store without a model: 7 retrievable stale, ≥3 mixed, 8 of 19 unresolved refs.
- It exits non-zero while any of those is non-zero, and zero once P0.1–P0.3 have landed and the store is migrated.
- `--json` output is stable and diffable across runs on an unchanged store.
- A store with one malformed row reports it as a diagnosable finding and still reports every other class.

## Test

A fixture store seeded with one instance of each defect class asserts each is reported with the right id and class, and that the exit code is non-zero. An unchanged-store double run asserts byte-identical `--json`. A malformed-row fixture asserts isolation rather than a lost report. Property: the report never mutates the store (assert canonical state hash unchanged).

## Scope / risk

Read-only, model-free, no new dependency — the lowest-risk item in the campaign and the one with the best value-per-effort. The only real risk is scope creep into "and then fix it automatically": that is P2.7/P3.9, and mixing them here would put a mutation path into a diagnostic verb. Keep `DerivedIndex` best-effort (a git-unavailable tree must degrade to filesystem stat, not crash the report), and keep the whole report bounded so a large store cannot make it pathological.

## Depends on

[P0.3](p0-3-quarantine-state.md) for the typed quarantine reason the freshness/retrieval sections report on. Everything else it measures exists today; it can be started in parallel with P0.1/P0.2 and finished after them.
