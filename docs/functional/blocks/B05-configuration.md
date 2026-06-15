# B05 — Конфигурация: схема, загрузка, валидация, апгрейд

## Назначение

Типизированная модель `config.yaml` и весь её жизненный цикл «как данных»: структурный парсинг YAML
в датаклассы (fail-closed), семантическая валидация правил §11/§21.4 и миграция ключей между
версиями схемы. Конфигурация задаёт поведение всей системы (провайдеры, маршруты, лимиты циклов,
безопасность, footprint, проверки, telegram, промпты, навыки).

## Ответственность

- Задать формы конфигурации (frozen-датаклассы по блокам) и инварианты-перечисления
  ([schema.py:105-334](../../../src/wastech_orchestrator/config/schema.py#L105)).
- Распарсить YAML в типизированный `OrchestratorConfig`, собирая все проблемы и поднимая
  `ConfigError` ([loader.py:778-808](../../../src/wastech_orchestrator/config/loader.py#L778)).
- Проверить семантические правила §11/§21.4 и поднять `ConfigError`
  ([validation.py:69-121](../../../src/wastech_orchestrator/config/validation.py#L69)).
- Проверить per-task route-override (чистая функция, без исключений)
  ([validation.py:218-240](../../../src/wastech_orchestrator/config/validation.py#L218)).
- Слить упакованный шаблон в конфиг оператора (add-missing-only), убрать удалённые ключи, проставить
  версию ([upgrade.py:58-120](../../../src/wastech_orchestrator/config/upgrade.py#L58)).

## Границы блока

### Входит в ответственность блока

- Модель данных (shapes), структурный парсинг (типы, неизвестные ключи, версия), семантическая
  валидация, миграция ключей, привязка относительного `templates_dir` к каталогу конфига.

### Не входит в ответственность блока

- **Поиск файла конфигурации** (`resolve_config_path`) и реестр привязок — это [B04](./B04-install-registry-and-config-discovery.md).
- **Атомарная запись/бэкап** файла конфига — это драйверы CLI/установщика ([B03](./B03-installer-and-scaffolding.md)/[B01](./B01-cli-and-operator-commands.md)).
- **Определение запрещённых флагов** — делегируется [B25 `find_forbidden_args`](./B25-security-policy.md) ([validation.py:46](../../../src/wastech_orchestrator/config/validation.py#L46)).
- **Нормализация/проверка команд проверок** — делегируется [B23 (checks.model)](./B23-check-discovery.md) ([validation.py:17-22,154-166](../../../src/wastech_orchestrator/config/validation.py#L154)).
- **Использование** значений (запуск, маршрутизация) — это блоки-потребители.

## Точки входа

- `load_config(path)` / `loads_config(text)` → `ConfigLoadResult` ([loader.py:811,795](../../../src/wastech_orchestrator/config/loader.py#L795)); `ConfigError` ([loader.py:67](../../../src/wastech_orchestrator/config/loader.py#L67)).
- `validate_config(config)` → warnings | raise ([validation.py:69](../../../src/wastech_orchestrator/config/validation.py#L69)); `check_task_route_override(override, config)` ([validation.py:218](../../../src/wastech_orchestrator/config/validation.py#L218)).
- `upgrade_config_mapping` / `parse_mapping` / `packaged_template_mapping` / `render` ([upgrade.py](../../../src/wastech_orchestrator/config/upgrade.py)).
- `OrchestratorConfig` + блочные датаклассы + `ROUTABLE_STAGES`/`SKIPPABLE_STAGES`/`CONFIG_SCHEMA_VERSION` ([schema.py](../../../src/wastech_orchestrator/config/schema.py)).
- Вызовы: CLI `_load_config` (load + validate, fail-closed) ([cli.py:542-546](../../../src/wastech_orchestrator/cli.py#L542)); `cmd_upgrade_config` ([cli.py:568](../../../src/wastech_orchestrator/cli.py#L568)); [B16 шлюз](./B16-task-parsing-and-validation-gate.md) и [B17 Router](./B17-agent-router-and-fallback.md) — `check_task_route_override`.

## Входные данные и состояние

Текст/путь `config.yaml`; для апгрейда — упакованный `templates/config.example.yaml`. Состояние не
хранится — каждый вызов независим, без побочных эффектов на импорте.

## Основной сценарий (загрузка + валидация)

1. `loads_config`: `yaml.safe_load`; не-mapping корень или YAML-ошибка → `ConfigError`.
2. `_parse`: проверка неизвестных верхнеуровневых ключей и `schema_version`; сборка каждого блока
   типизированными ридерами (несовпадение типа → проблема + безопасный дефолт).
3. Если есть проблемы — `ConfigError` со всем списком; иначе `ConfigLoadResult(config, warnings)`.
4. `load_config` дополнительно привязывает относительный `prompts.templates_dir` к каталогу конфига.
5. CLI вызывает `validate_config` (семантика) как fail-closed-шлюз перед использованием.

## Альтернативные сценарии

### Легаси-конфиг без `agents.routing`
Отсутствие блока маршрутизации → авто-миграция к Codex-маршруту для всех `ROUTABLE_STAGES` + warning
([loader.py:476-485,388-392](../../../src/wastech_orchestrator/config/loader.py#L476)).

### Удалённые ключи (schema v6)
`prompts.overrides`/`prompts.strict` на загрузке толерируются (игнор) с warning; `upgrade-config`
вырезает их ([loader.py:711-728](../../../src/wastech_orchestrator/config/loader.py#L711), [upgrade.py:27-30,77-89](../../../src/wastech_orchestrator/config/upgrade.py#L27)).

### Апгрейд конфига
`upgrade_config_mapping`: рекурсивный merge шаблона в конфиг оператора — значения оператора всегда
побеждают, добавляются только отсутствующие ключи (в т.ч. новые под-ключи), удалённые вырезаются,
`schema_version` ставится текущим ([upgrade.py:92-112](../../../src/wastech_orchestrator/config/upgrade.py#L92)).

## Проверки и ограничения

- **Структурные** (loader): не-mapping корень, неизвестные ключи (верх и блоки), неизвестный
  stage/provider/enum, неверные типы → `ConfigError`; `schema_version` новее текущего (=6) →
  `ConfigError` ([loader.py:758-775](../../../src/wastech_orchestrator/config/loader.py#L758)).
- **Семантические** (validation): маршруты только для `ROUTABLE_STAGES`, primary/fallback ∈
  `agents.allowed` и есть в `agents.providers`; `poll_interval_seconds ≥ 0`;
  `max_total_fix_iterations ≥ max_fix_cycles`; `decomposition.max_subtasks ≥ 2`; `extra_args` без
  bypass-флагов; footprint-пары (external несовместим с exclude_local/commit; in_repo требует
  tracking ≠ none) и анти-traversal `external_root` вне `repo.local_path`; команды проверок —
  argv без shell-метасимволов, без bypass-флагов, не из `denied_commands`; telegram timeout > 0 и
  валидные имена env-переменных ([validation.py:80-216](../../../src/wastech_orchestrator/config/validation.py#L80)).
- `check_task_route_override` — те же allowed/configured/routable-проверки, но **чистая** (возвращает
  список проблем, ничего не поднимает) ([validation.py:228-240](../../../src/wastech_orchestrator/config/validation.py#L228)).

## Результат

`ConfigLoadResult(config, warnings)`; список warnings из `validate_config` (например, `disabled`
discovery); `(merged, added, removed)` из апгрейда; YAML-текст из `render`. Сам блок ничего не
записывает (запись — у вызывающих).

## Побочные эффекты

- `load_config` читает файл; `packaged_template_mapping` читает упакованный шаблон. Прочие функции —
  чистые. Запись файла конфига выполняется не здесь.

## Ошибки и граничные случаи

- Любая структурная/семантическая проблема → `ConfigError(issues)` со **всем** списком (не первая).
- Частичный конфиг догружается безопасными дефолтами §11 (если не нарушает семантику).
- `schema_version` новее → fail-closed (CLI печатает сообщение + выход 2).

## Связи

### Использует

- [B25 — Security](./B25-security-policy.md) — `find_forbidden_args` (валидация `extra_args` и команд).
- [B23 — Проверки](./B23-check-discovery.md) — `checks.model` (`normalize_check_command`, `argv_matches_denied`, `shell_metachars`) при валидации команд.
- PyYAML.

### Используется в

- [B01 — CLI](./B01-cli-and-operator-commands.md) — `_load_config`, `cmd_upgrade_config`.
- [B06 — Конвейер](./B06-orchestrator-pipeline.md) и почти все блоки — читают типы `OrchestratorConfig`.
- [B16](./B16-task-parsing-and-validation-gate.md), [B17](./B17-agent-router-and-fallback.md) — `check_task_route_override`, `ROUTABLE_STAGES`/`SKIPPABLE_STAGES`.
- [B03 — Установщик](./B03-installer-and-scaffolding.md) — `loads_config` + `validate_config` (проверка сгенерированного конфига), `upgrade.*`.

## Место в общей системе

Конфигурация — единый источник параметров поведения. Fail-closed-загрузка и валидация образуют
config-time половину инварианта «политику безопасности нельзя ослабить»: небезопасный или
противоречивый конфиг не доходит до конвейера. Версионирование позволяет безопасно эволюционировать
формат и мигрировать существующие установки.

## Подтверждение в коде

- [config/schema.py:34-334](../../../src/wastech_orchestrator/config/schema.py#L34) — версия формата, `ROUTABLE_STAGES`/`SKIPPABLE_STAGES`, все блочные датаклассы и перечисления.
- [config/loader.py:778-829](../../../src/wastech_orchestrator/config/loader.py#L778) — `_parse`, `loads_config`, `load_config`, привязка `templates_dir`.
- [config/loader.py:458-502](../../../src/wastech_orchestrator/config/loader.py#L458) — легаси-миграция маршрутизации, дефолты.
- [config/validation.py:69-240](../../../src/wastech_orchestrator/config/validation.py#L69) — семантические правила и `check_task_route_override`.
- [config/upgrade.py:58-120](../../../src/wastech_orchestrator/config/upgrade.py#L58) — merge add-missing, удаление ключей, render.
- Тесты: [test_loader.py](../../../tests/config/test_loader.py), [test_validation.py](../../../tests/config/test_validation.py), [test_upgrade.py](../../../tests/config/test_upgrade.py), [test_config_schema_version.py](../../../tests/config/test_config_schema_version.py), [test_roundtrip.py](../../../tests/config/test_roundtrip.py), [test_checks_discovery.py](../../../tests/config/test_checks_discovery.py).
