# Разбор прогона: `p4-02-graph-algorithms`

## STATUS

- **Задача:** `p4-02-graph-algorithms` — «P4.02 — Graph algorithms (topo-sort, components, cycles)». Первая задача **branch-mode chain-теста** (docs/backlog/archive/done/branch-mode.md): вместо своей ветки на задачу — общая ветка `feat/p4-graph-chain` на всю цепочку `p4-02..p4-08`.
- **Final status:** `done` · **PR:** [#9](https://github.com/VladimirMakarevich/wastech-mdlint/pull/9) (открыт, НЕ смержен — `auto_merge:false`) · ветка `feat/p4-graph-chain`.
- **finished_at:** 2026-07-04T20:21:56Z · `fix_iterations=0`, `review_fix_cycles=0`, `test_fix_cycles=0`, `decomposed=false`, `attempt=1`, `manual=false`.
- **Провайдеры:** claude opus-4-8/high (planning/implementation/documentation), **codex gpt-5.4/xhigh** запиннен на `review` per-node override в целевом флоу (`.worc/flows/implementation.yaml:93-96`) — упал на attempt 1, fallback claude отработал вместо него.
- **Побочки:** реальный PR #9 открыт на GitHub, не смержен. Базовое дерево target чистое (task-файл штатно вышел из `pending/`); восстановление не требуется. До запуска потребовалась ручная правка `depends_on` в task-файле (см. F25) — заброшенный `p4-01-context-graph-model` заменён на реально смёрженный `p4-01-context-graph-model-v2`.

## Короткий вывод

Прогон **успешен по существу** (planning→implementation→testing→review→documentation→publish все зелёные, checks 160/160, PR открыт с содержательным диффом), и первый шаг branch-mode chain-теста прошёл штатно: `branch_mode: new` + кастомный `branch_name: feat/p4-graph-chain` создали именно ту ветку, которую должны продолжить `p4-03..p4-08`. Но прогон вскрыл **новый регресс от сегодняшнего же фикса F19**: review-узел на codex падает 100%-детерминированно с `invalid_json_schema` (не проблема качества модели — API отвергает схему ДО начала turn), и только claude-fallback спас результат.

**Единственный главный рычаг (P0):** `_FINDINGS_SCHEMA` в [core/flow/nodes/evaluator.py:57-78](../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L57) — новая обязательная findings-схема (введена сегодня как фикс F19) не проставляет `"additionalProperties": false` ни на верхнем, ни на вложенном `items`-object. OpenAI Structured Outputs (через который codex CLI реализует `--output-schema`) требует это на КАЖДОМ object-узле схемы и отвечает 400 `invalid_json_schema` до начала turn — гарантированный краш на любом codex-evaluator (review/verifier/critic/testing_quality), не флуктуация. `core/hitl.py` уже соблюдает эту конвенцию везде — просто забыли применить её к новой схеме. См. **F24**.

## Как прошёл прогон (фактический путь по флоу)

Флоу `implementation` (task_type=implementation), супервайзер — постоянный слой:

| # | Узел | kind | провайдер / модель / reasoning | attempt / статус | длительность |
| --- | --- | --- | --- | --- | --- |
| — | refinement | agent | — | **skipped** (`nodes.refinement.enabled=false`) | — |
| 1 | planning | agent | claude / opus-4-8 / high | 1 / succeeded | 180s |
| — | supervisor(planning) | — | claude | 1 / succeeded | ~180s* |
| 2 | implementation | agent | claude / opus-4-8 / high | 1 / succeeded | 195s |
| — | supervisor(impl) | — | claude | 1 / succeeded | ~22s |
| 3 | testing | checks | — (typecheck/lint/**160 тестов**/build) | passed | 7s |
| — | supervisor(testing) | — | claude | 1 / succeeded | ~26s |
| 4 | review | evaluator | **codex/gpt-5.4/xhigh → crashed** → fallback **claude** | 2 / succeeded → **`accept`** (2 low, 0 blocking) | 76s (5.5s crash + ~70s fallback) |
| — | supervisor(review) | — | claude | 1 / succeeded | ~98s |
| 5 | documentation | agent | claude / opus-4-8 / high | 1 / succeeded | 77s |
| — | supervisor(doc)+final | — | claude | succeeded | — |
| 6 | publish | publish | — | published → PR #9 | 55s |

*supervisor-длительности приблизительны (интервал между `route resolved`-событиями соседних узлов, не изолированный тайминг).

- **Ретраи/фоллбэки:** ровно один — `review`: codex attempt 1 → `process_crashed` (400 `invalid_json_schema` от OpenAI, см. F24) → claude attempt 2 → succeeded. Route resolved как `primary=codex, fallback=claude`; сработал штатный fallback-механизм (F19 fail-closed НЕ ошибочно сработал — это отдельный, новый баг схемы).
- **Per-node override сработал:** review перекрыл глобальный конфиг-дефолт codex `gpt-5.5/high` на declared в target-флоу `gpt-5.4/xhigh` (повторное подтверждение §12, впервые наблюдалось в прогоне 6).
- **F19-фикс подтверждён рабочим на claude:** `structured_output.findings` = 2 генуинных `low`-находки (пустой массив покрытия edge-case + документационная заметка), `verdict=accept` корректен по существу — это НЕ повтор бага «прозаические находки теряются» (F19 resolved действительно держит).
- **HITL-гейтов не было.**
- **Токены/стоимость (claude):** planning `out=13003`, implementation `out=14602`, review-fallback `out=4433`, documentation `out=4864` → суммарно `output≈36.9k`, `cache_read≈2.28M` (тяжёлое переиспользование контекста между узлами одной задачи). Codex-attempt не дошёл до usage (упал до первого токена).
- **Branch-mode `new` + кастомный `branch_name`:** ветка `feat/p4-graph-chain` создана от обновлённого `main` (после мержа PR #8 из прохода 6) с именем ровно как в task front matter — без auto-паттерна `{prefix}/{epoch}-...`. `task.normalized.json` резолвит `branch_mode: "new", branch_ref: null, publish: null`.

## Находки (по убыванию влияния)

Полные записи — в [TEST-FINDINGS.md](../../TEST-FINDINGS.md) (F24–F25).

### F24 (HIGH) — регресс от сегодняшнего F19-фикса: codex падает 100%-детерминированно на JSON Schema

`_FINDINGS_SCHEMA` ([evaluator.py:57-78](../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L57)), введённая сегодня как обязательная findings-схема для ВСЕХ evaluator-узлов, не проставляет `additionalProperties: false` ни на верхнем object, ни на вложенном `items`-object. OpenAI Structured Outputs (через который codex реализует `--output-schema`) требует это на каждом object-узле и отвечает `400 invalid_json_schema` ДО начала turn — см. буквальный текст ошибки в `stages/review/run-000060/1-codex/stdout.log`. Контрастная проверка: `core/hitl.py` соблюдает эту конвенцию везде (`_HUMAN_INPUT_SCHEMA`, `_SUBTASK_SCHEMA`, `typed_output_schema`) — паттерн в кодовой базе известен, просто не применён к новой схеме. Сегодня замаскировано claude-фоллбэком, но: (1) фатально при `agents.allowed: [codex]` (единственный провайдер); (2) в ЭТОМ target-репо review принудительно запиннен на codex — краш будет повторяться на КАЖДОЙ из `p4-03..p4-08`; (3) тихая деградация стоимости/латентности даже когда fallback спасает.

### F25 (MEDIUM) — `depends_on` не переживает abandon+retry-под-новым-id

Исходный task-файл `p4-02` ссылался на `p4-01-context-graph-model` (первая попытка P4.01, брошена в `manual_action_required`), а не на `p4-01-context-graph-model-v2` (реально смёрженный ретрай под ДРУГИМ task id). `worc run` корректно отказал (`error: refusing to run ...: dependency 'p4-01-context-graph-model' is manual_action_required (unmerged)`, exit 2) — но диагностика не подсказывает, что дело в переименованном ретрае; без ручного чтения ledger причина неочевидна. Постоянная тихая блокировка без авто-обнаружения.

## Пробелы в данных

- **`prompt-audit/` присутствует** (`timeline.jsonl` + 4 per-node JSON) — пробела по аудиту промптов нет.
- **Стоимость codex-attempt 1** недоступна (упал до первого токена — ожидаемо, не пробел наблюдаемости, а следствие самого краша).
- **Supervisor-длительности** оценены по интервалам между событиями лога, а не по изолированным `node_runs`-таймстампам (супервизор не заведён как отдельная строка в `node_runs`, только в `evaluations`) — точность приблизительная, не влияет на выводы.

## Что уже хорошо (проверено)

- **Branch-mode `new` + кастомный `branch_name` работает буквально** — первый содержательный прогон новой ADR-функциональности с созданием общей для цепочки ветки; готовит корректную базу для `p4-03..p4-08` (`existing`/`current`, PR-reuse).
- **F19-фикс держит на claude:** review реально гейтит (2 генуинных low-находки, а не пустой fail-open массив), `verdict=accept` — заслуженный, не баг.
- **Fallback-механизм сработал штатно:** codex crashed → claude подхватил тем же узлом без вмешательства оператора, без потери задачи.
- **Независимый checks-гейт:** testing прогнал typecheck/lint/**160 тестов**/build отдельно от самопроверки имплементера — совпало.
- **Diff/scope:** имплементер держал скоуп задачи (только `graph-algorithms.ts` + барabrel-экспорт + тесты + doc), сознательно отложил repo-wide prettier-drift и G7 global edge-collapse как follow-ups, не полез чинить их сам.
- **Explicit-run merge-gate сработал корректно** (F25) — отказ, а не тихий запуск на неготовой базе; проблема только в диагностируемости причины, не в самом гейте.

## План исправлений

### P0

- **F24 — починить `_FINDINGS_SCHEMA`.** Добавить `"additionalProperties": False` на оба object-уровня ([evaluator.py:57-78](../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L57)), по образцу `hitl.py`. Добавить регрессионный тест, валидирующий рекурсивно ВСЕ схема-константы кодовой базы на `additionalProperties: false` на каждом object-узле — существующий smoke-тест из `run-quality-gating-hardening.md` использовал свою упрощённую тестовую схему, а не буквально `_FINDINGS_SCHEMA`, поэтому не поймал регресс. Ожидаемый эффект: codex-evaluator (review/verifier/critic/testing_quality) больше не крашится детерминированно на первой попытке.

### P1

- **F25 — диагностика abandon+retry-под-новым-id.** Как минимум задокументировать в task-authoring: после abandon+retry-под-новым-id нужно вручную обновить `depends_on` у всех зависимых pending-задач. Опционально — `_resolve_dependency` при `abandoned`-статусе зависимости ищет более позднюю ledger-запись с тем же `title`/`done` и подсказывает вероятный id-заменитель. Рычаг: [core/orchestrator.py:722-743](../../src/wastech_orchestrator/core/orchestrator.py#L722).

### P2

- Нет P2-находок в этом прогоне.

## Сводная таблица

| Наблюдение | Причина | Рычаг (file:line) | Зона |
| --- | --- | --- | --- |
| codex-review падает 400 `invalid_json_schema` на attempt 1, каждый раз | `_FINDINGS_SCHEMA` без `additionalProperties:false` на обоих object-уровнях (регресс сегодняшнего F19-фикса) | `core/flow/nodes/evaluator.py:57-78` | orchestrator |
| `worc run p4-02` отказал с «dependency ... manual_action_required» до правки | `depends_on` ссылался на заброшенный id первой попытки P4.01, а не на смёрженный ретрай под новым id | `core/orchestrator.py:722-743` | orchestrator (+ target task-файл) |
| Ветка `feat/p4-graph-chain` создана корректно с кастомным именем | `branch_mode: new` + `branch_name` резолвятся как задокументировано в ADR | `git_manager.py` (branch-mode ADR) — работает как задумано | orchestrator |
