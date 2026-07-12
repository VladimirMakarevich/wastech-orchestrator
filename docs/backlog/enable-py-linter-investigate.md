## Рекомендуемый подход

Не стоит собирать тяжёлую комбинацию из Black, Flake8, isort, pyupgrade и множества плагинов. Практичный стек для современного Python-проекта:

* **Ruff** — форматирование, импорты и основной статический анализ.
* **mypy** — проверка корректности типов.
* **pytest** — тесты и строгая проверка тестовой конфигурации.
* **coverage.py** — покрытие ветвей, но не как самоцель.
* **pre-commit** — быстрые локальные проверки.
* **CI** — единственный обязательный источник истины.
* **Import Linter** — только при наличии явно определённых архитектурных слоёв.

Ruff заменяет большую часть традиционного набора линтеров и форматтеров, при этом его стандартный набор правил намеренно ограничен проверками с хорошим соотношением пользы и шума. Ruff поддерживает более 900 правил, поэтому включать `ALL` — почти наверняка плохая идея. ([Astral Docs][1])

---

# План внедрения

## 1. Зафиксировать границы проекта

До настройки линтера определить:

* минимальную поддерживаемую версию Python;
* где находится production-код: `src/`, `app/`, пакет в корне;
* какие каталоги являются generated/vendor-кодом;
* используются ли Django, FastAPI, SQLAlchemy, Pydantic, Celery, async;
* должны ли проверяться скрипты, миграции и notebooks;
* какие части проекта уже типизированы.

Версию Python в Ruff и mypy нужно указывать как **минимально поддерживаемую**, а не ту, которая установлена у конкретного разработчика.

Для нового проекта предпочтительна структура:

```text
project/
├── pyproject.toml
├── src/
│   └── project_name/
└── tests/
```

Pytest рекомендует `src` layout и для новых проектов — импортирование тестов через `--import-mode=importlib`: это уменьшает вероятность того, что тесты случайно работают только потому, что корень репозитория попал в `sys.path`. ([pytest][2])

---

## 2. Сначала подключить Ruff

Ruff должен отвечать за две отдельные операции:

```bash
ruff format .
ruff check --fix .
```

В CI:

```bash
ruff format --check .
ruff check .
```

CI не должен исправлять код — только проверять. Автоматические исправления выполняются локально. Ruff по умолчанию применяет только исправления, классифицированные как безопасные; unsafe fixes следует оставлять выключенными и применять только вручную после просмотра diff. ([Astral Docs][3])

### Стартовый `pyproject.toml`

```toml
[tool.ruff]
# Заменить на минимальную версию Python проекта.
target-version = "py311"

# Лучше оставить стандартное значение, если нет веской причины менять.
line-length = 88

# Нестабильные правила не должны неожиданно попадать в CI.
preview = false

[tool.ruff.lint]
select = [
    # Базовые ошибки Python и Pyflakes.
    "E4",
    "E7",
    "E9",
    "F",

    # Стабильная сортировка импортов.
    "I",

    # Частые реальные ошибки и опасные конструкции.
    "B",

    # Обновление синтаксиса под минимальную версию Python.
    "UP",

    # Не ловить Exception/BaseException без необходимости.
    "BLE001",

    # Явный T | None вместо неявного Optional.
    "RUF013",

    # Удаление устаревших noqa.
    "RUF100",

    # Запрет безадресного "# noqa".
    "PGH004",
]

[tool.ruff.lint.per-file-ignores]
# Здесь должны быть только обоснованные исключения.
# Например:
# "tests/**/*.py" = ["ARG001"]

[tool.ruff.format]
quote-style = "double"
indent-style = "space"
line-ending = "auto"

# Полезно, если в docstring есть исполняемые примеры.
docstring-code-format = true
```

### Почему именно эти правила

* `F`, `E4`, `E7`, `E9` ловят неопределённые имена, неиспользуемые импорты, синтаксические и другие базовые ошибки.
* `I` устраняет ручные споры о порядке импортов.
* `B` содержит преимущественно проверки потенциальных багов, а не вкусовых предпочтений.
* `UP` удерживает код на одном современном синтаксисе.
* `BLE001` обнаруживает слишком широкие `except`; при этом Ruff не ругается на некоторые нормальные сценарии вроде повторного выбрасывания исключения или `logging.exception`. ([Astral Docs][4])
* `RUF100` и `PGH004` не дают suppressions превращаться в свалку.

---

## 3. Не включать всё подряд

На старте я бы **не включал** следующие группы.

### `E501` — длина строки

Ruff Formatter делает best-effort перенос строк, но не гарантирует, что каждая строка окажется короче лимита. Поэтому `E501` может ругаться на результат самого форматтера. Авторы Ruff прямо рекомендуют осторожно относиться к этому правилу. ([Astral Docs][5])

Длинная строка должна исправляться, когда она ухудшает читаемость, а не потому что содержит 89-й символ.

### `ANN` — обязательные аннотации

`ANN` проверяет преимущественно наличие аннотации, но не её корректность. Этим должен заниматься mypy. Одновременное жёсткое использование `ANN` и mypy часто создаёт дублирование и шум.

### `D` — обязательные docstring

Не каждая функция нуждается в docstring. Хорошие имена, типы и небольшие функции зачастую информативнее формального:

```python
"""Return the user."""
```

Документировать стоит публичные контракты, необычное поведение, ограничения и причины решений.

### `C901`, `PLR*` — метрики сложности как жёсткий gate

Цикломатическая сложность и количество аргументов полезны как сигнал при ревью, но жёсткие числа часто приводят к механическому дроблению функций без улучшения дизайна.

### `S` целиком

Security-правила полезны, но их нужно включать по модели угроз. Например, проверки `eval`, небезопасного YAML или `shell=True` могут быть полезны для сервиса, но полный набор `S` обычно требует множества контекстных исключений.

### `TRY`, `RET`, `SIM`, `PERF`, `PTH`

В этих наборах есть полезные проверки, но значительная часть касается предпочтительного стиля или микрооптимизаций. Добавлять их стоит по одной или небольшими группами после пробного запуска на реальном коде.

### `preview = true`

Preview включает нестабильные правила, исправления и форматирование. Его лучше проверять в отдельном экспериментальном CI job, не блокирующем merge. ([Astral Docs][6])

---

# 4. Добавить mypy как отдельный quality gate

Линтер не проверяет корректность контрактов между функциями. Для надёжности и расширяемости нужен type checker.

### Прагматичный базовый конфиг

```toml
[tool.mypy]
python_version = "3.11"

files = [
    "src",
    "tests",
]

check_untyped_defs = true
disallow_incomplete_defs = true

warn_redundant_casts = true
warn_unused_ignores = true
warn_unreachable = true
strict_equality = true

show_error_codes = true
show_error_code_links = true
pretty = true
```

Для нового проекта или уже типизированных модулей добавить:

```toml
[tool.mypy]
disallow_untyped_defs = true
disallow_any_generics = true
```

Необязательно сразу включать `strict = true`. Mypy указывает, что состав `strict` может меняться между версиями. Явный набор параметров лучше показывает, какую именно гарантию предоставляет проект. ([mypy.readthedocs.io][7])

### Для существующего проекта

Вводить типизацию постепенно:

1. Новые функции обязаны иметь типы.
2. Изменяемый старый код типизируется в рамках той же задачи.
3. Сначала типизируются модели, DTO, shared utilities и другие широко импортируемые модули.
4. `disallow_untyped_defs` включается по пакетам.
5. Сторонние библиотеки без типов исключаются адресно.

Mypy рекомендует постепенную типизацию и отдельно советует начинать с широко импортируемых модулей. Глобальный `ignore_missing_imports = true` может скрывать реальные ошибки, поэтому исключения лучше задавать только для конкретных внешних библиотек. ([mypy.readthedocs.io][8])

Например:

```toml
[[tool.mypy.overrides]]
module = ["untyped_external_library.*"]
ignore_missing_imports = true

[[tool.mypy.overrides]]
module = ["project_name.legacy_package.*"]
disallow_untyped_defs = false
```

Legacy-исключения должны иметь владельца и план удаления.

---

# 5. Усилить тестовый контур

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]

addopts = [
    "--strict-config",
    "--strict-markers",
    "--import-mode=importlib",
]
```

`--strict-config` обнаруживает неизвестные или ошибочно записанные параметры конфигурации. `--strict-markers` превращает опечатки в pytest-маркерах в ошибки вместо предупреждений. ([pytest][9])

Для pytest 9 можно использовать общий strict mode, но он способен включать новые проверки после обновления pytest. Поэтому либо фиксируйте версию pytest, либо включайте строгие параметры явно. ([pytest][10])

## Покрытие

Предпочтительнее branch coverage:

```toml
[tool.coverage.run]
branch = true
source = ["src"]

[tool.coverage.report]
show_missing = true
skip_covered = true
```

Branch coverage показывает не только выполненную строку, но и непроверенную ветку условия. Например, строка с `if` может считаться покрытой line coverage, хотя сценарий `False` ни разу не выполнялся. ([coverage.readthedocs.io][11])

Не стоит сразу требовать условные 90–100% для всего проекта. Лучше:

* зафиксировать текущую базу;
* не позволять покрытию ухудшаться;
* отдельно повышать требования для доменной логики;
* не учитывать generated-код, миграции и тривиальные адаптеры;
* проверять ошибки, границы и альтернативные ветки, а не просто исполнять строки.

---

# 6. Настроить локальные и CI-проверки

## Pre-commit: быстрый feedback

На каждый commit:

```bash
ruff check --fix
ruff format
```

Дополнительно можно включить низкошумные проверки:

* неразрешённые merge-конфликты;
* корректность TOML/YAML;
* случайно добавленные приватные ключи;
* повреждённые или слишком большие файлы.

Mypy и полный pytest лучше запускать на `pre-push` или в CI, если они занимают заметное время.

Pre-commit обычно проверяет только изменённые файлы, а `pre-commit run --all-files` позволяет проверить весь репозиторий. Поэтому локальный hook — удобство, но не замена CI. ([pre-commit.com][12])

## CI: обязательные проверки

```bash
ruff format --check .
ruff check .
mypy
pytest
```

При наличии архитектурных контрактов:

```bash
lint-imports
```

Эти jobs можно выполнять параллельно. Версии Ruff, mypy, pytest и плагинов должны быть зафиксированы lock-файлом. Ruff также позволяет задать обязательную версию через `required-version`, чтобы разные окружения не выдавали разные результаты. ([Astral Docs][13])

---

# 7. Архитектурные правила — только для реальной архитектуры

Если проект разделён, например, на:

```text
domain
application
infrastructure
api
```

имеет смысл добавить Import Linter и зафиксировать допустимое направление зависимостей. Он умеет проверять не только прямые, но и косвенные импорты между слоями. ([import-linter.readthedocs.io][14])

Например:

* `domain` не импортирует инфраструктуру и web framework;
* `application` зависит от `domain`, но не от `api`;
* `infrastructure` реализует интерфейсы приложения;
* разные feature-модули не используют внутренние части друг друга.

Для небольшого проекта без чётких границ такой инструмент будет лишним.

---

# Политика исключений

Хорошая система линтинга допускает исключения, но делает их видимыми.

Использовать:

```python
value = unsafe_library_call()  # type: ignore[no-untyped-call]
```

```python
import optional_module  # noqa: F401 -- imported to register the plugin
```

Не использовать:

```python
# type: ignore
# noqa
```

Практики:

* всегда указывать код подавляемой ошибки;
* для неочевидного исключения писать причину;
* не отключать правило глобально из-за одного framework-specific случая;
* generated-код исключать централизованно;
* периодически удалять устаревшие suppressions;
* не использовать `# noqa` для сокрытия ещё не исправленного технического долга.

---

# Критерий добавления нового правила

Новое правило попадает в blocking CI только когда:

1. Оно находило или с высокой вероятностью предотвращает реальные дефекты.
2. Его результат детерминирован.
3. У него низкий уровень ложных срабатываний.
4. Ошибку можно понятно объяснить разработчику.
5. Оно не дублирует formatter, mypy или тесты.
6. Исправление обычно улучшает код, а не просто удовлетворяет инструмент.
7. Команда согласна поддерживать это правило долгосрочно.

Сначала правило запускается в report-only режиме на всём репозитории. После анализа результатов включается как gate либо отклоняется.

## Итоговый минимальный стандарт

**Обязательные:**

```text
Ruff formatter
Ruff: E4/E7/E9/F, I, B, UP, BLE001, RUF013, RUF100, PGH004
mypy с постепенным усилением
pytest strict-config + strict-markers
branch coverage
единые проверки в CI
```

**Только при доказанной необходимости:**

```text
security rules
async/datetime rules
complexity thresholds
architecture contracts
additional style families
```

Такой набор ловит реальные ошибки, стабилизирует интерфейсы и импорты, поддерживает тестируемость, но не заставляет команду бесконечно обслуживать линтер.

[1]: https://docs.astral.sh/ruff/rules/?utm_source=chatgpt.com "Rules | Ruff - Astral Docs"
[2]: https://docs.pytest.org/en/stable/explanation/goodpractices.html "Good Integration Practices - pytest documentation"
[3]: https://docs.astral.sh/ruff/linter/?utm_source=chatgpt.com "The Ruff Linter - Astral Docs"
[4]: https://docs.astral.sh/ruff/rules/blind-except/?utm_source=chatgpt.com "blind-except (BLE001) | Ruff - Astral Docs"
[5]: https://docs.astral.sh/ruff/formatter/ "The Ruff Formatter | Ruff"
[6]: https://docs.astral.sh/ruff/preview/?utm_source=chatgpt.com "Preview | Ruff - Astral Docs"
[7]: https://mypy.readthedocs.io/en/stable/command_line.html?utm_source=chatgpt.com "The mypy command line - mypy 2.2.0 documentation"
[8]: https://mypy.readthedocs.io/en/stable/existing_code.html "Using mypy with an existing codebase - mypy 2.2.0 documentation"
[9]: https://docs.pytest.org/en/stable/example/markers.html?utm_source=chatgpt.com "Working with custom markers"
[10]: https://docs.pytest.org/en/stable/explanation/goodpractices.html?utm_source=chatgpt.com "Good Integration Practices"
[11]: https://coverage.readthedocs.io/en/7.15.0/branch.html?utm_source=chatgpt.com "Branch coverage measurement"
[12]: https://pre-commit.com/ "pre-commit"
[13]: https://docs.astral.sh/ruff/settings/?utm_source=chatgpt.com "Settings | Ruff - Astral Docs"
[14]: https://import-linter.readthedocs.io/en/latest/contract_types/layers/?utm_source=chatgpt.com "Layers - Import Linter"
