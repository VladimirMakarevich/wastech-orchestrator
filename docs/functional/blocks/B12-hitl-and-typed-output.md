# B12 — HITL и типизированный вывод стадий

## Назначение

Обеспечивает «человека в контуре» (HITL) и строгий разбор структурированного вывода агентских стадий.
Две взаимосвязанные функции: (1) валидировать типизированный вывод `refinement`/`planning` и
извлечь из него сигнал запроса к человеку; (2) персистить и возобновлять долговечные HITL-взаимодействия
как файлы-артефакты, чтобы прерванный запрос пережил рестарт.

## Ответственность

- Задать строгие схемы вывода HITL-стадий и провалидировать вывод независимо от провайдера
  ([hitl.py:96-165](../../../src/wastech_orchestrator/core/hitl.py#L96)).
- Разобрать сигнал `human_input` (вид, текст, риск, нормализованные repo-относительные пути)
  ([hitl.py:168-198](../../../src/wastech_orchestrator/core/hitl.py#L168)).
- Персистить взаимодействие (waiting/answer/consumed/reconsidering) атомарно и уметь его перечитать
  ([hitl.py:308-415](../../../src/wastech_orchestrator/core/hitl.py#L308)).
- Давать детерминированные id взаимодействий (под лимиты Telegram callback) ([hitl.py:290-305](../../../src/wastech_orchestrator/core/hitl.py#L290)).

## Границы блока

### Входит в ответственность блока

- Строгая валидация типизированного вывода + сигнала; durable persist/resume HITL-артефактов;
  детерминированные id; реконструкция `AskHandle` из артефакта.

### Не входит в ответственность блока

- **Транспорт** (отправка/поллинг ответа) — это [B26 `Notifier`](./B26-notifications-telegram.md).
- **Оркестрация round-trip** (когда спросить, ждать, перезапустить стадию) — это [B06](./B06-orchestrator-pipeline.md) (`_run_typed_stage`, `_run_edit_stage_with_guardrail`, `_ask_check_command_approval`).
- **Правила редакции** — [B21](./B21-secret-redaction.md); **каталог артефактов** — [B20](./B20-artifact-layout.md).
- **Классификация опасного диффа** — [B14](./B14-dangerous-diff-guardrail.md); **приём декомпозиции** — [B11](./B11-task-decomposition.md) (здесь лишь валидируется схема сабтасков).

## Точки входа

- `stage_output_schema(stage)` ([hitl.py:96](../../../src/wastech_orchestrator/core/hitl.py#L96)) — кладётся в `AgentRunRequest.output_schema` ([orchestrator.py:1762](../../../src/wastech_orchestrator/core/orchestrator.py#L1762)).
- `parse_typed_stage_output(stage, structured)` → `TypedStageOutput` ([hitl.py:131](../../../src/wastech_orchestrator/core/hitl.py#L131)) — [B06 `_typed_output`](./B06-orchestrator-pipeline.md).
- Взаимодействия: `interaction_path`/`guardrail_interaction_path`/`discovery_interaction_path`, `interaction_id`/`discovery_interaction_id`, `load_interaction`, `write_waiting_interaction`, `write_answer`, `mark_consumed`/`mark_interaction_status`, `reset_pending_interactions`, `consume_pending_interactions`, `handle_from_artifact` — все из [B06](./B06-orchestrator-pipeline.md).
- Типы: `HumanInputSignal`, `TypedStageOutput`, `StageOutputError`.

## Входные данные и состояние

Структурированный вывод стадии; `AskHandle`/`AskResult` от [B26](./B26-notifications-telegram.md);
`artifacts_root`, `task_id`, `stage`, опц. `subtask`/`cycle`. Состояние — JSON-артефакты под
`logs/<task-id>/hitl/`.

## Основной сценарий (типизированный вывод + запрос)

1. `parse_typed_stage_output` строго проверяет набор ключей и типы; для planning — `decompose`/
   `subtasks`/`skills`; извлекает сигнал `human_input` (или `None`).
2. Если сигнал есть, [B06](./B06-orchestrator-pipeline.md) через [B26](./B26-notifications-telegram.md)
   отправляет запрос и пишет `write_waiting_interaction` (status `waiting`, редактированные текст/контекст).
3. `wait_for_answer` ([B26](./B26-notifications-telegram.md)) → `write_answer` (status `answered`/код
   ошибки, редактированный ответ, `approved`).
4. После успешного перезапуска стадии — `mark_consumed`.

## Альтернативные сценарии

### Возобновление после рестарта
`load_interaction` читает артефакт; `waiting`/`transport_error` → можно дождаться/перезапросить;
`answered`/`consumed` → ответ переиспользуется; `handle_from_artifact` восстанавливает `AskHandle`
(строгая валидация полей) ([hitl.py:418-459](../../../src/wastech_orchestrator/core/hitl.py#L418)).

### Continue / Finalize
`reset_pending_interactions` удаляет незавершённые (`waiting`/`transport_error`) артефакты для
`rerun --continue`; `consume_pending_interactions` помечает их `consumed` для `finalize`
([hitl.py:378-415](../../../src/wastech_orchestrator/core/hitl.py#L378)).

## Проверки и ограничения

- Только `refinement`/`planning` могут запрашивать человека ([hitl.py:23,135-136](../../../src/wastech_orchestrator/core/hitl.py#L23)).
- Набор ключей вывода должен быть **точным**; `content` — строка; сигнал: `kind∈{question,approval}`,
  ограниченные `question`/`context`, `risk∈{clarification,deletion,dependency,other}`, пути —
  repo-относительные, без `..`/абсолютных, ≤100 ([hitl.py:140-198,253-260](../../../src/wastech_orchestrator/core/hitl.py#L140)).
- Текст/контекст/ответ редактируются перед записью; запись атомарна (temp+replace) ([hitl.py:336-337,356,462-469](../../../src/wastech_orchestrator/core/hitl.py#L336)).
- Битый артефакт/handle → `StageOutputError` (fail-closed) ([hitl.py:308-315,458-459](../../../src/wastech_orchestrator/core/hitl.py#L308)).

## Результат

`TypedStageOutput(content, human_input, structured, skills)`; JSON-артефакты взаимодействий на диске;
восстановленный `AskHandle`. Содержимое HITL-артефакта — редактированное и аудируемое.

## Побочные эффекты

- Запись/чтение/удаление/пометка JSON-артефактов под `logs/<task-id>/hitl/`.
- Чисто-функциональные `stage_output_schema`/`parse_typed_stage_output` (без IO).

## Ошибки и граничные случаи

- Малформный типизированный вывод → `StageOutputError` (Core превращает в `PipelineFailed`).
- Недоставленный запрос → артефакт со status `transport_error`; ответ-неудача → соответствующий код.
- Путь вне репозитория/с `..` → `StageOutputError` при нормализации.

## Связи

### Использует

- [B26 — Telegram](./B26-notifications-telegram.md) — типы `AskHandle`/`AskKind`/`AskResult`.
- [B21 — Redaction](./B21-secret-redaction.md) — `redact_text` для текста/ответа.
- [B20 — Артефакты](./B20-artifact-layout.md) — `task_artifact_dir`.

### Используется в

- [B06 — Конвейер](./B06-orchestrator-pipeline.md) — round-trip refinement/planning, guardrail редактирующих стадий, согласование изменённого набора проверок.
- [B11 — Декомпозиция](./B11-task-decomposition.md) — соседний разбор вывода planning (схема сабтасков).

## Место в общей системе

Делает паузы «на человека» долговечными: даже если процесс упал во время ожидания ответа, артефакт
позволяет [B06](./B06-orchestrator-pipeline.md) корректно возобновиться. Строгая валидация вывода —
граница доверия к агенту: ядро принимает только то, что прошло схему и нормализацию.

## Подтверждение в коде

- [core/hitl.py:96-260](../../../src/wastech_orchestrator/core/hitl.py#L96) — схемы и валидация типизированного вывода + сигнала.
- [core/hitl.py:263-470](../../../src/wastech_orchestrator/core/hitl.py#L263) — пути/id/persist/resume взаимодействий, `handle_from_artifact`.
- Тесты: [tests/core/test_hitl.py](../../../tests/core/test_hitl.py), [tests/core/test_check_discovery_hitl.py](../../../tests/core/test_check_discovery_hitl.py) — валидация, persist/resume, reset/consume, восстановление handle.
