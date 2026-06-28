# 11. Final recommendation with trade-offs and rejected alternatives

[Previous](./10-evaluation-plan.md) | [Next](./12-source-list-with-links.md)

### 11.1 Final recommendation

Для `wastech-orchestrator` строить memory subsystem надо как **files-first, evidence-backed, stage-brief-based hybrid**, а не как universal memory platform.

Recommended order:

1. V1: `.worc/memory/` files + stage briefs + finalize extraction + validation + audit.
2. V1.1: bounded idle cleanup/autodream.
3. V2: separate `memory.sqlite` with FTS if file queries become painful.
4. V3: embeddings for episodic recall only if replay eval shows lexical misses.
5. V4: richer entity graph only if repo complexity and query patterns demand it.

### 11.2 Explicit trade-offs

- You give up some semantic recall by not starting with embeddings, but you gain provenance, simplicity and safety.
- You give up some query elegance by not starting with SQLite/graph, but you stay compatible with current path-based architecture and easier operator inspection.
- You accept that memory is advisory, not source-of-truth, which lowers autonomy but strongly improves debuggability.

### 11.3 Rejected alternatives

Rejected for now:

- `state.db` as memory store: wrong ownership and schema coupling.
- vector-first memory: high cost, weak provenance, premature.
- knowledge-graph-first memory: high maintenance before proven need.
- session-resume-as-memory: violates architecture and provider portability.
- giant repo summary file: prompt bloat and staleness.
- automatic procedural skill synthesis: too risky until governance exists.

