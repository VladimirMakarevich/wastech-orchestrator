# V1 implementation audit

Status: **findings remediated — F1–F4 + F6 resolved, F5 partial** Date: 2026-07-01 Owner: Vladimir Makarevich — [task hub](index.md)

A full review of the shipped V1 memory subsystem against its own contract — every functional/non-functional requirement, constraint, acceptance criterion, resolved question, and the design and ADR — cross-referenced against the implementation (`src/wastech_orchestrator/memory/` plus the supervisor / orchestrator / config / prompts / CLI seams) and the test suite. This document records _what was found_; it does not change behavior. It complements [definition-of-done.md](definition-of-done.md) (the merge gate, which holds) by capturing the residual gaps the gate does not catch.

**Method.** All twelve core modules read in full; the integration seams and test coverage audited; load-bearing claims verified by grep; the suite, `ruff`, and `mypy` run; every candidate finding checked against [../follow_ups.md](../follow_ups.md) so tracked deferrals are not reported as misses.

## Bottom line

The implementation is **faithful and high-quality**. The architecture matches [adr.md](adr.md) (D1–D8), the safety properties are real and adversarially tested, and the gates pass: **137 memory tests green, `ruff check` clean, `mypy src/wastech_orchestrator/memory` clean** (verified 2026-07-01). The team was disciplined about recording V1 scope-cuts (`follow_ups.md` rows 58–61), so most of what first looks "missing" is deliberately deferred and tracked.

The findings are now remediated (2026-07-01): **F1** wired `DerivedIndex` write-time entity-path validation into `apply_delta`; **F2** added lesson existence reconciliation to `CleanupJob` (contradiction-detection stays an explicit V2 item); **F3** seeded runtime redaction with the known config/env secret literals; **F4** made `restore` prune snapshot-absent tier files; **F6** annotated the minor nits. **F5** is partial — AC-S3 and an AC-S4 disabled-vs-baseline run were backfilled; the Windows path round-trip (AC-X) and AC-SF5 ride the standing Windows-CI follow-up. Per-finding resolution notes are inline below.

## Conformance summary (what holds)

| Area | Status | Evidence |
| --- | --- | --- |
| FR1 / C2 / AC-S2 / AC-S3 — files-first, gitignored, never in `state.db` | ✅ | `.worc/` ignored wholesale; `DB_SCHEMA_VERSION = 13`, no memory tables; the `evaluations` marker reuses the existing table |
| FR3 / AC-W1 / NFR9 — write at close, **zero extra LLM** | ✅ | delta rides the single finalize turn; `tests/core/test_supervisor.py` asserts the provider-request count is `1` whether memory is on or off |
| FR3 / AC-W3 — failure seam deterministic, never promotes | ✅ | `orchestrator._record_failure_memory` passes `delta=None`; `service.apply_delta` quarantines on `WriteSource.FAILURE` |
| FR3 best-effort — never blocks publish/close | ✅ | the memory write is wrapped in `try/except` (`BLE001`), logged and swallowed |
| FR4 / AC-R1 — node-driven packet by `{memory_path}`, never the root | ✅ | the agent node builds a packet only when its role prompt references the variable; path is `logs/<task-id>/memory/<node>.md` |
| FR5 / C4 — model-free deterministic core | ✅ | nothing under `memory/` calls an LLM; `trust_hint` is ignored, trust is assigned from evidence |
| FR7 / Q10 / AC-S5 — global switch, absent block → disabled, schema **v24** | ✅ | consistent across schema / loader / orchestrator / cli / prompts; the disabled path is airtight |
| NFR5 / C1 / AC-SF1 — redaction before every write | ✅ | single `_redact` chokepoint + audit rows redacted; planted-secret drill = **0 leaks**; runtime now also seeded with known config/env secret literals (F3 resolved) |
| AC-SF2 / AC-W4 — poisoning: low-trust / external never auto-promote | ✅ | `should_promote` gate + poisoning drill (quarantined, never out-ranks a trusted record, never silently overwrites) |
| FR8 / AC-SF3 / AC-SF4 — hash-chained audit + snapshot/restore | ✅ | `verify_chain`; rollback drill byte-identical, including pruning a tier file the cleanup first-created (F4 resolved) |
| FR6 / NFR6 / AC-C2 / AC-C3 — bounded idle cleanup | ✅ | idle-gap + no-active-task gate + rate-limit + budget; never promotes / edits code; no network; no `os.kill`/`signal` |
| AC-C1 — `worc memory show \| validate \| compact \| restore` + `--dry-run` | ✅ | read-only `show`/`validate`; mutating verbs gated on no active task; disabled → no-op |
| NFR4 / AC-R2 / AC-R3 — caps + whole-record drop + determinism | ✅ | `packet._fit` / `_drop_lowest`; deterministic-build test |
| NFR8 / AC-X1 — POSIX + `newline="\n"` + deterministic JSON | ✅\* | `as_posix()` throughout; `_io.py` fixes newline + `sort_keys` (\* Windows round-trip untested — see F5) |
| Q1 / Q3 / Q5 config defaults | ✅ | every integer matches the locked design |

## Findings

### F1 — ✅ RESOLVED (2026-07-01) — write-time entity-path validation wired into `apply_delta`

**Resolution.** `MemoryService` now takes an optional `DerivedIndex`; the orchestrator's `_memory_service` builds one over the live repo (same construction as the cleanup hook) and passes it in. `lifecycle.assign_entity_trust` gained a `path_exists` predicate, so `_ingest_entity` resolves each entity card's `paths` against the current tree: a card earns the durable `repo-observed` only when **all** its named paths verify present; an unverifiable path downgrades it to `agent-inferred`, which falls into the existing non-durable → quarantine branch (preserved, recoverable, never deleted). Best-effort and fail-closed (`path_exists` never raises; git-unavailable → filesystem stat), and the write is still `try/except`-wrapped so it never blocks the task. Read-only / legacy callers that wire no index keep the prior behavior. Tests: `test_apply_delta.py::test_entity_verified_path_stored_missing_or_pathless_quarantined` (+ a no-index back-compat case) and `test_lifecycle.py::test_assign_entity_trust_validates_paths_when_predicate_given`. `follow_ups.md` 61(a) corrected. Scope: entity-card paths only — lesson `scope.paths` existence is F2 (cleanup); symbol existence stays unwired (F6).

The original finding, for the record:

The highest-priority item. The plan deliberately **stubbed** path/symbol validation in phase 02, to be **tightened once `DerivedIndex` landed in phase 04**:

- [plan/02-write-path/04-apply-delta.md](plan/02-write-path/04-apply-delta.md) — _"Validate — resolve referenced paths/symbols via DerivedIndex (… may stub until phase 04.4 lands, **then tighten**); reject missing evidence; label external-only"_.
- [plan/04-curation/index.md](plan/04-curation/index.md) — _"DerivedIndex (04.4) is consumed by both `CleanupJob` (04.2) staleness **and `apply_delta` (02.4) validation**"_.

Phase 04 shipped `DerivedIndex` and wired it into `CleanupJob` — but **never wired it back into `apply_delta`**. Verified: `MemoryService` is constructed with no index (`orchestrator.py` `_memory_service` → `MemoryService(layout, config=..., marker=...)`), and `apply_delta` performs no existence check (the module docstring still reads _"DerivedIndex-backed path/symbol validation are later phases"_).

**Consequence.** An entity card from a _successful_ task is assigned the **durable `repo-observed`** trust for merely _naming_ a path, with no check that the path exists (`lifecycle.assign_entity_trust` → `service._ingest_entity`); a durable card then **out-ranks `artifact-backed`** in packet selection (`packet._TRUST_RANK`). A hallucinated/renamed path therefore becomes a durable, preferentially-served card until a later cleanup pass quarantines it — the NFR2 guarantee ("source of truth is code; durable entries verified") that the design placed at write time is not enforced there. The test enshrines the current behavior (`tests/memory/test_apply_delta.py::test_entity_with_paths_is_stored_pathless_is_quarantined` — a path _string_, not a verified path).

Blast radius is bounded (success-only, supervisor-sourced, eventually reconciled by cleanup). But **`follow_ups.md` row 61(a) still reads _"no DerivedIndex existence check yet; tightens in phase 04.4"_** — written as if pending, when 04.4 shipped without it. The deferral note has gone stale; this is now a true (small) V1 gap, not a within-phase sequencing note.

**Recommendation.** Either wire `DerivedIndex` into `apply_delta` (the planned tighten: resolve entity/lesson `scope.paths`, downgrade an unverifiable card off `repo-observed`, quarantine on a missing target), **or** make an explicit decision to leave write-time validation to cleanup — and in either case rewrite `follow_ups.md` 61(a) so it no longer claims a tighten that has already shipped without it.

### F2 — ✅ RESOLVED (existence half, 2026-07-01) — lessons now existence-checked; contradiction half = explicit V2

**Resolution.** `CleanupJob` gained `_reconcile_lessons`, mirroring `_reconcile_entities`: an active long-term lesson whose `scope.paths` names a since-vanished target is moved to quarantine (never deleted, never a judgment-based drop), quarantine-only (no basename remap, since a lesson's path scope is advisory metadata). A path-less lesson has no existence signal and is left fully intact, so the long-term auto-drop boundary holds. The contradiction half stays a **deliberate V2 deferral** — there is no contradiction ledger in V1, so `should_promote(has_contradiction=…)` is never fed `True`; the residual "lessons can still accrete contradiction-rot until a human curates them" risk is recorded in `follow_ups.md` 59(a) with the watch-queue steady state as the revisit trigger. Tests: `test_memory_staleness_drill.py::{test_lesson_scoped_to_present_path_is_kept,test_lesson_scoped_to_vanished_path_is_quarantined_not_deleted}` (+ the path-less-lesson boundary).

The original finding, for the record:

Two individually-tracked cuts combine into something the per-phase rows do not connect:

- Cleanup reconciles **entities** by path existence, but **lessons are never existence-checked** — `tests/memory/test_memory_staleness_drill.py::test_lesson_with_present_path_is_never_dropped_on_judgment` states the rule outright: _"cleanup never drops a long-term lesson on judgment."_ (tracked: row 59a)
- The `lifecycle.should_promote(has_contradiction=…)` gate exists but is **never fed `True`** by any caller — the only call site (`service.py`) omits the argument, and no contradiction ledger exists. (tracked: row 61b)

So a lesson scoped to a since-deleted file persists forever, and a lesson contradicted by current reality is never quarantined. The **only** lesson lifecycle in V1 is merge/dedup. That is in direct tension with design §5 ("Drop as stale: … lesson contradicted twice … superseded") and the ADR's central motivation ("without becoming an opaque, **rotting**, or attackable context dump"), and it bites hardest in the steady-state `watch`-a-queue scenario the subsystem is built for. Both halves are tracked; their _union_ — "lessons can only accrete, never rot out, in V1" — is not called out anywhere.

**Recommendation.** Record the union as an explicit risk with the watch-queue steady state as the trigger to revisit; it is the ADR's headline failure mode. Closing F1 (existence checks in `apply_delta`/cleanup, extended to lessons) covers the existence half; the contradiction half stays a deliberate V2 item.

### F3 — ✅ RESOLVED (2026-07-01) — runtime redaction now seeded with known secret literals

**Resolution.** `_memory_service` builds `extra_secrets` from `_memory_extra_secrets()` — `secret_env_values(security.allowed_environment)` (values of non-allowlisted, secret-named parent env vars) + `read_denied_secrets(repo.local_path, security.denied_read_paths)` (the repo's `.env` / `secrets/**`) — the same sources the provider adapters already scrub from artifacts. The env-harvesting logic was extracted to a single shared `redaction.secret_env_values` (the adapter's `_secret_env_values` now delegates to it). Tests: `test_redaction.py::test_secret_env_values_harvests_only_non_allowlisted_secret_names` and a redaction-drill pair (`test_env_secret_leaks_without_harvesting` shows the structural-only gap; `test_orchestrator_style_env_secret_is_scrubbed` shows the wiring closes it).

The original finding, for the record:

The orchestrator builds the service with **no** `extra_secrets` (`orchestrator._memory_service` → `MemoryService(layout, config=..., marker=...)`), so C1 / AC-SF1 protection at runtime rests entirely on `redact_text`'s structural patterns. A repo-specific secret that matches no known pattern (and is not a recognized token shape) would not be caught. The redaction drill plants only pattern-matching secrets, so it cannot surface this. Tracked (row 61f) as "denied-file secret harvesting not wired", but because C1 is a hard invariant this deserves a deliberate decision rather than a quiet default.

**Recommendation.** Feed the known config/env secret literals into `extra_secrets` at construction, or explicitly record the residual structural-only risk against C1.

### F4 — ✅ RESOLVED (2026-07-01) — `restore` prunes tier files absent from the snapshot

**Resolution.** `MemoryService.restore` now computes the set of files captured in the snapshot dir and, before restoring, prunes any **canonical tier** file that exists now but is absent from the snapshot (e.g. a `quarantine/pending.jsonl` first-created by the cleanup pass being rolled back). The pruning is scoped to the tier candidate set (`_tier_candidates`) — the append-only audit log, snapshots, and the derived cache are never touched — so the store returns to the exact pre-snapshot state, not a superset. The rollback drill now starts from an empty-quarantine store, asserts the cleanup first-creates `pending.jsonl`, and asserts it is gone (and `set(after) == set(before)`) after restore.

The original finding, for the record:

`take_snapshot` / `tier_files()` only capture files that already exist (`service.tier_files` filters on `path.is_file()`; `audit.take_snapshot` skips non-files). If a cleanup pass quarantines the first-ever stale entity, it _creates_ `quarantine/pending.jsonl`, which was not in the snapshot — so `restore_snapshot` will not remove it. The store is then **not** byte-identical to pre-cleanup, contrary to AC-SF4's "returns memory to the pre-cleanup state". The rollback drill does not trip it because its store already has the file.

**Recommendation.** Have the snapshot record the expected file _set_ (or have `restore` prune tier files absent from the snapshot), and extend the rollback drill to start from an empty-quarantine store. Low frequency, inert leftover, but a real fidelity hole.

### F5 — ◑ PARTIAL (2026-07-01) — AC-S3 + AC-S4 backfilled; Windows / AC-SF5 ride the standing follow-up

**Resolution.** **AC-S3** now has a state-store guard (`test_state_store.py::test_no_memory_tables_in_state_db`) asserting the exact table set carries no memory-tier table — the subsystem's only `state.db` touch stays the reused `evaluations` marker. **AC-S4** now has a real disabled-vs-baseline pair (`test_orchestrator.py::{test_memory_disabled_run_writes_no_store,test_memory_enabled_run_writes_store}`): the same complete happy-path task writes **no** `.worc/memory` store when disabled and a short-term episode when enabled. Still deferred (consistent with the documented "Windows CI matrix is the standing follow-up"): **AC-X1/AC-X2** Windows path round-trip, and **AC-SF5** re-blocking a hand-edited low-trust _active_ record.

The original finding, for the record:

- **AC-S3** (nothing in `state.db`) — structurally true (no memory tables; marker reuses `evaluations`), **no test**.
- **AC-S4** ("disabled = byte-for-byte today's, _regression-tested_") — covered at config/CLI level only; there is **no end-to-end disabled-vs-baseline task-run** comparison, which is what the AC literally asks for.
- **AC-X1 / AC-X2** (Windows path round-trip / no `os.kill`/`signal`) — POSIX storage is tested and a grep confirms no `os.kill`/`signal` in `cleanup.py`/`cli.py`, but there is no Windows-form round-trip test. Consistent with the documented "Windows CI matrix is the standing follow-up" — flagged as unverified, not enforced.
- **AC-SF5** — promotion-time enforcement is tested; a hand-edited low-trust _active_ record being re-blocked is not.

None are behavioral bugs; they are confidence gaps in the test net for criteria the DoD checks off.

**Recommendation.** Backfill at least AC-S3 (assert no memory rows reach `state.db`) and an AC-S4 disabled-vs-baseline run; let the rest ride the Windows-CI follow-up.

### F6 — ✅ RESOLVED (annotated, 2026-07-01)

- **`cleanup_promotions_per_pass`** — annotated as a documentation-only invariant in `config/schema.py` and `cleanup.py`: the never-promote guarantee is structural (`CleanupJob` has no promote code path), so the knob is not read at runtime and a non-zero value is inert.
- **`DerivedIndex.symbol_exists`** — its docstring now states it is intentionally unwired in V1 (path existence only — F1/F2) and marks the seam for the deferred symbol-level validation, so it no longer reads as dead code.
- **`EpisodeRecord.stage_outcomes`** — annotated write-once: built at construction, only ever read (serialized via `as_row`), kept a `dict` for JSON symmetry; treat as immutable despite `frozen=True`.
- **Entity merge is wholesale "latest card wins"** (`service._ingest_entity`) — left as tracked V1 scope cut (row 61c); a field-union merge is a later refinement, out of scope for this sweep.

## Already-tracked deferrals (verified — not reported as misses)

`follow_ups.md` rows 58–61 already capture: a real eval baseline pending (synthetic for now); V2/V3/V4 gated on a measured recall/quality lift (AC-O4); `worc memory add/edit` (V1.x); no stage-2 semantic rerank; symbol-scoped retrieval build-capable but unwired at runtime; in-memory rate-limit (a restarted daemon may run one pass early); same-basename-only rename-remap; and `entities/index.md` / `aliases.json` / `short_term/runs/<id>/episode.json` not materialized. These are legitimate, recorded scope decisions and are out of scope for this audit's findings.

## Actions taken (2026-07-01)

1. **F1 — done.** Wired `DerivedIndex` into `apply_delta` (resolve entity-card paths, downgrade an unverifiable card off `repo-observed`, quarantine on a missing target) and corrected the stale `follow_ups.md` 61(a) text.
2. **F2 — done (existence half).** Added `CleanupJob._reconcile_lessons` (quarantine a lesson scoped to a vanished path, never delete) and recorded the contradiction-rot risk + the watch-queue trigger as an explicit V2 item in `follow_ups.md` 59(a).
3. **F3 — done.** `_memory_service` passes known config/env secret literals as `extra_secrets` (`secret_env_values` + `read_denied_secrets`), via a shared harvesting helper the adapters reuse.
4. **F4 — done.** `restore` prunes snapshot-absent canonical tier files; the rollback drill now starts from an empty-quarantine store.
5. **F5 — partial.** Backfilled AC-S3 (no memory table in `state.db`) and an AC-S4 disabled-vs-baseline run; AC-X (Windows round-trip) and AC-SF5 ride the standing Windows-CI follow-up.
6. **F6 — done.** Annotated `cleanup_promotions_per_pass` (documentation-only invariant), `symbol_exists` (intentionally unwired in V1), and `EpisodeRecord.stage_outcomes` (write-once); the wholesale entity merge stays the tracked row-61(c) scope cut.

All changes are additive; none blocks the existing V1 merge gate. F1 and F2 — the two touching the subsystem's stated invariants (NFR2, anti-rot) — landed first, before the subsystem is exercised on a long-lived repo.
