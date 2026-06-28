# Open questions

Status: **living** Date: 2026-06-28 — [task hub](index.md)

Decisions still to lock. As we "потихоньку фиксируем", move an item to **Decided** with the decision + date rather than deleting it (keeps the trail). Each open item names what is needed to decide it.

## Open

- **Q1 — Autodream cadence & budget.** What fires the idle cleanup (every idle tick / after N tasks / a time threshold) and the exact per-pass scan/edit/wall-clock budget? Default: promotions-per-pass = 0. _Needs:_ a sense of memory growth rate (estimate or first live runs).
- **Q2 — Codebase-reconciliation source of truth.** How does cleanup/validate decide an entity or lesson is stale — path/symbol existence, rename remap, convention re-check — and at what confidence? _Needs:_ `DerivedIndex` capability decision (what it can cheaply answer).
- **Q3 — Promotion thresholds.** Concrete numbers: ≥2 tasks within what window? how is reviewer/operator "stable" signal expressed? _Needs:_ first real lessons; expect to tune once memory is live.
- **Q4 — CLI surface & scheduling.** Final `worc memory …` verbs (`show`/`validate`/`compact`|`defrag`/`restore`?), and whether scheduling is external cron vs. the autodream hook. _Needs:_ alignment with the console/scheduling backlog items.
- **Q5 — `memory_path` naming & caps.** Final variable name/semantics and the exact per-stage caps (lines/bullets/lessons/entities/episodic). _Needs:_ packet-size sanity check against real packets.
- **Q6 — Audit home.** Dedicated `audit/log.jsonl`, an `evaluations` marker row, or both? (Blueprint recommends both.) _Needs:_ confirm the `evaluations` row shape the supervisor already writes.
- **Q7 — Episodic detail home.** Where does resume/debug-grade detail live so short-term memory stays a distillation layer, not a transcript store? _Needs:_ confirm what task artifacts already persist for resume.
- **Q9 — Candidate-delta contract.** The exact structured schema the supervisor returns (fields, trust hints, evidence pointers) and how `finalize()` stays best-effort if the delta is malformed. _Needs:_ design sign-off before phase 02.
- **Q10 — Disabled-state guarantees.** Exactly what "disabled = today's behavior" covers (no dir created? no prompt var? no CLI?). _Needs:_ confirm with the regression test in AC-S4.

## Decided

- **Q8 — First-slice ordering.** ✅ Decided 2026-06-28 — all three tiers ship in V1, **staged**: long-term lessons end-to-end first, then short-term episodic, then entity cards (entity last to isolate staleness / `DerivedIndex` risk). See [requirements.md](requirements.md) FR2.
