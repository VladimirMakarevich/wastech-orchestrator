# Аудит подсистемы памяти после P4-кампании (8 задач на `wastech-mdlint`)

Часть C сквозного разбора фазы P4. Read-only аудит того, как подсистема памяти отработала **на практике** после 8 реальных задач (`p4-01-context-graph-model-v2` + `p4-02..p4-08`), в сопоставлении с тем, как она **должна** работать по дизайну (`docs/backlog/archive/done/orchestrator-memory.md`, `docs/backlog/memory/`). Источник данных — четыре непустых `jsonl` в `/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/memory/` + код подсистемы в `src/wastech_orchestrator/memory/`.

## Короткий вывод

Механика **управляемой** памяти (`.worc/memory/`) исправна и безопасна: write-funnel редактирует секреты, append-only аудит с hash-цепочкой цел, у каждой мутации есть детерминированный `rationale` (F9 — закрыт), poisoning-защита не пробита. Но у неё две беды. Первая: **накопительная ценность близка к нулю** — и это НЕ «by-design V1 не промоутит» (промоушен в `apply_delta` есть и работает), его блокируют два конкретных дефекта write-path. Вторая, более серьёзная: **параллельно управляемой памяти работает ВТОРАЯ, неуправляемая — нативная память Claude Code**, в которую спаунящиеся агенты читают и пишут прямо в домашней папке оператора (`~/.claude/projects/<target>/memory/`), вне изоляции, редакции и аудита. По иронии именно эта теневая память реально накопила корректные уроки, которые управляемая подсистема застряла квартинировать.

Итог по управляемой памяти после 8 задач: `long_term/` **пуст**; 8 эпизодов + 8 entity-карточек (все корректные и репо-подтверждённые) + **21 записанный урок, все в карантине навсегда**. Пакеты, которые узлы реально читают, содержат только entity-summary и бессодержательный список эпизодов — ровно тот слой (уроки/подводные камни), ради которого затевалась память, до агента не доходит.

Четыре главных рычага (все — orchestrator):

1. **F37 (HIGH) — теневая нативная память Claude Code вне изоляции.** Спаунящиеся агенты во ВСЕХ 8 задачах загружают нативную память проекта из `~/.claude/projects/-Users-a1234-…-wastech-mdlint/memory/MEMORY.md`, а имплементер p4-06 туда **записал** новую карточку + правку индекса (`Read`→`Write`→`Edit`, подтверждено в `events.jsonl`). Эти файлы вне рабочего дерева, невидимы для `current.diff`/review/commit, не редактируются (карточка содержит нередактированный `originSessionId: c99cbf29-…`) и дублируют/подрывают управляемую `.worc/memory/`. Накопились ещё с фазы P0 (`p0-04-…md`, `p0-complete-…md`).
2. **F29 (MEDIUM-HIGH) — рассинхрон словаря `evidence.type`.** Супервайзер помечает доказательства как `type:"file"` (32 из 36 указателей) и `"commit"` (1), а `assign_trust` таких типов не знает → все репо-обоснованные уроки схлопываются в `agent-inferred` → 18 из 21 навсегда в карантине.
3. **F30 (MEDIUM) — рекуррентность ключуется по дословному `subject`.** Один и тот же повторяющийся урок (prettier-drift, встречался в 3 задачах) получает 3 разных `memory_id`, каждый застревает на «1/2 tasks» и никогда не промоутится — хотя он `artifact-backed` и промоутнулся бы на 2-й задаче, совпадай `subject`.
4. **F31 (LOW-MEDIUM) — узел `review` не получает пакет памяти.** Evaluator-раннер не прокидывает `memory_path` (в отличие от agent-раннера), блок `{memory_path}` в `review.md` мёртв, reviewer-preference-ранжирование в `packet.py` для review не срабатывает.

---

## Что реально записалось после 8 задач

| Тир | Файл | Записей | Состав |
| --- | --- | --- | --- |
| Эпизоды (short-term) | `short_term/recent.jsonl` | **8** | `ep_p4-01-v2` … `ep_p4-08`, все `trust=artifact-backed`, `status` активный |
| Entity-карточки | `entities/entities.jsonl` | **8** | 8 модулей graph-слоя, все `trust=repo-observed`, все `active` |
| Уроки (long-term) | `long_term/*.jsonl` | **0** | тир пуст |
| Карантин | `quarantine/pending.jsonl` | **21** | 18 `agent-inferred` + 3 `artifact-backed`, все `quarantined` |
| Аудит | `audit/log.jsonl` | **37** | 8 append(эпизод) + 8 append(entity) + 21 quarantine; цепочка цела |

Суммарно 8 задач сгенерировали 21 урок-кандидат и 8 entity-карточек — но ни один урок не стал durable.

**Но это только управляемая память. Рядом с ней есть вторая — см. F37.**

---

## 0. F37 (HIGH) — теневая нативная память Claude Code, пишущаяся в домашнюю папку оператора вне изоляции

Самая серьёзная находка аудита и она НЕ про `.worc/memory/`. Спаунящийся `claude` — это полноценный Claude Code CLI, и у него активна **собственная нативная файловая память**, скоупнутая на проект по рабочей директории. Оркестратор запускает агента в `cwd=/Users/a1234/Documents/GitHub/wastech-mdlint`, и нативная память садится в `~/.claude/projects/-Users-a1234-Documents-GitHub-wastech-mdlint/memory/`.

**Доказательства (firsthand, не по отчёту субагента):**

- Директория существует и содержит: `MEMORY.md` (индекс, 3 записи), `p0-04-tsconfig-src-cleanup.md`, `p0-complete-config-deferrals.md` (обе с фазы P0, 3 июля), `p4-06-grp-coverage-idref.md` (создан **5 июля 00:19 — во время прогона p4-06**).
- `stages/implementation/run-000089/1-claude/events.jsonl` p4-06 (строки 393-399) буквально показывает: `Read` `…/memory/MEMORY.md` → tool_result с текущим индексом → `Write` `…/memory/p4-06-grp-coverage-idref.md` («File created successfully at: /Users/a1234/.claude/projects/…/memory/p4-06-…») → `Edit` `…/memory/MEMORY.md` («has been updated»). То есть агент имплементации сам прочитал индекс, дописал карточку и обновил индекс.
- Нативная память **читается/инъектируется во ВСЕХ 8 задачах** (10-15 упоминаний `.claude/projects/-Users…` в `events.jsonl` каждой) — `system init`-событие узла несёт полный нативный тулсет Claude Code. **Запись** произошла в p4-06 (и ранее в P0-задачах); в остальных P4-прогонах агент читал, но не писал — то есть поведение **недетерминированное** (агент сам решает, воспользоваться ли инструментом памяти).
- Карточка `p4-06-grp-coverage-idref.md` несёт в frontmatter `originSessionId: c99cbf29-95b9-4a51-a420-1b6325ab5d21` — **нередактированный сырой session-id** записан в домашнюю папку. Управляемая `.worc/memory/` редактирует session-id везде; нативная — нет.

**Почему это важно (не косметика):**

1. **Пробой изоляции.** Инвариант «агент работает в изолированном workspace; персистентные мутации — только через оркестратор» нарушен: агент пишет durable-файлы в `~/.claude/` оператора, которые переживают задачу и накапливаются между задачами (уже с P0). Это вне рабочего дерева, вне `current.diff`, вне commit — оркестратор об этих записях не знает.
2. **Вне редакции и аудита.** Ничего из машинерии `.worc/memory/` (redaction-чокпоинт, hash-цепочка, trust-гейт, poisoning-защита) к нативной памяти не применяется. Уже наблюдается утечка session-id; в общем случае туда может уехать что угодно, что агент сочтёт нужным «запомнить».
3. **Две параллельные памяти, одна неуправляемая.** Оркестратор построил тщательную управляемую подсистему (trust/redaction/audit/promotion), но спаунящийся CLI ведёт собственную неуправляемую память в том же контексте агента. Все poisoning-защиты `.worc/memory/` бессмысленны, пока рядом лежит сырая нативная.
4. **Ирония:** карточка `p4-06-grp-coverage-idref.md` — фактически корректный, детальный, полезный урок с `[[p4-07-…]]`-виклинками и разделами «Why»/«How to apply» — ровно то, что управляемая подсистема ДОЛЖНА была захватить, но застряла квартинировать (F29/F30). То есть реально работающая память кампании — это неуправляемая нативная, а не спроектированная управляемая.

**Корневая причина / рычаг.** Оркестратор спаунит `claude` с активной нативной памятью (нативный memory-system-prompt инъектируется, memory-директория авто-подхватывается по `cwd`) и не конфайнит `Write`/`Edit` рабочим деревом: `implementation`-узел идёт с `--allowedTools Read,Glob,Grep,Edit,Write,Bash`, а `--disallowedTools` запрещает лишь чтение `.env`/`secrets/**` и git/gh — ничто не мешает `Write` в `/Users/a1234/.claude/…`. Рычаг: конфигурация спауна в [providers/claude.py](../../src/wastech_orchestrator/providers/claude.py) — отключить нативную память для спаунящихся агентов (напр. изолированный `CLAUDE_CONFIG_DIR`/settings) и/или конфайнить запись рабочим деревом (`--disallowedTools` на пути вне репо). `CLAUDE_CONFIG_DIR` уже в allowlist `security.allowed_environment` — то есть он прокидывается как есть в домашний конфиг оператора.

**Влияние.** Тихая утечка знаний о проекте (и session-id) в неаудируемое хранилище в домашней папке, накапливается через все задачи всех кампаний; подрывает смысл управляемой подсистемы памяти. HIGH.

---

## 1. Эпизоды (`short_term/recent.jsonl`) — записаны, но бессодержательны

8 эпизодов, по одному на задачу, все `trust=artifact-backed`, TTL не истёк (`short_term_ttl_days=30`, записаны 2026-07-04). Каждая запись фактически корректна (правильный `task_id`, `created_at`), но **несёт нулевой распознаваемый контент**:

- `stage_outcomes` = `{}` (пусто) у всех 8 — хотя схема поля предполагает `{node: outcome}`. Источник: `_write_memory` ([core/orchestrator.py:2111-2117](../../src/wastech_orchestrator/core/orchestrator.py#L2111)) пишет успех-эпизод с `outcomes=None → {}`; непустой `outcomes` передаётся только на failure-пути (`_record_failure_memory`, `{"task": final}`).
- `touched_paths` = `[]`, `touched_symbols` = `[]`, `base_commit`/`head_commit` = `null` у всех 8 — коммиты и тронутые пути в эпизод не переносятся.
- В пакете эпизод рендерится как голое `- task p4-06-grp-refactor-coverage` (`_episode_bullet`, [memory/packet.py:314](../../src/wastech_orchestrator/memory/packet.py#L314)) — без исхода, без файлов. Три таких строки в разделе «Recent episodes» пакета не несут агенту никакой информации, кроме имён предыдущих задач.

**F36 (LOW) — абсолютные host-пути в durable-store + непоследовательная редакция.** `artifact_paths` эпизодов хранят абсолютный путь: для `ep_p4-02..ep_p4-07` — буквально `/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/logs/...`, а для `ep_p4-01-v2` и `ep_p4-08` — `[REDACTED]/.worc/logs/...`. То есть один и тот же безобидный путь в 6 задачах записан как есть, а в 2 — отредактирован. Причина: набор redaction-литералов харвестится в рантайме из env-секретов + `.env`/`secrets/**` (`_memory_extra_secrets`, [core/orchestrator.py:2047-2059](../../src/wastech_orchestrator/core/orchestrator.py#L2047)), поэтому зависит от преходящего состояния процесса; в прогонах p4-01-v2/p4-08 какой-то харвестнутый литерал совпал с префиксом репо-пути и вычистил его, в остальных — нет. Влияние низкое (это локальный путь, не credential), но: (а) редакция **невоспроизводима** для идентичных данных, что для security-чокпоинта — сигнал; (б) абсолютный путь с именем оператора вообще не место в durable-памяти — по инварианту `records.py` пути должны быть repo-relative POSIX, но `as_posix()` абсолютного пути остаётся абсолютным. Рычаг: [core/orchestrator.py:2117](../../src/wastech_orchestrator/core/orchestrator.py#L2117) — хранить `.worc`-относительный путь.

---

## 2. Entity-карточки (`entities/entities.jsonl`) — фактически корректны, одна устаревшая заметка

8 карточек graph-модулей, все `trust=repo-observed`, все `active`. **Проверка на факты (сверено с реальным кодом ветки `feat/p4-graph-chain` в рамках Части A):** summary каждой карточки точно описывает модуль — `query.ts` = унифицированный обход, `graph-algorithms.ts` = topo/components, `impact-analysis.ts` = getImpactSet/classifyImpact, и т.д. Символы (`buildContextGraph`, `topologicalSort`, `getContextSlice` …) присутствуют в коде. Токсичности нет: все карточки репо-обоснованы, ни одной внешней/недоверенной.

Замечания:

- **Устаревшая risk-заметка (staleness).** Карточка `build-context-graph` несёт `risk_notes: ["exclude/entryPoints options accepted but not consumed until P4.06", ...]`. По факту (проверка Части A) `exclude`/`entryPoints` **не потребляются и после P4.06** — читаются только `siteRouter`+`idRef`. Заметка не просто устарела, а **вводит в заблуждение**: обещала потребление в P4.06, которого не случилось. Cleanup-джоб это не поймает — он проверяет только существование путей, а не точность risk-заметок (`_classify_entity`, [memory/cleanup.py:163](../../src/wastech_orchestrator/memory/cleanup.py#L163)); файл жив → карточка «свежая». Вторая заметка той же карточки («id-ref via plain-text token scan, false-positive risk») — **корректна** (сверено с `buildIdRefEdges`).
- **Провенанс не заполняется.** У всех 8 карточек `last_seen_task_ids: []`, `last_validated_commit: null`, `memory_refs: []`. `_ingest_entity` ([memory/service.py:315-326](../../src/wastech_orchestrator/core/../memory/service.py#L315)) строит `EntityRecord` без `last_seen_task_ids` (дефолт `()`), хотя карточка создаётся в конкретной задаче. Нельзя ответить «какая задача последней трогала этот модуль» — провенанс, ради которого тир и заведён, пуст.
- **Ненормализованные `entity_id`.** Часть id семантические (`graph-algorithms-module`, `core-graph-query`, `core-graph-search-index`), часть — имена файлов (`load-context.ts`, `graph-render.ts`, `build-context-graph`, `context-graph-types`). Супервайзер выбирает id по своему усмотрению каждый раз; единой схемы нет. Дедупа по id это не ломает (upsert по точному `entity_id`), но при повторном появлении того же модуля под другим id получится дубль-карточка.

---

## 3. Карантин (`quarantine/pending.jsonl`) — 21 корректный урок, застрявший навсегда

Все 21 записи фактически **осмысленные и по большей части верные** уроки (напр. «readingOrder топологичен — не пересортировывать», «graph.cycles — единственный владелец детекции циклов», «сортировать user-visible массивы localeCompare по repo-relative POSIX»). Это не мусор и не токсичный контент — это ровно та ценность, ради которой затевалась память. Но каждая застряла в карантине по одной из двух причин:

**(а) 18 записей — `agent-inferred` из-за F29 (рассинхрон `evidence.type`).** Аудит-`rationale` у всех 18: `"quarantined: non-durable trust 'agent-inferred'"`. Причём это НЕ значит, что урок необоснован — у большинства есть доказательство-указатель на **реальный файл репозитория**, просто помеченный как `type:"file"`:

Фактические типы доказательств во всех карантинных уроках: **`file`: 32, `check`: 3, `commit`: 1**. `assign_trust` ([memory/lifecycle.py:36-54](../../src/wastech_orchestrator/memory/lifecycle.py#L36)) распознаёт `_REPO={repo,repo_doc,code,config,doc}` и `_ARTIFACT={artifact,check,diff,test,plan}` — **`file` и `commit` не входят ни в один класс** → грунтуют «ничего» → `agent-inferred`. То есть урок с доказательством `{"type":"file","ref":"packages/core/src/graph/query.ts"}` — репо-обоснованный по смыслу — деградирует до недурабельного только из-за токена типа. Схема `DELTA_OUTPUT_SCHEMA` ([memory/delta.py:119](../../src/wastech_orchestrator/memory/delta.py#L119)) оставляет `evidence.type` свободной строкой, а роль-промпт `summary.md` не задаёт словарь, поэтому супервайзер естественно пишет `file`/`commit` — и молча топит каждый урок.

**(б) 3 записи — `artifact-backed`, «held awaiting recurrence» из-за F30.** Аудит-`rationale`: `"held short-term: awaiting recurrence (1/2 tasks)"`. Это три ФАКТИЧЕСКИ ОДИНАКОВЫХ урока про prettier-baseline-drift, записанные в p4-01, p4-06, p4-07 — то есть урок **реально повторился в 3 задачах**. Но у каждого свой `subject`: `"npm run format baseline"` / `"repo-wide Prettier drift"` / `"prettier baseline drift"`. `_derive_id(kind, subject)` = `ltm_` + hash(`kind:normalize_subject(subject)`) ([memory/service.py:562-565](../../src/wastech_orchestrator/memory/service.py#L562)), а `normalize_subject` только lower+trim ([memory/lifecycle.py:79](../../src/wastech_orchestrator/memory/lifecycle.py#L79)) → три разных ключа `ltm_7ef2a85afddd`/`ltm_b13f8fdfeeb2`/`ltm_6019dbb25218` → `seen_task_ids` не накапливается → `recurrence=1 < min_tasks=2` каждый раз → «held». **Если бы `subject` совпал**, вторая задача нашла бы prior-запись, `seen=[p4-01,p4-06]`, `recurrence=2 ≥ 2` → **промоушен** (should_promote прошёл бы: `artifact-backed` durable + evidence + recurrence). То есть единственный класс промоутируемых уроков не промоутился чисто из-за вариативности формулировки `subject` у LLM-супервайзера.

**Зависших/просроченных записей нет** в смысле «давно пора разобрать» — карантин не имеет TTL, и это by-design (карантин — не корзина, а зал ожидания). Но 18+3 записей будут лежать там **вечно**: `agent-inferred` не промоутится по определению (нужна валидация/переоценка на durable-тип, которой V1 не делает), а 3 recurrence-кандидата не накопят повтор из-за F30. Cleanup-джоб их не трогает (он работает по `long_term`, не по карантину, и только demote/expire/quarantine/merge).

---

## 4. Почему `long_term/` пуст — это НЕ «by-design V1 не промоутит»

Ловушка интерпретации: `cleanup.py` действительно никогда не промоутит (`cleanup_promotions_per_pass` — documentation-only инвариант, рантайм его не читает, [memory/cleanup.py:38-47](../../src/wastech_orchestrator/memory/cleanup.py#L38)). **Но промоушен живёт не в cleanup, а в `apply_delta` на write-path** ([memory/service.py:284-302](../../src/wastech_orchestrator/memory/service.py#L284)): при durable-trust + evidence + пройденном `should_promote`-гейте урок пишется в `long_term` прямо в момент финализации задачи. Этот код **исполнялся 21 раз** и ни разу не промоутнул — не потому что его нет, а потому что:

- 18 кандидатов не прошли **необходимое** условие durable-trust (F29) — отсекаются веткой `trust not in DURABLE_TRUST_LEVELS` до гейта рекуррентности;
- 3 durable-кандидата не прошли **достаточное** условие (любое из recurrence/auto-promote/explained-failure) — recurrence сломан F30, а `artifact-backed` не входит в `_AUTO_PROMOTE` (только `human-curated`/`review-verified`).

Показательно: узел `review` (единственный, чьи находки могли бы дать `review-verified` — auto-promote-трест) фактически весь прогон исполнялся **claude-фоллбэком** (codex падал 9/9, F24), но его findings идут в `evaluations`, а не в `memory_delta`; супервайзер, формирующий delta, ссылается на файлы через `type:"file"`. Так что даже review-обоснованные уроки не получают `review-verified`. Итог: при текущем write-path `long_term` останется пустым и через 50 задач — не хватает не времени, а корректной классификации.

---

## 5. Целостность аудита и защита от poisoning — исправны (позитив)

- **Hash-цепочка цела.** Проверено программно по всем 37 строкам: `prev_hash[i] == row_hash[i-1]` без единого разрыва, genesis `prev_hash=""`. `pre_hash`/`post_hash` каждой записи консистентны (первый touch тира → `pre_hash` = SHA256("")).
- **F9 закрыт.** У всех 37 записей `rationale` заполнен конкретной причиной: 8× `"recorded short-term episode"`, 8× `"appended entity card (repo-observed trust)"`, 18× `"quarantined: non-durable trust 'agent-inferred'"`, 3× `"held short-term: awaiting recurrence (1/2 tasks)"`. `_reason`/`_quarantine_reason` ([memory/service.py:568-581](../../src/wastech_orchestrator/memory/service.py#L568)) дают детерминированную причину независимо от модели. Прошлая находка «rationale пуст» больше не актуальна. (Отдельно: у самих записей-уроков поле `rationale: null` — это опциональный `rationale` **кандидата** от модели, которого супервайзер не дал; это не audit-rationale и не дефект.)
- **Poisoning-инвариант держится.** Ни одной `external-untrusted`-записи; `assign_trust` ставит `external-untrusted` при любом внешнем указателе (`web/mcp/url/external/api`), и ничего такого не появилось. Кандидат никогда не самосертифицируется: `trust_hint` от модели — advisory, финальный трест назначает детерминированный `assign_trust`. Ирония в том, что тот же строгий классификатор, что защищает от poisoning, из-за F29 топит и легитимные репо-уроки — защита работает даже слишком агрессивно.
- **Редакция — единый чокпоинт.** Каждая строка проходит `_redact` перед записью ([memory/service.py:554-556](../../src/wastech_orchestrator/memory/service.py#L554)); секретов в дампах нет. Единственная шероховатость — непоследовательность на репо-пути (F36), но это не утечка секрета.

---

## 6. Полезность для агента: пакет читается, но нести ему нечего

Проверено на реально отрендеренных пакетах поздних задач (`p4-07`, `p4-08`), где память от p4-02..06 уже должна была накопиться:

- **Куда инъектируется.** Пакет получают только `planning`, `implementation`, `fixing` (подтверждено grep по `request.json`: у этих узлов в промпте есть блок «repository memory relevant», у `review`/`documentation`/`supervisor` — нет). Пакет пишется в `.worc/logs/<task>/memory/<node>.md`.
- **Читается ли агентом — ДА.** В `events.jsonl` planning-узла p4-07/p4-08 виден явный `Read` файла `…/memory/planning.md` **вторым tool-call'ом** (сразу после task-файла); в implementation — `Read` `…/memory/implementation.md`. Это не декоративная инъекция — агент реально открывает пакет.
- **Релевантен ли — ДА.** Пакет p4-07/planning.md нёс 5 entity-карточек ровно тех graph-модулей, которые CLI-задача обвязывает; p4-08/implementation.md — 7 карточек, включая свежесозданные в p4-07 `load-context.ts`/`graph-render.ts`. Ранжирование по path-overlap работает.
- **Использован ли — СЛАБО, подтверждающе, не решающе.** План p4-07 переиспользует `getContextSlice`/`classifyImpact`/`SLICE_RESOLUTION_DESCRIPTION` ровно как описано в карточках — но агент **и так открывал те же исходники напрямую**, поэтому пакет был подтверждающим дублем, а не источником нового знания. **Ключевой факт:** пакет содержит ТОЛЬКО entity-summary + бессодержательный список эпизодов; **ни одного урока/подводного камня** — хотя промпт обещает агенту «distilled lessons, conventions, known-fragile areas». Причина прямая: раздел «Lessons» пакета строится из `long_term` ([memory/packet.py:132-141,268-271](../../src/wastech_orchestrator/memory/packet.py#L132)), а он пуст (F29/F30) — карантинные уроки в пакет не попадают (`_is_active` фильтрует `status!=active`). То есть самый ценный слой памяти агент не видит именно из-за дефектов write-path.
- **Единственный случай, где пакет реально помог:** entity-карточка `build-context-graph` несла верный путь `build-context-graph.ts`, тогда как роль-промпт planning ссылается на несуществующий `graph/build.ts` (см. F34 в Части B) — пакет де-факто **исправил устаревший промпт**. Приятный побочный эффект, но иллюстрирует, что работающая часть памяти — это entity-карточки, а не уроки.

**F31 (LOW-MEDIUM) — `review` не получает пакет.** Evaluator-раннер не прокидывает `memory_path`: `_prompt_variables` ([memory/../core/flow/nodes/evaluator.py:289-300](../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L289)) не содержит ключа `memory_path`, и пакет не строится, тогда как agent-раннер это делает ([core/flow/nodes/agent.py:534,596-600](../../src/wastech_orchestrator/core/flow/nodes/agent.py#L534)). Поэтому блок `{?memory_path}` в `review.md` (строка 48) **мёртв**, а reviewer-preference-ранжирование `packet.py` (`_REVIEWER_PREF_NODES={review,fixing}`, [memory/packet.py:41](../../src/wastech_orchestrator/memory/packet.py#L41)) для review никогда не применяется. При этом именно review — узел, которому «recurring reviewer expectations» пригодились бы больше всего (в карантине есть reviewer-kind уроки вроде «assert sorted+POSIX в фикстурах»). Рычаг: либо прокинуть пакет в evaluator-раннере, либо убрать мёртвый блок из `review.md`.

---

## Находки (новые F-номера — полные записи в TEST-FINDINGS.md)

- **F37 (HIGH)** — теневая нативная память Claude Code: спаунящиеся агенты читают/пишут `~/.claude/projects/<target>/memory/` вне изоляции/редакции/аудита, параллельно управляемой `.worc/memory/`; наблюдается запись (p4-06) и утечка нередактированного session-id. Рычаг: [providers/claude.py](../../src/wastech_orchestrator/providers/claude.py) — отключить нативную память/конфайнить запись рабочим деревом.
- **F29 (MEDIUM-HIGH)** — рассинхрон словаря `evidence.type`: `file`/`commit` не распознаются `assign_trust` → 18/21 уроков навсегда `agent-inferred`. Рычаг: [lifecycle.py:24-28](../../src/wastech_orchestrator/memory/lifecycle.py#L24) (+ `file→repo`, `commit→artifact`) и/или enum-ограничение `evidence.type` в [delta.py:119](../../src/wastech_orchestrator/memory/delta.py#L119) + словарь в `summary.md`.
- **F30 (MEDIUM)** — рекуррентность по дословному `subject`: повторяющийся `artifact-backed` урок не промоутится, т.к. LLM меняет формулировку `subject`. Рычаг: [service.py:562](../../src/wastech_orchestrator/memory/service.py#L562)/[lifecycle.py:79](../../src/wastech_orchestrator/memory/lifecycle.py#L79) — более устойчивый ключ дедупа (scope.paths + kind, или fuzzy-нормализация).
- **F31 (LOW-MEDIUM)** — `review` не получает пакет памяти; блок в `review.md` мёртв. Рычаг: [evaluator.py:289](../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L289) или `review.md`.
- **F36 (LOW)** — абсолютные host-пути в эпизодах + невоспроизводимая редакция (2/8). Рычаг: [orchestrator.py:2117](../../src/wastech_orchestrator/core/orchestrator.py#L2117).

## Что уже хорошо (проверено, позитив)

- Hash-цепочка аудита цела на всех 37 записях; F9 (пустой rationale) — закрыт, все мутации объяснены.
- Poisoning-защита держится: ноль `external-untrusted`, кандидат не самосертифицируется, редакция — единый чокпоинт.
- 8 entity-карточек фактически корректны и репо-подтверждены (сверено с кодом ветки).
- Пакет реально читается агентом (не мёртвая инъекция) и релевантен по path-overlap; ранжирование/капы работают детерминированно.
- `memory_delta:false` у p4-08 (тест-онли задача) — корректное поведение: нет нового модуля → нет delta, эпизод всё равно записан.

## Сводная таблица

| Наблюдение | Причина | Рычаг (file:line) | Зона |
| --- | --- | --- | --- |
| Агенты читают/пишут нативную память в `~/.claude/…` вне изоляции (p4-06 записал карточку + session-id) | спаун `claude` с активной нативной памятью, `Write` не конфайнут рабочим деревом | `providers/claude.py` | orchestrator (F37, HIGH) |
| `long_term/` пуст после 8 задач, 18/21 уроков `agent-inferred` | `evidence.type:"file"/"commit"` не распознаны `assign_trust` | `memory/lifecycle.py:24-28` | orchestrator (F29) |
| 3× повторившийся prettier-урок не промоутнулся (3× «held 1/2») | `_derive_id` ключует по дословному `subject`, LLM его варьирует | `memory/service.py:562` / `lifecycle.py:79` | orchestrator (F30) |
| `review` не получает пакет, блок `{memory_path}` в `review.md` мёртв | evaluator-раннер не прокидывает `memory_path` | `core/flow/nodes/evaluator.py:289` | orchestrator (F31) |
| Эпизоды бессодержательны (`stage_outcomes={}`, `touched_paths=[]`) | success-эпизод пишется без outcomes/путей | `core/orchestrator.py:2111-2117` | orchestrator |
| Абсолютный host-путь в эпизодах, редакция 2/8 непоследовательна | набор redaction-литералов харвестится в рантайме; путь не relativized | `core/orchestrator.py:2117` | orchestrator (F36) |
| risk-заметка `build-context-graph` про exclude/entryPoints устарела | cleanup проверяет только существование путей, не точность заметок | `memory/cleanup.py:163` (по дизайну) | orchestrator/target |
| Пакет читается и релевантен, но несёт только entity-summary, без уроков | раздел «Lessons» строится из пустого `long_term` (следствие F29/F30) | `memory/packet.py:132` | orchestrator (следствие) |
| Hash-цепочка цела, rationale заполнен, poisoning не пробит | write-funnel + audit работают как задумано | — | orchestrator (позитив) |
