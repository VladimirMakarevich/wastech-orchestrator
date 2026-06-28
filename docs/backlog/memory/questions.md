# Open questions

Status: **living** Date: 2026-06-28 — [task hub](index.md)

Decisions still to lock. As we "потихоньку фиксируем", move an item to **Decided** with the decision + date rather than deleting it (keeps the trail). Each open item names what is needed to decide it.

## Open

- **Q1 — Idle-cleanup cadence & budget (exact integers).** Approach is locked (NFR6: opportunistic + work-gated; `min_interval` + `max_scanned` / `max_edits` / `max_wall_clock`; `promotions_per_pass = 0`). Remaining: the exact default integers. _Needs:_ a sense of memory growth rate (estimate or first live runs).
- **Q2 — Codebase-reconciliation source of truth.** How does cleanup/validate decide an entity or lesson is stale — path/symbol existence, rename remap, convention re-check — and at what confidence? _Needs:_ `DerivedIndex` capability decision (what it can cheaply answer).
- **Q3 — Promotion thresholds.** Concrete numbers: ≥2 tasks within what window? how is reviewer/operator "stable" signal expressed? _Needs:_ first real lessons; expect to tune once memory is live.
- **Q5 — Packet caps (exact integers) & `memory_path` name.** Cap _approach_ is locked (NFR4: per-node, item-counts + char backstop). Remaining: the exact per-node default integers (tune against real packets) and confirming `memory_path` as the final variable name. _Needs:_ packet-size sanity check on real packets.
- **Q7 — Episodic detail home.** Where does resume/debug-grade detail live so short-term memory stays a distillation layer, not a transcript store? _Needs:_ confirm what task artifacts already persist for resume.
- **Q9 — Candidate-delta contract.** The exact structured schema the supervisor returns (fields, trust hints, evidence pointers) and how `finalize()` stays best-effort if the delta is malformed. _Needs:_ design sign-off before phase 02.
- **Q10 — Disabled-state guarantees.** Exactly what "disabled = today's behavior" covers (no dir created? no prompt var? no CLI?). _Needs:_ confirm with the regression test in AC-S4.

## Decided

- **Q8 — First-slice ordering.** ✅ Decided 2026-06-28 — all three tiers ship in V1, **staged**: long-term lessons end-to-end first, then short-term episodic, then entity cards (entity last to isolate staleness / `DerivedIndex` risk). See [requirements.md](requirements.md) FR2.
- **Q4 — CLI surface & scheduling.** ✅ Decided 2026-06-28 — verbs `worc memory show | validate | compact | restore` (no `defrag` alias; hand-editing the plain files + a possible V1.x `add/edit` cover authoring). Scheduling: built-in bounded idle-gap `CleanupJob`; external cron over the CLI is operator-optional, not built. Cadence/budget of the idle job stays open in Q1. See [requirements.md](requirements.md) FR6.
- **Q6 — Audit home.** ✅ Decided 2026-06-29 — **both**: a primary append-only, hash-chained `audit/log.jsonl`, plus a best-effort `evaluations` marker row in the orchestrator's existing decision trail. Remaining impl detail (phase 02): the exact `evaluations` row shape. See [requirements.md](requirements.md) FR8.
