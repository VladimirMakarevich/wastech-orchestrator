# Deep Research: persistent memory для repo-scoped coding agents

Дата: 2026-06-28

Фокус: практики 2024-2026 для LLM coding/repo agents, которые работают с одним и тем же репозиторием через много независимых запусков и сессий. Ниже сильные утверждения помечены источниками. Где вывод является моей интерпретацией, это указано явно.

## 1. Executive summary

Короткий вывод: для `wastech-orchestrator` не нужен ни vector-first memory, ни full knowledge-graph-first architecture. Лучший следующий шаг - гибрид из:

1. already-authoritative repo guidance (`AGENTS.md`, `docs/functional/`, `.agents/rules/`, role prompts, repo docs);
2. deterministic current-state repo index/repo map, который строится из кода и поиска, а не из LLM memory;
3. небольшой repo-scoped persistent memory под `.worc/memory/`, разделенной на `short-term episodic`, `long-term semantic/procedural`, `entity memory`, с provenance, validation, bounded cleanup и audit trail.

Это соответствует тому, как production systems реально сходятся к памяти:

- Anthropic Claude Code держит user/project/local memory как набор файлов и явно ограничивает startup load; для строгих правил рекомендует hooks, а не memory prose. Это сильный сигнал в пользу small curated files, а не огромного dump-файла. [Anthropic Claude Code Memory](https://docs.anthropic.com/en/docs/claude-code/memory)
- OpenAI Codex использует локальный `.codex/memories/` c `AGENTS.md`, auto-generated summaries, durable memory entries, recent inputs и supporting evidence; generated memory можно вообще отключать при external context. Это сильный паттерн для layered memory + external-context quarantine. [OpenAI Codex Memories](https://developers.openai.com/codex/memories)
- GitHub Copilot Memory хранит repo-specific facts/preferences с citations, review/delete controls и retention window 28 days. Это production-proven pattern для provenance + bounded retention + human control. [GitHub Copilot Memory](https://docs.github.com/en/copilot/concepts/agents/copilot-memory)
- OpenAI Agents SDK, LangGraph и Letta все разделяют always-visible memory и retrieved/archival memory, и рекомендуют обновлять существенную часть памяти в background, а не в hot path. [OpenAI Agents SDK Memory](https://openai.github.io/openai-agents-python/sandbox/memory/) [LangGraph Memory](https://docs.langchain.com/oss/python/concepts/memory) [Letta Memory Blocks](https://docs.letta.com/guides/agents/memory-blocks) [Letta Archival Memory](https://docs.letta.com/guides/core-concepts/memory/archival-memory)

Для этого проекта рекомендую:

- не хранить repo architecture как большой auto-generated prose summary; хранить pointers и stable facts, потому что authoritative truth уже есть в `docs/functional/` и коде;
- не смешивать deterministic repo index и generated memory: repo map/symbol index отвечает на "что сейчас в коде", memory отвечает на "что важно помнить между задачами";
- писать memory только на task-finalization и в bounded idle cleanup, не на каждом stage step;
- читать memory через stage-specific brief file (`memory_path`), а не скармливать агенту весь `.worc/memory/`;
- не класть memory в существующий `state.db`: это ломает его роль как state-machine store; если позже понадобится database shape, нужен отдельный `memory.sqlite` под `.worc/memory/`, не schema bump `state.db`. Это также соответствует уже существующему backlog-направлению проекта. [docs/backlog/orchestrator-memory.md](../backlog/orchestrator-memory.md)

Итоговая рекомендация:

- V1: files-first hybrid memory (`md` + `json` + `jsonl`) + deterministic retrieval + validation + audit.
- V2: отдельный SQLite/FTS index, когда файловые merge/query станут неудобны.
- V3: embeddings only after measured recall failures on lexical/entity retrieval.
- V4: entity graph only after реальная потребность в multi-hop repo reasoning, а не "на всякий случай".

## 2. Landscape map современных подходов

### 2.1 Что реально полезно для coding/repo agents

Полезные memory classes для coding agents:

- `procedural memory`: conventions, do/don't, review checklists, operational gotchas;
- `semantic memory`: стабильные факты о repo, bounded contexts, fragile areas, dependency behavior;
- `episodic memory`: что произошло в конкретных задачах, какие ошибки повторялись, какие fix patterns сработали;
- `entity-centric memory`: knowledge, привязанное к файлам, модулям, тестам, зависимостям, owners;
- `failure/reviewer memory`: recurring review findings, flaky tests, known bad migration patterns.

Это хорошо согласуется с framework docs вроде LangGraph, которые явно разделяют semantic / episodic / procedural memory. [LangGraph Memory](https://docs.langchain.com/oss/python/concepts/memory)

Но для coding/repo agents есть важная поправка: `repo map` и `symbol graph` полезны, но это не то же самое, что long-term memory. RepoGraph показывает, что repository graph серьезно улучшает retrieval and planning for code tasks, но это graph over current codebase, а не долговременная "память" о прошлых задачах. [RepoGraph paper](https://arxiv.org/html/2410.14684v1)

### 2.2 Production-proven patterns

| Pattern | Что делают | Evidence level | Вывод |
| --- | --- | --- | --- |
| Checked-in instruction files | `AGENTS.md`, `CLAUDE.md`, project knowledge files, repo instructions | Strong production | Лучший носитель для stable normative guidance; не заменяется generated memory. [Anthropic Claude Code Memory](https://docs.anthropic.com/en/docs/claude-code/memory) [OpenAI Codex Best Practices](https://developers.openai.com/codex/learn/best-practices) |
| Small layered local memory | always-loaded summary + on-demand topic files + recent history | Strong production | Нужно tiering и caps на load/read size. [Anthropic Claude Code Memory](https://docs.anthropic.com/en/docs/claude-code/memory) [OpenAI Codex Memories](https://developers.openai.com/codex/memories) |
| Repo-scoped extracted facts with citations | facts/preferences backed by cited repo sources and review/delete controls | Strong production | Provenance и controllable lifecycle обязательны. [GitHub Copilot Memory](https://docs.github.com/en/copilot/concepts/agents/copilot-memory) |
| Background memory formation/compaction | summaries, rollups, memory extraction вне hot path | Strong production/framework | Память не должна удлинять critical path каждой stage. [OpenAI Agents SDK Memory](https://openai.github.io/openai-agents-python/sandbox/memory/) [LangGraph Memory](https://docs.langchain.com/oss/python/concepts/memory) |
| Deterministic repo indexing | repository indexing, DeepWiki, code search, semantic search | Strong production | Не путать с memory; это separate retrieval plane. [GitHub Repository Indexing](https://docs.github.com/en/copilot/concepts/context/repository-indexing) [Devin Knowledge](https://docs.devin.ai/onboard-devin/knowledge-onboarding) |
| Human review on memory changes | review/delete/quarantine before durable promotion | Medium production | Полезно для safety-sensitive memory classes. [GitHub Copilot Memory](https://docs.github.com/en/copilot/concepts/agents/copilot-memory) [Augment Memory Review](https://www.augmentcode.com/blog/how-we-built-memory-review) |

### 2.3 Approaches, которые выглядят красиво, но production-evidence слабее

Слабее доказаны:

- `vector DB as primary memory` с первого дня;
- `knowledge graph as universal memory substrate`;
- fully autonomous memory writing from every stage;
- storing raw transcripts / whole sessions as reusable memory;
- auto-generated procedural memory, которая напрямую меняет execution behavior.

Почему:

- свежая evaluation-работа по agent-native memory systems показывает, что ни одна архитектура не доминирует универсально, а усиление памяти почти всегда платится latency / token / maintenance cost. [Agent-Native Memory Systems Evaluation](https://arxiv.org/html/2606.24775v1)
- MemoryArena показывает, что multi-session tasks с interdependent memory operations остаются трудными даже для сильных моделей; просто "добавить память" не решает reliability автоматически. [MemoryArena](https://arxiv.org/abs/2602.16313)
- MPBench и related poisoning work показывают, что memory itself становится attack surface, особенно если она high-impact и плохо валидируется. [MPBench](https://arxiv.org/html/2606.04329v1)

Интерпретация: knowledge graph и embeddings нужны не как ideology, а как response to measured failure modes у более простого дизайна.

## 3. Comparison matrix storage / retrieval / update architectures

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

## 4. Recommended architecture specifically for `wastech-orchestrator`

### 4.1 Architectural principles

1. **Authoritative repo knowledge остается вне generated memory.**
   Источники истины уже есть: `AGENTS.md`, `.agents/rules/`, `docs/functional/`, код, role prompts. Memory не должна их дублировать большим prose; максимум - хранить short references и delta-facts. [docs/functional/index.md](../functional/index.md)

2. **Memory не равна repo index.**
   Repo map / symbol index отвечает на current structure. Persistent memory отвечает на cross-run lessons, hotspots, recurring failure patterns и entity annotations.

3. **Memory writes делает orchestrator-controlled component, не provider session напрямую.**
   Это важно и для security, и для auditability, и для соблюдения текущего инварианта, что supervisor advisory/read-only. [B31 Supervisor](../functional/blocks/B31-supervisor.md)

4. **Read path всегда bounded.**
   Агент получает не memory root, а `memory brief` file, сгенерированный для конкретного stage/task.

5. **Durable promotion требует evidence и validation.**
   Без cited evidence новая память живет максимум в short-term/quarantine.

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
- orchestrator already has idle watch gaps; туда естественно встраивается bounded cleanup/autodream. [docs/backlog/orchestrator-memory.md](../backlog/orchestrator-memory.md)
- project already explicitly rejects using provider session as source of truth; значит persistent memory должна жить вне vendor session resume. [AGENTS.md](../../AGENTS.md) [docs/functional/index.md](../functional/index.md)

## 5. Recommended memory tiers

### 5.1 Tier overview

| Tier | Что хранить | Чего не хранить | TTL / aging | Retrieval default |
| --- | --- | --- | --- | --- |
| Short-term episodic | task outcomes, recent findings, temporary hypotheses, failed approaches, changed entities | raw transcripts, full diffs, secrets, external web facts as durable truth | 14-45 days by default | planning, implementation, fixing |
| Long-term semantic/procedural | stable conventions, reviewer lessons, recurring failure patterns, architectural gotchas with evidence | one-off task trivia, unstable branch facts, duplicate docs prose | no TTL, but periodic validation | planning, review, fixing |
| Entity memory | facts tied to files/modules/tests/dependencies/owners, plus links and hotspots | free-form giant summaries, unverifiable entity claims | validate on touch + periodic sweep | implementation, review, fixing |

### 5.2 What to store

#### Short-term episodic

Хранить:

- concise task summary;
- files/modules touched;
- review findings and whether they were fixed;
- failing checks and eventual fix pattern;
- operator/HITL decisions that matter for future similar tasks;
- rejected approaches, если они likely to recur soon.

Не хранить:

- full `stdout`, full review transcript, raw chat, full patch;
- details without future reuse value.

#### Long-term lessons

Хранить:

- stable repo conventions not already captured in `AGENTS.md`/docs;
- fragile areas ("changing X usually requires Y and Z");
- recurring review rules;
- dependency-specific gotchas;
- stable failure signatures and their canonical remedy;
- "how to navigate this repo" facts only if they are both durable and not already better documented elsewhere.

#### Entity memory

Хранить:

- entity id (`path:...`, `module:...`, `test:...`, `dep:...`);
- type and aliases;
- bounded-context label;
- hotspots / fragility flags;
- key relationships (`calls`, `depends_on`, `owned_by`, `validated_by_tests`);
- linked long-term facts;
- provenance pointers;
- last validated commit/time.

### 5.3 What not to store

Нельзя хранить:

- secrets or tokens;
- raw provider sessions or chain-of-thought-like reasoning traces;
- repo-wide prose restatement of docs that already exist;
- low-confidence facts without evidence;
- facts learned only from external web search unless separately code-validated;
- anything that can weaken security policy or routing decisions;
- agent-generated procedural instructions that become executable automatically.

Последний пункт особенно важен для WORC: procedural memory должна оставаться advisory, пока не была явно promoted человеком в `AGENTS.md`, role prompt или repo skill. Иначе memory становится stealth control plane.

### 5.4 Promotion rules

Рекомендованные rules for `promote to long-term`:

1. entry has at least one local evidence pointer to repo/docs/artifacts;
2. entry is not contradicted by current code/docs validation;
3. one of:
   - observed in `>=2` tasks within `60` days;
   - reviewer/HITL explicitly marked it as stable/reusable;
   - it prevented or explained a failed test/review in a way likely to recur;
   - it annotates a stable entity hotspot seen across commits/tasks;
4. entry summary can be stated in one small, repo-specific sentence;
5. source trust is `internal_validated`, not `external_only`.

### 5.5 Eviction / pruning rules

Рекомендованные rules for `drop as stale`:

- episodic record older than retention window and never promoted;
- entity fact references file/symbol that no longer exists and could not be remapped;
- lesson contradicted by two consecutive validations against current code/docs;
- failure pattern has had no hits for `N` releases/tasks and validation says obsolete;
- duplicate superseded by newer canonical entry with same subject/fact.

### 5.6 Conflict handling

Не удалять конфликтующие факты silently. Делать так:

1. new conflicting fact goes to `quarantine/pending/`;
2. validator checks code/docs evidence;
3. if old fact false -> old fact gets `superseded_by`, new fact promoted;
4. if unresolved -> both remain, but old one marked `disputed`, new one stays quarantined;
5. all steps logged in `audit/mutations.jsonl`.

## 6. Recommended read/write lifecycle

### 6.1 Write path at task finalization

Рекомендованный write path:

1. Inputs:
   - task file
   - plan artifact
   - final diff/stat
   - check results
   - review findings
   - summary artifact
   - HITL outcomes

2. Memory extractor:
   - produces structured delta, not prose blob
   - emits candidate lessons, failures, entities, trust flags, evidence pointers

3. Redaction:
   - run the same redaction discipline as other artifacts before persistence

4. Validator:
   - resolve referenced repo paths/entities
   - reject missing evidence
   - mark external-only facts

5. Merge:
   - append episodic record
   - update entity memory
   - promote to long-term only if rules pass

6. Audit:
   - record one mutation event with affected ids and decision result

Important:

- successful task close -> full write path allowed;
- failed/manual task -> short-term failure memory yes, long-term promotion rarely;
- tasks with heavy web/MCP/external context -> default `quarantine unless code-validated`, mirroring Codex's idea of disabling generated memory under external context. [OpenAI Codex Memories](https://developers.openai.com/codex/memories)

### 6.2 Read path by stage

#### Planning

Нужен `planning memory brief`, который содержит:

- top repo conventions relevant to task area;
- related prior tasks;
- architectural hotspots and owners if available;
- 3-5 most relevant long-term lessons;
- candidate entities/modules.

Planning memory should bias toward `long-term + related episodic`, не toward full entity dump.

#### Implementation

Нужен `implementation memory brief`:

- entity facts for files/modules likely to change;
- known coupling/dependency gotchas;
- prior failure patterns in same area;
- reviewer lessons tied to those entities.

Implementation should receive the richest entity memory, but still bounded.

#### Review

Нужен `review memory brief`:

- recurring review checklist for touched areas;
- previous review findings on same entities;
- dependency/security pitfalls;
- known "looks okay but breaks tests/docs" patterns.

Review memory наиболее ценно, когда оно specific и prescriptive.

#### Fixing

Нужен `fixing memory brief`:

- same failure signature seen before?;
- same test/module changed before?;
- canonical remedies;
- relevant review comments from this and prior tasks.

### 6.3 How to avoid prompt bloat

Для WORC рекомендую:

- никогда не передавать весь `.worc/memory/` в prompt;
- всегда материализовать stage-specific `memory brief` file;
- hard cap:
  - `<= 120` lines;
  - `<= 15` bullets;
  - `<= 3` long-term lessons;
  - `<= 5` entity records;
  - `<= 3` related episodic notes;
- brief contains links/paths to deeper evidence files if agent wants to inspect.

Это согласуется и с path-based prompt architecture WORC, и с evidence, что context files часто вредят, когда они большие или нерелевантные; ETH paper по context files for coding agents показывает, что такие файлы нередко снижают task success и увеличивают inference cost. [Context Files paper](https://arxiv.org/abs/2602.11988)

### 6.4 Path-to-memory vs extracted fragments

Для этого проекта правильное правило такое:

- **default**: pass path to generated, extracted stage brief;
- **not default**: pass path to raw memory store;
- **rare fallback**: allow path to a deeper evidence file when brief explicitly references it.

Интерпретация: current WORC model already prefers passing artifact paths. Значит memory should conform to that model, но the path should point to a curated brief, not to an unbounded directory.

### 6.5 Cleanup / defrag / autodream

Между задачами запускать bounded cleanup:

- validate up to `N` entries per idle tick;
- compact duplicate episodic items;
- expire old non-promoted episodic records;
- refresh entity `last_validated_*` for touched entities;
- produce cleanup audit row;
- never mutate code repo;
- never block next task longer than configured budget.

Рекомендация для safety:

- autodream может `validate`, `merge duplicates`, `demote`, `quarantine`, `delete expired episodic`;
- autodream **не должен** создавать новые long-term lessons из ничего и не должен auto-edit `AGENTS.md`/docs/skills.

## 7. Safety model

### 7.1 Redaction

Memory должна считаться artifact-class storage, не "безопасным локальным кэшем". Значит:

- redaction before disk write;
- no raw secrets in memory, SQLite, logs, artifacts;
- no full environment capture;
- no raw session ids.

Это полностью соответствует текущим repo invariants. [AGENTS.md](../../AGENTS.md) [security rules](../../.agents/rules/security.md)

### 7.2 Deny-by-default storage policy

Писать в durable memory можно только из allowlisted source classes:

- local task artifacts;
- review/check outputs;
- repo files/docs;
- operator/HITL inputs;
- deterministic repo analysis.

Нельзя durable-promote:

- arbitrary web search facts;
- MCP connector output without local validation;
- raw agent self-claims without evidence.

### 7.3 Poisoning resistance

Рекомендованный trust model:

- `internal_validated`: derived from local code/docs + validator passed;
- `internal_unvalidated`: local artifact exists, but fact not reconciled;
- `external_mixed`: includes external input, needs quarantine;
- `human_curated`: manually edited/approved.

Rules:

- only `internal_validated` and `human_curated` can become durable long-term;
- `external_mixed` can live in episodic store but not durable long-term by default;
- procedural memory requires `human_curated` or explicit operator approval.

MPBench and related work show that memory poisoning harms agents more when malicious info can enter high-impact memory channels and be retrieved later as trusted context. [MPBench](https://arxiv.org/html/2606.04329v1)

### 7.4 Audit trail

Every memory mutation should log:

- mutation id;
- timestamp;
- actor (`finalizer`, `cleanup`, `operator`);
- source artifact ids;
- affected memory ids;
- action (`append`, `promote`, `merge`, `quarantine`, `prune`, `rollback`);
- pre/post hashes;
- rationale.

### 7.5 Rollback / quarantine

Нужны:

- pre-cleanup snapshots;
- mutation log;
- quarantine folders;
- simple restore command.

Это важнее, чем сложный ML scoring. Bad memory update should be cheap to undo.

### 7.6 Bounded autonomy

Autonomous cleanup must have hard limits:

- max entries scanned per pass;
- max promotions per pass;
- max wall-clock budget;
- fail-closed if validator uncertain;
- no writes during active task.

## 8. Concrete proposals for this repo

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

## 9. Evolution path

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

## 10. Evaluation plan

### 10.1 Core metrics

| Category | Metrics |
| --- | --- |
| Quality | task success rate, first-pass test pass rate, review pass rate, fix-loop count, manual-action rate |
| Efficiency | tokens per successful task, wall-clock per task, number of retrieval reads, cleanup time |
| Retrieval quality | memory brief precision, memory brief recall on known-relevant facts, unused-brief rate |
| Memory quality | promotion precision, duplicate rate, stale fact rate, contradiction rate, quarantine rate |
| Safety | secret leak count, poisoned-write acceptance rate, rollback frequency, external-only promotion count |
| Maintainability | file/db growth, cleanup churn, operator edits needed, audit restore success |

### 10.2 Recommended experiments

1. **Replay benchmark**
   Run historical tasks with:
   - no memory
   - long-term only
   - long-term + episodic
   - long-term + episodic + entity memory

2. **Read-path ablation**
   Compare:
   - no memory
   - raw memory root path
   - stage brief path

3. **Write-path ablation**
   Compare:
   - finalize-only writes
   - step-level writes
   - finalize-only + cleanup compaction

4. **Retrieval ablation**
   Compare:
   - lexical/entity retrieval
   - lexical/entity + FTS
   - lexical/entity + embeddings

5. **Safety eval**
   Inject:
   - secret-like strings in artifacts
   - malicious external hints
   - stale file references
   - contradictory memory entries

6. **Long-horizon eval**
   Use sequential task batches over same repo to measure whether memory helps over time instead of only one task.

STATE-Bench is especially useful conceptually because it breaks agent memory into fundamental operations like update, locate, preserve and use across stateful workflows. [STATE-Bench](https://opensource.microsoft.com/blog/2026/05/19/introducing-state-bench-a-benchmark-for-ai-agent-memory/)

### 10.3 Success criteria

Разумные initial success criteria for WORC:

- `>= 10%` reduction in tokens or wall-clock for repeated-repo tasks;
- `>= 10%` improvement in first-pass review/test success on repeated hotspots;
- stale contradiction rate `< 5%`;
- secret leak rate `0`;
- external-only long-term promotions `0`;
- cleanup overhead small enough to stay outside critical path.

### 10.4 Failure indicators

Красные флаги:

- memory brief often ignored or irrelevant;
- memory grows faster than cleanup can control;
- many promotions later rolled back;
- agents start following stale rules more often than without memory;
- operator trust drops because memory becomes opaque or noisy;
- vector/graph infra added without measurable recall or quality lift.

## 11. Final recommendation with trade-offs and rejected alternatives

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

## 12. Source list with links

### Production / official docs

1. Anthropic, **Claude Code memory**  
   https://docs.anthropic.com/en/docs/claude-code/memory

2. OpenAI, **Codex memories**  
   https://developers.openai.com/codex/memories

3. OpenAI, **Codex best practices / AGENTS.md guidance**  
   https://developers.openai.com/codex/learn/best-practices

4. GitHub, **Copilot Memory**  
   https://docs.github.com/en/copilot/concepts/agents/copilot-memory

5. GitHub, **Repository indexing**  
   https://docs.github.com/en/copilot/concepts/context/repository-indexing

6. OpenAI Agents SDK, **Memory sandbox / summaries and rollups**  
   https://openai.github.io/openai-agents-python/sandbox/memory/

7. LangGraph, **Memory overview**  
   https://docs.langchain.com/oss/python/concepts/memory

8. Letta, **Memory blocks**  
   https://docs.letta.com/guides/agents/memory-blocks

9. Letta, **Archival memory**  
   https://docs.letta.com/guides/core-concepts/memory/archival-memory

10. Devin, **Knowledge onboarding / Repo Knowledge / DeepWiki**  
    https://docs.devin.ai/onboard-devin/knowledge-onboarding

11. Augment Code, **Memory Review**  
    https://www.augmentcode.com/blog/how-we-built-memory-review

12. Augment Code, **Context Lineage**  
    https://www.augmentcode.com/blog/announcing-context-lineage

### Research / evaluation

13. RepoGraph, **Enhancing AI Software Engineering with Repository-Level Code Graphs**  
    https://arxiv.org/html/2410.14684v1

14. ETH Zurich et al., **Context Files and Coding Agents**  
    https://arxiv.org/abs/2602.11988

15. MemoryArena, **A Benchmark for Long-Term Memory and Inter-Session Consistency in Multi-Session Agentic Tasks**  
    https://arxiv.org/abs/2602.16313

16. Microsoft Open Source, **STATE-Bench: a benchmark for AI agent memory**  
    https://opensource.microsoft.com/blog/2026/05/19/introducing-state-bench-a-benchmark-for-ai-agent-memory/

17. MPBench, **Benchmarking Memory Poisoning Attacks Against Language Agents**  
    https://arxiv.org/html/2606.04329v1

18. **Agent-Native Memory Systems Evaluation**  
    https://arxiv.org/html/2606.24775v1

### Repo-specific current context

19. Existing project direction: [docs/backlog/orchestrator-memory.md](../backlog/orchestrator-memory.md)
20. Current supervisor contract: [docs/functional/blocks/B31-supervisor.md](../functional/blocks/B31-supervisor.md)
21. Current prompt path-variable model: [src/wastech_orchestrator/core/prompts.py](../../src/wastech_orchestrator/core/prompts.py)
22. Repo invariants and security rules: [AGENTS.md](../../AGENTS.md), [.agents/rules/security.md](../../.agents/rules/security.md), [docs/functional/index.md](../functional/index.md)

## Bottom line

Если свести все к одному решению: для WORC memory subsystem должна быть **маленькой, repo-scoped, evidence-backed, file-first и bounded**, а ее главная задача - не "знать все о репозитории", а **не терять дорого найденные repo-specific lessons между независимыми runs**, не превращаясь при этом в непрозрачный мусорный слой.
