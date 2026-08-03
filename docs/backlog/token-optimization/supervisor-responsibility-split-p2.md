# Supervisor P2: разделение обязанностей + telemetry

**Статус:** **implemented 2026-08-03** (все развилки были закрыты в «[Решения приёмки](#решения-приёмки-2026-07-26)»; отступления от текста задачи — в «[Отклонения от текста задачи](#отклонения-от-текста-задачи-2026-08-03)») **Приоритет:** P2 (структурная чистота и наблюдаемость; не блокирует экономию, которую уже дают P0+P1) **Источник:** [2026-07-16 варианты оптимизации supervisor](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/analysis/2026-07-16-supervisor-token-optimization-options.md) (§6 целевая архитектура, §8 P2).

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

Это эскиз из источника; что получилось по факту — в «[Отклонения от текста задачи](#отклонения-от-текста-задачи-2026-08-03)». Коротко: классов с такими именами нет и не нужно — `StepRecorder` стал набором функций (`StepFacts` / `fell_back_from` / `collect_step_facts`) в `core/flow/recorder.py`, потому что писатель `node_runs` — сами ноды; ограничение длины сообщения осталось на стороне рендера пакета, а не факта; `ObservationAdvisor` / `TaskFinalizer` / `SubtaskHandoff` / `SkillProposer` — это по-прежнему методы `Supervisor`, и разделять их на классы никто не просил (P2-D1 урезал объём до трёх пунктов).

## В объёме P2

Объём урезан решением P2-D1 (2026-07-26) до трёх пунктов:

1. Вынести детерминированную step-запись из LLM-supervisor в `core/flow/recorder.py` (решение P2-D2) — единый источник правды для packet и для гейта наблюдений. Строки `supervisor_step` рекордер **не** пишет: это продукт LLM-наблюдателя, и потребителя у второй, детерминированной строки нет (см. отклонение 2).
2. Persist нормализованного usage/cost **по каждой функции** (observe/finalize/handoff/skill) поверх normalized-usage-accounting: строки supervisor'а уже отделены (`provider_attempts` с `node_run_id IS NULL`, VF-8) и уже несут нормализованные колонки — не хватает только метки функции.
3. Добавить в summary секцию-отчёт: supervisor `calls / input / cache / output / cost / duration` (`summary.json` уже пишется всегда — `_write_summary_json` в `core/supervisor.py`).

Исключены:

- ~~раздельные бюджеты `SubtaskHandoff` / `SkillProposer`~~ — обоснование было «они делят бюджет с наблюдениями», но бюджетов в кампании больше нет (решение P1-D6), а обе функции по умолчанию даже не запускаются (`decomposition.enabled: false`, `skills.dynamic: false`).
- ~~предупреждение «supervisor доминирует по расходу»~~ — потребовало бы выдуманного порога; отчёт из пункта 3 и так показывает расход, и решение принимает человек.

## Решения приёмки (2026-07-26)

### P2-D1 — объём урезан до трёх пунктов

Зафиксировано выше, в §В объёме P2: остаются `StepRecorder`, per-function usage и supervisor-отчёт в summary; раздельные бюджеты handoff/skill и предупреждение о доминировании исключены.

### P2-D2 — расширяем существующий рекордер, а не заводим новый модуль

Детерминированная step-запись живёт в `core/flow/recorder.py`, рядом с уже существующим `StateStoreRunRecorder`, который реализует seam `RunRecorder` движка и чекпоинтит `FlowRunState`. Новый модуль не создаётся: иначе появятся два места, отвечающих за детерминированные факты об одной и той же ноде, и вопрос «кто источник правды» вернётся. (Предпосылка «рекордер пишет `node_runs`» оказалась неверной — см. отклонение 1; на само решение это не повлияло, но изменило объём.)

Разделение ответственности после этого читается однозначно:

- **факты** (нода, kind, исход, попытки, provider, fallback, изменённые пути, checks, сообщение шага) — рекордер, без LLM, всегда;
- **заметка** — LLM-observer, по-прежнему строкой `supervisor_step` в `evaluations` (`Supervisor.observe`), но она _дополняет_ факты, а не заменяет их;
- **пакет и гейт наблюдений** читают факты из рекордера, а не из LLM-класса.

Уточнение терминологии, чтобы исполнитель не полез в чужой модуль: слово «ledger» в этом документе означает строки `supervisor_step` в `evaluations`, а **не** `ledger.py` — тот про терминальные переходы задачи (`logs/completed.jsonl`, `failure_report.json`, минимальный summary).

Существующие импорт-контракты не нарушены (компонент остаётся внутри `core/flow/`, `providers` по-прежнему лист), но добавлен один новый — см. отклонение 4 ([.importlinter](../../../.importlinter)).

### P2-D4 — снят как несуществующий

Развилка «какой порог у предупреждения о доминировании и где оно показывается» отпала вместе с самим пунктом: решение P2-D1 исключило предупреждение из объёма, поэтому порог решать нечего.

### P2-D3 — одна nullable-колонка на `provider_attempts`

Функция вызова хранится как одна дополнительная nullable-колонка на `provider_attempts` (`observe` | `finalize` | `handoff` | `skill`; NULL для обычных нод графа). Отдельная таблица не заводится: она либо дублировала бы usage-колонки, либо требовала join на каждый отчёт, а критерий «сумма по функциям сходится с общим usage задачи» перестал бы быть одним запросом.

Сегодня функция в БД не записана вовсе: синтетический id (0 — finalize, 999 999 — skill-proposal, база+n — handoff, реальный id шага — observe) используется только для namespacing артефактов, а в `provider_attempts` пишется `node_run_id` NULL (VF-8). Выводить функцию разбором `attempt_dir` — отвергнуто: это семантика из магических чисел в строке пути.

Миграция — по домашнему правилу: аддитивная nullable-колонка добавляется в `_migrate`, БД более старой версии отвергается fail-closed и пересоздаётся (greenfield, переносить нечего) — так же, как делали v16 и v19 (`_migrate_usage_columns` + `_enforce_schema_version` в `state_store.py`).

Критерий «сумма по функциям сходится с общим usage задачи одним запросом» выполняется буквально, потому что колонка одна и `WHERE` не отбрасывает ни одной строки:

```sql
SELECT COALESCE(supervisor_function, 'node') AS fn,
       COUNT(*)                AS calls,
       SUM(usage_input_total)  AS input,
       SUM(usage_cache_read)   AS cache_read,
       SUM(usage_output_total) AS output,
       SUM(usage_cost)         AS cost
FROM provider_attempts
WHERE task_id = ?
GROUP BY fn
ORDER BY fn;
```

Строки результата — это бакеты одних и тех же строк, поэтому их сумма по построению равна `SELECT COUNT(*), SUM(usage_input_total) FROM provider_attempts WHERE task_id = ?`. Тождество закреплено тестом `tests/state/test_state_store.py::test_per_function_usage_reconciles_with_the_task_total_in_one_query`.

## Отклонения от текста задачи (2026-08-03)

Реализация следует решениям P2-D1…P2-D3. Пять мест, где текст задачи разошёлся с кодом; в каждом сохранено решение, но не его формулировка.

1. **Предпосылка P2-D2 «`StateStoreRunRecorder` пишет `node_runs`» неверна, и это изменило объём пункта 1.** Рекордер вызывает только `record_node_skip`; настоящие `record_node_run` / `complete_node_run` живут в реализациях нод (17 вызовов в `nodes/{agent,evaluator,checks,tool,hitl,publish}.py`, через собственный порт `NodeRunStorePort`), причём с разными шейпами: `route_*` передают только agent/evaluator, `provider_used`/`error_class`/`stage_attempts` — тоже, `commit_sha_after` — только publish. Вариант «рекордер становится единственным писателем» отвергнут: правка 6 модулей и расширение узкого seam движка не даёт ни поведения, ни выполнения критерия приёмки (критерий про то, **откуда пакет и observe берут факты**, а не про то, кто выполняет INSERT). Вместо этого рекордер стал единственным местом, которое **утверждает** факты о выполненной ноде: `StepFacts` (frozen dataclass без дефолтов — забытое поле тогда ловит mypy, а не тихо превращается в `None`), `fell_back_from(row)` и `collect_step_facts(...)`. Сборка фактов уехала из LLM-класса (`_step_messages` удалён, `_build_packet` теперь только зовёт рекордер), а `PacketFacts` вместо `node_runs` + `step_messages` несёт `steps: tuple[StepFacts, ...]`; `core/supervisor_packet.py` остался чистым рендером. Рендер пакета байт-в-байт тот же — это и было условием правки.
2. **Строка формы наблюдения не введена, и упоминание её как продукта рекордера убрано из §В объёме P2.** Потребителя нет: шаги пакета берутся из `node_runs` + `<node_id>.out.md`, `_finalize_digest` пропускает пустые заметки, а параллельная детерминированная строка в `evaluations` была бы вторым писателем тех же фактов — ровно то, что P2-D2 запрещает. Это подтверждает решение P1 (его отклонение 2), а не переоткрывает его.
3. **Гейт наблюдений тоже стал читателем рекордера — иначе «observe читает запись» было бы неправдой.** `supervisor_packet._steps` и `observe_cadence.triggers_for` независимо выводили один факт («шаг ушёл не туда, куда был смаршрутизирован») из одной пары колонок. Теперь вывод один — `fell_back_from` в рекордере, — а `triggers_for` принимает готовое `fell_back: bool` вместо `provider_used`/`route_primary` (арность 5 → 4, модуль остался на примитивах). Четыре комбинации колонок переехали тестом в `tests/core/test_flow_recorder.py`, чтобы правка не оказалась чистой потерей покрытия. Хук `_observes_step` **не** конструирует `StepFacts` ради одного булева.
4. **Добавлен один import-контракт, хотя P2-D2 говорил «импорт-контракты не меняются».** Новый шов делает возможным цикл `core.flow.recorder` ↔ `core.supervisor_packet`, а внутри `core.*` контрактов не было вовсе, так что направление держалось только на код-ревью. Контракт `step-record-below-supervisor` запрещает `core.flow.recorder` импортировать `core.supervisor` / `core.supervisor_packet` / `core.supervisor_usage`. Он намеренно узкий, по одному модулю: более широкий `source_modules = core.flow` падает на уже существующем `TYPE_CHECKING`-импорте `core.flow.wiring → core.orchestrator`, который стоит там как раз ради разрыва цикла.
5. **`_finalize_digest` и граница длины сообщения остались на стороне supervisor'а, хотя эскиз §6 отдавал рекордеру «bounded-факты».** Digest парсит приватный payload строк `supervisor_step` — контракт верхнего слоя, которому нечего делать в persistence-адаптере движка; а 500-символьный потолок сообщения — свойство размерного бюджета пакета, не свойство того, что сказала нода. Поэтому `StepFacts.message` несёт текст дословно, а ограничивает его рендер. Разделительная линия по итогу: **flow-слой владеет фактами о node-run, supervisor-слой — своим словарём** (свои заметки, свои метки функций). По той же причине `SupervisorFunction` живёт в новом `core/supervisor_usage.py` (рядом с `supervisor_packet.py`), а не в `core/flow/`: запрет P2-D2 на новый модуль относился к step-записи, а в стор и в `observability` уходит плоская строка.

Ещё три уточнения по фактам, а не по решениям:

- `DB_SCHEMA_VERSION` поднят 19 → **20**; `CONFIG_SCHEMA_VERSION` остался **33** (P2 схему конфига не трогает).
- Почти все ссылки `file.py:NNN` в этом документе на момент реализации были смещены P0 и P1 — сверяйтесь grep'ом, а не номерами. Заменены на имена функций там, где это не мешает.
- **Отчёт теряется на degraded-прогоне.** Если finalize не выдал summary, орхестратор пишет детерминированный минимальный summary — и его `write_minimal_summary` перезаписывает `summary.json` своим контрактом из четырёх ключей (закреплён тестом `tests/core/test_ledger.py`). То есть `supervisor_usage` виден на нормальном прогоне, но не на том, где finalize провалился, хотя расход там уже случился. Не лечим: авторитетные данные лежат в `provider_attempts`, откуда их и читают метрики кампании, а учить fallback чужому ключу — это ломать его контракт ради вырожденного случая.

## Критерии приёмки

- [x] Детерминированная step-запись живёт в `core/flow/recorder.py` и собирается без LLM; пакет и гейт наблюдений читают её, а не наоборот (`StepFacts` / `fell_back_from` / `collect_step_facts`). Имя `StepRecorder` не использовано — запись собирается функциями рядом с `StateStoreRunRecorder`, потому что писатель `node_runs` — сами ноды (см. отклонение 1). Тесты: `tests/core/test_flow_recorder.py`, `tests/core/test_supervisor.py::test_packet_step_*`.
- [x] Нормализованный usage/cost персистится с разбивкой по функции — одна nullable-колонка `provider_attempts.supervisor_function` (решение P2-D3), метка приходит явным параметром `function` через тот же шов, что `turn`. Сумма по функциям сходится с общим usage задачи одним `GROUP BY` (запрос — в P2-D3). Тесты: `tests/state/test_state_store.py::test_supervisor_function_round_trips_and_is_null_for_a_graph_node`, `::test_per_function_usage_reconciles_with_the_task_total_in_one_query`, `tests/core/test_supervisor.py::test_each_phase_labels_its_own_provider_calls`.
- [x] `summary.json` содержит supervisor-отчёт `calls / input / cache_read / cache_write / output / cost / duration_seconds` — итог и разбивка по функциям. В `summary.md` (тело PR) телеметрии нет сознательно: расход — операторский, не ревьюерский, и не должен уезжать в remote вместе с изменением. Тесты: `tests/core/test_supervisor.py::test_summary_json_reports_what_the_layer_spent_per_phase`, `::test_summary_md_carries_no_spend_telemetry`, `tests/core/test_supervisor_usage.py`.
- [x] Supervisor остаётся read-only и advisory; контракт «Core решает» не нарушен — ни одна правка не добавила слою решения, а сбор отчёта обёрнут best-effort, как остальной слой.
- [x] **(за оператором, нужен живой прогон — но без порогов)** Метрики P2 нет: фаза не про экономию. Проверить стоит одно и глазами: что `supervisor_usage` в `summary.json` первого же прогона выглядит осмысленно (`observe` против `finalize`) и что `cost` не `null` там, где провайдер его отдаёт.

## Тесты под замену/добавление

- `tests/core/test_flow_recorder.py` — step-запись как источник правды: `fell_back_from` на четырёх комбинациях колонок, `collect_step_facts` (порядок, сообщение из `.out.md`, отсутствующий файл, строка без `id`), дословность сообщения.
- `tests/core/test_supervisor.py` — **точный набор ключей** шага пакета (чистая agent-нода, skipped, subtask), пробельный `.out.md` → ключа `message` нет; метки функций сквозь четыре фазы; отчёт в `summary.json`; отсутствие телеметрии в `summary.md`.
- `tests/core/test_observe_cadence.py` — `triggers_for` на новой подписи (`fell_back`).
- `tests/state/test_state_store.py` — round-trip колонки, инвариант «метка ⟺ `node_run_id IS NULL`», тождество сходимости одним запросом.
- `tests/core/test_supervisor_usage.py` (новый) — `summarize` как чистая функция: пустой вход, только-Codex (`cost: null`), смешанные функции и порядок бакетов, непарсящиеся таймстемпы, неизвестная метка.
- `tests/core/test_orchestrator.py` — на чистом прогоне единственный вызов слоя помечен `finalize`, а attempt'ы нод графа не помечены вовсе.

## Области реализации (по факту)

- `src/wastech_orchestrator/core/flow/recorder.py` — `StepFacts`, `fell_back_from`, `step_facts`, `collect_step_facts` рядом с `StateStoreRunRecorder` (решение P2-D2).
- `src/wastech_orchestrator/core/supervisor_packet.py` — `PacketFacts.steps`, `_steps` как чистый рендер.
- `src/wastech_orchestrator/core/supervisor.py` — сборка фактов уехала в рекордер; метка функции на provider-вызовах; `supervisor_usage` в `summary.json`; `get_provider_attempts_for_task` добавлен в `SupervisorStorePort` (первый продакшн-потребитель ридера).
- `src/wastech_orchestrator/core/supervisor_usage.py` (новый) — `SupervisorFunction` + `summarize`.
- `src/wastech_orchestrator/core/observe_cadence.py`, `core/orchestrator.py` — гейт наблюдений читает вывод рекордера.
- `src/wastech_orchestrator/core/flow/observability.py`, `state_store.py` — метка на attempt-строке, колонка + bump `DB_SCHEMA_VERSION` 19 → 20 (решение P2-D3).
- `.importlinter` — контракт `step-record-below-supervisor` (отклонение 4).
- `src/wastech_orchestrator/packaged/guide/flows/roles.md`, `guide/config/reference.md`, `guide/footprint.md` — разделённые обязанности и где оператор читает расход слоя. Packaged role-промпты проверены и **не** менялись: состав пакета тот же, инструкция «читай факты из `packet`» в силе, а `roles/supervisor.md` уже говорит, что детерминированные шаги «recorded for you rather than observed». Derived `docs/worc_architecture.md` / `docs/configuration.md` на `dev` отсутствуют: только doc-impact note в PR (X2).
