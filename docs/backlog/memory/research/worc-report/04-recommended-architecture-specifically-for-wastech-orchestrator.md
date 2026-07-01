# 04. Recommended architecture specifically for `wastech-orchestrator`

[Previous](./03-comparison-matrix-storage-retrieval-update-architectures.md) | [Next](./05-recommended-memory-tiers.md)

### 4.1 Architectural principles

1. **Authoritative repo knowledge остается вне generated memory.** Источники истины уже есть: `AGENTS.md`, `.agents/rules/`, `docs/functional/`, код, role prompts. Memory не должна их дублировать большим prose; максимум - хранить short references и delta-facts. [docs/functional/index.md](../functional/index.md)

2. **Memory не равна repo index.** Repo map / symbol index отвечает на current structure. Persistent memory отвечает на cross-run lessons, hotspots, recurring failure patterns и entity annotations.

3. **Memory writes делает orchestrator-controlled component, не provider session напрямую.** Это важно и для security, и для auditability, и для соблюдения текущего инварианта, что supervisor advisory/read-only. [B31 Supervisor](../functional/blocks/B31-supervisor.md)

4. **Read path всегда bounded.** Агент получает не memory root, а `memory brief` file, сгенерированный для конкретного stage/task.

5. **Durable promotion требует evidence и validation.** Без cited evidence новая память живет максимум в short-term/quarantine.

### 4.2 Recommended component model

Рекомендованная схема:

1. `Authoritative layer`
   - `AGENTS.md`
   - `.agents/rules/`
   - `docs/functional/`
   - repo-native docs
   - role prompts / SKILL references

2. `Deterministic current-state index`
   - file tree
   - symbol/search index
   - changed-path analysis
   - optional later FTS/semantic code search

3. `Persistent memory layer` under `.worc/memory/`
   - `short-term episodic`
   - `long-term lessons`
   - `entity memory`
   - `audit + quarantine`

4. `Stage brief generator`
   - reads task + plan + diff + candidate entities
   - retrieves relevant memory
   - writes `logs/<task>/memory/<stage>.md`
   - agent receives only `memory_path`

5. `Memory validator / compactor`
   - file existence / symbol existence checks
   - dedup
   - stale detection
   - conflict handling
   - bounded idle cleanup

### 4.3 Recommended target layout for `.worc/memory/`

```text
.worc/memory/
  README.md
  memory_config.json
  long_term/
    conventions.md
    lessons.jsonl
    reviewer_memory.jsonl
    failure_memory.jsonl
  episodic/
    recent.jsonl
    runs/
      2026-06/
        task-1234.json
  entities/
    entities.json
    aliases.json
  audit/
    mutations.jsonl
    validations.jsonl
    cleanups.jsonl
  quarantine/
    pending/
    rejected/
    rolled_back/
  snapshots/
    2026-06-28T18-10-00Z/
```

Роли:

- `conventions.md`: small human-readable stable repo guidance, curated and low-churn;
- `lessons.jsonl`: atomic long-term lessons with provenance;
- `reviewer_memory.jsonl`: recurring review findings/checklists;
- `failure_memory.jsonl`: recurring failing check patterns, flaky areas, known fixes;
- `recent.jsonl`: append-only recent per-task deltas with TTL;
- `entities.json`: current entity-centric facts and links;
- `audit/*.jsonl`: every merge/prune/quarantine action;
- `snapshots/`: rollback anchor before batch cleanup.

### 4.4 Why this fits WORC specifically

`wastech-orchestrator` already has the right seams:

- supervisor finalize already synthesizes task-level summary at close and is best-effort; это естественная точка для memory extraction, если сама запись memory не блокирует publish. [src/wastech_orchestrator/core/supervisor.py](../../src/wastech_orchestrator/core/supervisor.py) [B31 Supervisor](../functional/blocks/B31-supervisor.md)
- prompt renderer already works через allowlisted path variables; туда естественно добавить `memory_path`. [src/wastech_orchestrator/core/prompts.py](../../src/wastech_orchestrator/core/prompts.py)
- orchestrator already has idle watch gaps; туда естественно встраивается bounded cleanup/autodream. [docs/backlog/archive/done/orchestrator-memory.md](../backlog/archive/done/orchestrator-memory.md)
- project already explicitly rejects using provider session as source of truth; значит persistent memory должна жить вне vendor session resume. [AGENTS.md](../../AGENTS.md) [docs/functional/index.md](../functional/index.md)
