# Синтез фазы P5: cross-phase разбор всей кампании (6 задач на wastech-mdlint)

STATUS: read-only, 2026-07-07. Охват: 6 задач `p5-01`…`p5-06` (проходы 15–20), все на ветке `feat/p5-compile`, один PR [#11](https://github.com/VladimirMakarevich/wastech-mdlint/pull/11) (не смержен, `branch_mode: existing`, 6 reuse-коммитов). Источники: `state.db` (`tasks`/`node_runs`/`provider_attempts`/`evaluations`/`check_runs`/`node_lineage`), `logs/<task>/**`, `result.json.usage`, TEST-FINDINGS F38–F42, пофазовые отчёты `p5-0{1..6}-*-run-analysis.md`. Не дублирую пофазовые отчёты — опираюсь на них и свожу трендами. Часть C (память) вынесена в отдельный [p5-memory-subsystem-audit.md](p5-memory-subsystem-audit.md).

## Короткий вывод

**Фаза выполнена полностью и чисто: 6/6 задач `done` с 1-й попытки, без декомпозиции, все чеки всегда зелёные, каждый codex-primary баг найден и закрыт в бою.** P5 — это стресс-тест меняющейся codex-конфигурации: проходы 15–17 гоняли **codex как кодера и resume** (нашли и починили F38/F39/F41), проходы 18–20 перевернули на **codex как ревьюера и супервайзера** (подтвердили F24/F41, нашли F42). Оба режима довели задачи до зелёного PR. Главный, ранее не достигавшийся результат: **кросс-вендорное ревью реально исполнилось — в обе стороны** (claude судит codex-код в 15–17; codex судит claude-код в 18–20), тогда как во всей кампании p4 оно было фикцией (F28: 0/9, codex всегда крашился в fallback).

**Единственный крупный рычаг фазы — калибровка блокирующего codex-ревью (F42).** Одна средняя задача (`p5-04-synthesize`) на review=`xhigh` дала **7 rework-циклов / ~2ч20м / +802 строки тестов** и в одиночку составила **~57% wall-времени и ~64% выходных токенов всей фазы**. Ревью прогрессировало (каждый раунд — новая находка, не «двигание ворот») и сошлось в бюджете, находки реальны — но поздние итерации дрейфовали от корректности к полноте тестов, а стоимость непропорциональна. При review=`high` (p5-05/06) loop сходился за 1 цикл. Это не баг, а вопрос дефолтной дотошности; всё остальное в фазе — здорово.

## Свод по 6 задачам

| Задача | Пр. | Итог | fix_it | Режим: plan / impl / fix / doc | review | supervisor (finalize) | Вердикт review | Node-wall | Out-токены |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| p5-01 classify-nodes | 15 | done | 0 | codex / codex / — / **claude(fb)** | claude opus/high | claude(fb) | accept ×1 (0 находок) | ~15 мин | 53k |
| p5-02 doc-profile | 16 | done | 0 | codex / codex / — / codex | claude opus/high | claude(fb) | accept ×1 | ~22 мин | 97k |
| p5-03 describe-rules | 17 | done | 0 | codex / codex / — / codex | claude opus/high | claude(fb finalize) | accept ×1 | ~20 мин | 125k |
| p5-04 synthesize | 18 | done | **7** | claude / claude / claude / claude | **codex gpt-5.4/xhigh** | **codex** | **rework ×7 → accept** | **~2ч20м** | **1157k** |
| p5-05 compile-config-cli | 19 | done | 1 | claude / claude / claude / claude | codex gpt-5.4/**high** | codex | rework → accept | ~34 мин | 225k |
| p5-06 compile-tests | 20 | done | 1 | claude / claude / claude / claude | codex gpt-5.4/**high** | codex | rework → accept | ~18 мин | 152k |

Node-wall = сумма длительностей `node_runs` (не end-to-end). Out-токены — из `result.json.usage` (выходные; входные не привожу — см. «Пробелы»). Провайдер по `node_runs.provider_used`/`node_lineage`. Все 6: `attempt=1`, `decomposition_accepted=0`, `refinement` skipped (per-task), `check_runs` 100% passed (p5-04 — 32/32).

**Стоимость сфокусирована в одной задаче.** Node-wall фазы ≈ 4.2 часа, из них **p5-04 = ~57%**; выходных токенов ≈ 1.81M, из них **p5-04 = 1.157M (64%)**. Внутри p5-04 доминируют codex-supervisor (out=693k — persistent-слой наблюдал все 26 шагов loop-а через resume) и codex-review (out=178k, 8 проходов). По провайдерам за фазу: codex out≈1.31M, claude out≈0.50M — codex сгенерировал ~2.6× больше, что отражает codex в дорогих ролях (supervisor+review) в режиме 18–20.

## Пошаговая оценка узлов через фазу

**refinement** — skipped во всех 6 (per-task, sanctioned). Не оценивается.

**planning** — стабильно succeeded, 1 попытка, 0 фоллбэков в обоих режимах. codex-planning (15–17, 337–428s) по пофазовым отчётам содержателен (p5-01: осознанно пронёс замеченное противоречие спеки про `cycles`); claude-planning (18–20, 355–696s) так же в рамках. Дифф всегда в скоупе. **Хорошо в обоих вендорах.**

**implementation** — succeeded, 1 попытка, 0 фоллбэков. codex-impl (255–572s), claude-impl (194–863s). Диффы соответствуют `task.normalized.json`, без переусложнения и без лишних файлов (проверено пофазово: p5-01 не трогал CLI/config; p5-04 — 9 файлов строго по плану). Самый длинный — p5-04 impl (863s, крупный synthesize). **Стабильно хорошо.**

**testing (checks)** — 8–10s, **всегда зелёные** (4/4 за проход, p5-04 32/32, 0 timeouts, 0 skips). За всю фазу checks **не поймали ни одного бага** — весь rework породило ревью, не тесты. Это либо чистый код на входе в checks, либо неглубокий чек-набор target-а; в любом случае checks здесь — быстрый always-green гейт, не источник сигнала. Монотонность тест-сьюта: растёт по задачам (p5-04 +802 строки тестов, p5-06 — задача целиком про тесты), регрессов чеков нет.

**review** — ключевой гейт фазы и источник единственного крупного рычага. Режим 15–17: **claude opus-4-8/high** судит codex-код → `accept` с 1-й попытки (0–1 находок); быстро (68–142s). Режим 18–20: **codex gpt-5.4** судит claude-код → содержательные `rework`. Качество находок высокое (реальные баги, см. F42), но дотошность на `xhigh` даёт длинный loop. **Работает, но требует калибровки (F42).**

**fixing** — только режим 18–20 (rework был). claude-fixing, 1 попытка на цикл, всегда succeeded, отзывчив на находки ревью (95–1151s; пик — 4-й цикл p5-04). Кросс-провайдерная петля «codex судит → claude чинит → codex перепроверяет» отработала end-to-end. **Хорошо.**

**documentation** — тонкий resume-узел (77–212s). Единственный сбой фазы здесь: p5-01 codex-resume упал `unsupported_version` (F38) → fallback claude (`provider_attempts`: 1 codex unsupported_version за всю фазу — это он). После фикса F38 (p5-02+) documentation на codex без фоллбэка. Режим 18–20 — claude. **Ок; ранний F38-симптом закрыт.**

**supervisor (persistent-слой)** — наблюдает каждый шаг + пишет финальный summary/memory_delta. Режим 15–16: finalize на claude (codex-resume крашил, F38/F39). Проход 17: per-step на codex ок, но finalize упал (F41) → claude-fallback. Проходы 18–20: **finalize на codex без фоллбэка** (F41 закрыт). **Главная стоимостная точка**: на p5-04 (26 наблюдений из-за 7 loop-ов) codex-supervisor сжёг out=693k — persistent-слой масштабируется по числу шагов × resume-контекст (см. F50).

## Устойчивость фиксов и поведение F42

Не пере-нарративю пофазовые подтверждения (сведены в TEST-FINDINGS, проход 20). Cross-phase картина:

- **F38** (codex resume-argv: `--cd/--sandbox/--json/--model` после `resume`) — **VERIFIED FIXED с прохода 16** и держится 16–20: единственный `unsupported_version` за фазу — на p5-01 (до фикса). После — resume-узлы (documentation, supervisor per-step, fixing) на codex без argparse-крашей.
- **F39** (`supervisor.model` без `provider` → claude-модель на codex) — **закрыт вариантом B (проход 17)**: явный `supervisor.provider: codex` + `gpt-5.4`. Остаётся orchestrator-side пробел preflight (унаследованный мисматч не ловится) — для текущего конфига неактуален, valid как защита.
- **F41** (finalize-схемы `DELTA_OUTPUT_SCHEMA`/`_FOLLOW_UPS_SCHEMA` не OpenAI-strict) — **VERIFIED FIXED с прохода 18**, стабилен 18–20: finalize на codex `succeeded`, каталога `2-claude/` нет. Побочно закрыл **F24** (codex-evaluator strict) — codex-review 8/8 (p5-04), 2/2 (p5-05), 2/2 (p5-06), 0 крашей, тогда как в p4 было 9/9 крашей.
- **F24** — **не воспроизводится с прохода 18** (тот же strict-фикс покрыл `_FINDINGS_SCHEMA`). Кросс-вендорное ревью впервые реально исполняется (см. ниже).

**Кросс-вендорное ревью — достигнуто в обе стороны (контраст с F28).** Режим 15–17: `claude opus-4-8` ревьюит codex-авторский код (0/9 в p4 → теперь реальный accept). Режим 18–20: `codex gpt-5.4` ревьюит claude-авторский код (в p4 — всегда fallback на claude, same-vendor). За P5 — 0 фоллбэков ревью в режиме 18–20, независимость ревью фактическая. Это прямое закрытие практического следствия F28.

**F42 — калибровка блокирующего ревью (единственный открытый крупный рычаг).** Содержание вердиктов p5-04 (`evaluations.in_flow_verdict`, 7 rework → accept) показывает дрейф:

- Итерации 1–4 — **корректность**: (1) `synthesize.ts` рендерит «(no documents found)» при пустом `readingOrder`, даже когда файлы есть, но исключены циклами (нарушение G6-honesty); (2) `resolveCompileSettings()` делает whole-object `safeParse` → `{}` на любой ошибке (all-or-nothing вместо lenient-defaulting); (3) то же по-полевой leniency `skill`/`sections`; (4) `contentHash` не включает provenance-строку → два разных SKILL.md с одним хэшем (нарушение S4).
- Итерации 5–7 — **полнота тестов/границы**: (5) `Document Architecture` не покрыт как load-bearing контракт; (6) routed missing-import не ассертит resolved path; (7) `hubMinInDegree` принимает `0/-1/1.5`.

То есть блокирующий `rework` возвращался и на корректность, и на тест-полноту, и на защитную валидацию — по одному батчу за проход. Вклад reasoning vs размер задачи (**не чистый A/B**): p5-04 (xhigh, крупный synthesize) — 7 циклов, review-проходы 300–800s; p5-05/06 (high, меньше) — 1 цикл, review 90–223s. Оба фактора менялись, но направление согласовано: **снижение reasoning блокирующего ревью укорачивает глубину/стоимость loop**. Рычаги (не срочно) — [packaged/flows/roles/implementation/review.md](src/wastech_orchestrator/packaged/flows/roles/implementation/review.md) (батчить находки одного прохода; разделять blocking-корректность vs advisory-полнота), потолок `max_rework_per_stage` на review-узле, либо неблокирующий `testing_quality`-evaluator для coverage. Детали промпт-рычага — в [p5-prompt-quality-per-node.md](p5-prompt-quality-per-node.md).

## Находки по убыванию влияния

Memory-находки (**F43** нечитаемый semantic-балласт, **F44** дубль entity, **F45** редакция subject, **F46** merge-rationale, **F47** пустые эпизоды, **F48** насыщение path-скоупа пакета) — в [p5-memory-subsystem-audit.md](p5-memory-subsystem-audit.md). Ниже — не-memory находки синтеза.

### F42 · Блокирующее codex-ревью чрезмерно дотошно на больших узлах — 7 циклов/задача, дрейф в тест-полировку · **LOW–MEDIUM** · зона **orchestrator (role-prompt/knob)** · статус **OPEN (наблюдение, cross-phase)**

Свод выше. Cross-phase усиление: наблюдение подтверждено на трёх задачах (p5-04 xhigh=7; p5-05/06 high=1), стоимость p5-04 = 64% выходных токенов фазы. Рычаг — role-prompt review + reasoning как регулятор глубины.

### F49 · `tasks.review_fix_cycles` не персистится (=0 при 7 фактических review-реворках) · **LOW** · уверенность HIGH · зона **orchestrator (observability)** · статус **OPEN**

**Доказательство.** `state.db`: `SELECT review_fix_cycles FROM tasks WHERE task_id LIKE 'p5-%'` → **0 во всех 6**, включая p5-04, где `evaluations.in_flow_verdict` содержит **7 rework** и `fix_iterations=7` (корректен). То есть общий счётчик fix-итераций пишется, а типизированный `review_fix_cycles` — нет.

**Влияние / рычаг.** Наблюдаемость: по `tasks` нельзя отличить review-реворки от test-реворков (важно для калибровки F42 и для будущей аналитики). Рычаг — точка персиста review_fix-счётчика в машине состояний/оркестраторе (там же, где инкрементится `fix_iterations`). Мелкий audit-пробел, отдельный от F42 (как отмечено в TEST-FINDINGS).

### F50 · Стоимость persistent-супервайзера супер-линейна по глубине fix-loop (наблюдает каждый шаг через resume) · **LOW–MEDIUM** · уверенность HIGH · зона **orchestrator (supervisor)** · статус **OPEN (наблюдение)**

**Доказательство.** `result.json.usage` p5-04: supervisor out=**693k** при 26 наблюдениях (`evaluations.supervisor_step`=26) — потому что 7 review-loop-ов умножают число шагов, а каждый шаг — codex-resume с полным контекстом own-lineage. Для сравнения supervisor out: p5-05=105k (8 шагов), p5-06=95k (8 шагов), p5-03=50k. То есть supervisor-стоимость растёт с числом node-run-ов, а число node-run-ов — с глубиной loop-а: длинное ревью удорожает не только review+fixing, но и supervisor.

**Влияние / рычаг.** На больших задачах с глубоким loop-ом persistent-supervisor становится крупнейшим потребителем токенов (p5-04). Рычаги (идеи): не пере-наблюдать неизменившиеся шаги (наблюдать только delta), или наблюдать на пониженном reasoning; точка — цикл наблюдения супервайзера [core/supervisor.py](src/wastech_orchestrator/core/supervisor.py). Связано с F42 (укрощение loop-а автоматически снижает и supervisor-стоимость).

**Вторичные наблюдения (не новые F, уже в TEST-FINDINGS):** `error_class` маскирует корневую причину — F38 писал `unsupported_version` для нашей же argparse-ошибки, F39/F41 писали `process_crashed` для 400-ответа модели/схемы; это затрудняет триаж (отмечено при F38/F39). Preflight не ловит унаследованный `supervisor.provider` мисматч (под-пункт F39, OPEN). F40 (`depends_on` × `branch_mode: existing`) — обойдено снятием `depends_on`.

## Пробелы в данных

- **Входные токены не сравнимы.** `result.json.usage.input_tokens` для codex-resume раздут кэш-контекстом (codex-supervisor p5-04 in=95M) — это переотправляемый контекст, не стоимость генерации. Веду анализ стоимости по **выходным** токенам; входные не привожу как метрику. (Codex usage теперь вообще пишется — F22 закрыт.)
- **Не чистый A/B по F42.** p5-04 vs p5-05/06 различаются и reasoning (xhigh→high), и размером задачи (synthesize vs config/tests) — вклад факторов не разделён. Честно: направление согласовано, величина — нет.
- **Причинность влияния памяти на поведение** не выделяется из артефактов (агенты читают бриф, но прямых «применил урок» следов мало) — см. memory-аудит §4.
- **HITL / декомпозиция / single-provider=codex / test_fix-цикл** в P5 не задействованы (0 human_input, 0 decomposition, всегда ≥1 провайдер, весь rework — review-driven) — эти пути фазой не покрыты.

## Что уже хорошо (проверено)

- **Инфраструктура почти безупречна:** 6/6 `done` с 1-й попытки; за фазу 42 provider-attempt succeeded, единственная нестабильность — 1 `unsupported_version` (F38, до фикса) с корректным фоллбэком. `branch_mode: existing` + PR-reuse собрали 6 коммитов в один PR #11 без сбоев.
- **Все codex-primary баги закрыты в бою** (F38/F39/F41/F24) — codex стал полноценным провайдером во всех ролях.
- **Кросс-вендорное ревью реально исполняется в обе стороны** (закрытие практического F28).
- **Кросс-провайдерная петля review-fix** (codex судит → claude чинит → codex перепроверяет) отработала на p5-04/05/06 без сбоев.
- **Чеки всегда зелёные, тест-сьют растёт монотонно**, диффы в скоупе без переусложнения (проверено пофазово).
- **Ревью ловит реальные баги** (G6-honesty, all-or-nothing leniency, contentHash-provenance, `--cwd`-резолвинг, тавтологичный тест) — качество гейта высокое.

## План действий

**P0 (сделать до масштабирования фазы):** ничего блокирующего — фаза здорова. Единственный кандидат «сначала» — **F42-калибровка**, если стоимость/латентность важны: дефолт review=`high` для крупных кодовых узлов и/или батч-режим находок в role-prompt.

**P1 (ближайшее):** F42 (role-prompt review + `max_rework_per_stage`); F49 (персист `review_fix_cycles`); memory-**F43** (не писать entity-дублирующие уроки) и **F48** (инкрементальный дифф для пакета) — см. memory-аудит.

**P2 (наблюдение/защита):** F50 (supervisor delta-observe/пониженный reasoning); F39-preflight (унаследованный мисматч); F40 (предупреждение при `depends_on` × own `branch_ref`); error_class-триаж (не маскировать argparse/400). Memory F44/F45/F46/F47/F37 — см. memory-аудит.

## Сводная таблица находок

| Наблюдение | Причина | Рычаг (file:line) | Зона / F |
| --- | --- | --- | --- |
| Блокирующее codex-ревью: 7 циклов на p5-04, дрейф в тест-полноту, 64% токенов фазы | `blocking:true` + role-prompt возвращает rework на каждый HIGH, вкл. coverage; xhigh усиливает | [packaged/flows/roles/implementation/review.md](src/wastech_orchestrator/packaged/flows/roles/implementation/review.md); reasoning review-узла; `max_rework_per_stage` | orchestrator / **F42** |
| `review_fix_cycles=0` при 7 реворках | типизированный счётчик не персистится | точка персиста в машине состояний (рядом с `fix_iterations`) | orchestrator / **F49** |
| supervisor out=693k на p5-04 (super-линейно по loop) | persistent-слой наблюдает каждый шаг через resume | [core/supervisor.py](src/wastech_orchestrator/core/supervisor.py): delta-observe / пониженный reasoning | orchestrator / **F50** |
| `error_class` маскирует причину (argparse→`unsupported_version`, 400→`process_crashed`) | классификация по симптому, не по источнику | адаптеры [providers/codex.py](src/wastech_orchestrator/providers/codex.py) | orchestrator / F38·F39 (вторично) |
| preflight пропускает унаследованный `supervisor.provider` мисматч | валидация только на явный provider | `validate_flow_against_config` / `SupervisorConfig` | orchestrator / F39 (OPEN) |
| Память: нечитаемый semantic-балласт, дубли, релевантность | см. отдельный аудит | см. [p5-memory-subsystem-audit.md](p5-memory-subsystem-audit.md) | orchestrator / **F43–F48** |
