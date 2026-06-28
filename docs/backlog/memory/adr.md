# ADR-0001 — Persistent memory subsystem (V1)

Status: **accepted** (2026-06-29) Date: 2026-06-29 Owner: Vladimir Makarevich

This ADR ratifies the V1 architecture for a persistent, repo-scoped memory subsystem in `wastech-orchestrator`: a **files-first, supervisor-distilled, deterministically-managed, evidence-backed** memory layer under `.worc/memory/` that lets the orchestrator stop re-paying for repo-specific lessons across independent runs — without becoming an opaque, rotting, or attackable context dump. It is the decision capstone for the [memory task](index.md); the detailed contract lives in [requirements.md](requirements.md) and [design.md](design.md), and the full rationale + evidence in [research/memory-architecture-blueprint.md](research/memory-architecture-blueprint.md). This document records _what was decided and why_; it does not restate the detail.

## Context

Today each task is an island. Nothing learned survives into the next task against the same repo, which recurs three costs on every run: **repo re-discovery** (re-deriving structure/conventions/commands from scratch), **lost lessons** (review findings, gotchas, rejected approaches rediscovered or re-violated), and **no entity knowledge** (no durable view of modules/files/tests/owners for cross-task reasoning). The cost compounds in the steady state we are building toward — autonomous `watch` over a queue on one repo. Memory bloat (duplicates, stale entries, contradictions, prompt noise) is the predictable failure mode _once memory exists_, and the empirical evidence is blunt that naive "bigger context / just add memory" makes agents worse — so curation and safety are part of the decision from the start, not later. Full framing: [problem.md](problem.md).

## Decision

Build the subsystem as the following locked set of decisions (each detailed in the linked source):

- **D1 — Files-first storage.** Canonical store is plain `md` / `json` / `jsonl` under `.worc/memory/`, **gitignored**, **task-independent**, repo-scoped by location, local (not shared across clones/machines in V1). **Never** in `state.db`; if a structured store is ever needed it is a separate `.worc/memory/memory.sqlite` (V2). ([requirements.md](requirements.md) FR1/C2, [design.md](design.md) §3)
- **D2 — Narrow supervisor + deterministic services (role split).** The supervisor only emits a structured `candidate_memory_delta`; all risky logic (validation, trust, promotion, dedup, conflict, retrieval ranking, cleanup) lives in deterministic, model-free, unit-testable services — `MemoryService`, `PacketBuilder`, `CleanupJob`, `DerivedIndex`. ([requirements.md](requirements.md) FR5/C4, [design.md](design.md) §1, [research/supervisor-role-split.md](research/supervisor-role-split.md))
- **D3 — Three tiers, staged.** Short-term episodic, long-term (semantic / procedural / reviewer / failure), and entity cards — all in V1, landed long-term → short-term → entity (entity last to isolate staleness risk). ([requirements.md](requirements.md) FR2, [design.md](design.md) §4)
- **D4 — Two-seam write at task close, zero extra LLM.** On **success** the supervisor emits the delta on its **existing** finalize turn (no added LLM call); on **terminal failure / manual** there is no supervisor turn, so a short-term/failure record is built **deterministically** (no LLM) and never promoted. Best-effort — never blocks publish/close. ([requirements.md](requirements.md) FR3, [design.md](design.md) §2/§9)
- **D5 — Node-driven per-node reads.** Memory reaches an agent only as a small, curated per-node packet file via `{memory_path}` — never the memory root. Which nodes get a packet is **fully node-driven, not hardcoded**: any node whose role prompt references `{memory_path}` gets one. Retrieval is precision-first / metadata-first with per-node caps. ([requirements.md](requirements.md) FR4/NFR4, [design.md](design.md) §6)
- **D6 — Safety day-one.** Redaction before every write (reuse `redact_text`/`redact_mapping`); deny-by-default allowlisted storage; trust levels + provenance enforced through write→retrieve→act; quarantine; append-only **hash-chained** `audit/log.jsonl` + best-effort `evaluations` marker; pre-batch snapshots; snapshot-level `restore`; bounded, opportunistic, work-gated cleanup that never runs under an active task. ([requirements.md](requirements.md) FR6/FR8/NFR5/NFR6, [design.md](design.md) §7)
- **D7 — Advisory, provider-neutral, switchable.** Memory shapes prompts only — it never routes, gates, enforces, or acts; enforcement stays in deterministic policies/hooks. The canonical store is our neutral format; providers only ever see the rendered packet. A single global `config.yaml` switch (`memory.enabled`) turns the whole subsystem on/off. ([requirements.md](requirements.md) NFR1/NFR7/FR7)
- **D8 — Evolve on measured need.** Roadmap V1 files → V2 SQLite/FTS → V3 embeddings → V4 entity graph, each unlocked only by a measured failure of the simpler design via the eval plan ("no vector/graph/SQLite infra without a measured recall/quality lift"). ([plan/index.md](plan/index.md) §Future, [acceptance-criteria.md](acceptance-criteria.md) §Outcome)

We choose this because it is the highest-quality, most reliable, most efficient shape that two independent deep-research efforts converged on, and it fits the orchestrator's seams (supervisor finalize, path-based prompts, idle watch gap) without violating its invariants. The cost of the rejected alternatives is below.

## Constraints respected

Hard invariants this decision honors (and does not override): **no secrets** in memory/logs/`state.db`/artifacts — even gitignored, because memory is read back into prompts that land in committed artifacts (C1); **not in `state.db`** (C2); **not an unbounded dump** (C3); **supervisor stays advisory** (C4); **single repo, single active task** in V1 (C5); and **cross-platform** Windows/Linux/macOS, incl. UTF-8 + explicit `\n` so audit content-hashes are stable (NFR8). These extend the repo invariants in [../../../CLAUDE.md](../../../CLAUDE.md), [../../../AGENTS.md](../../../AGENTS.md), [../../../.agents/rules/](../../../.agents/rules/).

## Alternatives considered

| Alternative | Why rejected |
| --- | --- |
| `state.db` as the memory store | Wrong ownership; couples memory to the state schema; un-hand-editable. |
| Vector / embeddings first | Weak provenance, poor exact-repo recall, extra infra — premature before the shape is known (→ V3). |
| Knowledge-graph first | High schema/maintenance burden before a proven multi-hop need (→ V4). |
| Provider-native memory as canonical | Machine-local / not portable; breaks the provider-neutral, supervisor-owned design. |
| Session-resume as memory | Vendor session is not a source of truth and is not provider-portable. |
| One giant `MEMORY.md` / append-only dump | Prompt bloat + rot; no provenance, promotion, or stale handling. |
| Supervisor-heavy (LLM builds packets / cleans) | Extra tokens, nondeterministic, hidden promotion logic, larger poisoning surface. |
| Aggressive autonomous "autodream" | Unjustified safety risk; replaced by a bounded, deterministic reconciliation job. |
| A dedicated per-task memory-synthesis turn | Taxes every task; we piggyback the supervisor's existing finalize turn. |

Full rationale and the "do nothing" baseline: [out-of-scope.md](out-of-scope.md) and blueprint §11.

## Consequences

**Gained:** less repo re-discovery and fewer re-violated conventions on known repos; better planning/review/fixing recall; an inspectable, auditable, reversible, bounded store; zero added LLM calls and negligible hot-path latency; provider portability; a clear, eval-gated path to scale.

**Given up (accepted trade-offs):** some semantic recall (no day-one embeddings) for provenance, simplicity, and safety; some query elegance (no day-one SQLite/graph) for inspectability and path-based compatibility; full autonomy — memory is advisory, never source-of-truth — for debuggability.

**New surface to own:** a new local artifact tree, four deterministic services, a `worc memory` CLI, a config block + schema bump, and a safety/eval burden (redaction, poisoning/staleness/rollback drills, replay baseline) that gates "done" ([definition-of-done.md](definition-of-done.md)).

## Open questions

None blocking. All V1 design questions (Q1–Q10) are **decided** — see [questions.md](questions.md) and the contracts in [design.md](design.md) §10. The only items left to tune are **provisional numeric defaults** (Q1 cleanup budget, Q5 packet caps), to be calibrated against the eval baseline; the _approach_ for both is locked. The one operator-facing judgment call — the `enabled` default (absent block → disabled; `worc install` → `true`) — is recorded in Q10 and easily flipped.

## Implementation notes

Seams (verified): supervisor `finalize()` ([../../../src/wastech_orchestrator/core/supervisor.py](../../../src/wastech_orchestrator/core/supervisor.py)) for the success delta; `_fail` / `_go_terminal` in [../../../src/wastech_orchestrator/core/orchestrator.py](../../../src/wastech_orchestrator/core/orchestrator.py) for the deterministic failure write; `ALLOWED_PROMPT_VARS` in [../../../src/wastech_orchestrator/core/prompts.py](../../../src/wastech_orchestrator/core/prompts.py) for `memory_path`; the `watch_loop` idle gap in [../../../src/wastech_orchestrator/cli.py](../../../src/wastech_orchestrator/cli.py) for cleanup; `redact_text`/`redact_mapping` ([../../../src/wastech_orchestrator/providers/redaction.py](../../../src/wastech_orchestrator/providers/redaction.py)) and the atomic-write pattern from [../../../src/wastech_orchestrator/core/hitl.py](../../../src/wastech_orchestrator/core/hitl.py); `MemoryConfig` in [../../../src/wastech_orchestrator/config/schema.py](../../../src/wastech_orchestrator/config/schema.py) + loader, with `CONFIG_SCHEMA_VERSION` bumped (currently 23). New deterministic modules: `MemoryService` / `PacketBuilder` / `CleanupJob` / `DerivedIndex`. Build order: [plan/index.md](plan/index.md) (Phase 01 Foundations → 02 Write → 03 Read → 04 Curation → 05 Safety & eval). Full seam list: [design.md](design.md) §9.
