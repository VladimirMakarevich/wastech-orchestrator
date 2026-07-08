# Разбор прогона — `p5-05-compile-config-cli` (стабильность F41/F24, review reasoning=high)

## STATUS

**Задача:** `p5-05-compile-config-cli` — «P5.05 compile config section and CLI compile command» **Итог:** `done` · ветка `feat/p5-compile` (`branch_mode: existing`) · PR [#11](https://github.com/VladimirMakarevich/wastech-mdlint/pull/11) **reuse** (5-й коммит, не смержен) · **`fix_iterations=1`** · attempt 1 · без декомпозиции **finished_at:** 2026-07-07T20:20:40Z **Версия:** 0.8.9a4. **Конфигурация:** узлы planning/implementation/fixing/documentation = claude/claude-sonnet-5/xhigh; **review = codex/gpt-5.4/`high`** (оператор снизил reasoning с xhigh); supervisor = codex/gpt-5.4/xhigh; primary = claude.

## Короткий вывод

Чистый, короткий прогон — **подтверждение стабильности фиксов, без новых находок**. F41 (finalize на codex) и F24 (codex-review) держатся; ни одного фоллбэка. review-fix-loop сошёлся за **1 цикл** (реальный `--cwd`-баг → fix → accept) против 7 у p5-04 — что согласуется с рычагом F42: review на `high` менее дотошен/быстрее, чем на `xhigh`.

## Как прошёл прогон

planning (claude 696s) → implementation (claude 507s) → testing (4/4) → review (**codex** 171s, `rework`) → fixing (claude 138s) → testing (4/4) → review (**codex** 223s, **accept**) → documentation (claude 177s) → finalize supervisor (**codex**) → publish (PR #11 reuse).

- **review**: 2 прохода, codex, оба `succeeded` (171s, 223s). Вердикты: rework → accept.
- **provider_attempts**: planning/implementation/fixing/documentation = claude; review = codex ×2 — **все succeeded, 0 fallback**.
- **supervisor**: per-step + finalize — все на codex, без fallback.
- **testing**: оба прохода 4/4 green.
- **Коммит:** 13 файлов, **+420/−166** (`config/config-schema.ts` +40 strict `compile`-секция, `compile-context.ts` упрощён, CLI `compile`-команда, `engine/schema.ts`, тесты `config-v2` +82 / `cli` +79 / `schema-generation` +17).

## Находки

**Новых нет.** Подтверждения:

### F41 — стабилен ✅

`stages/supervisor/run-000000/1-codex/result.json`: `status=succeeded exit_code=0`, каталога `2-claude/` нет (finalize на codex без fallback). memory_delta записан codex-супервизором (`evaluations.supervisor_final` `memory_delta:true, follow_ups:1`).

### F24 — стабилен ✅

`provider_attempts`: review/codex ×2 `succeeded`, 0 `process_crashed`/`invalid_json_schema`. codex-evaluator дал содержательный rework (реальный `--cwd`-баг) и затем accept.

### F42 — усиление наблюдения (review reasoning как регулятор дотошности)

review переведён оператором на `high` (с `xhigh`). Loop сошёлся за **1 rework-цикл**; review-проходы 171–223s. Для сравнения p5-04 (review xhigh, узел synthesize) — 7 циклов / 300–800s. Не чистый A/B (p5-05 меньше synthesize), но направление согласуется с рычагом F42: **reasoning блокирующего review — практический регулятор глубины/стоимости loop**; `high` вместо `xhigh` по умолчанию для больших кодовых узлов стоит рассмотреть как дефолт.

## Пробелы в данных

- Не A/B: p5-05 меньше p5-04, поэтому вклад «review=high» vs «меньшая задача» в короткий loop не разделён окончательно.
- Токены/стоимость по узлам детально не выгружались.

## Что уже хорошо

- **Codex-primary во всех codex-ролях устойчив**: review + весь supervisor (per-step + finalize) на codex без единого фоллбэка — второй прогон подряд после p5-04.
- **Эффективный loop**: 1 реальный баг пойман и починен, accept со второго review; `fix_iterations=1`.
- **Diff в скоупе** (config `compile`-секция + CLI compile + тесты), scope creep нет; PR #11 reuse (5-й коммит фазы).
- Реальная польза ревью: `--cwd` для относительного `--config` — настоящий UX-баг, пойман до мержа.

## План исправлений

Нет P0/P1 из этого прогона. Действующие orchestrator-side рычаги (в [follow_ups.md](../backlog/follow_ups.md)): F42 (калибровка review — дополнить рычагом reasoning=high для больших узлов), review_fix_cycles counter, F39-preflight, F40 warning.

## Сводная таблица

| Наблюдение | Причина | Рычаг | Зона |
| --- | --- | --- | --- |
| finalize+review на codex, 0 fallback | F41/F24 фиксы (strict-схемы) держатся | — (стабильно) | orchestrator |
| loop сошёлся за 1 цикл (против 7 у p5-04) | review reasoning=high (+ меньшая задача) | F42: reasoning review как регулятор дотошности | orchestrator (flow-node) |
| реальный `--cwd`-баг пойман ревью | codex-evaluator по существу | — (позитив) | — |
| PR #11 reuse, 5-й коммит | `branch_mode: existing` | — (позитив) | — |
