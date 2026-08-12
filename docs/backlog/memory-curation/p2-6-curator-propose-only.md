# P2.6 — the curator: a read-only, propose-only model pass over the store (`worc memory audit`)

Priority: **P2** Status: **proposed** Date: 2026-07-26 Source: [curation analysis](2026-07-25-auto-dream-memory-curation-analysis.md) §4.1, §4.3–§4.6, §4.9 — the operator's `auto-dream` idea, reshaped

## Problem

Five classes of memory decay are real, confirmed in the live store, and **not reachable by deterministic code**:

| Class | Why code cannot do it | Confirmed instance |
| --- | --- | --- |
| Semantic duplicates | `_collapse_duplicates` keys on `normalize_subject` — lowercase + whitespace collapse (`memory/cleanup.py:398`), so a paraphrase never matches | 3 near-duplicate pairs, 6 of 19 records; `ltm_af257b2e3b95` and `ltm_b8a499659d17` state one rule |
| Contradiction against current reality | Requires reading the file and judging whether the claim still holds | `should_promote(has_contradiction=…)` exists and is never computed (`memory/lifecycle.py:93`) |
| Evidence entailment | Existence is deterministic (P0.2); "does this file support this statement" is not | ≥1 confirmed semantically wrong evidence source |
| Claim atomicity | Where one thought ends and another begins is a judgment; sources are in snapshots | 4 collision groups (P0.1) |
| Human-label references | Canonicalization is deterministic, but `Part9` / `Part11` need interpretation | 8 of 19 relationships unresolved |

The memory ADR's own answer to this was "until **a human** curates them". This item automates the _finding_, not the _deciding_.

## Change

A new `memory/curator.py` — **beside** `CleanupJob`, never inside it, so cleanup's model-free/deterministic invariant (`memory/cleanup.py:1-17`) stays intact.

**Contract.** The pass reads the store, the deterministic report from [P1.5](p1-5-deterministic-health-report.md), and the repository. It writes **only**:

```
.worc/memory/curation/<timestamp>/
  report.md          # human-readable: what it found, on which records, what it disputes
  proposals.jsonl    # machine-readable: one proposal per line
```

Each proposal: `op` (closed enum), `target_ids`, `rationale`, `evidence` (non-empty — dropped otherwise, the `parse_follow_ups` rule at `supervisor.py:258`), `confidence`, `reversible`, `proposal_hash`. A malformed proposal is dropped, never raised — the pass is best-effort by contract, exactly like every other supervisor turn.

**Authority table** (the campaign's central rule):

| Operation | Who applies it | Why |
| --- | --- | --- |
| Mark stale (path gone / unresolvable) | code, automatically | fully deterministic — already `CleanupJob`'s job |
| Mark a conflict set, withhold both versions from packets | code, on proposal | mechanically checkable, reversible, fail-closed |
| Flag evidence unsupported | code for "does not exist"; proposal for "does not entail" | the consequence (losing durable trust) is reversible |
| Collapse near-duplicates | **proposal → human** ([P2.7](p2-7-review-and-apply.md)) | irreversibly loses one record's wording |
| Split a mixed record into atomic claims | **proposal → human** | the model guesses claim boundaries; a wrong guess is new damage |
| Rewrite `statement` / `subject` / `rationale` | **proposal → human** | the laundering class — the most dangerous |
| Resolve a relationship to a canonical id | code on exact match; proposal otherwise | exact match is verifiable |
| Create a record that was not in the store | **forbidden** | the curator audits the store; knowledge is produced by a task with evidence |
| Assign a trust level | **forbidden** | `assign_trust` stays the only source (AC-SF5) |
| Promote to durable | **forbidden** | AC-C3, and the P0.2 ceiling |
| Delete a record | **forbidden** | quarantine, never silent delete (Q2) |
| Edit repo files / docs / skills | **forbidden** | AC-C3 |

**Trigger** — a health signal, never a run counter (a counter is uncorrelated with need: 20 chapter tasks add almost nothing, 3 renames create nearly all the staleness):

```
run only if ALL hold:
  the store changed since the last pass    (state_hash != last_curated_state_hash)
  AND (new/changed long-term records >= N
       OR quarantined records >= M
       OR median quarantine age > D days
       OR P1.5 reports >= 1 P0 invariant)
  AND >= T hours since the last pass
  AND no active task
```

`last_curated_at` / `last_curated_state_hash` live in `manifest.json`, which already carries `MEMORY_SCHEMA_VERSION` as a migration hook and placeholder tier fields (`memory/paths.py:27,122`). A task counter is legitimate only as a **ceiling** ("no more than one pass per N tasks"), counting tasks that actually wrote a delta, stored durably — not the in-memory rate limit the idle hook uses today (`cli.py:1580`).

**Seam and identity.** Runs in the watch-loop idle gap (already guarded by `has_active_task`, `cli.py:1673`) and behind `worc memory audit`, alongside `show / validate / compact / restore / clear` (`cli.py:2335`). It gets its own route identity `memory_curator` with its own config block modelled on `SupervisorConfig` (`config/schema.py:501`) — own `model`, `reasoning`, `role_file`, `permission_profile` forced `read-only`. It must **not** reuse the per-task supervisor: that layer holds a durable lineage session scoped to a task (`__supervisor__`, `supervisor.py:75`), and mixing store-wide curation into it corrupts both jobs and pays for a warm task session.

**Structured-output trap.** `_schema_safe_reasoning` (`supervisor.py:199`) caps a schema turn to `high`, because at `xhigh`/`max` the provider spends the turn thinking and never emits a valid tool call (`error_max_structured_output_retries`). Curation is reasoning-heavy **and** structured, so it hits this first: either cap the same way, or split into a free-text analysis turn followed by a compact structured turn over its own result.

**Incrementality.** Curate what changed since `last_curated_state_hash`, not the whole store by default. Today the whole store is 73 495 B (≈20–30k tokens) and fits in one call — which is exactly why "review everything" looks free now and stops scaling at the first thousand records.

## Acceptance

- The pass never mutates a tier file: canonical state hash before == after, for every input including a store full of defects.
- A proposal without resolvable evidence never reaches `proposals.jsonl`.
- The op enum is closed: an unknown op is dropped, and no code path exists for create / assign-trust / promote / delete.
- A no-op pass (unchanged store) writes 0 files and 0 audit events, and does not run at all when the trigger's conditions are unmet.
- A pass attempted while a task is active refuses, symmetrically to `compact` / `restore`.
- The same store + same repo yields the same proposals (idempotent enough to diff two consecutive passes).
- Cost per pass is recorded, so the "≈100–300k input tokens, ≈$0.2–0.6" estimate can be replaced with a measurement.

## Test

A fixture store seeded with one instance of each of the five classes asserts a proposal of the expected `op` for each, with evidence. An adversarial fixture: a proposal payload attempting `op: create` / `assign_trust` / `promote` / `delete` is dropped. A no-op fixture asserts zero files written. An active-task fixture asserts refusal. A determinism test runs the pass twice against a fake provider returning the same output and asserts identical `proposals.jsonl`. Provider failure (no route, infra error, malformed output) yields an empty proposal set and a logged warning — never a raised exception.

## Scope / risk

This is where a model enters the memory subsystem, so every guarantee it might weaken must be structural rather than prompt-based: read-only permission profile, no write API reachable from the curator module, a closed op enum in code, and the trust ceiling from P0.2. Two things to watch: (a) cost is not the objection (one supervisor observe turn already measured 44 107 input tokens, and seven advisory calls were 70,3% of a task's Claude input — see `../token-optimization/`), but the cost of a **wrong** proposal multiplies into every future packet, which is why nothing auto-applies here; (b) an "authority halo" — a curated-looking record invites less scrutiny, so the report must state plainly that a proposal is a hypothesis until approved.

## Depends on

[P0.1](p0-1-claim-identity-and-merge.md), [P0.2](p0-2-evidence-validation-and-trust.md), [P0.3](p0-3-quarantine-state.md), [P1.4](p1-4-memory-audit-trail.md), [P1.5](p1-5-deterministic-health-report.md). All five: identity so a corrected claim can exist; trust so a synthesized claim cannot self-certify; quarantine so the curated store is not partly mis-read; audit so a later edit is distinguishable from corruption; and the deterministic report so the model spends its context on judgments instead of rediscovery.
