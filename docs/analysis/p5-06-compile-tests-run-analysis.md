# Разбор прогона — `p5-06-compile-tests` (финал фазы P5)

## STATUS

**Задача:** `p5-06-compile-tests` — «P5.06 compile tests and fixtures» **Итог:** `done` · ветка `feat/p5-compile` (`branch_mode: existing`) · PR [#11](https://github.com/VladimirMakarevich/wastech-mdlint/pull/11) **reuse** (6-й/последний коммит фазы, не смержен) · **`fix_iterations=1`** · attempt 1 · без декомпозиции **finished_at:** 2026-07-07T20:51:33Z **Версия:** 0.8.9a4. **Конфигурация:** узлы claude/claude-sonnet-5/xhigh; review = codex/gpt-5.4/`high`; supervisor = codex/gpt-5.4/xhigh; primary = claude.

## Короткий вывод

Финальная задача фазы P5 — чистый короткий прогон, **без новых находок**. F41/F24 держатся (3-й прогон подряд codex во всех codex-ролях без фоллбэков), loop сошёлся за 1 цикл. Вся цепочка P5 (p5-01…p5-06, 6 коммитов) собрана в одном PR #11. Кампания codex-primary завершена: все баги (F38/F39/F41/F24) закрыты и подтверждены.

## Как прошёл прогон

planning (claude 355s) → implementation (claude 194s) → testing (4/4) → review (**codex** 90s, `rework`) → fixing (claude 120s) → testing (4/4) → review (**codex** 98s, **accept**) → documentation (claude 88s) → finalize supervisor (**codex**) → publish (PR #11 reuse).

- **review**: 2 прохода, codex, оба `succeeded` (90s, 98s). rework → accept.
- **provider_attempts**: planning/implementation/fixing/documentation=claude, review=codex ×2 — **все succeeded, 0 fallback**.
- **finalize supervisor**: codex `succeeded exit 0`, без `2-claude/` (F41).
- **testing**: оба прохода 4/4 green.
- **Коммит:** 5 файлов, **+96/−8** (`compile-context.test.ts` +33, `compile-synthesize.test.ts` +15, `cli.test.ts` +11, docs `06-compile-tests.md` +37 / `index.md`).

## Находки

**Новых нет.** Подтверждения:

- **F41 стабилен** (3-й прогон): finalize на codex без fallback (`stages/supervisor/run-000000/1-codex` succeeded exit 0).
- **F24 стабилен**: codex-review 2/2 succeeded, 0 крашей. rework был содержательным — поймал **тавтологичный тест** (CJK-budget тест считал `expected` через тот же `estimateTokens`, что использует SUT `compileContext()`), что для задачи «compile tests and fixtures» точно в цель.
- **F42**: review на `high` → снова 1 rework-цикл (как p5-05). Согласуется с рычагом (reasoning регулирует дотошность).

## Пробелы в данных

- Токены/стоимость по узлам детально не выгружались (короткий прогон, вердикт ясен).

## Что уже хорошо

- **Codex-primary во всех codex-ролях устойчив третий прогон подряд** (p5-04/05/06): review + supervisor (per-step + finalize) на codex, суммарно 0 фоллбэков.
- **Ревью тест-качества по существу**: тавтологичный ассерт — реальный дефект теста, пойман до мержа (ровно то, что нужно на тест-задаче).
- **Вся фаза P5 в одном PR #11**: `git log main..feat/p5-compile` = 6 коммитов p5-01…p5-06; `branch_mode: existing` + PR reuse отработали всю цепочку.
- **Diff в скоупе** (тесты + фикстуры + docs), scope creep нет.

## Итог фазы P5 / кампании codex-primary (проходы 15–20)

- **F38** (codex resume-argv) — VERIFIED FIXED (Проход 16).
- **F39** (supervisor provider/model) — closed вариантом B (явный `supervisor.provider: codex`, Проход 17); orchestrator-side preflight-пробел в follow_ups.
- **F41** (finalize strict-схемы) — VERIFIED FIXED (Проход 18), стабилен 18/19/20.
- **F24** (codex-evaluator strict) — не воспроизводится с Прохода 18 (codex-review 8/8, 2/2, 2/2 succeeded).
- **F40** (depends_on × branch_mode:existing) — обойдено (снят depends_on), orchestrator-warning в follow_ups.
- **F42** (глубина/калибровка блокирующего codex-review) — открытое наблюдение; регулируется reasoning review-узла; в follow_ups.

Codex — полноценный основной провайдер во всех ролях. Не покрыто за кампанию (для будущих прогонов): HITL, decomposition, single-provider=codex, test_fix-цикл.

## План исправлений

Нет P0/P1 из этого прогона. Действующие orchestrator-side рычаги — в [follow_ups.md](../backlog/follow_ups.md): F42, review_fix_cycles counter, F39-preflight, F40.

## Сводная таблица

| Наблюдение | Причина | Рычаг | Зона |
| --- | --- | --- | --- |
| finalize+review на codex, 0 fallback (3-й прогон) | F41/F24 фиксы держатся | — (стабильно) | orchestrator |
| loop 1 цикл, ревью поймало тавтологичный тест | review=high + codex-evaluator по существу | — (позитив) | — |
| вся фаза P5 в одном PR #11 (6 коммитов) | `branch_mode: existing` + PR reuse | — (позитив) | — |
