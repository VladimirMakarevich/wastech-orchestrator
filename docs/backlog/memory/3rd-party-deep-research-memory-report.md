# Persistent memory for LLM coding agents in `wastech-orchestrator`

## Executive summary

Для `wastech-orchestrator` лучшая архитектура **в рамках текущих ограничений** — это не “универсальная AI memory”, а **repo-scoped, supervisor-owned, evidence-backed memory layer** с тремя практическими слоями: **bounded episodic short-term memory**, **semantic plus procedural long-term memory**, и **entity memory** по ключевым объектам репозитория. Каноническое хранилище в v1 стоит держать **локально в `.worc/memory/` в структурированных файлах `json/jsonl/md`**, а не начинать с внешнего vector DB или graph DB. При этом важно сразу отделить **память** от **derived repo context**: repo map, symbol index, code search и dependency graph лучше считать **производными индексами текущего состояния кода**, а не долгоживущей памятью как таковой. Это лучше соответствует тому, как production-инструменты реально решают задачу: маленький всегда-загружаемый индекс плюс более детальные topic/entity файлы, читаемые по требованию. Claude Code, например, хранит repo-scoped auto memory в plain markdown, загружает в стартовый контекст только краткий `MEMORY.md`, а подробные topic files читает on demand; Codex similarly делает ставку на small durable guidance через `AGENTS.md` и progressive disclosure через skills. citeturn19view1turn18view2turn18view4turn18view5

Ключевой вывод из свежих данных такой: **большие “context files” и агрессивное memory stuffing почти никогда не являются оптимумом**. В AGENTbench developer-written context files дали лишь **небольшой средний прирост около 4%**, тогда как LLM-generated context files имели **небольшой отрицательный эффект около 3%**, при этом context files увеличивали exploration/testing/reasoning cost более чем на **20%**. ContextBench отдельно показывает, что coding agents обычно **переизвлекают контекст**, frontier LLMs систематически **предпочитают recall precision**, а сложный scaffolding даёт лишь **ограниченный выигрыш** в retrieval quality. Для WORC это означает, что memory subsystem должна быть заточена под **precision-first retrieval**, stage-aware filtering и progressive disclosure, а не под максимальное накопление текста. citeturn10view0turn7search0turn23view2

Write path для WORC лучше строить так, чтобы **память обновлялась главным образом на task finalization supervisor-слоем**, а не в середине исполнения каждым агентом. Современные memory frameworks прямо различают hot-path writes и background writes: hot-path даёт немедленную доступность, но увеличивает latency и мешает основной работе агента; background formation снижает latency и лучше отделяет application logic от memory management. Для coding orchestration это почти идеально накладывается на ваш pipeline: тяжелая экстракция lessons learned, dedup, promotion и reconciliation должны происходить **после** implementation/testing/review/fixing, когда уже есть артефакты, результаты проверок и supervisor summary. citeturn28view0turn28view1turn28view2turn28view3

При этом долгоживущая память должна хранить **не task conclusions как “истину”**, а только **reusable repository knowledge**: conventions, fragile areas, build/test commands, stable architectural facts, recurring review findings, recurring failure modes, verified workflow lessons и entity cards. OpenAI в своём примере memory plus compaction специально делает reviewed artifact источником истины, а memory — слоем для reusable workflow lessons; case-specific conclusions предлагается **не класть** в долгосрочную память. Это очень хорошо совпадает с WORC, где source of truth уже определён как код репозитория, persisted orchestrator state и auditable artifacts. citeturn22view0turn22view1turn22view2

Более сильная альтернатива, если выйти за рамки минималистичного текущего направления, — **canonical SQLite store with generated file projections**, плюс derived FTS retrieval и, позже, entity graph. Но начинать с этого не стоит: research действительно показывает ценность code/repo graphs для multi-hop retrieval и связи issue/PR artifacts с code entities, однако это пока скорее **research-proven / production-adjacent**, а не основной proven pattern в общедоступных production writeups. KGCompass, Prometheus и MemCoder очень интересны и дают сильные сигналы о будущем направлении, но production tools today гораздо чаще используют small durable guidance, compact memory indices, hooks, code search и selective retrieval, чем полновесный long-lived knowledge graph. citeturn10view7turn12view0turn11view3turn19view0turn18view3turn18view5

## Landscape map of modern approaches

### What kinds of memory are actually useful for coding and repository agents

Современная литература по agent memory почти консенсусно различает **working**, **episodic**, **semantic** и **procedural memory**. Свежий survey по memory for autonomous LLM agents формализует память как цикл **write–manage–read** и отдельно описывает episodic memory как записи конкретных опытов, semantic memory как абстрагированные факты, а procedural memory как reusable skills and plans. Тот же survey прямо отмечает, что software engineering agents особенно сильно зависят от **procedural memory**, то есть от verified code patterns, workflows и architecture decisions. Аналогичные категории дают LangGraph/LangMem docs: semantic — facts/knowledge, episodic — past experiences, procedural — system behavior and instructions. citeturn13view0turn13view2turn13view3turn27view2turn27view3

Для repo agents в реальной инженерной практике полезны не все типы одинаково. Наиболее полезны:

| Memory type | Что реально даёт coding/repo agent | Практический статус |
|---|---|---|
| **Semantic memory** | repo conventions, stable architecture facts, critical commands, fragile areas, dependency gotchas | production-proven |
| **Procedural memory** | rollout/fix/review workflows, “как здесь обычно чинят X”, test and verification routines | production-proven if explicit; speculative if self-modifying |
| **Entity memory** | file/module/service/owner/hotspot cards с путями, связями и ссылками на evidence | production-proven in lightweight form; graph-heavy variants are research-adjacent |
| **Episodic memory** | summaries of prior tasks, review outcomes, failure traces, lessons from recent runs | useful, but must be bounded |
| **Task history as full transcript** | редко окупается; быстро раздувает retrieval и повышает staleness/poisoning risk | generally poor default |

Этот расклад хорошо подтверждается и со стороны production tooling. Claude Code разделяет **user-written instructions** (`CLAUDE.md`) и **auto memory** с learnings and patterns; она repo-scoped и shared across worktrees, но loaded в стартовый контекст только как краткий индекс. Codex similarly продвигает `AGENTS.md` как durable project guidance и skills как отдельный reusable procedural layer с progressive disclosure: сначала в контекст попадают лишь имена, descriptions и paths skills, а полные `SKILL.md` подгружаются только при релевантности. Это очень сильный сигнал, что в coding agents полезны не “все прошлые слова”, а **компактные, типизированные, selectively loaded artifacts**. citeturn19view0turn19view1turn18view2turn18view5turn18view1

### What is production-proven and what is still speculative

Если разделить рынок на **production-proven** и **research-promising**, картина довольно чёткая.

Production-proven patterns сегодня — это **small project guidance files**, **local audit-friendly memory artifacts**, **hooks/guardrails**, **code search and symbol retrieval**, **compaction/trimming**, и **selective reuse of workflow lessons**. Официальные docs OpenAI, Anthropic, GitHub и Sourcegraph сходятся именно на этих паттернах. Codex читает `AGENTS.md` перед запуском работы, поддерживает layered project guidance и использует skills с progressive disclosure. Claude Code имеет auto memory directory per repo with `MEMORY.md` plus topic files, plain markdown audit/editability и deterministic hooks. GitHub и Copilot продвигают repository/custom instructions и specialist agents для code review, explore, planning и task execution. Sourcegraph repeatedly emphasizes that outcome quality in coding agents определяется прежде всего тем, **какой контекст retrieved и как он ranked**, а не длиной prompt alone. citeturn18view1turn18view2turn19view1turn18view3turn26view2turn26view3turn23view0turn23view2

Research-promising patterns — это **repository knowledge graphs**, **historical-commit distillation**, **workflow memory induction** и **memory-enhanced long-horizon navigation**. KGCompass показывает, что graph, связывающий code entities и repo artifacts вроде issues/PRs, даёт точнее bug localization и path-guided repair; Prometheus показывает, что working memory plus lightweight repository graph может улучшать long-horizon navigation; MemCoder показывает сильный сигнал, что структура historical commits и human-validated solutions может быть ценным source of long-term repo memory; AWM показывает, что reusable workflow induction может очень заметно улучшать long-horizon task solving — пусть и в web/navigation, а не в code repositories. Всё это важно для WORC как ориентир на будущее, но пока это скорее **направление эволюции**, чем обязательный MVP. citeturn10view7turn12view0turn12view3turn11view0turn11view1turn16view0turn16view2

Отдельно важно, что evidence против “больше текста = лучше” уже достаточно сильный. AGENTbench нашёл только marginal upside у developer context files и small downside у LLM-generated ones, а ContextBench показывает систематическое over-retrieval. Даже long-context systems не отменяют задачу context engineering: Sourcegraph found quality gains from long context, but also notes linear time-to-first-token growth with context size, so long context — это не замена disciplined retrieval and memory shaping. citeturn10view0turn7search0turn23view3

## Comparison of storage, retrieval, and update architectures

Ниже — **моя инженерная оценка**, собранная на основе official docs и recent research: files как inspectable baseline опираются на Claude Code-style plain markdown memory; vector retrieval и structured stores — на survey and LangMem; graphs — на Prometheus and KGCompass; hybrid — на Mem0 and MemCoder patterns. citeturn19view1turn13view2turn27view2turn14view0turn10view7turn12view0turn11view3

### Comparison matrix

Легенда: **High / Medium / Low** — это suitability для WORC, а не абсолютная “хорошесть”.

| Variant | Usefulness for coding agents | Token efficiency | Retrieval precision | Staleness risk | Implementation complexity | Inspectability | Safety surface | Auditability | Portability | Fit for WORC now | Migration path |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **Plain files `md/json/jsonl`** | High | High | Medium | Medium | Low | **High** | Medium | **High** | **High** | **High** | Excellent |
| **SQLite only** | High | High | High | Medium | Medium | Medium | Medium | High | High | Medium-High | Excellent |
| **Vector store only** | Medium | Medium-High | Medium for fuzzy recall, Low for exact repo semantics | High | Medium | Low | Lower by default | Medium | Medium | Low | Weak alone |
| **Knowledge graph only** | Medium-High | High | High for relations/multi-hop | Medium | **High** | Low-Medium | Medium | Medium | Medium | Low in v1 | Expensive |
| **Hybrid files + structured index** | **High** | **High** | **High** | Medium | Medium | **High** | Medium-High | **High** | **High** | **Best near-term** | Excellent |
| **Hybrid structured store + embeddings + entity graph** | Very High | High | Very High | Medium | Very High | Medium | Medium | High | Medium | Low for v1, High later | Strong later |

Почему **vector-only** слаб как базовая memory architecture для repo agents: свежий survey прямо отмечает, что vector stores хорошо масштабируются и помогают similarity retrieval, но **теряют structured relationships** — можно хорошо отвечать на “что похоже”, но хуже на “что зависит от чего” или “какая review finding относится к этому модулю и этой стадии”. Для codebases это особенно болезненно, потому что repo reasoning часто завязан на paths, symbols, ownership, dependency chains и chronology. citeturn13view2

Почему **graph-only** тоже не лучший старт: графы действительно помогают repository-level repair и long-horizon navigation. KGCompass показывает пользу связи issues/PRs с files/functions/classes, а Prometheus — пользу unified repository graph plus working memory; более того, Prometheus reports lightweight graph construction on average around **1.99 seconds per instance** on SWE-bench Verified, что показывает практическую жизнеспособность lightweight schemas. Но graph-first architecture всё ещё дорога по schema design, reconciliation and operator tooling, особенно если у вас single-repo supervisor-owned system v1. citeturn10view7turn12view0

Почему **files plus structured index** — лучший компромисс для WORC: plain files already match strong production evidence on inspectability and auditability; SQLite or FTS sidecar later даст exact filtering, metadata joins и bounded search complexity; embeddings можно добавить не как canonical memory, а как retrieval accelerator для long-tail natural-language queries. Этот путь лучше всего соответствует и вашим ограничениям, и real-world provider portability: OpenAI продвигает AGENTS.md plus MCP as open conventions, Anthropic — CLAUDE.md plus repo-scoped memory; значит, WORC должен хранить canonical memory **в своём neutral format**, а провайдерам отдавать лишь curated views. citeturn30view0turn19view0turn19view1

## Recommended architecture for `wastech-orchestrator`

### The core design

Рекомендую следующую архитектуру для WORC:

**Canonical memory lives in `.worc/memory/` as structured files.**  
Это основной source of truth для supervisor-owned memory. Форматы: `json` / `jsonl` для canonical records, `md` для human-readable indices and packets. Это даёт inspectability, git-friendliness, low ops burden и provider-neutrality. Подход хорошо согласуется с тем, как Claude Code делает repo-scoped auto memory с `MEMORY.md` plus topic files, plain markdown editability и shared memory across worktrees. citeturn19view0turn19view1

**Derived current-state indices are separate from memory.**  
Сюда входят repo map, symbol index, code search index, optional dependency graph и, позже, embeddings. Их надо трактовать как **rebuildable caches of the current codebase**, а не как долгоживущую repo knowledge memory. Это уменьшает staleness risk и не смешивает “что было выучено из прошлых задач” с “что сейчас существует в дереве кода”. Такой разделитель — моя рекомендация, но он опирается на общую логику write–manage–read architectures и на то, что graph-based systems вроде Prometheus строят repository graph from the repo itself, а не из свободной исторической памяти. citeturn13view1turn12view0

**Retrieval is two-stage and metadata-first.**  
Сначала — deterministic filtering по stage, touched paths, symbols, bounded contexts, task type, recency, trust level и entity relations. Только потом — optional semantic rerank. Это directly motivated by ContextBench: coding agents tend to over-retrieve, and LLMs already bias toward recall over precision. WORC не должен усугублять этот паттерн “сырым similarity search everywhere”. citeturn7search0

**Supervisor owns writes, agents consume packets.**  
Supervisor, который already sees the whole task end-to-end, должен быть единственной сущностью, которая промотирует memory into long-term layers. Исполняющие agents получают не весь memory dump, а **memory packet files** по stage: planning packet, implementation packet, review packet, fixing packet. Это хорошо сочетается с вашим требованием “prefer file paths over giant inline dumps” и с production pattern progressive disclosure in Codex skills and Claude topic files. citeturn18view2turn19view1

**Long-term memory stores reusable repo knowledge, not raw task record.**  
Reviewed artifacts, task summaries, checks output и commits — это evidence. Memory — это distilled knowledge with provenance. OpenAI’s own memory-plus-compaction pattern very explicitly separates reviewed artifact as source of truth from memory as reusable workflow layer; для WORC стоит сделать то же самое. citeturn22view0turn22view1turn22view2

### Target `.worc/memory/` layout

Ниже — рекомендуемый **target layout** для v1. Это уже архитектурное предложение, а не прямой источник.

```text
.worc/memory/
  README.md
  manifest.json
  audit/
    log.jsonl
    snapshots/
  quarantine/
    entries.jsonl
    notes/
  short-term/
    recent_runs.jsonl
    runs/
      <task-id>/
        episode.json
        planning-summary.md
        implementation-summary.md
        review-summary.md
        fixing-summary.md
        artifacts.json
  long-term/
    index.md
    semantic.jsonl
    procedural.jsonl
    reviewer.jsonl
    failures.jsonl
  entities/
    index.md
    files/
      <path-hash>.json
    modules/
      <slug>.json
    contexts/
      <slug>.json
    dependencies/
      <slug>.json
    owners/
      <slug>.json
  derived/
    repo-map.json
    symbol-index.sqlite
    retrieval.sqlite
    embeddings/              # optional, later
  packets/
    planning/
      <task-id>.md
    implementation/
      <task-id>.md
    review/
      <task-id>.md
    fixing/
      <task-id>.md
```

Практический смысл этой структуры такой. `short-term/` хранит bounded episodic memory о последних задачах. `long-term/` хранит reusable facts, workflows, reviewer patterns и failure patterns. `entities/` хранит repo cards по file/module/context/dependency/owner/hotspot. `derived/` — rebuildable indexes. `packets/` — stage-specific curated context handed to agents. `audit/` и `quarantine/` нужны **с самого начала**, потому что свежая security research показывает, что long-term memory — это отдельная attack surface, и bad writes должны быть rollbackable and reviewable. citeturn24view3turn24view4turn24view5

### Recommended tier structure

#### Short-term memory

`short-term` в WORC лучше понимать не как “active conversation history”, а как **bounded episodic task memory across recent runs**. Туда стоит класть:

- короткий итог по каждой стадии;
- список touched paths and symbols;
- test/build/review outcomes;
- reviewer findings;
- unresolved questions and superseded assumptions;
- candidate promotions into long-term memory;
- pointers to artifacts and commits.

Не стоит класть туда полный transcript агентской сессии, полный shell log или большие diffs. Для продолжения работы и resumability нужны task artifacts and orchestrator state; для memory нужен лишь distillation layer. Иначе short-term быстро превратится в prompt bloat source, а не в useful memory. Этот вывод хорошо согласуется и с OpenAI compaction guidance, и с Anthropic’s long-running agent lessons that compaction alone is not enough when agents leave half-documented work behind. citeturn21view1turn25view0

#### Long-term memory

`long-term` должно содержать четыре practically useful вида записей:

| Kind | Что хранить | Когда это полезно |
|---|---|---|
| **semantic** | stable repo facts, commands, conventions, fragile integrations, architectural facts | planning, implementation, review |
| **procedural** | verified workflows, stage-specific checklists, “как здесь обычно делать X” | planning, fixing, review |
| **reviewer** | recurring review feedback patterns, common misses, “what reviewers care about here” | review, fixing |
| **failure** | repeated failure signatures, commands that often mislead, stale assumptions that reappear | implementation, fixing |

Это соответствует и recent memory taxonomies, и practical evidence. LangMem and survey literature separate facts, experiences and procedures; AWM shows value of reusable workflows; MemCoder shows value of deriving memory from historical commits and crystallized human-verified solutions; OpenAI’s cookbook shows reusable workflow lessons are a good long-term target while case-specific conclusions should remain in reviewed artifacts. citeturn27view2turn27view3turn16view0turn11view1turn22view2

#### Entity memory

`entity memory` для WORC — это **легковесная, inspectable альтернатива full repository graph на старте**. Каждая сущность — file, module, bounded context, dependency, owner, hotspot — получает карточку с:

- canonical name and aliases;
- paths and symbols;
- short summary;
- important relationships;
- risky change notes;
- relevant commands;
- owners/contacts if they are safe to store;
- links to supporting long-term memory records and artifacts.

Именно entity-centric memory чаще всего даёт лучший latency/precision trade-off для repo agents. Research on KGCompass and Prometheus suggests that relations among code entities and repository artifacts are genuinely useful; но в v1 это можно реализовать как explicit entity cards and lightweight relations, не как graph database. citeturn10view7turn12view0

### Example schemas

Ниже — **предлагаемые схемы**.

**Short-term episode**

```json
{
  "task_id": "task_2026_06_28_001",
  "repo": "wastech-orchestrator",
  "base_commit": "abc123",
  "head_commit": "def456",
  "task_type": "bugfix|feature|refactor|review-fix",
  "touched_paths": ["src/supervisor/memory.ts", "docs/memory.md"],
  "touched_symbols": ["MemorySupervisor", "promoteCandidate"],
  "stage_outcomes": {
    "planning": "done",
    "implementation": "done",
    "testing": "done_with_failures",
    "review": "done_with_findings",
    "fixing": "done"
  },
  "checks": [
    {"name": "unit", "status": "passed"},
    {"name": "lint", "status": "passed"}
  ],
  "review_findings": [
    {
      "category": "missing-regression-test",
      "severity": "medium",
      "paths": ["src/supervisor/memory.ts"]
    }
  ],
  "supervisor_summary_path": ".worc/tasks/task_2026_06_28_001/summary.md",
  "artifact_paths": [
    ".worc/tasks/task_2026_06_28_001/plan.md",
    ".worc/tasks/task_2026_06_28_001/review.md"
  ],
  "candidate_promotions": ["cand_01", "cand_02"]
}
```

**Long-term record**

```json
{
  "memory_id": "ltm_000142",
  "kind": "semantic|procedural|reviewer|failure",
  "title": "Reviewers expect regression tests for orchestration retry logic",
  "statement": "Changes under src/orchestrator/retry should include a regression test for resume semantics.",
  "scope": {
    "paths": ["src/orchestrator/", "tests/orchestrator/"],
    "symbols": ["RetryPolicy", "ResumeState"],
    "stages": ["implementation", "review", "fixing"]
  },
  "evidence": [
    {
      "type": "artifact",
      "path": ".worc/tasks/task_2026_06_28_001/review.md",
      "anchor": "finding-3"
    },
    {
      "type": "commit",
      "sha": "def456"
    }
  ],
  "trust_level": "review-verified",
  "confidence": "high",
  "first_seen_commit": "def456",
  "last_verified_commit": "def456",
  "status": "active",
  "supersedes": [],
  "usage_count": 0,
  "expiry_policy": {
    "mode": "revalidate_on_touch"
  }
}
```

**Entity card**

```json
{
  "entity_id": "module:supervisor-memory",
  "entity_type": "module",
  "name": "Supervisor memory subsystem",
  "aliases": ["memory supervisor", "repo memory"],
  "paths": ["src/supervisor/", "src/memory/"],
  "symbols": ["MemorySupervisor", "MemoryStore"],
  "summary": "Owns memory extraction, promotion, retrieval packets, and cleanup.",
  "relationships": [
    {"type": "depends_on", "entity_id": "module:task-artifacts"},
    {"type": "writes_to", "entity_id": "path:.worc/memory/"}
  ],
  "hotspot_score": 0.81,
  "commands": ["pnpm test memory", "pnpm lint src/supervisor"],
  "notes": [
    "Resume semantics are fragile when stage output and memory promotion disagree."
  ],
  "memory_refs": ["ltm_000142", "ltm_000151"]
}
```

### What to store and what not to store

Хранить стоит только то, что either **repeats**, **stays true**, or **saves rediscovery**:

- build, test, lint, migration and verification commands;
- stable conventions and local rules;
- architecture facts unlikely to change every week;
- fragile areas and integration gotchas;
- recurring reviewer expectations;
- recurring failure modes and anti-patterns;
- important entities and hotspots;
- links to prior tasks that are strong precedents.

Не стоит хранить:

- secrets or raw credentials;
- raw session transcripts as primary memory;
- full diffs and large logs;
- speculative hypotheses not backed by artifact evidence;
- case-specific conclusions as long-term truth;
- provider-native hidden memory as canonical source of truth;
- high-churn repo map snapshots that are better derived from code.

Этот boundary важен не только из-за token efficiency, но и из-за security. свежие poisoning papers и field reports показывают, что persistent memory can become an attack vector when untrusted or weakly grounded content is promoted into future-context authority. citeturn24view3turn24view4turn24view5

## Lifecycle, write path, read path, and safety model

### Recommended read and write lifecycle

**Planning stage.**  
На planning supervisor должен собирать `planning packet` из трёх источников, в таком порядке приоритета: current task artifacts and issue text; derived repo map/symbol hits for likely-in-scope paths; then a handful of relevant memory records. Planning packet должен содержать **краткий repo profile**, top entity cards, 3–7 high-value long-term records, и ссылки на deeper files. Это соответствует progressive disclosure patterns in Codex skills and Claude memory topic files, а также помогает не повторять ContextBench-style over-retrieval. citeturn18view2turn19view1turn7search0

**Implementation stage.**  
Implementation packet должен быть уже уже, чем planning packet. Агенту обычно нужны touched-path entity cards, precise commands, known fragile areas, and failure memories relevant to touched paths. На этом этапе наиболее важно взаимодействие memory с code search and symbol index: memory should bias where to look, but source of truth remains the live codebase. Sourcegraph’s context-engineering material and Cody retrieval work strongly support this emphasis on repo-aware retrieval rather than raw memory dump. citeturn23view1turn23view2

**Review stage.**  
Review packet должен извлекать **reviewer memory** and **failure memory** по touched paths and categories. Здесь memory often yields the largest marginal gain, потому что reviewer patterns and recurring misses are exactly the type of knowledge that rediscovery wastes time on. В production tools GitHub and Codex increasingly use specialized review/explore/task agents rather than one generic agent for everything; WORC should mimic that by stage-specific retrieval, even if provider is the same. citeturn26view2turn18view5

**Fixing stage.**  
Fixing packet должен быть самым причинно-ориентированным: конкретные review findings, nearest historical precedents, matching failure signatures, and exact verification commands. Здесь важна не общая repo memory, а **memory filtered by finding category, path, and stage**.

**Write at task finalization.**  
Основной write path должен запускаться **после** review/fixing and final summary. Supervisor extracts candidate memories from merged evidence: plan, implementation summary, checks, review findings, supervisor summary, and touched paths. Затем отдельный promotion pipeline делает type assignment, dedup, conflict detection, redaction, trust labeling and promotion into long-term/entity layers. LangChain/LangMem docs прямо подчеркивают trade-off hot-path versus background writes; для WORC background style is the right default. citeturn28view0turn28view1

**Cleanup and bounded autodream between tasks.**  
“Autodream” для WORC я бы не реализовывал как свободное autonomous memory writing. Лучший вариант — **bounded reconciliation job** между задачами, с жёстким budget и no network, который умеет:
- deduplicate near-duplicates;
- compact indices;
- revalidate commands or path existence read-only;
- mark stale entries;
- move suspicious entries to quarantine;
- regenerate small stage-specific indices.

Такой bounded background memory processing совпадает с “subconscious formation” style memory systems, но для WORC должен быть ещё более constrained из-за repo security и poisoning risk. citeturn28view2turn24view3turn24view4

### Promotion, pruning, stale detection, and conflict handling

Ниже — рекомендуемые decision rules. Это уже design proposal, но он основан на cited patterns above.

| Decision | Rule |
|---|---|
| **Promote to long-term** | если знание repo-stable, backed by artifact evidence, и либо повторилось в нескольких задачах, либо критично для planning/review every time |
| **Promote to entity memory** | если знание naturally attaches to a file/module/context/dependency/owner and improves future path-scoped retrieval |
| **Keep only in short-term** | если факт task-specific, recent, still possibly superseded, or useful mainly for resume/debug |
| **Drop as stale** | если path vanished, symbol disappeared, command repeatedly fails against current default branch, or newer verified entry supersedes it |
| **Merge duplicates** | если normalized subject plus predicate match and evidence overlaps; preserve union of provenance and keep newest verification timestamp |
| **Quarantine conflict** | если new evidence contradicts active memory but is only agent-inferred or weakly grounded |

Практически это означает такие heuristics:

- **Promote to long-term**: build/test command repeatedly used successfully; reviewer repeatedly asks for same test style; module repeatedly shows same fragile integration point; architecture fact confirmed by code and docs.
- **Do not promote**: “task X failed because provider Y was flaky”; “this one PR preferred a strange workaround”; “the agent speculated that file A is legacy”.
- **Drop as stale**: path deleted, command no longer works, entity renamed and relation can be deterministically migrated, or entry has gone unused and unverified through many relevant changes.
- **Merge duplicates**: same convention expressed with slightly different wording.

AGENTbench’s evidence against bloated context files and ContextBench’s evidence of recall-heavy retrieval are the two strongest empirical reasons to keep these promotion rules **strict rather than permissive**. citeturn10view0turn7search0

### Safety model

**Redaction and secret handling.**  
Memory writes must pass through mandatory redaction and secret scanning. GitHub secret scanning docs emphasize that secret scanning uses pattern matching and validation and that push protection blocks pushes containing secrets before they land in the repo. TruffleHog documents verification-first secret detection, and GitGuardian documents large detector coverage. For WORC the minimum viable rule is: **nothing writes into `.worc/memory/` before a secret scan passes**, and any suspicious candidate is either redacted or rejected. citeturn9search0turn9search5turn9search16turn9search2turn9search3

**Deny-by-default storage policy.**  
Persist only allowlisted fields and types. This is my recommendation, but it is strongly motivated by current memory-poisoning evidence: memory poisoning is persistent, existing prompt-injection defenses are incomplete, and write/retrieve aggressiveness increases attack success. In other words, the more your system writes and blindly reuses, the larger the attack surface becomes. citeturn24view3turn24view4

**Trust levels and provenance.**  
Каждая memory entry должна иметь `trust_level` and `provenance`:
- `repo-observed` — directly verifiable from code/config;
- `artifact-backed` — derived from task artifacts, checks, review comments;
- `review-verified` — confirmed in review/fixing outcome;
- `agent-inferred` — LLM-derived synthesis not yet independently confirmed;
- `external-untrusted` — came from web/doc/user/API content and cannot auto-promote.

Recent poisoning work shows that both content-based trust scoring and lineage alone are malleable; origin matters, but it also needs to be enforced in the write-retrieve-act pipeline. WORC should therefore never let low-trust memory silently behave like high-trust repo facts. citeturn24view3

**Audit trail, rollback, and quarantine.**  
Append-only `audit/log.jsonl`, content hashes, and periodic snapshots are worth adding from day one. Anthropic’s plain-file auto memory shows the value of auditable editable artifacts; recent deterministic-control-plane research argues for hash-chained audit logs and traceability around agent-governing artifacts; and security work makes clear that bad memory writes must be reversible. citeturn19view0turn25view1

**Bounded autonomy.**  
Hooks and enforcement should remain deterministic. Anthropic explicitly notes that memory and instruction files are only context, not enforced configuration, and recommends hooks such as `PreToolUse` when you need hard blocking behavior. WORC should follow the same principle: memory may guide planning and review, but enforcement lives in orchestrator policies, not in memory text. citeturn19view0turn18view3

**Containment over supervision-only.**  
Anthropic’s engineering writeup on containing Claude stresses that human approvals alone degrade because users approve most prompts, and that strict access boundaries and sandboxes are central to limiting blast radius. For WORC this means memory cleanup, extraction and retrieval should run under the same containment assumptions as agents themselves: no secret-bearing environment, no arbitrary network, no hidden write channels into core state. citeturn24view0

## Evaluation plan, evolution path, and final recommendation

### Concrete first version

Самая полезная первая версия для этого проекта выглядит так:

- canonical files in `.worc/memory/`;
- supervisor-only writes at task finalization;
- `short-term`, `long-term`, `entities`, `audit`, `quarantine`;
- no external vector DB;
- no graph DB;
- no autonomous self-editing prompt optimizer;
- stage-specific packets written as files and handed to agents by path;
- metadata-first retrieval using paths, entities, stage and task type;
- optional lightweight SQLite FTS sidecar only if file-based filtering gets clumsy.

Это already даст большую часть value: меньше rediscovery, better planning packets, better review/fixing recall, and bounded inspectable memory growth. It also matches provider portability goals because WORC stays independent of provider-native memory semantics. OpenAI explicitly positions AGENTS.md and MCP as open interoperability conventions, and Anthropic’s native memory is machine-local rather than a portable system-of-record. citeturn30view0turn19view1

### What to defer and what not to build yet

**Defer until later:**
- embeddings as canonical retrieval path;
- automatic prompt/procedure rewriting from noisy review logs;
- full issue/PR plus code entity graph;
- cross-repo shared memory;
- memory generated mid-run on every stage transition;
- model-driven freeform “autodream” without deterministic guardrails.

**Do not build yet:**
- vector-only memory architecture;
- transcript warehouse as memory;
- provider-native memory as system of record;
- giant `MEMORY.md` / giant `AGENTS.md` style monoliths.

И empirical evidence, и product docs говорят против этого. AGENTbench suggests big context files do not reliably pay off; ContextBench shows agents over-retrieve; Claude and Codex both structure durable context as concise startup material plus on-demand deeper files; long-context models still pay latency costs and don’t remove the need for retrieval discipline. citeturn10view0turn7search0turn19view1turn18view2turn23view3

### When files are enough, when SQLite becomes justified, when embeddings or graphs become justified

**Files are enough when** memory volume is still human-browsable, writes are sequential, and most retrieval can be driven by touched paths, stage, simple tags and a concise index. That is exactly your v1 shape: one active task, one repo, supervisor-owned memory.

**SQLite becomes justified when** you start needing:
- robust dedup and conflict queries;
- filtered search by stage/path/entity/trust/status;
- FTS over hundreds or thousands of records;
- transactional updates and compaction;
- richer audit queries.

**Embeddings become justified when** metadata-first retrieval starts missing relevant long-tail natural-language matches, especially across older lessons and procedural memories whose wording drifted. They should first be added as a **secondary recall layer**, not as the primary truth store.

**Entity graph becomes justified when** the codebase and task mix repeatedly require multi-hop reasoning across code entities and repository artifacts — for example, when issues, PR history, ownership, hotspots and dependencies all need to be traversed together. KGCompass and Prometheus give the clearest evidence for this threshold: graphs pay off when relational traversals become first-class rather than occasional. citeturn10view7turn12view0

### Evaluation plan

Оценивать subsystem нужно не только по final task success, но и по intermediate retrieval quality and operational cost.

Рекомендую такой metric stack:

| Category | Metrics |
|---|---|
| **End-to-end quality** | task success, review pass rate, reopen rate, fix-after-review iterations |
| **Retrieval quality** | context recall, precision, efficiency, explored-vs-used gap |
| **Planning quality** | first relevant file hit rate, files opened before first correct edit, plan acceptance by supervisor |
| **Cost** | tokens per stage, p50/p95 latency, time-to-first-meaningful-edit |
| **Memory health** | active records, stale rate, duplicate rate, quarantine rate, promotion acceptance rate |
| **Safety** | secret scan failures, redaction misses, low-trust retrieval incidents |
| **Operator UX** | time-to-debug wrong memory retrieval, audit trace completeness |

ContextBench is the strongest direct source for intermediate retrieval metrics in coding agents: it proposes recall, precision and efficiency over human-annotated gold contexts. Sourcegraph’s Cody evaluation adds useful answer-level metrics such as **Essential Recall**, **Essential Concision** and **Helpfulness**, which are also appropriate for planning summaries and review packets. citeturn7search0turn23view3

Экспериментально я бы делал так:

- **Offline replay** на historical WORC tasks with fixed models and fixed prompts, comparing memory-off vs memory-on vs memory-on-without-entity-cards.
- **Stage-level evals**: planning only, implementation only, review only, fixing only.
- **Ablations**: short-term only; long-term only; entities only; metadata-only retrieval vs metadata+semantic rerank.
- **Staleness drills**: intentionally outdated commands and renamed modules to test quarantine and stale detection.
- **Poisoning drills**: inject low-trust misleading candidate memories and verify they do not auto-promote or outrank trusted repo-backed memories.

Success criteria should be framed in benefit-versus-cost terms: fewer rediscovery steps and review loops without meaningful growth in irrelevant retrieved context, latency, or safety incidents. Failure indicators are equally important: rising duplicate rate, more wrong-path retrievals, stale command suggestions, and cases where memory overrides better current-code evidence.

### Final recommendation and rejected alternatives

**Final recommendation for `wastech-orchestrator`:**

Реализуйте **file-first, supervisor-owned, evidence-backed repo memory** в `.worc/memory/`, сохранив вашу трёхуровневую идею, но уточнив её так:

- **short-term** = bounded episodic run memory;
- **long-term** = semantic plus procedural plus reviewer plus failure memory;
- **entity memory** = lightweight entity cards with relations;
- **repo map / symbol index / embeddings** = derived indices, not canonical memory;
- **primary write point** = task finalization;
- **background job** = bounded reconciliation, not freeform dreaming;
- **primary retrieval** = metadata-first, precision-biased, stage-aware;
- **source of truth** = code plus task artifacts plus checks, not memory.

Это решение лучше всего подходит single-repo supervisor-owned orchestrator’у, минимизирует provider lock-in, сохраняет inspectability, operational simplicity и auditability, и при этом не закрывает путь к дальнейшему усилению architecture. Оно также лучше всего согласуется с сильнейшими доступными данными: small durable guidance beats giant context files, over-retrieval is a real problem, and persistent memory needs strict provenance and bounded updates. citeturn10view0turn7search0turn24view3turn24view4

**Rejected alternatives:**

- **Pure vector DB from day one** — слишком слабая inspectability и relation-awareness для repo memory, слишком легко получить fuzzy but wrong retrieval. citeturn13view2
- **Pure append-only markdown dump** — operator-friendly, но быстро гниёт без structured metadata, promotion logic and stale handling. citeturn19view1turn21view1
- **Full knowledge graph in v1** — promising, but architecture cost and schema burden are too high for current scope. citeturn10view7turn12view0
- **Native provider memory as canonical layer** — не переносимо и не соответствует your supervisor-owned design; у Claude auto memory machine-local, у Codex durable guidance is instruction-layer rather than a neutral repo memory system. citeturn19view1turn18view1
- **Aggressive autonomous autodream** — unjustified safety risk given current memory-poisoning results. citeturn24view3turn24view4turn24view5

### Source list

Ниже — ключевые источники, на которых опирается рекомендация.

- Anthropic, **How Claude remembers your project** — `CLAUDE.md`, repo-scoped auto memory, `MEMORY.md`, on-demand topic files, auditability. citeturn19view0turn19view1
- Anthropic, **Automate actions with hooks** — deterministic hooks and enforceable controls. citeturn18view3
- Anthropic, **Effective harnesses for long-running agents** — why compaction alone is not enough across many sessions. citeturn25view0
- OpenAI, **Custom instructions with AGENTS.md** and **Best practices for Codex** — layered repo guidance, durable small instruction files. citeturn18view1turn18view5
- OpenAI, **Agent Skills** and **Codex Prompting Guide** — progressive disclosure, context budgeting, injected instructions behavior. citeturn18view2turn18view4
- OpenAI, **Building Reliable Agents with Memory and Compaction** — compaction vs memory, reusable workflow lessons, reviewed artifact as source of truth. citeturn21view0turn22view0turn22view2
- OpenAI, **OpenAI for Developers in 2025** — AGENTS.md, MCP, provider-agnostic agent building blocks and portability. citeturn30view0
- GitHub Blog, **How to write a great agents.md** and related Copilot guidance — practical instruction-file patterns seen across real repos. citeturn26view0turn26view1turn26view3
- Sourcegraph, **AI-assisted Coding with Cody** and **Context Engineering** — context retrieval, evaluation, and why context pipeline quality dominates coding-agent outcomes. citeturn23view1turn23view2
- Sourcegraph, **Toward infinite context for code** — long context helps but has cost/latency trade-offs and does not eliminate retrieval engineering. citeturn23view3
- Gloaguen et al., **Evaluating AGENTS.md** — marginal gains from developer context files, slight negative effect for LLM-generated files, cost increase. citeturn10view0
- Li et al., **ContextBench** — process-oriented retrieval benchmark for coding agents; over-retrieval and recall-over-precision finding. citeturn7search0
- Yang et al., **KGCompass** — repo-aware knowledge graph linking PR/issues and code entities for repository repair. citeturn10view7
- Prometheus, **memory-centric codebase navigation** — working memory plus lightweight repository graph for long-horizon repo tasks. citeturn12view0turn12view3
- MemCoder, **structured memory from historical commits and human-verified solutions** — strong research signal for repo-specific long-term memory. citeturn11view0turn11view1turn11view3
- Wang et al., **Agent Workflow Memory** — reusable procedural workflows as memory, strong gains on long-horizon tasks. citeturn16view0turn16view2
- Memory survey, **Memory for Autonomous LLM Agents** — write–manage–read framework, storage substrate trade-offs, memory taxonomy. citeturn13view0turn13view1turn13view2turn13view3
- LangGraph/LangMem docs — semantic, episodic, procedural memory; hot-path vs background writing; prompt optimization. citeturn28view0turn28view1turn27view2turn27view3turn27view0
- MCP Security Best Practices and Microsoft MCP injection guidance — treat untrusted content as adversarial; secure tool/context surfaces. citeturn24view1turn24view2
- Recent memory poisoning work and Unit 42 field writeup — persistent memory as attack surface, incomplete defenses, need for provenance and quarantine. citeturn24view3turn24view4turn24view5
- GitHub Secret Scanning, TruffleHog, GitGuardian docs — mandatory secret scanning and redaction before persistence. citeturn9search0turn9search5turn9search2turn9search3