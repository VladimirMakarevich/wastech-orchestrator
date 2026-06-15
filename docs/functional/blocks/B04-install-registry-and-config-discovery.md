# B04 — Реестр привязок и обнаружение конфигурации

## Назначение

Персистентный per-user стор, связывающий корень репозитория с его сгенерированной `config.yaml`, и
логика разрешения пути конфигурации. Позволяет командам (`preflight`/`watch`/`status`/…) найти
конфигурацию из любого места внутри репозитория без `--config`.

## Ответственность

- Хранить и читать привязки `repo-root → config.yaml` в JSON-файле в пользовательском config-каталоге
  ([registry.py:88-104](../../../src/wastech_orchestrator/install/registry.py#L88)).
- Разрешать путь конфигурации по приоритету (`--config` → `./config.yaml` → привязка реестра)
  ([cli.py:549-565](../../../src/wastech_orchestrator/cli.py#L549)).

## Границы блока

### Входит в ответственность блока

- Персистентный стор привязок (bind/lookup/unbind) и разрешение пути конфигурации.

### Не входит в ответственность блока

- **Генерация/валидация конфигурации** — [B03](./B03-installer-and-scaffolding.md)/[B05](./B05-configuration.md).
- **`git_info`** (определение корня репозитория) — [B03 detect](./B03-installer-and-scaffolding.md); `resolve_config_path` лишь использует его.
- **Загрузка конфигурации** — [B05](./B05-configuration.md).

## Точки входа

- `registry.bind(repo_root, config_path)` / `lookup(repo_root)` / `unbind(repo_root)` ([registry.py:88-104](../../../src/wastech_orchestrator/install/registry.py#L88)); `registry_dir`/`registry_path`.
- `cli.resolve_config_path(args)` ([cli.py:549](../../../src/wastech_orchestrator/cli.py#L549)) — используется всеми командами, загружающими конфигурацию.
- Вызовы: `bind` — [B03 cmd_install](./B03-installer-and-scaffolding.md) ([cli.py:1478](../../../src/wastech_orchestrator/cli.py#L1478)); `lookup` — внутри `resolve_config_path`.

## Входные данные и состояние

Корень репозитория и путь конфигурации (оба нормализуются к абсолютным). Состояние —
`registry.json` (`{version, bindings}`) в `$WASTECH_ORCHESTRATOR_HOME` или per-user config-каталоге
(`platformdirs`).

## Основной сценарий

- `bind`: прочитать карту, добавить/заменить `repo_root → config_path` (абсолютные), записать атомарно.
- `lookup`: прочитать карту, вернуть путь или `None`.
- `resolve_config_path`: вернуть `--config`, иначе `./config.yaml` (если есть), иначе
  `registry.lookup(git_info.root)`, иначе `None`.

## Проверки и ограничения

- Ключи нормализуются к абсолютным путям (resolve символлинков/регистра) ([registry.py:44-46](../../../src/wastech_orchestrator/install/registry.py#L44)).
- Запись атомарна (temp + `os.replace`); чтение **forward-tolerant**: игнорирует `version`,
  missing/corrupt → `{}` (обнаружение конфигурации не должно падать на реестре от более новой версии)
  ([registry.py:49-85](../../../src/wastech_orchestrator/install/registry.py#L49)).
- Секреты не хранятся — только пути.

## Результат

Путь к `config.yaml` (или `None`); обновлённый `registry.json`.

## Побочные эффекты

- Чтение/запись `registry.json` в пользовательском config-каталоге. Никаких секретов.

## Ошибки и граничные случаи

- Отсутствующий/битый реестр → пустая карта (без ошибки).
- `resolve_config_path` вне git-репозитория без `./config.yaml`/`--config` → `None` (вызывающий печатает подсказку).

## Связи

### Использует

- `platformdirs`; [B03 detect.git_info](./B03-installer-and-scaffolding.md) (в `resolve_config_path`).

### Используется в

- [B01 — CLI](./B01-cli-and-operator-commands.md) — `resolve_config_path` во всех командах, загружающих конфигурацию.
- [B03 — Установщик](./B03-installer-and-scaffolding.md) — `bind` при `install`.

## Место в общей системе

Связующее звено между установкой и последующими командами: `install` записывает привязку, а любая
команда затем находит конфигурацию из любой поддиректории репозитория. Толерантность чтения держит
обнаружение конфигурации устойчивым между версиями.

## Подтверждение в коде

- [install/registry.py:31-104](../../../src/wastech_orchestrator/install/registry.py#L31) — пути, чтение/запись, bind/lookup/unbind.
- [cli.py:549-565](../../../src/wastech_orchestrator/cli.py#L549) — `resolve_config_path` (приоритет источников).
- Тесты: [tests/install/test_registry.py](../../../tests/install/test_registry.py), [tests/test_cli_config_discovery.py](../../../tests/test_cli_config_discovery.py) — roundtrip bind/lookup, нормализация, версионированный JSON, толерантность к битому файлу, приоритет разрешения.
