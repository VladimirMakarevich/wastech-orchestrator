# 01. Executive summary

[Previous](./worc-deeep-research-memory-report.md) | [Next](./02-landscape-map-of-modern-approaches.md)

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
- не класть memory в существующий `state.db`: это ломает его роль как state-machine store; если позже понадобится database shape, нужен отдельный `memory.sqlite` под `.worc/memory/`, не schema bump `state.db`. Это также соответствует уже существующему backlog-направлению проекта. [docs/backlog/archive/done/orchestrator-memory.md](../backlog/archive/done/orchestrator-memory.md)

Итоговая рекомендация:

- V1: files-first hybrid memory (`md` + `json` + `jsonl`) + deterministic retrieval + validation + audit.
- V2: отдельный SQLite/FTS index, когда файловые merge/query станут неудобны.
- V3: embeddings only after measured recall failures on lexical/entity retrieval.
- V4: entity graph only after реальная потребность в multi-hop repo reasoning, а не "на всякий случай".
