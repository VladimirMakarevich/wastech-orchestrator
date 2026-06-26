# Разбор прогона задачи td-be-003-conform-m1-m2-contract-shapes (Windows 10) и план улучшений

STATUS: SOLVED — `final_status=done`, PR `argudebate#5` (после прогона **смержен оператором вручную**, не оркестратором). Это первый сквозной прогон оркестратора на Windows 10 на реальном проекте `argudebate` (раньше проверяли только на macOS).

Анализируемый прогон: `td-be-003-conform-m1-m2-contract-shapes` в репозитории `argudebate` (`I:/github/argudebate`), попытка **1** (не rerun), флоу `implementation_backend` (операторский, в `<target>/.worc/flows/`, без packaged-дефолта). Задача — бэкенд (.NET / `Chat1o1.*`): привести shapes уже реализованных M1/M2 REST-эндпоинтов (auth, conversations, messages) к design-first canon `mobile/openapi/swagger.json` — по paths, status codes, request/response shapes и property casing, атомарно через `/contract-change`.

## Короткий вывод

Сам прогон прошёл чисто и качественно: **ноль кругов исправлений** (`fix_iterations=0`), ноль fallback'ов, ноль крашей, все 5 узловых попыток провайдера завершились с первого раза. Дифф большой (35 файлов, +1242/−234), аккуратно расслоён по проектам бэкенда (Contracts → Domain → Infrastructure/Migration → Application → Tests → Docs), а `review` дал острый, предметный разбор (поймал PascalCase-сериализацию enum, деструктивную миграцию, отсутствующий null-guard). Планирование на opus/xhigh выдало по-настоящему сильный анализ скоупа. Для Windows это успех: прогон отработал end-to-end без проблем с путями/процессами, Windows-allowlist окружения сработал (Node-овые `claude.exe`/`codex` не падали с `0xC0000409`).

Но вердикт **`done` переоценивает завершённость именно для этой задачи — по двум причинам**, и обе системные, не разовые:

1. **Оркестратор не прогнал ни одной проверки.** `checks.command_sets` пуст (`{}`), поэтому узел `testing` — «пустой проход» (`check_runs`=0). Явный критерий приёмки «`dotnet build src/Chat1o1.sln` зелёный» оркестратором **не проверялся**: за «зелёным» стоят только самостоятельный прогон `dotnet build` агентом внутри сессии и чтение LLM-ревью, но не гейт оркестратора.
2. **Кросс-стек задача отправлена в backend-only флоу.** Задача по своей природе кросс-стек (атомарный `/contract-change`, включая `mobile/openapi/swagger.json` + регенерацию `@app/api-client`), но `task_type: implementation_backend` выбирает флоу, чья роль реализации **прямо запрещает трогать `mobile/`** и велит лишь пометить регенерацию api-client как follow-up. В итоге критерий приёмки №6 (атомарный кросс-стек) структурно невыполним; дифф — чисто бэкендный (`mobile/` не тронут), а `review` пометил это как **medium, не блокирующим**.

**🔴 КРИТИЧНО (директива оператора, наивысший приоритет).** HITL **пересоздаёт сессию агента вместо возобновления**: после ответа оператора узел `planning` запустился во **второй, новой** сессии (`session:c8162f4f92ff` → `session:c5d799a3aef0`), переиграв план с нуля, а ответ получил отдельным файлом-контекстом, а не как продолжение диалога. Требование: **HITL никогда не должен пересоздавать сессию агента — он обязан её возобновлять. Независимо от того, в каком узле и когда сработал HITL, после общения с оператором сессия ОБЯЗАНА продолжаться ровно с того момента, на котором был сделан запрос в Telegram.** Это критическая находка №0 (см. ниже); рычаг — `core/flow/nodes/agent.py` (HITL-реинвок не прокидывает `session_id` первого запуска).

Прочие важные рычаги: **дать узлу `testing` настоящий command_set (`dotnet build …`)**, чтобы `done` означал «скомпилировалось у оркестратора», а не «агент так сказал»; и развести кросс-стек задачу и backend-only флоу (рычаг — `task_type`/флоу или формулировка критерия №6).

## Как прошёл прогон (факты)

- **Истинный итог (не по exit-code фоновой задачи):** `worc status` → `status=done`; ledger `completed.jsonl` → `final_status=done`, `pr_url=…/pull/5`, `fix_iterations=0`, `auto_merged=false`, `decomposed=false`, `attempt=1`, `finished_at=2026-06-26T17:25:56Z`. `state.db.tasks`: `test_fix_cycles=0`, `review_fix_cycles=0`, `fix_iterations=0`, `decomposition_enabled=0`.
- **Путь по флоу (фактический):** `refinement(пропуск)` → `planning` → **`planning` (ПОВТОРНО, из-за HITL-approval)** → `implementation` → `testing(пустой проход)` → `review(accept)` → `documentation` → `publish`. Супервайзер-слой наблюдал каждый завершённый шаг (6 запусков). В `node_runs`/`provider_attempts` всё чисто: `stage_attempts=1` у агентных узлов, `route_fallback=NULL`, `error_class=NULL` везде.
- **`refinement` пропущен** детерминированно (`skip_reason: "deterministic skip: when derived.needs_refinement != True"`) — валидация дала `completeness=complete`, задача хорошо специфицирована.
- **Модели/reasoning (из `request.json`):** `planning` — `claude-opus-4-8 / xhigh` (закреплено в флоу), `implementation` — `claude-sonnet-4-6 / max` (дефолт провайдера), `review` — `claude-sonnet-4-6 / max`, `documentation` — `claude-sonnet-4-6 / medium` (reasoning закреплён в флоу), `supervisor` — `claude-opus-4-8 / medium`. Права узлов верны: `planning`/`review`/`supervisor` read-only, `implementation`/`documentation` workspace-write.
- **HITL:** узел `planning` выпустил запрос **approval** (risk=dependency) с отличным разбором границы скоупа (4 пункта: EF-миграция под canon-поля conversation, jsonb-хранение `StructuredMessage`, удаление `deviceId`, судьба backend-extra эндпоинтов). Ответ — `approved` через Telegram (`message_id=28`, `hitl/planning.json`), пришёл за ~2.5 мин. Из-за этого `planning` выполнился **дважды** (run-000002 спросил → run-000003 применил ответ, `fresh_disposable` → полный реплан с нуля).
- **Циклы исправлений:** 0. Узлы `fixing` не запускались. `review` принял с первой попытки (`evaluations.in_flow_verdict=accept`, `review/findings.json={"findings": []}`).
- **Токены / стоимость:** деньги = **0** (`total_cost_usd=null` везде — подписка/OAuth). Суммарно ≈ **32.6k вход / 185k выход**, кэш-чтения огромные (≈9.5M; `implementation` один прочитал 4.75M из кэша). Выход доминируют `implementation` (65.5k), `review` (38.3k) и **`planning` (≈67k за два запуска)** — HITL-реплан удвоил выход планирования (+≈34k) и время (+≈531 c).
- **Wall-time ≈ 62 мин** (локально 18:24:04 → 19:25:57, UTC+2). Раскладка: planning #1 555 c, ожидание approval ~2.5 мин, planning #2 532 c, implementation **1442 c (≈24 мин)**, review **738 c (≈12 мин)**, documentation 128 c, супервайзеры ≈150 c, finalize+publish ≈50 c.
- **Дифф (реальный, `master..branch`): 35 файлов, +1242/−234.** Contracts (`CommandEnvelope`, `StructuredMessage`, `MessageDto`, переименованные auth-типы, новый conversation request/response), Domain (`InvitationPending`, `Topic/Context/Goal/AccessCode`, `Message.PlainText` + `StructuredMessageContent`), Infrastructure + миграция `20260626170838_ConformM1M2ContractShapes` (jsonb через `OwnsOne().ToJson()`), Application handlers, интеграционные тесты, доки (`techdebt.md`, `PROGRESS.md`). **`mobile/openapi/swagger.json` и `@app/api-client` — НЕ тронуты** (`git diff --name-only master..branch -- mobile/` пуст).

## Разбор находок (по убыванию влияния)

### 0. 🔴 КРИТИЧНО (директива оператора): HITL пересоздаёт сессию агента вместо возобновления — ПОДТВЕРЖДЕНО

- **Категория:** flow / HITL / архитектура сессий. **Серьёзность:** КРИТИЧЕСКАЯ. **Уверенность:** высокая (доказано идентификаторами сессий и argv).
- **Требование (директива оператора).** HITL **никогда не должен пересоздавать сессию агента** — он обязан её **возобновлять**. Неважно, в каком узле и когда оператор был вызван: **ВСЕГДА после общения с пользователем сессия должна продолжаться ровно с того момента, на котором был сделан запрос в Telegram** (агент должен «проснуться» в той же сессии с ответом оператора, а не начать новую с нуля + файл-контекст).
- **Доказательство.** Узел `planning` выполнился дважды с **разными** идентификаторами сессий: `result.session_id` run-000002 = `session:c8162f4f92ff`, run-000003 = `session:c5d799a3aef0`. Ни в одном `request.json.argv` нет `--resume`/`--continue`/`--session`. Отличие входа второго запуска — только добавленный `context_paths: [task_path, human_input_path]`, то есть ответ оператора пришёл **файлом**, а не продолжением диалога. Итог: полный реплан (≈34k выходных токенов, ≈532 c продублировано), и агент потерял весь рабочий контекст первой сессии (зачем спрашивал, что уже прочитал/решил).
- **Корневая причина (точный рычаг, file:line).** `src/wastech_orchestrator/core/flow/nodes/agent.py`:
  - `_run_with_hitl` после получения ответа делает второй прогон `self._invoke(node, ctx, route, human_input_path=str(path))` (**`agent.py:132`**) — но НЕ передаёт `session_id` первого прогона.
  - `_build_request` ставит `session_id=self._resume_session_id(node, ctx, route)` (**`agent.py:441`**).
  - `_resume_session_id` (**`agent.py:491-508`**) **возвращает `None` для всех, кроме `SessionScope.EDITING_LINEAGE`** (`agent.py:503`). `planning`/`refinement` (`fresh_disposable`) → `None` → свежая сессия.
  - Тот же дефект в `_reconsider` (**`agent.py:373`**, реинвок после отказа в dangerous-diff approval) и в `_resume_interaction` после рестарта (`agent.py:138-149`).
  - **Механизм resume уже существует** и работает: так `implementation`→`fixing` продолжают сессию через `AgentRunRequest.session_id` → `--resume` в `providers/claude.py` (см. `_persist_session`/`get_editing_lineage`). HITL-реинвок просто его не использует для не-`editing_lineage` узлов.
- **Рекомендуемая правка (рычаг).** В `agent.py`: захватывать `session_id` первого прогона (`outcome.result.session_id`) в `_run_with_hitl`/`_reconsider` и прокидывать его во второй `_invoke` → `_build_request` → `AgentRunRequest.session_id`, **в обход** ограничения `_resume_session_id` «только editing_lineage» для случая HITL-реинвока. То есть resume сессии при HITL должен работать для ЛЮБОГО `session_scope`, а не только editing_lineage. Учесть провайдерскую сторону (`providers/claude.py`, `_adapter_base.py`): resume по `session_id` уже поддержан; убедиться, что `codex` так же резюмируется (нельзя резюмировать claude-сессию на codex — gate по провайдеру, как в `_resume_session_id:506`). Случай после рестарта процесса (сессия CLI могла исчезнуть) — отдельная под-задача: пытаться resume, при неуспехе — честный фоллбэк, но это не отменяет требование для нормального (в рамках одного процесса) HITL.
- **Зона:** дефолт оркестратора (на все репозитории и все узлы с HITL). **Ожидаемый эффект:** после ответа оператора агент продолжает ту же сессию с точки запроса — нет повторного реплана, не теряется рабочий контекст, экономятся токены и время; поведение HITL становится тем, что и ожидает оператор.

### 1. Узел `testing` — «пустой проход»: оркестратор не компилирует и не тестирует — ПОДТВЕРЖДЕНО

- **Категория:** config / checks. **Серьёзность:** высокая. **Уверенность:** высокая.
- **Доказательство.** `config.yaml` → `checks.command_sets: {}`; preflight → `checks: no command_sets configured (no quality gate)`; `state.db` → `select count(*) from check_runs` = **0**; `node_runs` строка `testing`: `status=passed, outcome=pass, stage_attempts=0`. `summary.md` (caveat 2): «Test "pass" unverified as a real execution… the `testing` node reported a bare "pass" with no log or count». Критерий приёмки №7 — «`dotnet build src/Chat1o1.sln` зелёный» — объективно проверяем, но оркестратор его не выполнял; критерий №6 (интеграционные тесты обновлены/прогнаны) — тоже. (NB: агент `dotnet build` всё же сам гонял — в `events.jsonl` есть «Build is clean — 0 errors, 0 warnings» после восстановления от транзиентного `dotnet restore`-сбоя; но это self-report внутри сессии, а не гейт оркестратора.)
- **Корневая причина.** Узел `testing` (`kind: checks`, `checker: command_profile`) выбирает наборы из `checks.command_sets`; набор пуст → выбирать нечего → vacuous pass. Гейт качества прогона = только LLM-`review`, без компилятора и тестов.
- **Рекомендуемая правка (рычаг).** Заполнить `checks.command_sets` в `I:/github/argudebate/.worc/config.yaml` профилем с `dotnet build src/Chat1o1.sln` (рабочая директория `backend/`), привязанным к путям `backend/**`; при наличии Docker — Docker-gated профиль `dotnet test`. Схема/дефолты: `src/wastech_orchestrator/config/schema.py`, `config/loader.py`, документация `docs/configuration.md` (раздел про `checks.command_sets`). Поскольку задача сама допускает непрогон `dotnet test` (Docker), минимально нужен хотя бы `dotnet build`.
- **Зона:** target (конфиг этого репо). **Ожидаемый эффект:** `done` сертифицирует реальную компиляцию; критерий №7 выполняется объективно, а не со слов агента.

### 2. Кросс-стек задача отправлена в backend-only флоу → критерий приёмки №6 структурно невыполним — ПОДТВЕРЖДЕНО

- **Категория:** flow / spec. **Серьёзность:** высокая. **Уверенность:** высокая.
- **Доказательство.** Критерий приёмки №6: «атомарно через `/contract-change`: `Chat1o1.Contracts` + handlers (+EF) + `mobile/openapi/swagger.json` + регенерированный `@app/api-client` в одном изменении». Флоу `I:/github/argudebate/.worc/flows/implementation_backend.yaml` → `name: implementation_backend`, backend-only. Роль реализации (`rendered-prompt.md`, строки 3 и 15, дословно): «Work only under `backend/src`… do not touch `mobile/`» и «This flow is backend-only… regenerate `@app/api-client`… **as a follow-up marker… even though you do not implement it here**». Дифф: 35 файлов, все под `backend/` + `tasks/` (`mobile/` пуст). `review/summary.md` M2: «`api-client` regeneration not evidenced in the diff… acceptance criterion requires regeneration in the same change» — но это помечено **MEDIUM, не блокирующим**. `summary.md` стр. 20: «`@app/api-client` is regenerated… (deliberately out of this PR's scope)».
- **Корневая причина.** `task_type: implementation_backend` выбирает флоу, чья роль запрещает мобильную половину; задача по сути кросс-стек (корневая команда `/contract-change` в самом оркестраторе описана как «backend Contracts + EF + handlers, **then** mobile OpenAPI + api-client + model»). Отдельно — внутренний конфликт самой задачи: constraint «`swagger.json` — источник истины, backend конформит К нему, swagger остаётся фиксированным» против критерия №6 «swagger.json + регенерированный api-client в одном изменении». Если swagger не меняется, регенерация api-client — no-op, и атомарный критерий повисает.
- **Рекомендуемая правка (рычаг).** Либо (a) маршрутизировать такие задачи в кросс-стек флоу: завести `task_type: contract_change` со своей ролью реализации, разрешающей `mobile/openapi/**` + `@app/api-client` (флоу-файл `<target>/.worc/flows/contract_change.yaml` + роль `roles/.../implementation.md`); либо (b) согласовать саму задачу — критерий №6 переформулировать как «backend конформит к неизменному canon; регенерация api-client — отдельный follow-up», тогда `done` честен. Зеркальный апстрим: реестр флоу `core/flow/registry.py` (резолв `task_type`→флоу) и инвариант «ядро не знает CLI-синтаксис» (мобильная регенерация — это команда `/contract-change` в target-репо, а не логика ядра).
- **Зона:** target (флоу/роль или формулировка задачи). **Ожидаемый эффект:** либо прогон реально делает мобильную половину, либо критерий приёмки приведён в соответствие — `done` перестаёт скрывать невыполненный пункт.

### 3. `review` принял прогон с заведомо невыполненным критерием приёмки и деструктивной миграцией — ПОДТВЕРЖДЕНО

- **Категория:** checks / evaluator. **Серьёзность:** средне-высокая. **Уверенность:** высокая.
- **Доказательство.** `review/summary.md`: **M1** «Destructive migration without data backfill — `Up()` drops `messages.body` (NOT NULL) without first copying to `messages.plain_text`. Any existing rows silently lose their content»; **M2** «api-client regeneration not evidenced… acceptance criterion requires regeneration in the same change». При этом `review/findings.json = {"findings": []}` и `evaluations.in_flow_verdict = accept` — то есть ни M1, ни M2 не сделаны блокирующими. Роль `review` (`roles/implementation_backend/review.md`, стр. 11) прямо требует: «a contract change that skipped `/contract-change` (backend changed without `swagger.json`) is **blocking**».
- **Корневая причина.** Поскольку _флоу_ сказал агенту, что `mobile/` вне скоупа (находка 2), `review` интерпретировал отсутствующую мобильную половину как medium, а не как «contract change skipped `/contract-change`» (что по его же чеклисту блокирующее). Деструктивную миграцию роль не классифицирует как блокирующую («fine for a dev-only reset»). Итог: планка блокировки не поймала ни невыполненный критерий, ни потенциальную потерю данных.
- **Рекомендуемая правка (рычаг).** В роли `roles/implementation_backend/review.md`: явно сделать блокирующими (a) «критерий приёмки не продемонстрирован/не выполнен» и (b) «деструктивная миграция без backfill (`DROP`/`NOT NULL` по колонке с данными)». Чище — закрыть это апстримом через находку 2 (когда флоу/задача согласованы, противоречие в роли исчезает).
- **Зона:** target (роль `review`; при желании — packaged-дефолт роли). **Ожидаемый эффект:** `review` блокирует на невыполненных критериях и потере данных, а не пропускает их «адвайзори».

### 4. `planning` эскалировал approval в Telegram → прогон неавтономен; HITL переигрывает планирование с нуля — ПОДТВЕРЖДЕНО

- **Категория:** HITL / flow / эффективность. **Серьёзность:** средняя. **Уверенность:** высокая.
- **Доказательство.** `hitl/planning.json`: `kind=approval`, `risk=dependency`, `answer=approved`, `telegram_message_id=28`, `status=consumed`. `state.db.node_runs` — две строки `planning` (обе `succeeded`). В логе: planning #1 завершён 18:33:24, planning #2 стартовал 18:35:55 (разрыв ~2.5 мин = ожидание ответа). `planning` — `session_scope: fresh_disposable`, поэтому run-000003 переделал план целиком (≈33.9k выход, 532 c).
- **Корневая причина.** Узел `planning` имеет `hitl: {allow_question: true, allow_approval: true}`, а роль велит «use human_input for a material clarification or approval». Сама задача оставила границу скоупа неоднозначной (честный конформинг canon пересекает заявленную границу M2↔M3/M6: тянет миграцию, persistence и сужение security-scope), так что агент **корректно** эскалировал — но прогон стал зависеть от человеческого «тапа» (таймаут `telegram.ask_timeout_s=28800` = 8 ч), и из-за `fresh_disposable` ответ запускает полный реплан, а не возобновление сессии.
- **Рекомендуемая правка (рычаги).** (a) Автономность: для хорошо специфицированных задач предрешать границу скоупа в самом task-файле (task-authoring), либо понизить `allow_approval` планирования до «вопрос-с-дефолтом» (у агента уже был внятный дефолт: «if you just say proceed…»). (b) Эффективность: возобновлять сессию при HITL вместо `fresh_disposable`-реплана — **это поднято в отдельную критическую находку 0** (рычаг `agent.py:132/441/491-508`); здесь — только автономность. **Важно:** само поведение во многом ХОРОШЕЕ — вопрос предотвратил тихое расползание скоупа.
- **Зона:** target (task-spec / флоу). **Ожидаемый эффект:** меньше остановок на человека на хорошо описанных задачах; меньше продублированного компьюта планирования.

### 5. В консольном логе нет сигнала, что прогон заблокирован на HITL-approval — ПОДТВЕРЖДЕНО

- **Категория:** наблюдаемость. **Серьёзность:** низкая-средняя. **Уверенность:** высокая.
- **Доказательство.** Греп консольного лога по `human|approval|telegram|await|question|hitl|input` — **ни одной строки**. Между «planning #1 completed» (18:33:24) и «planning route resolved» (18:35:55) — 2.5 мин тишины; запрос ушёл только в Telegram (`message_id=28`). Оператор, смотрящий в консоль/heartbeat, видит необъяснимый провал и не понимает, что прогон ждёт человека.
- **Корневая причина.** Вход в ожидание `human_input` не логируется в основной логгер и не эмитит heartbeat (в отличие от провайдер-операций, у которых heartbeat есть). Тот же класс, что наблюдение 5 из прошлого разбора (`redesign-form-controls`): «тихий» участок прогона.
- **Рекомендуемая правка (рычаг).** В HITL-пути оркестратора (`src/wastech_orchestrator/core/orchestrator.py`, отправка `human_input`/`_log`; плюс observability) эмитить строку лога + heartbeat при входе в ожидание: «awaiting human approval (interaction=…, channel=telegram, timeout=8h)» и при получении ответа.
- **Зона:** дефолт оркестратора. **Ожидаемый эффект:** «заблокирован на человеке» видно в логе, а не как немой разрыв.

### 6. Висящие плейсхолдеры в промпте реализации, когда задача не декомпозирована — ПОДТВЕРЖДЕНО

- **Категория:** prompt. **Серьёзность:** низкая. **Уверенность:** высокая.
- **Доказательство.** `stages/implementation/rendered-prompt.md`, стр. 17: «…you must implement ONLY that subtask — subtask of — per its immutable spec: » — плейсхолдеры `{subtask_order}` / `{subtask_count}` / `{subtask_spec_path}` отрендерились пустыми (задача не декомпозирована), оставив бессмысленную хвостовую фразу. Тот же шаблон — в `roles/implementation_backend/fixing.md`, стр. 5.
- **Корневая причина.** Роль всегда включает «subtask»-клаузу; при недекомпозированной задаче подстановка даёт пустую висящую фразу.
- **Рекомендуемая правка (рычаг).** Сделать клаузу условной в ролях `roles/implementation_backend/implementation.md` и `fixing.md`, либо опускать её в сборщике промпта (`_adapter_base.build_effective_prompt` / место подстановки) при недекомпозированной задаче.
- **Зона:** target-роли (и проверить packaged-аналоги). **Ожидаемый эффект:** чище промпт; очень низкий приоритет.

### 7. Соответствие модель/reasoning — Sonnet/max хватило; opus/xhigh на планировании оправдан — ПОДТВЕРЖДЕНО (по большей части «уже хорошо»)

- **Категория:** model / reasoning. **Серьёзность:** низкая. **Уверенность:** высокая.
- **Доказательство.** `implementation` (sonnet/max) сделал изменение на +1242 строки (миграция + Domain + Contracts + handlers + тесты) за одну попытку, 0 кругов исправлений, причём по ходу сессия даже авто-сжалась («This session is being continued from a previous conversation that ran out of context» в `events.jsonl`) — изменение крупное, но провайдер справился и довёл build до 0/0. `review` (sonnet/max) поймал реальные баги. Признаков недостатка мощности нет → бампить `implementation` до Opus не нужно. Единственный перерасход — `planning` дважды на opus/xhigh (≈67k выход, ≈18 мин компьюта) из-за HITL-реплана (см. находку 4), но это не про выбор модели.
- **Рекомендуемая правка (рычаг).** Изменений модели не рекомендую. Если важна автономность/время — см. находку 4 (HITL-реплан), а не смена тира.
- **Зона:** — . **Ожидаемый эффект:** —.

## Пробелы в данных

- **`prompt_audit: false`** → нет `prompt-audit/timeline.jsonl` (нет хронологического по-промптного аудита: provider/model/attempt/fallback по шагам). Рекомендую включить перед следующим прогоном.
- **Денежная стоимость** не оценивается (`total_cost_usd=null`, подписка/OAuth) — только токены и wall-time.
- **Реальный op-by-op diff** эмитированного Swagger ↔ canon нигде не зафиксирован — независимо подтвердить заявление «conforms» нечем (`summary.md` caveat 1 это признаёт честно).
- **Реальный прогон интеграционных тестов** не выполнялся (ни агентом до конца — Testcontainers/Docker, ни оркестратором — пустой `command_sets`). «Tests updated» ≠ «tests pass».

## Что уже хорошо

- **Чистая машина состояний:** 0 кругов исправлений, 0 fallback'ов, 0 крашей, все попытки провайдера — с первого раза (`provider_attempts` все `succeeded`/`exit_code=0`).
- **Сильное планирование.** HITL-вопрос (`hitl/planning.json`) предметный и точный: агент корректно увидел, что честный конформинг canon пересекает заявленную границу скоупа (миграция/persistence/удаление `deviceId`) и затребовал sign-off с разумным дефолтом — это предотвратило тихое расползание скоупа, а не создало шум.
- **Агент сам обеспечил часть гейтов:** прогнал `dotnet build` до 0 ошибок/0 предупреждений и **самостоятельно подтянул repo-скил `backend-contracts`** (`events.jsonl`: «Base directory for this skill: …\backend\.claude\skills\backend-contracts») — то есть пустой `selected_skills.json=[]` (из-за `skills.scan_root: ''`) агента не обеднил: спавненный `claude` читает `.claude/skills/` целевого репо напрямую.
- **`review` адверсариальный и содержательный:** поймал PascalCase-сериализацию `InvitationPending` (вместо snake_case), деструктивную миграцию, отсутствующий null-guard, дубль `MapStructured` — всё по чеклисту роли.
- **Супервайзер написал содержательный финальный summary** (`summary_written: true`) и честно перечислил реальные слабости прогона (непроверенный прогон тестов, «заявлен, но не показан» conformance-diff, data-safety миграции, нерешённую регенерацию api-client) — адвайзори-слой РЕАЛЬНО поймал проблемы (хоть и не сделал их блокирующими).
- **Дифф строго в скоупе backend-only флоу:** нет public-debate/voting-механик, нет спонтанной смены TFM, слоистость соблюдена (Domain framework-free, EF только в Infrastructure, wire-типы только в `Chat1o1.Contracts`), доки (`techdebt.md`/`PROGRESS.md`) обновлены, `CLAUDE.md`/`docs/v2/architecture` не тронуты.
- **`refinement` корректно пропущен** (валидация `complete`).

## Windows-специфика (особо — то, ради чего был этот прогон)

- **`worc` нет в PATH** на этой машине — консольный shim не установлен/не зарегистрирован. Запускал через `python -m wastech_orchestrator …` (python 3.14.5, модуль резолвится из editable-инсталла `I:\github\wastech-agent\src`). Это операционный нюанс, не баг оркестратора, но в инструкции по запуску на Windows стоит явно дать вариант `python -m`.
- **Windows-allowlist окружения сработал.** Конфиг уже несёт `SystemRoot/SystemDrive/windir/ComSpec/PATHEXT/TEMP/TMP/APPDATA/LOCALAPPDATA/HOMEDRIVE/HOMEPATH/...` (с комментарием, что без `SystemRoot` Node-овые `claude.exe`/`codex` падают на старте с `0xC0000409`). Прогон это подтвердил: ни одного краша провайдера. Кросс-платформенная работа по env окупилась.
- **Глюк листинга на диске `I:`.** Первый `Glob .worc/flows/*.yaml` и рекурсивный `Get-ChildItem -Recurse -Filter *.yaml` **ложно вернули пусто**, хотя файлы есть; прямой `Get-ChildItem .worc/flows` их нашёл. Похоже на квирк индексации/инструментов на вторичном диске Windows — не баг оркестратора, но нюанс при скриптинге анализа на не-системных дисках (перепроверять «пусто» прямым листингом).
- **Пути в артефактах/`state.db` — с обратными слэшами.** `tasks.source_path = I:\github\argudebate\tasks\done\…`. Правило проекта (CLAUDE.md / coding-style): для любого хранимого/сравниваемого/отображаемого пути — `pathlib` + `Path.as_posix()`. Здесь хранимый путь backslash-стайл. В ЭТОМ прогоне не аукнулось (задача завершилась), но это потенциальный вектор хрупкости rerun на Windows (тот же класс, что наблюдение про `source_path` в прошлом разборе, но усиленный разделителем пути) — стоит проверить, что сравнения `source_path` нормализуют разделитель. Низкая уверенность по влиянию, средняя по факту.
- **`dotnet` в сессии агента на Windows отработал** (build 0/0) с одним транзиентным `dotnet restore`/build-сбоем и восстановлением — обычная toolchain-фрикция, не Windows-специфика.

## Побочные эффекты теста (как и предупреждалось — с поправкой на факт)

- **PR `argudebate#5` СМЕРЖЕН** (squash) — состояние `MERGED`, коммит `92fdbd0` «…(#5)» уже в `master` (локально и на origin, в синхроне). Это сделал **оператор вручную** (оркестратор `auto_merged=false`, `merge_outcome=null`). То есть backend-only изменение — с заявленным (не показанным) conformance и деструктивной миграцией (drop `messages.body` без backfill, см. находку 3) — теперь **в `master`**, при этом `mobile/swagger.json` и `@app/api-client` так и не регенерированы.
- **Рабочее дерево целевого репо ЧИСТОЕ** (`git status --porcelain` пуст). Предупреждавшееся «висящее удаление файла задачи» НЕ материализовалось: файл задачи корректно переехал в `tasks/done/td-be-003-…md` (часть смерженного PR), в `tasks/pending/` его уже нет.
- **Команда восстановления (если нужно откатить тест).** Так как PR смержен в `master`, «закрыть PR» уже неприменимо. Полный откат — ревертом merge-коммита: `git -C I:/github/argudebate revert -m 1 92fdbd0` и `git push origin master` (+ при желании удалить ветку `git push origin --delete worc/td-be-003-…-swagger-json`). **Но**: судя по тому, что оператор смержил PR сам, изменение, вероятно, нужное — откатывать без подтверждения не рекомендую.

---

## План исправлений

Приоритеты: P0 — самый большой эффект, P1 — важно, P2 — приятно иметь. У каждого пункта — «рычаг» и зона.

### P0. 🔴 HITL обязан возобновлять сессию агента (директива оператора), гейт качества — настоящим, кросс-стек задачу — не в backend-only флоу

0. **🔴 КРИТИЧНО — HITL возобновляет сессию, а не пересоздаёт** (находка 0). В `src/wastech_orchestrator/core/flow/nodes/agent.py` прокидывать `session_id` первого прогона во второй `_invoke` при HITL (`agent.py:132`) и в `_reconsider` (`agent.py:373`), сняв ограничение `_resume_session_id` «только editing_lineage» (`agent.py:491-508`) для случая HITL-реинвока — так, чтобы после ответа оператора агент продолжал ту же сессию с точки запроса для **любого** `session_scope` и **любого** узла. Провайдерская сторона resume (`providers/claude.py`/`_adapter_base.py`) уже есть; gate по провайдеру сохранить (нельзя резюмировать claude-сессию на codex). Тест: прогнать задачу с HITL и убедиться, что `result.session_id` второго прогона == первого. **Зона:** дефолт оркестратора.
1. **Заполнить `checks.command_sets`** в `I:/github/argudebate/.worc/config.yaml` хотя бы `dotnet build src/Chat1o1.sln` (рабочая директория `backend/`, привязка к `backend/**`); при наличии Docker — Docker-gated `dotnet test`. Схема/доки: `config/schema.py`, `config/loader.py`, `docs/configuration.md`. **Причина:** находка 1. **Зона:** target.
2. **Развести задачу и флоу** (находка 2): либо завести `task_type: contract_change` с кросс-стек ролью реализации (разрешает `mobile/openapi/**` + `@app/api-client`) и помечать такие задачи им; либо переформулировать критерий приёмки №6 под «backend конформит к неизменному canon; регенерация api-client — отдельный follow-up». **Зона:** target (флоу/роль или task-файл).

**Эффект:** HITL продолжает сессию (без реплана/потери контекста); `done` означает «скомпилировано/проверено оркестратором»; либо мобильная половина реально делается, либо критерий честен.

### P1. Ужесточить планку `review` и осветить HITL-ожидание

1. **`review`-роль** (`roles/implementation_backend/review.md`): сделать блокирующими «невыполненный/непродемонстрированный критерий приёмки» и «деструктивную миграцию без backfill». **Причина:** находка 3. **Зона:** target (± packaged-дефолт).
2. **Лог/heartbeat на входе в `human_input`-ожидание** (`core/orchestrator.py` + observability): строка «awaiting human approval (channel=telegram, timeout=…)». **Причина:** находка 5. **Зона:** дефолт оркестратора.

**Эффект:** ревью не пропускает невыполненные критерии/потерю данных; «заблокирован на человеке» виден в логе.

### P2. Автономность, эффективность планирования и косметика

1. **Снизить избыточный HITL** на хорошо специфицированных задачах: предрешать границу скоупа в task-файле, либо понизить `planning.hitl.allow_approval` до вопроса-с-дефолтом; рассмотреть возобновление сессии планирования вместо `fresh_disposable`-реплана при HITL. **Причина:** находка 4. **Зона:** target.
2. **Условная «subtask»-клауза** в ролях `implementation.md`/`fixing.md` (или опускать в сборщике промпта при недекомпозированной задаче). **Причина:** находка 6. **Зона:** target-роли. Очень низкий приоритет.
3. (Опц.) **Включить `prompt_audit: true`** перед следующим прогоном — закрывает пробел в данных (по-промптный аудит).

---

## Сводная таблица «наблюдение → причина → рычаг»

| Наблюдение | Причина | Что менять (рычаг) | Зона |
| --- | --- | --- | --- |
| **0. 🔴 КРИТ. HITL пересоздаёт сессию агента (разные `session_id`), а не возобновляет** | HITL-реинвок не прокидывает `session_id` первого прогона; `_resume_session_id` резюмирует только `editing_lineage` | `core/flow/nodes/agent.py:132` (2-й `_invoke`), `:373` (`_reconsider`), `:491-508` (`_resume_session_id`) — резюмировать сессию при HITL для любого `session_scope`/узла | дефолт |
| 1. `testing` — пустой проход, build/тесты не прогнаны оркестратором | `checks.command_sets: {}` → выбирать нечего | `config.yaml` → `checks.command_sets` (`dotnet build …`); `config/schema.py`, `docs/configuration.md` | target |
| 2. Критерий №6 (атомарный кросс-стек) невыполним; дифф backend-only | Кросс-стек задача в backend-only флоу (`task_type: implementation_backend`); роль запрещает `mobile/` | `task_type`/новый флоу `contract_change` + роль, либо переформулировать критерий №6; `core/flow/registry.py` | target |
| 3. `review` принял с невыполненным критерием и деструктивной миграцией | Флоу объявил `mobile/` вне скоупа → ревью смягчил планку; миграция «dev-only» | `roles/implementation_backend/review.md` — блокировать unmet-criterion + DROP-без-backfill | target (± дефолт) |
| 4. `planning` дважды + ожидание Telegram-approval | `hitl.allow_approval: true` + неоднозначная граница скоупа задачи; `fresh_disposable` → реплан | task-spec; `planning` в `implementation_backend.yaml` (`hitl`/`session_scope`); HITL-путь `core/orchestrator.py` | target |
| 5. Нет лог-сигнала о блокировке на HITL (2.5 мин тишины) | Вход в `human_input`-ожидание не логируется/без heartbeat | `core/orchestrator.py` (HITL) + observability — строка лога/heartbeat | дефолт |
| 6. Висящие плейсхолдеры `subtask  of  … spec:` | «subtask»-клауза всегда в роли; недекомпозировано → пусто | `roles/implementation_backend/implementation.md` + `fixing.md` (условная клауза) / сборщик промпта | target |
| 7. `xhigh`/`max` — мощности хватило; планирование ×2 | Sonnet справился; перерасход планирования — из-за HITL-реплана (см. 4) | смена модели не нужна; см. находку 4 | — |
| W. Windows: `worc` нет в PATH; backslash в `source_path`; ложно-пустой листинг на `I:` | shim не зарегистрирован; `as_posix()` не применён к хранимому пути; квирк диска | доки запуска (`python -m`); проверить нормализацию `source_path`; перепроверять листинг | target/дефолт |
