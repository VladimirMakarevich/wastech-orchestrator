---
name: add-provider
description: Создать новый адаптер кодинг-агента (AgentProvider) для wastech-orchestrator по контракту из providers/base.py. Использовать при добавлении Codex/Claude Code адаптера или другого CLI-провайдера.
---

# add-provider

Скаффолдинг нового provider-адаптера строго по контракту.

## Прежде чем начать

Прочитай:
- `src/wastech_orchestrator/providers/base.py` — контракт `AgentProvider`, структуры запроса/результата, классы ошибок;
- `orchestrator_final_plan.md` §4.3, §4.4, §7 — обязанности адаптера и нормализация ошибок;
- `docs/rules/architecture.md` и `docs/rules/security.md` — инварианты.

## Шаги

1. Создай модуль `src/wastech_orchestrator/providers/<provider>.py` с классом, реализующим `AgentProvider`:
   - `id` = канонический идентификатор (`codex` / `claude`);
   - `preflight()` → `ProviderHealth` (executable, версия, авторизация, нужные возможности; сообщение без секретов);
   - `run(request)` → `AgentRunResult` (или `ProviderError` с корректным `ErrorClass` при инфраструктурном сбое).
2. Построение вызова CLI:
   - **список аргументов**, без `shell=True` и интерполяции пользовательских строк;
   - обязательный таймаут;
   - sandbox/permission profile из request, **без** опций обхода;
   - передавать только allowlisted env (см. security.md).
3. Нормализация:
   - exit code и события → `RunStatus` / `ErrorClass`;
   - structured output (JSONL / stream-json) → `structured_output`;
   - stdout/stderr/event log → пути артефактов (спек §10), redacted request-артефакт.
4. **Запрещено** в адаптере: fallback, изменение state machine, commit/push/PR.
5. Тесты (см. docs/rules/testing.md):
   - unit: command builder, парсинг output, классификация ошибок;
   - integration: fake CLI executable на сценарии успех/timeout/crash/malformed/auth-fail.
6. Прогони `/run-checks`.

## Definition of Done

- класс проходит `isinstance(obj, AgentProvider)` (Protocol runtime-checkable);
- все инфраструктурные сбои возвращают корректный `ErrorClass`;
- нет секретов в логах/артефактах;
- зелёные ruff/mypy/pytest.
