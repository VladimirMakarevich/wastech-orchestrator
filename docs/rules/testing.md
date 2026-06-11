# Правила тестирования

Источник истины — [orchestrator_final_plan.md §14](../../orchestrator_final_plan.md).

## Уровни

### Unit
Покрывают чистую логику без внешних процессов:
- валидация конфигурации и task overrides;
- route resolution и allowlist;
- command builder каждого провайдера (без реального запуска CLI);
- парсинг structured output;
- классификация ошибок (`ProviderError` → класс);
- переходы state machine;
- redaction секретов и нормализация путей;
- лимиты retry / fallback / fix-циклов.

### Integration
Используют **fake CLI executables** (скрипты-заглушки), а не реальные Codex/Claude:
- успешный запуск;
- `binary_not_found`, `authentication_failed`, `rate_limited`, `timeout`, `process_crashed`, malformed output;
- инфраструктурная ошибка **после** изменения файлов;
- успешный fallback;
- запрет fallback при quality failure.

### End-to-end
На временном Git-репозитории:
- Claude выполняет planning/implementation, Codex — review;
- упавшие checks запускают `fixing`;
- успех → ровно один commit, push и PR;
- рестарт не дублирует публикацию;
- исчерпание attempts → `failed`.

## Принципы

- Тесты детерминированны и изолированы (никаких сетевых вызовов и реальных CLI в unit/integration).
- Внешние процессы и время мокируются/инжектируются.
- Каждое изменение поведения сопровождается тестом.
- Цель — высокий охват критичных путей (router, fallback, state machine, security/redaction), а не процент ради процента.
- `pytest` зелёный — обязательное условие коммита и перехода между стадиями реализации.
