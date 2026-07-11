# Implementation sequencing plan for the eight open backlog ADRs

Status: **tracking** (2026-07-11) Date: 2026-07-11 Owner: Vladimir Makarevich

A build-order plan for the eight currently-proposed backlog ADRs, chosen to avoid merge conflicts and honor the logical dependencies between them. Ordering is driven by two axes: **logical dependency** (what an ADR assumes another has already done) and **shared hot files** (where two ADRs edit the same function/region and would collide at merge). This is a coordination document — it does not restate or supersede any ADR's own decision; each linked ADR remains the source of truth for its scope.

## Recommended order

1. [reliable-rate-limit-handling](reliable-rate-limit-handling.md) — root of the infra-failure group. Establishes the `detect → raise → fallback → park → queue-pause` seam at `_adapter_base` finalize, the no-op fixing guard in `_charge_rework`, the park path, and the auto_mode queue circuit-breaker. Two other ADRs build on this seam.
2. [no-work-agent-run-is-infra](no-work-agent-run-is-infra.md) — explicitly **generalizes** #1 ("specific signatures first, generic no-work test last") at the same finalize seam, and narrows F4 into a core-side stall guard layered on top of #1's guard.
3. [universal-resume-recovery](universal-resume-recovery.md) — the ADR states automatic infra recovery is "owned by the rate-limit ADR and assumed here". Its grant-cycles / WIP-tolerance controls touch `_charge_rework` and the park path and must sit on top of correct classification + guards. Phase 0 (fresh fix budget + WIP tolerance) is enough to close the p6-04 catch-22.
4. [preserve-node-run-artifact-history](preserve-node-run-artifact-history.md) — largely standalone (its own writers in `artifacts.py`, `observability.py`, `postprocess.py`, `checks.py`, `tool.py`); the only overlap is `evaluator.py` in a different region than #2. Placed right after the infra group so better per-run history helps debug exactly the failures that group classifies.
5. [flow-validation-cli-command](flow-validation-cli-command.md) — small, low-risk: removes flow validation from preflight (kills the false `NOT ready` on mdlint) and adds `worc validate-flow` plus an operator-only registry seam. First of the two registry ADRs.
6. [packaged-delivery-only](packaged-delivery-only.md) — the heavier architectural change to the same `registry.py`. Easier after #5: preflight no longer walks packaged flows and #5's operator-only seam becomes automatic, so #6 just deletes the now-redundant runtime fallback.
7. [memory-concepts-over-episodic-ledger](memory-concepts-over-episodic-ledger.md) — separate domain (`memory/`, supervisor, episode-write); independent of #1–#6. Substantial refactor, done before the small #8 so the memory hub is already in its V2 shape.
8. [agent-native-memory-opt-in](agent-native-memory-opt-in.md) — smallest and most exploratory (default-off flag gating the F37 deny in `providers/claude.py`); nothing depends on it and its doc cross-link lands cleanly on the already-updated memory hub.

## Why this order

The single tightest constraint is that **three ADRs edit `_charge_rework` ([engine.py:282-299](../../src/wastech_orchestrator/core/flow/engine.py#L282-L299))**: #1 (no-op guard), #2 (stall guard), #3 (grant-cycles). The safe merge order coincides with the logical layering — classification and guards first (#1 → #2), recovery on top (#3). Any other order means hand-resolving conflicts and risks grant-cycles bypassing a guard. The same "specific before generic" logic applies at the shared `_adapter_base` finalize seam and in `providers/claude.py`, where #1 and #2 both touch classification/raise.

The registry pair (#5, #6) has a stated tension: #5 says "keep the `resolve()`/`_find` fallback as-is", #6 removes it. This is not a deadlock — #5 merely leaves the fallback untouched, and #6 later removes it and simplifies #5's seam. So the small symptom fix (#5) goes first and the heavier architectural change (#6) absorbs it.

Memory (#7, #8) is an independent domain, placed last as refinement/exploratory work. #7 before #8 keeps `memory/index.md` conflict-free (one updates the hub, the other adds a cross-link onto the updated hub).

## Cross-cutting cautions (not about order)

- **`config/schema.py` / `schema_version` serialize.** At least #3 (resume block) and #8 bump the schema; #1 and #7 may. Whoever lands second takes the next integer and updates `config.example.yaml` + `config_writer` + the loader. Do not develop two bumps on parallel branches — land them sequentially as in this plan.
- **#6 (packaged-delivery-only) has an external prerequisite** — the `upgrade-flows` re-sync, which is not among these eight. Because the repo is greenfield and `worc install` already delivers all built-in flows into `.worc/`, #6 can ship standalone with the "clear error" path from its Open questions rather than blocking on `upgrade-flows`. Record this as a conscious choice when implementing.
- **#4 (preserve-history) is the one true parallelization candidate** — it touches neither `engine.py`, `_adapter_base`, nor `providers/`. It is kept sequential here only for the shared `evaluator.py` file (different region from #2).

## Living log — update after each implemented ADR

This file is meant to be kept alive as the eight items land. **After finishing each ADR, before moving to the next, append the details a later iteration needs to know** — because the ADRs share hot files and seams, and later items were shaped assuming the earlier ones. For each completed item record: the branch / commit, the final `schema_version` it took (if it bumped), the exact seams/functions it changed (so the next ADR editing the same file knows what it now looks like), any decision made during implementation that diverged from or resolved an Open question in the ADR, and anything that changes the assumptions of a still-pending item (e.g. "grant-cycles now sits below both guards in `_charge_rework`"). Update the item's row in the Status tracking table in the same edit. Keep entries short and factual — the goal is that iteration N+1 opens this file and immediately sees what iterations 1..N left behind.

### Completed-ADR notes

#### 2 — no-work-agent-run-is-infra (implemented 2026-07-11)

- **No schema bump** (the stall state is transient on the engine; N=2 is a constant).
- **`_adapter_base` finalize seam**: a new generic block sits **after** the rate-limit block (`if not parsed.succeeded and parsed.rate_limited:`) and **before** `if parsed.succeeded:`, calling module-level `_produced_no_work(parsed)` (normalized fields only) and raising `ProviderError(AGENT_NO_PROGRESS)`. Any later ADR adding a signature keeps the "specific before generic" order — new specific signatures go **above** the no-work block.
- **`providers/base.py`**: `ErrorClass.AGENT_NO_PROGRESS` added to `FALLBACK_ELIGIBLE` only (not `PARK_ELIGIBLE`/`TRANSIENT_RETRYABLE`). Message in `errors.py:_MESSAGES` (the `test_errors.py` all-members loop enforces it).
- **`_charge_rework` region ([engine.py](../../src/wastech_orchestrator/core/flow/engine.py))** — **important for #3 (grant-cycles)**: #1 (rate-limit) added **no** code here (a parked `RATE_LIMITED` never reaches the charge point), so **#2 is the first actual code in this region**. The new shape: in `run()`, the rework branch now calls `self._check_stall(edge.loop)` **before** `self._charge_rework(edge)` and returns the first non-`None` `_Stuck`. `_check_stall` is a sibling method using transient per-loop state (`self._stall_fp`, `self._stall_streak`) fed by an injected `diff_fingerprint: DiffFingerprint | None` callable (threaded through `drive_flow`; `None` ⇒ inert), and `_reset_loops_at` now also clears that per-loop stall state on a forward edge. #3 (grant-cycles) must layer its fresh-budget logic **below** this stall check so a grant cannot bypass the no-effective-work guard.
- **Terminals**: a no-work boundary raise → non-evaluator node `_fail` (FAILED), evaluator node degrades to `MANUAL_ACTION_REQUIRED` (unchanged) now with an empty-diff annotation on the reason; a fix-loop stall → the existing stuck → `MANUAL_ACTION_REQUIRED` path with `limit_name="no_file_change"`.
- **Follow-up left open**: probe the **real** codex CLI's usage/`output_tokens` shape (the shared predicate degrades conservatively — no-fire → `task_failure` — if the real key differs).

## Status tracking

| Order | ADR                                  | Status                   |
| ----- | ------------------------------------ | ------------------------ |
| 1     | reliable-rate-limit-handling         | proposed                 |
| 2     | no-work-agent-run-is-infra           | implemented (2026-07-11) |
| 3     | universal-resume-recovery            | proposed                 |
| 4     | preserve-node-run-artifact-history   | proposed                 |
| 5     | flow-validation-cli-command          | proposed                 |
| 6     | packaged-delivery-only               | proposed                 |
| 7     | memory-concepts-over-episodic-ledger | proposed                 |
| 8     | agent-native-memory-opt-in           | proposed                 |
