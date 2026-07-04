# Разбор прогона: `p4-01-context-graph-model-v2`

## STATUS

- **Задача:** `p4-01-context-graph-model-v2` — «P4.01 — ContextGraph model + buildContextGraph (semantic edges)».
- **Final status:** `done` · **PR:** [#8](https://github.com/VladimirMakarevich/wastech-mdlint/pull/8) (открыт, НЕ смержен — `auto_merge:false`) · ветка `feat/p4-01-context-graph-model-v2`.
- **finished_at:** 2026-07-04T16:56:05Z · `fix_iterations=0`, `review_fix_cycles=0`, `test_fix_cycles=0`, `decomposed=false`, `attempt=1`, `manual=false`.
- **Провайдеры:** claude opus-4-8/high (primary), codex gpt-5.5/high (fallback конфига). Побочки: базовое дерево target чистое (task-файл штатно перемещён pending→done + `summary.md` закоммичен); восстановление не требуется.

## Короткий вывод

Прогон **выглядит** идеально зелёным (все узлы succeeded с первой попытки, checks-гейт зелёный, PR открыт), но это **false-green**: кросс-провайдерное review (codex) нашло **три реальных `high` blocking**-нарушения AC/инвариантов, а оркестратор записал вердикт `accept` с пустым списком находок, пропустил `fixing` и опубликовал PR #8 с этими багами как «done». Супервайзер поверх этого выдал уверенное «clean run, no interventions needed».

**Единственный главный рычаг (P0):** узел-evaluator извлекает находки **только из `structured_output`**, но роль-промпт ревью просит их **прозой** и схема провайдеру не передаётся (`output_schema=None`) → находок ноль → вердикт fail-open в `accept`. Починка — сделать evaluator fail-**closed** (findings-схема ИЛИ прозы-парсер; при неразборе → `rework`/`manual`, не `accept`). Рычаг: [core/flow/nodes/evaluator.py:178](../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L178) + `_extract_findings` (стр. 268).

## Как прошёл прогон (фактический путь по флоу)

Флоу `implementation` (task_type=implementation), супервайзер — постоянный слой, проверяющий каждый завершённый шаг:

| # | Узел | kind | провайдер / модель / reasoning | attempt / статус | длительность |
| --- | --- | --- | --- | --- | --- |
| — | refinement | agent | — | **skipped** (`nodes.refinement.enabled=false`) | — |
| 1 | planning | agent | claude / opus-4-8 / high | 1 / succeeded | 518s |
| — | supervisor(planning) | — | claude | 1 / succeeded | 27s |
| 2 | implementation | agent | claude / opus-4-8 / high | 1 / succeeded | 432s |
| — | supervisor(impl) | — | claude | 1 / succeeded | 27s |
| 3 | testing | checks | — (typecheck/lint/test/build) | passed | — |
| — | supervisor(testing) | — | claude | 1 / succeeded | 10s |
| 4 | review | evaluator | **codex / gpt-5.4 / xhigh** | 1 / succeeded → **`accept`** | 511s |
| — | supervisor(review) | — | claude | 1 / succeeded | 18s |
| 5 | documentation | agent | claude / opus-4-8 / high | 1 / succeeded | 187s |
| — | supervisor(doc)+final | — | claude | succeeded | 24s+52s |
| 6 | publish | publish | — | published → PR #8 | — |

- **Ретраи/фоллбэки:** нет ни одного. `provider_attempts` все `succeeded`, `error_class` пусто, exit 0. Codex шёл на codex (модель НЕ утекла — контраст с прошлой находкой про cross-provider model-leak).
- **Per-node override сработал:** review перекрыл глобальный конфиг-дефолт codex `gpt-5.5/high` → declared в flow `gpt-5.4/xhigh` (`request.json` argv: `--model gpt-5.4 -c model_reasoning_effort="xhigh"`).
- **HITL-гейтов не было** (planning увёл развилки в plan-mode UX — см. находку F21).
- **Токены/стоимость:** суммарно claude `output≈92k`, `cache_read≈8.67M` (тяжёлое переиспользование контекста). Дороже всего по времени — planning (518s) и review (511s). **Codex review отчитался 0 токенов** (usage не снимается — F22), поэтому полную стоимость прогона посчитать нельзя.

## Находки (по убыванию влияния)

Полные записи — в [TEST-FINDINGS.md](../../TEST-FINDINGS.md) (F19–F23). Кратко:

### F19 (CRITICAL) — review-evaluator фактически no-op

`evaluations` id=85: `review / in_flow_verdict / verdict=accept / findings_json=[]`, при том что `last-message.txt` codex содержит 3 `high` blocking. Флоу пропустил `fixing`, PR открыт как `done`. **Три бага верифицированы в коде PR #8:** (1) anchor-рёбра без валидации фрагмента по heading-slug (`build-context-graph.ts:141-148`); (2) `extractDefinedIds` без «(+ headings)» из AC #3 (стр. 68-72, осознанно отложено в P4.04 — спорно); (3) идентичность узлов из `documents.keys()` при рёбрах на `document.path` (стр. 125 vs 111/142). Причина: промпт просит прозу, экстрактор читает `structured_output`, схема не передаётся → fail-open `accept`. Рычаг: evaluator.py:178 + `_extract_findings` (268) → сделать fail-closed.

### F20 (HIGH) — `current.diff` неполон

Артефакт: 7 файлов, **без тест-файла**, ядро как «Binary files differ». Реальный коммит: 8 файлов, тест `+163` строки, ядро — `Bin` (из-за 3 NUL-байтов в БАЗОВОМ P3-blob). Причина: `write_current_diff` = `git diff <base>` без untracked-файлов и без `--text` ([git_manager.py:1173](../../src/wastech_orchestrator/git_manager.py#L1173)). Ревью-`{diff_path}` и тело PR не видят новых файлов и содержимое NUL-файлов.

### F21 (MEDIUM) — planning plan-mode обходит `human_input`

`--permission-mode plan` → агент вынес 2 реальные развилки через `AskUserQuestion`/`ExitPlanMode` (ответа нет → свои дефолты), `human_input=null` → оркестратор не встал на MANUAL_ACTION_REQUIRED. Рычаг: [claude.py:74](../../src/wastech_orchestrator/providers/claude.py#L74) (`read-only → plan`).

### F22 (LOW) — codex-evaluator usage=0

Стоимость codex-узлов не снимается ([codex.py:211-255](../../src/wastech_orchestrator/providers/codex.py#L211)).

### F23 (LOW, target) — пре-существующий NUL-делимитер в P3

Базовый `build-context-graph.ts` содержал NUL как делимитер ключа → git-binary; задача попутно устранила. Контекст к F20.

## Пробелы в данных

- **`prompt-audit/` присутствует** (`timeline.jsonl` + `000050-planning.json`) — пробела по аудиту промптов НЕТ.
- **Стоимость codex** не снята (F22) — суммарный бюджет прогона неполный.
- **`current.diff` неполон** (F20) — оценка диффа велась по фактическому коммиту ветки (`git show`/`git diff base..branch`), а не по артефакту.
- **`state.db node_runs` был пуст в середине прогона** (флашится на финализации) — это ожидаемо, на терминале данные полные.

## Что уже хорошо (проверено)

- **Изоляция enforced:** агент работал в изолированном workspace, в основной ветке до publish коммита не было — инвариант «commit/push делает только оркестратор» соблюдён.
- **Read-only planning:** `--permission-mode plan` + `--disallowedTools` на git commit/push, gh pr create/merge, `Read(.env)`/`Read(secrets/**)`.
- **Per-task node-skip:** `refinement` skipped с явной причиной в `node_runs.skip_reason` (санкционированное исключение).
- **Независимый checks-гейт:** testing-узел прогнал typecheck/lint/**146 тестов**/build отдельно от самопроверки имплементера — совпало.
- **Per-node model/reasoning override:** review→codex gpt-5.4/xhigh перекрыл глобальный дефолт; зафиксировано в `request.json`.
- **Чистая инфраструктура:** 0 ретраев, 0 фоллбэков, 0 крэшей; модель не утекла между провайдерами.
- **Качество planning/impl по существу:** трезвый скоуп (расширение, не новая инфра), корректные отсрочки (G7 dedup, asset-nodes → P4.06), имплементер отказался переформатировать 123 pre-existing prettier-drift файла (не поддался scope-creep).

## План исправлений

### P0

- **F19 — сделать evaluator fail-closed.** Либо передавать findings-схему (`severity`/`path`/`what`/`fix`) и требовать её в роль-промпте ревью (оба провайдера умеют `--json-schema`/`--output-schema`), либо добавить прозы-парсер по `final_message`/`last-message.txt` при пустом `structured_output`; при невозможности распарсить вердикт — `rework`/`manual`, **никогда** тихий `accept`. Рычаг: `core/flow/nodes/evaluator.py:178,268`. Ожидаемый эффект: review снова гейтит; реальные blocking-баги уходят в `fixing`, а не в PR.

### P1

- **F20 — `current.diff` полный.** В `git_manager.py:1173` включать untracked-файлы (`git add -N`/intent-to-add или явное перечисление) и `--text`. Эффект: review-`{diff_path}`, тело PR и failure-report видят новые файлы и содержимое NUL-файлов.
- **F21 — planning без plan-mode UX.** `claude.py:74`: режим `default`+whitelist (проверив блокировку Write/Edit) ИЛИ disallow `AskUserQuestion`/`ExitPlanMode`. Эффект: реальные развилки идут в `human_input` → HITL-пауза вместо тихого дефолта.

### P2

- **F22 — снимать codex usage** (`codex.py:211-255`). Эффект: полная стоимость прогона.

## Сводная таблица

| Наблюдение | Причина | Рычаг (file:line) | Зона |
| --- | --- | --- | --- |
| review=`accept`, findings=[] при 3 blocking → PR с багами как «done» | промпт просит прозу, экстрактор читает `structured_output`, `output_schema=None` → fail-open | `core/flow/nodes/evaluator.py:178,268` | orchestrator |
| `current.diff` без тест-файла + ядро «Binary files differ» | `git diff <base>` без untracked и без `--text`; NUL в базовом P3-blob | `git_manager.py:1173` | orchestrator (+ target NUL) |
| planning: развилки в `AskUserQuestion`, `human_input=null` | `read-only → plan` активирует plan-mode UX | `providers/claude.py:74` | orchestrator |
| codex review usage=0 | usage не снимается из codex `--json` | `providers/codex.py:211-255` | orchestrator |
| `build-context-graph.ts` git-binary | NUL-делимитер в P3 (пре-существующий, задачей снят) | target `packages/core/src/graph/build-context-graph.ts` | target |
