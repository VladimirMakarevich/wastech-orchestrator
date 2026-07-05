# P4 — план исправления качества ролевых промптов по узлам

План устранения всех находок из [p4-prompt-quality-per-node.md](../../../analysis/p4-prompt-quality-per-node.md) (Часть B сквозного разбора P4-кампании). Это **промпт-слой**: точные правки текста ролевых промптов + системная проблема расхождения target-копий и packaged-дефолтов. Кодовые рычаги тех же находок (F24/F28/F32/F31/F29/F33/F34) уже расписаны в [p4-findings-remediation-plan.md](p4-findings-remediation-plan.md) — здесь они **не дублируются**, а помечены «см. код: A1/A3/A4/B1/F1/F2»; данный план отвечает только на вопрос «какой текст в каком промпт-файле поменять и как этот фикс доходит до всех инсталляций».

Формат пункта: **Цель · Файл-рычаг · Было → Стало (точная формулировка) · Delivery (target / packaged / оба) · Тест · Связь с кодовым планом**.

> **Статус реализации (2026-07-05).** План выполнен. Итог по пунктам:
>
> - **T1** (review.md → пустой `findings`-массив вместо прозы) — ✅ сделано (target).
> - **T2** (review.md → защита от кумулятивного / pre-documentation диффа) — ✅ сделано: недостающий пункт про кумулятивный дифф добавлен в target `review.md`, а generic-версия всех трёх guardrail'ов — в **packaged** `review.md` (P2); два других пункта (source-path+symbol, «doc — позже») в target уже были.
> - **T3** (implementation.md + review.md → carve-out для упорядоченных последовательностей) — ✅ уже было применено ранее (target); не переприменялось.
> - **T4** (documentation.md → шаг «flip phase-doc → Done») — ✅ сделано (target).
> - **T5** (planning.md → реальные пути core-примитивов) — ✅ уже было применено ранее (target); пути сверены с репо 2026-07-05 (`parse-document.ts` / `build-context-graph.ts` / `discovery/` / `engine/tokens.ts` существуют, `llm/` нет).
> - **T6 / F31** (мёртвый `{memory_path}`-блок) — ✅ снят кодом: код-план A4 выбрал «прокинуть пакет» (`evaluator.py._memory_path` строит packet как agent-раннер), блок живой — правок промпта ноль, ни в target, ни в packaged.
> - **P1 / F29-prompt** (summary.md → словарь `evidence.type`) — ⏭️ намеренно пропущено (решение оператора): код B1 уже распознаёт токены `file`/`commit`, которые супервайзер фактически пишет (~99% указателей кампании), поэтому жёсткий список токенов в общем промпте был бы избыточен и склонен к дрифту (та самая болезнь, против которой этот план). `evidence.type` остаётся свободной строкой (не enum).
> - **Системное (§0)** — в [follow_ups.md](../../follow_ups.md) заведён отдельный candidate (lightweight preflight-проверка существования project-путей в `.worc/flows/*.md`, ловит F34-класс); T1 помечен acceptance-кейсом будущего `upgrade-flows`.
>
> **Расхождение с планом:** «Было»-цитаты местами устарели — часть правок (T3, T5, два из трёх пунктов T2) уже была применена предыдущей сессией к target-копиям `.worc/flows/` (они gitignored, поэтому история недоступна). Эти пункты не переприменялись; применены только реально отсутствовавшие (T1, кумулятивный-дифф-пункт T2, T4) + packaged-эхо (P2).

## Ключевая рамка: target-копия ≠ packaged-дефолт (и в какую сторону дрифт)

Активные промпты, которые реально рендерились в кампании, — это **editable-копии в target-репо** `wastech-mdlint` (`.worc/flows/implementation/*.md`), посеянные при `install` (см. [[install-seeds-flows-and-prompts]]). Они **переопределяют** пакетные дефолты (`_copy_packaged_flows`), поэтому у каждой правки есть два адреса и разное направление дрифта:

| Правка | target-копия | packaged-дефолт | Направление дрифта |
| --- | --- | --- | --- |
| review.md «## Output» (F28) | **прозаический стиль, до-F19** | **уже исправлен** (empty `findings`, поля `path/what/fix`) | target отстал назад → **pull из packaged** |
| review.md diff-контекст (F32) | нет оговорки | нет оговорки | обоих → добавить в **оба** |
| summary.md словарь `evidence.type` (F29) | идентичен packaged | идентичен packaged | **дефект дефолта** → править packaged, ре-синк target |
| review.md `{memory_path}` (F31) | мёртвый блок стр. 48 | **тоже мёртвый блок стр. 3** | **дефект дефолта** → решение в коде A4, затем оба |
| implementation.md + review.md sort-инвариант (F33) | target-кастомизация | **в packaged блока нет** | только target |
| documentation.md phase-doc шаг (doc/F32) | размытая формулировка | generic (без project-структуры) | только target (+ опц. generic в packaged) |
| planning.md primitive-пути (F34) | **устаревшие пути** | generic (путей нет) | только target |

Вывод: 3 находки — чисто локальный target-дрифт (F33, doc/F32, F34), 1 — target отстал от уже-исправленного packaged (F28), 2 — дефект packaged-дефолта, повторённый в target (F29, F31), 1 — пробел в обоих (F32).

## Секция 0 — системная причина: у посеянных промптов нет проверки актуальности

Все 7 находок — симптом одного: **посеянная в `.worc/flows/` копия живёт своей жизнью и никак не сверяется ни с кодом репо, ни с packaged-дефолтом.** Отсюда обе беды сразу — target дрифтует ВПЕРЁД (planning-пути протухли относительно v2-монорепо) и ОТСТАЁТ назад (review не получил F19-фикс). Точечные правки ниже чинят конкретные случаи, но не механизм.

**Рычаг (уже в бэклоге, не заводить дубль).** `upgrade-flows` — умный ре-синк посеянных `.worc/flows/` к packaged (обновлять неотредактированные built-in-файлы, репортить дрифт на отредактированных, не трогать кастомные флоу) — запланирован в [follow_ups.md](../../follow_ups.md) (строка `2026-06-23 upgrade-flows`). Сегодня единственный путь обновления — `install --reconfigure`, который бэкапит и перезатирает **все** packaged-именованные файлы, теряя операторские правки.

**Рекомендация.** Правки этого плана выполнить точечно сейчас, но два из них (F28-re-sync, F31-block) — это ровно то, что должен был бы сделать `upgrade-flows`; пометить их как **acceptance-кейсы для `upgrade-flows`** (когда его будут строить, эти два должны воспроизводиться автоматически: «packaged ушёл вперёд → предложить обновить неотредактированный блок»). Дополнительно — самое дешёвое усиление, не дожидаясь `upgrade-flows`: **lightweight preflight-проверка актуальности кастом-промптов**, которая грепает project-пути, упомянутые в `.worc/flows/*.md`, и предупреждает о несуществующих (ловит F34-класс). Это отдельный кандидат — вынести в follow_ups, не раздувать данный план.

---

## Секция 1 — target-копии (`.worc/flows/implementation/*.md` в `wastech-mdlint`)

Правки в target-репо (additional working dir), а не в оркестраторе. Порядок = приоритет из тюнинг-листа Части B.

### T1 · F28/F24-prompt (MEDIUM) — review.md «## Output» отстал от packaged F19-фикса

**Цель.** Убрать расхождение текста с контрактом evaluator'а: `output_schema=_FINDINGS_SCHEMA`, fail-**closed** при отсутствии `{"findings": …}` ([evaluator.py:134-142](../../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L134)). Инструкция «сказать одной строкой прозой» формально ведёт к `manual`; claude-фоллбэк не попался только потому, что схема принудительна.

**Файл-рычаг.** target [.worc/flows/implementation/review.md:11](/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/review.md).

**Было (target, стр. 11):**

```
- If nothing blocks, say so in one line.
```

**Стало (ре-синк с packaged review.md):**

```
- No findings means the diff is clean — return an empty `findings` array, not prose.
```

Заодно сверить, что «## Output» target называет поля схемы явно (`severity` / `path` / `what` / `fix`) — packaged формулирует их в лид-абзаце («set `path` … `what` … `fix`»); при желании подтянуть и лид, но минимально достаточно правки стр. 11.

**Delivery.** **Только target** (packaged уже корректен). Направление — pull из packaged.

**Тест.** Кампанийная проверка: review-узел на «чистом» диффе возвращает `findings:[]`, не прозу; вердикт `accept`, не `manual`.

**Связь с кодом.** F24 (`additionalProperties:false` в `_FINDINGS_SCHEMA`) — код-план **A1**; F28 (кросс-вендор review не бежит) — **A2**. Этот пункт — их промпт-компонент.

### T2 · F32-prompt (MEDIUM) — review.md не защищён от кумулятивного / pre-documentation диффа

**Цель.** Снять два наблюдавшихся класса ложных находок: (1) ложный scope-drift на код прежних задач цепочки (p4-06: «index.ts exports the full P4.02–P4.05 surface» — реальная дельта 2 строки, дифф был кумулятивным по общей ветке); (2) повторяющийся ложный «phase-doc не обновлён» (documentation бежит ПОСЛЕ review). Плюс сделать line-refs пригодными для fixing-агента (наблюдались нерезолвимые `coverage.ts:529-539` при файле в 97 строк).

**Файл-рычаг.** target [.worc/flows/implementation/review.md](/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/review.md) — секции «## Requirements And Correctness» / «## Blocking Invariant Violations».

**Стало (добавить в «## Requirements And Correctness»):**

```
- The diff may be cumulative: on a shared branch it can include files committed by earlier tasks. Judge only the changes that belong to **this task's plan** — do not flag prior-task code as scope drift.
- Documentation is updated by a **later** step in this flow. Do **not** flag missing doc, changelog, or phase-file updates as a finding.
- Cite each finding by **repository-relative source path + symbol** (function/class/const), not by diff-offset line numbers — the diff you see may not line up with the current file.
```

**Delivery.** **Оба** (кумулятивный дифф и «doc — позже» — свойства любого branch-mode-прогона с documentation-узлом после review, не только `wastech-mdlint`). Добавить те же три пункта в packaged review.md как generic-guardrail.

**Тест.** См. код A3 (интеграция на инкрементальный дифф). Промпт-часть — кампанийная: нет рецидива ложного scope-drift / «doc не обновлён».

**Связь с кодом.** Корневой рычаг — **A3** (F32): `write_current_diff` должен давать инкрементальный дифф задачи ([git_manager.py:1173](../../../../src/wastech_orchestrator/git_manager.py#L1173)). Промпт-оговорка — belt-and-suspenders: полезна и после код-фикса (branch-mode всё равно может дать кумулятив).

### T3 · F33 (LOW-MEDIUM) — «sort every output array» без исключения для упорядоченных последовательностей

**Цель.** Убрать провокацию over-sorting осмысленных последовательностей — единственный blocking-баг всей кампании (p4-05, вердикт `rework`): агент написал `readingOrder.map(relativize).sort(byPath)`, затерев топологический порядок алфавитным (ревью: «silently overwrites the topological order with an alphabetical one»).

**Файлы-рычаги.** target [.worc/flows/implementation/implementation.md:15](/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/implementation.md) (## Hard Invariants) + [review.md:23](/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/review.md) (зеркалит правило в blocking-списке).

**Было (implementation.md, стр. 15):**

```
- **Determinism**: sort every output array before returning or rendering it; never depend on filesystem or map-iteration order.
```

**Стало:**

```
- **Determinism**: sort **path-keyed / set-like** output arrays before returning or rendering (repository-relative POSIX); never depend on filesystem or map-iteration order. Do **not** re-sort arrays that carry a meaningful order — topological, reading, or ranked order: map/filter them element-wise but preserve the sequence.
```

**Было (review.md, стр. 23):**

```
- **Nondeterminism**: unsorted output arrays, or absolute / `\`-separated paths in data and reports (public paths must be repository-relative POSIX).
```

**Стало:**

```
- **Nondeterminism**: unsorted **path-keyed / set-like** output arrays, or absolute / `\`-separated paths in data and reports (public paths must be repository-relative POSIX). Do not treat a preserved topological/reading/ranked order as a nondeterminism finding — re-sorting such an array is itself the bug.
```

**Delivery.** **Только target** — блок «## Hard Invariants» отсутствует в packaged implementation.md (это target-кастомизация). Править синхронно обе target-копии, чтобы review не требовало сортировать упорядоченные последовательности.

**Тест.** Кампанийная проверка: на задачах с упорядоченным выходом (reading/topological order) нет рецидива over-sort и нет обратной ложной review-находки.

**Связь с кодом.** Отдельного кода нет; в код-плане это **F1** (тот же target-дрифт, но там формулировка сжатая — здесь дан точный before/after).

### T4 · doc/F32 (LOW) — documentation.md не проговаривает шаг «flip phase-doc → Done»

**Цель.** Зашить в промпт канонический паттерн завершения фазы, который сейчас выполняется непоследовательно (имплементер p4-02/03 флипал phase-doc сам, в p4-04/05/06 — нет, флипал потом documentation) и который порождает ложный review-сигнал «phase-doc не обновлён» (F32). Паттерн был выведен агентами как урок памяти (`ltm_33764fe6d4f2`/`ltm_9353c1d1ce51`), но застрял в карантине (F29) и до агентов не доходит — надёжнее зашить в промпт, чем полагаться на промоушен памяти.

**Файл-рычаг.** target [.worc/flows/implementation/documentation.md](/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/documentation.md) — 3-й абзац (project-specific, `docs/mdlint_v2/`).

**Было (фрагмент 3-го абзаца):**

```
… and the relevant file under `docs/mdlint_v2/` when the change touches a requirement or advances a phase.
```

**Стало (расширить до явного шага):**

```
… and the relevant file under `docs/mdlint_v2/`. When the change **completes a phase**, update that phase's task file: set **Status → Done**, check its exit-criteria boxes, and add an **Implementation notes** section for the non-obvious decisions. Touch only the phase this task belongs to — do not flip sibling phases' files.
```

**Delivery.** **Только target** (шаг завязан на project-структуру `docs/mdlint_v2/`, phase-файлы, exit-criteria — в packaged documentation.md таких имён быть не должно). Опционально — generic-предложение в packaged: «When a change completes a milestone tracked by a status doc, flip that doc's status and record the non-obvious decisions» — но это уже мягче и не обязательно.

**Тест.** Кампанийная проверка: после задачи, закрывающей фазу, phase-doc = `Status Done` с проставленными exit-criteria и секцией Implementation notes; нет ложного review-«phase-doc не обновлён».

**Связь с кодом.** Нет прямого кодового рычага; пересекается с A3 (промпт-оговорка «не флагай doc-обновления» в T2 закрывает review-сторону, а T4 — implementer/doc-сторону).

### T5 · F34 (LOW) — planning.md ссылается на несуществующие «core primitives»

**Цель.** Убрать латентный misdirect: секция «Reuse the existing core primitives» перечисляет пути, которых нет (opus не обманулся исследованием кода, но более слабый планировщик был бы уведён).

**Файл-рычаг.** target [.worc/flows/implementation/planning.md:25-30](/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/planning.md).

**Было (стр. 27-30):**

```
- remark-based parser — `packages/core/src/markdown/parse.ts`
- graph builder — `packages/core/src/graph/build.ts`
- discovery — `packages/core/src/discovery/`
- isolated token estimator — `packages/core/src/llm/budget.ts`
```

Проверено против репо (2026-07-05): фактические имена — `packages/core/src/markdown/parse-document.ts` и `packages/core/src/graph/build-context-graph.ts`; директории `packages/core/src/llm/` **нет вовсе**; `discovery/` существует (`discovery/globs.ts`). То есть 3 из 4 путей неверны.

**Стало (вариант A — реальные пути):**

```
- remark-based parser — `packages/core/src/markdown/parse-document.ts`
- graph builder — `packages/core/src/graph/build-context-graph.ts`
- discovery — `packages/core/src/discovery/`
```

(убрать `llm/budget.ts` — токен-эстиматора по этому пути нет; если он существует под другим именем, вписать актуальный, иначе не упоминать).

**Стало (вариант B — generic, устойчив к переименованиям, рекомендуется):**

```
Reuse the existing core primitives rather than rewriting them — look under `packages/core/src/{markdown,graph,discovery}` for the current parser, graph builder, and discovery modules, and confirm the exact file names by reading the directory before you cite them in the plan.
```

Вариант B предпочтителен: он не протухает при следующем переименовании (ровно та причина, по которой список сломался), и снимает нагрузку с `upgrade-flows`/preflight-проверки.

**Delivery.** **Только target** (project-пути; packaged planning.md — generic, этих путей не содержит, менять не нужно).

**Тест.** Все пути, упомянутые в `planning.md`, существуют в репо (кандидат в lightweight preflight-проверку из Секции 0).

**Связь с кодом.** Нет; в код-плане это **F2**.

### T6 · F31 (LOW-MEDIUM) — мёртвый `{memory_path}`-блок в review.md — см. решение в коде

**Цель.** Устранить мёртвый блок `{?memory_path}…{/memory_path}` (target стр. 48 **и** packaged стр. 3): evaluator-раннер не прокидывает `memory_path` ([evaluator.py:289-300](../../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L289)), в отличие от agent-раннера ([agent.py:534](../../../../src/wastech_orchestrator/core/flow/nodes/agent.py#L534)) — блок всегда схлопывается в пусто, а reviewer-preference-ранжирование `packet.py` (`_REVIEWER_PREF_NODES={review,fixing}`) инертно.

**Файлы-рычаги.** код [evaluator.py:289](../../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L289) (первично); промпт — target review.md:48 **и** packaged review.md:3.

**Решение — двухвариантное, зависит от код-плана A4:**

- **Если A4 выбирает «прокинуть пакет»** (предпочтительно — ревью полезнее всего кормить recurring reviewer expectations): промпт-блок **оставить как есть** в обоих файлах, он заработает. Промпт-правок ноль.
- **Если A4 выбирает «не кормить review памятью»**: удалить мёртвый блок `{?memory_path}…{/memory_path}` из **обоих** — target review.md:48 и packaged review.md:3 (это дефект дефолта, а не только target-дрифт).

**Delivery.** Определяется кодовым решением A4; при варианте «удалить» — **оба** файла.

**Тест.** См. код A4 (evaluator прокидывает `memory_path`, рендерит непустой блок при непустом пакете). При варианте «удалить» — в review.md не остаётся `{memory_path}`.

**Связь с кодом.** **A4** (F31) — первичен и решает, какой из двух промпт-вариантов применять. Наблюдаемый эффект появляется только после B1/B2 (пока память пуста).

---

## Секция 2 — packaged-дефолты (`src/wastech_orchestrator/packaged/flows/implementation/*.md`)

Правки в самом оркестраторе — доходят до **новых** инсталляций через `install`; до уже посеянных — только через `upgrade-flows` (Секция 0) или `--reconfigure`.

### P1 · F29-prompt (MEDIUM-HIGH) — summary.md не задаёт словарь `evidence.type`

**Цель.** Промпт-паллиатив к F29: финализирующий turn (по `summary.md`) эмитит `memory_delta` с `evidence`, и супервайзер естественно помечает `type:"file"` (32/36 указателей) и `"commit"` (1). Детерминированный `assign_trust` таких токенов не знает → 18/21 репо-обоснованных урока навсегда деградируют до `agent-inferred` и застревают в карантине. Задать в промпте словарь допустимых типов, чтобы модель писала только распознаваемые токены.

**Файл-рычаг.** [packaged/flows/implementation/summary.md](../../../../src/wastech_orchestrator/packaged/flows/implementation/summary.md) (**идентичен** target — дефект дефолта). Кодовые рычаги — [memory/lifecycle.py:24](../../../../src/wastech_orchestrator/memory/lifecycle.py#L24) (`assign_trust`, классы `_REPO`/`_ARTIFACT`) и/или enum-констрейнт `evidence.type` в [memory/delta.py:119](../../../../src/wastech_orchestrator/memory/delta.py#L119).

**Стало (добавить к абзацу про memory/lessons в summary.md, стр. 5):**

```
For each memory `evidence` pointer set `type` to one of the recognized tokens: `repo_doc` / `code` / `config` / `doc` for a repository file, or `check` / `test` / `diff` / `plan` for a task artifact. Do not invent other type tokens (e.g. `file`, `commit`) — an unrecognized type downgrades the lesson to non-durable and it never accumulates.
```

**Delivery.** Править **packaged** (target идентичен → ре-синк target тем же текстом). Но: **код-фикс первичен** — расширить/нормализовать словарь `assign_trust` (`file→repo`, `commit→artifact`) или enum-констрейнить схему delta; тогда неважно, как формулирует модель. Промпт-словарь — вторая линия обороны (модель может писать свободный текст мимо enum, если схема его не принуждает).

**Тест.** См. код B1 (юнит `assign_trust`: `{"type":"file"}`/`{"type":"commit"}` → durable-класс). Промпт-часть: после N задач `evidence.type` в дельтах — только из словаря; `long_term/` накапливает уроки.

**Связь с кодом.** **B1** (F29) — первичен. Также B2 (F30, рекуррентность по `subject`) — второй «замок» на пустой `long_term/`, чисто кодовый ([service.py:562](../../../../src/wastech_orchestrator/memory/service.py#L562)), промпт-компонента не имеет (стабильный canonical subject хрупок).

### P2 · packaged-эхо T2 и T6 (при выборе «удалить»)

- **T2 (F32-guardrail):** три пункта про кумулятивный дифф / «doc — позже» / source-path+symbol добавить и в **packaged review.md** (generic для любого branch-mode-флоу с documentation после review).
- **T6 (F31):** если A4 решает не кормить review памятью — удалить `{?memory_path}`-блок и из **packaged review.md:3**.

---

## Секция 3 — промпт-смежные кодовые рычаги (ссылки, без дублирования)

Эти находки имеют промпт-компонент, но чинятся прежде всего в коде — детальные шаги/тесты в [p4-findings-remediation-plan.md](p4-findings-remediation-plan.md):

| Находка | Код-рычаг | Пункт код-плана | Промпт-компонент здесь |
| --- | --- | --- | --- |
| F24 | `_FINDINGS_SCHEMA` + `additionalProperties:false` | **A1** | — (разблокирует кросс-вендор review для T1) |
| F28 | кросс-вендор review не бежит (следствие F24) | **A2** | T1 |
| F32 | инкрементальный дифф задачи (`write_current_diff`) | **A3** | T2 |
| F31 | прокинуть `memory_path` в evaluator-раннер | **A4** | T6 (решает промпт-вариант) |
| F29 | `assign_trust` словарь / enum `evidence.type` | **B1** | P1 |
| F30 | дедуп-ключ рекуррентности (не сырой `subject`) | **B2** | — |

---

## Сводный порядок и приоритет

Промпт-правки дешёвы и почти все независимы; порядок диктуется зависимостью от кодовых фиксов и разблокировкой:

1. **T1 (F28)** — MEDIUM, только target, тривиальный pull из packaged. Делать сразу (после/вместе с код-A1, иначе кросс-вендор review всё равно не бежит).
2. **T3 (F33)** — LOW-MED, только target, снимает единственный класс blocking-багов кампании. Независим.
3. **T5 (F34)** — LOW, только target, вариант B (generic). Независим.
4. **T4 (doc/F32)** — LOW, только target. Независим; парен с T2 по review-стороне.
5. **T2 / P2 (F32-guardrail)** — оба; вторичен к код-A3, но полезен и без него.
6. **P1 (F29-prompt)** — packaged+target; вторичен к код-B1 (код первичен).
7. **T6 (F31)** — определяется код-A4; выполнять после A4 и B1/B2.

**Системно (не блокирует точечные правки):** пометить T1 и T6 как acceptance-кейсы будущего `upgrade-flows` (Секция 0); завести отдельным кандидатом в [follow_ups.md](../../follow_ups.md) lightweight preflight-проверку существования project-путей, упомянутых в `.worc/flows/*.md` (ловит F34-класс на будущее).

## Явный позитив (не трогать)

Из Части B, зафиксировано как работающее — тюнинга не требует: **fixing** (единственный запуск p4-05 отработал буквально по промпту, повторный review дал `accept`); **planning** верифицирует план против кода (44 tool-call'а в p4-06, `human_input`-контракт корректен); **implementation** соблюдает свои Hard Invariants (кроме единичного over-sort — T3) и держит скоуп; **supervisor** держит advisory-инвариант и всегда пишет структурный summary без вставки служебных полей в текст; **review** (когда бежит на claude-фоллбэке) — предметное, не rubber-stamp, с верной severity-градацией.
