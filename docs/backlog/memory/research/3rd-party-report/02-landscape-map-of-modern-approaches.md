# 02. Landscape map of modern approaches

[Previous](./01-executive-summary.md) | [Next](./03-comparison-of-storage-retrieval-and-update-architectures.md)

### What kinds of memory are actually useful for coding and repository agents

Современная литература по agent memory почти консенсусно различает **working**, **episodic**, **semantic** и **procedural memory**. Свежий survey по memory for autonomous LLM agents формализует память как цикл **write–manage–read** и отдельно описывает episodic memory как записи конкретных опытов, semantic memory как абстрагированные факты, а procedural memory как reusable skills and plans. Тот же survey прямо отмечает, что software engineering agents особенно сильно зависят от **procedural memory**, то есть от verified code patterns, workflows и architecture decisions. Аналогичные категории дают LangGraph/LangMem docs: semantic — facts/knowledge, episodic — past experiences, procedural — system behavior and instructions. citeturn13view0turn13view2turn13view3turn27view2turn27view3

Для repo agents в реальной инженерной практике полезны не все типы одинаково. Наиболее полезны:

| Memory type | Что реально даёт coding/repo agent | Практический статус |
| --- | --- | --- |
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
