# Интерактивный установщик проекта

> **Статус: реализовано.** Поставляется как команда `wastech-orchestrator install`. См. §20.4 канонической спецификации ([implementation_stages/00_orchestrator_final_plan.md](../implementation_stages/00_orchestrator_final_plan.md)) и [operations.md](../operations.md). Этот файл сохранён как исходная проектная заметка.

## Кратко

Добавить двухэтапную установку:

```powershell
pipx install "git+https://github.com/VladimirMakarevich/wastech-orchestrator.git"
cd C:\projects\my-repo
wastech-orchestrator install .
```

На macOS используются те же команды. `install .` привязывает текущий Git-репозиторий, создаёт соседний control workspace и запускает интерактивную настройку.

## Реализация

- Добавить команду:

```text
wastech-orchestrator install [repo-path]
  --workspace PATH
  --provider auto|codex|claude|both
  --check COMMAND
  --create-pr / --no-create-pr
  --auto-mode / --no-auto-mode
  --non-interactive
  --reconfigure
  --skip-preflight
  --dry-run
```

- Мастер последовательно:
  1. Определяет Git root, `origin`, текущую/base-ветку и чистоту репозитория.
  2. Предлагает соседний workspace `<repo-name>-orchestrator`.
  3. Находит `codex`, `claude` и предлагает доступную маршрутизацию.
  4. Определяет checks по `pyproject.toml`, `package.json`, `Cargo.toml` или `go.mod`; позволяет подтвердить или ввести команды по одной.
  5. Спрашивает про автоматическое создание PR. При отсутствии `gh` значение по умолчанию `false`.
  6. Спрашивает про auto mode, по умолчанию `false`.
  7. Показывает итоговую конфигурацию и просит подтверждение.

- Генерировать `config.yaml` структурно через YAML:
  - только выбранные providers;
  - безопасный `workspace-write`;
  - in-repo audit footprint (`location: in_repo`, `tracking: commit`): задачи и артефакты хранятся в самом репозитории;
  - абсолютные native Windows/macOS пути;
  - найденные remote и base branch;
  - неизменяемые безопасные security defaults;
  - никаких секретов или credentials.

- Не изменять tracked-файлы целевого репозитория при установке: каталоги задач/логов создаются **пустыми** (git их не видит). `config.yaml` и карантин отклонённых задач (`tasks/rejected/`) лежат в соседнем workspace, вне репозитория; `tasks/`, `logs/` и SQLite-состояние — в самом репозитории (артефакты коммитятся оркестратором во время задач, а `state.db`/`config.yaml` исключены из коммитов).

- Сохранять привязку `repo root -> config.yaml` в пользовательском системном каталоге через `platformdirs`:
  - Windows: `%LOCALAPPDATA%`;
  - macOS: `~/Library/Application Support`;
  - Linux: XDG config directory.

- Изменить поиск конфигурации:
  1. Явный `--config`.
  2. Существующий `./config.yaml` для обратной совместимости.
  3. Привязка текущего Git-репозитория из registry.
  4. Иначе сообщение с предложением выполнить `install .`.

- После установки команды из целевого репозитория работают без WSL-путей и без `--config`:

```text
wastech-orchestrator preflight
wastech-orchestrator watch
wastech-orchestrator status
```

- `watch` берёт `tasks/pending` из настроенного artifact root, а не из текущей директории.

- Повторный `install` идемпотентен. `--reconfigure` создаёт timestamped backup и атомарно заменяет конфигурацию. Чужой существующий workspace без binding не перезаписывается.

- После успешной записи автоматически запускать preflight. Ошибка preflight оставляет созданную конфигурацию, но возвращает ненулевой exit code и конкретные инструкции.

- Не устанавливать и не авторизовывать Codex, Claude или GitHub CLI автоматически. Установщик только обнаруживает их и сообщает, что отсутствует.

## Тесты

- Интерактивный и `--non-interactive` сценарии.
- Codex-only, Claude-only и mixed routing.
- Автоопределение Python/Node/Cargo/Go checks.
- Windows- и macOS-пути корректно проходят YAML round-trip.
- Registry, приоритет `--config` и поиск binding из вложенной директории.
- Идемпотентный повторный install и `--reconfigure` с backup.
- `--dry-run` ничего не записывает.
- Целевой Git-репозиторий остаётся без изменений.
- Отсутствующие provider/`gh`, невалидный remote и failed preflight.
- Существующие тесты `init`, `run`, `watch`, `status`.
- Полный `ruff check .`, `mypy src`, `pytest`.

## Документация

Обновить README, cookbook, operations, configuration и каноническую спецификацию:

- установка через `pipx` на Windows/macOS;
- различие между установкой CLI и `install .` для проекта;
- полный сценарий мастера;
- расположение workspace и registry;
- повторная настройка;
- предупреждение, что `--no-create-pr` отключает PR, но не commit/push.
