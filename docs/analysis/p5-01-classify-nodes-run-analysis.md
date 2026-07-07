# Разбор прогона — `p5-01-classify-nodes` (первый прогон с codex-primary)

## STATUS

**Задача:** `p5-01-classify-nodes` — «P5.01 classifyNodes and analyzeGraph» **Итог:** `done` · ветка `feat/p5-compile` · PR [#11](https://github.com/VladimirMakarevich/wastech-mdlint/pull/11) (открыт, не смержен — `auto_merge:false`) **finished_at:** 2026-07-07T08:58:17Z · `fix_iterations=0` · attempt 1 · без декомпозиции **Конфигурация:** первый прогон с переключённым primary — глобальный `primary` перенесён с claude на **codex `gpt-5.4`/`xhigh`**; рабочие узлы флоу перепинены на codex (planning/implementation/fixing/documentation); review-узел перевёрнут на **claude `opus-4-8`/high**; claude = fallback. Изменения внесены оператором до прогона, `preflight: ready`.

## Короткий вывод

Прогон **успешен по продукту**: codex как основной провайдер написал корректный, точно-в-скоупе код (4 файла, +257/−3), все 4 чека зелёные, claude-ревью приняло с первого раза (0 findings), PR открыт. Смена primary и переворот review отработали как задумано на **fresh**-узлах.

Но вскрыт **единственный главный рычаг — F38**: codex `exec resume` собирается адаптером с флагами (`--cd`/`--sandbox`/`--model`/`-c`), которые установленная **codex 0.142.5** отвергает (`error: unexpected argument '--cd'`). Из-за этого **весь resume-путь на codex падает 100%** и держится только на fallback'е к claude: supervisor-оверсайт де-факто выполнялся на claude (×6), documentation — тоже на claude. То есть заявленная «codex-primary» на resume-узлах не соблюдается, а при недоступном claude (или `agents.allowed:[codex]`) прогон бы захлебнулся на documentation и каждом supervisor-шаге. **Чинить F38 в `providers/codex.py` — приоритет №1.**

## Как прошёл прогон (фактический путь по флоу)

| Узел | Провайдер / модель / reasoning | Попытки | Итог | Время |
| --- | --- | --- | --- | --- |
| refinement | — | — | skipped (per-task `enabled:false`) | — |
| planning | **codex** gpt-5.4 / xhigh (fresh) | 1 | succeeded | 356s |
| implementation | **codex** gpt-5.4 / xhigh (fresh) | 1 | succeeded | 255s |
| testing | checks (npm) | — | pass 4/4 | ~9s |
| review | **claude** opus-4-8 / high (fresh) | 1 | **accept**, 0 findings | 68s |
| documentation | codex gpt-5.4 (resume) → **fallback claude** opus-4-8 | 2 | succeeded (на claude) | 107s |
| publish | — | — | published, PR #11 | ~7s |
| **supervisor** (слой, не узел) | codex (resume) → **fallback claude** ×6 | 2 каждый | succeeded (на claude) | 25–120s/шаг |

Общее время ~19 мин. `test_fix_cycles=0`, `review_fix_cycles=0`.

**Токены/стоимость** (`result.json.usage`): codex-узлы — большой вход ~1.0M токенов каждый, но почти целиком из кэша (planning: in 996 453 / cached 896 768, out 14 694, reasoning 9 970; implementation: in 1 013 097 / cached 887 296, out 11 241, reasoning 4 818). Claude-узлы дешевле по входу с кэшем (review: cache_read 175 022 + cache_creation 56 600, out 4 176; documentation: cache_read 535 729, out 5 559). Каждый resume-краш codex сжигал одну provider-attempt + добавлял латентность fallback'а (~0.17–2s краш + повторный запуск на claude).

**Наблюдения по маршрутизации (позитив):**

- Глобальный `primary=codex` реально развёл маршрутизацию: fresh-узлы (planning, implementation) выполнены на codex без фоллбэка.
- Переворот review сработал: `route resolved node_id=review primary=claude fallback=codex source=flow_node`, ревью выполнено на claude.
- Кросс-провайдерный fallback codex→claude корректно **сбросил и модель, и сессию**: documentation-fallback ушёл с `session_id: None` (свежая сессия) и `--model claude-opus-4-8` (не утёкший `gpt-5.4`), контекст подан через `plan_path`+`diff_path`. Это подтверждает фикс cross-provider-model-leak в направлении codex→claude.

## Находки по убыванию влияния

### F38 (HIGH) — codex `exec resume` строится с флагами, которые codex 0.142.5 отвергает → весь resume-путь падает на codex

**Доказательство.** `stages/documentation/run-000113/1-codex/stderr.log` + все `stages/supervisor/run-*/1-codex/stderr.log`: `error: unexpected argument '--cd' found` / `Usage: codex exec resume <SESSION_ID> [PROMPT]`. `request.json` argv (documentation): `codex --ask-for-approval never exec resume [REDACTED] --cd <dir> --sandbox workspace-write --json … --model gpt-5.4 -c model_reasoning_effort="medium" -`; `result.json`: `exit_code=2`, `error_class=unsupported_version`. `state.db provider_attempts`: documentation `codex attempt=1 unsupported_version` → `claude attempt=2 succeeded`. Контраст: fresh-сессии codex (planning/implementation, без `resume`) — `succeeded`.

**Корневая причина.** [providers/codex.py:146-159](../../src/wastech_orchestrator/providers/codex.py#L146-L159) добавляет `resume <SESSION_ID>`, затем безусловно дописывает `--cd/--sandbox/--json/--output-last-message` (+ ниже `--model`, `-c`). Комментарий: «verified on codex-cli 0.139.0». В **codex 0.142.5** грамматика `exec resume` сузилась до `resume <SESSION_ID> [PROMPT]` — `--cd` (первый дописанный флаг) отвергается парсером. Сломанное допущение о версии CLI. Вторично: `error_class=unsupported_version` маскирует, что это argparse-ошибка нашего же argv.

**Рычаг.** [providers/codex.py:146-159](../../src/wastech_orchestrator/providers/codex.py#L146-L159) — для resume-ветки строить argv по грамматике 0.142.5 (проверить `codex exec resume --help`: перенести `--cd`/`--sandbox`/approval в допустимое положение или в `-c`-конфиг). Закрепить контракт версии рядом с capability-probe [codex.py:300-310](../../src/wastech_orchestrator/providers/codex.py#L300). Зона — **orchestrator** (пакетный адаптер, задевает все репо на codex 0.142.x).

### F39 (MEDIUM) — `supervisor` имеет `model` без `provider` → под codex-primary уводит claude-модель на codex

**Доказательство.** `.worc/config.yaml` блок `supervisor`: `model: claude-opus-4-8`, `reasoning: high`, **без `provider`** → унаследован `primary=codex` (`route resolved node_id=supervisor primary=codex … source=config`). `stages/supervisor/run-000000/1-codex/request.json` argv: `… --model claude-opus-4-8 -c model_reasoning_effort="high"` — claude-модель на codex. Падает раньше на `--cd` (F38), поэтому мисматч модели даже не валидируется.

**Корневая причина.** Резолвинг провайдера supervisor берёт глобальный primary при отсутствии явного `provider`, хотя `model` задан claude-специфичный; нет проверки согласованности model↔provider (в отличие от flow-узлов, где её ловит `validate_flow_against_config`). Ср. [config/schema.py](../../src/wastech_orchestrator/config/schema.py) (`SupervisorConfig`).

**Рычаг.** (а) `SupervisorConfig` получает свой `provider` + валидацию model↔provider, либо (б) быстрый обход в target — `provider: claude` в блок `supervisor`. Даже после починки F38 без этого supervisor-codex падал бы на невалидной модели. Зона — **orchestrator** (валидация/резолвинг), временный обход — target-config.

## Пробелы в данных

- `prompt-audit/timeline.jsonl` присутствует (`prompt_audit:true`) — использовался косвенно; детально по узлам не разбирался (не требовалось, path ясен из `state.db` + логов).
- **Память:** `state.db evaluations` показывает 1 `memory_write` append (эпизод `ep_p5-01-classify-nodes` + entity `compile-graph-analysis`) и **2 `quarantine`** (`ltm_e86463bb3050`, `ltm_aac6b1f3f755`). Причина карантина двух long-term записей в этом прогоне не разбиралась — вероятная связь с открытыми F29/F30 (рекуррентность/словарь evidence); отдельно не подтверждено. Требует проверки, не квартинит ли cleanup валидные записи.
- Различие сигнатур первого supervisor-краша (`process_crashed`, ~2s в логе) и последующих (`unsupported_version`, ~0.17s) не докопано до конца — доминирующая и воспроизводимая причина по stderr/request одна: `--cd` resume-rejection.

## Что уже хорошо

- **Codex как основной кодер справился**: план (codex/gpt-5.4/xhigh) сильный — заметил внутреннюю противоречивость спеки про `cycles` и осознанно провёл его через результат; имплементация ровно по плану, без scope creep (CLI/config не тронуты, как план и обещал).
- **Diff в скоупе и минимален**: `graph-analysis.ts` (+71) + barrel `index.ts` (+9) + тест (+141) + doc (+39). Оба критерия приёмки закрыты фактическим кодом.
- **Checks-гейт железный**: 4/4 (typecheck/lint/test/build), 0 timeout.
- **Переворот review на claude** и **глобальный primary=codex** отработали по маршрутизации без сюрпризов.
- **Кросс-провайдерный fallback** корректно сбросил модель и сессию (codex→claude), не утекла codex-модель на claude — фикс cross-provider-model-leak подтверждён в этом направлении.
- **Claude-ревью на codex-коде** дало осмысленный `accept` с 0 findings — кросс-вендорное ревью (обратное F28) фактически исполнилось.

## План исправлений

**P0**

- **F38** — починить построение argv для `codex exec resume` под codex 0.142.x ([providers/codex.py:146-159](../../src/wastech_orchestrator/providers/codex.py#L146-L159)). Без этого codex-primary на resume-узлах фиктивен (всё делает claude), а без claude — hard-fail. Тест: fake-CLI-сценарий на resume-argv + capability-probe версии.

**P1**

- **F39** — дать `SupervisorConfig` явный `provider` + валидацию model↔provider (или чётко документировать наследование primary и требовать согласованную модель). Быстрый обход в target: `provider: claude` в блок supervisor.
- Уточнить классификацию: argparse-rejection нашего argv не должен маскироваться под `unsupported_version` — это скрывает баг построения команды.

**P2**

- Разобраться с 2 `quarantine` long-term записей памяти в этом прогоне (связь с F29/F30) — не квартинит ли cleanup валидное знание.

## Сводная таблица

| Наблюдение | Причина | Рычаг | Зона |
| --- | --- | --- | --- |
| Все codex resume-узлы (supervisor ×6, documentation) падают, держатся на claude-fallback | `codex exec resume` собирается с `--cd`/`--sandbox`/`--model`, отвергаемыми codex 0.142.5 | [providers/codex.py:146-159](../../src/wastech_orchestrator/providers/codex.py#L146-L159) — argv по грамматике 0.142.x | orchestrator |
| Supervisor уводит `--model claude-opus-4-8` на codex | `supervisor.model` без `provider` → наследует `primary=codex`, нет валидации model↔provider | `SupervisorConfig` + резолвинг ([config/schema.py](../../src/wastech_orchestrator/config/schema.py)); обход — `provider: claude` в target | orchestrator (+ target-config) |
| `error_class=unsupported_version` вместо «bad argv» | Классификация exit 2 codex по версии, а не по argparse-ошибке | точка классификации ошибок codex-адаптера | orchestrator |
| Fresh-узлы codex (planning/implementation) — чисто | Fresh-путь argv валиден для 0.142.5 | — (позитив) | — |
| Fallback codex→claude сбросил модель и сессию | Router корректно очищает codex-модель/сессию при кросс-провайдерном переключении | — (позитив, фикс подтверждён) | — |
| 2 long-term записи памяти квартинированы | Вероятно связано с F29/F30 (рекуррентность/словарь evidence) | требует отдельной проверки | orchestrator (не подтверждено) |
