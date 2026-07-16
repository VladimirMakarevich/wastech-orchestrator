# Supervisor P2: разделение обязанностей + telemetry

**Статус:** proposal **Приоритет:** P2 (структурная чистота и наблюдаемость; не блокирует экономию, которую уже дают P0+P1) **Источник:** [2026-07-16 варианты оптимизации supervisor](../analysis/2026-07-16-supervisor-token-optimization-options.md) (§6 целевая архитектура, §8 P2).

**Дорожная карта:** [P0 — packet + fresh finalize](supervisor-finalize-packet-and-cadence.md) → [P1 — управляемый cadence](supervisor-observation-cadence-p1.md) → **P2 (этот документ)**.

## Зависимости

- **Требует P1** (детерминированная step-запись и раздельные observe/finalize уже есть — P2 их извлекает в отдельный компонент и делает handoff/skill бюджетируемыми).
- Telemetry по функциям опирается на уже реализованный [normalized-usage-accounting.md](normalized-usage-accounting.md) (нормализованный usage per attempt).

## Проблема

Сегодня в одном классе `Supervisor` смешаны четыре разные обязанности (per-step observer, finalizer, skill-proposer, subtask-handoff — `core/supervisor.py`). Это затрудняет раздельное бюджетирование и делает невозможным честный ответ на вопрос «сколько стоила каждая функция». После P0+P1 экономия уже получена, но: (1) детерминированная фиксация фактов живёт внутри LLM-класса, а не как самостоятельный источник правды; (2) handoff и skill-proposal делят один необъятный бюджет с наблюдениями; (3) нет per-function учёта calls/input/cache/output/cost/duration и нет сигнала, когда supervisor снова становится крупнейшим потребителем задачи.

## Требуемый результат

Монолит разделён на явные обязанности с чётким источником правды и раздельными бюджетами; каждая функция измерима, а оператор получает предупреждение, когда supervisor доминирует по расходу.

## Целевая архитектура (§6)

```text
StepRecorder          детерминированный, всегда, без LLM — источник правды
ObservationAdvisor    опциональный, event-triggered (политика из P1)
TaskFinalizer         один fresh LLM-turn из SupervisorPacket (P0)
SubtaskHandoff        только на реальной границе subtasks (свой бюджет)
SkillProposer         только при dynamic skills и непустом inventory (свой бюджет)
```

`StepRecorder` сохраняет bounded-факты без LLM: node id/kind/outcome, run/attempt/provider/model, changed paths + diff fingerprint, checks summary, evaluator verdict + severity counts, bounded final message, HITL/fallback/retry-факты, artifact references. `ObservationAdvisor` добавляет note к ledger, но не заменяет факты. `TaskFinalizer` всегда начинает fresh и получает один `SupervisorPacket`. `SubtaskHandoff` и `SkillProposer` — отдельные capabilities со своим бюджетом.

## В объёме P2

1. Вынести детерминированный `StepRecorder` из LLM-supervisor в самостоятельный компонент — единый источник правды для packet/digest/ledger.
2. Сделать `SubtaskHandoff` и `SkillProposer` отдельными budgeted capabilities (свой бюджет вызовов, независимый от observation-бюджета P1).
3. Persist нормализованного usage/cost **по каждой функции** (observe/finalize/handoff/skill) поверх normalized-usage-accounting.
4. Добавить в task summary секцию-отчёт: supervisor `calls / input / cache / output / cost / duration`.
5. Предупреждать (лог + видимый callout), когда supervisor становится крупнейшим потребителем задачи.

## Критерии приёмки

- [ ] `StepRecorder` пишет полную детерминированную step-запись без LLM; observe/finalize/packet читают её, а не наоборот.
- [ ] Handoff и skill-proposal тратят собственный бюджет; исчерпание observation-бюджета их не блокирует и наоборот.
- [ ] Нормализованный usage/cost персистится с разбивкой по функции; сумма сходится с общим usage задачи.
- [ ] Task summary содержит supervisor-отчёт (calls/input/cache/output/cost/duration).
- [ ] При доминировании supervisor по расходу в логах и в summary появляется предупреждение.
- [ ] Supervisor остаётся read-only и advisory; контракт «Core решает» не нарушен.

## Тесты под замену/добавление

- `tests/core/test_supervisor.py` — `StepRecorder` как отдельный источник; раздельные бюджеты handoff/skill.
- `tests/` state-store — round-trip per-function usage.
- Тест на присутствие supervisor-отчёта в summary и на срабатывание предупреждения о доминировании.

## Вероятные области реализации

- `src/wastech_orchestrator/core/supervisor.py` — извлечение `StepRecorder`, раздельные capabilities и бюджеты.
- `src/wastech_orchestrator/core/orchestrator.py` — вызов `StepRecorder` в post-node hook, сборка supervisor-отчёта в finalize.
- `src/wastech_orchestrator/state_store.py` — per-function usage/cost (поверх normalized-usage-accounting).
- `src/wastech_orchestrator/packaged/guide/`, `docs/worc_architecture.md`, `docs/configuration.md` — архитектура и отчётность.
