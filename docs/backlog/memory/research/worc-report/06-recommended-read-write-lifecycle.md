# 06. Recommended read/write lifecycle

[Previous](./05-recommended-memory-tiers.md) | [Next](./07-safety-model.md)

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
