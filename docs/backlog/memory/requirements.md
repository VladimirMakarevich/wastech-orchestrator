# Requirements

Status: **draft — to refine** Date: 2026-06-28 — [task hub](index.md)

Requirements for the V1 build of the memory subsystem. Each item is tagged firm (decided) or **[refine]** (to be locked collaboratively — tracked in [questions.md](questions.md)). Rationale lives in [design.md](design.md) and [research/memory-architecture-blueprint.md](research/memory-architecture-blueprint.md).

## Functional requirements (FR)

- **FR1 — Persistent cross-task store.** Memory persists under `.worc/memory/` across independent runs on the same repo. It is **repo-scoped by location** (one `.worc/memory/` per working copy, alongside `state.db` / `flows/` / `config.yaml`) and is **local, gitignored orchestrator state** — task-independent, never committed, never in a PR, and **not shared across clones / machines / team members in V1** (a deliberate boundary tied to C1 no-secrets and C5 single-repo). **[firm]**
- **FR2 — Three tiers, all in V1.** Short-term episodic, long-term (semantic / procedural / reviewer / failure), and entity cards — all three are in V1 scope. They share the same seams (write at finalize, read as packets, one `MemoryService`) and land **staged** to isolate risk: (1) long-term lessons end-to-end first (proves the write→read→curate loop), (2) short-term episodic (cheap append + TTL), (3) entity cards last (most complex — needs `DerivedIndex` path/symbol existence checks for staleness, Q2). **[firm]**
- **FR3 — Write at task close (background, zero extra LLM).** Memory is written once per task at task close, through one deterministic funnel (`MemoryService`) fed by two sources: on **success** (publish) the supervisor emits a structured `candidate_memory_delta` in its **existing** finalize turn (no additional LLM call); on **terminal failure / manual** there is no supervisor turn, so a short-term **failure** record is built **deterministically** from the failure artifacts (no LLM at all) and is never promoted to long-term. The write is **best-effort** — it never blocks publish/close. **[firm]** (Verified against code: success rides `_engine_finalize` → `supervisor.finalize()`; failure/manual close via `_fail` / `_go_terminal` has no supervisor turn — see [design.md](design.md) §2/§9.)
- **FR4 — Read as per-node packets, node-driven by `{memory_path}`.** Memory reaches an agent only as a small, curated, per-node packet file passed by path — never the memory root. Which nodes get a packet is **fully node-driven and never hardcoded**: PacketBuilder builds one for **any** node whose (operator-editable) role prompt references `{memory_path}`, and skips the rest — so any node, including custom operator nodes, can opt into memory with no Core change. The packaged role prompts reference `{memory_path}` in `planning` / `implementation` / `review` / `fixing` by default, but that is editable configuration, not a Core constraint. A node with no relevant memory gets a minimal/empty packet, never a fabricated one. **[firm]**
- **FR5 — Deterministic management.** Validation, trust assignment, promotion, dedup, conflict handling, retrieval ranking, and cleanup are deterministic, inspectable, and **unit-testable without a model** (no fake-CLI). The model's **only** involvement in V1 is the success-path `candidate_memory_delta`, riding the supervisor's existing finalize turn; the failure-path delta and everything else are model-free. (The one future place a model/embedding could enter retrieval is the optional V3 semantic rerank — bounded, over the deterministic filter.) **[firm]**
- **FR6 — Curation surfaces.** Two triggers over one funnel (`MemoryService`), both audited and snapshotted: (a) an operator CLI **`worc memory show | validate | compact | restore`** (each with `--dry-run` where it mutates), and (b) an automatic **bounded `CleanupJob`** in the `watch_loop` idle gap. Hand-editing the plain `md` / `jsonl` files is a first-class operator path (a `worc memory add/edit` for human-curated entries is a possible V1.x). External cron over the CLI is the operator's option, not a built feature. **[firm]** (cadence/budget of the idle job: Q1).
- **FR7 — Global enable/disable switch.** A single switch in the **global `config.yaml`** (the `enabled` flag of the `MemoryConfig` block, e.g. `memory.enabled`) turns the entire subsystem on or off for the whole orchestrator. When disabled there are no `.worc/memory/` reads or writes, no `memory_path` injected into prompts, no `worc memory` side effects, and no background cleanup — behavior is exactly today's. It is a global switch (not a per-task toggle in V1), and the default is chosen so an unconfigured or pre-existing project behaves as today. **[firm]** (exact default value + full disabled-state guarantees: [questions.md](questions.md) Q10).
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
