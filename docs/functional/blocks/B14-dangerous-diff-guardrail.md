# B14 — Классификация «опасного» диффа

## Назначение

Чистый классификатор изменений рабочего дерева, который определяет, требует ли результат
редактирующей стадии согласования человеком. Выделяет два класса риска: удаление файлов и изменение
манифестов/локов зависимостей. Это «детектор» для guardrail-а редактирующих стадий —
сам guardrail-поток (запрос согласования) реализован в [B06](./B06-orchestrator-pipeline.md).

## Ответственность

- По списку изменений `ChangedPath` определить удаления и затронутые файлы зависимостей
  ([dangerous_diff.py:82-109](../../../src/wastech_orchestrator/core/dangerous_diff.py#L82)).
- Классифицировать риск (`deletion`/`dependency`/`other`) и вернуть точные нормализованные пути.

## Границы блока

### Входит в ответственность блока

- Чистая классификация диффа в `DangerousDiff` (или `None` для обычного диффа).

### Не входит в ответственность блока

- **Получение диффа** — это [B22 `changed_code_entries`](./B22-git-manager.md).
- **Поток согласования** (HITL-запрос, повторный прогон при отказе, проверка покрытия planning-аппрувом)
  — это [B06 `_run_edit_stage_with_guardrail`](./B06-orchestrator-pipeline.md) совместно с [B12](./B12-hitl-and-typed-output.md).

## Точки входа

- `classify_dangerous_diff(entries)` → `DangerousDiff | None` ([dangerous_diff.py:82](../../../src/wastech_orchestrator/core/dangerous_diff.py#L82)) — вызывается в [B06](./B06-orchestrator-pipeline.md) после редактирующих стадий ([orchestrator.py:1902,1965](../../../src/wastech_orchestrator/core/orchestrator.py#L1902)).
- `DangerousDiff` (risk, paths, deleted_paths, dependency_paths).

## Входные данные и состояние

Кортеж `ChangedPath` (status, path, previous_path) из [B22](./B22-git-manager.md). Состояния нет.

## Основной сценарий

1. Для каждой записи: статус `D` (или `R` с `previous_path`) → путь в «удалённые»; базовое имя,
   совпавшее по `fnmatch` со списком манифестов/локов → в «зависимости».
2. Нет ни удалений, ни зависимостей → `None` (обычный дифф).
3. Иначе риск: оба → `other`; только удаления → `deletion`; только зависимости → `dependency`.
4. Возврат `DangerousDiff` с отсортированным объединением путей.

## Проверки и ограничения

- Список паттернов зависимостей охватывает множество экосистем (pyproject/locks, package.json,
  Cargo, go.mod, Gemfile, *.csproj, gradle, …) ([dangerous_diff.py:10-69](../../../src/wastech_orchestrator/core/dangerous_diff.py#L10)).
- Сопоставление по **базовому имени** файла через `fnmatch` ([dangerous_diff.py:112-114](../../../src/wastech_orchestrator/core/dangerous_diff.py#L112)).

## Результат

`DangerousDiff` (или `None`). Пути нормализованы и отсортированы — [B06](./B06-orchestrator-pipeline.md)
сравнивает их с ранее одобренным набором, чтобы не запрашивать согласование повторно для того же набора.

## Побочные эффекты

Нет — чистая функция.

## Ошибки и граничные случаи

- Переименование (`R…`) учитывает `previous_path` как удаление исходного пути ([dangerous_diff.py:90-91](../../../src/wastech_orchestrator/core/dangerous_diff.py#L90)).

## Связи

### Использует

- [B22 — Git Manager](./B22-git-manager.md) — тип `ChangedPath` (вход — из `changed_code_entries`).

### Используется в

- [B06 — Конвейер](./B06-orchestrator-pipeline.md) — guardrail редактирующих стадий (implementation/fixing).
- [B12 — HITL](./B12-hitl-and-typed-output.md) — риск/пути попадают в сигнал согласования.

## Место в общей системе

Часть guardrail-а: после редактирующей стадии [B06](./B06-orchestrator-pipeline.md) классифицирует
дифф этим блоком и, если он опасен и не покрыт согласованием на planning, запрашивает у человека
одобрение через [B12](./B12-hitl-and-typed-output.md)/[B26](./B26-notifications-telegram.md); отказ
даёт одну «безопасную» переработку.

## Подтверждение в коде

- [core/dangerous_diff.py:82-114](../../../src/wastech_orchestrator/core/dangerous_diff.py#L82) — классификация и список паттернов.
- Проверяется через guardrail-тесты конвейера ([tests/core/test_orchestrator.py](../../../tests/core/test_orchestrator.py)) и HITL-тесты ([tests/core/test_hitl.py](../../../tests/core/test_hitl.py)).

## Неопределённости

- Отдельного модульного теста `dangerous_diff` в наборе не обнаружено; поведение подтверждается косвенно через guardrail-сценарии конвейера. Прямой unit-тест классификатора не найден.
