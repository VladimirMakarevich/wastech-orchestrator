# P5 — план исправления открытых находок кампании (проходы 15–20)

Единый план работ по всем **OPEN**-находкам фазы P5 (6 задач `p5-01`…`p5-06` на `wastech-mdlint`, PR [#11](https://github.com/VladimirMakarevich/wastech-mdlint/pull/11)). Сводит воедино три источника: [синтез фазы](../analysis/p5-phase-synthesis.md) (F42/F49/F50 + вторичные), [качество промптов по узлам](../analysis/p5-prompt-quality-per-node.md) (калибровка review), [аудит памяти](../analysis/p5-memory-subsystem-audit.md) (F43–F48) и открытые пункты из [follow_ups.md](follow_ups.md) по этим прогонам. Первоисточник каждой находки — [TEST-FINDINGS.md](../../TEST-FINDINGS.md).

**Контекст: фаза здорова, блокеров нет.** 6/6 задач `done` с 1-й попытки, чеки всегда зелёные, все codex-primary баги закрыты в бою (F38 VERIFIED, F39 per-step закрыт вариантом B, F41 VERIFIED, F24 не воспроизводится — в план **не входят**). Это план **тюнинга и харденинга**, а не аварийного ремонта: приоритеты — стоимость/латентность (F42/F50), наблюдаемость (F49), полезность памяти (F43/F48) и защита от граничных конфигураций (F39-preflight/F40). Две находки — P5-резидуалы уже частично закрытых p4-находок: **F48** — сиблинг F32 (инкрементальный дифф), **F45** — сиблинг F36 (недетерминированная редакция).

Формат пункта: **Цель · Рычаг (file:line) · Шаги · Тест · Зависимость/порядок**. Приоритет = «серьёзность + разблокировка других находок + доля стоимости фазы». Зоны: **orchestrator** (пакетный код — задевает каждый репо) и **target** (`.worc/flows/` `wastech-mdlint`, gitignored, install-seeded).

## Рекомендованный сквозной порядок

1. **A1 (F42)** — единственный крупный рычаг фазы: одна задача `p5-04` дала 7 rework-циклов и в одиночку — ~57% wall-времени и ~64% выходных токенов всей фазы. Дешёвая правка target-промпта + кноба, максимальный ROI. Делать первым.
2. **B1 (F43) → B2 (F48)** — снять основной write-only-шум памяти (15 нечитаемых durable-уроков) и вернуть пакету path-релевантность на shared-ветке. P1.
3. **A2 (F49)** — дешёвый audit-фикс наблюдаемости (нужна оговорка cumulative vs consecutive, см. ниже).
4. **A3 (F50)** — стоимость persistent-супервайзера; напрямую спадает при укрощении loop-а (A1), поэтому после A1.
5. **C1 (F39-preflight) → C2 (error_class-триаж)** — робастность codex-primary для будущих конфигов.
6. **D1 (F40)** — UX-гейт противоречивой конфигурации цепочек.
7. **B3 → B4 → B5 → B6 (F44 → F45 → F47 → F46)** — гигиена памяти.
8. **E1 (F37-остаток)** — подтвердить структурную изоляцию нативной памяти Claude Code.

---

## Секция A — Калибровка блокирующего ревью и стоимость петли

### A1 · F42 (LOW–MEDIUM · OPEN) — блокирующее codex-ревью чрезмерно дотошно на больших узлах: 7 rework-циклов, дрейф в тест-полировку

**Цель.** Снизить глубину/стоимость блокирующего review-loop-а на крупных кодовых узлах, не теряя корректностный сигнал. Наблюдение подтверждено на трёх задачах: `p5-04` (review=`xhigh`) — 7 циклов; `p5-05`/`p5-06` (review=`high`) — 1 цикл. Итерации 1–4 на `p5-04` — реальные корректностные HIGH (G6-honesty пустого `readingOrder`; all-or-nothing `resolveCompileSettings.safeParse`; per-field leniency; `contentHash` без provenance); итерации 5–7 дрейфовали в полноту тест-покрытия и валидацию границ. Loop прогрессировал и сошёлся в бюджете — это вопрос дефолтной дотошности, не баг.

**Рычаг.** Прежде всего **target-дрифт** (не packaged): [.worc/flows/implementation/review.md:26,42-46](/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/review.md) — раздел `## Blocking Invariant Violations` объявляет «Missing test coverage for user-visible behavior» блокирующим + раздел `## Test Coverage`. Packaged [review.md](../../src/wastech_orchestrator/packaged/flows/implementation/review.md) — 6 строк, покрытие блокирующим НЕ объявляет. Кнобы: target [.worc/flows/implementation/implementation.yaml:96,99](/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/implementation.yaml) (reasoning review-узла; закомментированный `# max_rework_per_stage: 1`); схема кноба — [core/flow/schema.py](../../src/wastech_orchestrator/core/flow/schema.py) (`max_rework_per_stage`).

**Шаги** (по убыванию эффекта; выбрать 1–2, не всё сразу):

1. **Демотировать «missing test coverage» из Blocking в advisory** в target `review.md:26` + `## Test Coverage` 42–46: корректность/инварианты блокируют, полнота покрытия — advisory. Самый прямой рычаг (это амплификатор итераций 5–7; packaged так и устроен). Альтернатива, если полностью демотировать нежелательно: **сузить coverage-blocking** до основного user-visible поведения, а не «каждой граничной ветки».
2. **Батчить находки одного прохода**: попросить ревьюера возвращать ВСЕ находки прохода в одном вердикте и явно разделять blocking-корректность vs advisory-полнота (снижает число раундов).
3. **reasoning review = `high` по умолчанию** для крупных кодовых узлов (уже применено на `p5-05/06`; кандидат в дефолт `implementation.yaml`; reasoning работает как регулятор глубины).
4. **Включить `max_rework_per_stage`** на review-узле как детерминированный потолок глубины loop-а (в target раскомментировать `max_rework_per_stage: 1`).
5. (Крупнее) отдельный **неблокирующий `testing_quality`-evaluator** для coverage-замечаний, чтобы correctness-review не блокировал на тест-полноте.

**Решение перед реализацией.** Правки 1–4 — почти все в target-копии (одна репа). Если richer-review желателен как стандарт для всех репо — отдельное решение вынести в packaged `review.md` (иначе packaged остаётся минимальным).

**Тест.** Не чистый A/B (у `p5-04` менялись и reasoning, и размер узла) — принять как наблюдение. Проверка эффекта: на следующей крупной задаче с review на `high` + демотированным coverage-blocking loop сходится за ≤2 цикла без потери корректностных находок.

**Зависимость.** Первый во всей кампании. Разблокирует наблюдаемое снижение стоимости; A3 (F50) спадает автоматически при укрощении loop-а.

---

### A2 · F49 (LOW · OPEN) — `tasks.review_fix_cycles` = 0 при 7 фактических review-реворках

**Цель.** Дать audit-трейлу отличать review-реворки от test-реворков (важно для калибровки F42 и будущей аналитики). Сейчас `state.db tasks.review_fix_cycles = 0` во всех 6 задачах, включая `p5-04`, где `fix_iterations=7` корректен.

**Рычаг (с оговоркой — фикс не тот, что в исходной follow-up-строке).** Персист-точка УЖЕ существует: [core/orchestrator.py:1818-1832](../../src/wastech_orchestrator/core/orchestrator.py#L1818) `_sync_counters_from_run_state` зеркалит `run_state.counter("review_fix")` в `p.counters.review_fix_cycles`. Причина нуля глубже: [core/flow/engine.py:418-430](../../src/wastech_orchestrator/core/flow/engine.py#L418) `_reset_loops_at` **сбрасывает** именованный consecutive-cycle счётчик, когда back-edge удовлетворён — т.е. когда review наконец возвращает `accept`, `review_fix` обнуляется ДО терминального sync. По контракту [core/loop_control.py:12](../../src/wastech_orchestrator/core/loop_control.py#L12) `review_fix_cycles` = «длина _текущей последовательной_ fix-петли», а сошедшаяся петля законно = 0. Значит это не «счётчик не персистится», а **семантический мисматч**: поле хранит consecutive-длину, а для атрибуции нужен кумулятивный total (как `fix_iterations`).

**Шаги.**

1. Ввести кумулятивный per-loop-kind счётчик (например `review_fix_total` в [core/loop_control.py](../../src/wastech_orchestrator/core/loop_control.py) `LoopCounters`), инкрементируемый на каждом review-driven rework-edge рядом с `record_rework` ([engine.py:404-413](../../src/wastech_orchestrator/core/flow/engine.py#L404)) и НЕ обнуляемый `_reset_loops_at`; либо сохранять пик consecutive-петли перед сбросом.
2. Прокинуть его в терминальный sync и на `tasks`-строку (там же, где `fix_iterations`). Решить: хранить оба (consecutive для live-прогресса + total для audit) или переопределить `review_fix_cycles` на кумулятивный (последнее меняет смысл существующего поля — предпочтителен отдельный total, чтобы не ломать live-surfaces `worc status`).

**Тест.** Юнит на engine/loop_control: сценарий N review-реворков → accept даёт `review_fix_total=N` (не 0) на `tasks`-строке; `test_fix` не задевается. Регресс: `fix_iterations` остаётся корректным.

**Зависимость.** Независим; ценен для аналитики F42. Мелкий audit-пробел.

---

### A3 · F50 (LOW–MEDIUM · OPEN) — стоимость persistent-супервайзера супер-линейна по глубине fix-loop

**Цель.** Убрать доминирование супервайзера в токенах на больших задачах с глубоким loop-ом. На `p5-04` supervisor out=**693k** при 26 наблюдениях (7 loop-ов × шаги, каждый шаг — codex-resume с полным own-lineage контекстом); для сравнения `p5-05`=105k / `p5-06`=95k (8 шагов). Supervisor-стоимость растёт с числом node-run-ов, а те — с глубиной loop-а: длинное ревью удорожает не только review+fixing, но и supervisor.

**Рычаг.** Цикл наблюдения супервайзера в [core/supervisor.py](../../src/wastech_orchestrator/core/supervisor.py) (per-step observe через `resume_own_lineage`). Финализатор-промпт — [core/supervisor.py:868](../../src/wastech_orchestrator/core/supervisor.py#L868).

**Шаги** (идеи; supervisor остаётся advisory-слоем — контракт не менять):

1. **Не пере-наблюдать неизменившиеся шаги**: наблюдать только delta (новые/изменившиеся node-run-ы), а не каждый повтор шага в петле.
2. И/или **наблюдать на пониженном reasoning** — per-step observe не требует xhigh.
3. Оба совместимы с F42: укрощение loop-а (A1) автоматически снижает и supervisor-стоимость — сначала A1, затем измерить остаток.

**Тест.** Наблюдение по `result.json.usage`: на задаче с глубоким loop-ом supervisor out растёт заметно медленнее числа node-run-ов после delta-observe.

**Зависимость.** После A1 (значительная часть эффекта — производная от глубины loop-а).

---

## Секция B — Управляемая память: шум и релевантность

Профиль дефектов P5 сместился с «пусто/сломано» (p4) на **шум и релевантность**. Ценность реальна на двух слоях (entity-карточки + промоутнутые failure-уроки, доказанно инъектируются в последующие задачи), но половина объёма записи не читается никогда.

### B1 · F43 (MEDIUM · OPEN) — durable `repo-observed` уроки semantic/procedural/reviewer навсегда осядают в карантине и не читаются пакетом

**Цель.** Убрать основной источник write-only-шума: 15 P5-уроков получили durable-trust `repo-observed`, но осели в `quarantine/pending.jsonl` («held awaiting recurrence 1/2»), а `PacketBuilder` карантин не читает вообще — при этом они дублируют entity-карточки, которые читаются. Растёт линейно по задачам, никогда не деградирует.

**Рычаг.** Маршрут «held»: [memory/service.py:305](../../src/wastech_orchestrator/memory/service.py#L305). Промоушен только `kind=failure` через `explained_failure`: [memory/lifecycle.py:88-109](../../src/wastech_orchestrator/memory/lifecycle.py#L88) (`repo-observed` НЕ в `_AUTO_PROMOTE` — [lifecycle.py:35](../../src/wastech_orchestrator/memory/lifecycle.py#L35)). Пакет читает только long_term/entities/episodes — [memory/packet.py:144-166](../../src/wastech_orchestrator/memory/packet.py#L144); `read_quarantine` существует ([service.py:468](../../src/wastech_orchestrator/memory/service.py#L468)), но не вызывается на чтении. Финализатор-промпт — [core/supervisor.py:868](../../src/wastech_orchestrator/core/supervisor.py#L868).

**Шаги** (в порядке предпочтения; выбрать 1, возможно + 2):

1. **Перестать писать semantic/procedural-уроки, дублирующие entity-карточки** (самый чистый способ убрать шум). Финализатор уже пишет entity с `risk_notes`; отдельный semantic-урок «что делает модуль X» избыточен. Рычаг — финализатор-промпт `supervisor.py:868`: ограничить `lessons` уроками-паттернами (reviewer/procedural «как не наступить»), а «что это» отдать entity.
2. **Дать пакету читать высокотрастовый карантин** (reviewer-kind для review/fixing): `packet.py:144-166` добавить `read_quarantine` с фильтром `trust ∈ durable`. Дёшево, сразу оживляет 15 durable-уроков.
3. (Осторожно) **промоутить `repo-observed` semantic как entity-подобные**: расширить `_AUTO_PROMOTE`/условие в `lifecycle.py:88-109`. Риск раздуть `long_term` — не первый выбор.

**Тест.** Юнит: durable-урок, дублирующий entity risk_note, не пишется отдельной записью (вариант 1); либо пакет отдаёт durable-карантинный reviewer-урок для review-узла (вариант 2). Регресс: `agent-inferred` в пакет по-прежнему не попадает.

**Зависимость.** Главный memory-рычаг фазы; независим, P1.

---

### B2 · F48 (MEDIUM · OPEN) — пакет теряет path-релевантность на shared-ветке (кумулятивный `touched_paths`)

**Цель.** Вернуть ранжированию пакета релевантность к файлам ТЕКУЩЕЙ задачи. На общей ветке цепочки path-overlap насыщается → пакет показывает алфавитно-первые entity (`p5-04` review synthesize видел карточки `build-context-graph.ts`, `doc-profile.ts` и др., а самого `synthesize.ts` в топ-5 нет). Семейство F32.

**Рычаг.** `touched_paths` для пакета берётся из `changed_code_paths_since_base()` — [core/flow/nodes/agent.py:598](../../src/wastech_orchestrator/core/flow/nodes/agent.py#L598) и [core/flow/nodes/evaluator.py:328](../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L328); реализация [git_manager.py:717](../../src/wastech_orchestrator/git_manager.py#L717). **Важно:** F32-фикс (2026-07-05) уже перевёл `write_current_diff`/`diff_stat`/`cumulative_committed_diff` на per-task chain-базу `_diff_base`, но `changed_code_paths_since_base()` (питающий memory-пакет) всё ещё диффит от `base_branch` — это незакрытый хвост того же фикса.

**Шаги.**

1. Перевести `changed_code_paths_since_base()` (или его вызовы в пакет-раннерах) на ту же per-task базу `_diff_base`, что уже используется `write_current_diff` — диапазон коммитов/файлы именно этой задачи, а не `<base>..worktree`.
2. Убедиться, что `checks`-узел ([nodes/checks.py:85](../../src/wastech_orchestrator/core/flow/nodes/checks.py#L85)), тоже вызывающий этот метод для command-set selection, не регрессирует (для selection кумулятивность как раз может быть желательна — возможно нужен отдельный per-task вариант метода, а не смена семантики существующего).

**Тест.** Юнит/интеграция: две задачи на одной ветке — `touched_paths` пакета второй содержит только её файлы; ранжирование entity ставит карточку изменённого задачей файла в топ.

**Зависимость.** Независим; парн с A1 по духу (тот же chain-контекст). P1.

---

### B3 · F44 (LOW · OPEN) — дубль entity-карточки на один файл + пустой `last_seen_task_ids`

**Цель.** Не плодить две карточки на один путь (`compile-context.ts` получил `core-compile-context` от `p5-04` и `compile-context` от `p5-05` — разные `entity_id`, идентичный `paths[0]`); заполнять провенанс-метадату.

**Рычаг.** Upsert entity по LLM-`entity_id`: [memory/service.py:317-336](../../src/wastech_orchestrator/memory/service.py#L317) (`_index_by(entities, "entity_id", ...)`). `last_seen_task_ids: []` во всех 23 карточках (поле в [records.py](../../src/wastech_orchestrator/memory/records.py), нигде не заполняется).

**Шаги.**

1. Ключевать entity по `canonical_name`/`paths[0]`, а не по LLM-`entity_id` (модель даёт разные id одному файлу).
2. Заодно заполнять `last_seen_task_ids` (и по возможности `last_validated_commit`) при upsert — сейчас нельзя понять, какая задача карточку трогала.

**Тест.** Юнит: две карточки на один `paths[0]` от разных задач сливаются в одну; `last_seen_task_ids` накапливает оба task_id.

**Зависимость.** Гигиена; после B1/B2.

---

### B4 · F45 (LOW–MEDIUM · OPEN) — редакция-оверфайр испортила `subject` промоутнутого long_term-урока

**Цель.** Не портить durable-записи ложной редакцией: `ltm_5bbeeaf38f00` имеет `subject: "ta[REDACTED].compileContext-sync-vs-async"` — литерал `[REDACTED]` вкраплён в осмысленный subject. Сиблинг F36 (там пути эпизодов уже relativized, а редакция-оверфайр — нет).

**Рычаг.** Безграничный подстрочный `.replace` без границ слова, литералы ≥4 симв.: [providers/redaction.py:127-134](../../src/wastech_orchestrator/providers/redaction.py#L127) (`_MIN_LITERAL_LEN`). Харвест секрет-литералов из преходящего состояния процесса: [core/orchestrator.py:2081](../../src/wastech_orchestrator/core/orchestrator.py#L2081) (`_memory_extra_secrets`).

**Шаги.**

1. `redaction.py:127-134` — редактировать по границам слова и/или поднять минимальную длину литерала выше 4; не вырезать короткие подстроки из середины токенов.
2. Не применять подстрочный `.replace` к структурным ключевым полям (`subject`) — `subject` служит fallback-ключом дедупа/рекуррентности (`_derive_id` → `normalize_subject`), испорченный subject подрывает фикс F30 для path-less уроков.
3. Опц. сузить харвест в `_memory_extra_secrets` (не тащить безобидные короткие раны из `.env`).

**Тест.** Юнит: осмысленный `subject`, случайно содержащий короткую подстроку харвест-набора, не редактируется; реальный секрет-литерал по-прежнему вырезается; редакция идентичных данных детерминирована.

**Зависимость.** Гигиена/безопасность-наблюдаемость; независим.

---

### B5 · F47 (LOW · OPEN) — эпизоды бессодержательны (занимают строку пакета впустую)

**Цель.** Либо наполнить эпизод сигналом, либо не рендерить пустой. Сейчас каждый эпизод несёт только id/task/trust/artifact-dir; `stage_outcomes={}`, `touched_paths=[]`, `touched_symbols=[]` → рендер даёт голый буллет `- task p5-04-synthesize`.

**Рычаг.** Построение эпизода без прокидывания touched/outcomes: [core/orchestrator.py:2145-2158](../../src/wastech_orchestrator/core/orchestrator.py#L2145). Рендер эпизода — [memory/packet.py](../../src/wastech_orchestrator/memory/packet.py) (буллет по эпизоду).

**Шаги.**

1. Прокинуть `touched_paths`/`stage_outcomes` (пер-нодовые исходы) в конструктор эпизода в `_write_memory`.
2. Альтернатива (если наполнять нечем): не рендерить эпизод-слой при пустом содержании (не тратить строку пакета).

**Тест.** Юнит: эпизод строится с непустыми `touched_paths`/`stage_outcomes`; пустой эпизод не рендерится в пакет.

**Зависимость.** Гигиена; независим.

---

### B6 · F46 (LOW · OPEN, косметика) — вводящий в заблуждение audit merge-rationale

**Цель.** Убрать неверную формулировку в audit-логе: rationale «merged … (same subject)» при том, что дедуп теперь по `kind+scope.paths` (F30), плюс `affected_ids` шумит (перечисляет все строки).

**Рычаг.** [memory/service.py:281](../../src/wastech_orchestrator/memory/service.py#L281) (rationale «same subject»; `affected_ids`).

**Шаги.** Привести rationale к фактическому ключу дедупа (`kind+scope.paths`); сузить `affected_ids` до реально затронутой записи.

**Тест.** Юнит: merge пишет rationale с верным ключом и точечным `affected_ids`.

**Зависимость.** Косметика; последней в секции.

---

## Секция C — Codex-primary / робастность провайдера

### C1 · F39-preflight (OPEN под-пункт) — preflight не ловит унаследованный `supervisor.provider`-мисматч

**Цель.** Закрыть остаточный пробел F39: код-фикс (`SupervisorConfig.provider` + валидация model↔provider) есть, но preflight прошёл `ready`, не поймав случай «`supervisor.provider` не задан → наследуется `primary=codex`, а `supervisor.model` — claude-специфичный» (на `p5-02` это дало 400 на каждом supervisor-шаге, замаскировано claude-fallback). Валидация срабатывает только на ЯВНО заданный provider.

**Рычаг.** [config/schema.py](../../src/wastech_orchestrator/config/schema.py) (`SupervisorConfig`) + точка резолвинга/валидации supervisor-провайдера в preflight.

**Шаги.**

1. Расширить supervisor-preflight на inherited-путь: когда `supervisor.provider` не задан, валидировать `supervisor.model` против вендора резолвнутого глобального primary. Fatal, если нет безопасного fallback; warn иначе (следуя принципу: fatal только когда нет runtime-fallback).

**Тест.** Юнит preflight: codex-primary + `supervisor.model: claude-*` без `supervisor.provider` → диагностика мисматча (не тихий `ready`).

**Зависимость.** Независим; защита для будущих codex-primary конфигов (в текущем target обойдено явным `supervisor.provider: codex`).

---

### C2 · error_class-триаж (OPEN candidate (a)) — codex exit-2 argparse классифицируется как `unsupported_version` (маскирует bad-argv)

**Цель.** Не маскировать корневую причину: сигнатура stderr адаптера мапит argparse/exit-2 отказ нашего же argv в `unsupported_version` (скрыла F38 — bad-argv — за version-подобной ошибкой); тот же класс: 400-ответ модели/схемы писался `process_crashed` (F39/F41). Затрудняет триаж.

**Рычаг.** [providers/codex.py](../../src/wastech_orchestrator/providers/codex.py) `_CODEX_SIGNATURES` (regex `unknown option`/`unrecognized option`/`unexpected argument`).

**Шаги.**

1. Ввести отдельный класс `configuration_error`/`bad_argv` для exit-2 argparse-отказов, отличный от настоящего version-гейта.
2. (Опц.) отделить model/schema-400 (`invalid_request_error`) от общего `process_crashed` для точного триажа codex-узлов.

**Тест.** Юнит: exit-2 `unexpected argument` → `bad_argv` (не `unsupported_version`); 400 `invalid_json_schema` → отдельный класс (не `process_crashed`).

**Зависимость.** Независим; латентная ловушка для следующего grammar-дрифта (сейчас moot для resume-пути после F38).

---

## Секция D — Цепочки задач (UX-гейт)

### D1 · F40 (MEDIUM · OBSERVED / обойдено) — предупреждать, когда `depends_on` указывает на задачу, делящую собственный `branch_ref`

**Цель.** Ловить противоречивую конфигурацию цепочки до отказа в середине фазы. Вся P5 заблокировалась на шаге 2, т.к. каждая задача совмещала `depends_on: [предшественник]` (merge-гейт — «PR is OPEN (unmerged)») с `branch_mode: existing` + `branch_ref: feat/p5-compile` (shared-branch, PR открыт до конца фазы). Это взаимоисключающие механизмы; p4-кампания использовала shared-branch БЕЗ `depends_on`.

**Рычаг.** [core/flow/validator.py](../../src/wastech_orchestrator/core/flow/validator.py) / задачный validation-gate; `depends_on`-гейт в [core/orchestrator.py](../../src/wastech_orchestrator/core/orchestrator.py).

**Шаги.**

1. Первично (target task-authoring, уже сделано для p5-02..06): не совмещать `depends_on` с same-ref `branch_mode: existing`.
2. Orchestrator-UX: при валидации задачи предупреждать (или отклонять), когда ветка цели `depends_on` совпадает с собственным `branch_ref` задачи — ловит противоречие до прогона.

**Тест.** Юнит: задача с `depends_on`-целью на своём `branch_ref` → warn/reject на валидации.

**Зависимость.** Независим; UX-защита.

---

## Секция E — Изоляция нативной памяти Claude Code (верификация)

### E1 · F37-остаток (fixed 2026-07-05, изоляция не доказана) — подтвердить структурную изоляцию нативной памяти

**Цель.** Доказать, что защита структурна, а не «инцидентна». За всю P5 — 0 новых карточек в `~/.claude/projects/<target>/memory/` (фикс `--disallowedTools` на config-dir держит по результату), НО путь всё ещё анонсируется спаунящемуся агенту (по 1 упоминанию на узел в `events.jsonl`, без tool_use по нему). Значит агент пока просто не выбрал писать — не жёсткий enforcement.

**Рычаг.** [providers/claude.py](../../src/wastech_orchestrator/providers/claude.py) (изолированный `CLAUDE_CONFIG_DIR`/settings — auth-safe вариант, либо конфайн `Write`/`Edit` рабочим деревом).

**Шаги.**

1. Живой smoke (owner): спаун claude-узла с промптом, провоцирующим запись в нативную память → подтвердить отказ (агент не читает и не пишет `~/.claude/.../memory/`). Пересекается с существующими live-verify строками follow_ups (F37/F21-allowlist).
2. Если live-smoke покажет, что запись всё ещё возможна — довести до структурной изоляции (изолированный config-dir), кросс-платформенно (не завязываться на `~/.claude` буквально).

**Тест.** Интеграция/фикстура: спаун claude не пишет вне рабочего дерева; live-smoke подтверждает enforcement.

**Зависимость.** Верификация, не новый код (пока smoke не покажет пробел); независим.

---

## Сводная таблица

| # | Находка | Серьёзность | Зона | Рычаг | Приоритет |
| --- | --- | --- | --- | --- | --- |
| A1 | F42 — блокирующее ревью чрезмерно дотошно (7 циклов, 64% токенов фазы) | LOW–MED | target (+ опц. packaged) | `review.md:26,42-46`; `implementation.yaml:96,99`; `max_rework_per_stage` | **P1** |
| A2 | F49 — `review_fix_cycles`=0 (consecutive vs cumulative) | LOW | orchestrator | `orchestrator.py:1818`; `engine.py:418`; `loop_control.py` | P1 |
| A3 | F50 — supervisor супер-линеен по глубине loop (out=693k) | LOW–MED | orchestrator | `core/supervisor.py` observe-цикл | P2 |
| B1 | F43 — durable уроки заперты в карантине, не читаются | MEDIUM | orchestrator | `supervisor.py:868`; `packet.py:144-166`; `lifecycle.py:88-109` | **P1** |
| B2 | F48 — пакет теряет path-релевантность на shared-ветке | MEDIUM | orchestrator | `agent.py:598`/`evaluator.py:328`; `git_manager.py:717` (`_diff_base`) | **P1** |
| B3 | F44 — дубль entity-карточки + пустой `last_seen_task_ids` | LOW | orchestrator | `service.py:317-336`; `records.py` | P2 |
| B4 | F45 — редакция-оверфайр портит `subject` durable-урока | LOW–MED | orchestrator | `redaction.py:127-134`; `orchestrator.py:2081` | P2 |
| B5 | F47 — эпизоды бессодержательны | LOW | orchestrator | `orchestrator.py:2145-2158`; `packet.py` | P2 |
| B6 | F46 — audit merge-rationale вводит в заблуждение | LOW | orchestrator | `service.py:281` | P2 |
| C1 | F39-preflight — унаследованный supervisor мисматч не ловится | MED | orchestrator | `config/schema.py` `SupervisorConfig` + preflight | P2 |
| C2 | error_class-триаж — argparse→`unsupported_version`, 400→`process_crashed` | LOW | orchestrator | `providers/codex.py` `_CODEX_SIGNATURES` | P2 |
| D1 | F40 — `depends_on` × own `branch_ref` противоречие | MED | orchestrator (+ target) | `core/flow/validator.py`; `orchestrator.py` depends_on-гейт | P2 |
| E1 | F37-остаток — структурная изоляция нативной памяти не доказана | HIGH (класс) | orchestrator | `providers/claude.py`; live-smoke (owner) | P2 (verify) |

**Не входят в план (закрыты в бою фазой P5):** F38 (resume-argv, VERIFIED Проход 16), F39 per-step (вариант B, Проход 17), F41 (finalize strict-схемы, VERIFIED Проход 18), F24 (codex-evaluator strict — не воспроизводится с Прохода 18). См. [TEST-FINDINGS.md](../../TEST-FINDINGS.md) проходы 15–20.
