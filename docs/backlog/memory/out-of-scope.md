# Out of scope

Status: **stable** Date: 2026-06-28 — [task hub](index.md)

What this task deliberately does **not** include. Rationale and the full rejected-alternatives table are in [research/memory-architecture-blueprint.md](research/memory-architecture-blueprint.md) §11.

## Not built in V1 (deferred to later, gated by the eval plan)

- **Vector store / embeddings** as a retrieval path → **V3**, and only after offline replay shows metadata-first retrieval measurably misses relevant facts. Even then, embeddings are a secondary recall layer over episodic summaries + normalized lessons, never the primary truth store, never over raw transcripts.
- **Separate SQLite + FTS store** → **V2**, only when file-based dedup/merge/validation-scan gets messy or slow (rough trigger: ≳500 durable lessons or ≳5000 episodic). When it lands it is `.worc/memory/memory.sqlite`, never a `state.db` schema bump.
- **Entity / knowledge graph with multi-hop traversal** → **V4**, only when tasks regularly need relational reasoning (issues/PRs ↔ code ↔ owners ↔ hotspots). V1 ships lightweight entity cards with explicit relations instead.
- **Cross-repo / shared operator-level memory.** V1 is single-repo, single-active-task.
- **Concurrent-task memory access.** Tied to the worktree-concurrency item; out until the single-active-task invariant is relaxed.

## Not in this subsystem at all (wrong shape / rejected)

- **Memory in `state.db`** — breaks "state.db is the state machine only"; un-hand-editable; schema bump per shape change.
- **Provider-native memory as the canonical layer** (Claude auto-memory, Codex durable guidance) — machine-local / instruction-layer, not portable; violates the provider-neutral, supervisor-owned design. (We may emit curated _views_ to providers, but the canonical store is ours.)
- **Vendor session-resume as memory** — the session is not a source of truth and is not transferable between providers.
- **One giant `MEMORY.md` / append-only dump** — prompt bloat + rot; no provenance, promotion, or stale handling.
- **Supervisor-heavy design** (LLM builds packets / decides promotions / runs cleanup) — extra tokens, nondeterministic, hidden logic, larger poisoning surface. The supervisor only emits a candidate delta.
- **Aggressive autonomous "autodream"** that freely writes/rewrites memory — replaced by a bounded, deterministic reconciliation job.
- **A dedicated per-task memory-synthesis LLM turn** — taxes every task; we piggyback the supervisor's existing finalize turn.
- **Automatic promotion of memory into repo `AGENTS.md` / role prompts / skills** — that is human-promoted only; memory stays advisory, never an enforcement/control channel.

## Adjacent, separately tracked (not this task)

- **Derived repo index** (repo map / symbol index / code search) is a separate rebuildable plane. `DerivedIndex` is referenced here because memory retrieval uses it for validation, but building out a full code-search/index capability is its own concern.
- **Disk-space cleanup of artifacts** — that is `worc logs clean` (see [../log-management.md](../log-management.md)), distinct from memory curation.
- **Operator approval/scheduling plumbing** (Telegram gates, cron) — reused if present, not built here.
