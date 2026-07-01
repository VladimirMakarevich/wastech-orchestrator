Ты работаешь на РЕАЛЬНОЙ Windows-машине в репозитории wastech-orchestrator, ветка feat/memory-subsystem.

ЦЕЛЬ: закрыть находку AC-X (кросс-платформенность) для подсистемы памяти на настоящем
Windows. Ключевой факт: подсистема памяти (src/wastech_orchestrator/memory/, tests/memory/)
появилась 2026-06-30..07-01 и на Windows НИКОГДА не запускалась. Прошлый Windows-прогон
(follow_ups.md, "Windows test-suite portability gap", done 2026-06-26) был ДО памяти. Аудит
docs/backlog/memory/implementation-audit.md, находка F5, прямо помечает AC-X1/AC-X2 как
"POSIX storage is tested ... but there is no Windows-form round-trip test" и оставляет их на
"standing Windows-CI follow-up". Твоя задача — реально это проверить и покрыть.

СНАЧАЛА ПРОЧИТАЙ (это спецификация, не выдумывай):
1. CLAUDE.md — раздел "Hard invariants" про кросс-платформенность (pathlib + Path.as_posix()
   для любого хранимого/сравниваемого/показываемого пути; newline="\n"/UTF-8 для файлов;
   никаких os.kill/signal; git только через providers.process.run_process).
2. docs/backlog/memory/implementation-audit.md — находка F5 (что именно про AC-X отложено).
3. docs/backlog/memory/{requirements.md (NFR8, AC-X1/AC-X2), design.md §7} — контракт.
4. Код, который отвечает за пути и запись на диск:
   - src/wastech_orchestrator/memory/derived.py  (DerivedIndex.path_exists / find_by_basename /
     git_tracked_paths — git через run_process, as_posix() при сравнении)
   - src/wastech_orchestrator/memory/_io.py       (atomic write, newline, sort_keys — детерминизм)
   - src/wastech_orchestrator/memory/service.py    (apply_delta, tier_files, restore, F1 index=)
   - src/wastech_orchestrator/memory/cleanup.py    (F2 _reconcile_lessons, никаких os.kill/signal)
5. tests/memory/ — существующие тесты и 4 safety-drill'а
   (test_memory_{redaction,poisoning,staleness,rollback}_drill.py).

ПРЕДУСЛОВИЕ (проверь ПЕРВЫМ делом, иначе остановись и скажи мне):
Убедись, что правки F1–F6 присутствуют в рабочем дереве. Признаки:
  - в memory/service.py есть параметр `index: DerivedIndex | None` у MemoryService.__init__;
  - в memory/lifecycle.py у assign_entity_trust есть аргумент `path_exists`;
  - в memory/cleanup.py есть метод `_reconcile_lessons`;
  - в providers/redaction.py есть функция `secret_env_values`.
Если чего-то нет — НЕ продолжай: сообщи, что ветку нужно синхронизировать (эти правки не были
закоммичены в другой сессии).

ЖЁСТКИЕ ПРАВИЛА:
- Ядро памяти детерминированно и model-free — НИЧЕГО не зовёт LLM.
- Если тест на Windows выявит РЕАЛЬНУЮ проблему (путь сохранился с обратными слэшами; файл
  записался с \r\n; git ls-files вернул не тот вид; path_exists не находит существующий файл) —
  это НАСТОЯЩАЯ находка: почини её минимальной кросс-платформенной правкой в продакшн-коде
  (pathlib/as_posix/newline) и опиши в отчёте. НЕ ослабляй тест, чтобы он "позеленел".
- Новые тесты должны быть кросс-платформенными (жить в общей сюите и проходить на всех ОС);
  их ценность — что ты их прогонишь именно на Windows. Windows-специфичные проверки (обратные
  слэши) оборачивай в `@pytest.mark.skipif(sys.platform != "win32", ...)`.

ПОРЯДОК РАБОТЫ:
1. Окружение: Python 3.14, создай/активируй venv (`py -3.14 -m venv .venv` →
   `.venv\Scripts\activate`), `pip install -e ".[dev]"`. Убедись, что `git --version` работает
   (DerivedIndex шеллит `git ls-files`).
2. БАЗОВЫЙ ПРОГОН (главное): впервые прогони память на Windows и зафиксируй результат:
   `python -m pytest tests/memory -q`
   плюс явно 4 дрилла. Любой упавший тест — уже ценная находка; разберись в причине.
3. Прогони весь гейт на Windows: `ruff check .`, `mypy src`, `python -m pytest -q`.
4. Добавь ЯВНЫЕ Windows round-trip тесты (в tests/memory/, напр. новый
   test_memory_cross_platform.py), доказывающие:
   (a) POSIX-хранимый путь резолвится против нативной Windows-ФС: создай реальный файл
       repo\src\a.py через pathlib, построй DerivedIndex(repo) и проверь
       path_exists("src/a.py") is True, path_exists("src/gone.py") is False;
       find_by_basename возвращает пути с прямыми слэшами.
   (b) end-to-end F1 на Windows: apply_delta с MemoryService(index=DerivedIndex(repo)) и
       entity-карточкой paths=("src/a.py",) при реально существующем файле → карточка хранится
       как repo-observed, а в записанном .jsonl путь — С ПРЯМЫМИ слэшами (не "src\\a.py");
       карточка с несуществующим путём → карантин.
   (c) запись LF, не CRLF: после реальной записи tier-файла ассерт, что байты содержат b"\n" и
       НЕ содержат b"\r\n"; JSON читается обратно идентично (детерминизм sort_keys).
   (d) git_tracked_paths на настоящем Windows git-репо (сделай temp-репо, `git init`, добавь
       файл в подпапке, закоммить) возвращает пути с прямыми слэшами (POSIX-вид).
   (e) (skipif win32) подай путь с обратным слэшом и проверь, что as_posix()-нормализация даёт
       "src/a.py" и он всё равно резолвится.
5. grep-подтверди отсутствие os.kill/signal в memory/ (особенно cleanup.py) — на Windows это
   критично (кросс-процессное управление там иное).
6. Прогони новую сюиту + все 4 дрилла + ruff + mypy на Windows — всё зелёное.
7. Синхронизируй доки в том же изменении:
   - implementation-audit.md, находка F5: перепиши статус AC-X1/AC-X2 с "untested/rides
     follow-up" на "проверено на Windows N (Python 3.14): memory-сюита + дриллы зелёные, добавлен
     round-trip тест" — и обнови сноску AC-X1 в conformance-таблице.
   - follow_ups.md: строку про Windows отметь, что memory-сюита теперь покрыта на Windows
     (AC-SF5 всё ещё отдельно).
   - Прогони prettier: `npx prettier@3 --write "docs/backlog/**/*.md"`.
8. Коммить ТОЛЬКО когда я попрошу.

ОТЧЁТ (в конце): что прогнал, сколько тестов памяти прошло на Windows, какие РЕАЛЬНЫЕ
Windows-проблемы нашёл (если были) и как починил, какие новые тесты добавил, статус ruff/mypy.
Если всё зелёное без правок продакшн-кода — так и скажи (значит as_posix()/newine-инварианты
держатся на настоящем Windows).
