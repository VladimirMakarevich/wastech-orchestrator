# Аудит подсистемы памяти после фазы P5 (6 задач на wastech-mdlint)

STATUS: read-only, дата 2026-07-07. Охват: все 6 задач фазы P5 (`p5-01`…`p5-06`, PR [#11](https://github.com/VladimirMakarevich/wastech-mdlint/pull/11), ветка `feat/p5-compile`). Прочитаны целиком все tier-файлы `/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/memory/**` (`short_term/recent.jsonl`, `long_term/failures.jsonl`, `entities/entities.jsonl`, `quarantine/pending.jsonl`, `audit/log.jsonl`, `manifest.json`), сопоставлены с `state.db` (`evaluations` kind=`memory_write`/`supervisor_final`, `node_lineage`), проверена теневая нативная память Claude Code (F37) и рычаги в исходниках оркестратора. Это продолжение [p4-memory-subsystem-audit.md](p4-memory-subsystem-audit.md); новые находки нумеруются с **F43** (последняя в TEST-FINDINGS — F42). Ничего не менялось.

## Короткий вывод

**Память в P5 перешла из состояния «безопасна, но бесполезна» (вердикт p4) в «реально полезна на двух слоях — entity-карточки и промоутнутые failure-уроки — но половина объёма записи не читается никогда и дублирует entity-слой».** Три из четырёх «главных рычагов» p4 закрыты и подтверждены на данных: **F29** (словарь `evidence.type` — `file`/`commit` теперь грунтуют durable-класс), **F31** (review-узел получает пакет), **F36** (эпизодные пути стали repo-relative). `long_term/` впервые **непуст** — 3 промоутнутых failure-урока, и они реально инъектируются в пакеты последующих задач (проверено на `p5-06/memory/review.md`). Это качественный сдвиг: подсистема начала приносить пользу.

Но новый профиль дефектов сместился к **шуму и релевантности**, а не к «пусто/сломано»:

1. **Главный рычаг — F43: ~поло­вина управляемых записей никогда не читается.** 15 P5-уроков `semantic`/`procedural`/`reviewer` получили durable-trust `repo-observed`, но осели в `quarantine/pending.jsonl` («held awaiting recurrence 1/2»), а `PacketBuilder` карантин **не читает вообще** ([packet.py:163-167](src/wastech_orchestrator/memory/packet.py#L163)). При этом они по смыслу дублируют entity-карточки, которые читаются. Это чистый write-only шум.
2. **F48 — пакет теряет релевантность на shared-ветке:** `touched_paths` для ранжирования берётся из `changed_code_paths_since_base()` ([agent.py:598](src/wastech_orchestrator/core/flow/nodes/agent.py#L598), [evaluator.py:328](src/wastech_orchestrator/core/flow/nodes/evaluator.py#L328)), а на общей ветке цепочки это **кумулятивный** дифф всех задач → path-overlap насыщается → пакет показывает алфавитно-первые entity, а не файлы текущей задачи (семейство F32).
3. Точечные дефекты записи: **F44** (дубль entity-карточки на один файл), **F45** (редакция-оверфайр испортила `subject` промоутнутого урока), **F47** (эпизоды бессодержательны), **F46** (косметика audit-rationale).

Целостность — исправна: hash-цепочка audit-лога цела (78 строк), `rationale` в каждой audit-строке осмыслен, редакция-funnel активна, poisoning-гейт держит. Один codex-специфичный факт: рост `memory_write` 4/5/5→9/9/9 объясняется сменой финализатора с claude на codex (F41) — **codex-финализатор извлекает вдвое больше entity/уроков под тем же контрактом** (не код, а поведение модели).

## Что реально записалось после 6 задач

| Тир | Файл | Записей | Читается пакетом? | Состав |
| --- | --- | --- | --- | --- |
| short_term | `short_term/recent.jsonl` | 14 (8×p4 + 6×p5) | да, но пусто по содержанию | по одному эпизоду на задачу; `touched_paths=[]`, `stage_outcomes={}` |
| long_term | `long_term/failures.jsonl` | **3** (все p5) | **да** | `ltm_5bbee` (sync/async сигнатура), `ltm_8c75` (--cwd/config), `ltm_9efc` (тавтологичный тест) — все `repo-observed`, `kind=failure` |
| entities | `entities/entities.jsonl` | 23 (8×p4 + 15×p5) | **да** | module/document/test_file-карточки, все `repo-observed` |
| quarantine | `quarantine/pending.jsonl` | 36 (21×p4 + 15×p5) | **нет** | 18 `agent-inferred` (p4) + 18 «held 1/2» durable (3×p4 `artifact-backed` + 15×p5 `repo-observed`) |
| audit | `audit/log.jsonl` | 78 | — | hash-цепочка цела; actor=`finalizer` во всех |

Разложение 78 audit-действий (по `rationale`, `state.db`-независимая проверка): `appended entity card` ×23, `quarantined: non-durable trust 'agent-inferred'` ×18, `held short-term: awaiting recurrence (1/2 tasks)` ×18, `recorded short-term episode` ×14, `promoted to long-term (repo-observed trust, evidence, gate met)` ×3, `upserted entity card` ×1, `merged into existing failure record` ×1.

---

## 1. Что реально записано (по тирам)

### 1.1 Эпизоды (`short_term/recent.jsonl`) — записаны, но бессодержательны (по-прежнему)

6 P5-эпизодов (`ep_p5-01…ep_p5-06`), по одному на задачу. Каждый несёт только `id`, `task_id`, `created_at`, `trust_level: artifact-backed` и **`artifact_paths: [".worc/logs/<task-id>"]`** — теперь repo-relative (см. §5, F36 закрыт). Всё остальное дефолтно: `stage_outcomes: {}`, `touched_paths: []`, `touched_symbols: []`, `base_commit: null`, `head_commit: null`.

**Рычаг.** Эпизод строится в `_write_memory` ([core/orchestrator.py:2145-2158](src/wastech_orchestrator/core/orchestrator.py#L2145)): `touched_paths`/`touched_symbols` в конструктор **не передаются вообще** → дефолт `()`; `stage_outcomes=outcomes or {}`, а `outcomes` непусто только на failure-пути (`{"task": <status>}`, [orchestrator.py:2123-2125](src/wastech_orchestrator/core/orchestrator.py#L2123)). Пер-нодовые исходы не прокидываются. Рендер даёт голый буллет `- task p5-04-synthesize` ([packet.py:314-326](src/wastech_orchestrator/memory/packet.py#L314)) — то есть эпизод занимает строку в пакете, но не несёт сигнала. **Это буквально та же находка, что и в p4 (там без F-номера); в P5 не тронута.** → **F47**.

### 1.2 Entity-карточки (`entities/entities.jsonl`) — самый ценный слой, фактически корректны

15 P5-карточек (+8 унаследованных p4) — module/document/test_file. Качество высокое и репо-обоснованное: `summary` точны, `symbols`/`paths` заполнены, `risk_notes` содержательны и durable. Примеры:

- `core-synthesize` → «Pure deterministic SKILL.md renderer responsible for provenance hashing, section gating, cycle honesty, budget rendering, and Markdown-safe output», `risk_notes: ["Future output changes should preserve the Markdown-safety helpers and provenance-hash behavior."]`.
- `core-compile-context` → `risk_notes: ["Task doc still advertises a stale sync signature for this async API."]` — прямая, полезная заметка.
- `compile-graph-analysis` → «P5.01 degree-based node classifier … DEFAULT_HUB_MIN_IN_DEGREE=3 … hubMinInDegree is the single tunable option surface for compile».

Все — `trust_level: repo-observed`, `status: active`. **Это тот durable-контекст, ради которого память и нужна.** Два дефекта:

- **Дубль на один файл.** `packages/core/src/compile/compile-context.ts` имеет **две** карточки: `core-compile-context` (создана p5-04) и `compile-context` (создана p5-05) — разные `entity_id`, идентичный `canonical_name`/`paths[0]`, слегка разные `summary`. → **F44**.
- **Пустая провенанс-метадата.** `last_seen_task_ids: []` и `last_validated_commit: null` **во всех 23 карточках** — поле есть, но не заполняется нигде (grep по `src/` даёт только дефолт [records.py:139](src/wastech_orchestrator/memory/records.py#L139)). Нельзя понять, какая задача карточку трогала и когда она в последний раз верифицирована против кода.

### 1.3 Long_term (`long_term/failures.jsonl`) — впервые непуст: 3 промоутнутых урока

Все три — `kind: failure`, `trust_level: repo-observed`, `status: active`, каждый с `evidence` типа `file` на конкретные строки и `scope.paths`:

1. `ltm_5bbeeaf38f00` (p5-04): «When implementation intentionally diverges from a task doc signature, update the frozen signature block…»; evidence → `docs/…/04-synthesize.md:93/:112`, `compile-context.ts:196`. **`subject: "ta[REDACTED].compileContext-sync-vs-async"`** — испорчен редакцией (→ F45).
2. `ltm_8c7578b60ff9` (p5-05): «Print `normalizeRelativePath(path.relative(command.cwd, outputPath))`…» / remedy «Resolve relative `--config` against the command's explicit `--cwd`…»; evidence → `commands.ts:286/:317`, `cli.test.ts:276/:285`.
3. `ltm_9efc032d0d2e` (p5-06): «Compute the expected token count from the documented formula and pin a literal value instead of calling the same helper the implementation uses»; evidence → `compile-context.test.ts:193`, `06-compile-tests.md:36`.

Все три **напрямую соответствуют реальным rework-находкам ревью** (см. синтез, §F42) — то есть память зафиксировала именно те уроки, которые стоили fix-циклов. Это durable и полезно. Дефекты: `rationale: null` в самой записи (при этом audit-строка rationale несёт — см. §5); `statement`==`remedy` дословно в 2 из 3 (косметика, при рендере не дублируется — [packet.py:293](src/wastech_orchestrator/memory/packet.py#L293)).

### 1.4 Карантин (`quarantine/pending.jsonl`) — 36 записей, ни одна не читается

15 P5-уроков здесь — **все `repo-observed`** (durable trust!), `kind` = `semantic`/`procedural`/`reviewer`, evidence `file`/`doc`. Примеры содержания качественные: «There are 8 built-in rule category codes (TBL…LLM) plus custom; there is NO 'CHK' category», «Compile rule descriptions must be read from `ruleRegistry.getAllMetadata()` … never re-authored», «idPattern is inferred only from table-cell tokens … returns undefined when families disagree». То есть карантин копит **корректные, durable-обоснованные уроки** — но они заперты (см. §3, F43).

---

## 2. Кто и когда писал (финализатор, провайдер, счётчики)

Все записи памяти делает whole-task финализатор супервайзера в момент закрытия задачи: в `state.db` `evaluations.supervisor_final` для всех 6 задач = `{"summary_written": true, "memory_delta": true, ...}` (memory_delta писался всегда). Audit-`actor` во всех 78 строках = `finalizer`. Провайдер финализатора (по `node_lineage.__supervisor__`):

| Задача | Проход | supervisor(finalize) провайдер | `memory_write` действий |
| --- | --- | --- | --- |
| p5-01 | 15 | claude | 4 |
| p5-02 | 16 | claude | 5 |
| p5-03 | 17 | **claude** (codex-finalize упал по F41 → fallback) | 5 |
| p5-04 | 18 | **codex** (F41 fixed) | 9 |
| p5-05 | 19 | **codex** | 9 |
| p5-06 | 20 | **codex** | 9 |

**Разрешение расхождения с брифом.** Бриф относил переход supervisor→codex к проходу 17 (p5-03). Но `node_lineage` для p5-03 показывает `__supervisor__ = claude`, а не codex. Это не противоречие: на p5-03 per-step-наблюдения супервайзера шли на codex, но **финальный finalize упал на codex по F41 (`invalid_json_schema`) и ушёл в fallback на claude** — а `node_lineage` фиксирует провайдера завершившей own-lineage сессии (finalize). Значит `memory_delta` на p5-03 записал **claude-fallback**, не codex. Начиная с p5-04 (F41 закрыт) finalize идёт на codex без fallback (`stages/supervisor/run-*/2-claude/` отсутствует — подтверждено в TEST-FINDINGS проходы 18–20), и `memory_delta` пишет именно codex-финализатор. **Ответ на вопрос Части C.2: да, при supervisor-на-codex (проходы 18–20) memory_delta написан codex-финализатором; на проходе 17 — ещё claude-fallback.**

**Почему рост 4/5/5 → 9/9/9.** Скачок совпадает ровно с переходом финализатора claude→codex (p5-04). Раскладка `memory_write` по действиям:

- claude-финализатор (p5-01/02/03): 1 эпизод + 2–3 quarantine + **1 entity** + 0 промоушенов.
- codex-финализатор (p5-04/05/06): 1 эпизод + 2–3 quarantine + **3–5 entities** + 1 промоушен (failure) (+ иногда 1 merge).

То есть codex-финализатор (а) извлекает **в разы больше entity-карточек** (в т.ч. для документов/тест-файлов/таск-доков: `p5_compile_tests_task_doc`, `p5_compile_phase_index`, `compile_context_tests`), и (б) корректно триггерит промоушен failure-урока. В исходниках **нет пер-провайдерного ветвления** finalize/схемы (подтверждено: `_finalize_schema`/`_finalize_prompt` одни на всех, провайдер только резолвится в [core/supervisor.py:713](src/wastech_orchestrator/core/supervisor.py#L713)) — значит разница чисто поведенческая: **под идентичным контрактом codex gpt-5.4 извлекает память агрессивнее claude**. Полезность двойственная: часть лишних entity ценны (тест-файлы), часть — шум (карточки roadmap-доков `p5_compile_phase_index` — это статусные документы, не durable-код-знание).

---

## 3. Качество и польза — F43 (главный рычаг памяти P5)

### F43 · Durable `repo-observed` уроки semantic/procedural/reviewer навсегда осядают в карантине и не читаются пакетом (write-only объём) · **MEDIUM** · уверенность HIGH · зона **orchestrator**

**Доказательство.**

- Маршрутизация записи: durable-урок, у которого не набралась рекуррентность, кладётся в pending с rationale «held short-term: awaiting recurrence (N/min_tasks)» ([memory/service.py:304-306](src/wastech_orchestrator/memory/service.py#L304)). В audit-логе таких — **18** («held short-term: awaiting recurrence (1/2 tasks)»); из них 15 — P5 `repo-observed`.
- Промоушен `failure`-уроков идёт мимо рекуррентности через `explained_failure` ([lifecycle.py:88-111](src/wastech_orchestrator/memory/lifecycle.py#L88); `repo-observed` НЕ в `_AUTO_PROMOTE` — [lifecycle.py:35-37](src/wastech_orchestrator/memory/lifecycle.py#L35)), поэтому `long_term/` наполняют **только** failure-уроки, а semantic/procedural/reviewer ждут рекуррентности ≥2.
- `PacketBuilder` читает `long_term` (`read_long_term` по каждому `LongTermKind`), `entities` (`read_entities`), `episodes` (`read_episodes`) — и **не вызывает `read_quarantine` никогда** ([packet.py:163-167](src/wastech_orchestrator/memory/packet.py#L163); метод `read_quarantine` существует — [service.py:468](src/wastech_orchestrator/memory/service.py#L468) — но не используется на чтении). Значит 36 карантинных записей (в т.ч. 15 durable P5-уроков) **не попадают ни в один пакет ни одной задачи**.
- Дублирование с entity-слоем: карантин-урок `ltm_0f60` «core compileContext API … async … even though the task doc still contains an older sync signature snippet» ≈ entity `core-compile-context.risk_notes` «Task doc still advertises a stale sync signature for this async API»; `ltm_a860` «compile config validation» ≈ entity `compile-config-schema`. То есть уроки в основном повторяют знание, уже лежащее в читаемых entity-карточках.

**Корневая причина.** Только `kind=failure` имеет обходной промоушен (`explained_failure`); `repo-observed` не auto-promote, а рекуррентность ≥2 в пределах фазы почти не набирается (каждый semantic-урок появляется 1 раз). Пакет карантин не читает by-design («precision over recall», [packet.py:1-14](src/wastech_orchestrator/memory/packet.py#L1)). Пересечение двух решений: durable-но-не-failure уроки существуют, но недостижимы для чтения → write-only.

**Влияние.** ~15 P5-записей (и 18 «held» суммарно) — это работа финализатора и место в сторедже без единого чтения. Хуже — это **основной источник «мусора в памяти»**: он растёт линейно по задачам, дублирует entity-слой и никогда не деградирует (cleanup карантин не подчищает — §5).

**Рычаг / идеи (в порядке предпочтения).**

1. **Перестать писать semantic/procedural-уроки, дублирующие entity-карточки** — самый чистый способ убрать шум. Финализатор уже пишет entity с `risk_notes`; отдельный semantic-урок «что делает модуль X» избыточен. Рычаг: промпт финализатора [core/supervisor.py:864-875](src/wastech_orchestrator/core/supervisor.py#L864) — ограничить `lessons` уроками-паттернами (reviewer/procedural «как не наступить»), а «что это» отдать entity.
2. **Дать пакету читать высокотрастовый карантин** (reviewer-kind для review/fixing) — [packet.py:163-167](src/wastech_orchestrator/memory/packet.py#L163) добавить чтение `read_quarantine` с фильтром `trust ∈ durable`. Дёшево, сразу оживляет 15 durable-уроков.
3. **Промоутить `repo-observed` semantic как entity-подобные** (они репо-верифицируемы) — расширить `_AUTO_PROMOTE`/условие в [lifecycle.py:88-111](src/wastech_orchestrator/memory/lifecycle.py#L88). Осторожно: рискует раздуть `long_term` (см. дискуссию в §6).

### Использование на чтении — см. §4; целостность — §5.

---

## 4. Использование на чтении: пакет инъектируется, релевантность страдает (F48)

**Пакет инъектируется во все нужные узлы (F31 закрыт).** В rendered-prompt.md P5-задач memory-бриф присутствует у `planning`, `implementation`, `fixing` и **`review`** (в p4 review пакета не получал — F31). Пример (p5-04): `review/rendered-prompt.md:50` → «A brief of repository memory relevant to this task — recurring reviewer expectations… is at …/memory/review.md». Файлы пакетов существуют: `p5-04-synthesize/memory/{planning,implementation,review,fixing}.md`. `documentation` пакета не получает (тонкий resume-узел, 0 memory-упоминаний) — ожидаемо. Пустой пакет → файла нет (AC-R4): у p5-01/02/03 нет `fixing.md` (не было rework). Всё это — здоровый read-path.

**Долгосрочная память реально доходит до агента (позитив).** `p5-06/memory/review.md` содержит секцию `## Lessons` с двумя промоутнутыми failure-уроками (`--cwd`/config и sync/async), с reviewer-preference-ранжированием ([packet.py:138,202-204](src/wastech_orchestrator/memory/packet.py#L138)). То есть уроки p5-04/p5-05 инъектированы в ревью p5-06 — память начала переносить знание между задачами.

### F48 · На shared-ветке пакет теряет path-релевантность: `changed_code_paths_since_base()` кумулятивен → ранжирование по path-overlap насыщается · **MEDIUM** · уверенность HIGH · зона **orchestrator**

**Доказательство.**

- `touched_paths` для пакета берётся из `self._s.git.changed_code_paths_since_base()` ([agent.py:598](src/wastech_orchestrator/core/flow/nodes/agent.py#L598), [evaluator.py:328](src/wastech_orchestrator/core/flow/nodes/evaluator.py#L328)).
- На общей ветке цепочки `feat/p5-compile` (base=`main`) «изменённое с base» = **кумулятивный** дифф всех предыдущих задач (то же основание, что F32). Наблюдаемо в `p5-04-synthesize/memory/review.md`: пакет ревью, судящего `synthesize.ts`, показывает entity `build-context-graph.ts`, `context-graph-types.ts` (файлы p4!), `doc-profile.ts`, `describe-rules.ts`, `graph-analysis.ts` — **а самого `synthesize.ts` в топ-5 нет**.
- Ранжирование entity: `-path_overlap`, затем `-trust`, затем `entity_id` (алфавит) ([packet.py:147-149](src/wastech_orchestrator/memory/packet.py#L147)). Когда touched кумулятивен, path-overlap≥1 у почти всех карточек, trust у всех `repo-observed`=3 → решает алфавитный tiebreak → «первые по имени», а не «файлы задачи».

**Корневая причина.** База диффа для `changed_code_paths_since_base` — `main`, а не старт текущей задачи; на неслитой chain-ветке это накопленный дифф. Сложный path-overlap-ранкер ([packet.py:223-239](src/wastech_orchestrator/memory/packet.py#L223)) де-факто не работает как задумано.

**Влияние.** Пакет систематически недорелевантен: ревью synthesize видит карточки чужих модулей. Сегодня смягчено тем, что entity-summary полезны и вне точной релевантности, а long_term-уроки узкие; но при росте entity-слоя нерелевантный топ-N будет вытеснять нужные карточки. **Рычаг** — дать пакету инкрементальные пути задачи (диапазон коммитов задачи / diff `<task-start>..worktree`), тот же лечебный рычаг, что для F32 ([git_manager.py:1173](src/wastech_orchestrator/git_manager.py#L1173) + точки вызова [agent.py:598](src/wastech_orchestrator/core/flow/nodes/agent.py#L598)/[evaluator.py:328](src/wastech_orchestrator/core/flow/nodes/evaluator.py#L328)).

**Влияло ли на поведение?** По `events.jsonl` P5-агенты memory-бриф читают (путь фигурирует в контексте), но прямых следов «применил урок X» немного — уроки/карточки узкие и часто про соседние модули. Наиболее вероятный полезный эффект: reviewer p5-06 был заранее сориентирован на «recurring reviewer expectations» (2 failure-урока в пакете). Чистого causal-эффекта из артефактов не выделить — отмечаю честно как слабый-положительный.

---

## 5. Целостность и безопасность

**Audit hash-цепочка — цела.** Программная проверка сцепки `prev_hash → row_hash` по всем 78 строкам: разрывов нет (`chain_intact=True`). `pre_hash`/`post_hash`/`row_hash` присутствуют, `prev_hash` строки 0 = пустой, `pre_hash` строки 0 = sha256("") — корректный genesis. Actor во всех = `finalizer`.

**`rationale` — заполнен на уровне audit-лога, но null в самой записи.** В `audit/log.jsonl` rationale осмыслен и детерминирован (`_reason`, [service.py:579-583](src/wastech_orchestrator/memory/service.py#L579)): «promoted to long-term (repo-observed trust, evidence, gate met)», «held short-term: awaiting recurrence (1/2 tasks)», «appended entity card (repo-observed trust)». А вот в `long_term/failures.jsonl` поле записи `rationale: null` во всех трёх уроках — модельный `rationale` кандидата не персистится в запись (детерминированная причина живёт только в audit). Не дефект целостности, но лёгкая несогласованность наблюдаемости (`worc memory show` по записи rationale не покажет).

**Редакция секретов активна, но оверфайрит (F45).** См. ниже. Значений секретов в tier-файлах не обнаружено (redaction-funnel работает — [service.py:555-557](src/wastech_orchestrator/memory/service.py#L555), единственный чокпоинт). Но редакция сработала **ложноположительно** на безобидном `subject`.

### F45 · Редакция-оверфайр испортила `subject` промоутнутого long_term-урока (`ta[REDACTED].compileContext-sync-vs-async`) · **LOW–MEDIUM** · уверенность HIGH · зона **orchestrator**

**Доказательство.** `long_term/failures.jsonl` урок `ltm_5bbeeaf38f00`: `subject: "ta[REDACTED].compileContext-sync-vs-async"` — литерал `[REDACTED]` вкраплён в середину осмысленного subject.

**Корневая причина.** Набор секретов харвестится в рантайме: `_memory_extra_secrets` = env-значения sensitive-именованных переменных (≥8 симв.) + токены из `.env`/`secrets/**` (целые строки, не-разделительные раны, значения после `=`, ≥8 симв.) ([orchestrator.py:2081-2093](src/wastech_orchestrator/core/orchestrator.py#L2081); [providers/redaction.py:109-124,211-235](src/wastech_orchestrator/providers/redaction.py#L109)). Применение — безграничный подстрочный `.replace` **без границ слова**, литералы ≥4 символа ([redaction.py:130-134](src/wastech_orchestrator/providers/redaction.py#L130)). `subject` не является sensitive-ключом → проходит value-scrubbing, и любой харвест-литерал, случайно оказавшийся подстрокой subject, вырезается. Это тот же класс невоспроизводимой редакции, что F36 (там — префиксы путей).

**Влияние — выше, чем у F36.** (1) Испорчена **durable** запись (long_term, не эфемерный эпизод). (2) `subject` в fallback-ветке служит **ключом дедупа/рекуррентности** (`_derive_id` → `normalize_subject(subject)` при пустом `scope.paths` — [service.py:569-576](src/wastech_orchestrator/memory/service.py#L569)); испорченный/невоспроизводимый subject ломает будущее совпадение того же урока (подрывает фикс F30 для path-less-уроков). (3) Редакция невоспроизводима: тот же урок в другом прогоне (другой харвест-набор) даст другой subject. **Рычаг** — [redaction.py:130-134](src/wastech_orchestrator/providers/redaction.py#L130) (границы слова / минимальная длина литерала выше 4) и/или сузить харвест [orchestrator.py:2081-2093](src/wastech_orchestrator/core/orchestrator.py#L2081); не редактировать структурные ключевые поля (`subject`) подстрочным replace.

### Теневая нативная память Claude Code (F37) — держится за фазу, но не изолирована жёстко

**Новых P5-карточек нет.** В `~/.claude/projects/-Users-a1234-Documents-GitHub-wastech-mdlint/memory/` — 4 файла, новейший `p4-06-grp-coverage-idref.md` (2026-07-05 00:19); `find -newermt 2026-07-06` не находит ничего. P5 шёл 2026-07-07 → **за всю фазу ни одной записи в теневую память**. Это ключевой регресс-чек: **F37-фикс держит по результату.**

**Нюанс.** В `events.jsonl` claude-узлов P5 путь `~/.claude/projects/.../memory/` всё ещё фигурирует (по 1 упоминанию на узел, включая read-only planning), но **без единого tool_use `Read`/`Write`/`Edit`/`Bash` по этому пути**. То есть нативная память по-прежнему **анонсируется** спаунящемуся агенту (директория в контексте), просто ни один агент за P5 не записал. Значит защита сейчас скорее «инцидентная» (агент не выбрал писать), чем жёстко enforced (изолированный `CLAUDE_CONFIG_DIR`). Рекомендую подтвердить, что фикс структурный: рычаг тот же — [providers/claude.py](src/wastech_orchestrator/providers/claude.py) (изолированный конфиг / конфайн `Write`/`Edit` рабочим деревом). Пока — не регресс, но и не доказанная изоляция.

---

## 6. Вердикт по памяти за фазу P5 + рычаги

**Польза памяти за фазу P5: положительная и впервые ненулевая — но с большим балластом.** Что реально ценно:

- **Entity-карточки (23)** — durable, репо-обоснованные module/doc/test-карты с точными summary и полезными risk_notes; читаются пакетом; это рабочая лошадка полезности.
- **3 промоутнутых failure-урока** — узкие, evidence-gated, привязаны к реальным rework-находкам; **доказанно инъектируются** в ревью/фиксинг последующих задач (`p5-06/memory/review.md`). Именно то, ради чего долгосрочная память задумана.
- **Целостность/безопасность** — audit-цепочка цела, rationale осмыслен, редакция активна, poisoning-гейт держит, теневая память за фазу не писала.

Что — шум/дефект (кандидаты «убрать/уменьшить мусор»):

- **Слой semantic/procedural-уроков в карантине (15 P5)** — durable по trust, но не читается никогда и дублирует entity-слой (**F43**). Это главный источник мусора; его надо либо не писать (дедуп с entity), либо начать читать.
- **Эпизоды (14)** — бессодержательны, занимают строки пакета впустую (**F47**).
- **Дубль entity-карточки** на `compile-context.ts` (**F44**); **пустой `last_seen_task_ids`** во всех карточках (провенанс-слепота).
- **Пакет нерелевантен на shared-ветке** — кумулятивный `touched_paths` (**F48**).
- **Редакция-оверфайр** портит durable `subject` (**F45**); **audit merge-rationale** вводит в заблуждение (**F46**, косметика).

**Рычаги (file:line), по убыванию влияния:**

1. **F43** — [core/supervisor.py:864-875](src/wastech_orchestrator/core/supervisor.py#L864) (не писать entity-дублирующие semantic-уроки) и/или [packet.py:163-167](src/wastech_orchestrator/memory/packet.py#L163) (читать durable-карантин) и/или [lifecycle.py:88-111](src/wastech_orchestrator/memory/lifecycle.py#L88) (промоушен repo-observed).
2. **F48** — [git_manager.py:1173](src/wastech_orchestrator/git_manager.py#L1173) + [agent.py:598](src/wastech_orchestrator/core/flow/nodes/agent.py#L598)/[evaluator.py:328](src/wastech_orchestrator/core/flow/nodes/evaluator.py#L328): инкрементальный дифф задачи, не `<base>..worktree`.
3. **F44** — [memory/service.py:316-319,336](src/wastech_orchestrator/memory/service.py#L316): ключевать entity по `canonical_name`/`paths[0]`, а не по LLM `entity_id`; заодно заполнять `last_seen_task_ids`.
4. **F45** — [providers/redaction.py:130-134](src/wastech_orchestrator/providers/redaction.py#L130) (границы слова, min-len) + [orchestrator.py:2081-2093](src/wastech_orchestrator/core/orchestrator.py#L2081) (сузить харвест).
5. **F47** — [core/orchestrator.py:2145-2158](src/wastech_orchestrator/core/orchestrator.py#L2145): прокидывать `touched_paths`/`stage_outcomes` в эпизод (иначе эпизод-слой можно просто не рендерить).
6. **F46** — [memory/service.py:279-281](src/wastech_orchestrator/memory/service.py#L279): rationale «same subject» неверен (дедуп по `kind+scope.paths`, F30) и `affected_ids=_ids(all rows)` шумит.
7. **F37** — [providers/claude.py](src/wastech_orchestrator/providers/claude.py): подтвердить структурную изоляцию нативной памяти.

**Идея по снижению мусора (сводно).** Память слоится верно (эпизод/entity/урок/карантин), но write-path переусердствует на semantic-слое. Минимальный, максимально-эффектный шаг: (а) **дедуп semantic-урок ↔ entity risk_note** на записи (не плодить два представления одного факта), (б) **инкрементальный дифф** для релевантности пакета, (в) **не рендерить пустые эпизоды**. Это убирает основной шум, ничего ценного не теряя, и не требует новых тиров.

## Сравнение с p4-аудитом (что изменилось)

| Находка p4 | Статус в P5 | Доказательство |
| --- | --- | --- |
| **F29** (evidence `file`/`commit` не распознаются → всё `agent-inferred`, long_term пуст) | **ЗАКРЫТА** | [lifecycle.py:31-32](src/wastech_orchestrator/memory/lifecycle.py#L31) `file∈_REPO`, `commit∈_ARTIFACT` (явный F29-коммент); P5-уроки теперь `repo-observed`; long_term непуст |
| **F30** (рекуррентность по дословному subject) | **ЗАКРЫТА для path-bearing** | `_derive_id` ключует `kind+normalize(scope.paths)` ([service.py:569-576](src/wastech_orchestrator/memory/service.py#L569)), `_merge_long_term` объединяет `seen_task_ids` ([service.py:636](src/wastech_orchestrator/memory/service.py#L636)). Остаток: path-less уроки всё ещё по subject; финализатор иногда кладёт путь в `subject`, а `scope.paths` оставляет `[]` (напр. `ltm_e864`) |
| **F31** (review не получает пакет) | **ЗАКРЫТА** | [evaluator.py:328-331](src/wastech_orchestrator/core/flow/nodes/evaluator.py#L328) строит пакет; `review/rendered-prompt.md:50` содержит memory-бриф; `p5-06/memory/review.md` с `## Lessons` |
| **F36** (абсолютные host-пути + невоспроизводимая редакция) | **ПОЛОВИНА закрыта** | Пути эпизодов теперь `.worc/logs/<task>` ([artifacts.py:64-77](src/wastech_orchestrator/providers/artifacts.py#L64)). Но редакция-оверфайр не устранён и **регрессировал на `subject` durable-урока** → F45 |
| **F37** (теневая нативная память) | **ДЕРЖИТСЯ (за фазу)** | 0 новых карточек в `~/.claude/.../memory/` за P5; нативная память ещё анонсируется агенту, но не записывалась (см. §5) |
| long_term был ПУСТ (p4) | **непуст (3)** | промоушен через `explained_failure` для `kind=failure`, не через рекуррентность |

**Итог сравнения.** p4-вердикт «механика цела, ценность ≈ 0, а рядом работает неуправляемая теневая память» в P5 сменился на «ценность реальна на entity+failure-слоях, теневая память замолчала, но write-path производит нечитаемый semantic-балласт». Из 5 memory-находок p4 закрыты/держатся четыре (F29/F31/F37 + половина F36); F30 закрыта для основного случая; F36-редакция дала новый сиблинг F45. Новый профиль работы — про шум и релевантность (F43/F48/F44/F47), а не про «пусто/сломано».
