# Качество ролевых промптов по узлам (P5-кампания, 6 задач)

STATUS: read-only, 2026-07-07. Часть B фазового разбора P5. Оценивает, что просил ролевой промпт каждого узла (`rendered-prompt.md` = ролевой файл + Core-скаффолдинг + footer), как вёл себя агент (`events.jsonl`, `evaluations`), характерные удачи и слабости, и точный рычаг. Отличаю **target-локальный дрифт** (`/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/*.md`, gitignored, install-seeded и доработанные) от **packaged-дефолта** ([src/wastech_orchestrator/packaged/flows/implementation/*.md](src/wastech_orchestrator/packaged/flows/implementation)). Синтез фазы — [p5-phase-synthesis.md](p5-phase-synthesis.md); аудит памяти — [p5-memory-subsystem-audit.md](p5-memory-subsystem-audit.md).

**Два режима (учитывать при чтении).** Проходы 15–17: код-узлы (planning/impl/fixing/documentation) на **codex gpt-5.4/xhigh**, review на **claude opus-4-8/high**. Проходы 18–20: код-узлы на **claude sonnet-5/xhigh**, review на **codex gpt-5.4** (p5-04 xhigh, p5-05/06 high). Значит выводы «codex-как-кодер» и «codex-как-ревьюер» приходят из непересекающихся задач.

**Короткий вывод по промптам.** Ролевые промпты target-а **зрелые и хорошо откалиброваны** — оба дрифта p4 закрыты (**F33** «sort every array» теперь исключает осмысленный порядок; **F34** planning-primitives указывают реальные пути), F32-оговорки про кумулятивный/pre-doc дифф присутствуют в review. Единственная промпт-проблема фазы — **review делает отсутствие тест-покрытия блокирующим инвариантом** (target `review.md:26`), что и разгоняет loop (F42). Причём это **target-локальный дрифт**: packaged-review минимален (6 строк) и покрытие блокирующим не объявляет.

## Рычаги: target-копии ≠ packaged-дефолты

Все ролевые файлы target-а существенно **обогащены** относительно packaged-дефолтов (packaged лаконичны и generic; target — с project-инвариантами, Roadmap, гейтами). Значит промпт-правки — почти всегда в **target-копии** (одна репа), а в packaged — только если правка нужна каждому репо.

| Узел | target vs packaged | Характер |
| --- | --- | --- |
| planning | target богаче: `## Roadmap And Architecture` с реальными путями, «verify every path against the tree» | локальный, здоровый (F34 закрыт) |
| implementation | target богаче: `## Hard Invariants` (determinism/POSIX/severities/no-exit/token-estimator/deps) | локальный, здоровый (F33 закрыт) |
| review | target **51 строка vs packaged 6**: добавлены `## Blocking Invariant Violations` (вкл. coverage), `## Test Coverage` | локальный; **амплификатор F42** |
| fixing | target богаче: scope-discipline + «не чинить toolchain-провалы» + quality-gate | локальный, здоровый |
| documentation | target богаче: жёсткий «Stay strictly within documentation» (запрет install/build/network) | локальный, здоровый (hardened) |
| supervisor / summary | target ≈ packaged по духу (advisory-observe + structured-summary) | здоровый |

---

## planning (codex gpt-5.4/xhigh 15–17 · claude sonnet-5/xhigh 18–20, read-only)

**Соответствие поведение↔промпт — высокое.** Промпт (`planning.md`) просит верное: «Produce a full, implementation-ready plan… Return the typed structured result» (стр. 1), «name the hardest or most uncertain part so the implementer starts there» (стр. 6), и раздел `## Explore Before You Plan` — «Verify every path you cite against the current tree» (стр. 16), «If the plan departs from an existing pattern, say so… instead of quietly diverging» (стр. 17). Есть дисциплина `human_input` (стр. 21–22) и precedence «surface the contradiction instead of guessing» (стр. 31).

**Удачи.** codex-planning (15–17) вёл себя ровно по этому контракту: на p5-01 осознанно пронёс замеченное противоречие спеки про `cycles` (а не «угадал») — прямое следствие стр. 31. claude-planning (18–20) так же в рамках; все планы дали в-скоуп диффы без переусложнения. За фазу 0 фоллбэков, 0 повторов, 1 попытка.

**F34 закрыт (позитив).** В p4 (F34) planning ссылался на несуществующие `graph/build.ts`/`markdown/parse.ts`/`llm/budget.ts`. Теперь `planning.md:35-40` перечисляет **реальные** `packages/core/src/markdown/parse-document.ts` (`parseDocument`), `.../graph/build-context-graph.ts` (`buildContextGraph`), `discovery/`, `engine/tokens.ts` — плюс защитная оговорка «confirm each path against the current tree… the module layout evolves». Дрифт исправлен в target-копии.

**Слабостей нет.** Рычаг — только поддержание актуальности путей (защитная оговорка это уже смягчает). Зона — target `.worc/flows/implementation/planning.md`.

## implementation (codex 15–17 · claude sonnet-5/xhigh 18–20, workspace-write)

**Соответствие — высокое.** `implementation.md:1`: «Make the smallest focused change… do not refactor unrelated code, widen scope, or add abstractions the task does not require» — YAGNI прямо в промпте. `## Hard Invariants` (стр. 13–20): determinism, POSIX-пути, только `error`/`warning`, no-exit в library, изолированный token-estimator, никаких deps без approval. `## Comments And Rationale` (why-not-what). `## Verify` с командами гейта.

**Удачи.** Диффы фазы строго в скоупе, без лишних файлов (p5-01 не трогал CLI/config; p5-04 — 9 файлов по плану; проверено пофазово). Кросс-платформенные/детерминизм-инварианты соблюдались (единственный blocking по детерминизму в фазе — G6-honesty p5-04, не over-sort).

**F33 закрыт (позитив).** В p4 (F33) инвариант «sort every output array» был абсолютным и спровоцировал over-sort топологического порядка. Теперь `implementation.md:15`: «sort path-keyed and set-like output arrays… **but do not sort an array whose order is itself meaningful** (topological, reading, ranked/scored)… preserve a computed sequence exactly». Ровно та оговорка, которой не хватало; в P5 over-sort не воспроизвёлся. Зеркалируется и в review (`review.md:25`).

**Слабостей нет.** Зона — target.

## review (claude opus-4-8/high 15–17 · codex gpt-5.4 18–20, read-only) — центр F42

**Соответствие — высокое по форме, но одна калибровочная строка разгоняет loop.** Хорошее в промпте (`review.md`): взвешивание «correctness and invariant violations block; quality and style observations are advisory… do not over-block on nits» (стр. 1); F32-оговорки — «The diff may be cumulative… Judge only the changes that belong to this task's plan» (стр. 19) и «captured before the documentation step… do not block on a phase doc not yet flipped» (стр. 12); cite-by-symbol, не по diff-offset (стр. 8, закрывает line-ref-часть F32); F33-зеркало (стр. 25); memory-бриф (стр. 50, F31-закрыт).

**Проблема (F42) — прямо в промпте.** Раздел `## Blocking Invariant Violations` (стр. 21–30) объявляет блокирующим, среди прочего: **«Missing test coverage for user-visible behavior — no unit test and/or no fixture test»** (стр. 26). Плюс раздел `## Test Coverage` (стр. 42–46): «A unit test per new/changed rule or algorithm, and a focused per-scenario fixture… exercise the edges above, not just the happy path». То есть роль **обязывает** ревьюера возвращать `rework` за каждый пробел покрытия и за каждую непокрытую граничную ветку.

**Поведение (доказательство).** `evaluations.in_flow_verdict` p5-04 (7 rework → accept): итерации 1–4 — корректность (G6-honesty пустого `readingOrder`; all-or-nothing `resolveCompileSettings.safeParse`; per-field leniency `skill`/`sections`; `contentHash` без provenance-строки); **итерации 5–7 — ровно то, что предписывают стр. 26/42–46**: «`Document Architecture` not covered as a load-bearing contract», «routed missing-import does not assert the resolved fallback path», «`hubMinInDegree` accepts `0/-1/1.5`». То есть поздние реворки — промпт, работающий как написано, а не сбой.

**target-дрифт, не packaged.** packaged [review.md](src/wastech_orchestrator/packaged/flows/implementation/review.md) — 6 строк, generic: «blocking/critical/high marks something that must change… medium/low advisory», кумулятивная оговорка, memory — и **покрытие блокирующим не объявляет**. Значит амплификатор F42 внесён в target-копию (`review.md:26,42-46`), а не в дефолт. Рычаг для F42 — прежде всего target-файл.

**Что предлагается (детально в разделе «Калибровка review» ниже).** Перевести «missing test coverage» из Blocking Invariants в advisory (или сузить до основного user-visible поведения, не каждой граничной ветки); reasoning review-узла `high` как дефолт для крупных кодовых узлов; включить закомментированный `max_rework_per_stage: 1` (target `implementation.yaml:99`).

**Режим 15–17 (claude ревьюит codex).** Тот же промпт на claude/high давал `accept` с 1-й попытки (0–1 находок). Это не «claude мягче» — задачи 15–17 были меньше/чище на входе; но контраст показывает, что дотошность loop-а — функция (модель × reasoning × размер узла × coverage-blocking-правило), а не только промпта.

## fixing (claude sonnet-5/xhigh 18–20, workspace-write)

**Соответствие — высокое, слабостей нет.** `fixing.md:1`: «Make the minimal change needed to resolve them»; `## Scope Discipline` — «fix only what your change broke… Do not edit files outside the task's scope» (стр. 5) и явный запрет чинить toolchain-провалы (стр. 6–7). Quality-gate по одному провалу за раз (стр. 20).

**Удачи.** На p5-04/05/06 fixing на каждый цикл делал точечную правку, всегда `succeeded` за 1 попытку, отзывчив на находки codex-ревью (кросс-провайдерная петля работала). Никакого scope-creep в fix-циклах. Зона — target, правок не требует.

## documentation (codex-resume 15–17 · claude 18–20, workspace-write→docs-only)

**Соответствие — высокое; промпт заметно ужесточён (позитив).** `documentation.md:7`: «**Stay strictly within documentation.**… Do not install dependencies… run builds… reach the network, or run throwaway experiments… The docs formatter (`npm run format`) is the one sanctioned command». Это отражает прошлое ужесточение docs-узла (конфайн read-only-к-коду). Просит «why, not what» и обновление phase-doc при завершении фазы (стр. 5).

**Поведение.** Тонкий resume-узел (77–212s). Единственный сбой — p5-01 codex-resume `unsupported_version` (F38) → fallback claude; после фикса F38 documentation на codex без фоллбэка. Промпт слабостей не даёт. Зона — target.

## supervisor / summary (observe + finalize)

**observe** (`supervisor.md`): advisory-only, «never edit… never request rework… do not block» (стр. 3), с явной просьбой ловить «the run repeating the same failure across fix cycles without real progress» и scope-drift (стр. 5). На p5-04 (7 циклов) супервайзер корректно НЕ флагнул «застревание», т.к. loop прогрессировал (каждый раунд — новое) — то есть промпт-эвристика сработала верно.

**finalize** (`summary.md`): просит structured-markdown summary (лид + `##`-секции), явно «Put only human prose in `summary`; return follow-ups, memory, and lessons in their own fields — never paste tags/JSON into the summary» (стр. 5), «Always produce a real summary — never empty» (стр. 7). За фазу `supervisor_final.summary_written=true` во всех 6.

**Наблюдение (не промпт-дефект, а поведение модели).** memory_delta финализатора зависит от провайдера: claude-финализатор (15–17) извлекал ~1 entity/задачу, codex-финализатор (18–20) — 3–5 (рост `memory_write` 5→9). Контракт finalize один на всех (нет пер-провайдерного ветвления, [core/supervisor.py:713](src/wastech_orchestrator/core/supervisor.py#L713)) — значит разница чисто модельная. Промпт-рычаг для снижения шума entity/уроков — `summary.md`/finalize-инструкция (ограничить уроки паттернами, «что делает модуль» отдать entity) — см. memory-аудит F43.

## Калибровка review (F42): почему 7 циклов у p5-04 и что предлагается

**Механика.** Крупный узел `synthesize` (много user-visible поведений: honesty, leniency, provenance, budget, Markdown-safety) × review на `xhigh` × правило «coverage = blocking» (`review.md:26`) → каждый проход честно находит следующий реальный пробел (сначала корректность, затем непокрытые граничные ветки), возвращает `rework`, fixing чинит, следующий проход находит следующий. Ревьюер отдаёт все находки прохода (`review.md:11`), но поскольку правки одного слоя обнажают/оставляют непокрытым следующий, а coverage-пробел блокирует — loop растягивается на 7 проходов. Все чеки при этом зелёные (rework — чисто review-driven).

**Вклад reasoning vs размер — не чистый A/B.** p5-04 (xhigh, крупный synthesize) — 7 циклов, review-проходы 300–800s. p5-05/06 (high, меньше) — 1 цикл, 90–223s. Менялись оба фактора; направление согласовано (ниже reasoning + меньше узел → короче loop), величина не разделена. Честно: наблюдение, не эксперимент.

**Предложения (по убыванию эффекта, все — не срочно):**

1. **Демотировать «missing test coverage» из Blocking в advisory** (target `review.md:26` + `## Test Coverage` 42–46): correctness/инварианты блокируют, полнота покрытия — advisory. Самый прямой рычаг: это и есть амплификатор итераций 5–7. (packaged так и устроен — покрытие не блокирует.)
2. **Сузить coverage-blocking до основного user-visible поведения**, а не «каждой граничной ветки» — если полностью демотировать нежелательно.
3. **reasoning review = `high` по умолчанию для крупных кодовых узлов** (уже применено на p5-05/06; кандидат в дефолт `implementation.yaml`).
4. **Включить потолок `max_rework_per_stage`** — в target `implementation.yaml:99` он присутствует закомментированным (`# max_rework_per_stage: 1`); ограничивает глубину loop детерминированно.
5. **Неблокирующий `testing_quality`-evaluator** для coverage-замечаний, чтобы correctness-review не блокировал на тест-полноте (более крупная перестройка флоу).

Зона — прежде всего target `.worc/flows/implementation/review.md` + `implementation.yaml` (флоу-кноб). Если richer-review желателен как стандарт — отдельное решение по packaged.

## Что уже хорошо (проверено)

- **Оба промпт-дрифта p4 закрыты в target:** F33 (determinism с исключением осмысленного порядка, `implementation.md:15`), F34 (реальные core-primitives, `planning.md:35-40`).
- **F32-оговорки в review присутствуют:** кумулятивный дифф (`review.md:19`), pre-doc (стр. 12), cite-by-symbol-не-offset (стр. 8).
- **F31 закрыт:** memory-бриф инъектируется во все агент/evaluator-узлы, включая review (`review.md:50`).
- **documentation ужесточён** до read-only-к-коду с явным запретом install/build/network.
- **Дисциплина скоупа** (implementation «smallest focused change», fixing «fix only what your change broke») — соблюдалась, диффы фазы в скоупе.
- **supervisor-эвристика «застревание vs прогресс»** сработала верно на 7-цикловом p5-04 (не ложно-флагнула).

## Тюнинг-лист (по приоритету)

| # | Узел | Правка | Рычаг | Дрифт/дефолт | F |
| --- | --- | --- | --- | --- | --- |
| 1 | review | «missing test coverage» → advisory (или сузить до основного поведения) | `.worc/flows/implementation/review.md:26,42-46` | target-дрифт (packaged не блокирует) | F42 |
| 2 | review | reasoning `high` дефолт для крупных узлов; включить `max_rework_per_stage:1` | `.worc/flows/implementation/implementation.yaml:96,99` | target-кноб | F42 |
| 3 | summary/finalize | ограничить `lessons` паттернами; «что делает модуль» — только entity | `summary.md` / [core/supervisor.py:864-875](src/wastech_orchestrator/core/supervisor.py#L864) | packaged/prompt | F43 (память) |
| 4 | review/fixing | инкрементальный (не кумулятивный) дифф для точности находок и пакета | [git_manager.py:1173](src/wastech_orchestrator/git_manager.py#L1173) | orchestrator | F32/F48 |
| — | planning/impl/fixing/doc | правок не требуют (зрелые, дрифты закрыты) | — | — | — |
