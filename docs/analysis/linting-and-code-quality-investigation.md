# Инвестигейт: линтинг и инструменты качества кода

> **Статус (2026-07-13): Фазы 1–2 реализованы** на ветке `chore/lint-and-code-quality`. Отклонения от этого отчёта, найденные при реализации: (1) утечка контракта была **не одна** — кроме `build_providers`, `core` тянул конкретные адаптеры транзитивно через `security.isolation` (таблица `isolation_reasons`); закрыто инъекцией таблицы из нового composition-root `composition.py` (обе фабрики + `ISOLATION_CHECKS` вынесены туда), контракт `core-not-concrete-adapters` зелёный **без** `ignore_imports`. (2) Числа ниже заявленных (код улучшился): `C901`@15 → **7** нарушителей (не 13), `PLR0913`@8 → **19** (не 30); стартовый порог `PLR0913` взят 10. (3) Покрытие докстрингов по факту **70.8%** (не ~81%) — `interrogate fail-under` выставлен **70**. (4) `vulture` на этом коде шумит (79 кандидатов @60%, почти все ложные — Pydantic/enum/Protocol); гейт настроен на `min_confidence 80`, реальные кандидаты в мёртвый код вынесены в follow_ups. (5) `deptry` нашёл **реально неиспользуемые** runtime-зависимости `watchdog` и `platformdirs` — удалены из `pyproject.toml`. `pre-commit` сделан на local/system-хуках (не на закреплённых `rev`) — паритет с CI и обход расхождения версии mypy.

Цель — усилить **надёжность, поддерживаемость, расширяемость и читаемость** кода `wastech-orchestrator` за счёт статических инструментов. Это аналитический отчёт: он ничего не меняет в конфигурации, а даёт измеренную, приоритизированную рекомендацию с готовыми к применению конфигами. Все числа получены на текущем `HEAD` инструментами `ruff 0.15.17` / `mypy` и двумя под-аудитами кодовой базы; их можно воспроизвести командами из раздела «Метод».

**Главный вывод одной строкой.** Базовый линтинг у проекта уже хороший (`ruff` + `mypy --strict` + `pytest`, `ruff check .` проходит чисто), но три вещи, критичные именно для этого проекта, статически **не проверяются вообще**: (1) архитектурные границы импортов — сильнейший инвариант «core не знает синтаксиса CLI»; (2) рост сложности и размера god-модулей; (3) единый локальный прогон качества перед коммитом. Их закрывают `import-linter`, набор правил-храповиков `ruff` и `pre-commit` — почти всё это добавляется с нулевым или околонулевым churn.

## TL;DR — рекомендуемый стек

| Инструмент / правило | Что даёт (цель) | Измеренная стоимость сейчас | Вердикт |
| --- | --- | --- | --- |
| **import-linter** (`forbidden`-контракты) | Расширяемость, надёжность | 5 контрактов проходят сразу, 1 — фиксит точечную утечку | **Фаза 1 — топ-приоритет** |
| **ruff: безопасный набор** `FA,TID,ISC,ICN,PIE,RSE,LOG,G,DTZ,PTH,PERF,FURB,YTT,ASYNC,INP,RUF,T20` | Надёжность, читаемость, кроссплатформенность | ~30 находок, почти все автофиксимые; `T20` — 0 вне CLI-слоя | **Фаза 1** |
| **pre-commit** | Все 4 цели (единый гейт) | Конфиг + `pre-commit install` | **Фаза 1** |
| **ruff: храповики** `C901,PLR0915,PLR0912,PLR0911,PLR0913` | Поддерживаемость | С порогами: `C901`≥15 → 13, `PLR0913`>8 → 30 | **Фаза 2 (ratchet)** |
| **interrogate** (порог покрытия докстрингов) | Читаемость | Текущее покрытие ~81% → порог 80 проходит | **Фаза 2** |
| **vulture** (мёртвый код) | Поддерживаемость | ~7 реальных кандидатов | **Фаза 2** |
| **deptry** (гигиена зависимостей) | Надёжность, поддерживаемость | конфиг-only | **Фаза 2** |
| **mypy --strict** (оставить как есть) | Надёжность | уже есть | Не менять |
| Полный `layers`-контракт; правила `TC`, `TRY`, `EM`, `D`, `S` | — | Требуют рефактора / шумят на этом коде | **Фаза 3 / отклонить** (см. §5) |

## 1. Текущее состояние (baseline)

Из [pyproject.toml](../../pyproject.toml), [.github/workflows/ci.yml](../../.github/workflows/ci.yml) и правил в [.agents/rules/](../../.agents/rules/):

- **Линтер/форматтер:** `ruff`, набор правил `select = ["E", "F", "I", "UP", "B", "SIM", "C4"]`, `line-length = 100`, `target-version = "py312"`.
- **Типы:** `mypy` в режиме `strict` для `src/` (тесты исключены).
- **Тесты:** `pytest` + `pytest-cov`. Тестовый корпус большой и здоровый — **149 файлов, ~39k строк** против ~33k строк исходников (без `packaged/`).
- **CI** ([ci.yml](../../.github/workflows/ci.yml)): матрица py3.12/3.13, гейт `ruff check` → `ruff format --check` → `mypy src` → `pytest`. Зеркалируется в `release.yml`.

Чего **нет**: `pre-commit`, линтера архитектурных границ импортов, проверок цикломатической сложности/размера функций, покрытия докстрингов, поиска мёртвого кода, гигиены зависимостей.

Что уже **хорошо** (и это надо просто зафиксировать правилами, а не «чинить»):

- Подавлений мало: **7** `# type: ignore`, **13** `# noqa`, **0** `# ruff: noqa`, **0** маркеров `TODO/FIXME/HACK/XXX` во всём пакете.
- `datetime` везде tz-aware — все 8 вызовов это `datetime.now(UTC)` ([пример](../../src/wastech_orchestrator/state_store.py#L30)).
- Дисциплина subprocess соблюдается: **ни одного реального `shell=True`** (единственное совпадение — строка в докстринге [providers/process.py:10](../../src/wastech_orchestrator/providers/process.py#L10), документирующая запрет), запуск через argv-обёртку.
- Все внутренние импорты **абсолютные** (нет относительных) — контракты `import-linter` писать легко.
- **Нет побочных эффектов на импорте**: на уровне модулей только определения, константы и `logging.getLogger(__name__)` (хендлеры не навешиваются). Единственная регистрация — YAML-конструктор на приватном лоадере в [task/parser.py:89](../../src/wastech_orchestrator/task/parser.py#L89), без I/O.

## 2. Метод

- `ruff check <path> --select <RULE> --statistics` (ruff 0.15.17) — точный подсчёт срабатываний каждого кандидат-правила отдельно на `src/` и `tests/`.
- Пороговые кривые: `--config "lint.mccabe.max-complexity=N"`, `lint.pylint.max-args=N`, `max-branches=N` — чтобы подобрать реалистичный старт храповика.
- `git grep` — подавления, subprocess, портируемость, `print`, `datetime`.
- Два под-аудита кодовой базы: (A) карта импортов и границы слоёв + поиск хардкода node-id и побочек на импорте; (B) хотспоты сложности/размера, покрытие докстрингов (AST-проход по 1252 функциям), кандидаты в мёртвый код, смеллы консистентности.
- Веб-сверка с практиками 2025–2026 (ruff, import-linter, ландшафт тайп-чекеров, docstring/dead-code инструменты) — см. «Источники».

## 3. Находки по четырём целям

### 3.1 Надёжность

**Архитектурные границы не проверяются машинно — только ревью.** Это главный риск: инвариант «core не строит команды провайдеров» держится сегодня благодаря дисциплине, а не гейту. Фактическое состояние по аудиту импортов:

- `providers → core/routing/…` — **не зависит вообще** (пусто). Сильнейший, максимально защитимый инвариант.
- `routing → core` — **пусто**.
- `core → конкретные адаптеры` — по сути соблюдается: `core` касается только интерфейс-кластера провайдеров (`providers.base/artifacts/redaction/process/capabilities`), который сам по себе свободен от config/CLI. Единственная статически видимая «утечка» — отложенный (функционально-локальный) импорт в фабрике `build_providers()`: [core/orchestrator.py:3306-3307](../../src/wastech_orchestrator/core/orchestrator.py#L3306) `from …providers.claude import ClaudeCodeProvider` / `…codex import CodexProvider`. Это composition-root, но `grimp`/`import-linter` его видят.

`import-linter` превращает эти инварианты в CI-гейт (см. §4, контракты #1–#6).

**Кроссплатформенность.** Правило `PTH` (pathlib вместо `os.path`) почти бесплатно — миграция уже почти завершена (в `src/` осталось ~7 срабатываний: `PTH105`×3, `PTH118`×2, `PTH123`×2), большинство автофиксимые. `DTZ` (tz-aware datetime) даёт **0 срабатываний** — включение фиксирует уже правильное поведение даром. Обе темы — прямые требования [coding-style.md](../../.agents/rules/coding-style.md) («paths via pathlib», «no POSIX-only assumptions»).

**Subprocess/безопасность.** Дисциплина уже соблюдается кодом и обёрткой [providers/process.py](../../src/wastech_orchestrator/providers/process.py), поэтому bandit-правила `S` дают здесь в основном шум (см. §5) — их ценность мала.

### 3.2 Поддерживаемость

**Два god-модуля доминируют над всей базой.** [cli.py](../../src/wastech_orchestrator/cli.py) — **3576 строк / 102 функции**, [core/orchestrator.py](../../src/wastech_orchestrator/core/orchestrator.py) — **3398 строк**, включая god-класс `Orchestrator` из **106 методов** (тело класса ~2859 строк). Вместе это ~21% пакета. Крупные следом: [state_store.py](../../src/wastech_orchestrator/state_store.py) (1450), [git_manager.py](../../src/wastech_orchestrator/git_manager.py) (1419), [core/supervisor.py](../../src/wastech_orchestrator/core/supervisor.py) (1005).

**Цикломатическая сложность** (`ruff C901`, порог по умолчанию 10) — **47 функций** превышают порог. Худшие:

| Функция | Сложность |
| --- | --- |
| [core/flow/validator.py:210](../../src/wastech_orchestrator/core/flow/validator.py#L210) `_check_graph` | **43** |
| [cli.py:3510](../../src/wastech_orchestrator/cli.py#L3510) `main` | 27 |
| [task/validation_gate.py:331](../../src/wastech_orchestrator/task/validation_gate.py#L331) `_check_field_types` | 23 |
| [core/orchestrator.py:908](../../src/wastech_orchestrator/core/orchestrator.py#L908) `plan_rerun` | 19 |
| [core/flow/validator.py:470](../../src/wastech_orchestrator/core/flow/validator.py#L470) `_check_config_consistency` | 19 |
| [providers/codex.py:226](../../src/wastech_orchestrator/providers/codex.py#L226) `parse_events` | 18 |

Пороговая кривая (число нарушителей): `>10` → **47**, `>15` → **13**, `>20` → **7**. Реалистичный старт храповика — **порог 15** (13 функций к постепенному разбору), затем снижать. `radon` не нужен: `ruff C901` покрывает эту метрику полностью.

**Длина сигнатур** (`ruff PLR0913`, >5 аргументов) — **61 функция**; это сигнал реального дефицита parameter-object. Чемпион — [core/flow/wiring.py:47](../../src/wastech_orchestrator/core/flow/wiring.py#L47) `build_node_services` с **25 аргументами**; далее [process_control.py:381](../../src/wastech_orchestrator/process_control.py#L381) `stop_process` (17), [orchestrator.py:435](../../src/wastech_orchestrator/core/orchestrator.py#L435) `Orchestrator.__init__` (14). Кривая: `>5` → 61, `>6` → 48, `>8` → 30, `>10` → 19, `>12` → 15. `PLR0915` (тело >50 инструкций) почти бесплатно — всего **5** срабатываний (исключить генератор argparse [cli.py:174](../../src/wastech_orchestrator/cli.py#L174) `build_parser`).

**Мёртвый код** (кандидаты для `vulture`, каждый проверен как единственное вхождение во всём `src/`+`tests/`): [orchestrator.py:306](../../src/wastech_orchestrator/core/orchestrator.py#L306) `_validate_session_id`, [orchestrator.py:310](../../src/wastech_orchestrator/core/orchestrator.py#L310) `_artifact_kind`, [providers/process.py:320](../../src/wastech_orchestrator/providers/process.py#L320) `_coerce_stderr`, приватные методы `Orchestrator._require_human_result` / `_save_counters`, константа [core/decomposition.py:42](../../src/wastech_orchestrator/core/decomposition.py#L42) `SUBTASK_IN_PROGRESS`. Список короткий и высокосигнальный — ровно то, где god-класс прячет мёртвые методы.

**Зависимости.** Гигиена не проверяется. `deptry` ловит неиспользуемые/необъявленные/транзитивные зависимости в [pyproject.toml](../../pyproject.toml) — дёшево и предотвращает дрейф (например, extra `[shell]` использует `prompt_toolkit` только лениво — deptry подтверждает такие контракты).

### 3.3 Расширяемость

**Инвариант «движок generic по `node.kind`, без хардкода node-id» соблюдается.** Диспетчеризация — [core/flow/engine.py:353](../../src/wastech_orchestrator/core/flow/engine.py#L353) `runner = self._runners.get(node.kind)`; `node.id` используется только как непрозрачный ключ графа. Ни одного литерального switch вида `== "implementation"` / `== "review"` в `core/` для управления потоком нет; все подозрения (`node.id == decomp.proposed_by`, резервные имена в [snapshot.py:157](../../src/wastech_orchestrator/core/flow/snapshot.py#L157)) — data-driven или валидация, не поведенческое ветвление. Это соответствует запрету из [architecture.md](../../.agents/rules/architecture.md) и намеренному «нет `Stage` enum».

Инструмент, который **защищает** эту расширяемость от регрессий, — тот же `import-linter` (провайдерский шов) плюс опционально лёгкий кастомный тест-гвард (см. §4, Фаза 3).

**Циклы пакетов блокируют полный `layers`-контракт.** Обнаружены двунаправленные зависимости: `core ↔ core.flow` (один арх-юнит), `core ↔ state_store` ([state_store.py:25-26](../../src/wastech_orchestrator/state_store.py#L25) импортит `core.loop_control`/`core.state_machine`), `config ↔ providers`, `config ↔ security`, `config ↔ checks`, `security ↔ providers`. Корень — общий словарь типов, прежде всего `ProviderId` в [providers/base.py](../../src/wastech_orchestrator/providers/base.py), который тянут `config/security/task/install/routing/core`, тогда как конкретные адаптеры в том же пакете `providers` импортят `config`/`security` обратно. Полный упорядоченный `layers`-контракт станет достижим после небольшого рефактора (вынести чистые vocabulary-типы в модуль без зависимостей) — это Фаза 3.

### 3.4 Читаемость

**Покрытие докстрингов ~81%** (628/774 публичных функций/классов/методов) — это **сильная сторона**, а не проблема. Проседают: `notify` 55%, `install` ~65%, `config` ~66% (напр. модели в [config/schema.py:197+](../../src/wastech_orchestrator/config/schema.py#L197), нотификатор [notify/telegram.py:128+](../../src/wastech_orchestrator/notify/telegram.py#L128)). Важный нюанс: значительная часть «пробелов» — самодокументируемые Pydantic/dataclass-модели, где смысл несут имена полей. Поэтому правильный инструмент — `interrogate` с порогом (зафиксировать 80% и подтянуть отстающие пакеты), а **не** полный `pydocstyle D`, который зашумит эти модели и придирается к стилю (`D401` non-imperative-mood — 157 срабатываний).

**Смелл консистентности (самый предметный):** сосуществуют четыре стиля обработки ошибок валидации — accumulate-and-return (`list[str]`/`list[Violation]`), accumulate-then-raise, raise-immediately, return-`None`-sentinel. Хуже того, у [config/validation.py:105](../../src/wastech_orchestrator/config/validation.py#L105) `validate_config` **сигнатура `-> list[str]` противоречит докстрингу** «Raises `ConfigError` on any violation». Линтер это не поймает — это цель для точечного рефактора (Фаза 3), но упомянуть стоит: единый контракт ошибок валидации сильнее всего улучшит читаемость этого слоя.

**`print` — 221 в `src/`, но это НЕ нарушение**: все сосредоточены в презентационном слое — [cli.py](../../src/wastech_orchestrator/cli.py), [cli_shell.py](../../src/wastech_orchestrator/cli_shell.py), [install/](../../src/wastech_orchestrator/install/); вне этих трёх мест `print` нет. Значит `T20` (flake8-print) надо включать **с per-file-ignore** на CLI-слой — тогда правило ловит 0 сейчас и защищает от «случайного `print` в ядре» в будущем.

**`RUF001-003`** (~19) — намеренные типографские/математические символы (en-dash `–`, знак объединения `∪`) в русских комментариях/докстрингах, а не опечатки. Их надо **игнорировать** (или занести в `allowed-confusables`), не «чинить».

## 4. Рекомендуемый план (по фазам)

### Фаза 1 — дёшево, ~0 churn, включить сразу

**1a. Расширить `ruff` безопасным набором.** Все перечисленные группы дают 0 срабатываний, кроме помеченных; `T20` — 0 после per-file-ignore. В [pyproject.toml](../../pyproject.toml):

```toml
[tool.ruff.lint]
select = [
    "E", "F", "I", "UP", "B", "SIM", "C4",   # существующие
    "FA", "TID", "ISC", "ICN", "PIE", "RSE", # структурные, 0 срабатываний
    "LOG", "G",                               # дисциплина логирования (G004: 3)
    "DTZ",                                    # tz-aware datetime (0 — фиксируем хорошее)
    "PTH",                                    # pathlib вместо os.path (~7, автофикс) — кроссплатформенность
    "PERF", "FURB",                           # перф + идиомы (~16, часть автофикс)
    "YTT", "ASYNC", "INP",                    # корректность, 0 срабатываний
    "T20",                                    # запрет print вне CLI-слоя
    "RUF",                                    # ruff-специфичные (unused noqa, sorted __all__)
]
ignore = [
    "RUF001", "RUF002", "RUF003",  # намеренные типографские символы (– en-dash, ∪) в RU-тексте
    "ISC001",                       # конфликтует с ruff format — отключить (рекомендация astral)
]

[tool.ruff.lint.per-file-ignores]
# CLI/shell/install легитимно пишут в stdout через print() — это вывод для пользователя, не логирование.
"src/wastech_orchestrator/cli.py" = ["T20"]
"src/wastech_orchestrator/cli_shell.py" = ["T20"]
"src/wastech_orchestrator/install/*" = ["T20"]
# Тесты: assert — это суть; докстринги/магические значения/приватный доступ/namespace — тестовые идиомы.
# (Правила S/D/PLR/ARG попадают сюда заранее — no-op, пока не включены в Фазе 2.)
"tests/**" = ["S101", "PLR2004", "PLR0913", "SLF001", "INP001", "ARG", "D", "T20"]
```

Затем один прогон `ruff check . --fix` уберёт автофиксимые (`RUF100` unused-noqa, `RUF022` unsorted `__all__`, `PTH`, часть `PERF/FURB`). Проверить, что `ruff format --check .` остаётся зелёным.

**1b. `import-linter` — контракты границ.** Добавить `import-linter` в extra `dev` и файл `.importlinter` в корне:

```ini
[importlinter]
root_package = wastech_orchestrator

[importlinter:contract:providers-are-leaf]
name = Провайдеры не зависят от core/оркестрации
type = forbidden
source_modules =
    wastech_orchestrator.providers
forbidden_modules =
    wastech_orchestrator.core
    wastech_orchestrator.routing
    wastech_orchestrator.cli
    wastech_orchestrator.git_manager
    wastech_orchestrator.state_store
    wastech_orchestrator.task
    wastech_orchestrator.checks
    wastech_orchestrator.memory
    wastech_orchestrator.notify
    wastech_orchestrator.install

[importlinter:contract:routing-not-core]
name = Routing не зависит от core
type = forbidden
source_modules = wastech_orchestrator.routing
forbidden_modules = wastech_orchestrator.core

[importlinter:contract:core-not-concrete-adapters]
name = Core не импортирует конкретные CLI-адаптеры
type = forbidden
source_modules = wastech_orchestrator.core
forbidden_modules =
    wastech_orchestrator.providers.claude
    wastech_orchestrator.providers.codex
    wastech_orchestrator.providers._adapter_base
# Точечная утечка: фабрика build_providers() (composition-root). Либо игнор, либо перенос в cli — см. ниже.
ignore_imports =
    wastech_orchestrator.core.orchestrator -> wastech_orchestrator.providers.claude
    wastech_orchestrator.core.orchestrator -> wastech_orchestrator.providers.codex

[importlinter:contract:adapters-independent]
name = Адаптеры Claude и Codex независимы
type = independence
modules =
    wastech_orchestrator.providers.claude
    wastech_orchestrator.providers.codex

[importlinter:contract:config-not-orchestration]
name = Config не зависит от оркестрации
type = forbidden
source_modules = wastech_orchestrator.config
forbidden_modules =
    wastech_orchestrator.core
    wastech_orchestrator.routing
```

Из шести контрактов пять проходят немедленно; `core-not-concrete-adapters` падает ровно на одном ребре (`build_providers`). **Лучший вариант** вместо `ignore_imports` — перенести `build_providers()` из [orchestrator.py](../../src/wastech_orchestrator/core/orchestrator.py) в composition-root (`cli`/отдельный модуль сборки), тогда контракт станет строго зелёным без исключений и утечка исчезнет физически.

**1c. `pre-commit`** — единый локальный гейт (запускает то же, что CI, но до коммита). Файл `.pre-commit-config.yaml` (версии закрепить актуальными на момент применения):

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.15.17
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.13.0
    hooks:
      - id: mypy
        additional_dependencies: [types-PyYAML]
        args: [--strict]
        files: ^src/
  - repo: https://github.com/seddonym/import-linter
    rev: v2.5
    hooks:
      - id: import-linter
```

**1d. CI** — добавить шаг `lint-imports` в [ci.yml](../../.github/workflows/ci.yml) после `mypy` (и зеркально в `release.yml`), плюс задокументировать `pre-commit install` в quickstart.

### Фаза 2 — храповики с ограниченным бэклогом

Включать по одному правилу, разбирая накопленный список; god-модули при необходимости временно закрыть `per-file-ignores`, чтобы правило было зелёным на всём остальном и не давало регрессий.

- **`C901`** с `max-complexity = 15` → 13 нарушителей (позже снижать к 10). Начать с [validator.py:210](../../src/wastech_orchestrator/core/flow/validator.py#L210) `_check_graph` (43).
- **`PLR0915`** (тело функции) — 5 срабатываний, исключить `build_parser`. Почти бесплатно.
- **`PLR0912`** (`max-branches = 15` → 12) и **`PLR0911`** (too-many-returns, 18) — параллельно `C901`.
- **`PLR0913`** — стартовать с `max-args = 8` (30 нарушителей, ловит все вопиющие: 25/17/14 аргументов) и снижать к 6/5. Первый рефактор — parameter-object для [wiring.py:47](../../src/wastech_orchestrator/core/flow/wiring.py#L47) `build_node_services`.
- **`interrogate`** — порог покрытия докстрингов:

```toml
[tool.interrogate]
fail-under = 80
ignore-init-method = true
ignore-magic = true
ignore-nested-functions = true
ignore-private = true
```

- **`vulture`** — мёртвый код с allowlist для ложных срабатываний (динамический доступ). Ожидаемо короткий список (~7).
- **`deptry`** — гигиена зависимостей: `deptry src` в CI.

### Фаза 3 — по желанию / после рефактора

- **Полный `layers`-контракт** — после выноса чистых vocabulary-типов (`ProviderId` и контракты `AgentRunRequest/Result`) из [providers/base.py](../../src/wastech_orchestrator/providers/base.py) в модуль без зависимостей и перемещения `Status`/`TERMINAL` ниже `state_store`. Это растворяет циклы `config↔providers`, `config↔security`, `core↔state_store` и делает возможным строгий упорядоченный контракт слоёв.
- **`TC`** (typing-only imports в `TYPE_CHECKING`) — 176 срабатываний, но частично автофиксимо и полезно (снижает рантайм-импорты, помогает рвать те самые циклы). Включать после Фазы 2 через `--fix`.
- **Единый контракт ошибок валидации** — устранить 4 стиля и противоречие сигнатуры/докстринга в [config/validation.py:105](../../src/wastech_orchestrator/config/validation.py#L105).
- **Опциональные кастомные гварды** (тесты, не линтеры): (1) AST-проверка «в `core/` нет сравнения `node.id` с литералом-id» — цементирует инвариант расширяемости; (2) «нет логики на импорте» с allowlist для [task/parser.py:89](../../src/wastech_orchestrator/task/parser.py#L89).

## 5. Чего НЕ делать (анти-рекомендации)

- **Не включать `T20`/запрет `print` глобально** без per-file-ignore — 221 легитимный CLI-`print` даст ложный красный.
- **Не менять тайп-чекер.** `mypy --strict` работает, CI построен вокруг него, код инвариант-тяжёлый — зрелость важнее скорости. `ty` (Astral) на июль 2026 всё ещё beta; `pyright` — сильный дефолт, но тянет Node. Разумный опцион: `pyright`/`ty` в редакторе для скорости DX, **без** замены mypy в гейте.
- **Не делать `select = ["ALL"]`** — при апгрейде ruff молча включаются новые правила и ломают CI.
- **Не включать `pydocstyle D` целиком** — зашумит самодокументируемые Pydantic/Row-модели и стилевые придирки (`D401`=157). Использовать `interrogate`.
- **Осторожно с bandit-`S`** — на этом проекте шумит: `S608` (SQL-инъекция) — ложные срабатывания на корректном параметризованном паттерне `",".join("?" * len(...))` в [state_store.py:664](../../src/wastech_orchestrator/state_store.py#L664); `S101` (21) — это narrowing-`assert` в ядре; `S603/S607` — легитимный subprocess (дисциплина уже обеспечена кодом). Ценность мала.
- **Не «чинить» `RUF001-003`** — символы `–`/`∪` намеренные; занести в ignore/`allowed-confusables`.
- **Не гнаться за полным `layers`-контрактом** до устранения циклов пакетов — контракт не сойдётся, это не баг конфигурации.

## Источники

- [How to configure recommended Ruff defaults — pydevtools](https://pydevtools.com/handbook/how-to/how-to-configure-recommended-ruff-defaults/)
- [The Ruff Linter — Astral Docs](https://docs.astral.sh/ruff/linter/)
- [Import Linter — Layers contract](https://import-linter.readthedocs.io/en/latest/contract_types/layers/)
- [Import Linter — Contract types](https://import-linter.readthedocs.io/en/latest/contract_types.html)
- [Improving Python code quality with architecture contract validation](https://blog.ukena.de/posts/2021/10/using-python-import-linting-to-improve-code-quality/)
- [mypy vs Pyright vs ty: Python Type Checker Comparison (2026) — danilchenko.dev](https://www.danilchenko.dev/posts/ty-vs-mypy-vs-pyright/)
- [How do Python type checkers compare — pydevtools](https://pydevtools.com/handbook/explanation/how-do-mypy-pyright-and-ty-compare/)
- [The Ultimate Pre-Commit Hooks Guide for 2025](https://gatlenculp.medium.com/effortless-code-quality-the-ultimate-pre-commit-hooks-guide-for-2025-57ca501d9835)
- [best-of-python-dev — ranked dev tools](https://github.com/ml-tooling/best-of-python-dev)
