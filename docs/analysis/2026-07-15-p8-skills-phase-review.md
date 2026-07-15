# Post-mortem фазы: P8 «Skills» (`p8-01` … `p8-05`) на wastech-mdlint

- **Целевой репозиторий:** `/Users/a1234/Documents/GitHub/wastech-mdlint`
- **Прогоны:** `p8-01-frontmatter-schema-model`, `p8-02-skill-init`, `p8-03-skill-fix`, `p8-04-skill-impact`, `p8-05-skills-validation`
- **Ветка / PR:** все пять на `feat/p8-skills` → [PR #14](https://github.com/VladimirMakarevich/wastech-mdlint/pull/14) (не авто-мёрдж)
- **Финальный статус:** все пять `done`, `attempt: 1`, decomposition выключена, `terminal_cleanup: completed`
- **Окно прогона:** 2026-07-15 12:37 → 16:34 UTC (~4 часа сплошняком)
- **Флоу:** `implementation` (packaged, но с переопределёнными нодами — см. ниже)
- **Анализ:** только чтение артефактов `.worc/` + исходников оркестратора. Правки не вносились.

---

## Вердикт (кратко)

Фаза завершилась **успешно и без единого инфраструктурного сбоя** — 62 запуска провайдеров, 0 ошибок, 0 реальных фолбэков, 0 крашей, все проверки всегда зелёные. Кросс-провайдерное ревью на Codex работает отлично и ловит настоящие, конкретные баги. Это здоровый прогон.

Единственная реальная проблема — **цена**: фаза потратила **21 круг доработки (`review_fix`), все до единого — из ревью, ни одного из тестов**. Две задачи (`p8-02` = 8 кругов, `p8-05` = 6) съели непропорционально много. Корневая причина одна и та же во всех пяти задачах: нода `implementation` работает на **`reasoning: low`** (ниже глобального дефолта `high`), а нода `fixing` — на `medium` и чинит строго процитированную ревьюером строку, не перепроверяя исходник. В связке с сильным ревьюером (Codex `gpt-5.4` @ high) это даёт «чистку луковицы»: слабый автор оставляет закладки → сильный ревьюер находит их по одной за круг → фиксер лечит ровно одну → следующий круг находит соседнюю того же класса.

**Одно изменение с наибольшим эффектом:** поднять `implementation.reasoning` `low → high` в [флоу целевого репо](../../../wastech-mdlint/.worc/flows/implementation.yaml) (строка 68). Ревью — доминирующая статья расходов фазы (~17,2 млн входных токенов), и оно масштабируется линейно от числа кругов; сокращение кругов бьёт точно по этой статье.

---

## Рамка прогона (факты)

Идентичный путь по флоу у всех пяти задач: `planning → implementation → testing(pass) → review(rework) → fixing → testing(pass) → review(rework) → … → review(accept) → documentation → publish`.

| Задача | fix_iters | review-reworks | ревью-запусков | fixing-запусков | supervisor-вызовов | файлов в diff | +строк |
| --- | --: | --: | --: | --: | --: | --: | --: |
| p8-01 frontmatter-schema-model | 2 | 2 | 3 | 2 | 12 | 7 | 244 |
| **p8-02 skill-init** | **8** | **8** | **9** | **8** | **30** | 2 | 271 |
| p8-03 skill-fix | 3 | 3 | 4 | 3 | 15 | 2 | 257 |
| p8-04 skill-impact | 2 | 2 | 3 | 2 | 12 | 2 | 209 |
| **p8-05 skills-validation** | **6** | **6** | **7** | **6** | **24** | 7 | 861 |
| **Итого** | **21** | **21** | **26** | **21** | **93** | — | — |

Ключевые наблюдения из `state.db`:

- `test_fix_cycles = 0` и `test_fix_total = 0` у **всех** задач; `review_fix_total == fix_iterations` у всех. Значит **100 % кругов — это ревью-реворки**, тесты не спровоцировали ни одного круга.
- `node_runs`: у каждой ноды `stage_attempts = 1`, `route_fallback` заполнен (сконфигурированный запасной провайдер), но `provider_used` всегда равен `route_primary` — **фолбэк ни разу не сработал**.
- `provider_attempts`: 36 успешных запусков Claude + 26 успешных Codex, `error_class` пустой везде, `exit_code = 0` везде.
- `check_runs`: `npm run typecheck / lint / test / build` — `passed = 1`, `timed_out = 0` во всех 26 прогонах проверок.

### Как раскладываются провайдеры и модели по нодам (активный флоу целевого репо)

| Нода | kind | Провайдер | Модель | Reasoning | Права |
| --- | --- | --- | --- | --- | --- |
| planning | agent | claude | claude-opus-4-8 | **high** | read-only |
| implementation | agent | claude | claude-opus-4-8 | **low** ⚠️ | workspace-write |
| testing | checks | — | — | — | — |
| review | evaluator | codex | gpt-5.4 | **high** | read-only |
| fixing | agent | claude | claude-opus-4-8 | **medium** | workspace-write |
| documentation | agent | claude | claude-opus-4-8 | low | workspace-write |

> **Важно про scope правок.** В **packaged**-флоу [`implementation.yaml`](../../src/wastech_orchestrator/packaged/flows/implementation.yaml) все per-node `provider/model/reasoning` **закомментированы** — дефолт наследует глобальные `claude@high` / `codex@xhigh`. Значит `implementation: low`, `fixing: medium`, `review: codex@high` — это **осознанные переопределения оператора** в копии целевого репо. Поэтому почти все рекомендации ниже — **target-only** (правится `.worc/...` в wastech-mdlint), а не packaged-дефолт.

---

## Стоимость (почему круги — это дорого)

Агрегировано по всем `result.json` фазы:

- **Ревью (Codex) — доминанта:** ~**17,2 млн** входных токенов суммарно за 26 вызовов (≈14,7 млн из кэша, ≈2,5 млн «свежих»), ~351 тыс. выходных/reasoning. Один вызов ревью грузит ~650 тыс. токенов контекста.
- **Claude-ноды (planning+impl+fixing+doc+supervisor):** ~**42,7 млн** токенов контекста (в основном cache-read), ~373 тыс. выходных.
- **`p8-02` в одиночку:** ревью 9 вызовов = 5,84 млн входных / 131 тыс. выходных; fixing 8 вызовов = 5,94 млн контекста / 35 тыс. выходных; supervisor 30 вызовов.

Всё, что множится на число кругов, — ревью, fixing и supervisor — и есть цена реворков. Прикидка: если бы `p8-02` уложился в ~2 круга (как `p8-01`/`p8-04`), только по ревью экономия ≈ 6 × 650 тыс. ≈ **3,9 млн входных токенов** на одной задаче, плюс 6 лишних прогонов fixing и ~18 лишних вызовов supervisor.

---

## Находки (по убыванию влияния)

### F1 — [model/reasoning] Нода `implementation` на `reasoning: low` недомощна для работы «точное соответствие контракту». **severity: high · confidence: high · scope: target-only**

**Свидетельства.** [`implementation.yaml:68`](../../../wastech-mdlint/.worc/flows/implementation.yaml) — `reasoning: low`. Из `events.jsonl` ноды implementation (`p8-02/stages/implementation/run-000218`) видно, что агент **прочитал** нужные исходники — `packages/cli/src/program.ts`, `packages/cli/src/init-command.ts`, `skill-model.ts`, `skill-frontmatter.ts`, `synthesize.ts` — и всё равно в `final_message` заявил, что «ссылается только на поверхности, подтверждённые в program.ts (`init`, `lint`, `--config`, …)», хотя `init` **не принимает** `--config`. Первое же ревью (`p8-02` round 1, `evaluations.findings_json`) вернуло 3 blocking: непортируемый раннер lint, неверный CI-flow, README с npm вместо vendor-neutral install. В `p8-01` автор недовалидировал инвариант «repo-relative POSIX path», который **прямо прописан** в его же роль-промпте (`## Hard Invariants`).

**Корневая причина.** Контекст у автора был — не хватило бюджета рассуждения, чтобы точно синтезировать его. Класс задач фазы (описать в SKILL.md портируемый бутстрап поверх CLI с 5 пакет-менеджерами; ужесточить публичный инвариант пути) — тонкая correctness-работа, а `low` даёт правдоподобные-но-неверные артефакты. Оператор занизил ноду ниже глобального `high` до `low`, экономя копейки на авторе (вывод ~5,8 тыс. токенов) и переплачивая многократно на ревью.

**Рычаг.** [`.worc/flows/implementation.yaml`](../../../wastech-mdlint/.worc/flows/implementation.yaml), нода `implementation`: `reasoning: low → high` (как минимум `medium`). Модель менять не нужно.

**Ожидаемый эффект.** Меньше закладок в артефакте → меньше кругов ревью → прямое сокращение доминирующей статьи расходов. Это самый высокорычажный пункт фазы.

### F2 — [prompt/flow] `fixing` лечит ровно процитированную строку и не перепроверяет исходник → «чистка луковицы». **severity: high · confidence: high · scope: target-only**

**Свидетельства.** `events.jsonl` прогонов fixing (`p8-02` FIX#1 `run-000221` и FIX#8 `run-000242`) показывают, что фиксер открывал **только** `findings.json` ревью + сам `SKILL.md` — и **ни разу** не переоткрывал `program.ts`/`init-command.ts`. Ревью отдаёт готовое поле `fix:` (см. `result.json` ревью), и фиксер по сути переносит его дословно. Роль [`fixing.md`](../../../wastech-mdlint/.worc/flows/implementation/fixing.md) требует «make the minimal change needed to resolve them». Итог `p8-02`: 8 последовательных однопунктовых кругов, каждый лечит процитированное, а свежий (fresh_disposable) ревьюер находит соседний дефект того же класса.

**Корневая причина.** Фиксер оптимизирован на минимализм и не имеет инструкции: получив finding про фактическое утверждение об **этом же продукте** (CLI/MCP/схема), переоткрыть процитированный источник и «подмести» весь артефакт на другие экземпляры того же класса. Плюс `fixing.reasoning: medium`.

**Рычаг.** (1) [`.worc/flows/implementation/fixing.md`](../../../wastech-mdlint/.worc/flows/implementation/fixing.md) — добавить блок «class sweep»: для findings о фактах про собственный продукт после починки процитированного места перечитать авторитетный источник и проверить весь артефакт на однотипные ошибки, а не только указанную строку. (2) [`implementation.yaml`](../../../wastech-mdlint/.worc/flows/implementation.yaml), нода `fixing`: `reasoning: medium → high`.

**Ожидаемый эффект.** Схлопывает несколько скрытых findings в один круг; именно это сжало бы `p8-02`/`p8-05`.

### F3 — [prompt] Ни у одной ноды нет режима «авторская/документационная поставка»; шаг Verify чисто TS-код-центричный. **severity: med-high · confidence: high · scope: target-only**

**Свидетельства.** [`implementation.md`](../../../wastech-mdlint/.worc/flows/implementation/implementation.md) `## Verify` = `npm run typecheck / test / build`. Эти проверки **проходили каждый круг**, пока проза SKILL.md была неверной (`init --config`, `graph --format summary`, `readingOrder` наоборот). Весь класс ошибок фазы **невидим** для чек-сета, потому что поставка `p8-02/03/04` — чистый Markdown, который не трогает компилируемый код. Автор сам это отметил в `final_message`: «typecheck/test/build не имеют релевантной поверхности».

**Корневая причина.** Роль-промпт заточен под изменения TS-кода (это верно для большинства задач репо), но P8 — фаза авторинга. Нет дисциплины: «факты, утверждаемые о собственном CLI/MCP/схеме, обязаны быть сверены с авторитетным источником; каждый флаг привязан к владеющей им команде; стандартные проверки прозу не валидируют».

**Рычаг.** [`.worc/flows/implementation/implementation.md`](../../../wastech-mdlint/.worc/flows/implementation/implementation.md) — добавить секцию «Authoring / documentation deliverables». Packaged-дефолт трогать не нужно (он намеренно generic).

**Ожидаемый эффект.** Дисциплина сверки переносится в implementation, до ревью доезжает меньше ошибок.

### F4 — [prompt] Планирование перечисляет флаг CLI, не привязывая к владеющей команде — сеет повторяющуюся ошибку `init --config`. **severity: low-med · confidence: med · scope: target-only**

**Свидетельства.** [`p8-02/plan.md`](../../../wastech-mdlint/.worc/logs/p8-02-skill-init/plan.md) строка 26: «Reference only surfaces confirmed in program.ts: init, lint, **--config**, --yes, --on-existing, --with-ci-workflow» — плоский список. Implementation затем написал `init --config`; ревьюер поймал, что у `init` нет `--config` (`p8-05` round 4, `p8-02`). Роль [`planning.md`](../../../wastech-mdlint/.worc/flows/implementation/planning.md) уже требует «verify every path against the current tree», но не «привяжи каждый флаг к команде».

**Рычаг.** [`.worc/flows/implementation/planning.md`](../../../wastech-mdlint/.worc/flows/implementation/planning.md) — при перечислении поверхности продукта для последующей документации привязывать каждый флаг/опцию/значение к конкретной владеющей команде и цитировать строку источника.

**Ожидаемый эффект.** Убирает конкретную повторяющуюся закладку.

### F5 — [flow/cost] Каждый круг реворка гоняет 4 npm-проверки и ~3 advisory-вызова supervisor, бесполезные для класса «проза vs CLI». **severity: low (эффективность) · confidence: high · scope: наблюдение**

**Свидетельства.** 26 прогонов testing (все pass), 93 advisory-вызова supervisor — всё множится на число кругов; для `p8-02/03/04` единственная поставка — Markdown, который проверки не читают.

**Рычаг.** Множитель — число кругов, поэтому лечится через F1/F2/F3 (апстрим). Отдельно отключать `testing` для авторских задач не рекомендую (дёшево и страхует). Пункт информационный.

**Ожидаемый эффект.** Снижение цены — побочный эффект F1–F3.

---

## Что уже хорошо (проверено — не трогать)

- **Инфраструктура безупречна.** 62 запуска провайдеров, 0 ошибок, 0 реальных фолбэков, 0 крашей/auth/transient. `stage_attempts = 1` везде.
- **Кросс-провайдерное ревью — главный актив фазы.** Codex `gpt-5.4` @ high выдаёт конкретные, корректные, действенные findings (структура path/what/fix), полный набор за один проход, ловит настоящие баги портируемости и дрейфа skill↔CLI. Именно оно удержало качество. Оставить как есть — это **не** overkill.
- **Проверки всегда зелёные, без флаки.** Чек-сет (typecheck/lint/test/build) адекватен код-задачам `p8-01`/`p8-05`.
- **Diff'ы точно соответствуют замыслу, scope-крипа нет.** `p8-02/03/04` трогают ровно 2 файла (SKILL.md + свой phase-doc), `p8-01`/`p8-05` — core+тесты по делу. Нода `documentation` корректно обновляла только phase-doc.
- **Планирование сильное** (opus @ high) — планы подробные и в основном точные (`p8-02/plan.md` заранее предугадал chicken-and-egg раннера, version-coupling, orchestrate-not-reimplement).
- **Полная аудируемость.** `prompt_audit: true` + `logging.artifacts: full`. Дыр в данных практически нет.
- **Спеки адекватны.** `validation_passed = 1` у всех; задачи хорошо ссылаются на источники требований.

---

## Дыры в данных

Минимальны (аудит включён, артефакты полные). Единственное: я не читал все 93 advisory-вывода supervisor, чтобы оценить, окупает ли **поштучное** (каждый шаг) наблюдение свою немалую токен-цену при том, что ревью и так держит качество. Если стоимость критична — стоит отдельно оценить каденс supervisor (каждый шаг против finalize-only). Это вторично и не влияет на F1–F4.

---

## Итоговый список рычагов (все — target-only, кроме прямо указанного)

1. **F1 (high):** [`implementation.yaml`](../../../wastech-mdlint/.worc/flows/implementation.yaml) нода `implementation` → `reasoning: high`.
2. **F2 (high):** [`fixing.md`](../../../wastech-mdlint/.worc/flows/implementation/fixing.md) + `implementation.yaml` нода `fixing` → `reasoning: high`; добавить «class sweep».
3. **F3 (med-high):** [`implementation.md`](../../../wastech-mdlint/.worc/flows/implementation/implementation.md) — секция про авторские/док-поставки.
4. **F4 (low-med):** [`planning.md`](../../../wastech-mdlint/.worc/flows/implementation/planning.md) — привязка флагов к владеющей команде.

Детальные пофайловые разборы каждой задачи — в соседних файлах `2026-07-15-p8-0X-*.md`.
