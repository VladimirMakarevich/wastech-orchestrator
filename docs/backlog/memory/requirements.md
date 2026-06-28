# Requirements

Status: **draft — to refine** Date: 2026-06-28 — [task hub](index.md)

Requirements for the V1 build of the memory subsystem. Each item is tagged firm (decided) or **[refine]** (to be locked collaboratively — tracked in [questions.md](questions.md)). Rationale lives in [design.md](design.md) and [research/memory-architecture-blueprint.md](research/memory-architecture-blueprint.md).

## Functional requirements (FR)

- **FR1 — Persistent cross-task store.** Memory persists under `.worc/memory/` (task-independent, gitignored) across independent runs on the same repo.
- **FR2 — Three tiers.** Short-term episodic, long-term (semantic / procedural / reviewer / failure), and entity cards. Long-term is the simplest end-to-end slice and may land first; all three are in V1 scope.
- **FR3 — Write at task finalization.** Memory is written once per task, at finalization, as a structured byproduct of the supervisor's existing summary turn — **zero new LLM calls** per task.
- **FR4 — Read as per-stage packets.** Each stage (planning / implementation / review / fixing) receives a small, curated packet file by path; agents never receive the memory root.
- **FR5 — Deterministic management.** Validation, trust assignment, promotion, dedup, conflict handling, retrieval ranking, and cleanup are deterministic and unit-testable; the LLM only proposes a candidate delta.
- **FR6 — Curation surfaces.** A `worc memory …` CLI (show / validate / compact|defrag / restore) and a bounded background cleanup job between tasks.
- **FR7 — Enable/disable.** The whole subsystem can be turned off by config; when off, behavior is exactly today's.
- **FR8 — Audit & rollback.** Every mutation is recorded; batch cleanups are snapshotted; a bad write is cheap to undo.

## Non-functional requirements (NFR)

- **NFR1 — Advisory only.** Memory shapes prompts; it never routes, gates, enforces, or acts. The Core still decides. **[firm — invariant]**
- **NFR2 — Source of truth is code + artifacts + checks.** Memory is distilled, provenanced knowledge about them, never a replacement. **[firm — invariant]**
- **NFR3 — Memory ≠ derived index.** Repo map / symbol index / code search are a separate, rebuildable plane, not durable memory. **[firm]**
- **NFR4 — Precision-first & bounded.** Retrieval is metadata-first with hard caps per packet; small and precise beats large and complete. **[refine]** exact caps.
- **NFR5 — Safety from day one.** Redaction + secret scan before every write; deny-by-default allowlisted storage; trust levels + provenance enforced through write→retrieve→act; quarantine; append-only audit; snapshots; rollback. **[firm]**
- **NFR6 — Bounded autonomy.** Background cleanup runs only when no task is active, within a wall-clock/edit budget, fail-closed, no network, never creates long-term lessons, never edits code/docs/skills. **[firm]** **[refine]** cadence + budget.
- **NFR7 — Provider-neutral.** Canonical store is our own format; providers get curated views only. **[firm]**
- **NFR8 — Cross-platform.** Windows / Linux / macOS: `pathlib` + `Path.as_posix()` for stored/compared paths; explicit encoding/newline discipline; no `os.kill`/`signal` assumptions for the idle/cleanup control. **[firm — invariant]**
- **NFR9 — No new LLM cost on the hot path.** No per-stage or hot-path memory LLM calls; packet building and cleanup are model-free. **[firm]**

## Constraints (hard invariants — must not be violated)

- **C1 — No secrets** in memory, logs, `state.db`, or artifacts — even though memory is gitignored, because agents read it back into prompts whose outputs land in committed artifacts.
- **C2 — Not in `state.db`.** Memory must not live in the state-machine store. If a DB is ever needed, it is a separate `.worc/memory/memory.sqlite` (V2), never a `state.db` schema bump.
- **C3 — Not an unbounded context dump.** Memory stays small and curated; raw transcripts / whole vendor sessions are forbidden.
- **C4 — Supervisor stays advisory.** It distills (emits a candidate delta); it does not gain promotion authority, packet assembly, cleanup ownership, or policy enforcement.
- **C5 — Single repo, single active task (V1).** Cross-repo memory and concurrent-task memory access are out of scope while those invariants hold.

These extend, never override, the repo invariants in [../../../CLAUDE.md](../../../CLAUDE.md), [../../../AGENTS.md](../../../AGENTS.md), and [../../../.agents/rules/](../../../.agents/rules/).
