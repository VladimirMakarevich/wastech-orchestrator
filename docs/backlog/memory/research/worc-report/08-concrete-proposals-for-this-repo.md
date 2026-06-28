# 08. Concrete proposals for this repo

[Previous](./07-safety-model.md) | [Next](./09-evolution-path.md)

### 8.1 Simplest useful first version

Рекомендую V1 exactly as follows:

1. Add `.worc/memory/` files-first store.
2. Add `memory_path` as a new allowlisted prompt variable.
3. Generate `logs/<task-id>/memory/<node-id>.md` briefs before planning/implementation/review/fixing.
4. At task close, produce a structured memory delta from supervisor finalization or an adjacent orchestrator-owned extractor step.
5. Persist:
   - `episodic/recent.jsonl`
   - `long_term/lessons.jsonl`
   - `long_term/reviewer_memory.jsonl`
   - `entities/entities.json`
   - `audit/mutations.jsonl`
6. Add `worc memory validate`, `worc memory compact`, `worc memory show`.
7. Add idle cleanup hook with strict budget.

В этом V1 deliberately:

- no vector DB;
- no graph DB;
- no cross-repo memory;
- no automatic edits to repo docs/prompts/skills;
- no direct provider write access to memory files.

### 8.2 Recommended sample schemas

#### Short-term episodic record

```json
{
  "id": "ep_2026-06-28_task-1234_01",
  "task_id": "task-1234",
  "created_at": "2026-06-28T18:00:00Z",
  "kind": "review_finding",
  "summary": "Review found that dependency lockfile must be updated together with config schema changes.",
  "entities": ["path:pyproject.toml", "path:src/wastech_orchestrator/config/schema.py"],
  "source_artifacts": ["summary_json", "review_path", "checks_path"],
  "trust": "internal_validated",
  "promotion_state": "candidate",
  "expires_at": "2026-08-12T00:00:00Z"
}
```

#### Long-term lesson

```json
{
  "id": "lt_000381",
  "type": "convention",
  "subject": "config-schema-changes",
  "statement": "Any config schema change must update docs and packaged config examples in the same change.",
  "rationale": "Docs-sync gate and config versioning otherwise break operator workflows.",
  "entities": ["path:src/wastech_orchestrator/config/schema.py", "path:docs/configuration.md"],
  "evidence": [
    {"kind": "repo_doc", "ref": "AGENTS.md"},
    {"kind": "task", "ref": "task-1177"}
  ],
  "first_seen_task_id": "task-1177",
  "last_validated_commit": "abc1234",
  "last_validated_at": "2026-06-28T18:00:00Z",
  "trust": "human_curated",
  "status": "active"
}
```

#### Entity memory record

```json
{
  "entity_id": "path:src/wastech_orchestrator/core/supervisor.py",
  "entity_type": "file",
  "canonical_name": "core/supervisor.py",
  "aliases": ["Supervisor", "__supervisor__ lineage"],
  "bounded_context": "supervision",
  "hotspots": [
    "best-effort finalize behavior",
    "durable own-session lineage"
  ],
  "linked_lessons": ["lt_000381"],
  "relationships": [
    {"type": "writes", "target": "artifact:summary.md"},
    {"type": "depends_on", "target": "path:src/wastech_orchestrator/state_store.py"}
  ],
  "last_seen_task_ids": ["task-1177", "task-1183"],
  "last_validated_commit": "abc1234",
  "status": "active"
}
```

### 8.3 Decision rules

#### `promote to long-term`

Promote if:

- trust in `{internal_validated, human_curated}`;
- evidence exists;
- entry seen in at least two tasks or explicitly marked stable by reviewer/operator;
- summary is short and repo-specific;
- validator finds no contradiction in current repo/docs.

#### `drop as stale`

Drop if:

- episodic and expired;
- entity target removed and no alias/remap found;
- contradicts current code/docs twice in a row;
- never retrieved or promoted over long retention window.

#### `merge duplicates`

Merge if:

- same normalized subject;
- overlapping entity set;
- semantic similarity high enough;
- evidence compatible, not contradictory.

Canonical merge policy:

- keep oldest stable id;
- union evidence and entities;
- prefer newest validated wording;
- append alias/source refs;
- log merge in audit.

### 8.4 What to defer

Отложить:

- separate SQLite/FTS store;
- embeddings over episodic history;
- entity graph traversal engine;
- automated promotion into repo `SKILL.md` / `AGENTS.md`;
- shared operator/user-level memory across repositories.

### 8.5 What not to build yet

Не строить пока:

- "memory as source of truth" instead of repo/docs;
- transcript warehouse;
- global forever-growing `memory.md`;
- memory writes on every stage completion;
- autonomous procedural memory that can alter security, routing, or publish behavior.

