# Supervisor P0: детерминированный SupervisorPacket → fresh finalize → пропуск tool/checks

**Статус:** **accepted 2026-07-26** (все развилки закрыты в «[Решения приёмки](#решения-приёмки-2026-07-26)»; первый шаг реализации — снять baseline по X1) **Приоритет:** P0 (самый крупный и самый безопасный резерв экономии в content-pipeline) **Источник:** [2026-07-16 варианты оптимизации supervisor](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/analysis/2026-07-16-supervisor-token-optimization-options.md) (§7 SupervisorPacket, §8 P0, Варианты A/E/F). Смежные задачи: [normalized-usage-accounting.md](normalized-usage-accounting.md) (мерная подложка для A/B), [content-flow-token-hygiene.md](content-flow-token-hygiene.md).

**Дорожная карта:** **P0 (этот документ)** → [P1 — управляемый cadence](supervisor-observation-cadence-p1.md) → [P2 — разделение обязанностей и telemetry](supervisor-responsibility-split-p2.md).

## Проблема

Supervisor — самый тяжёлый потребитель Claude-контекста. На исследованном прогоне `blog-review-happy-in-my-misfortunes-4` его семь вызовов потратили 480 293 input-токенов и $0.77 — это 70% всего Claude-input задачи. Шесть из семи вызовов — промежуточные наблюдения, а supervisor по контракту advisory-only: он не читается движком для роутинга, у него нет `route`/`rework` (`core/supervisor.py`, подтверждено в анализе).

Главная тонкость, из-за которой «просто урезать наблюдения» — это баг, а не оптимизация: **finalize сегодня не получает детерминированный пакет фактов.** Он опирается на тёплую сессию, которая по ходу прогона читала diff и копила наблюдения, а на revive — на digest этих наблюдений (`_finalize_digest`, `core/supervisor.py:837`). Встроенный finalize-промпт прямо просит синтез «grounded in the actual committed change» и «caveats you noted across the steps» (`_BUILTIN_FINALIZE`, `core/supervisor.py:177-182`). Если перевести finalize на fresh-сессию (Вариант E) или убрать наблюдения (Вариант F / `observe.mode: none`) **до** появления пакета, то на чистом прогоне digest пуст, и finalize остаётся с одним `role_file + task_title` — он либо идёт заново читать репозиторий (медленно, съедает всю экономию), либо пишет тонкий summary. Заодно деградируют `follow_ups` и `memory_delta`, ценность которых именно в том, что supervisor наблюдал прогон.

Отсюда обязательный порядок внедрения, который и фиксирует эта задача.

## Требуемый результат

Единый детерминированный `SupervisorPacket`, собираемый из уже существующих durable-источников, который делает finalize независимым от живой тёплой сессии. Только после этого finalize переводится на fresh-сессию по умолчанию, а наблюдения детерминированных нод (`tool`, `checks`) отключаются. Итог: экономия input-токенов без потери полноты summary; normal-путь и revive-путь finalize становятся одинаковыми и воспроизводимыми.

## Решения (обязательный порядок)

**Внутри P0 порядок работ P0.1 → P0.2 → P0.3, и весь P0 едет одним изменением (решение P0-D5).** Жёсткое требование формулируется точно: пакет обязателен **перед выключением всех наблюдений** — то есть перед `observe.mode: none`/`events` из [P1](supervisor-observation-cadence-p1.md). Пропуск детерминированных `tool`/`checks` под это требование не попадает и запретом не является (см. P0.3), но отдельным PR раньше пакета его не выносим.

- **P0.1 — сначала пакет.** `finalize` всегда собирает `SupervisorPacket` из уже имеющихся фактов (`node_runs`, `evaluations`, `current.diff`, findings, checks) + компактный digest материальных наблюдений (`_finalize_digest`). Новых данных собирать не нужно — всё уже лежит на диске и в `state.db`.
- **P0.2 — затем fresh finalize по умолчанию.** Убираем ветку «тёплый resume vs digest»: и normal, и revive идут одним путём — fresh-сессия (`resume_session=False`), засеянная пакетом. Механизм fresh-из-digest уже существует как recovery (`_finalize_digest` + `resume=False`), делаем его основным.
- **P0.3 — затем пропуск наблюдений `tool`/`checks`.** Правка одного условия в post-node hook (`core/orchestrator.py:3204`, текущее условие `node.kind != "publish"`): `node.kind not in {"tool", "checks", "publish"}`. Безопасно: пропущенные/недетерминированные ноды и так не наблюдаются, а на стейт-машину cadence не влияет (advisory). Этот пропуск сам по себе НЕ обесточивает finalize даже без пакета — содержательные наблюдения (`revise`/`tone_style`/`polish`) продолжают идти, — поэтому он и не подпадает под требование «сначала пакет»; но по решению P0-D5 он едет в том же изменении, а не отдельным PR. В packaged-флоу таких нод ровно по одной на flow (`blog_article_revise` → `length`, `implementation` → `checks`), то есть минус один наблюдательный вызов за прогон (исторически ~44k input-токенов на `length`).

Дополнительно:

- **Наблюдения (кроме `tool`/`checks`) в P0 продолжают идти как сейчас.** Это переходное состояние: они по-прежнему кормят `material_observations` в пакете, поэтому качество `follow_ups`/`memory_delta` не проседает до того, как пакет будет проверен на A/B. Понижение cadence (`observe.mode: events | none`) — это P1, не P0.
- **Пакет ссылается на полные артефакты и содержит только bounded-выжимку.** Полный diff встраиваем только если он мал; иначе — changed paths + diff stat + путь к `current.diff` (§7 анализа).
- **Актуализация 2026-07-23 — пакет передаётся по пути к read-only копии в exchange, а не инлайн-JSON.** После этого анализа приземлился **WRI-011**: finalize уже читает задачу не инлайном, а из замороженного exchange-пакета через context-footer (`_finalize_prompt`, `core/supervisor.py:1281` — «the task reaches the turn as the frozen exchange packet … never inline title/body»), и `finalize` уже прокидывает `task_path`. Точный маршрут для `SupervisorPacket` зафиксирован ниже в решении P0-D1 — в частности, «тот же паттерн» означает тот же принцип (путь к редактированной read-only копии в agent-facing корне), а **не** тот же frozen-bundle.

## Решения приёмки (2026-07-26)

### P0-D1 — маршрут пакета: своё поле запроса + публикация в `.worc-io`

`finalize` получает пакет как путь к отредактированной read-only копии в agent-facing корне, отдельным именованным полем запроса:

1. Оркестратор пишет приватный авторитетный `packet.json` в supervisor-артефакты задачи (`node_run_dir(artifacts_root, task_id, "supervisor", 0)` — тот же namespacing, что у остальных supervisor-артефактов, где `0` — finalize-сентинел).
2. Публикует редактированную копию готовым сеамом: `publish_artifact(exchange_root, task_id, "supervisor/packet.json", content, extra_secrets=…, private_path=…)` (`core/flow/nodes/exchange_publish.py:117`) → `.worc-io/<task-id>/supervisor/packet.json`. `extra_secrets` передавать обязательно — тот же набор литералов, что использует редакция сохранённых промптов.
3. Новое поле `AgentRunRequest.supervisor_packet_path` + строка `packet` в `build_context_footer` (`providers/base.py:229-250`). Поля контекста перечислены явно ещё в двух местах, оба нужно дополнить: containment-проверка `providers/exchange.py:398` и audit-дикт `providers/_adapter_base.py:658`.
4. Путь передаётся в POSIX-форме (`as_posix()`), как и остальные exchange-пути.

**Почему не frozen instruction bundle.** `instruction-bundles/<task-id>/` (WRI-011) — снимок _входов_, замороженный на старте задачи, с composite `instruction_manifest_digest`, который сверяется перед переиспользованием provider-сессии (`core/flow/instruction_bundle.py:1-31`). Пакет рождается в конце прогона, поэтому попал бы туда только через исключение в manifest-контракте — цена выше, чем одно новое поле запроса.

**Почему не инлайн и не чужое поле.** Инлайн возвращает в промпт ровно те байты, ради сокращения которых затевался P0, и обходит единый redaction-seam. Переиспользование `plan_path`/`check_artifacts_path` ломает диагностику: в context-footer, в rendered-prompt и в prompt-audit пакет назывался бы планом или checks.

**Следствие:** P0-D6 (редакция пакета) закрыт этим же решением — публикация в exchange редактирует по построению, отдельного механизма не требуется; в критерии остаётся только «`extra_secrets` переданы».

### P0-D2 — детерминизм = чистая функция durable-состояния

«Пакет детерминирован» означает: **сборка пакета — чистая функция `state.db` + артефактов задачи**, без каких-либо иных входов. Ни системных часов, ни env, ни абсолютных путей, ни порядка обхода файловой системы: все пути в пакете — repo-relative POSIX, шаги упорядочены по `node_runs.id`, сериализация каноническая (`json.dumps(..., sort_keys=True)`, запись с `newline=""` — тот же домашний паттерн, что у manifest'ов, `core/flow/exchange_seal.py:189`).

Что это даёт в терминах проверки — два утверждения вместо одного непроверяемого:

1. Сборка дважды из одного и того же состояния даёт байтово одинаковый пакет.
2. Revive, который не переисполнил ни одной ноды, даёт тот же пакет; если ноды переисполнились, пакет отличается **ровно** на эти шаги и ни на что другое.

Таймстемпы, `stage_attempts`, `provider_used`, fallback- и retry-факты из пакета **не** вычищаются: они детерминированы (лежат в БД) и это именно тот материал, из которого finalize пишет caveats. Требование идентичности normal vs revive относится к тому, что состояние одинаково, — а не к тому, что мы прячем различия.

**Следствие:** P0-D7 (кросс-платформенность) закрыт этим же решением — repo-relative POSIX внутри пакета и `newline=""` при записи; отдельного решения не нужно.

### P0-D3 — конкретные границы пакета

Именованные char-константы в коде (не config — настраиваемость никто не просил), обрезка через «…», как у `_SKILL_DESCRIPTION_INLINE_CAP` / `_FINDING_TITLE_MAX`:

| Что | Граница | Поведение при превышении |
| --- | --- | --- |
| полный diff в пакете | ≤ 4 000 симв. | не встраивается: остаются changed paths + diff stat + путь к `current.diff` |
| `steps[].message` | ≤ 500 симв. | обрезка с «…» |
| `material_observations` (digest) | ≤ 8 000 симв. | обрезаются самые старые строки, остаток помечается |

Итоговый потолок пакета — порядка 16 КБ; это и есть смысл «пакет измеряется килобайтами, а не сотнями килобайт истории».

Почему малый diff всё-таки встраивается, а не отдаётся только путём: отсутствие инлайна экономит байты пакета, но заставляет модель сделать лишний tool-round, а каждый round заново отправляет весь промпт как input — на типичном content-diff это дороже, чем встроить 4 КБ сразу.

Попутно закрывается дефект, найденный при сверке: сегодня `final_message` шага попадает в observe-промпт вообще без ограничения (`core/supervisor.py:1235`), так что болтливая нода раздувает каждое наблюдение. Тот же 500-символьный кап применяется и там.

### P0-D4 — тёплая ветка finalize удаляется вместе с флагом

Ветка «resume на свою durable-сессию» (`warm = self._session_live`, `core/supervisor.py:727`) удаляется без config-тумблера: остаётся один путь — fresh-сессия, засеянная пакетом. Флаг `_session_live` удаляется вместе с ней: он читается только в finalize (объявлен `:648`, ставится `:1044`), и оставленный write-only будет справедливо помечен `vulture` в CI.

Тумблер не вводим: проект greenfield (нигде не развёрнут), поэтому обратная совместимость ничего не требует, а ключ стоил бы bump схемы config внутри P0 — вопреки его же заявлению, что схема не меняется, — и второго кодового пути в тестах навсегда. Если A/B покажет просадку качества, откат — это `git revert`. Config-ключа не будет и в P1: у finalize останется единственный режим, а ключ с единственным допустимым значением — мёртвая настройка (см. решение P1-D8).

Тёплую сессию как автоматический фолбэк «если пакет не собрался» тоже не вводим: это вернуло бы недетерминизм в единственный путь и замаскировало бы баг сборки. Штатный фолбэк остаётся тем же — детерминированный минимальный summary оркестратора при неудачном turn.

## В объёме P0

1. Тип `SupervisorPacket` и его сборка из durable-состояния: `task {id,title,type}`, `flow {name,final_status}`, `changes {paths, diff_path, diff_stats}`, `steps [{node,outcome,message(bounded)}]`, `checks {passed,failed}`, `findings_path`, `material_observations` (из `_finalize_digest`).
2. `finalize` всегда строит пакет и всегда запускается на fresh-сессии, засеянной пакетом; ветка тёплого resume удаляется вместе с флагом `_session_live` (решение P0-D4). `summary.json` по-прежнему пишется всегда; deterministic-фолбэк оркестратора при неудаче turn сохраняется без изменений.
3. Пропуск `observe` для `node.kind ∈ {tool, checks}` в post-node hook.
4. Тесты (см. раздел ниже).
5. Синхронизация доков, которые физически есть на `dev` (решение X2, 2026-07-26): `src/wastech_orchestrator/packaged/guide/flows/roles.md:63` утверждает, что supervisor «observes **each step** and writes the final summary» — после P0 это неверно, фразу нужно переписать под «наблюдаются исполненные ноды, кроме `tool`/`checks`/`publish`» и под packet-first finalize. Схема config в P0 не меняется, поэтому `packaged/config.example.yaml` и `guide/config/reference.md` не трогаем. Derived `docs/` на `dev` не существует — вместо правки в описании PR оставляем строку doc-impact («затронуты finalize + cadence supervisor; вероятно влияет на `worc_architecture.md` и `configuration.md`») как хлебную крошку для реверс-инжиниринга на `main`.

Ожидаемый эффект на исследованном прогоне (историческая оценка 2026-07-16; код с тех пор менялся, поэтому эти числа — ориентир, а не порог): пропуск `length` снимает минимум 44 107 input-токенов; fresh finalize из компактного пакета ориентировочно уменьшает финальный вызов (тогда — 104 567 input-токенов) на 65–85 тыс. Проверяется относительным порогом против свежего baseline — см. «A/B и baseline» ниже.

### P0-D5 — весь P0 одним изменением

Пропуск `tool`/`checks` не выносится в отдельный PR: пакет, fresh finalize и пропуск едут одним изменением. Противоречие в документе снято выше — жёсткое требование «сначала пакет» относится к выключению всех наблюдений (P1), а не к пропуску детерминированных нод.

Следствие для измерений: точек замера две (до P0 → после P0), вклад пропуска и вклад пакета по отдельности не разделяются.

## Критерии приёмки

- [ ] `tool`/`checks` нода не порождает supervisor provider-request (нет observe-turn), но задача завершается штатно.
- [ ] Обычный (non-revive) finalize запускается на fresh-сессии: turn не получает warm session id, вход — `SupervisorPacket`.
- [ ] Сборка пакета — чистая функция durable-состояния (решение P0-D2): два вызова на одном и том же `state.db` дают байтово одинаковый пакет; пути внутри — repo-relative POSIX; сериализация каноническая, запись с `newline=""`.
- [ ] Revive без переисполнения нод даёт тот же пакет; при переисполнении отличается ровно на новые шаги — воспроизводимость summary.
- [ ] `SupervisorPacket` содержит changed paths + diff stats + путь к `current.diff`; полный diff встраивается только при ≤ 4 000 симв., `steps[].message` обрезан на 500, digest — на 8 000 (решение P0-D3); тот же 500-символьный кап применён к `final_message` в observe-промпте.
- [ ] `SupervisorPacket` передаётся finalize как путь к редактированной read-only копии `.worc-io/<task-id>/supervisor/packet.json` через новое поле `supervisor_packet_path` и строку `packet` в context-footer (решение P0-D1), а не инлайн-JSON в тексте промпта; публикация идёт через `publish_artifact` с непустыми `extra_secrets`, поэтому ни секрет, ни сырой diff в промпт не попадают.
- [ ] `follow_ups` (когда flow включил `emit_follow_ups`) и `memory_delta` (когда `memory.enabled`) по-прежнему производятся тем же одним finalize-turn — без доп. LLM-вызовов.
- [ ] Полнота summary не хуже baseline по четырём пунктам (что изменено / почему / какие проверки прошли / какие caveats).
- [ ] A/B против свежего baseline (см. «[A/B и baseline](#ab-и-baseline-решение-x1-2026-07-26)» ниже): supervisor input падает минимум на 60% при 0 пропущенных blocking-issue (их держит `tone_style`).
- [ ] Supervisor остаётся read-only и advisory; handoff и skill-proposal работают независимо от изменений.

## A/B и baseline (решение X1, 2026-07-26)

Абсолютные пороги из исходного анализа (`< 60 000` при baseline 480 293) непроверяемы: те числа получены на коде, которого больше нет — с тех пор приземлились WRI-011 (finalize читает задачу из замороженного exchange-пакета) и content-flow-token-hygiene (packaged-дефолты `blog_article_revise`, `polish` → `fresh_disposable`). Поэтому baseline переснимается, а порог задаётся относительным.

**Порядок:** до реализации P0 прогнать один `blog_article_revise` на текущем `dev` и записать таблицу ниже; она — единственный baseline, с которым сравнивается A/B. Прогон делается один раз и переиспользуется для A/B в [P1](supervisor-observation-cadence-p1.md).

| Baseline | task_id | дата | supervisor calls | input_total | cost |
| --- | --- | --- | --- | --- | --- |
| до P0 | _заполнить_ | _заполнить_ | _заполнить_ | _заполнить_ | _заполнить_ |
| после P0 | _заполнить_ | _заполнить_ | _заполнить_ | _заполнить_ | _заполнить_ |

**Чем мерим — нового кода не нужно.** VF-8 (DB v19) уже даёт task-anchor и отделяет постоянный supervisor-слой: он не нода графа, поэтому его provider-вызовы лежат в `provider_attempts` c `node_run_id IS NULL` (`state_store.py:103-108`, DDL `:298-329`), а нормализованные колонки появились в v16.

```sql
SELECT COUNT(*) AS calls, SUM(usage_input_total) AS input_total, SUM(usage_cost) AS cost
FROM provider_attempts
WHERE task_id = ? AND node_run_id IS NULL;
```

Операторскую поверхность чтения (`worc usage` / блок в `worc status`) в P0 **не** делаем: данные уже есть, а вывод — это пункты 3–4 из [P2](supervisor-responsibility-split-p2.md) (per-function usage + supervisor-отчёт в summary). Ридер `get_provider_attempts_for_task` существует, но сегодня используется только в тестах.

## Вне объёма P0 (следующие фазы)

- `observe.mode: all | selected | events | none`, event-триггеры, flow-local narrowing, раздельные `observe`/`finalize` model+reasoning — **[P1](supervisor-observation-cadence-p1.md)** (Варианты B/C/D/H/I). Для content-flow — `none`, для implementation — `events` (там `emit_follow_ups: true`, `none` просадил бы follow_ups/память).
- Разделение монолита на `StepRecorder`/`ObservationAdvisor`/`TaskFinalizer`/`SubtaskHandoff`/`SkillProposer` и per-function telemetry — **[P2](supervisor-responsibility-split-p2.md)** (§6, §8 P2).
- Полностью deterministic summary без LLM — опционально (Вариант G), не default.
- Изменения handoff/skill-proposer — не входят в P0.

## Тесты под замену/добавление

Текущие тесты в `tests/core/test_supervisor.py` жёстко пинят старый контракт «warm resume finalize» и «observe на каждом completed step» — их нужно переписать под fresh-from-packet и новый cadence.

- `test_supervisor_observes_each_completed_step` (`:158`) — переписать: `tool`/`checks` больше не наблюдаются; агент/эвалюатор — да.
- `test_finalize_warm_session_resumes_without_digest` (`:582`) — **инвертируется**: обычный finalize теперь НЕ резюмится на тёплую сессию, а идёт fresh из пакета.
- `test_finalize_reseeds_from_digest_when_session_not_live` (`:562`) — из revive-only становится обычным путём finalize.
- `test_finalize_digest_skips_failed_and_empty_notes` (`:600`), `test_finalize_digest_none_when_no_usable_observations` (`:612`) — остаются валидны, но digest теперь часть пакета (`material_observations`).
- **Новый:** пакет — чистая функция состояния: две сборки на одном `state.db` байтово равны (канонический JSON, repo-relative POSIX-пути).
- **Новый:** revive без переисполнения нод даёт тот же пакет; с переисполнением — отличается ровно на новые шаги.
- **Новый:** пакет содержит diff stats + путь к `current.diff` и bounded step-messages (не весь diff при большом размере).
- `tests/core/test_flow_engine.py` — post-node lifecycle: `tool`/`checks` не вызывает observe; прочие executed-ноды вызывают.

## Вероятные области реализации

- `src/wastech_orchestrator/core/supervisor.py` — `SupervisorPacket`, всегда-fresh finalize, сборка пакета из `_finalize_digest` + durable-фактов.
- `src/wastech_orchestrator/core/orchestrator.py` — условие пропуска `tool`/`checks` в post-node hook (`:3204`, текущее `node.kind != "publish"`); прокидывание фактов задачи (changed paths / diff / findings / checks) в `finalize`.
- `src/wastech_orchestrator/providers/base.py` — поле `supervisor_packet_path` в `AgentRunRequest` + строка `packet` в `build_context_footer` (P0-D1).
- `src/wastech_orchestrator/providers/exchange.py` (`:398`) и `providers/_adapter_base.py` (`:658`) — два места, где поля контекста перечислены явно (containment + audit): новое поле нужно добавить в оба, иначе путь либо не пройдёт проверку содержания, либо не попадёт в аудит.
- `src/wastech_orchestrator/core/flow/nodes/exchange_publish.py` — используется как есть (`publish_artifact`), менять не нужно.
- `tests/core/test_supervisor.py`, `tests/core/test_flow_engine.py` — см. выше.
- `src/wastech_orchestrator/packaged/guide/flows/roles.md` — единственный присутствующий на `dev` doc-файл, который описывает это поведение: cadence-фраза про «each step» (`:63`) и packet-first finalize. Derived `docs/worc_architecture.md` / `docs/configuration.md` на этой ветке отсутствуют — только doc-impact note в PR (X2).
