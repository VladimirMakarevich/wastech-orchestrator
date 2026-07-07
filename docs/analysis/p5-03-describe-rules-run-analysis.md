# Разбор прогона — `p5-03-describe-rules` (чистый codex-primary, supervisor тоже codex)

## STATUS

**Задача:** `p5-03-describe-rules` — «P5.03 describeRules» **Итог:** `done` · ветка `feat/p5-compile` (`branch_mode: existing`) · PR [#11](https://github.com/VladimirMakarevich/wastech-mdlint/pull/11) **reuse** (3-й коммит, не смержен) · `fix_iterations=0` · attempt 1 · без декомпозиции **finished_at:** 2026-07-07T12:11:14Z **Версия:** 0.8.9a3. **Смена конфигурации перед прогоном** (вариант B): supervisor зафиксирован полностью на codex — `provider: codex`, `model: gpt-5.4`, `reasoning: xhigh` (закрытие F39 согласованной codex-конфигурацией, а не обходом claude).

## Короткий вывод

Первый **почти полностью codex-primary** прогон: planning/implementation/documentation + **все 5 per-step supervisor-наблюдений на codex без единого фоллбэка**. Вариант B закрыл F39: явный `supervisor.provider: codex` + валидная `gpt-5.4` убрали `400 model not supported`, durable-сессия supervisor на codex отработала. F38 снова подтверждён.

Осталась одна щель: **финальный supervisor-summary (finalize) крашит codex** (`invalid_json_schema`) → fallback на claude. Причина — `DELTA_OUTPUT_SCHEMA`/`_FOLLOW_UPS_SCHEMA` не OpenAI-strict-совместимы (тот же класс, что F24). **Единственный главный рычаг — привести finalize-схемы супервизора к OpenAI-strict** ([memory/delta.py](../../src/wastech_orchestrator/memory/delta.py), [core/supervisor.py](../../src/wastech_orchestrator/core/supervisor.py)).

## Как прошёл прогон

| Узел | Провайдер / модель | Попытки | Итог | Время |
| --- | --- | --- | --- | --- |
| refinement | — | — | skipped | — |
| planning | **codex** gpt-5.4/xhigh (fresh) | 1 | succeeded | 337s |
| implementation | **codex** gpt-5.4/xhigh (fresh) | 1 | succeeded | 516s |
| testing | checks | — | pass 4/4 | ~9s |
| review | **claude** opus-4-8/high (fresh) | 1 | **accept** | 115s |
| documentation | **codex** gpt-5.4 (resume) | 1 | succeeded | 80s |
| publish | — | — | PR #11 reuse | ~6s |
| **supervisor** per-step ×5 | **codex** gpt-5.4/xhigh (resume) | 1 каждый | **succeeded (все на codex)** | 19–115s |
| **supervisor** finalize | codex (resume) → **fallback claude** | 2 | succeeded (claude) | codex-краш 1.8s + claude 129s |

Общее время ~25 мин. `test_fix_cycles=0`, `review_fix_cycles=0`.

## Находки по убыванию влияния

### F39 — закрыт для per-step вариантом B ✅

Явный `supervisor.provider: codex` + `gpt-5.4` устранил `400: "claude-opus-4-8 not supported with ChatGPT account"`. 5 per-step supervisor-наблюдений (`run-000123..127/1-codex`) — succeeded, 0 фоллбэков. durable-сессия supervisor на codex (`resume_own_lineage`) отработала. Orchestrator-side пробел preflight (не ловит унаследованный мисматч) остаётся как защита, но для этого конфига неактуален.

### F41 — finalize-схема супервизора не OpenAI-strict → codex-краш на finalize (MEDIUM, новая)

**Доказательство.** `stages/supervisor/run-000000/1-codex/stdout.log`: `invalid_request_error / invalid_json_schema — Invalid schema for response_format 'codex_output_schema': context=('properties','memory_delta','properties','lessons','items','properties','scope'), 'required' … Missing 'paths'`, status 400 → `turn.failed`, `exit_code=1 error_class=process_crashed`. Fallback на claude (129s, succeeded). Per-step supervisor (observe-turn, другая схема) на codex прошли — ломается только finalize с `memory_delta`.

**Корневая причина.** OpenAI strict structured-output требует `required` = все ключи `properties` у каждого объекта. Нарушают: [memory/delta.py:110-118](../../src/wastech_orchestrator/memory/delta.py#L110-L118) — `scope` без `required`; lesson-объект [delta.py:122](../../src/wastech_orchestrator/memory/delta.py#L122); [_FOLLOW_UPS_SCHEMA supervisor.py:98-113](../../src/wastech_orchestrator/core/supervisor.py#L98). Раньше не всплывало — supervisor-finalize всегда шёл на claude. Прямой родственник F24.

**Рычаг.** Привести `DELTA_OUTPUT_SCHEMA` ([memory/delta.py:96-165](../../src/wastech_orchestrator/memory/delta.py#L96)) и `_FOLLOW_UPS_SCHEMA` ([supervisor.py:98-113](../../src/wastech_orchestrator/core/supervisor.py#L98)) к OpenAI-strict (`required` = все ключи; опциональность через nullable-типы), либо общий codex-адаптерный «strict-ify» перед отправкой (как для F24). Зона — orchestrator.

**Влияние.** Замаскировано claude-fallback'ом. Но на `agents.allowed:[codex]`/без claude finalize-summary + memory_delta + follow_ups **всегда** проваливались бы (нет PR-body/памяти); на codex-supervisor каждый прогон = 1 сожжённая codex-попытка на finalize; memory_delta codex-супервизора не пишется (пишет claude-fallback).

## Пробелы в данных

- Токены/стоимость по узлам детально не выгружались.
- Причина того, что observe-turn супервизора использует иную (совместимую) схему, а finalize — `DELTA_OUTPUT_SCHEMA`, принята из кода (observe без memory_delta); отдельно схему observe не сверял.

## Что уже хорошо

- **Почти полный codex-primary достигнут**: весь кодо-флоу + per-step оверсайт на codex, 0 фоллбэков до finalize. Вариант B (supervisor codex) — валидный способ закрыть F39.
- **F38** снова подтверждён (documentation resume на codex).
- **branch_mode: existing + PR reuse** — 3-й коммит подряд на общей ветке, PR #11.
- **Diff чистый и в скоупе** (describeRules: `describe-rules.ts` +237, тест +238, barrel +123, doc +29); review-claude accept с первого раза.

## План исправлений

**P0**

- **F41:** привести finalize-схемы супервизора (`DELTA_OUTPUT_SCHEMA`, `_FOLLOW_UPS_SCHEMA`) к OpenAI-strict — чтобы codex-supervisor писал summary/memory_delta/follow_ups сам, а не через claude-fallback. Родственно F24; проверить, нет ли ещё codex-несовместимых output-схем.

**P1**

- **F39 (orchestrator):** расширить preflight-валидацию supervisor на унаследованный provider (защита на будущее).
- **F40 (orchestrator UX):** warning при `depends_on` на задачу с тем же `branch_ref`.

## Сводная таблица

| Наблюдение | Причина | Рычаг | Зона |
| --- | --- | --- | --- |
| 5 per-step supervisor на codex без крашей | вариант B: явный `supervisor.provider: codex` + валидная `gpt-5.4` | — (F39 закрыт для per-step) | target-config |
| finalize supervisor крашит codex (invalid_json_schema) | `DELTA_OUTPUT_SCHEMA`/`_FOLLOW_UPS_SCHEMA` не OpenAI-strict (`required` ≠ все ключи) | [memory/delta.py:110-118](../../src/wastech_orchestrator/memory/delta.py#L110-L118) + [supervisor.py:98](../../src/wastech_orchestrator/core/supervisor.py#L98) | orchestrator |
| documentation resume на codex | F38-фикс | — (VERIFIED) | orchestrator |
| PR #11 reuse, 3-й коммит | `branch_mode: existing` | — (позитив) | — |
