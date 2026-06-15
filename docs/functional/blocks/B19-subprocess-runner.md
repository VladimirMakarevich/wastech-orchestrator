# B19 — Безопасный запуск подпроцессов

## Назначение

Единственная точка запуска любого внешнего CLI. Гарантирует системные инварианты безопасности
запуска: запуск списком аргументов (без shell), обязательный таймаут, точно заданное окружение,
подача промпта через stdin, потоковая запись stdout в файл. Сам по себе примитив ничего не знает о
синтаксисе Codex/Claude/git и о классах ошибок — он лишь запускает процесс и возвращает «сырой»
результат для нормализации вызывающей стороной.

## Ответственность

- Запустить `argv` как список через `subprocess.run(..., shell=False)`
  ([process.py:81-93](../../../src/wastech_orchestrator/providers/process.py#L81)).
- Передать дочернему процессу **ровно** переданный словарь окружения (`env=dict(env)`), не сливая с
  родительским ([process.py:84](../../../src/wastech_orchestrator/providers/process.py#L84)).
- Применить обязательный таймаут; при истечении убить процесс и пометить `timed_out`
  ([process.py:90,96-98](../../../src/wastech_orchestrator/providers/process.py#L96)).
- Подать `stdin_text` на stdin процесса (через `input=`), либо отправить `DEVNULL`, если текста нет
  ([process.py:75-77](../../../src/wastech_orchestrator/providers/process.py#L75)).
- Потоково писать stdout в `stdout_path` (файл `wb`), захватывать stderr в память
  ([process.py:80,86](../../../src/wastech_orchestrator/providers/process.py#L80)).
- Вернуть `ProcessResult` (exit_code/timed_out/launch_error/duration/stdout_path/stderr_text)
  ([process.py:32-41,105-112](../../../src/wastech_orchestrator/providers/process.py#L32)).

## Границы блока

### Входит в ответственность блока

- Безопасный запуск процесса и измерение длительности (через инъектируемый `monotonic`).
- Захват stdout в файл и stderr в строку (нередактированную).

### Не входит в ответственность блока

- **Построение окружения** — выполняет [B25 `build_child_env`](./B25-security-policy.md); сюда уже
  готовый `env` передаёт вызывающая сторона. Модуль не импортирует `security.env`.
- **Редактирование секретов** — stderr возвращается «как есть»; редактирует вызывающая сторона перед
  записью в артефакт ([B21](./B21-secret-redaction.md)); это явно отмечено в типе результата
  ([process.py:41](../../../src/wastech_orchestrator/providers/process.py#L41)).
- **Классификация ошибок** в `ErrorClass` — это [B18](./B18-agent-providers.md) (`errors.classify`).
- **Построение `argv`** — это адаптеры/менеджеры, которые вызывают `run_process`.

## Точки входа

- `run_process(argv, *, cwd, env, timeout_seconds, stdout_path, stdin_text=None, monotonic=...)`
  ([process.py:44](../../../src/wastech_orchestrator/providers/process.py#L44)). Вызывается из
  адаптеров провайдеров ([B18](./B18-agent-providers.md)), Git Manager ([B22](./B22-git-manager.md)),
  Check Runner ([B24](./B24-check-execution.md)) и обнаружения git при установке
  ([B03/B04](./B03-installer-and-scaffolding.md)).

## Входные данные и состояние

`argv` (список), `cwd`, `env` (полное окружение для ребёнка), `timeout_seconds` (обязательный),
`stdout_path`, опциональный `stdin_text`. Состояние не хранится — функция чистая по отношению к
процессу (кроме создания файла stdout).

## Основной сценарий

1. Открывается файл `stdout_path` на запись (бинарно).
2. Запускается `subprocess.run(list(argv), cwd=…, env=dict(env), stdout=file, stderr=PIPE,
   text=utf-8/replace, timeout=…, shell=False)` с `input=stdin_text` или `stdin=DEVNULL`.
3. По завершении фиксируются `exit_code` и `stderr_text`.
4. Возвращается `ProcessResult` с измеренной длительностью.

## Альтернативные сценарии

### Истечение таймаута
`subprocess.TimeoutExpired` → `timed_out=True`, `exit_code=None`, частичный stderr из исключения
([process.py:96-98](../../../src/wastech_orchestrator/providers/process.py#L96)).

### Невозможность запустить бинарь
`FileNotFoundError/PermissionError/NotADirectoryError/OSError` → ошибка **не пробрасывается**, а
записывается в `launch_error` (с именем `argv[0]`, без секрета); для пустого `argv` — метка
`"<empty argv>"` ([process.py:99-102](../../../src/wastech_orchestrator/providers/process.py#L99)).

## Проверки и ограничения

- `shell=False` всегда; `argv` — только список (никакой shell-интерполяции пользовательских строк).
- Окружение — ровно `dict(env)`, родительское не наследуется.
- Таймаут обязателен (тип `int`, передаётся вызывающей стороной).
- stdin: либо `input`, либо `DEVNULL` — родительский stdin никогда не наследуется.

## Результат

`ProcessResult` — «сырой» итог запуска: код возврата (или `None`), флаги `timed_out`,
`launch_error`, длительность, путь к stdout-файлу, текст stderr (нередактированный).

## Побочные эффекты

- Создаётся/перезаписывается файл `stdout_path`.
- Порождается один дочерний процесс (с заданными cwd/env/таймаутом).

## Ошибки и граничные случаи

- Таймаут и неудачный запуск — это **значения** результата, а не исключения (см. альтернативные
  сценарии). Вызывающая сторона решает, как их классифицировать.
- Длительность измеряется всегда, даже при ошибке запуска.

## Связи

### Использует

- стандартную библиотеку (`subprocess`, `os`, `time`). Внешних блоков не использует.

### Используется в

- [B18 — Адаптеры провайдеров](./B18-agent-providers.md) — запуск `codex`/`claude`.
- [B22 — Git Manager](./B22-git-manager.md) — запуск `git`/`gh`.
- [B24 — Выполнение проверок](./B24-check-execution.md) — запуск команд-проверок.
- [B23 — Обнаружение проверок](./B23-check-discovery.md) — пробы запускаемости.
- [B03 — Установщик](./B03-installer-and-scaffolding.md) — `git_info` (read-only git-пробы).

## Место в общей системе

Это «горлышко» исполнения: инвариант «запуск CLI без shell-интерполяции пользовательских строк»
(см. [CLAUDE.md], правила безопасности) держится именно здесь. Каждый внешний процесс системы
проходит через `run_process`, поэтому редактирование секретов и аллой-лист окружения применяются
последовательно во всех подсистемах.

## Подтверждение в коде

- [providers/process.py:44-112](../../../src/wastech_orchestrator/providers/process.py#L44) —
  `run_process`: argv-список, `shell=False`, `env=dict(env)`, таймаут, stdin, stdout-в-файл.
- [providers/process.py:32-41](../../../src/wastech_orchestrator/providers/process.py#L32) —
  `ProcessResult` (stderr помечен как «не редактирован»).
- [tests/providers/test_process.py](../../../tests/providers/test_process.py) — подтверждает: stdout
  в файл, stdin не в argv, окружение ровно переданное (секреты родителя не утекают), таймаут →
  `timed_out`, отсутствующий бинарь → `launch_error` без исключения.
- [tests/security/test_no_shell_interpolation.py](../../../tests/security/test_no_shell_interpolation.py)
  — подтверждает, что `subprocess` используется только через этот модуль.
