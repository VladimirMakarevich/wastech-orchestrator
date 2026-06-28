# 03. Comparison of storage, retrieval, and update architectures

[Previous](./02-landscape-map-of-modern-approaches.md) | [Next](./04-recommended-architecture-for-wastech-orchestrator.md)

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

