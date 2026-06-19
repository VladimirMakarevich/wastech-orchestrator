# P3 — Flows research + audit + их виды узлов

Статус: **backlog / инженерная спека (не запланировано к исполнению)** Дата: 2026-06-17 Владелец: Vladimir Makarevich

Детализация фазы P3 из [plan.md](plan.md). Цель: два не-implementation flow данными; **доказательство обобщаемости** движка. Адаптирует [task workflow profiles](../outdated/task_workflow_profiles.md). База проверки фазы: **co-design тест трёх flow** — финальный гейт абстракции ([index.md](index.md) §11): три flow выражаются данными без доменного знания в движке.

Вход: полный evaluator-примитив (P2, включая `resume_own_lineage`), packaged [deep_research.yaml](../../../src/wastech_orchestrator/core/flow/packaged/deep_research.yaml) и [security_audit.yaml](../../../src/wastech_orchestrator/core/flow/packaged/security_audit.yaml) (уже грузятся/валидируются в P0; в P3 — исполняются).

Заметка про права записи ([flow-contract.md](flow-contract.md) §8): research/audit держат `permission_ceiling: workspace-write` (пишущие узлы `synthesis`/`report` создают файлы), но `output_policy` + path-containment + after-stage guard ограничивают **куда**; исходники repo остаются read-only. Sandbox разрешает запись, политика ограничивает путь — два слоя.

---

## Пункты P3

- [P3.1 — Ядровые чекеры `checks`](p3.1-core-checkers.md) — `citation` + `dependency_scan`; делают `checks` пригодным для research/audit без LLM.
- [P3.2 — Политики output / publishing / network](p3.2-output-policies.md) — `repository_document`, `private_control_workspace_report`, after-stage guard, `network_policy`.
- [P3.3 — `deep_research.yaml`](p3.3-deep-research-yaml.md) — research-flow данными; citation-loop pinned 1, bounded critic, non-blocking на исчерпании → Open questions.
- [P3.4 — `security_audit.yaml`](p3.4-security-audit-yaml.md) — audit-flow данными; publishing `none`; co-design тест — финальный гейт абстракции.

---

## Сквозной обзор зависимостей P3

```text
P2 (полный evaluator + durable resume_own_lineage)
   ├─> P3.1 checkers (citation, dependency_scan)
   ├─> P3.2 policies (output/publishing/network + path-containment)
   │     └─> P3.3 deep_research.yaml ──┐
   │     └─> P3.4 security_audit.yaml ─┴─> co-design тест (финальный гейт абстракции)
```

P3.1 и P3.2 — параллельны (checkers ↔ policies); оба нужны обоим flow.

## Контракт выхода P3 → P4

- Абстракция доказана тремя flow данными — движок без доменного знания. P4 открывает ту же поверхность операторам (операторский flow = тот же контракт).
- Все политики/чекеры/потолки — ядровые; flow их только выбирает. P4 проверяет, что **операторский** flow не может их обойти.

## Пересечения для ревью (потенциальные противоречия)

- **`permission_ceiling: workspace-write` у research/audit** при read-only-намерении для repo. Разрешается двумя слоями (sandbox разрешает запись, `output_policy` + path-containment ограничивают путь). Убедиться, что after-stage guard (P3.2) — единственный механизм, отделяющий «можно писать в research-dir» от «нельзя трогать src». Это не должно быть выражено новым видом узла или спец-кейсом движка.
- **`blocking: false` семантика на исчерпании** (research/audit) vs **`manual_action_required` на исчерпании** (implementation). Разница — свойство узла (`blocking`), не движка: движок на исчерпании budget-ребра у `blocking:false`-evaluator идёт по `accept`-ребру; у `blocking:true` — в manual. Анкерить тестом `test_research_non_blocking_exhaustion_publishes_with_open_questions`.
