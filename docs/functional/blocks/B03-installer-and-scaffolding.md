# B03 — Установщик и развёртывание проекта

## Назначение

Создаёт и поддерживает on-disk установку оркестратора: `init` (скелет каталогов/шаблонов/доков/конфига),
`install` (мастер → привязка репозитория к sibling-воркспейсу + генерация валидной `config.yaml`), и
команды поддержки (`upgrade-config`, `upgrade-docs`, `install-templates`). Детекция окружения —
read-only.

## Ответственность

- `init`: идемпотентно создать runtime-каталоги (`.gitkeep`), `config.yaml` из упакованного примера с
  выбранным git-режимом, дерево `templates/`, доки `worc/`, runtime-excludes ([cli.py:464-539](../../../src/wastech_orchestrator/cli.py#L464)).
- `install`: прогнать мастер → `InstallSpec`, сгенерировать+провалидировать `config.yaml`, создать
  каталоги, привязать репозиторий, авто-preflight ([cli.py:1430-1494](../../../src/wastech_orchestrator/cli.py#L1430)).
- Мастер: детекция git/провайдеров/проверок, разрешение настроек, hard-stops ([wizard.py:68-122](../../../src/wastech_orchestrator/install/wizard.py#L68)).
- Генерация конфигурации с безопасными дефолтами + round-trip-валидация ([config_writer.py:100-195](../../../src/wastech_orchestrator/install/config_writer.py#L100)).
- Read-only детекция окружения ([detect.py:64-163](../../../src/wastech_orchestrator/install/detect.py#L64)).

## Границы блока

### Входит в ответственность блока

- Скелет проекта, мастер установки, генерация+валидация конфигурации, детекция, команды апгрейда/доставки шаблонов.

### Не входит в ответственность блока

- **Модель/правила конфигурации** — [B05](./B05-configuration.md) (`loads_config`/`validate_config`/`upgrade`).
- **Стор привязок** — [B04](./B04-install-registry-and-config-discovery.md) (`registry.bind`).
- **git-операции конвейера** — [B22](./B22-git-manager.md) (используется только `append_runtime_excludes`).
- **Запуск задач** — [B06](./B06-orchestrator-pipeline.md).

## Точки входа

- `cmd_init`/`cmd_install`/`cmd_upgrade_config`/`cmd_upgrade_docs`/`cmd_install_templates` — диспетчер [B01](./B01-cli-and-operator-commands.md).
- `wizard.run_wizard(...)` → `WizardOutcome` ([wizard.py:68](../../../src/wastech_orchestrator/install/wizard.py#L68)).
- `config_writer.build_and_validate(spec)` ([config_writer.py:186](../../../src/wastech_orchestrator/install/config_writer.py#L186)).
- `detect.git_info`/`detect_providers`/`detect_checks`/`has_gh`/`require_gh` ([detect.py](../../../src/wastech_orchestrator/install/detect.py)) — `require_gh` также из [B01/B02](./B01-cli-and-operator-commands.md) при `run`/`watch`.

## Входные данные и состояние

CLI-флаги (`--git-mode`, `--workspace`, `--provider`, `--check`, `--create-pr`, `--auto-mode`,
`--non-interactive`, `--reconfigure`, `--dry-run`, …); окружение оператора (git, PATH, экосистема
репозитория); упакованные `templates/`, `worc/`, `config.example.yaml`. Постоянного состояния нет
(создаёт файлы/привязку).

## Основной сценарий (`install`)

1. `run_wizard`: проверить git; `git_info` (корень/origin/ветка/чистота); разрешить
   workspace/провайдеров/проверки/create_pr/auto_mode → `InstallSpec` (+подтверждение).
2. `build_and_validate(spec)`: собрать dict с безопасными дефолтами, отрендерить YAML, round-trip через
   `loads_config`+`validate_config`.
3. Создать runtime-каталоги (in-repo) + quarantine (workspace); атомарно записать `config.yaml`.
4. `registry.bind(repo → config)` ([B04](./B04-install-registry-and-config-discovery.md)); скопировать `worc/`; добавить runtime-excludes ([B22](./B22-git-manager.md)).
5. Сидировать профиль проверок (опц. агентский резолвинг) и авто-preflight ([cli.py:1390-1494](../../../src/wastech_orchestrator/cli.py#L1390)).

## Альтернативные сценарии

### `init`
Скелет без мастера: каталоги + `config.yaml` (с `--git-mode`) + `templates/` + `worc/` + excludes;
идемпотентно (skip-existing), `--force`/`--dry-run`/`--quiet` ([cli.py:464-539](../../../src/wastech_orchestrator/cli.py#L464)).

### Апгрейд/доставка шаблонов
`upgrade-config` (через [B05 upgrade](./B05-configuration.md): add-missing + бэкап + атомарная запись),
`upgrade-docs` (overwrite `worc/` упакованной версией), `install-templates` (add-missing-only) ([cli.py:568-731](../../../src/wastech_orchestrator/cli.py#L568)).

### Повторный install
Без `--reconfigure`: no-op при привязке к этому же конфигу; отказ при чужой привязке; с `--reconfigure`
— бэкап + перегенерация ([cli.py:1461-1473](../../../src/wastech_orchestrator/cli.py#L1461)).

## Проверки и ограничения

- Hard-stops мастера → `InstallError`: нет git/не репозиторий/нет origin/workspace внутри репозитория/нет
  доступного провайдера/отмена ([wizard.py:80-135,150-169](../../../src/wastech_orchestrator/install/wizard.py#L80)).
- `build_and_validate` fail-closed: сгенерированный конфиг обязан загрузиться и пройти §11/§21.4 ([config_writer.py:186-195](../../../src/wastech_orchestrator/install/config_writer.py#L186)).
- Безопасные дефолты «зашиты»: strict_isolation, denied-команды/пути, in_repo/commit footprint, auto_merge off ([config_writer.py:127-177](../../../src/wastech_orchestrator/install/config_writer.py#L127)).
- Детекция git — argv через [B19](./B19-subprocess-runner.md) (без shell), с таймаутом ([detect.py:41-61](../../../src/wastech_orchestrator/install/detect.py#L41)).

## Результат

Созданные каталоги/файлы (`config.yaml`, `templates/`, `worc/`, `.gitkeep`), привязка
репозиторий→конфиг, runtime-excludes; результат авто-preflight. Коды возврата печатает [B01](./B01-cli-and-operator-commands.md).

## Побочные эффекты

- Создание каталогов и файлов; атомарная запись `config.yaml` (+ бэкап при reconfigure/upgrade);
  привязка в реестре ([B04](./B04-install-registry-and-config-discovery.md)); добавление excludes
  ([B22](./B22-git-manager.md)); read-only git-пробы; авто-preflight (запуск provider.preflight,
  изоляция, проверки, telegram).

## Ошибки и граничные случаи

- `InstallError` → сообщение + ненулевой выход ([B01](./B01-cli-and-operator-commands.md)).
- `--dry-run` ничего не пишет (печатает план).
- Команды апгрейда/доставки fail-closed (выход 2), если не удаётся разрешить расположение установки.

## Связи

### Использует

- [B05 — Конфигурация](./B05-configuration.md) — `loads_config`/`validate_config`/`upgrade`.
- [B04 — Реестр](./B04-install-registry-and-config-discovery.md) — `registry.bind`.
- [B19 — Запуск подпроцессов](./B19-subprocess-runner.md) — git-пробы в `detect`.
- [B22 — Git Manager](./B22-git-manager.md) — `append_runtime_excludes`.
- [B25 — Security](./B25-security-policy.md) — безопасные дефолты в генерируемом конфиге.
- [B18](./B18-agent-providers.md)/[B23](./B23-check-discovery.md) — `build_providers`/резолвер при сидировании проверок.

### Используется в

- [B01 — CLI](./B01-cli-and-operator-commands.md) — диспетчер команд установки/апгрейда; `require_gh` при `run`.
- [B02 — Демон watch](./B02-watch-daemon-and-scheduling.md) — `require_gh` при старте `watch`.
- [B04 — Реестр](./B04-install-registry-and-config-discovery.md) — `git_info` в `resolve_config_path`.

## Место в общей системе

Точка входа в развёртывание: превращает «репозиторий + машину оператора» в готовую, безопасно
сконфигурированную установку, которую затем используют все остальные команды. Round-trip-валидация
гарантирует, что небезопасный конфиг не будет записан.

## Подтверждение в коде

- [cli.py:464-731,1390-1494](../../../src/wastech_orchestrator/cli.py#L464) — `cmd_init`/`cmd_install`/апгрейды.
- [install/wizard.py:68-230](../../../src/wastech_orchestrator/install/wizard.py#L68) — мастер и hard-stops.
- [install/config_writer.py:100-195](../../../src/wastech_orchestrator/install/config_writer.py#L100) — генерация + round-trip-валидация.
- [install/detect.py:64-163](../../../src/wastech_orchestrator/install/detect.py#L64) — read-only детекция.
- Тесты: [tests/install/](../../../tests/install/), [tests/test_cli_init.py](../../../tests/test_cli_init.py), [tests/test_cli_install.py](../../../tests/test_cli_install.py), [tests/test_cli_install_templates.py](../../../tests/test_cli_install_templates.py), [tests/test_cli_upgrade_config.py](../../../tests/test_cli_upgrade_config.py), [tests/test_cli_upgrade_docs.py](../../../tests/test_cli_upgrade_docs.py), [tests/test_cli_preflight.py](../../../tests/test_cli_preflight.py).
