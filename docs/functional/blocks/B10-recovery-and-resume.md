# B10 — Восстановление и возобновление

## Назначение

На старте сверяет персистентное состояние (SQLite ↔ ветка ↔ артефакты) и решает, что делать с
единственной незавершённой операцией: возобновить, завершить прерванную очистку, пометить как
требующую ручного вмешательства, или ничего (слот свободен). Это «мозг» идемпотентного перезапуска —
само действие выполняет [B06](./B06-orchestrator-pipeline.md).

## Ответственность

- Найти активные задачи и применить правило единого слота §8.2
  ([recovery.py:57-74](../../../src/wastech_orchestrator/core/recovery.py#L57)).
- Для декомпозированной задачи сверить записанные коммиты сабтасков с веткой и найти точку
  возобновления ([recovery.py:76-107](../../../src/wastech_orchestrator/core/recovery.py#L76)).
- Вернуть решение `RecoveryPlan` (NONE/RESUME/CLEANUP/MANUAL) — без побочных действий.

## Границы блока

### Входит в ответственность блока

- Вычисление решения о восстановлении и точки возобновления; детект несогласованности.

### Не входит в ответственность блока

- **Выполнение** решения — это [B06](./B06-orchestrator-pipeline.md) (`resume` → `_resume_task`/`_resume_cleanup`/`_resume_manual`).
- **Проверка коммита на ветке** делегируется [B22 `commit_on_branch`](./B22-git-manager.md).
- **Чтение состояния** — [B07](./B07-state-machine-and-store.md); сам блок ничего не пишет.
- **Идемпотентность публикации** — [B22](./B22-git-manager.md) (fingerprints publish-операций).

## Точки входа

- `RecoveryReconciler(config, store, git).reconcile()` → `RecoveryPlan` ([recovery.py:49-74](../../../src/wastech_orchestrator/core/recovery.py#L49)); строится и вызывается в [B06 `resume`](./B06-orchestrator-pipeline.md) ([orchestrator.py:661](../../../src/wastech_orchestrator/core/orchestrator.py#L661)).
- `RecoveryAction`, `RecoveryPlan` — типы решения.

## Входные данные и состояние

Конфиг, `StateStore`, `GitManager`. Решение основано на: списке активных задач, наличии незавершённой
очистки, и (для декомпозиции) записях сабтасков + их коммитах на ветке. Состояния не хранит.

## Основной сценарий (`reconcile`)

1. `> 1` активной задачи → `MANUAL` (неоднозначно, §8.2) со списком id.
2. Ровно 1 активная: если декомпозирована → `reconcile_decomposed`; иначе → `RESUME`.
3. `0` активных: если есть задача с незавершённой очисткой → `CLEANUP`; иначе → `NONE` (слот свободен).

## Альтернативные сценарии

### Сверка декомпозиции
Для каждого сабтаска с записанным `commit_sha`: если коммита нет на ветке → `MANUAL`
(несогласованность). Если закоммичено больше, чем `subtasks_completed` → `MANUAL`. Иначе точка
возобновления = `committed + 1` → `RESUME` ([recovery.py:76-107](../../../src/wastech_orchestrator/core/recovery.py#L76)).

## Проверки и ограничения

- Единый слот: больше одной активной задачи всегда `MANUAL`.
- Никогда не пере-коммитить записанный SHA и не продолжать при обнаруженной несогласованности —
  fail-safe в `MANUAL` ([recovery.py:8-16](../../../src/wastech_orchestrator/core/recovery.py#L8)).

## Результат

`RecoveryPlan(action, task_id, resume_subtask, manual_reason, manual_task_ids)`.

## Побочные эффекты

Нет — блок только читает состояние и возвращает решение.

## Ошибки и граничные случаи

- Несогласованность коммитов/счётчиков сабтасков → `MANUAL` с понятной причиной.
- Прерванная терминальная очистка (терминальный статус + ветка + `cleanup_completed` не выставлен) →
  `CLEANUP`.

## Связи

### Использует

- [B07 — State Store](./B07-state-machine-and-store.md) — `find_active_tasks`, `get_subtasks`, `find_incomplete_cleanup`.
- [B22 — Git Manager](./B22-git-manager.md) — `commit_on_branch`.
- [B05 — Конфигурация](./B05-configuration.md) — типы конфигурации.

### Используется в

- [B06 — Конвейер](./B06-orchestrator-pipeline.md) — `resume` и `rerun --continue` (через оживление задачи).

## Место в общей системе

Делает перезапуск безопасным: при старте `watch`/`run`/`continue` [B06](./B06-orchestrator-pipeline.md)
сначала спрашивает этот блок, и лишь затем продолжает единственную незавершённую задачу или
освобождает слот. Совместно с идемпотентностью [B22](./B22-git-manager.md) обеспечивает свойство
«падение не повреждает состояние» (§13).

## Подтверждение в коде

- [core/recovery.py:49-107](../../../src/wastech_orchestrator/core/recovery.py#L49) — `reconcile` и `reconcile_decomposed`.
- Тест: [tests/core/test_recovery.py](../../../tests/core/test_recovery.py) — NONE/RESUME/CLEANUP/MANUAL, сверка декомпозиции, точка возобновления.
