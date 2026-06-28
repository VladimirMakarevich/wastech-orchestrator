# 09. Evolution path

[Previous](./08-concrete-proposals-for-this-repo.md) | [Next](./10-evaluation-plan.md)

### 9.1 When files are enough

Files are enough when:

- single active task per repo;
- `< 500` durable lessons/facts;
- `< 5,000` episodic records;
- retrieval mostly driven by task text + entity hits + changed files;
- compaction can finish fast enough in idle time.

### 9.2 When SQLite becomes justified

SQLite becomes justified when:

- file-based dedup/merge code gets messy;
- you need indexed entity joins or FTS over thousands of lessons;
- validation scans across whole memory become slow;
- audit/rollback queries need structured access.

If that happens, use:

- separate `.worc/memory/memory.sqlite`;
- `FTS5` for long-term lessons and episodic summaries;
- `json`/`jsonl` retained for snapshots/export/human inspection.

Do **not** overload `state.db`.

### 9.3 When embeddings/vector retrieval become justified

Embeddings become justified only when:

- lexical/entity retrieval misses known relevant prior tasks;
- corpus of episodic summaries large enough that keyword retrieval is weak;
- you can measure recall lift on replay tasks;
- every embedded item still has stable provenance and stale-handling.

Good initial scope for embeddings:

- only episodic summaries and normalized lessons;
- never raw transcripts;
- retrieved results must still pass rerank/validator before entering brief.

### 9.4 When entity graph becomes justified

Entity graph becomes justified when:

- tasks regularly require multi-hop impact analysis;
- repo grows toward monorepo/service graph complexity;
- you need queries like "if module X changes, what tests/configs/owners/history matter?" often enough;
- deterministic code index can already produce a decent relation graph.

Until then, `entities.json` plus links is enough.
