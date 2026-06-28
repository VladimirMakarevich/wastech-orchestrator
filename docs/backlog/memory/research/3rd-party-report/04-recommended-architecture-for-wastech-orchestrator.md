# 04. Recommended architecture for `wastech-orchestrator`

[Previous](./03-comparison-of-storage-retrieval-and-update-architectures.md) | [Next](./05-lifecycle-write-path-read-path-and-safety-model.md)

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
| --- | --- | --- |
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
    { "name": "unit", "status": "passed" },
    { "name": "lint", "status": "passed" }
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
    { "type": "depends_on", "entity_id": "module:task-artifacts" },
    { "type": "writes_to", "entity_id": "path:.worc/memory/" }
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
