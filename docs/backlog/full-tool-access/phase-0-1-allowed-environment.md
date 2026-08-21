# Фаза 0.1 — сломанный `allowed_environment` виден до запуска

Status: **implemented 2026-08-18** (AC0.1.1–AC0.1.3 зелёные; AC0.1.4 — живая проба на Windows **не доказана**, хоста нет; запись в разделе поправок [README.md](README.md)) Date: 2026-08-13 Owner: Vladimir Makarevich Требования: Т0.1 из [requirements-step-0.md](requirements-step-0.md) `schema_version`: без изменения Зависимости: нет — фаза идёт первой

Самая дешёвая фаза Шага 0 и единственная, которая ничего не разрешает: она чинит диагностику. Ключ `allowed_environment` **заменяет** дефолт целиком, а дефолт OS-зависимый, поэтому оператор, вычеркнувший одно имя, ломает запуск CLI и видит это как «CLI did not succeed» — на Windows ещё и без единой строки вывода, потому что Node-овый `claude.exe` падает с `0xC0000409` до печати чего-либо.

## Поправка follow-ups 2026-08-20

`PATH` остаётся host-independent load error, но его причина теперь разделена: orchestrator-owned `git`/`gh` нуждается в нём при любом режиме, agent CLI — при strict isolation. Windows `SystemRoot` проверяется в обоих режимах и не только в preflight: `run` / `watch` / `rerun` повторяют тот же launch-critical gate до работы. Полная диагностическая формулировка про наблюдавшийся `0xC0000409` живёт в одной строке таблицы [`guide/config/security.md`](../../../src/wastech_orchestrator/packaged/guide/config/security.md); остальные носители кратко описывают обязательность и ссылаются на неё. CLI-dispatch всех трёх рабочих команд покрыт тестом на конфиг без `PATH`; platform-specific ветки принимают `system=` и не патчат глобальный модуль `platform`.

## Что делаем

**1. Валидация конфига (host-independent).** `PATH` обязан покрываться `security.allowed_environment`. Текст issue разделяет последствие: strict-mode agent CLI и orchestrator-owned `git`/`gh` при любом режиме.

**2. Проверка хоста (host-specific).** На Windows отсутствие `SystemRoot` ставит preflight FAIL и тем же чистым helper'ом останавливает `run` / `watch` / `rerun` до работы. Это FAIL, а не предупреждение: orchestrator-owned `git`/`gh` всегда остаётся на allow-list, а при strict isolation тот же пробел достигает agent CLI.

Разделение проверок по этой границе — требование, а не вкус: конфиг должен получать одинаковый load-verdict на любой машине, а host-specific verdict живёт в полном preflight-отчёте и повторяется узким launch-critical gate на старте задачи.

**3. Доки.** Убрать из корпуса утверждение, что сгенерированный `config.yaml` содержит кросс-платформенный союз: `install` пишет `list(default_allowed_environment())` — дефолт хост-ОС, 9 имён на POSIX ([`config_writer.py:143`](../../../src/wastech_orchestrator/install/config_writer.py)); 22 имени — это шипнутый шаблон, из которого мержит `upgrade-config`. Правится таблица ключа и абзац replace-not-extend в [`guide/config/reference.md`](../../../src/wastech_orchestrator/packaged/guide/config/reference.md), комментарий над списком в [`config.example.yaml`](../../../src/wastech_orchestrator/packaged/config.example.yaml) и, если фраза попадёт под грепом, [`guide/config/README.md`](../../../src/wastech_orchestrator/packaged/guide/config/README.md).

## Тесты

- `tests/config/test_validation.py` — конфиг без `PATH` даёт issue; конфиг с `PATH` не даёт.
- `tests/test_cli_preflight.py` — `system="Windows"` без `SystemRoot` → FAIL; с `SystemRoot` → OK; `system="Linux"` без `SystemRoot` → OK; `run` / `watch` / `rerun` отвергают конфиг без `PATH`. Платформа передаётся аргументом чистой функции, не глобальным monkeypatch модуля `platform`.
- `tests/security/test_env.py` — регрессия на числа дефолтов (9 / 19 / 22), чтобы правка доков не разъехалась с кодом.

## Живая проба (часть DoD)

На Windows-хосте: конфиг без `SystemRoot` → `worc preflight` завершается FAIL-ом с внятной причиной, а не запуском CLI, который упадёт молча. Хоста нет — фаза закрывается с записью «не доказано (Windows)» в разделе поправок README кампании; проверка при этом остаётся в коде и покрыта тестом с подставленной платформой.

## Риск и откат

Риск один: валидатор начинает отвергать конфиг, который вчера грузился. Это ровно те конфиги, на которых ничего и не работало, но список ошибок при обновлении оркестратора неприятен — поэтому сообщение обязано быть самодостаточным (что добавить и куда). Откат — снятие одной проверки, состояния в конфиге фаза не заводит.

## DoD

AC0.1.1–AC0.1.4 зелёные; `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports`, `pytest` зелёные; доки ветки поправлены тем же изменением; в описании PR — doc-impact строка про `configuration.md` / `operations.md` на `main`.
