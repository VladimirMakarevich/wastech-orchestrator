# 03. Comparison matrix storage / retrieval / update architectures

[Previous](./02-landscape-map-of-modern-approaches.md) | [Next](./04-recommended-architecture-specifically-for-wastech-orchestrator.md)

### 3.1 Storage shape matrix

| Shape | Сильные стороны | Слабые стороны | Лучший fit | Вердикт для WORC |
| --- | --- | --- | --- | --- |
| Plain files (`md`, `json`, `jsonl`) | human-readable, auditable, easy diff/review, natural fit for path-based prompt injection | слабее query/update semantics, merge/dedup сложнее, no indexes | small curated memory, stable rules, append-only episodic logs | Лучший V1 |
| SQLite | atomic merges, indexes, FTS, easier dedup, validation queries, scalable entity store | хуже human editing, schema evolution, more code | normalized entity/fact tables, larger corpora, retrieval services | Лучший V2, но отдельный DB |
| Vector store / embeddings | semantic similarity across many summaries/tasks | weak provenance, harder stale detection, bad at policy memory, extra infra/cost | large episodic corpus, fuzzy recall over task history | Не нужен в V1 |
| Knowledge graph / entity graph | multi-hop relation queries, impact analysis, hotspots traversal | maintenance-heavy, extraction quality critical, schema drift | large monorepos, dependency-heavy ecosystems | Не нужен в V1 |
| Hybrid | best of both worlds | complexity | mature memory subsystem | Целевой end-state |

### 3.2 Retrieval / update architecture matrix

| Architecture | Read path | Write path | Cost profile | Production evidence | Fit |
| --- | --- | --- | --- | --- | --- |
| Single giant memory file | always-loaded or always-referenced | append/update in place | cheapest to start, worst to age | weak | Reject |
| Files + stage-specific briefs | retrieve relevant entries, materialize small brief file | finalize + cleanup | low-medium | strong | Best V1 |
| SQLite + FTS + brief generation | query normalized memory, then synthesize brief | structured delta merge | medium | medium-high | Best V2 |
| Vector retrieval + rerank + brief | semantic search over summaries/facts | embed every update | medium-high | medium | Later only if metrics justify |
| Graph traversal + brief | traverse entity relations, then compose pack | graph extraction/update | high | mostly emerging | Later only |

### 3.3 Key trade-off summary

- `Files` выигрывают на auditability и compatibility с нынешней архитектурой `wastech-orchestrator`, где контекст передается агенту как path, а не как встроенный prompt blob. [src/wastech_orchestrator/core/prompts.py](../../src/wastech_orchestrator/core/prompts.py) [B31 Supervisor](../functional/blocks/B31-supervisor.md)
- `SQLite` выигрывает, как только появляются реальные потребности в dedup, FTS, validation scans и entity joins.
- `Vector store` хорош как complement к episodic history, но плох как source of truth для conventions/review rules.
- `Knowledge graph` хорошо работает как index over code/entities, но плохо как first storage layer для произвольно извлеченных LLM facts.

