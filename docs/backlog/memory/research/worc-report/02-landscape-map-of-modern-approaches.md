# 02. Landscape map современных подходов

[Previous](./01-executive-summary.md) | [Next](./03-comparison-matrix-storage-retrieval-update-architectures.md)

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
