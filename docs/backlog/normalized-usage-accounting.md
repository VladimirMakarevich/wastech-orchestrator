# Normalized token-usage accounting (provider-aware, persisted per attempt)

**Status:** open **Priority:** P0 (measurement substrate — blocks any token A/B) **Source:** [2026-07-16 token analysis](../analysis/2026-07-16-blog-review-happy-in-my-misfortunes-4-token-analysis.md) (F1 / P0), realizes Phase 0 of [archive/token_optimization.md](archive/token_optimization.md)

## Problem

`AgentRunResult.usage` is a free-form `dict[str, Any]` passthrough of whatever the CLI emitted (`providers/base.py:218`), copied verbatim onto the result (`providers/_adapter_base.py` ~432) and written only into the flat per-attempt `result.json` (`artifacts.py` ~224). It is **not** persisted in SQLite at all — `provider_attempts` has no usage column (`state_store.py` ~222-234 / `ProviderAttemptRow` ~394-405) — and nothing in the codebase ever sums usage across nodes. Three concrete problems follow.

1. **Codex resume usage is cumulative for the whole session.** On `codex exec resume`, `turn.completed.usage` includes all prior turns (`providers/codex.py` terminal branch ~363-372). Any analysis that sums per-node `result.json` double-counts the parent node's usage. In the analyzed run this inflated the reported Codex total from a true 282 699 to 424 163 input tokens (+50%). There is no `scope`, no baseline, and no delta concept anywhere.
2. **Claude usage is split across three input fields that are never summed.** Claude's real input is `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` (`providers/claude.py` ~409-411 stores the raw dict, same shape as Codex). Reading only `input_tokens` shows a misleadingly tiny 35 tokens for a run that actually processed 683 078.
3. **Latent bug found while investigating:** `_produced_no_work` decides "empty run" on `output_tokens == 0` (`providers/_adapter_base.py` ~96-111, gated in `run` ~442). On a resumed Codex node the cumulative `output_tokens` is never 0, so the no-work guard can never fire on a resume — it is silently defeated by the same cumulative-usage semantics.

The join key needed to compute a Codex resume delta **already exists**: the durable `raw_session_id` is persisted in `editing_lineage` / `node_lineage` (`state_store.py` ~297-315, redacted everywhere else). It simply has no usage row to subtract against.

## Required outcome

One provider-aware normalized usage record, persisted per attempt in SQLite alongside the raw payload, so that summing per-node usage is correct by construction and a token baseline can be collected.

## In scope

- A typed normalized-usage structure with, at minimum: `scope` (e.g. `session_cumulative` vs `per_invocation`), `input_total`, `cache_read`, `cache_write`, `uncached_input`, `output_total`, `reasoning_output`, `cost` (nullable).
- Keep the raw provider payload verbatim for audit (`provider_usage_raw`); normalized fields are derived, not a replacement.
- **Codex:** fresh run → scope `session_cumulative` with baseline 0. Resume → subtract the previous snapshot of the same provider session (baseline keyed by the durable lineage/session id already in `editing_lineage`/`node_lineage`). If a new snapshot is smaller than the baseline (reset / compaction / version drift), keep raw, mark the delta unknown, and log a warning — never emit negative tokens.
- **Claude:** normalize as `per_invocation` with `input_total` = sum of the three input categories.
- Persist normalized usage (and cost where the CLI reports it) per attempt in SQLite — the `provider_attempts` entity is the natural home.
- Fix `_produced_no_work` to use the normalized per-run delta, so the no-work guard works on resumed Codex nodes.

## Acceptance criteria

- [ ] A test reproduces the analyzed run: a fresh `turn.completed` (input=141464, cached=76288, output=8329, reasoning=5935) followed by a resume `turn.completed` (input=282699, cached=187904, output=9364, reasoning=6066) yields a resume delta of input=141235, cached=111616, output=1035, reasoning=131, and a whole-session total equal to the latest snapshot only (input=282699), never the naive sum (424163).
- [ ] Claude usage normalizes to `input_total = input_tokens + cache_creation_input_tokens + cache_read_input_tokens`.
- [ ] Normalized usage is queryable per attempt from SQLite; the raw payload is still retrievable for audit.
- [ ] A smaller-than-baseline resume snapshot degrades to raw + `delta unknown` + warning, with no negative values.
- [ ] `_produced_no_work` fires correctly on a resumed Codex node that did nothing (per-run output delta == 0).
- [ ] No secrets / raw session ids leak into SQLite usage rows or artifacts (raw session id stays only in the existing lineage tables).

## Out of scope

- Migration / backfill machinery — greenfield, nothing is deployed (see [greenfield-mvp-no-migration] memory / [architecture.md](../../.agents/rules/architecture.md)). Add the schema directly, no migration path.
- Cross-provider dollar normalization by billing rate (Codex ran on subscription and records no cost) — raw totals are a within-provider rate-limit/relative signal only.
- Deterministic artifact reduction (sink B) and RTK (sink A) — those are Phase 1/2 of [archive/token_optimization.md](archive/token_optimization.md), separate work.
- Supervisor cadence — tracked separately (see [../analysis/2026-07-16-supervisor-token-optimization-options.md](../analysis/2026-07-16-supervisor-token-optimization-options.md)).

## Likely implementation areas

- `src/wastech_orchestrator/providers/base.py` (typed usage on `AgentRunResult`)
- `src/wastech_orchestrator/providers/_adapter_base.py` (`_produced_no_work`, usage plumbing)
- `src/wastech_orchestrator/providers/codex.py` and `providers/claude.py` (provider-aware normalization)
- `src/wastech_orchestrator/state_store.py` (`provider_attempts` usage columns + read/write; baseline lookup via existing lineage tables)
- `src/wastech_orchestrator/artifacts.py` (persist normalized alongside raw in `result.json`)
- `tests/providers/` (parsing + resume-delta), `tests/` state-store round-trip
- `docs/` — record the usage contract where token behavior is documented
