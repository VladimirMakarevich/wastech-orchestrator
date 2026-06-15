# B17 — Router агентов и политика fallback

## Назначение

Слой между ядром-конвейером и адаптерами провайдеров. Для каждой агентской стадии выбирает пару
`(primary, fallback)`, запускает primary и — **только при инфраструктурных ошибках** (плюс условный
случай auth/permission) — переключается на fallback, считая попытки. Реализует инвариант «fallback
только для инфраструктурных ошибок; качественный провал уходит в fixing, а не на другого провайдера».

## Ответственность

- Разрешить маршрут стадии из `agents.routing` + валидированного task-override, зафиксировать
  источник маршрута ([router.py:133-169](../../../src/wastech_orchestrator/routing/router.py#L133)).
- Решить, допускает ли поднятая ошибка fallback (`fallback_allowed`)
  ([router.py:58-73](../../../src/wastech_orchestrator/routing/router.py#L58)).
- Прогнать последовательность провайдеров, считая `stage_attempts` (ограничено
  `max_stage_attempts`), и вернуть `StageOutcome` ([router.py:171-313](../../../src/wastech_orchestrator/routing/router.py#L171)).
- Передать fallback'у частичный дифф предыдущей попытки, **не откатывая** файлы
  ([router.py:271-273,315-322](../../../src/wastech_orchestrator/routing/router.py#L271)).

## Границы блока

### Входит в ответственность блока

- Разрешение маршрута (swap-on-collision), решение о fallback, запуск последовательности, подсчёт
  попыток, передача частичного диффа.

### Не входит в ответственность блока

- **Построение команды CLI и запуск провайдера** — это [B18](./B18-agent-providers.md); Router
  вызывает только `AgentProvider.run` ([router.py:222](../../../src/wastech_orchestrator/routing/router.py#L222)).
- **Переходы состояний и персист** — это [B06](./B06-orchestrator-pipeline.md); Router не меняет
  state machine и хранит состояние только в возвращаемом `StageOutcome`
  ([router.py:12-16](../../../src/wastech_orchestrator/routing/router.py#L12)).
- **Снимок/дифф рабочего дерева** — контракт `SnapshotHook` реализует [B22](./B22-git-manager.md),
  объект передаёт [B06](./B06-orchestrator-pipeline.md) ([snapshots.py:43-56](../../../src/wastech_orchestrator/routing/snapshots.py#L43)).
- **Классификация ошибок** — это [B18](./B18-agent-providers.md); Router лишь потребляет `ErrorClass`.
- **Что делать с качественным `status=failed`** — это [B06](./B06-orchestrator-pipeline.md).

## Точки входа

- `AgentRouter.resolve_route(stage, override=None)` ([router.py:133](../../../src/wastech_orchestrator/routing/router.py#L133)) — [B06](./B06-orchestrator-pipeline.md) `_run_stage` ([orchestrator.py:1728](../../../src/wastech_orchestrator/core/orchestrator.py#L1728)).
- `AgentRouter.run_stage(request, route, *, snapshot=None)` ([router.py:171](../../../src/wastech_orchestrator/routing/router.py#L171)) — [B06](./B06-orchestrator-pipeline.md) ([orchestrator.py:1777](../../../src/wastech_orchestrator/core/orchestrator.py#L1777), `snapshot=self._git`).
- `fallback_allowed(error_class, *, primary_profile, fallback_profile)` ([router.py:58](../../../src/wastech_orchestrator/routing/router.py#L58)) — чистая, отдельно тестируется.
- Конструируется в `build_orchestrator` ([orchestrator.py:2616](../../../src/wastech_orchestrator/core/orchestrator.py#L2616)).

## Входные данные и состояние

`AgentRunRequest` (готовит [B06](./B06-orchestrator-pipeline.md)), `ResolvedRoute`, опц. `SnapshotHook`.
Router держит словарь экземпляров провайдеров и конфиг; иного состояния нет (stateless помимо
возвращаемого `StageOutcome`).

## Основной сценарий (`run_stage`)

1. Снимок «до» через `snapshot.capture()` (если hook передан).
2. Формируется последовательность `[primary]` (+ `fallback`, если он не None).
3. Для каждого провайдера, пока `stage_attempts < max_stage_attempts`: собрать per-attempt запрос,
   увеличить `stage_attempts`, вызвать `provider.run(req)`.
4. Если `run` вернул результат (успех **или** качественный `failed`) — записать попытку и **сразу**
   вернуть `StageOutcome` (fallback не запускается) ([router.py:294-303](../../../src/wastech_orchestrator/routing/router.py#L294)).
5. Если все попытки подняли `ProviderError` — вернуть `StageOutcome(result=None, terminal_error=...)`.

## Альтернативные сценарии

### Инфраструктурный сбой → fallback
`ProviderError` от primary: записать попытку (status=None); если есть следующий провайдер, лимит не
исчерпан и `fallback_allowed(...)` истинно — снять `partial_change_since(before)` и перейти к
fallback'у (его запрос получает `diff_path` частичного диффа) ([router.py:244-273](../../../src/wastech_orchestrator/routing/router.py#L244)).

### Fallback запрещён → терминально для стадии
Если `fallback_allowed` ложно (ошибка не инфраструктурная, либо fallback-профиль слабее) — выйти из
цикла с `result=None` и `terminal_error` ([router.py:248-262](../../../src/wastech_orchestrator/routing/router.py#L248)).

### Override маршрута
Task-override перенацеливает **primary** (после `check_task_route_override`); при коллизии с
настроенным fallback роли меняются местами; `None`-fallback остаётся `None`
([router.py:151-169](../../../src/wastech_orchestrator/routing/router.py#L151)).

## Проверки и ограничения

- `fallback_allowed`: безусловно для `FALLBACK_ELIGIBLE` ([base.py:60-72](../../../src/wastech_orchestrator/providers/base.py#L60)); условно для `authorization_failed`/`permission_denied` — только если fallback-профиль не слабее (`is_same_or_stricter`); никогда для `task_failure`/`configuration_error` ([router.py:69-73](../../../src/wastech_orchestrator/routing/router.py#L69)).
- `permission_profile` fallback'у **никогда не ослабляется** ([router.py:318-319](../../../src/wastech_orchestrator/routing/router.py#L318)).
- `stage_attempts` ограничено `agents.max_stage_attempts`; `max_stage_attempts=1` полностью блокирует fallback.
- `resolve_route` defensively перепроверяет allowed/configured/наличие экземпляра → `ConfigError`
  ([router.py:327-345](../../../src/wastech_orchestrator/routing/router.py#L327)).
- Нет операции отката — частичные изменения не отменяются ([snapshots.py:43-48](../../../src/wastech_orchestrator/routing/snapshots.py#L43)).

## Результат

`StageOutcome`: маршрут, итоговый `result` (или `None`, если все попытки — инфра-сбой),
`provider_used`, `stage_attempts`, `terminal_error`, кортеж `attempts`, `partial_change`. Решения
дальше не принимаются — их принимает [B06](./B06-orchestrator-pipeline.md).

## Побочные эффекты

- Структурированные лог-записи о маршруте и каждой попытке (через [B27](./B27-observability.md)).
- Косвенно: запуск провайдером пишет артефакты (это [B18](./B18-agent-providers.md)).
- Сам Router ничего не пишет в БД/файлы.

## Ошибки и граничные случаи

- Нет маршрута для стадии или недоступный провайдер → `ConfigError` (из `resolve_route`).
- Все попытки инфра-сбойны → `result=None` + `terminal_error`; [B06](./B06-orchestrator-pipeline.md) трактует это как терминальный `failed` стадии.
- Качественный `failed` не считается сбоем Router — он проходит дальше как результат.

## Связи

### Использует

- [B18 — Адаптеры провайдеров](./B18-agent-providers.md) — `AgentProvider.run`, `ErrorClass`, `FALLBACK_ELIGIBLE`.
- [B25 — Security](./B25-security-policy.md) — `is_same_or_stricter` (условный fallback).
- [B05 — Конфигурация](./B05-configuration.md) — маршруты/провайдеры, `check_task_route_override`.
- [B22 — Git Manager](./B22-git-manager.md) — реализация `SnapshotHook` (снимок/частичный дифф).
- [B27 — Наблюдаемость](./B27-observability.md) — структурированный лог попыток.

### Используется в

- [B06 — Конвейер](./B06-orchestrator-pipeline.md) — единственный вызыватель `resolve_route`/`run_stage`.

## Место в общей системе

Router изолирует ядро от провайдеров: ядро готовит запрос и реагирует на `StageOutcome`, а Router
инкапсулирует «попробуй primary, при инфра-сбое — fallback». Это держит инвариант разделения
ответственности (ядро не знает синтаксиса CLI) и инвариант «fallback только для инфраструктуры».

## Подтверждение в коде

- [routing/router.py:58-73](../../../src/wastech_orchestrator/routing/router.py#L58) — таблица решений fallback.
- [routing/router.py:133-169](../../../src/wastech_orchestrator/routing/router.py#L133) — разрешение маршрута + swap-on-collision.
- [routing/router.py:171-313](../../../src/wastech_orchestrator/routing/router.py#L171) — цикл попыток, fallback, `StageOutcome`.
- [routing/snapshots.py:43-56](../../../src/wastech_orchestrator/routing/snapshots.py#L43) — `SnapshotHook` без отката.
- Тесты: [test_fallback_policy.py](../../../tests/routing/test_fallback_policy.py), [test_route_resolution.py](../../../tests/routing/test_route_resolution.py), [test_stage_attempts.py](../../../tests/routing/test_stage_attempts.py), [test_router_integration.py](../../../tests/routing/test_router_integration.py) — eligibility-классы, override/swap, лимит попыток, передача частичного диффа, «качественный failed не вызывает fallback».
