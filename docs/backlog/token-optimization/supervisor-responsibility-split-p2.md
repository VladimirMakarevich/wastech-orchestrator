# Supervisor P2: разделение обязанностей + telemetry

**Статус:** **accepted 2026-07-26** (все развилки закрыты в «[Решения приёмки](#решения-приёмки-2026-07-26)»; реализацию не начинать до мёржа P1) **Приоритет:** P2 (структурная чистота и наблюдаемость; не блокирует экономию, которую уже дают P0+P1) **Источник:** [2026-07-16 варианты оптимизации supervisor](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/analysis/2026-07-16-supervisor-token-optimization-options.md) (§6 целевая архитектура, §8 P2).

**Дорожная карта:** [P0 — packet + fresh finalize](supervisor-finalize-packet-and-cadence.md) → [P1 — управляемый cadence](supervisor-observation-cadence-p1.md) → **P2 (этот документ)**.

## Зависимости

- **Требует P1** (детерминированная step-запись и раздельные observe/finalize уже есть — P2 извлекает запись в отдельный компонент).
- Telemetry по функциям опирается на уже реализованный [normalized-usage-accounting.md](normalized-usage-accounting.md) (нормализованный usage per attempt).
- **Актуализация 2026-07-26:** половина подложки для пункта 3 уже есть. VF-8 (DB v19) дал `provider_attempts.task_id` и сделал `node_run_id` nullable — постоянный supervisor-слой пишет свои вызовы как `node_run_id IS NULL` (`state_store.py:103-108`), так что «сколько потратил supervisor» уже считается одним запросом. Не хватает только разбивки _по функции_ (observe / finalize / handoff / skill) — это и есть работа пункта 3, а не учёт с нуля. Ридер `get_provider_attempts_for_task` существует и пока используется только в тестах: пункты 4–5 делают его первым продакшн-потребителем.

## Проблема

Сегодня в одном классе `Supervisor` смешаны четыре разные обязанности (per-step observer, finalizer, skill-proposer, subtask-handoff — `core/supervisor.py`). Это делает невозможным честный ответ на вопрос «сколько стоила каждая функция». После P0+P1 экономия уже получена, но: (1) детерминированная фиксация фактов живёт внутри LLM-класса, а не как самостоятельный источник правды; (2) нет per-function учёта calls/input/cache/output/cost/duration, поэтому неизвестно, какая функция сколько тратит.

## Требуемый результат

Монолит разделён на явные обязанности с чётким источником правды, и расход каждой функции измерим — оператор видит в summary, сколько стоили observe / finalize / handoff / skill.

## Целевая архитектура (§6)

```text
StepRecorder          детерминированный, всегда, без LLM — источник правды
ObservationAdvisor    опциональный, event-triggered (политика из P1)
TaskFinalizer         один fresh LLM-turn из SupervisorPacket (P0)
SubtaskHandoff        только на реальной границе subtasks (как сейчас)
SkillProposer         только при dynamic skills и непустом inventory (как сейчас)
```

`StepRecorder` сохраняет bounded-факты без LLM: node id/kind/outcome, run/attempt/provider/model, changed paths + diff fingerprint, checks summary, evaluator verdict + severity counts, bounded final message, HITL/fallback/retry-факты, artifact references. `ObservationAdvisor` добавляет note к ledger, но не заменяет факты. `TaskFinalizer` всегда начинает fresh и получает один `SupervisorPacket`. `SubtaskHandoff` и `SkillProposer` остаются как есть — отдельными бюджетируемыми capabilities их не делаем (решение P2-D1).

## В объёме P2

Объём урезан решением P2-D1 (2026-07-26) до трёх пунктов:

1. Вынести детерминированную step-запись из LLM-supervisor в `core/flow/recorder.py` (решение P2-D2) — единый источник правды для packet/digest и строк `supervisor_step`.
2. Persist нормализованного usage/cost **по каждой функции** (observe/finalize/handoff/skill) поверх normalized-usage-accounting: строки supervisor'а уже отделены (`provider_attempts` с `node_run_id IS NULL`, VF-8) и уже несут нормализованные колонки — не хватает только метки функции.
3. Добавить в summary секцию-отчёт: supervisor `calls / input / cache / output / cost / duration` (`summary.json` уже пишется всегда — `core/supervisor.py:1391`).

Исключены:

- ~~раздельные бюджеты `SubtaskHandoff` / `SkillProposer`~~ — обоснование было «они делят бюджет с наблюдениями», но бюджетов в кампании больше нет (решение P1-D6), а обе функции по умолчанию даже не запускаются (`decomposition.enabled: false`, `skills.dynamic: false`).
- ~~предупреждение «supervisor доминирует по расходу»~~ — потребовало бы выдуманного порога; отчёт из пункта 3 и так показывает расход, и решение принимает человек.

## Решения приёмки (2026-07-26)

### P2-D1 — объём урезан до трёх пунктов

Зафиксировано выше, в §В объёме P2: остаются `StepRecorder`, per-function usage и supervisor-отчёт в summary; раздельные бюджеты handoff/skill и предупреждение о доминировании исключены.

### P2-D2 — расширяем существующий рекордер, а не заводим новый модуль

Детерминированная step-запись живёт в `core/flow/recorder.py`, рядом с уже существующим `StateStoreRunRecorder`, который реализует seam `RunRecorder` движка, пишет `node_runs` и чекпоинтит `FlowRunState`. Новый модуль не создаётся: иначе появятся два места, пишущих детерминированные факты об одной и той же ноде, и вопрос «кто источник правды» вернётся.

Разделение ответственности после этого читается однозначно:

- **факты** (нода, kind, исход, попытки, provider/model, изменённые пути, checks, вердикт эвалюатора, bounded-сообщение) — рекордер, без LLM, всегда;
- **заметка** — LLM-observer, по-прежнему строкой `supervisor_step` в `evaluations` (`core/supervisor.py:682`), но она _дополняет_ факты, а не заменяет их;
- **пакет и digest** читают факты из рекордера, а не из LLM-класса.

Уточнение терминологии, чтобы исполнитель не полез в чужой модуль: слово «ledger» в этом документе означает строки `supervisor_step` в `evaluations`, а **не** `ledger.py` — тот про терминальные переходы задачи (`logs/completed.jsonl`, `failure_report.json`, минимальный summary).

Импорт-контракты не меняются: компонент остаётся внутри `core/flow/`, а `providers` по-прежнему лист ([.importlinter](../../../.importlinter)).

### P2-D4 — снят как несуществующий

Развилка «какой порог у предупреждения о доминировании и где оно показывается» отпала вместе с самим пунктом: решение P2-D1 исключило предупреждение из объёма, поэтому порог решать нечего.

### P2-D3 — одна nullable-колонка на `provider_attempts`

Функция вызова хранится как одна дополнительная nullable-колонка на `provider_attempts` (`observe` | `finalize` | `handoff` | `skill`; NULL для обычных нод графа). Отдельная таблица не заводится: она либо дублировала бы usage-колонки, либо требовала join на каждый отчёт, а критерий «сумма по функциям сходится с общим usage задачи» перестал бы быть одним запросом.

Сегодня функция в БД не записана вовсе: синтетический id (0 — finalize, 999 999 — skill-proposal, база+n — handoff, реальный id шага — observe) используется только для namespacing артефактов, а в `provider_attempts` пишется `node_run_id` NULL (VF-8). Выводить функцию разбором `attempt_dir` — отвергнуто: это семантика из магических чисел в строке пути.

Миграция — по домашнему правилу: аддитивная nullable-колонка добавляется в `_migrate`, БД более старой версии отвергается fail-closed и пересоздаётся (greenfield, переносить нечего) — так же, как делали v16 и v19 (`state_store.py:96-108`).

## Критерии приёмки

- [ ] `StepRecorder` пишет полную детерминированную step-запись без LLM; observe/finalize/packet читают её, а не наоборот.
- [ ] Нормализованный usage/cost персистится с разбивкой по функции — одна nullable-колонка на `provider_attempts` (решение P2-D3); сумма по функциям сходится с общим usage задачи одним запросом.
- [ ] Summary содержит supervisor-отчёт (calls/input/cache/output/cost/duration).
- [ ] Supervisor остаётся read-only и advisory; контракт «Core решает» не нарушен.

## Тесты под замену/добавление

- `tests/core/test_supervisor.py` — `StepRecorder` как отдельный источник правды.
- `tests/` state-store — round-trip per-function usage.
- Тест на присутствие supervisor-отчёта в summary.

## Вероятные области реализации

- `src/wastech_orchestrator/core/flow/recorder.py` — детерминированная step-запись рядом с `StateStoreRunRecorder` (решение P2-D2).
- `src/wastech_orchestrator/core/supervisor.py` — LLM-заметка перестаёт быть источником фактов; метка функции на provider-вызовах.
- `src/wastech_orchestrator/core/orchestrator.py` — вызов `StepRecorder` в post-node hook, сборка supervisor-отчёта в finalize.
- `src/wastech_orchestrator/state_store.py` — колонка функции на `provider_attempts` + bump `DB_SCHEMA_VERSION` (решение P2-D3).
- `src/wastech_orchestrator/packaged/guide/flows/roles.md` — описание supervisor-слоя (разделённые обязанности + supervisor-отчёт в summary). Derived `docs/worc_architecture.md` / `docs/configuration.md` на `dev` отсутствуют: только doc-impact note в PR (X2).
