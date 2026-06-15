# B11 — Декомпозиция задачи

## Назначение

Детерминированно решает, разбивать ли задачу на сабтаски, по структурированному выводу стадии
`planning`, и фиксирует артефакты сабтасков. Реализует принцип «агент предлагает — ядро решает»:
планировщик может *рекомендовать* разбиение, но ядро принимает его только по жёсткому правилу (агент
не может ослабить `max_subtasks`, маршруты или безопасность). По умолчанию выключено.

## Ответственность

- Применить правило приёма §5.1 к структурированному выводу planning
  ([decomposition.py:106-145](../../../src/wastech_orchestrator/core/decomposition.py#L106)).
- Записать `subtasks/index.json` и по одному неизменяемому `NN-<slug>.md` на сабтаск
  ([decomposition.py:170-199](../../../src/wastech_orchestrator/core/decomposition.py#L170)).
- Транзакционно обновлять статус/`commit_sha` сабтаска в индексе
  ([decomposition.py:202-224](../../../src/wastech_orchestrator/core/decomposition.py#L202)).

## Границы блока

### Входит в ответственность блока

- Детерминированное решение accept/reject (с кодом причины) и файловые артефакты сабтасков.

### Не входит в ответственность блока

- **Запуск сабтасков** (цикл implement→test→review→fix на единицу) — это [B06](./B06-orchestrator-pipeline.md).
- **Персист сабтасков в SQLite** — это [B07 `insert_subtasks`/`set_subtask_commit`](./B07-state-machine-and-store.md).
- **Резолв `gate_on`** (config `decomposition.enabled` + per-task `decompose` tri-state) — это [B06 `_decomposition_gate_on`](./B06-orchestrator-pipeline.md).
- **Валидация схемы вывода planning** — это [B12 `parse_typed_stage_output`](./B12-hitl-and-typed-output.md); здесь вход перепроверяется защитно.

## Точки входа

- `decide_decomposition(structured_output, *, gate_on, max_subtasks)` → `DecompositionDecision`
  ([decomposition.py:106](../../../src/wastech_orchestrator/core/decomposition.py#L106)) — [B06 `_planning`](./B06-orchestrator-pipeline.md) ([orchestrator.py:1093](../../../src/wastech_orchestrator/core/orchestrator.py#L1093)).
- `write_subtask_artifacts` / `update_subtask_index` ([decomposition.py:170,202](../../../src/wastech_orchestrator/core/decomposition.py#L170)) — [B06](./B06-orchestrator-pipeline.md).
- `SubtaskSpec`, `DecompositionDecision`, коды причин, статусы `SUBTASK_*`.

## Входные данные и состояние

Структурированный вывод planning (`decompose`, `subtasks[]`), флаг `gate_on`, `max_subtasks`.
Состояние — файлы под `logs/<task-id>/subtasks/` (источник для индекса, дополняется SQLite в [B07](./B07-state-machine-and-store.md)).

## Основной сценарий (`decide_decomposition`)

1. `gate_on=False` → один юнит (`gate_off`).
2. Нет mapping / `decompose != True` / нет списка `subtasks` → один юнит (`not_recommended`).
3. `n < 2` или `n > max_subtasks` → один юнит (`n_out_of_range`).
4. Любой сабтаск с некорректными полями → один юнит (`malformed_subtask`).
5. `order` не ровно `1..n`, либо `depends_on` ссылается не строго на более ранние → один юнит
   (`non_linear_dependencies`).
6. Иначе → `accepted` с отсортированными `SubtaskSpec`.

## Проверки и ограничения

- `2 ≤ n ≤ max_subtasks`; `order == 1..n`; `depends_on` — только строго более ранние (линейно, без
  forward/циклов) ([decomposition.py:124-144](../../../src/wastech_orchestrator/core/decomposition.py#L124)).
- Поля сабтаска валидируются по типам; `bool` отвергается там, где ждут `int`
  ([decomposition.py:71-103](../../../src/wastech_orchestrator/core/decomposition.py#L71)).
- `NN-<slug>.md` неизменяемы — никогда не перезаписываются; `index.json` пишется атомарно ([decomposition.py:163-199](../../../src/wastech_orchestrator/core/decomposition.py#L163)).

## Результат

`DecompositionDecision(accepted, reason, n, subtasks)`; на диске — `subtasks/index.json` и спеки
сабтасков. Реальный прогон и персист выполняют [B06](./B06-orchestrator-pipeline.md)/[B07](./B07-state-machine-and-store.md).

## Побочные эффекты

- Запись `subtasks/index.json` и `NN-<slug>.md` под `logs/<task-id>/` (никогда в целевой репозиторий).
- `update_subtask_index` атомарно правит индекс.

## Ошибки и граничные случаи

- Любой дефект структуры → «один юнит» с кодом причины (не исключение).
- `update_subtask_index` при отсутствии порядка в индексе → `KeyError` ([decomposition.py:222-223](../../../src/wastech_orchestrator/core/decomposition.py#L222)).

## Связи

### Использует

- [B20 — Артефакты](./B20-artifact-layout.md) — `task_artifact_dir`.

### Используется в

- [B06 — Конвейер](./B06-orchestrator-pipeline.md) — `planning` (решение), цикл по юнитам, обновление индекса при коммите сабтаска; восстановление декомпозиции на resume.
- [B12 — HITL/типизированный вывод](./B12-hitl-and-typed-output.md) — соседний валидатор вывода planning (схема сабтасков).

## Место в общей системе

Декомпозиция — опциональная под-фаза `planning`. При принятии [B06](./B06-orchestrator-pipeline.md)
прогоняет каждый сабтаск как отдельную единицу (со своим локальным коммитом), а глобальный счётчик
`fix_iterations` ([B09](./B09-fix-loop-control.md)) продолжает копиться, не давая обойти жёсткий стоп.

## Подтверждение в коде

- [core/decomposition.py:106-224](../../../src/wastech_orchestrator/core/decomposition.py#L106) — правило приёма, артефакты, обновление индекса.
- Тест: [tests/core/test_decomposition.py](../../../tests/core/test_decomposition.py) — каждый код причины, линейность зависимостей, неизменяемость спеков.
