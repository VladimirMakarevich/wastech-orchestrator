# Разбор прогона — `p5-02-doc-profile` (codex-primary на 0.8.9a3, проверка фикса F38/F39)

## STATUS

**Задача:** `p5-02-doc-profile` — «P5.02 extractDocProfile» **Итог:** `done` · ветка `feat/p5-compile` (`branch_mode: existing`) · PR [#11](https://github.com/VladimirMakarevich/wastech-mdlint/pull/11) **reuse** (2-й коммит, не смержен) · `fix_iterations=0` · attempt 1 · без декомпозиции **finished_at:** 2026-07-07T11:33:22Z **Версия оркестратора:** **0.8.9a3** (доставлен фикс ADR [codex-primary-correctness.md](../backlog/archive/done/codex-primary-correctness.md)). Конфиг — codex-primary (`gpt-5.4`/`xhigh`), claude=fallback+review (с прошлого прогона, не менялся).

## Короткий вывод

Прогон успешен по продукту (чистый коммит 4 файла +386/−4, 4/4 чека, claude-review accept с первого раза) **и одновременно — проверка фикса**: **F38 подтверждён исправленным end-to-end** — codex resume-путь заработал, documentation (resume-узел) впервые прошёл на codex без фоллбэка. Но **F39 в этом прогоне не закрыт**: supervisor по-прежнему крашит codex на каждом шаге, потому что target-конфиг не задаёт `supervisor.provider`, а preflight унаследованный мисматч не ловит. **Единственный главный рычаг сейчас — добавить `supervisor.provider: claude` в target `config.yaml` и расширить preflight-валидацию F39 на случай унаследованного primary.**

Побочно: первый запуск p5-02 был отказан гейтом `depends_on` (PR #11 открыт) — вскрылся конфликт `depends_on` с общей веткой `branch_mode: existing` (F40); обойдено снятием `depends_on` с p5-02…p5-06.

## Как прошёл прогон (фактический путь по флоу)

| Узел | Провайдер / модель | Попытки | Итог | Время |
| --- | --- | --- | --- | --- |
| refinement | — | — | skipped (per-task) | — |
| planning | **codex** gpt-5.4/xhigh (fresh) | 1 | succeeded | 428s |
| implementation | **codex** gpt-5.4/xhigh (fresh) | 1 | succeeded | 572s |
| testing | checks (npm) | — | pass 4/4 | ~9s |
| review | **claude** opus-4-8/high (fresh) | 1 | **accept** | 142s |
| documentation | **codex** gpt-5.4 (resume) | 1 | **succeeded (на codex!)** | 77s |
| publish | — | — | published, PR #11 reuse | ~6s |
| **supervisor** (слой) | codex (resume) → **fallback claude** ×6 | 2 каждый | succeeded (на claude) | 20–75s/шаг |

Общее время ~25 мин. `test_fix_cycles=0`, `review_fix_cycles=0`.

**Стоимость:** основной вес — два fresh-codex-узла (planning 428s, implementation 572s). Каждый supervisor-шаг тратит один codex-attempt впустую (~0.5–2s краш) перед claude-fallback — 6 сожжённых codex-попыток за прогон (F39).

## Находки по убыванию влияния

### F38 — VERIFIED FIXED ✅ (codex resume-argv)

**Доказательство.** `stages/documentation/run-000120/1-codex/request.json` `argv`: `codex --ask-for-approval never exec --cd <dir> --sandbox workspace-write --json --output-last-message <file> resume [REDACTED] --model gpt-5.4 -c model_reasoning_effort="medium" -` — exec-опции стоят **до** подкоманды `resume`, `--model`/`-c` после (ровно фикс из ADR). `provider_attempts`: documentation `codex attempt=1 succeeded exit 0` (77s), **без fallback**. В Проходе 15 этот же узел падал `unsupported_version` (argparse) → claude. Фикс работает в бою.

### F39 — CONFIRMED, не закрыт в target-конфиге + пробел preflight (MEDIUM)

**Доказательство.** После починки F38 supervisor-codex доходит до реального запуска и падает на модели: `stages/supervisor/run-000116/1-codex/stdout.log` → `{"type":"error","status":400,"error":{"type":"invalid_request_error","message":"The 'claude-opus-4-8' model is not supported when using Codex with a ChatGPT account."}}`, `result.json` `exit_code=1 error_class=process_crashed`. Повторилось ×6 (каждый supervisor-шаг) → claude-fallback.

**Корневая причина.** F39-код-фикс (`SupervisorConfig.provider` + валидация) в 0.8.9a3 присутствует, но не эффективен: (1) target `config.yaml` не задаёт `supervisor.provider` → наследует `primary=codex`; (2) `preflight` прошёл `ready`, не поймав мисматч — валидация, вероятно, срабатывает лишь на ЯВНЫЙ `supervisor.provider`, а не на наследование primary при claude-специфичной `model`.

**Рычаг.** Быстрый обход (target): `supervisor.provider: claude` в `.worc/config.yaml`. Системно (orchestrator): расширить F39-валидацию на случай «`provider` не задан → наследуется primary, а `model` чужого вендора» — точка в проверке совместимости supervisor (см. ADR). Зона — target-config (обход) + orchestrator (валидация).

### F40 — `depends_on` × `branch_mode: existing` конфликт заблокировал фазу (MEDIUM)

**Доказательство.** Первый запуск p5-02 отказан: `error: refusing to run p5-02-doc-profile: dependency 'p5-01-classify-nodes' PR is OPEN (unmerged)` (exit 2, задача осталась `pending`). Все p5-задачи совмещали `depends_on` с `branch_mode: existing` + `branch_ref: feat/p5-compile`.

**Корневая причина.** `depends_on` = merge-гейт (зависимость должна быть смержена), а shared-branch `branch_mode: existing` = «мерж в конце фазы, PR остаётся открытым» — взаимоисключающие механизмы. В кампании p4 shared-branch работал именно без `depends_on`.

**Рычаг.** Target task-authoring: не совмещать (снято с p5-02…p5-06 — сделано). Orchestrator (UX): предупреждать при валидации, когда `depends_on` указывает на задачу, чья ветка = собственный `branch_ref`. Зона — target + orchestrator.

## Пробелы в данных

- Токены/стоимость по узлам детально не выгружались (не требовалось для вердикта).
- Память: `evaluations` этого прогона отдельно не разбирались (фокус — проверка фикса).
- supervisor-крашей нет в `provider_attempts` (supervisor — слой, не node_run); их причина взята из `stages/supervisor/**/stdout.log` — данные полные.

## Что уже хорошо

- **F38-фикс подтверждён в бою** — codex resume-узлы (documentation) работают на codex; заявленный codex-primary теперь соблюдается на fresh- И resume-узлах, кроме supervisor (F39).
- **Codex как основной кодер** снова справился: planning + implementation на codex, diff чистый и в скоупе (extractDocProfile: `doc-profile.ts` +129, тест +230, barrel +6, doc +25).
- **branch_mode: existing + PR reuse** отработали: 2-й коммит лёг на `feat/p5-compile`, PR #11 переиспользован (не открыт #12).
- **Кросс-провайдерный fallback** предсказуемо спасал supervisor на каждом шаге — задача дошла до `done`.
- **review-claude accept** с первого раза, rework-цикла нет.

## План исправлений

**P0**

- **F39 обход (target):** добавить `supervisor.provider: claude` в `.worc/config.yaml` перед следующим прогоном — supervisor перестанет крашить codex.

**P1**

- **F39 системно (orchestrator):** расширить preflight-валидацию supervisor на унаследованный provider (primary с моделью чужого вендора → fatal/warning), чтобы мисматч ловился до прогона.
- **F40 (orchestrator UX):** предупреждать/отклонять `depends_on` на задачу с тем же `branch_ref` (противоречивая конфигурация цепочки).

**P2**

- Рассмотреть, стоит ли `error_class` codex различать «bad model» (400 invalid_request) и настоящий `process_crashed` — сейчас оба классифицируются одинаково.

## Сводная таблица

| Наблюдение | Причина | Рычаг | Зона |
| --- | --- | --- | --- |
| documentation прошёл на codex без fallback | F38-фикс: exec-опции переупорядочены до `resume` | — (VERIFIED FIXED в 0.8.9a3) | orchestrator |
| supervisor крашит codex ×6 (400: claude-модель не поддержана codex) | target-конфиг без `supervisor.provider` → наследует codex; preflight не ловит | `supervisor.provider: claude` (обход) + расширить F39-валидацию | target + orchestrator |
| Первый запуск p5-02 отказан (PR #11 open) | `depends_on` (merge-гейт) совмещён с shared-branch `branch_mode: existing` | снять `depends_on` (сделано) + warning в валидации задачи | target + orchestrator |
| Fresh-узлы codex (planning/implementation) чисто | fresh-путь argv валиден | — (позитив) | — |
| PR #11 reuse, 2-й коммит на общей ветке | `branch_mode: existing` + `branch_ref` | — (позитив) | — |
