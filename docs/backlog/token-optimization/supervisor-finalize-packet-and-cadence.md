# Supervisor P0: детерминированный SupervisorPacket → fresh finalize → пропуск tool/checks

**Статус:** **implemented 2026-08-03** (accepted 2026-07-26, все развилки закрыты в «[Решения приёмки](#решения-приёмки-2026-07-26)»; метрики приёмки пересмотрены 2026-08-03 — отдельный baseline-прогон отменён, см. «[A/B и метрики](#ab-и-метрики-решение-x1-пересмотрено-2026-08-03)». Код и тесты в ветке `feat/supervisor-p0-packet-fresh-finalize`; количественные пороги снимаются оператором с первого прогона после мёржа, см. «[Что осталось за оператором](#что-осталось-за-оператором)») **Приоритет:** P0 (самый крупный и самый безопасный резерв экономии в content-pipeline) **Источник:** [2026-07-16 варианты оптимизации supervisor](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/analysis/2026-07-16-supervisor-token-optimization-options.md) (§7 SupervisorPacket, §8 P0, Варианты A/E/F). Смежные задачи: [normalized-usage-accounting.md](normalized-usage-accounting.md) (мерная подложка для A/B), [content-flow-token-hygiene.md](content-flow-token-hygiene.md).

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
- **P0.3 — затем пропуск наблюдений `tool`/`checks`.** Правка одного условия в post-node hook (`core/orchestrator.py:3176`, текущее условие `node.kind != "publish"`): `node.kind not in {"tool", "checks", "publish"}`. Безопасно: пропущенные/недетерминированные ноды и так не наблюдаются, а на стейт-машину cadence не влияет (advisory). Этот пропуск сам по себе НЕ обесточивает finalize даже без пакета — содержательные наблюдения (`revise`/`tone_style`/`polish`) продолжают идти, — поэтому он и не подпадает под требование «сначала пакет»; но по решению P0-D5 он едет в том же изменении, а не отдельным PR. В большинстве packaged-флоу таких нод по одной (`blog_article_revise` → `length`, `implementation` → `checks`), но не во всех — у `deep_research` их две (`checks`), уточнено 2026-08-02, — то есть минус один-два наблюдательных вызова за прогон (исторически ~44k input-токенов на `length`).

Дополнительно:

- **Наблюдения (кроме `tool`/`checks`) в P0 продолжают идти как сейчас.** Это переходное состояние: они по-прежнему кормят `material_observations` в пакете, поэтому качество `follow_ups`/`memory_delta` не проседает до того, как пакет будет проверен на A/B. Понижение cadence (`observe.mode: events | none`) — это P1, не P0.
- **Пакет ссылается на полные артефакты и содержит только bounded-выжимку.** Полный diff встраиваем только если он мал; иначе — changed paths + diff stat + путь к `current.diff` (§7 анализа).
- **Актуализация 2026-07-23 — пакет передаётся по пути к read-only копии в exchange, а не инлайн-JSON.** После этого анализа приземлился **WRI-011**: finalize уже читает задачу не инлайном, а из замороженного exchange-пакета через context-footer (`_finalize_prompt`, `core/supervisor.py:1281` — «the task reaches the turn as the frozen exchange packet … never inline title/body»), и `finalize` уже прокидывает `task_path`. Точный маршрут для `SupervisorPacket` зафиксирован ниже в решении P0-D1 — в частности, «тот же паттерн» означает тот же принцип (путь к редактированной read-only копии в agent-facing корне), а **не** тот же frozen-bundle.

## Решения приёмки (2026-07-26)

### P0-D1 — маршрут пакета: своё поле запроса + публикация в `.worc-io`

`finalize` получает пакет как путь к отредактированной read-only копии в agent-facing корне, отдельным именованным полем запроса:

1. Оркестратор пишет приватный авторитетный `packet.json` в supervisor-артефакты задачи (`node_run_dir(artifacts_root, task_id, "supervisor", 0)` — тот же namespacing, что у остальных supervisor-артефактов, где `0` — finalize-сентинел).
2. Публикует редактированную копию готовым сеамом: `publish_artifact(exchange_root, task_id, "supervisor/packet.json", content, extra_secrets=…, private_path=…)` (`core/flow/nodes/exchange_publish.py:117`) → `.worc-io/<task-id>/supervisor/packet.json`. `extra_secrets` передавать обязательно — тот же набор литералов, что использует редакция сохранённых промптов.
3. Новое поле `AgentRunRequest.supervisor_packet_path` + строка `packet` в `build_context_footer` (`providers/base.py:172-231`). Поля контекста перечислены явно ещё в двух местах, оба нужно дополнить: containment-проверка `providers/exchange.py:398` и audit-дикт `providers/_adapter_base.py:658`.
4. Путь передаётся в POSIX-форме (`as_posix()`), как и остальные exchange-пути.

**Почему не frozen instruction bundle.** `instruction-bundles/<task-id>/` (WRI-011) — снимок _входов_, замороженный на старте задачи, с composite `instruction_manifest_digest`, который сверяется перед переиспользованием provider-сессии (`core/flow/instruction_bundle.py:1-31`). Пакет рождается в конце прогона, поэтому попал бы туда только через исключение в manifest-контракте — цена выше, чем одно новое поле запроса.

**Почему не инлайн и не чужое поле.** Инлайн возвращает в промпт ровно те байты, ради сокращения которых затевался P0, и обходит единый redaction-seam. Переиспользование `plan_path`/`check_artifacts_path` ломает диагностику: в context-footer, в rendered-prompt и в prompt-audit пакет назывался бы планом или checks.

**Приватная копия авторитетна, опубликованная — расходуема (уточнение 2026-08-02).** С v32 схемы конфига `logging.clean_runs_on_success` по умолчанию `true`: успешная задача на терминальном переходе сносит свой подкаталог в `runs/` вместе с запечатанным exchange (`remove_task_runs`). Опубликованная копия пакета до пост-мортема, таким образом, не доживает, а приватный `packet.json` в артефактах задачи — доживает. Порядок операций это не ломает: finalize (`_engine_finalize`) — хук publish-ноды, он отрабатывает задолго до `seal_exchange` и до eviction'а, так что окно для `publish_artifact` открыто. Практическое следствие для разбора прогонов: источником всегда считать приватный файл, а не `.worc-io`-копию.

**Следствие:** P0-D6 (редакция пакета) закрыт этим же решением — публикация в exchange редактирует по построению, отдельного механизма не требуется; в критерии остаётся только «`extra_secrets` переданы».

### P0-D2 — детерминизм = чистая функция durable-состояния

«Пакет детерминирован» означает: **сборка пакета — чистая функция `state.db` + артефактов задачи**, без каких-либо иных входов. Ни системных часов, ни env, ни абсолютных путей, ни порядка обхода файловой системы: все пути в пакете — repo-relative POSIX, шаги упорядочены по `node_runs.id`, сериализация каноническая (`json.dumps(..., sort_keys=True)`, запись с `newline=""` — тот же домашний паттерн, что у manifest'ов, `core/flow/exchange_seal.py:182`).

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
5. Синхронизация доков, которые физически есть на `dev` (решение X2, 2026-07-26): `src/wastech_orchestrator/packaged/guide/flows/roles.md` утверждает, что supervisor «observes **each step** and writes the final summary» — после P0 это неверно, фразу нужно переписать под «наблюдаются исполненные ноды, кроме `tool`/`checks`/`publish`» и под packet-first finalize. Схема config в P0 не меняется, поэтому `packaged/config.example.yaml` не трогаем. Derived `docs/` на `dev` не существует — вместо правки в описании PR оставляем строку doc-impact («затронуты finalize + cadence supervisor; вероятно влияет на `worc_architecture.md` и `configuration.md`») как хлебную крошку для реверс-инжиниринга на `main`.

**Фактический объём doc-sync (2026-08-03).** X2 назвал одну строку в одном файле; в реализации затронуто шесть, потому что то же утверждение продублировано в нескольких shipped-доках и — что важнее — в самих packaged role-промптах, которые после P0 стали говорить о сессии, которой у finalize больше нет: `guide/flows/roles.md` (фраза про cadence + новый абзац про пакет), `guide/flows/README.md` (та же фраза + `finalize_role_file`), `guide/config/reference.md` (описание слоя + абзац «что ограничивает стоимость» — раздел про сам ключ не менялся, схема осталась той же), `guide/footprint.md` (состав seal), `flows/roles/supervisor.md` и `flows/implementation/{supervisor,summary}.md` (cadence + «читай пакет, а не память сессии»). Флоу-локальные content-линзы (`blog_article_revise/summary.md`, `deep_research/summary.md`) не правились: они не опираются на память сессии, а инструкция про пакет добавляется в промпт кодом для любой линзы.

Ожидаемый эффект на исследованном прогоне (историческая оценка 2026-07-16; код с тех пор менялся, поэтому эти числа — ориентир, а не порог): пропуск `length` снимает минимум 44 107 input-токенов; fresh finalize из компактного пакета ориентировочно уменьшает финальный вызов (тогда — 104 567 input-токенов) на 65–85 тыс. Проверяется нормированной долей плюс структурным инвариантом — см. «A/B и метрики» ниже.

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
- [ ] Полнота summary: все четыре пункта на месте (что изменено / почему / какие проверки прошли / какие caveats) — проверяется по самому summary, без before-прогона; если прогон на актуальном `dev` уже есть, дополнительно сравнивается с его summary.
- [ ] Структурный инвариант (см. «[A/B и метрики](#ab-и-метрики-решение-x1-пересмотрено-2026-08-03)» ниже): supervisor-вызовов ровно столько, сколько исполненных нод кроме `tool`/`checks`/`publish`, плюс один finalize.
- [ ] Вход finalize-вызова (последний supervisor-ряд) — главный количественный порог P0: он укладывается в «role_file + промпт + пакет ≤ 16 КБ» (исторически было 104 567 токенов) и **не растёт с числом rework-циклов**.
- [ ] Средний observe на вызов относительно before не вырос (контрольная метрика: P0 не должен трогать наблюдения, кроме капа 500 на `final_message`).
- [ ] Доля supervisor в Claude-input прогона зафиксирована как контрольное число (ожидается ~60–65% против исторических ~70% — P0 не снимает наблюдения, поэтому доля почти не двигается **by design**; её порог — критерий [P1](supervisor-observation-cadence-p1.md), не P0), при 0 пропущенных blocking-issue (их держит `tone_style`).
- [ ] Supervisor остаётся read-only и advisory; handoff и skill-proposal работают независимо от изменений.

## A/B и метрики (решение X1, пересмотрено 2026-08-03)

Абсолютные пороги из исходного анализа (`< 60 000` при baseline 480 293) непроверяемы: те числа получены на коде, которого больше нет — с тех пор приземлились WRI-011 (finalize читает задачу из замороженного exchange-пакета) и content-flow-token-hygiene (packaged-дефолты `blog_article_revise`, `polish` → `fresh_disposable`).

**Пересмотр 2026-08-03: отдельный baseline-прогон до P0 не делается.** Исходное решение X1 требовало прогнать один `blog_article_revise` на текущем `dev` и сравнивать с ним суммарный supervisor input. Оба основания этого требования не выдержали проверки:

- **Повтор на уже обработанной статье занижает baseline.** `blog_article_revise` на отревизованном тексте выполняет меньше работы: `revise` находит меньше правок, `tone_style` с большой вероятностью принимает с первого раза вместо `rework`, второй `polish` не запускается. Меньше исполненных нод → меньше observe-вызовов → baseline занижен, и сравнение с ним врёт. Взять «такую же, но нетронутую» статью не спасает: это уже другой вход, а не baseline того же прогона.
- **Дисперсия суммарного total между прогонами — того же порядка, что измеряемый эффект.** Главный её источник — число rework-циклов: один лишний `tone_style → rework` добавляет node-run, ещё один observe-вызов и удлиняет editing lineage, из которого растёт вход последующих turn'ов.

Вывод: негодной была не точка замера, а метрика — суммарный абсолютный total не годится ни до, ни после. Порог заменяется на **структурный инвариант плюс вход одного finalize-ряда**, а нормированная доля пишется как контрольное число и становится порогом только в P1. Всё это читается с одного прогона после P0, на новой статье. Нового кода по-прежнему не нужно — VF-8 (DB v19) анкорит попытки по `task_id`, а ряды постоянного supervisor-слоя это ровно `node_run_id IS NULL` (он не нода графа; `state_store.py:103-108`, DDL `:298-329`), нормализованные колонки появились в v16.

**Важно, какая метрика чей порог.** P0 наблюдения не снимает (это P1), поэтому доля supervisor в нём почти не двигается, и требовать от P0 её падения — значит завалить работающий P0. Арифметика на исторических числах: наблюдений было 375 726 при общем Claude-input прогона ~686 тыс. (480 293 / 0,70), то есть не-supervisor часть ~206 тыс. После P0 уходит только `length` (−44 107) и сжимается finalize (104 567 → ~20–40 тыс.), так что supervisor ≈ 360 тыс. из ≈ 566 тыс. — **доля ~64%**. Реальный порог P0 — это finalize-ряд. Доля падает до однозначных-низких двузначных процентов только в P1, когда наблюдений не остаётся вовсе.

### 1. Структурный инвариант — без A/B и без дисперсии

Пропуск `tool`/`checks` — тождество, а не статистика: вызовов supervisor'а должно быть ровно столько, сколько исполненных нод кроме `tool`/`checks`/`publish`, плюс один finalize.

```sql
-- сколько вызовов сделал supervisor
SELECT COUNT(*) FROM provider_attempts WHERE task_id = ? AND node_run_id IS NULL;
-- сколько их должно быть
SELECT COUNT(*) + 1 FROM node_runs
WHERE task_id = ? AND node_kind NOT IN ('tool', 'checks', 'publish') AND skipped = 0;
```

Не сошлось — баг в условии post-node hook. Baseline для этого не нужен вовсе; заодно на любом прошлом прогоне видно, сколько вызовов пропуск снял бы: `COUNT(*) FROM node_runs WHERE task_id = ? AND node_kind IN ('tool','checks')`.

**Правка запросов 2026-08-03 (при реализации).** В первой редакции обе колонки были названы неверно и запрос молча возвращал бы 1: колонка называется `node_kind`, а не `kind`, и **значения `status = 'completed'` у `node_runs` не существует** — статус там per-kind (`succeeded` у agent/evaluator, `passed`/`incomplete`/`dirtied_working_tree` у checks, `published` у publish, свой набор у tool). Исполненность выражается через `skipped = 0`, что и закреплено тестом `test_supervisor_layer_observes_the_interpretive_steps_and_writes_one_summary`.

### 2. Доля supervisor в Claude-input — самонормирующаяся метрика (порог в P1, контрольное число в P0)

Замена «−60% от абсолюта». Доля нормирована на объём работы прогона, поэтому лишний rework-цикл её почти не двигает — он растит и числитель, и знаменатель, и в этом её ценность против суммарного total. Историческая доля известна и к смене кода устойчивее абсолюта: **~70%**. В P0 записывается как контрольное число (ожидание ~60–65%, см. арифметику выше), порогом становится в P1.

```sql
SELECT
  SUM(CASE WHEN node_run_id IS NULL THEN usage_input_total ELSE 0 END) AS supervisor_input,
  SUM(usage_input_total) AS task_input
FROM provider_attempts WHERE task_id = ?;
```

### 3. Finalize-вызов — один ряд, сравнимый между статьями

Главное число P0 (исторически 104 567 из 480 293) — это **один** provider-вызов, и после P0 его вход почти детерминирован: role_file + промпт + пакет ≤ 16 КБ (решение P0-D3). Поэтому он осмысленно сравнивается даже между разными статьями — в отличие от суммарного total. Различить finalize без нового кода можно по порядку: это последняя supervisor-строка задачи.

```sql
SELECT usage_input_total, usage_cost FROM provider_attempts
WHERE task_id = ? AND node_run_id IS NULL ORDER BY id DESC LIMIT 1;
```

Опираться вместо этого на `attempt_dir LIKE '%run-000000%'` (finalize-сентинел в пути) не нужно: это семантика из магического числа в строке пути — ровно то, что решение P2-D3 отвергло. Явный ярлык функции у ряда появится в [P2](supervisor-responsibility-split-p2.md) (nullable колонка `function`), после чего запрос станет прямым.

### 4. Средний observe на вызов — контрольная метрика

В P0 меняться почти не должен: единственное влияние — кап 500 симв. на `final_message` (решение P0-D3). Заметный сдвиг означает, что изменилось что-то помимо заявленного. Считается из тех же рядов: `supervisor_input` минус finalize-ряд, делённое на `COUNT(*) - 1`.

### Если before-прогон уже есть — использовать его, а не запускать новый

Ряды `provider_attempts` не удаляются никогда: ретенция трогает только `runs/` (`runs_retention.py`) и `logs/` (`worc logs clean`), а все `DELETE FROM` в `state_store.py` — idempotency-сбросы под `rerun`, не ретенция. Поэтому любой уже существующий прогон `blog_article_revise` на актуальном `dev` — бесплатный before, и специально запускать ничего не нужно:

```bash
sqlite3 .worc/state.db "SELECT task_id, COUNT(*), SUM(usage_input_total) FROM provider_attempts WHERE node_run_id IS NULL GROUP BY task_id ORDER BY MIN(id) DESC LIMIT 10;"
```

Операторскую поверхность чтения (`worc usage` / блок в `worc status`) в P0 **не** делаем: данные уже есть, а вывод — это пункты 3–4 из [P2](supervisor-responsibility-split-p2.md) (per-function usage + supervisor-отчёт в summary). Ридер `get_provider_attempts_for_task` существует, но сегодня используется только в тестах.

### Что осталось за оператором

Всё, что проверяется без прогона, закрыто кодом и тестами. Три критерия по своей природе требуют реального прогона и снимаются с **первого** `blog_article_revise` после мёржа, на новой статье:

1. вход finalize-ряда (главный количественный порог — «≤ 16 КБ входа, не растёт с rework») — запрос из §3 выше;
2. доля supervisor в Claude-input как контрольное число — запрос из §2;
3. средний observe на вызов — §4.

Структурный инвариант (§1) в прогоне проверять не нужно: он закреплён тестом `test_supervisor_layer_observes_the_interpretive_steps_and_writes_one_summary` в `tests/core/test_orchestrator.py`, который считает то же тождество на настоящем прогоне флоу с фейковыми провайдерами.

## Отклонения от текста задачи (реализация 2026-08-03)

Три места, где реализация сознательно расходится с формулировками выше. Каждое — не упущение, а решение, принятое при сверке с кодом.

- **`flow.final_status` в пакет не попал.** «В объёме P0» перечисляет `flow {name, final_status}`, но `finalize` вызывается ровно из одного места — publish-хука success-пути (`_engine_finalize`), — поэтому поле было бы константой `done`: мёртвое поле, которое читатель пакета принял бы за живой признак. В пакете остался `flow {name}`; `task {id,title,type}` — как в задаче.
- **`steps[].message` берётся из durable `<node_id>.out.md`, а не из записи наблюдения.** Соблазнительный вариант — писать bounded `final_message` в payload `supervisor_step` при observe (данные уже под рукой). Он отравлен для дорожной карты: в [P1](supervisor-observation-cadence-p1.md) при `observe.mode: none` строк `supervisor_step` не будет вовсе, и пакет остался бы одним скелетом «нода → исход» — то есть ровно то, ради чего P1 требует P0, перестало бы работать. Источник, не зависящий от cadence, — собственный durable-вывод ноды. Следствие, которое надо знать: у слот-нод (`output_artifact`: plan/diff/report/summary) `.out.md` не пишется, поэтому у них поля `message` нет — их продукт и так лежит в пакете как `changes`/`findings_path`.
- **Добавлен узкий ридер `StateStore.get_check_runs(task_id)`** (schema не менялась) под поле `checks {passed, failed, skipped}`. Без него «какие проверки прошли» просело бы именно там, где P0.3 снял наблюдение checks-ноды. `skipped` — отдельный список, а не часть `failed`: проверка с отсутствующим тулчейном не падала, и summary не должен утверждать обратное.

## Вне объёма P0 (следующие фазы)

- `observe.mode: all | selected | events | none`, event-триггеры, flow-local narrowing, раздельные `observe`/`finalize` model+reasoning — **[P1](supervisor-observation-cadence-p1.md)** (Варианты B/C/D/H/I). Для content-flow — `none`, для implementation — `events` (там `emit_follow_ups: true`, `none` просадил бы follow_ups/память).
- Разделение монолита на `StepRecorder`/`ObservationAdvisor`/`TaskFinalizer`/`SubtaskHandoff`/`SkillProposer` и per-function telemetry — **[P2](supervisor-responsibility-split-p2.md)** (§6, §8 P2).
- Полностью deterministic summary без LLM — опционально (Вариант G), не default.
- Изменения handoff/skill-proposer — не входят в P0.

## Тесты (реализовано)

Старые тесты в `tests/core/test_supervisor.py` пинили снятый контракт «warm resume finalize»; они переписаны под fresh-from-packet.

- `test_finalize_warm_session_resumes_without_digest` → **`test_finalize_runs_fresh_from_the_packet_even_with_a_live_session`**: инвертирован — живая сессия НЕ резюмится, запрос несёт `supervisor_packet_path`, инлайн-digest в промпте отсутствует.
- `test_finalize_reseeds_from_digest_when_session_not_live` → **`test_finalize_packet_carries_the_observation_digest`**: из revive-only стал обычным путём; digest проверяется в `material_observations` файла пакета, а не в тексте промпта.
- `test_supervisor_observes_each_completed_step` → **`test_supervisor_records_one_advisory_row_per_observed_step`**: cadence проверяется не здесь (метод `observe` безусловен) — переименован и снабжён ссылкой на orchestrator-тест, который решает, какие ноды до него доходят.
- `test_finalize_digest_skips_failed_and_empty_notes`, `test_finalize_digest_none_when_no_usable_observations` — валидны; сигнатура `_finalize_digest` принимает уже прочитанные ряды (finalize читал `get_evaluations` дважды).
- **Новые в `test_supervisor.py`:** пакет — чистая функция состояния (две сборки байтово равны, JSON канонический); пути внутри repo-relative POSIX; revive без переисполнения даёт тот же пакет, с переисполнением отличается ровно на новый шаг; малый diff встраивается, большой — только stats + путь; `steps[].message` обрезан на 500; digest обрезан на 8 000 с маркером и выкидывает **старые** строки; `checks` разложен на passed/failed/skipped; fallback/retry-факты сохранены; `findings_path` указывает на published `findings.json`; публикация редактирует секрет и оставляет приватную копию; сбой сборки не ломает finalize (`packet_built: false`); кап 500 на `final_message` в observe-промпте.
- **`tests/core/test_orchestrator.py`:** `test_supervisor_layer_observes_each_step_and_writes_one_summary` → `…_observes_the_interpretive_steps_and_writes_one_summary` — `testing` (checks) и `publish` не наблюдаются, плюс структурный инвариант (§1 метрик) на настоящем прогоне флоу.
- **`tests/providers/`:** footer рендерит `packet: …` (`test_claude_command.py`); `supervisor_packet_path` вне exchange падает закрыто (`test_exchange.py`) — поле, забытое в containment-кортеже, не проверяется молча.

Отдельный тест в `tests/core/test_flow_engine.py` не добавлялся: пропуск живёт не в движке, а в orchestrator-хуке (`_engine_post_node`), и проверяется там же, где решение принимается.

## Затронутые файлы (реализовано)

- **`src/wastech_orchestrator/core/supervisor_packet.py` — новый модуль:** `PacketFacts` + `render_packet` (чистая функция, канонический JSON), границы P0-D3 как именованные константы, `bound_step_message` — единственный источник капа 500 для пакета и observe-промпта, парсер `current.diff` для changed paths и diff stat.
- `src/wastech_orchestrator/core/supervisor.py` — `_publish_packet` / `_build_packet` / `_step_messages` / `_exchange_relpath` / `_findings_relpath`; всегда-fresh finalize; удалены ветка `warm` и флаг `_session_live`; `packet_built` вместо ставшего константой `recovered_from_digest`; секция промпта «Run facts (the packet)» вместо инлайн-digest (секция `gates` осталась инлайн — она ограничена числом evaluator-нод и это анти-галлюцинационный гард).
- `src/wastech_orchestrator/core/orchestrator.py` — `_UNOBSERVED_NODE_KINDS = {tool, checks, publish}` в условии post-node hook; `flow_name`/`task_type` в `_build_supervisor` (сигнатура `_engine_finalize` не менялась — оба факта известны при конструировании слоя).
- `src/wastech_orchestrator/state_store.py` — ридер `get_check_runs` + маппер `_check_run_from_row`.
- `src/wastech_orchestrator/providers/base.py` (поле + строка footer), `providers/exchange.py` (containment), `providers/_adapter_base.py` (audit) — те самые четыре места; `core/flow/nodes/exchange_publish.py` использован как есть.
- Доки — см. «Фактический объём doc-sync» выше. Derived `docs/worc_architecture.md` / `docs/configuration.md` на `dev` отсутствуют: только строка doc-impact в описании PR (X2).
