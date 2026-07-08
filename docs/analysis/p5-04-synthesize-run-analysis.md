# Разбор прогона — `p5-04-synthesize` (проверка F41 + F24 на codex-ревью, глубокий fix-loop)

## STATUS

**Задача:** `p5-04-synthesize` — «P5.04 synthesize and CompileResult» **Итог:** `done` · ветка `feat/p5-compile` (`branch_mode: existing`) · PR [#11](https://github.com/VladimirMakarevich/wastech-mdlint/pull/11) **reuse** (4-й коммит, не смержен) · **`fix_iterations=7`** · attempt 1 · без декомпозиции · ~2ч40м **finished_at:** 2026-07-07T19:23:06Z **Версия:** 0.8.9a4 (F41-фикс + guard-тест). **Конфигурация:** узлы planning/implementation/fixing/documentation = claude/claude-sonnet-5/xhigh; review = codex/gpt-5.4/xhigh; **supervisor = codex/gpt-5.4/xhigh** (проверка F41); глобальный primary = claude.

## Короткий вывод

Ключевой верификационный прогон — **два фикса подтверждены в бою и одна новая находка**:

1. **F41 VERIFIED FIXED**: finalize-supervisor выполнился на codex без fallback, memory_delta записан codex-супервизором. Strict-схемы работают.
2. **F24 не воспроизводится**: codex-evaluator (review) отработал 8 проходов подряд без единого краша (в p4 падал 9/9) — тот же strict-фикс закрыл и `_FINDINGS_SCHEMA`.
3. **F42 (новая)**: codex-as-reviewer чрезмерно дотошен — 7 rework-циклов на одной задаче (~2ч40м), дрейф от корректности к тест-полировке. Не баг, но дефолтная калибровка blocking-review дорога на больших узлах.

Задача доехала до `done` с чистым, хорошо покрытым тестами кодом; главный «рычаг» на будущее — калибровка глубины review (F42).

## Как прошёл прогон

planning (claude 652s) → implementation (claude 863s) → **[testing(4/4) → review(codex) → fixing(claude)] ×7** → review **accept** → documentation (claude 212s) → **finalize supervisor (codex, 90s)** → publish (PR #11 reuse).

- **review**: 8 проходов, все codex, все `succeeded` (300–800s каждый). Вердикты: 7×`rework` → `accept`.
- **fixing**: 7 проходов, все claude, все `succeeded`.
- **supervisor**: per-step ×много + finalize — все на codex `succeeded` (0 fallback).
- **testing**: каждый проход 4/4 green; `passed=false`=0 — test_fix-цикл ни разу не запускался, все фиксы review-driven.
- **Коммит:** 9 файлов, **+1547/−17** (`synthesize.ts` +382, `compile-context.ts` +223, `skill-frontmatter.ts` +25, `llm.ts` +35, `index.ts` +12, doc +85, тесты `compile-synthesize` +438 / `compile-context` +307 / `rules-llm` +57).

## Находки по убыванию влияния

### F41 — VERIFIED FIXED ✅ (finalize-схема супервизора на codex)

`stages/supervisor/run-000000/1-codex/result.json`: `status=succeeded exit_code=0` (90s), каталога `2-claude/` нет → finalize на codex без fallback. `"task finalize: supervisor summary written"`. memory_delta записан codex-супервизором: `evaluations.supervisor_final` = `{"summary_written":true,"memory_delta":true,"follow_ups":1}`, `memory_write` append `ep_p5-04-synthesize` + entities `core-synthesize`/`core-compile-context`/`core-skill-frontmatter`/`llm001-rule`. Strict-схемы (`DELTA_OUTPUT_SCHEMA`/`_FOLLOW_UPS_SCHEMA`/…) в 0.8.9a4 устранили `invalid_json_schema`.

### F24 — не воспроизводится ✅ (codex-evaluator review)

`provider_attempts`: review/codex/`succeeded` ×8, 0 `process_crashed`/`invalid_json_schema`. В кампании p4 codex-review падал 9/9 (F24, `_FINDINGS_SCHEMA` без strict). На 0.8.9a4 тот же F41-класс-фикс покрыл `_FINDINGS_SCHEMA` → codex-ревью исполняется и даёт содержательные вердикты. F24 закрыт тем же фиксом.

### F42 — codex-review чрезмерно дотошен (7 rework-циклов, LOW–MEDIUM, новая)

**Доказательство.** `evaluations`: 7×rework→accept. Итерации 1–4 — реальные корректностные HIGH (G6-honesty пустого `readingOrder`; all-or-nothing `resolveCompileSettings`; per-field leniency `skill`/`sections`; `contentHash` без provenance). Итерации 5–7 — полнота тестов/ассертов и валидация границ (`Document Architecture` без unit-теста; routed missing-import не ассертит resolved path; `hubMinInDegree` принимает `0/-1/1.5`). Все чеки зелёные; `fix_iterations=7`; diff +1547 (тесты +802); ~2ч40м. Loop прогрессировал (каждый раунд НОВОЕ) и сошёлся в пределах бюджета.

**Корневая причина.** review-роль + `blocking:true` evaluator по умолчанию возвращают `rework` на каждый HIGH, включая полноту тестов и защитную валидацию; мощная модель на большом узле (synthesize) → длинный последовательный loop (батч находок за проход).

**Рычаг (не срочно).** (1) review-роль: группировать все находки прохода в один батч + явно разделять blocking-корректность vs advisory-полнота; (2) `max_rework_per_stage` на review-узле как потолок; (3) неблокирующий `testing_quality`-evaluator для coverage-замечаний. Зона — orchestrator (role-prompt + flow-node knob).

**Влияние.** Качество высокое (баги пойманы, тесты усилены), но одна средняя задача = 7 циклов / ~2ч40м / +802 строки тестов; дефолтный blocking-review может доминировать стоимость на больших узлах.

## Пробелы в данных

- `state.db tasks.review_fix_cycles=0` при 7 фактических review-реворках (`fix_iterations=7` корректен) — счётчик review_fix, похоже, не персистится; мелкий audit-пробел, отдельно от F42.
- Токены/стоимость по узлам детально не выгружались (loop длинный; вердикт ясен по таймингам/аттемптам).

## Что уже хорошо

- **F41 и F24 закрыты одним классом фикса (strict-схемы)** и подтверждены в бою — codex теперь полноценный и для finalize-супервизора, и для evaluator-ревью.
- **Кросс-провайдерный review-fix-loop работает end-to-end**: codex судит → claude чинит → codex перепроверяет, 7 итераций до accept, все чеки зелёные.
- **Качество ревью высокое**: первые 4 находки — настоящие корректностные баги в synthesize (G6, leniency, hash, config-parse), реально улучшившие результат.
- **Diff в скоупе** задачи (synthesize + CompileResult + тесты), scope creep нет; PR #11 reuse (4-й коммит).

## План исправлений

**P1**

- **F42:** откалибровать глубину blocking-review (батчинг находок / `max_rework_per_stage` / вынести coverage в неблокирующий evaluator) — снизить длину loop на больших узлах без потери correctness-качества.

**P2**

- Починить персист `tasks.review_fix_cycles` (сейчас 0 при реальных реворках).
- (ранее) F39-системный preflight, F40 warning `depends_on`×`branch_ref`.

## Сводная таблица

| Наблюдение | Причина | Рычаг | Зона |
| --- | --- | --- | --- |
| finalize supervisor на codex succeeded, memory_delta записан | F41-фикс (strict-схемы) в 0.8.9a4 | — (VERIFIED FIXED) | orchestrator |
| codex-review 8/8 succeeded (в p4 было 9/9 crash) | тот же strict-фикс покрыл `_FINDINGS_SCHEMA` | — (F24 закрыт) | orchestrator |
| 7 rework-циклов, дрейф в тест-полировку, ~2ч40м | blocking-review возвращает rework на каждый HIGH вкл. coverage | review-роль батчинг / `max_rework_per_stage` / testing_quality | orchestrator |
| `review_fix_cycles=0` при 7 реворках | счётчик не персистится | точка обновления `tasks.review_fix_cycles` | orchestrator |
| узлы claude, review+supervisor codex — как задано | per-node override + supervisor.provider | — (позитив) | target-config |
