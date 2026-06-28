# 06. Evaluation plan, evolution path, and final recommendation

[Previous](./05-lifecycle-write-path-read-path-and-safety-model.md) | [Next](./3rd-party-deep-research-memory-report.md)

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
| --- | --- |
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
