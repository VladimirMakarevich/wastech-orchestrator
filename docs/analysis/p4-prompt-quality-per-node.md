# Качество ролевых промптов по узлам (P4-кампания, 8 задач)

Часть B сквозного разбора. Для каждого типа узла собраны ВСЕ реально отрендеренные промпты через все P4-задачи (`stages/<node>/*/request.json` → `prompt`, сверено с `prompt-audit/timeline.jsonl`), и поведение агента (`events.jsonl`, `structured_output`, `final_message`) сопоставлено с тем, что промпт просит. Каждая находка: **доказательство (цитата из реального прогона) → рычаг (file:line) → рекомендация (конкретная правка формулировки)**.

## Важное про рычаги: target-копии ≠ packaged-дефолты

Активные промпты, которые реально рендерились, — это **editable-копии в target** (`/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/*.md`), а НЕ пакетные дефолты. Они существенно кастомизированы под `wastech-mdlint` (project-context, TS-стиль, инварианты, blocking-списки) — это **by-design** (install сеет редактируемые копии для пер-репо-тюнинга, см. [[install-seeds-flows-and-prompts]]), и большинство кастомизаций **полезны**. Поэтому для каждой находки отдельно указано: это **локальный дрифт** target-копии или **дефект общего packaged-дефолта**.

Сверка target ↔ packaged (`diff`):

| Узел | target vs packaged | Характер |
| --- | --- | --- |
| refinement | +project-context (2 стр.) | локальная кастомизация |
| planning | +38 стр. (What To Produce, Roadmap, **primitives-список**, Testing) | локальная |
| implementation | +53 стр. (Rules Of Record, TS Style, **Hard Invariants**, Tests, Verify) | локальная |
| review | +46 стр., но **отстаёт от F19-фикса packaged** (см. ниже) | локальный дрифт назад |
| fixing | +29 стр. (Scope Discipline, Quality Gate) | локальная |
| documentation | +4 стр. (project docs, why-not-what) | локальная |
| supervisor | +project-context (1 стр.) | локальная |
| summary (finalize) | **идентичен packaged** | дефолт |

Итог: 5 из 8 находок Части B — локальный дрифт target-копий (правятся в `.worc/flows/`), 1 — дефект packaged-дефолта (`summary.md`, идентичен), 2 — прежде всего кодовые рычаги с промпт-компонентом.

---

## planning (claude / opus-4-8 / high, read-only)

**Соответствие поведения промпту — высокое.** Промпт просит: «The files you expect to touch … and the order … The risks, the cross-cutting invariants … and the tests». Планы во всех 8 задачах это дают, read-only соблюдён (`--allowedTools Read,Glob,Grep`), планы верифицированы против кода («Key facts verified against the code»; p4-06 planning прошёл 44 tool-call'а и явно проверил отсутствие import-цикла до рекомендации рефактора). `human_input`-контракт присутствует и корректен.

### F34 (LOW) — planning-промпт ссылается на несуществующие «core primitives»

**Доказательство.** Секция «Reuse the existing core primitives rather than rewriting them» в target `planning.md` перечисляет: `remark-based parser — packages/core/src/markdown/parse.ts`, `graph builder — packages/core/src/graph/build.ts`, `isolated token estimator — packages/core/src/llm/budget.ts`. Проверка репозитория (обе задачи, где это критично — p4-04/p4-07): **3 из 4 путей не существуют** — фактически `parse-document.ts`, `build-context-graph.ts`, а директории `llm/` нет вовсе; корректен только `discovery/`. opus не обманулся (нашёл верные модули исследованием + пакет памяти нёс правильный `build-context-graph.ts`), но более слабый планировщик был бы уведён.

**Рычаг.** target [.worc/flows/implementation/planning.md](/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/planning.md) — секция «Roadmap And Architecture» / список primitives. **Это локальный дрифт**: packaged `planning.md` — generic, этих project-путей не содержит; их вписали в target-копию и они устарели относительно фактического v2-монорепо.

**Рекомендация.** Заменить пути на реальные (`packages/core/src/markdown/parse-document.ts`, `packages/core/src/graph/build-context-graph.ts`), убрать несуществующий `llm/budget.ts` (или заменить на актуальный token-эстиматор, если он есть), либо сделать список generic («ищи существующие примитивы в `packages/core/src/{markdown,graph,discovery}` — не переписывай»). Это единственная промпт-текстовая причина возможного расхождения в planning; F21 (plan-mode → `AskUserQuestion` мимо `human_input`) — провайдерный флаг ([providers/claude.py:74](../../src/wastech_orchestrator/providers/claude.py#L74)), не текст промпта, и уже RESOLVED.

---

## implementation (claude / sonnet-5 / xhigh, workspace-write)

**Соответствие — высокое.** «Make the smallest focused change … do not refactor unrelated code, widen scope, or add abstractions» соблюдено во всех 8: имплементер держал скоуп, гонял `npm run typecheck/test/build/lint`, сознательно НЕ трогал ~137-файловый prettier-baseline-drift (проверял `git stash`-ом, что он пре-существующий). Инварианты «## Hard Invariants» (детерминизм-сортировка, POSIX-пути, только error/warning, no `process.exit` в library) реально соблюдаются в коде — ревью это подтверждает. То есть controls этого промпта в основном **работают**.

### F33 (LOW-MEDIUM) — инвариант «sort every output array» без исключения для упорядоченных последовательностей

**Доказательство.** target `implementation.md` (## Hard Invariants): «**Determinism**: sort every output array before returning or rendering it; never depend on filesystem or map-iteration order». Это абсолютное правило без оговорки. Единственный `blocking`-баг всей кампании (p4-05, review verdict=`rework`) — ровно его переприменение: агент написал `readingOrder: impactResult.readingOrder.map(relativize).sort(byPath)`, а `readingOrder` — топологический порядок, который сортировать НЕЛЬЗЯ. Цитата ревью: «`relativizeImpact` re-sorts `readingOrder` alphabetically with `.sort(byPath)` … silently overwrites the topological order with an alphabetical one». Фикс даже добавил объясняющий комментарий, что этот массив сортировать не надо. То же зеркалит `review.md:23` («Nondeterminism: unsorted output arrays … blocking») — тоже без исключения.

**Рычаг.** target [.worc/flows/implementation/implementation.md:15](/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/implementation.md) + target [.worc/flows/implementation/review.md:23](/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/review.md). **Локальный дрифт** (блок Hard Invariants — target-кастомизация; packaged `implementation.md` его не содержит).

**Рекомендация.** Развести два случая: «Sort **path-keyed / set-like** output arrays for determinism (repo-relative POSIX). Do **not** re-sort arrays that carry a meaningful order — topological/reading order, ranked results: map/filter them element-wise but preserve sequence.» Аналогичную оговорку добавить в `review.md` blocking-список, чтобы ревью не требовало сортировать упорядоченные последовательности.

_(Также: allowlist implementation-узла `--allowedTools …,Edit,Write,Bash` не конфайнит `Write`/`Edit` рабочим деревом, из-за чего p4-06 записал в `~/.claude/…` — это F37, полностью разобран в Части C.)_

---

## review (declared codex / gpt-5.4 / xhigh → факт claude / opus-4-8 / high, read-only)

Контекст: codex-review крашится 9/9 (F24), фактически ревьюит claude-фоллбэк (F28). Промпт `codex-request.json` рендерится полностью, но codex падает на JSON-схеме ДО генерации — так что оцениваем текст промпта + поведение claude-фоллбэка, который его реально исполнил.

**Что работает.** Когда ревью бежит (claude-fallback), оно НЕ rubber-stamp: во всех 8 задачах — конкретные, привязанные к файлам находки с верной severity-градацией (blocking только p4-05), корректно НЕ блокирует на low/medium, не выдумывает блокеры «чтобы что-то найти». Секции «Blocking Invariant Violations» и «Code Quality» реально используются (ревью ссылается на YAGNI, детерминизм, покрытие).

### F32-prompt (MEDIUM) — промпт ревью не защищён от неполного/зашумлённого `{diff_path}`

**Доказательство.** Корневая причина — кодовая (`{diff_path}` = кумулятивный `git diff <base>`, разобрано в Части A/F32), но у промпта есть свой вклад: он не оговаривает, что дифф может быть кумулятивным (chain) или pre-documentation. Следствия живьём: p4-06 review выдал ЛОЖНУЮ находку «index.ts newly exports the full P4.02–P4.05 surface … broader than the P4.06 plan step» — реальная дельта коммита 2 строки; и повторяющийся ложный «phase-doc не обновлён» (documentation обновляет его ПОСЛЕ ревью). Плюс line-refs находок не резолвятся (`coverage.ts:529-539` при файле в 97 строк).

**Рычаг.** Кодовый — [git_manager.py:1173](../../src/wastech_orchestrator/git_manager.py#L1173) (давать инкрементальный дифф задачи). Промпт-компонент — target [.worc/flows/implementation/review.md:13-17](/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/review.md) (секции Requirements/Correctness).

**Рекомендация.** В `review.md`: «The diff may include files committed by earlier tasks on a shared branch — judge only changes relevant to **this task's plan**; do not flag prior-task code as scope drift. Documentation is updated by a later step — do **not** flag missing doc/phase-file updates. Cite findings by **source-file path + symbol**, not diff-offset line numbers.» Это снимает два наблюдавшихся класса ложных находок и делает line-refs пригодными для fixing-агента.

### F28/F24-prompt (MEDIUM) — «say so in one line» противоречит обязательной findings-схеме; target отстаёт от packaged-фикса

**Доказательство.** target `review.md:11`: «If nothing blocks, say so in one line». Но evaluator принуждает `output_schema=_FINDINGS_SCHEMA` и fail-**closed** при отсутствии `{"findings": …}` ([evaluator.py:134-142](../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L134)). То есть «сказать одной строкой прозой» вместо `findings:[]` формально ведёт к `manual`. claude-фоллбэк не попался (схема принудительна), но текст промпта расходится с контрактом. Packaged `review.md` уже исправлен под F19 («No findings means the diff is clean — return an empty `findings` array, **not prose**» + называет поля `path/what/fix`) — **target-копия отстала** (осталась в прозаическом стиле, до-F19).

**Рычаг.** target [.worc/flows/implementation/review.md:1-11](/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/review.md) — **локальный дрифт назад** относительно packaged-фикса.

**Рекомендация.** Ре-синхронизировать «## Output» target-копии с packaged: явно «чисто → верни пустой массив `findings`, не прозу», назвать поля схемы (`severity`/`path`/`what`/`fix`). Кодовый рычаг F24 (добавить `additionalProperties:false` в `_FINDINGS_SCHEMA`) — отдельно, в Части A.

### F31 (LOW-MEDIUM) — мёртвый блок `{memory_path}` в review.md

**Доказательство.** target `review.md:48` содержит `{?memory_path}…{/memory_path}`, но evaluator-раннер не прокидывает `memory_path` ([evaluator.py:289-300](../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L289) — нет ключа), в отличие от agent-раннера ([agent.py:534](../../src/wastech_orchestrator/core/flow/nodes/agent.py#L534)). Блок всегда схлопывается в пусто. Разобрано в Части C.

**Рекомендация.** Либо прокинуть пакет в evaluator-раннере (и тогда `_REVIEWER_PREF_NODES`-ранжирование `packet.py` заработает для review — reviewer-kind уроки), либо убрать мёртвый блок из `review.md`.

---

## fixing (claude / sonnet-5 / xhigh, workspace-write)

**Отработал чисто — находок нет (позитив).** Единственный запуск за кампанию (p4-05): промпт «Address … blocking review findings … Make the minimal change … Stay strictly within scope» исполнен буквально — точечный фикс `readingOrder` + объясняющий комментарий, повторный review дал `accept`. Секции «## Scope Discipline» и «## Quality Gate» (target-кастомизация) соблюдены. `{memory_path}` в fixing.md реально фидится (`memory/fixing.md` p4-05 существует). Отдельного тюнинга не требует.

---

## documentation (claude / sonnet-5 / medium, workspace-write, `--resume` impl-сессии)

**Соответствие высокое:** «Document the why, not the what» соблюдено (Implementation-notes реально про rationale, не пересказ кода); «stay strictly within documentation» — правил только доки + гонял `npm run format`. Финально все 7 phase-docs = `Status Done`, index.md = все галочки — обновлял именно documentation-узел.

### documentation-prompt (LOW) — шаг «flip phase-doc → Done + exit-criteria + Implementation notes» не проговорён явно

**Доказательство.** Промпт `documentation.md` про phase-doc говорит расплывчато: «the relevant file under `docs/mdlint_v2/` when the change touches a requirement or advances a phase». Из-за этого шаг выполняется непоследовательно: имплементер p4-02/p4-03 сам флипал phase-doc, а в p4-04/p4-05/p4-06 — нет (флипал потом documentation). Именно эта непоследовательность порождает повторяющийся ложный review-сигнал «phase-doc не обновлён» (F32). Канонический паттерн («flip Status→Done, check exit criteria, add Implementation notes, prettier») был выведен агентами как урок памяти (`ltm_33764fe6d4f2`/`ltm_9353c1d1ce51`) — но он квартинирован и до агентов не доходит (F29, Часть C).

**Рычаг.** target [.worc/flows/implementation/documentation.md:5](/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/documentation.md) (локальная кастомизация).

**Рекомендация.** Проговорить явно: «When the change completes a phase, update that phase's task file under `docs/mdlint_v2/`: set **Status → Done**, check its exit-criteria boxes, and add an **Implementation notes** section for non-obvious decisions. Do not touch sibling phases' files.» Это ровно тот шаблон, который сейчас выводится как (застревающий) урок памяти — стоит зашить в промпт, а не полагаться на промоушен памяти.

---

## supervisor / summary (claude / opus-4-8 / high, read-only, advisory)

**Соответствие высокое:** супервайзер наблюдал КАЖДЫЙ завершённый шаг во всех 8 задачах (advisory, никогда не блокировал), корректно атрибутировал subtask-контекст, всегда писал финальный summary структурной прозой (## секции, без вставки `follow_ups`/`memory_delta` в текст — как просит `summary.md`). Advisory-инвариант соблюдён.

### F29-prompt (MEDIUM-HIGH) — finalize-промпт не задаёт словарь `evidence.type` для delta

**Доказательство.** Финализирующий turn (по `summary.md`) эмитит `memory_delta` с `evidence`, и супервайзер естественно помечает доказательства как `type:"file"` (32 из 36 указателей) и `"commit"` (1). Но детерминированный `assign_trust` таких токенов не распознаёт (`_REPO={repo,repo_doc,code,config,doc}`, `_ARTIFACT={artifact,check,diff,test,plan}`) → 18 из 21 уроков схлопываются в `agent-inferred` и навсегда в карантине (полный разбор — Часть C/F29). Промпт `summary.md` про memory/lessons не задаёт словарь допустимых типов доказательств.

**Рычаг.** **Дефект дефолта, не локальный дрифт:** `summary.md` идентичен в target и packaged. Промпт-рычаг — [packaged/flows/implementation/summary.md](../../src/wastech_orchestrator/packaged/flows/implementation/summary.md) (+ его target-копия). Кодовые рычаги — [memory/lifecycle.py:24-28](../../src/wastech_orchestrator/memory/lifecycle.py#L24) (распознать `file→repo`, `commit→artifact`) и/или enum-ограничение `evidence.type` в [memory/delta.py:119](../../src/wastech_orchestrator/memory/delta.py#L119).

**Рекомендация.** Надёжнее чинить в коде (расширить/нормализовать словарь `assign_trust` или enum-констрейнить схему delta — тогда неважно, как формулирует модель). Дополнительно в `summary.md`: «For each memory evidence pointer, set `type` to one of: `repo_doc`/`code`/`config`/`doc` (a repository file), `check`/`test`/`diff` (a task artifact), `review`/`fixing` (a review outcome). Do not invent other type tokens — an unrecognized type downgrades the lesson to non-durable.» Это лучший из промпт-only паллиативов, но код-фикс первичен.

_(F30 — рекуррентность по дословному `subject` — прежде всего кодовый рычаг ([service.py:562](../../src/wastech_orchestrator/memory/service.py#L562)); промпт мог бы просить стабильный canonical subject, но это хрупко — разобрано в Части C.)_

---

## Тюнинг-лист (сводно, по приоритету)

| # | Узел | Правка | Рычаг | Дрифт/дефолт | F |
| --- | --- | --- | --- | --- | --- |
| 1 | review | «чисто → пустой `findings`, не проза»; назвать поля схемы (ре-синк с packaged) | `.worc/flows/implementation/review.md:1-11` | локальный дрифт назад | F28/F24 |
| 2 | review | «дифф может быть кумулятивным/pre-doc — суди по плану задачи; не флагай doc-обновления; цитируй source-path+symbol» | `.worc/flows/implementation/review.md:13-17` (+ git_manager.py:1173) | локальный | F32 |
| 3 | supervisor/summary | задать словарь `evidence.type` (первично — код `lifecycle.py`/`delta.py`) | `summary.md` (packaged=target) + `memory/lifecycle.py:24` | **дефолт** | F29 |
| 4 | implementation+review | оговорка к «sort every output array»: не сортировать упорядоченные последовательности | `implementation.md:15` + `review.md:23` | локальный | F33 |
| 5 | documentation | явный шаг «flip phase-doc → Done + exit-criteria + Implementation notes» | `documentation.md:5` | локальный | doc/F32 |
| 6 | planning | заменить несуществующие primitive-пути на реальные/generic | `planning.md` (Roadmap секция) | локальный | F34 |
| 7 | review | прокинуть пакет памяти или убрать мёртвый `{memory_path}`-блок | `evaluator.py:289` / `review.md:48` | код + локальный | F31 |

Позитив, зафиксированный явно: **fixing** отработал чисто (тюнинга не требует); **implementation** соблюдает свои Hard Invariants (кроме единичного переприменения sort — F33); **planning** верифицирует план по коду; **supervisor** advisory-инвариант держит и всегда пишет summary; **review** (когда бежит) — предметное, не rubber-stamp. Проблемы сосредоточены в review (вход + схема + мёртвый memory-блок) и в цепочке «finalize → память» (словарь evidence), а не в качестве большинства промптов.
