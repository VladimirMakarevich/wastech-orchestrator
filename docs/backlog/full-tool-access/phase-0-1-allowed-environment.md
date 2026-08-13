# Фаза 0.1 — сломанный `allowed_environment` виден до запуска

Status: **ready to implement** Date: 2026-08-13 Owner: Vladimir Makarevich Требования: Т0.1 из [requirements-step-0.md](requirements-step-0.md) `schema_version`: без изменения Зависимости: нет — фаза идёт первой

Самая дешёвая фаза Шага 0 и единственная, которая ничего не разрешает: она чинит диагностику. Ключ `allowed_environment` **заменяет** дефолт целиком, а дефолт OS-зависимый, поэтому оператор, вычеркнувший одно имя, ломает запуск CLI и видит это как «CLI did not succeed» — на Windows ещё и без единой строки вывода, потому что Node-овый `claude.exe` падает с `0xC0000409` до печати чего-либо.

## Что делаем

**1. Валидация конфига (host-independent).** `_validate_security` ([`validation.py:326`](../../../src/wastech_orchestrator/config/validation.py)) сегодня проверяет только `protected_paths`. Добавить: `PATH` обязан присутствовать в `security.allowed_environment`. Текст issue называет ключ и последствие («дочерний процесс не найдёт ни агентский CLI, ни `git`»), как это делают соседние сообщения валидатора.

**2. Проверка хоста (host-specific).** В `run_preflight` — рядом с блоками, которые уже громко печатают состояние read-isolation и git-evidence ([`cli.py:2945`](../../../src/wastech_orchestrator/cli.py), [`cli.py:2961`](../../../src/wastech_orchestrator/cli.py)) — добавить: на Windows-хосте отсутствие `SystemRoot` в списке ставит `ok = False` и печатает причину с упоминанием `0xC0000409`. Именно FAIL, а не предупреждение: без этого имени CLI не стартует вовсе, а preflight существует ровно для того, чтобы это стоило ноль.

Разделение проверок по этой границе — требование, а не вкус: конфиг должен получать одинаковый вердикт на любой машине (иначе один и тот же файл «валиден» на macOS и «сломан» на Windows), а всё, что зависит от ОС хоста, живёт в preflight.

**3. Доки.** Убрать из корпуса утверждение, что сгенерированный `config.yaml` содержит кросс-платформенный союз: `install` пишет `list(default_allowed_environment())` — дефолт хост-ОС, 9 имён на POSIX ([`config_writer.py:143`](../../../src/wastech_orchestrator/install/config_writer.py)); 22 имени — это шипнутый шаблон, из которого мержит `upgrade-config`. Правится таблица ключа и абзац replace-not-extend в [`guide/config/reference.md`](../../../src/wastech_orchestrator/packaged/guide/config/reference.md), комментарий над списком в [`config.example.yaml`](../../../src/wastech_orchestrator/packaged/config.example.yaml) и, если фраза попадёт под грепом, [`guide/config/README.md`](../../../src/wastech_orchestrator/packaged/guide/config/README.md).

## Тесты

- `tests/config/test_validation.py` — конфиг без `PATH` даёт issue; конфиг с `PATH` не даёт.
- `tests/test_cli_preflight.py` — `system="Windows"` без `SystemRoot` → FAIL с ожидаемым текстом; с `SystemRoot` → OK; `system="Linux"` без `SystemRoot` → OK. Платформа подставляется, а не берётся у машины, иначе тест зелен только на одной ОС.
- `tests/security/test_env.py` — регрессия на числа дефолтов (9 / 19 / 22), чтобы правка доков не разъехалась с кодом.

## Живая проба (часть DoD)

На Windows-хосте: конфиг без `SystemRoot` → `worc preflight` завершается FAIL-ом с внятной причиной, а не запуском CLI, который упадёт молча. Хоста нет — фаза закрывается с записью «не доказано (Windows)» в разделе поправок README кампании; проверка при этом остаётся в коде и покрыта тестом с подставленной платформой.

## Риск и откат

Риск один: валидатор начинает отвергать конфиг, который вчера грузился. Это ровно те конфиги, на которых ничего и не работало, но список ошибок при обновлении оркестратора неприятен — поэтому сообщение обязано быть самодостаточным (что добавить и куда). Откат — снятие одной проверки, состояния в конфиге фаза не заводит.

## DoD

AC0.1.1–AC0.1.4 зелёные; `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports`, `pytest` зелёные; доки ветки поправлены тем же изменением; в описании PR — doc-impact строка про `configuration.md` / `operations.md` на `main`.
