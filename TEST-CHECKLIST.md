# Тест-чеклист реализованных функций

Полный список функций, реализованных за последнее время, которые нужно протестировать. Источники: `docs/backlog/archive/done/` и подсистема памяти `docs/backlog/memory/`.

Каждая задача оформлена отдельной секцией. Внутри секции — чеклист (`- [ ]`) конкретных функций/поведений, которые нужно покрыть тестами (ручными и/или автоматическими).

> Заметки: технические идентификаторы (имена функций, конфиг-ключей, CLI-команд) даны как в коде/доках. Описания — на русском.

---

## Проход 1 — что проверено (2026-07-02)

Источник: установка worc на `wastech-mdlint` + smoke-задача `lint-cmd-alias` (done → PR #1 **замержен** в main) + `/analyze-task-run`. Галочками (`[x]`) ниже отмечено только то, что **реально наблюдалось** в этом прогоне. Находки — в [TEST-FINDINGS.md](TEST-FINDINGS.md) (F1–F8).

**Покрыто (happy-path одиночной задачи):** §3 `worc list --pending`/read-only/render; §5.2 условный subtask-блок (non-decomp → без висячей фразы); §15 single-provider → `fallback=None`; §16 OS-aware env-allowlist (POSIX/macOS: `USER`/`TMPDIR`/`DYLD…`, claude authenticated); §17.2/.3/.5/.6/.7/.9 (install доставляет `config.example.yaml` + guide + flow-owned промпты, lifecycle без `processing`, flow-local supervisor/finalize промпты, prompt-variable контракт + чистый preflight-lint); §18 Cluster A+B (prompt-authoring, `emit_follow_ups` opt-in, supervisor advisory-инвариант, malformed structured-output → summary fallback); §19 node-output `{<id>_path}` (agent-узлы → `.out.md`, спец-слот `plan` без дубля); §23 memory AC-S2/S3/S5 + `MemoryConfig` + AC-R4.

**Частично / заблокировано (перепроверить в Phase 3):**

- §23 **AC-S1/AC-W1** — ✅ РАЗБЛОКИРОВАНО в прогоне 2 (`p0-01`, см. ниже): F7 **VERIFIED FIXED** (`supervisor.reasoning: high`) → finalize-turn зелёный, `short_term`/`entities`/`audit` непусты, `memory_delta` едет тем же turn'ом (AC-W1). Новый нюанс: **AC-SF3 частичен** — хэш-цепочка целостна, но `rationale` во всех audit-строках пуст (**F9**).
- §3 **`--format ids`** — не отмечен: пустой вывод для pending-only задач (**F4**, DB-only источник).
- §5.2 decomposed-случай (subtask N of M) — не тестировался (задача не декомпозирована).
- §12 **per-node model/reasoning override** — ✅ ЧАСТИЧНО НАБЛЮДЁН в прогоне 4 (`p0-04`, см. §12): flow-YAML-уровень overrides применились на runtime (planning=opus/high, impl/review/fixing=sonnet/xhigh, doc=sonnet/medium; подтверждено `prompt-audit/timeline.jsonl`), seam покрыл agent+evaluator. Task-front-matter уровень (`nodes.<id>.model`) и fallback-на-невалидный ещё не проверены (ждут p0-06).
- §13 **priority-scheduling** — ✅ ПОДТВЕРЖДЕНО в прогоне 5 (diamond p0-05‖p0-06): среди 2 одновременно-eligible high (p0-05) заклеймлен раньше low (p0-06); §8 single-active подтверждён (p0-06 не побежал параллельно). reject-BROKEN ещё нет.
- §9 **operator-gates (autonomous)** + §4 **HITL** — ✅ ЧАСТИЧНО в прогоне 5: implementation-узел p0-05 поднял `kind=approval`-гейт, авто-резолв за 7s (`status=answered`, оператора нет → autonomous auto-proceed). Оператор-driven resume/deny ещё не наблюдался.

**Ещё не запускалось (ждёт P0-цепочку / отдельные прогоны):** §1 branch-epoch, §5.1 observability, §6 console/stop-ladder, §7 logs clean, §8 queues (single-active подтверждён, tags — нет), §10 prs/merge-task, §11 skills-selection, §14 telegram-trace (формат подтверждён визуально), §20 subtask-handoff, §21 autonomous-run, §22 integration. §12/§13/§9/§4 — частично (см. выше). Windows-специфика (§16) — вне scope (macOS-хост).

---

## Проход 6 — что проверено (2026-07-04)

Источник: `worc run` задачи `p4-01-context-graph-model-v2` (первая задача кампании P4) → `done` + PR [#8](https://github.com/VladimirMakarevich/wastech-mdlint/pull/8) (НЕ смержен) + `/analyze-task-run`. Отчёт: [docs/analysis/p4-01-context-graph-model-v2-run-analysis.md](docs/analysis/p4-01-context-graph-model-v2-run-analysis.md). Находки — [TEST-FINDINGS.md](TEST-FINDINGS.md) F19–F23. Галочки — только реально наблюдённое в этом прогоне.

**Покрыто (multi-node implementation-флоу, кросс-провайдер):**

- §12 **per-node override (flow-YAML → provider-default)** — ✅ ПОВТОРНО ПОДТВЕРЖДЁН на **codex-evaluator**: review перекрыл глобальный конфиг-дефолт codex `gpt-5.5/high` → declared `gpt-5.4/xhigh` (`stages/review/…/request.json` argv `--model gpt-5.4 -c model_reasoning_effort="xhigh"`). Task-front-matter уровень по-прежнему НЕ проверен (задача задаёт только `nodes.refinement.enabled:false`).
- **Per-task node-skip** — ✅ `refinement` skipped, `state.db node_runs.skip_reason="disabled by task: nodes.refinement.enabled=false"`.
- **Изоляция + orchestrator-only commit** — ✅ до publish в основной ветке коммита нет (агент в изолированном workspace); `git log feat/p4-01…` на базе `ce946f6`.
- **Read-only planning** — ✅ argv `--permission-mode plan --allowedTools Read,Glob,Grep --disallowedTools Bash(git commit)/…/Read(.env)/Read(secrets/**)`.
- **Независимый checks-гейт** — ✅ testing-узел (kind:checks) прогнал typecheck/lint/**146 тестов**/build (`checks/001–004.log`), совпал с самопроверкой имплементера.
- **Постоянный supervisor-слой** — ✅ проверил КАЖДЫЙ завершённый шаг (`evaluations` supervisor_step ×6: planning/impl/testing/review/doc + final), advisory, не блокирует.
- **Чистая инфра** — ✅ 0 ретраев/фоллбэков/крэшей (`provider_attempts` все succeeded, exit 0); codex не утёк на claude-модель.
- **prompt-audit** — ✅ `timeline.jsonl` присутствует (пробела в данных нет).

**Находки (см. F19–F23):** 🔴 F19 review-evaluator **no-op** (3 codex-blocking → `verdict=accept, findings=[]` → PR с багами как `done`); F20 `current.diff` неполон (нет untracked-тестфайла + ядро «Binary files differ»); F21 planning plan-mode обходит `human_input`; F22 codex usage=0; F23 (target) пре-существующий NUL-делимитер. **Статусы (2026-07-04): F19/F20/F22 RESOLVED, F21 RESOLVED (live-подтверждение allowlist-гейта — follow-up), F23 RESOLVED-BY-TASK** — см. [TEST-FINDINGS.md](TEST-FINDINGS.md) и [run-quality-gating-hardening.md](docs/backlog/archive/done/run-quality-gating-hardening.md).

**Не наблюдалось в этом прогоне:** fix-loop (`fixing`/`review_fix_cycles`>0) — из-за F19 так и не запустился; HITL-пауза оператора; MANUAL_ACTION_REQUIRED; decomposition; fallback/retry.

---

## Проход 7 — что проверено (2026-07-04)

Источник: первая задача **branch-mode chain-теста** — `worc run` задачи `p4-02-graph-algorithms` с `branch_mode: new` + кастомный `branch_name: feat/p4-graph-chain` (общая ветка для цепочки p4-02..p4-08) → `done` + PR [#9](https://github.com/VladimirMakarevich/wastech-mdlint/pull/9) (НЕ смержен) + `/analyze-task-run`. Отчёт: [docs/analysis/p4-02-graph-algorithms-run-analysis.md](docs/analysis/p4-02-graph-algorithms-run-analysis.md). Находки — [TEST-FINDINGS.md](TEST-FINDINGS.md) F24–F25. Галочки — только реально наблюдённое.

**Покрыто:** §24 branch-mode `new` + кастомный `branch_name` (см. ниже); §12 повторное подтверждение per-node override на codex-evaluator (review→gpt-5.4/xhigh); §13 explicit `worc run` refuse на unmerged/abandoned `depends_on` (новый вариант — abandoned-статус, не только «PR OPEN»). **Регресс от сегодняшнего F19-фикса:** codex-evaluator крашится 100% детерминированно (`_FINDINGS_SCHEMA` без `additionalProperties:false` → OpenAI 400 `invalid_json_schema`), см. F24 — замаскировано claude-фоллбэком, но фатально при single-provider=codex.

**Не наблюдалось:** `branch_mode: existing`/`current`, `branch_ref`, PR-reuse, per-task `publish`-cap — запланированы на следующие задачи цепочки (p4-03..p4-08).

## Проход 8 — что проверено (2026-07-04)

Источник: `p4-03-query-layer`, второй шаг chain-теста — `branch_mode: existing` + `branch_ref: feat/p4-graph-chain` → `done`, PR **переиспользован** ([#9](https://github.com/VladimirMakarevich/wastech-mdlint/pull/9), не новый). Отчёт: [docs/analysis/p4-03-query-layer-run-analysis.md](docs/analysis/p4-03-query-layer-run-analysis.md). Находка — [TEST-FINDINGS.md](TEST-FINDINGS.md) F27.

**Покрыто:** §24 `existing`-mode checkout + multi-task accumulation + PR-reuse (см. ниже, галочки в разделе 24). **F24 повторно подтверждён** (2/2): codex review падает идентично p4-02 (`process_crashed`, ~5с) → claude fallback → `accept` — детерминированность зафиксирована, дальше не перепроверяется на каждой задаче отдельно.

**Также перед запуском:** живой демо-отказ на p4-03 с исходным `depends_on: [p4-02-graph-algorithms]` (PR #9 открыт, не смержен) → `error: refusing to run p4-03-query-layer: dependency 'p4-02-graph-algorithms' PR is OPEN (unmerged)`, exit 2 — см. **F26** (депендс-он-merge-gate не интегрирован с branch-mode chain).

## Проход 9 — что проверено (2026-07-04)

`p4-04-search-index-slice` (3-й шаг chain-теста, `existing`) → `done`, PR **#9 переиспользован 3-й раз подряд** (накопил p4-02+p4-03+p4-04). Codex review упал 3/3 идентично F24 (не перепроверяется дальше — детерминированность подтверждена). Новое: claude-review нашёл `medium`-находку (phase-doc P4.04 не обновлён) — корректно классифицирована как advisory, не блокирует. Лёгкий отчёт: [docs/analysis/p4-04-search-index-slice-run-analysis.md](docs/analysis/p4-04-search-index-slice-run-analysis.md). Без новых F-номеров.

## Проход 10 — что проверено (2026-07-04)

`p4-05-impact-analysis` (4-й шаг chain-теста, `existing` + `publish: push` + намеренно невалидный `provider: gemini`) → `done`, `pr_url=null` (push-only, без PR). Три независимых механизма подтверждены живьём одновременно: §12 invalid-override fallback (см. выше), §24 `publish: push` (см. выше), и **первый в кампании настоящий fix-цикл** — review нашёл реальный HIGH-баг (`relativizeImpact` портит топологический `readingOrder` лишней алфавитной сортировкой, `impact-analysis.ts:486`), `fixing` исправил за 1 итерацию с объясняющим комментарием, повторный review дал `accept`. Codex упал 5-е/6-е подряд (F24, детерминированность окончательно подтверждена, дальше не отслеживается). Отчёт: [docs/analysis/p4-05-impact-analysis-run-analysis.md](docs/analysis/p4-05-impact-analysis-run-analysis.md). Без новых F-номеров — все механики сработали как задокументировано.

## Проход 11 — что проверено (2026-07-05)

`p4-06-grp-refactor-coverage` (5-й шаг chain-теста, `existing`, без спецтвиков) → `done`, PR **#9 переиспользован 5-й раз подряд**. Codex упал 7/7 (F24, не отслеживается дальше). Review — 4 LOW, все non-blocking (одна интересная: implementer расширил экспорт `index.ts` шире плана P4.06, безвредно). Лёгкий отчёт: [docs/analysis/p4-06-grp-refactor-coverage-run-analysis.md](docs/analysis/p4-06-grp-refactor-coverage-run-analysis.md). Без новых F-номеров.

## Проход 12 — что проверено (2026-07-05)

`p4-07-cli-graph-slice-impact` (6-й шаг chain-теста, `existing`, зависел от p4-04+p4-05 без `depends_on` — только порядком ручного запуска) → `done`, PR **#9 переиспользован 6-й раз подряд**. Codex упал 8/8 (F24). Review — 2 LOW, обе явно by-design. Лёгкий отчёт: [docs/analysis/p4-07-cli-graph-slice-impact-run-analysis.md](docs/analysis/p4-07-cli-graph-slice-impact-run-analysis.md). Без новых F-номеров.

## Проход 13 — что проверено (2026-07-05), финал branch-mode chain-теста

`p4-08-graph-tests` (7-й и последний шаг, `branch_mode: current` — оператор вручную `git checkout feat/p4-graph-chain` до `worc run`) → `done`, PR **#9 закрыл всю цепочку** (7 коммитов p4-02..p4-08, 47 файлов, +2645/−110, всё ещё открыт). Подтверждены оба safety-инварианта `current`-режима (см. §24 выше: no-op checkout + no-force-cleanup-на-base). Codex упал 9/9 (F24, финально детерминирован). Отчёт: [docs/analysis/p4-08-graph-tests-run-analysis.md](docs/analysis/p4-08-graph-tests-run-analysis.md). Без новых F-номеров — **вся ADR branch-mode функциональность (new/existing/current/publish-cap/PR-reuse) протестирована и работает как задокументировано**, кроме нескольких явно неиспробованных негативных веток (см. непроставленные пункты §24: `branch_ref` not-exists, detached HEAD, `reset` refuse в existing, closed/merged non-reuse, head==base guard, `publish: commit`).

## Проход 14 — cross-run синтез всей фазы P4 (2026-07-05, только анализ)

Не прогон, а сквозной разбор всех 8 задач кампании под тремя призмами. Три отчёта: [синтез фазы](docs/analysis/p4-phase-synthesis.md) (A), [качество промптов по узлам](docs/analysis/p4-prompt-quality-per-node.md) (B), [аудит памяти](docs/analysis/p4-memory-subsystem-audit.md) (C). Находки — [TEST-FINDINGS.md](TEST-FINDINGS.md) F28–F37. Ничего не запускалось/не менялось.

**Подтверждено анализом (`[x]` — доказано данными/кодом):**

- [x] **Инфраструктура детерминированно чистая через 8 задач**: 32 agent/publish-прогона `succeeded` с 1-й попытки (`provider_attempts`), 0 неожиданных фоллбэков/крахов кроме codex-review; модель не утекала между провайдерами.
- [x] **Checks-гейт железный**: 32/32 `check_runs` passed, 0 timeout; тест-сьют рос монотонно 146→242 без регрессий.
- [x] **Per-node override на каждом узле** (opus/high planning, sonnet-5/xhigh impl+fixing, gpt-5.4/xhigh review-declared, sonnet-5/medium doc) — сверено `timeline.jsonl` каждой задачи.
- [x] **Все AC 8 фаз закрыты фактическим кодом** ветки (независимая проверка против спеков, не по галочкам doc-узла); **3 бага F19 исправлены человеком до мержа PR #8** (авто-гейт дал false-green).
- [x] **Fix-loop механика** (p4-05, единственный blocking кампании — readingOrder): `rework`→`fixing`(1 итерация)→re-review→`accept`, end-to-end как задокументировано.
- [x] **Fallback-механизм** штатно спасал codex-review-краш 9/9 (F24) — предсказуемо, не хрупко.
- [x] **Фикс F20 (`--text`) подтверждён рабочим на живом NUL-файле** (`current.diff` p4-02/p4-08 рендерит binary-класс файлы как текст).
- [x] **Промпт-аудит 6 типов узлов** (planning/implementation/review/fixing/documentation/supervisor): поведение агентов в основном соответствует промптам; проблемы локализованы (review-вход/схема/мёртвый memory-блок + finalize→память словарь).
- [x] **Аудит памяти**: 4 непустых jsonl прочитаны целиком; hash-цепочка аудита цела (37/37), rationale заполнен (F9 закрыт), poisoning не пробит; entity-карточки фактически корректны.

**Новые находки (F28–F37):** 🔴 **F37** теневая нативная память Claude Code пишет в `~/.claude/…` вне изоляции (HIGH); **F29** словарь `evidence.type` (`file`/`commit` не распознаны) топит 18/21 уроков в `agent-inferred`; **F28** кросс-вендорное ревью не исполнилось 0/9 (фактически claude ревьюит claude); **F30** рекуррентность по дословному `subject` не промоутит реально повторившийся урок; **F32** review судит по кумулятивному/pre-doc диффу → ложная находка (p4-06) + шум «phase-doc»; **F31** review без пакета памяти; **F33** «sort every output array» без оговорки; **F34** planning-промпт с несуществующими primitive-путями; **F35** рецидив NUL-делимитеров; **F36** абсолютный host-путь + невоспроизводимая редакция в эпизодах.

**Не покрыто / открытые ветки:** [ ] режим single-provider=codex (был бы `manual_action_required` на КАЖДОЙ задаче из-за F24 — не прогонялся); [ ] фактический промоушен памяти (`long_term/` пуст by F29/F30 — накопление durable-знания не наблюдалось ни разу); [ ] реальный eval-baseline памяти (синтетический, greenfield); [ ] исполнение codex-ревью по существу (падало 9/9 до генерации).

---

## Проход 15 — что проверено (2026-07-07), первый прогон с codex-primary

Задача `p5-01-classify-nodes` → `done`, PR [#11](https://github.com/VladimirMakarevich/wastech-mdlint/pull/11), `fix_iterations=0`. Перед прогоном (по команде оператора) конфиг переключён: глобальный `primary` claude→**codex `gpt-5.4`/`xhigh`**, рабочие узлы флоу перепинены на codex, review перевёрнут на **claude `opus-4-8`/high**. Находки — [TEST-FINDINGS.md](TEST-FINDINGS.md) **F38–F39**. Отчёт: [docs/analysis/p5-01-classify-nodes-run-analysis.md](docs/analysis/p5-01-classify-nodes-run-analysis.md).

**Подтверждено этим прогоном (`[x]` — наблюдалось в артефактах):**

- [x] **Глобальный primary=codex реально маршрутизирует незапиненные + codex-пинненные узлы на codex**: `route resolved primary=codex fallback=claude` на planning/implementation/documentation (`p5-01-run.log`); codex **fresh**-сессии planning (356s) и implementation (255s) — `provider_attempts` `succeeded` exit 0.
- [x] **§12 per-node override в ОБЕ стороны на flow-уровне**: review перевёрнут на claude (`route resolved node_id=review primary=claude fallback=codex source=flow_node`; `stages/review/run-000112/1-claude` `succeeded`), рабочие узлы — на codex. Task-front-matter уровень по-прежнему НЕ проверялся.
- [x] **§15 симметричный fallback codex→claude на ГЛОБАЛЬНОМ primary** (не только на запиненном evaluator, как в F24): documentation codex `unsupported_version` → claude `succeeded` (`state.db provider_attempts`; `p5-01-run.log` `msg="falling back" from=codex to=claude`).
- [x] **Кросс-провайдерный fallback сбрасывает и модель, и сессию**: documentation-fallback `request.json` — `session_id: None` (свежая сессия, argv без `resume`), `--model claude-opus-4-8` (НЕ утёкший codex `gpt-5.4`); контекст «что зашипано» подан через `plan_path`+`diff_path` в промпте. Подтверждает фикс cross-provider-model-leak в направлении codex→claude.
- [x] **Checks-гейт**: 4/4 `check_runs` passed (typecheck/lint/test/build, exit 0, 0 timeout).
- [x] **review-evaluator на claude дал `accept` с 0 findings** (`review/findings.json` = `{"findings": []}`, `state.db evaluations` `in_flow_verdict=accept`); diff чистый и в скоупе (4 файла, +257/−3).

**Вскрытые дефекты (F38–F39):** 🔴 **F38** codex `exec resume` строится с `--cd`/`--sandbox`/`--model`, которые codex 0.142.5 отвергает → ВСЕ resume-узлы (supervisor ×6, documentation) падают на codex и уходят в fallback (HIGH); **F39** `supervisor` имеет `model` без `provider` → под codex-primary уводит `--model claude-opus-4-8` на codex (MEDIUM).

**Не наблюдалось в этом прогоне:** codex resume-путь успешно (F38 — падал 100%); fix-loop (`fixing`/`review_fix_cycles`>0 — review принял с первого раза); HITL-пауза; decomposition; MANUAL_ACTION_REQUIRED; single-provider=codex.

---

## Проход 16 — что проверено (2026-07-07), codex-primary на 0.8.9a3 (фикс F38/F39)

Задача `p5-02-doc-profile` → `done`, PR [#11](https://github.com/VladimirMakarevich/wastech-mdlint/pull/11) reuse (2-й коммит), `fix_iterations=0`, на ветке `feat/p5-compile` (`branch_mode: existing`). Версия **0.8.9a3** (доставлен фикс ADR codex-primary-correctness). Находки — [TEST-FINDINGS.md](TEST-FINDINGS.md): **F38 VERIFIED FIXED**, **F39 CONFIRMED (не закрыт в конфиге)**, новая **F40**. Отчёт: [docs/analysis/p5-02-doc-profile-run-analysis.md](docs/analysis/p5-02-doc-profile-run-analysis.md).

**Подтверждено этим прогоном (`[x]` — наблюдалось в артефактах):**

- [x] **F38 VERIFIED FIXED**: codex resume-путь рабочий. `stages/documentation/run-000120/1-codex/request.json` argv — exec-опции (`--cd`/`--sandbox`/`--json`/`--output-last-message`) **до** `resume [SESSION_ID]`, `--model gpt-5.4`/`-c` после; documentation `provider_attempts codex attempt=1 succeeded exit 0` (77s), **без fallback** (в Проходе 15 падал `unsupported_version`).
- [x] **§24 `branch_mode: existing` + PR reuse (повторно)**: p5-02 на `feat/p5-compile` добавил 2-й коммит (`git log`: `feat(p5-02-doc-profile)` над `feat(p5-01-classify-nodes)`), PR #11 переиспользован (ledger `pr_url` тот же, не #12).
- [x] **§13 `worc run` refuse на unmerged `depends_on`** (новое наблюдение, F40): первый запуск p5-02 отказан `dependency 'p5-01' PR is OPEN (unmerged)`, exit 2, задача осталась `pending` — гейт `depends_on` реально срабатывает на PR-OPEN.
- [x] **Кросс-провайдерный fallback codex→claude** повторно штатно спасал supervisor ×6 (F39): `provider=codex process_crashed` → `provider=claude succeeded` на каждом шаге.
- [x] **Checks-гейт**: 4/4 passed; коммит p5-02 в скоупе (4 файла, +386/−4).
- [x] **review-claude accept** с первого раза (`node_runs review outcome=accept`), rework-цикла нет.

**Вскрытые/подтверждённые дефекты:** **F39** — supervisor всё ещё крашит codex (`400: "The 'claude-opus-4-8' model is not supported when using Codex with a ChatGPT account"`, `stdout.log`), т.к. target-конфиг без `supervisor.provider` + preflight не ловит унаследованный мисматч; **F40** — `depends_on` × `branch_mode: existing` конфликт.

**Не наблюдалось:** fix-loop; HITL; decomposition; single-provider=codex; успешный supervisor на codex (F39 не закрыт в конфиге).

---

## Проход 17 — что проверено (2026-07-07), чистый codex-primary (supervisor тоже codex)

Задача `p5-03-describe-rules` → `done`, PR [#11](https://github.com/VladimirMakarevich/wastech-mdlint/pull/11) reuse (3-й коммит), `fix_iterations=0`, ветка `feat/p5-compile`. Конфиг: supervisor зафиксирован на codex (`provider: codex`/`gpt-5.4`/`xhigh`, вариант B). Находки — [TEST-FINDINGS.md](TEST-FINDINGS.md): **F39 закрыт для per-step** (вариант B), новая **F41** (finalize-схема). Отчёт: [docs/analysis/p5-03-describe-rules-run-analysis.md](docs/analysis/p5-03-describe-rules-run-analysis.md).

**Подтверждено этим прогоном (`[x]` — наблюдалось в артефактах):**

- [x] **F39 закрыт согласованной codex-конфигурацией supervisor** (вариант B): 5 per-step supervisor-наблюдений (planning/impl/testing/review/documentation) — codex attempt=1 succeeded, 0 фоллбэков (лог `p5-03-run.log`). Явный `supervisor.provider: codex` + валидная `gpt-5.4` устранили `400 model not supported`.
- [x] **durable-сессия supervisor на codex** (`resume_own_lineage`): codex создавал/возобновлял thread штатно на каждом per-step шаге — нюанс «раньше сессия была claude'овская» не помешал.
- [x] **§12 per-node override**: supervisor-слой уважает свой `provider`/`model`/`reasoning` (codex/gpt-5.4/xhigh) — `route resolved node_id=supervisor primary=codex source=flow_node` + успешные codex-attempt.
- [x] **F38 повторно** (Проход 17): documentation resume на codex succeeded (80s), без fallback.
- [x] **§24 branch_mode: existing + PR reuse (3-й раз подряд)**: p5-03 добавил 3-й коммит на `feat/p5-compile`, PR #11 переиспользован (`533ba7c feat(p5-03-describe-rules)` над p5-02/p5-01).
- [x] **Checks 4/4**, review-claude accept с первого раза; коммит в скоупе (4 файла, +598/−29).

**Вскрытые дефекты:** **F41** — finalize supervisor-summary крашит codex (`invalid_json_schema`: `memory_delta.lessons.items.scope` без `required`-всех-ключей, `DELTA_OUTPUT_SCHEMA`/`_FOLLOW_UPS_SCHEMA` не OpenAI-strict) → fallback claude; класс F24.

**Не наблюдалось:** fix-loop; HITL; decomposition; single-provider=codex; успешный supervisor-**finalize** на codex (F41).

---

## Проход 18 — что проверено (2026-07-07), F41/F24 на 0.8.9a4, глубокий codex-review fix-loop

Задача `p5-04-synthesize` → `done`, PR [#11](https://github.com/VladimirMakarevich/wastech-mdlint/pull/11) reuse (4-й коммит), **`fix_iterations=7`**, ~2ч40м. Конфиг: узлы claude/sonnet-5/xhigh, review codex/gpt-5.4/xhigh, supervisor codex, primary=claude. Находки — [TEST-FINDINGS.md](TEST-FINDINGS.md): **F41 VERIFIED FIXED**, **F24 не воспроизводится**, новая **F42**. Отчёт: [docs/analysis/p5-04-synthesize-run-analysis.md](docs/analysis/p5-04-synthesize-run-analysis.md).

**Подтверждено этим прогоном (`[x]` — наблюдалось в артефактах):**

- [x] **F41 VERIFIED FIXED**: finalize supervisor на codex — `stages/supervisor/run-000000/1-codex/result.json` `succeeded exit 0` (90s), нет `2-claude/` (без fallback); `"task finalize: supervisor summary written"`. memory_delta записан codex-супервизором (`supervisor_final` `memory_delta:true`; `memory_write` append `ep_p5-04-synthesize` + entities `core-synthesize`/`core-compile-context`/`core-skill-frontmatter`/`llm001-rule`).
- [x] **F24 не воспроизводится**: codex-evaluator (review) 8 попыток подряд `succeeded` (`provider_attempts` review/codex ×8), 0 `process_crashed`/`invalid_json_schema` (в p4 было 9/9 crash). codex-ревью исполняется и даёт содержательные вердикты.
- [x] **Полноценный review-fix-loop end-to-end** (первый глубокий в кампании): 7×`rework`→`accept`, `node_runs` чередует review(codex)/fixing(claude)/testing; `fix_iterations=7`, все чеки зелёные. Кросс-провайдерный цикл (codex судит → claude чинит) работает.
- [x] **§12 раскладка применилась**: planning/impl/fixing/documentation на claude (`provider_used=claude`), review на codex (`provider_used=codex`), supervisor на codex — по `node_runs`/`route resolved`.
- [x] **F38** повторно: documentation resume на codex? (в этом прогоне documentation шёл на **claude** — узел перепинен; resume-путь codex наблюдался на supervisor per-step ×многие, все succeeded).
- [x] **§24 PR reuse (4-й коммит)** на `feat/p5-compile` (`f02b6b0 feat(p5-04-synthesize)`).

**Вскрытые дефекты:** **F42** — codex-review чрезмерно дотошен (7 rework, дрейф корректность→тест-полировка, ~2ч40м, тесты +802); мелочь — `tasks.review_fix_cycles=0` при 7 реворках (не персистится; `fix_iterations` корректен).

**Не наблюдалось:** HITL; decomposition; single-provider=codex; test_fix-цикл (чеки ни разу не падали — все fix'ы review-driven).

---

## Проход 19 — что проверено (2026-07-07), стабильность F41/F24 + review reasoning=high

Задача `p5-05-compile-config-cli` → `done`, PR [#11](https://github.com/VladimirMakarevich/wastech-mdlint/pull/11) reuse (5-й коммит), **`fix_iterations=1`**. Конфиг как в Проходе 18, но review = **codex/gpt-5.4/high** (снижен reasoning). Без новых F. Отчёт: [docs/analysis/p5-05-compile-config-cli-run-analysis.md](docs/analysis/p5-05-compile-config-cli-run-analysis.md).

**Подтверждено этим прогоном (`[x]` — наблюдалось в артефактах):**

- [x] **F41 стабилен**: finalize supervisor на codex `succeeded exit 0` (`run-000000/1-codex`, без `2-claude/`), memory_delta записан codex-супервизором.
- [x] **F24 стабилен**: codex-review 2/2 succeeded (rework→accept), 0 крашей; **0 фоллбэков во всём прогоне** (`provider_attempts`: planning/impl/fixing/documentation=claude, review=codex — все succeeded).
- [x] **Короткий review-fix-loop**: 1 rework (реальный `--cwd`-баг, `compile` не учитывал `--cwd` для относительного `--config`) → fixing (claude) → accept. `fix_iterations=1`.
- [x] **§24 PR reuse (5-й коммит)** на `feat/p5-compile`; коммит в скоупе (config-schema strict `compile`, CLI compile, тесты; 13 файлов +420/−166).
- [x] **F42 — review reasoning как регулятор**: review на `high` → loop 1 цикл / проходы 171–223s (против 7 циклов / 300–800s у p5-04 на xhigh). Направление согласуется с рычагом F42 (не чистый A/B — задача меньше).

**Не наблюдалось:** HITL; decomposition; single-provider=codex; test_fix; supervisor-фоллбэк (весь оверсайт на codex чисто).

---

## Проход 20 — что проверено (2026-07-07), ФИНАЛ фазы P5

Задача `p5-06-compile-tests` → `done`, PR [#11](https://github.com/VladimirMakarevich/wastech-mdlint/pull/11) reuse (6-й/последний коммит фазы), **`fix_iterations=1`**. Конфиг как в Проходе 19. Без новых F. Отчёт: [docs/analysis/p5-06-compile-tests-run-analysis.md](docs/analysis/p5-06-compile-tests-run-analysis.md).

**Подтверждено этим прогоном (`[x]`):**

- [x] **F41 стабилен** (3-й прогон подряд): finalize supervisor на codex `succeeded exit 0` (`run-000000/1-codex`, без `2-claude/`).
- [x] **F24 стабилен**: codex-review 2/2 succeeded (rework про тавтологичный CJK-budget тест → accept), 0 крашей; **0 фоллбэков во всём прогоне**.
- [x] **Короткий review-fix-loop** (как p5-05, review=high): 1 rework → fixing (claude) → accept; `fix_iterations=1`.
- [x] **§24 PR reuse — вся фаза P5 в одном PR #11**: `git log main..feat/p5-compile` = 6 коммитов (p5-01…p5-06); коммит p5-06 в скоупе (5 файлов, +96/−8, тесты+фикстуры+docs).
- [x] **Ревью по существу на тест-задаче**: поймало тавтологичный ассерт (`expected` через тот же `estimateTokens`, что и SUT) — качественная тест-находка.

**Итог кампании codex-primary (проходы 15–20):** F38/F39/F41/F24 закрыты и подтверждены; codex — полноценный primary во всех ролях. Открыто (не блокеры): F42, review_fix_cycles counter, F39-preflight, F40.

**Не наблюдалось за кампанию:** HITL-пауза оператора; decomposition; single-provider=codex (only-codex режим); MANUAL_ACTION_REQUIRED; test_fix-цикл (чеки ни разу не падали).

---

## 1. Branch name: epoch-префикс + ограничение длины

**Файл:** `archive/done/branch-name-epoch-and-slug-limit.md` · **Статус:** implemented 2026-06-26

Новая формула имени ветки `{prefix}/{epoch}-{task_id}-{slug}` с общим лимитом 50 символов и мягкой деградацией для оператора.

- [ ] `parser.py:slugify_bounded(value, max_len)` — вызывает `slugify()`, затем усечение до `max_len` и удаление хвостовых дефисов
- [ ] `slugify_bounded` возвращает `""` (а не `"task"`) при `max_len <= 0`, чтобы вызывающий код мог опустить сегмент slug
- [x] `git_manager.py:branch_name(epoch=...)` — строит фиксированный префикс `f"{branch_prefix}/{epoch}-{task_id}"` — прогон 2: ветка `worc/1783012905-p0-01-workspace-decisions-...`
- [x] Остаточный бюджет `= 50 - len(fixed)`; сегмент `-{slug}` добавляется только когда бюджет позволяет (через `slugify_bounded(slug, budget - 1)`) — прогон 2: slug усечён до `p0-01-fi`, чтобы уложиться в 50
- [x] Общая длина авто-имени не превышает 50 символов — прогон 2: `len == 50` ровно
- [ ] Когда `prefix + epoch + task_id >= 50`, сегмент slug полностью опускается (без падения)
- [x] `orchestrator.py:_prepare_branch()` — захватывает `int(time.time())` один раз и прокидывает epoch в `git.prepare_branch(...)` → `branch_name(epoch=…)` — прогон 2: epoch `1783012905` присутствует в имени ветки
- [ ] Перезапуск той же задачи (тот же `task_id`) даёт РАЗНОЕ имя ветки (epoch отличается) — нет коллизии `git checkout -b`
- [ ] Восходящий порядок epoch совпадает с `git branch --sort=creatordate`
- [ ] `validation_gate.py:_branch_name()` — операторский `branch_name` длиной > 50: `logger.warning(...)` + сброс `p.task.branch_name = None` → авто-генерация (без ошибки валидации)
- [x] Операторский `branch_name` длиной <= 50 используется как есть (оператор отвечает за уникальность) — прогон 2: p0-02 `branch_name: "feat/p0-monorepo-workspace"` использован дословно (не `worc/{epoch}-...` авто-паттерн)
- [ ] Жёсткий предел 255 UTF-8 байт (`model.py:BRANCH_NAME_MAX_BYTES`) остаётся hard error для любого источника имени
- [ ] Кросс-платформенность: имя ветки корректно строится/сравнивается на Windows/Linux/macOS

---

## 2. Configurable tasks directory (`paths.tasks_dir`)

**Файл:** `archive/done/configurable-tasks-dir.md` · **Статус:** implemented 2026-06-26

Корневая директория задач (`tasks/`) становится конфигурируемой через `paths.tasks_dir` (относительный путь внутри репо).

- [ ] `config/schema.py:PathsConfig` — dataclass с `tasks_dir: str = "tasks"`, встроен в `OrchestratorConfig`
- [ ] `config/loader.py` читает `paths.tasks_dir`, валидирует через `is_safe_relpath` (нет абсолютного пути, нет `~`, нет `..`)
- [ ] Доп. guard: значение не может равняться `.worc/` или лежать внутри него (иначе gitignore тихо ломает audit trail)
- [ ] Разрешены относительные подпути: `tasks`, `.tasks`, `worktasks`, `config/tasks`
- [ ] Path traversal (`../`) и абсолютные/ведущий `/` пути отклоняются на этапе загрузки конфига
- [ ] Bump версии конфига (если требуется) корректно обрабатывается
- [ ] `install/config_writer.py` всегда пишет дефолт `paths.tasks_dir: tasks` и создаёт `tasks/` (RESOLVED: без интерактивного prompt и без CLI-флага)
- [ ] `--reconfigure` регенерирует дефолт как и остальные значения
- [ ] `cli.py` — `REPO_TASK_DIRS`, `tasks_root_for`, `pending_dir` берут значение из загруженного конфига, а не из литерала `"tasks"`
- [ ] `orchestrator.py:_relocate_task_file` / `_resolve_task_source` — корень задач выводится структурно (walking `parent.parent` от lifecycle-папки), согласован с конфигом
- [ ] `git_manager.py:EXCLUDED_DIRS` — на уровне инстанса (или передаётся при конструировании), совпадает с настроенным именем директории
- [ ] Lifecycle-подпапки (`pending`/`processing`/`done`/`failed`) остаются захардкоженными — НЕ конфигурируемы
- [ ] Git-ignore деградация: `_relocate_task_file` использует `src.replace(dest)` (только ФС, без git)
- [ ] Git-ignore: `commit_audit` guard (`if stageable and self._git("add", ...).ok:`) ловит non-zero код для gitignored пути → `sha = None`, операция пишется как `"noop"` (без падения)
- [ ] `state.db` audit фиксирует каждый lifecycle-переход независимо от git-игнора

---

## 3. Task discovery: `worc list` + shell completion

**Файл:** `archive/done/cli-task-list-and-completion.md` · **Статус:** implemented 2026-06-27

Read-only команда перечисления задач `worc list` + генератор скрипта автодополнения `worc completion bash|zsh` (обёртка над `list`).

### `recent_tasks` (read-хелпер)

- [ ] `state_store.py:recent_tasks(limit) -> list[TaskRow]` — последние N терминальных задач по `updated_at`
- [ ] Только терминальные статусы (`done`/`failed`/`manual_action_required`); корректный порядок и лимит
- [ ] Без bump схемы `state.db`

### `worc list` (`cmd_list`)

- [ ] По умолчанию (без флагов) — три секции: active (`find_active_tasks`), pending (`select_pending` + `_scan_pending_meta`), recent (`recent_tasks`)
- [x] `--pending` — только очередь `tasks/pending`
- [ ] `--recent [N]` — только недавние терминальные (дефолтный N)
- [ ] `--all` — все известные задачи (строки БД по всем статусам)
- [ ] `--format table` (человекочитаемо, дефолт) / `--format ids` (по одному `task_id` в строке) / `--format json` (структурировано)
- [ ] `--format ids` печатает ТОЛЬКО id в stdout, ошибки/заметки — в stderr
- [ ] `--scope rerun` → только rerun-eligible (`failed`/`manual_action_required`)
- [ ] `--scope status` → любой известный id
- [ ] `--scope finalize` → набор для finalize (по факту совпадает со `status`, т.к. finalize status-agnostic — задокументированное отклонение)
- [x] Чтение через `StateStore.open_readonly` (безопасно при активном daemon), без мутаций
- [ ] Edge: нет задач вообще → однострочное «нет задач», exit 0
- [ ] Edge: `.worc` не инициализирован → сообщение «run `worc install`»
- [ ] Edge: pending-файл без парсящегося id → показан по имени файла
- [ ] Edge: `--format ids` без совпадений → пустой stdout, exit 0
- [x] Рендерит только `task_id`/`status`/`title`/`branch` (без секретов), не пишет собственных логов

### `worc completion bash|zsh` (`cmd_completion`)

- [ ] Печатает статический completion-скрипт в stdout (паттерн kubectl/gh)
- [ ] Дополняет имена субкоманд и флагов статически
- [ ] id-позиционные дополняются динамически: вызов `worc list --format ids --scope <command>` через `compgen`
- [ ] `rerun <Tab>` → rerun-eligible id; `status <Tab>` → любой id; `finalize <Tab>` → finalize-eligible id
- [ ] `run <Tab>` → файлы `tasks/pending/*.{md,json}` (или fallback на path-completion шелла)
- [ ] Оба варианта (`bash`/`zsh`) печатают непустой валидный скрипт, содержащий вызов `worc list --format ids`
- [ ] Скрипт статичен; единственный динамический вход — вывод `worc list --format ids` (charset `task_id`), нет риска инъекции
- [ ] Без новой runtime-зависимости (нет `argcomplete`)

---

## 4. HITL session resume & planning autonomy

**Файл:** `archive/done/hitl-session-resume-and-autonomy.md` · **Статус:** P0 done (in-process resume), P2 — только документация

HITL-раунд-трип теперь ВОЗОБНОВЛЯЕТ ту же сессию агента после ответа оператора, а не создаёт новую. Всё в `core/flow/nodes/agent.py`.

### #1 Session resume на HITL re-entry (P0)

- [ ] `_run_with_hitl` — после ответа прокидывает `session_id` первого запуска (`outcome.result.session_id`) во второй `_invoke`
- [ ] `_hitl_resume_session_id` — гейтит resume тем же провайдером (`outcome.provider_used == route.primary`)
- [ ] `_invoke` / `_build_request` принимают `resume_session_id`, который побеждает editing-lineage lookup
- [ ] Resume работает для ЛЮБОГО `session_scope` и ЛЮБОГО узла (не только `editing_lineage`) — обходит ограничение `_resume_session_id`
- [ ] Провайдерный гейт: нельзя возобновить Claude-сессию на Codex (и наоборот)
- [ ] Второй запуск узла имеет `result.session_id` РАВНЫЙ id первого запуска (регресс `test_agent_hitl_round_trip_resumes_first_run_session`)
- [ ] В `request.json` второго вызова присутствует `--resume`-style session id; агент продолжает диалог, не перечитывая всё заново
- [ ] Across-restart fallback: session id первого запуска потерян (`fresh_disposable` без durable-слота) → честный откат на свежую сессию + файл контекста ответа; без новой persistence, без bump `state.db` (`test_agent_hitl_round_trip_no_resume_when_first_run_used_fallback`)
- [ ] `_reconsider` (re-run при отказе по опасному диффу) возобновляет `editing_lineage` через существующий `_resume_session_id` — изменений не требовал (`test_dangerous_diff_reconsider_resumes_editing_lineage`)
- [ ] Инвариант: HITL остаётся data-driven через флаги `hitl` (нет спец-кейса по имени стадии); state machine и «commit/push/PR только оркестратор» не меняются

### #2 Autonomy policy (P2 — документация, кода нет)

- [ ] Проверить, что политика эскалации задокументирована: `task-authoring.md → Planning escalation and unattended runs` и `operations.md → Running` (auto_mode управляет последовательностью, не HITL)
- [ ] Кодовых knobs в этом раунде НЕТ (per-node HITL timeout и bounded-wait auto-proceed отложены в follow_ups)

---

## 5. HITL-wait observability + очистка prompt при не-декомпозиции

**Файл:** `archive/done/hitl-wait-observability-and-prompt-cleanup.md` · **Статус:** done 2026-06-27

Два независимых полировочных пункта: видимость ожидания HITL в логе + условный блок в prompt-рендерере.

### #1 Сигнал ожидания HITL (`HumanGate.request`/`resume`)

- [ ] `core/flow/nodes/human_gate.py` — на входе в блокирующее ожидание пишется entry-строка `awaiting human input`
- [ ] Heartbeat `awaiting human input heartbeat` на каждый тик (переиспользует `run_with_heartbeat`)
- [ ] На выходе — строка `human input resolved` со статусом резолюции (`answered`/`timeout`/`failed`)
- [ ] Интервал = orchestrator-wide `--heartbeat-seconds`, прокинут через `NodeServices.ask_heartbeat_seconds`
- [ ] Без секретов: только ids/kind/timeout (не текст вопроса и не креды)
- [ ] Acceptance: run, заблокированный на HITL, показывает ≥1 info-строку в логе + периодический heartbeat + строку резолюции

### #2 Условный блок subtask в prompt (`core/prompts.py`)

- [x] Безопасный рендерер получил обратно-совместимый условный блок `{?name}…{/name}`
- [x] Пакетные роли `implementation`/`fixing` оборачивают subtask-клаузу в `{?subtask_spec_path}…{/subtask_spec_path}`
- [x] Не-декомпозированный run: в `rendered-prompt.md` НЕТ висячей фразы «subtask of …» (пустые плейсхолдеры не рендерятся)
- [ ] Декомпозированный run: по-прежнему корректный текст «subtask N of M» (поведение не изменилось)

---

## 6. Interactive operator console (`worc top` / `worc shell`) + stop-ladder

**Файлы:** `archive/done/cli-upgrade.md` + `cli-upgrade-remediation.md` · **Статус:** implemented; R1 done; R2/R3 в follow_ups; Windows-smoke ожидается

Операторская консоль как клиент над демоном `watch` (не второй хост движка). Реализованы `worc top`, `worc shell` и трёхступенчатая стоп-лестница; R1 исправил критический дефект жёсткого стопа.

### Phase 1 — `worc top` (read-only live-монитор)

- [ ] `cmd_top` / `_run_top_loop` / `render_top` — периодический read-only опрос `state.db`
- [ ] `build_top_snapshot` использует `scan_pending_sorted(pending_dir, selector)` — фильтр по обслуживаемой очереди + сортировка по приоритету КАК в `watch_once` (не сырой `select_pending`)
- [ ] `_ActiveView.parked_since` — маркер «parked» при `running` + ненулевом `blocked_since` («paused — every provider unavailable since …»)
- [ ] `_has_pending_gate` через `iter_task_interactions` — маркер ждущего durable max-turns gate; non-durable next-task gate НЕ показывается (правильно)
- [ ] Переиспользование `store.recent_tasks(recent_limit)` (не добавляется заново)
- [ ] `tail_lines` — хвост лог-файла демона; `_stdin_quit_watcher` — выход по `q`

### Phase 2 — `worc shell` (prompt_toolkit REPL)

- [ ] `cli_shell.py:dispatch` — только диспетчер, форвардит на существующие `cmd_*` (без логики оркестрации)
- [ ] `[shell]` optional extra (`prompt_toolkit>=3`), ленивый импорт, `_prompt_toolkit_available()` guard, чистый fallback «install the extra»
- [ ] `spawn_or_attach_watch` → `spawn_detached` (argv-список, `shell=False`, stdin `DEVNULL`), прокидывает `--log-file` и `--queue`
- [ ] `enqueue`/`run <file>` — копирование в config-resolved `pending_dir(config)`, неблокирующе
- [ ] Slot-guard мутационных команд (делегируется в `cmd_*`); `merge-task`/`prs --sync` отказывают, пока жив PID демона
- [ ] `cancel <id>` — pending-файл уезжает в `.worc/tasks/rejected`, иначе маршрут в стоп-лестницу
- [ ] prompt_toolkit-loop тестируется через инъекцию списка строк (не реальный TTY)

### Phase 3 — стоп-лестница (idle/busy × `--force`/`--force-full`)

- [x] `_resolve_stop_level` — решение: idle→стоп без промпта; busy+no-flag→refuse; `--force`/`YES`→soft; `--force-full`→hard — прогон 3: наблюдались ветки **idle→стоп** (p0-02 manual → `worc stop` → «watcher 77007 stopped») и **busy+no-flag→refuse** (p0-02 running → `worc stop` → «a task is active; pass --force»); ветки `--force`/`--force-full` не тестировались
- [ ] `_gated_stop` используется и `cmd_stop`, и `cmd_restart`; консольный `down`→`stop`
- [ ] Soft: `_stop_via_signal` (POSIX SIGTERM→SIGKILL) / `_stop_via_pid_file` (Windows ждёт удаления PID-файла); `_can_signal` split; стоп-файл `orchestrator.stop` на обеих платформах
- [x] idle → стоп без подтверждения (все три формы); busy без флага + не-TTY → refuse, ничего не сигналится — прогон 3 (только форма `worc stop`, non-TTY): idle → чистый стоп без промпта; busy → refuse, демон (pid 77005/77007) НЕ тронут (подтверждено pgrep после). Формы `restart`/консольный `down` не тестировались
- [ ] `stop`/`restart` получили `--force`/`--force-full` (взаимоисключающие) рядом с `--timeout`

### R1 — исправление жёсткого стопа (C1) — «fully implemented» работа

- [ ] **POSIX топология инвертирована**: `run_process` больше НЕ делает `setsid` (агент наследует группу демона)
- [ ] `spawn_detached` ставит `start_new_session=True` на POSIX (консоль-порождённый демон лидирует свою группу)
- [ ] `cmd_watch` вызывает `process_control.ensure_own_process_group()` (демон — лидер группы, без отсоединения controlling terminal — foreground `Ctrl-C` работает)
- [ ] `killpg(getpgid(daemon_pid))` убивает демон + активного агента + checks-субпроцесс разом, ничего не осиротеет, нет collateral вне группы демона
- [ ] Поведенческое изменение: foreground `Ctrl-C` на `worc watch` теперь доставляет `SIGINT` агенту (он умирает mid-stage, не осиротеет) — задокументировать
- [ ] **Windows**: `agent_process.hard_kill_tree(pid)` → `["taskkill","/F","/T","/PID",str(pid)]` (argv, `shell=False`), терпит «no such process» exit
- [ ] Windows `level="full"` больше не деградирует в soft: hard-kill дерева через инъектируемый `hard_kill_fn` seam; `process_control.py` сохраняет no-subprocess инвариант (сам `taskkill` не зовёт)
- [ ] `StopOutcome.tree_killed` флаг; `degraded_to_soft` только когда `hard_kill_fn` не передан (defensive fallback)
- [ ] `cmd_stop`/`cmd_restart` сообщения: Windows tree-kill рапортует «hard-stopped (killed its process tree)»
- [ ] Тест real-topology POSIX: родитель порождает ребёнка в общей группе; `stop_process(level="full")` реапит ОБОИХ через настоящий `os.killpg`
- [ ] Тест: `spawn_detached` ставит `start_new_session=True` на POSIX, а `run_process` — НЕТ (перехват kwargs)
- [ ] Тест Windows-пути: `can_signal=False` + фейковый `hard_kill_fn` → вызван с PID демона, outcome = tree-kill (не `degraded_to_soft`); `hard_kill_tree` строит `taskkill /F /T /PID` argv с `shell=False`
- [ ] **AC-R1a (POSIX)**: `--force-full`/`down --force-full` при активной задаче убивает демон И агента, ничего не осиротеет, не сигналит вне группы демона
- [ ] **AC-R1b (Windows)**: hard-kill дерева демона через `taskkill` seam (без soft-degrade); ⚠️ реальный Windows-smoke ОЖИДАЕТСЯ (owner) перед доверием в prod

### Известные незакрытые пробелы (R2/R3 — в follow_ups, не исправлены)

- [ ] H1: консольный `down` (busy, no flag, TTY) падает в `input()` внутри REPL — нарушает «один читатель stdin» (R2)
- [ ] M1: `dispatch` использует `shlex.split` POSIX-mode → ломает Windows абсолютные пути в `enqueue`/`cancel` (R2; 2 теста падают на Windows)
- [ ] M2: attach без `--log-file` показывает «(no output)» — демону нужен дефолтный лог-путь (R3)
- [ ] M3: quit при busy+spawned просто оставляет демон работать без предложения detach/soft/force-full (R3)
- [ ] L5: `docs/functional/` не содержит блока про `top`/`shell`/стоп-лестницу (R3)

---

## 7. Log management: `worc logs clean` + `logging.*` config

**Файл:** `archive/done/log-management.md` · **Статус:** implemented 2026-06-27 (config schema_version 23)

Команда очистки артефактов логов + конфиг-ключи уровня логирования и объёма артефактов.

### `worc logs clean`

- [ ] `worc logs clean` — промпт `Are you sure? [y/N]`, затем удаляет все task-директории артефактов под `.worc/logs/`; `completed.jsonl` (ledger) сохраняется по умолчанию
- [ ] `worc logs clean --keep N` — оставляет N последних по mtime (`os.path.getmtime`), удаляет остальные; без подтверждения (явный счётчик = ясное намерение)
- [ ] `worc logs clean --keep 0` — подтверждает как bare delete-all (LOCKED-решение)
- [ ] `worc logs clean --all` — удаляет всё, включая ledger `completed.jsonl`; требует подтверждения независимо от других флагов
- [ ] Ledger исключается по умолчанию (через ссылку на собственный путь класса `Ledger`)
- [ ] Сортировка task-dirs по mtime, `shutil.rmtree` для тех, что вне `--keep`
- [ ] Безопасно при idle `watch`; во время активной задачи — undefined/unsupported (не жёсткий guard в v1)

### `logging.level`

- [ ] `logging.level` (`debug|info|warning|error`, дефолт `info`) персистит verbosity оператора
- [ ] CLI-флаг `--log-level` переопределяет ключ (мёрдж на call site, не в loader)
- [ ] Читается в `configure_logging()`

### `logging.artifacts`

- [ ] `logging.artifacts` (`minimal|standard|full`, дефолт `standard`) управляет per-attempt файлами под `.worc/logs/<task-id>/stages/`
- [ ] `minimal` → только `result.json` (СТРОГО — даже при падении, нет errors-only исключения; LOCKED)
- [ ] `standard` → `stdout.log`, `stderr.log`, `result.json`
- [ ] `full` → `events.jsonl`, `request.json`, `before.diff`, `after.diff`, `result.json`, `stdout.log`, `stderr.log`
- [ ] Область действия — только per-attempt provider-файлы; prompt-audit по-прежнему управляется ключом `prompt_audit` (независимо)
- [ ] `minimal` НЕ ломает логику оркестратора, читающую собственные артефакты (парсинг результата)
- [ ] Config `schema_version` bump до 23
- [ ] Инвариант: редакция секретов держится на ВСЕХ уровнях verbosity (`RedactionFilter` не обходится)

---

## 8. Task queue tags для нескольких worc-инстансов (`queue`)

**Файл:** `archive/done/multi-instance-task-queues.md` · **Статус:** implemented 2026-06-26

Статическое партиционирование пула задач по строковому тегу `queue` + селектор инстанса — чтобы несколько worc-инстансов на одном git-пуле не хватали одну задачу.

- [ ] Task-модель: поле `queue` (дефолт `default`, непустая строка, fail-closed на malformed — задача отклоняется, не дефолтится молча)
- [ ] `_scan_pending_meta` (`cli.py`) дополнительно читает `queue` (eligibility остаётся дешёвым чтением)
- [ ] `select_pending()` / `watch_once()` — отбрасывают pending-задачи, у которых `queue != instance selector` (плейн string equality)
- [ ] Оба defaults = `default`: пул без тегов + инстанс без селектора ведёт себя как раньше; untagged-задача попадает в `default` и берётся только `default`-инстансом
- [ ] Конфиг-ключ `orchestrator.queue` (дефолт `default`) — loader, валидация, bump config-schema, install-шаблоны/`config_writer`
- [ ] CLI `--queue` на `worc watch` переопределяет значение конфига
- [ ] Инвариант single-active-task остаётся ПО-ИНСТАНСНЫМ (не релаксится)
- [ ] Нет нового статуса задачи, нет schema-миграции
- [ ] Same-selector: два инстанса с одним селектором на одном пуле ВСЁ РАВНО коллизят — «one worc per queue» это operator-enforced инвариант (guard'а нет — resolved)
- [ ] Cross-queue `depends_on`: out-of-queue задачи невидимы; зависимый в `alpha` с зависимостью в `beta` остаётся WAITING до merge beta (детекции нет — только документация)
- [ ] Decomposition inheritance: сабтаски идут в пайплайне родителя на его ветке, не проходят `watch_once` pending-selection → наследуют `queue` родителя (нет per-subtask `queue`)
- [ ] `worc install`: шаблон сеет `orchestrator.queue: "default"`, без prompt

---

## 9. Operator confirmation gates в autonomous mode

**Файл:** `archive/done/operator-confirmation-gates.md` · **Статус:** implemented (config v22)

Два чекпоинта одобрения оператором через durable Telegram-гейт: перед взятием следующей задачи (idea 27) и при исчерпании turn-бюджета агента (idea 29). Оба off by default, fail-closed.

### Next-task gate (idea 27)

- [ ] `orchestrator.auto_mode.confirm_next_task: bool` (дефолт `false`)
- [ ] Гейт в `cli.watch_once`: при `auto_mode.enabled` + новой pending-задаче перед claim шлёт approve/deny prompt (task id + title)
- [ ] Approve → claim и run; Deny → оставить pending и прекратить chaining в этом цикле
- [ ] Гейтит ТОЛЬКО claim новой задачи; resume уже in-flight задачи при restart демона НЕ гейтится
- [ ] Гранулярность per-task (один prompt на следующую задачу)

### Max-turns gate (idea 29)

- [ ] `agents.providers.<id>.max_turns_gate: bool` (дефолт `false`), рядом с `max_turns`, резолвится orchestrator-side
- [ ] `error_max_turns` surface'ится как СТРУКТУРНОЕ поле (`failure_subtype`), а не substring-match в сообщении `NormalizedError`
- [ ] Перехват в agent-node runner через `_invoke_with_turn_gate` (не в `providers/`), ДО терминальной классификации
- [ ] Continue → orchestrator возобновляет ту же сессию агента со свежим turn-grant (durable-resume path); grant переиспользует настроенный `max_turns` (нет отдельного grant-ключа в v1)
- [ ] Stop → run терминируется как сегодня
- [ ] `max_turns` — Claude-only (у Codex нет turn cap); Claude-адаптер добавляет `--max-turns` при каждом invocation независимо от `--resume` → resumed-сессия получает свежий бюджет (⚠️ эмпирическая проверка против реального аккаунта рекомендована)
- [ ] Нет hard resume cap в v1 (каждый continue — новое одобрение + timeout→STOP ограничивают unattended loop)

### Fail-safe posture (оба гейта)

- [ ] Preflight (`config/validation.py`): гейт включён при `telegram.enabled == false` → валидация падает, старт отклоняется (не тихий no-op)
- [ ] Timeout (`telegram.ask_timeout_s`) → безопасный дефолт STOP: не claim'ить следующую задачу (27); не продолжать жечь turns, терминировать run (29)
- [ ] Оба переиспользуют durable HITL (`ask_human`, durable «waiting» артефакт, resume-across-restart) — без нового транспорта/persistence
- [ ] Без секретов в сообщениях гейта (только task id/title и node id)
- [ ] Schema version bump до v22
- [ ] Тесты через fake-CLI фикстуры (эмиссия `error_max_turns` терминального стрима): гейт вызывается; continue возобновляет; deny/timeout стопает; preflight отклоняет gate-on-without-telegram

---

## 10. Orchestrator-driven PR merge (`worc prs` / `worc merge-task`)

**Файл:** `archive/done/orchestrator-driven-pr-merge.md` · **Статус:** implemented 2026-06-27

Операторский merge orchestrator-созданного PR: подтянуть базу, разрешить конфликты (agent-assisted), смержить + CLI-обзор PR. **Важное отклонение от locked-решения:** разрешение конфликтов реализовано как оператор-редактируемый **`merge` FLOW** (Option B), а не рутина (Option A).

### Phase 1 — read surface: `worc prs` / `worc tasks`

- [x] `state_store.py:find_open_pr_tasks() -> list[TaskRow]` — задачи с завершённым `pr`-op и без `pr_merge` (read-only, без bump схемы) — прогон 2/3: `prs --sync` (dry-run) вернул ровно p0-01 + lint-cmd-alias (у обоих есть `pr`-op, нет `pr_merge`; последнее подтверждает F11 — `pr_merge` не пишется в watch-режиме)
- [x] `cmd_prs` (default) — `open_readonly`, DB-only печать; колонки `task_id`/`title`/`status`/`branch`/`pr_url` — прогон 2/3: `worc prs` при живом демоне напечатал p0-01 + lint-cmd-alias со `status`/`branch`/`pr_url` (read-only, демон не тронут)
- [ ] `worc prs --check` — обогащение живым состоянием GitHub через `verify_pr_state`/`pr_merge_state` (единственный сетевой режим)
- [x] `worc prs --sync` (dry-run по умолчанию) — reconcile внешне-смерженных PR; печатает план, ничего не пишет — прогон 2/3: `worc prs --sync` → «[dry-run] p0-01…: would record merge», «re-run with --yes to write»; работает даже при живом демоне (только `--yes` блокируется, F11)
- [ ] `worc prs --sync --yes` MERGED → пишет `pr_merge` publish-op + финализирует `manual_action_required`→`DONE` через `finalize`-путь
- [ ] `--sync --yes` CLOSED (не merged) → только рапорт, статус не меняется; OPEN → пропуск
- [ ] `--sync` идемпотентен: задача с уже записанным `pr_merge` не в `find_open_pr_tasks` → no-op
- [ ] `worc tasks` + `cmd_tasks` — вся таблица со `status`/`branch`, фильтр `--status`

### Phase 2 — GitManager (fixed argv, без shell)

- [ ] `update_branch_with_base(branch, base) -> bool` — checkout ветки, `fetch origin`, `git merge origin/<base>`; возвращает наличие конфликтов
- [ ] `merge_in_progress() -> bool` — `git rev-parse -q --verify MERGE_HEAD`
- [ ] `commit_merge_resolution(task_id, message) -> str | None` — стейджит разрешённые пути, финализирует мерж-коммит (отличен от `commit_code`, т.к. включает изменения базы; идемпотентность через `publish_operations`)
- [ ] `merge_abort()` — `git merge --abort` (идемпотентно, no-op без `MERGE_HEAD`)
- [ ] Тесты через временный git-репо: чистый мерж; конфликт → маркеры → `merge_abort` восстанавливает дерево; идемпотентность `commit_merge_resolution`

### Phase 3 — `merge_task` рутина + merge FLOW + CLI

- [ ] `orchestrator.py:merge_task(task_id, *, strategy, wait_for_checks, resolve, dry_run)` рядом с `_auto_merge`
- [ ] Refuse при активной задаче (single slot); resolve PR через `recorded_pr_url` (refuse если нет)
- [ ] `verify_pr_state`: `MERGED` → идемпотентный успех; `CLOSED`/нет → refuse; `OPEN` → дальше
- [ ] Safety-only гейт: запуск команды = «добро»; жёсткие гейты только «PR открыт» + protection ветки (`merge_pr` БЕЗ `--admin`)
- [ ] `--dry-run` → печать плана (PR, отставание ветки, возможен ли чистый мерж), ничего не пишет
- [ ] Чистый мерж → `push` + `merge_pr` (форма вызова `_auto_merge`) + запись, без агента
- [ ] Конфликт + `resolve` → запускается **`merge` flow** (`git.merge_flow`, дефолт `merge`, seeded `.worc/flows/merge.yaml`), эфемерный engine-run, БЕЗ checkpoint; агент правит файлы
- [ ] Оркестратор проверяет отсутствие маркеров, `commit_merge_resolution`, перезапуск Check Runner; pass → `push`+`merge_pr`+запись
- [ ] Fail/маркеры → ограниченный fixing-проход (cap 1) → ре-тест → всё ещё fail → bail
- [ ] `--no-resolve` или bail → `merge_abort`, PR открыт, exit non-zero; `DONE` НЕ понижается в `FAILED`; `manual_action_required`→`DONE` при успехе
- [ ] Guard на старте рутины и при восстановлении: `merge_in_progress()` без активной задачи → `merge_abort` (зачистка после краша); весь конфликтный путь транзакционный (`merge_abort` в `finally`)
- [ ] Выделенный role-prompt резолвера, доставляемый редактируемой копией под `.worc/`
- [ ] `cmd_merge_task` (subcommand `merge-task <id>`): `--strategy {merge,squash,rebase}` (дефолт `git.auto_merge_strategy`), `--wait-for-checks/--no-wait-for-checks` (дефолт `git.auto_merge_wait_for_checks`), `--no-resolve`, `--dry-run`, `-y/--yes`
- [ ] Интеграция через fake-CLI: (а) чистый мерж; (б) конфликт→агент резолвит→зелёные проверки→смерж; (в) агент не справился/красные проверки→`merge_abort`, PR открыт, non-zero, статус не понижен; (г) идемпотентность (PR уже MERGED); (д) refuse при активной задаче; (е) `--dry-run` ничего не пишет; (ж) `--no-resolve` на конфликте→сразу abort

### Инварианты и связи

- [ ] Commit/push/PR/merge только оркестратор (агент лишь правит файлы при конфликте)
- [ ] Без нового статуса задачи, без bump схемы (переиспользуются `tasks` + `publish_operations`)
- [ ] Успешный `merge-task` / `prs --sync` питает `depends_on`: `pr_merge_state`=`MERGED` → зависимые становятся eligible
- [x] `merge-task`/`prs --sync` отказывают, пока жив PID демона (`running_daemon_pid`) — прогон 2: `worc prs --sync --yes` → «the watch daemon is running (pid 77007); stop it first» (dry-run `--sync` при этом работает); см. F11 — цепочка всё равно авто-продвигается по live-проверке PR демоном
- [ ] Deferred: path/area-based коллекция флоу `git.merge_flows` (как `checks.command_sets`) — не реализована

---

## 11. Skills selection rework: operator-pinned + supervisor-proposed

**Файл:** `archive/done/skills-selection-rework.md` · **Статус:** implemented 2026-06-27 (config v19)

Выбор repo-скиллов уходит с узла `planning` на два слоя: операторские пины per-node (статические) + опциональное once-per-task предложение супервизора (Core решает). Model A сохранена (скилл = read-only reference путь, не native tool, не исполняется).

- [ ] **Discovery**: `SkillInventoryScanner` — whole-repo `git ls-files` для `**/SKILL.md` (ignore-aware, bounded); frontmatter (`name`/`description`) читается bounded + denied-aware
- [ ] **Identity**: скилл адресуется frontmatter `name` при глобальной уникальности; при коллизии — repo-relative path; identity не зависит от scope
- [ ] Неоднозначное bare-имя в операторском реф: strict → error, warn → skip с предупреждением
- [ ] **Static layer**: `AgentNode.skills: tuple[str,...]` — пины на узле flow YAML; детерминированы, всегда включены, dynamic-слой их НЕ удаляет; узлы «open» (dynamic может добавить)
- [ ] **Dynamic layer**: `core/supervisor.py:propose_skill_map` — once-per-task upfront turn (read-only, propose-only); видит flow-граф + task spec + inventory, предлагает `node → skills` map
- [ ] Core принимает предложение детерминированно (propose, Core decides); супервизор предлагает, но не роутит
- [ ] Dynamic работает независимо от `planning` (переживает его отключение); пропускается при пустом inventory (репо без скиллов не платит)
- [ ] Dynamic-предложение персистится для resume (`skill_map.json` / `selected_skills.json` эквивалент)
- [ ] **Effective set per node** = `Core_filter(pins(node) ∪ dynamic_accepted(node))`, дедуп против inventory (`resolve_skills`/`_resolve_skill_layers`)
- [ ] Node wiring: `skill_reference_paths` / `{skills_path}` заполняются из per-node resolved set (не из глобального `inputs.skill_paths`); `base.py:skill_paths` per-node (`skill_paths_by_node`)
- [ ] **Retire planning-branch**: `core/hitl.py:_validate_skills` + `skills` structured field + `orchestrator.py:_engine_apply_skills` удалены
- [ ] **Strict vs warn (только операторские пины)**: dynamic-предложение с missing-скиллом → фильтруется (`dropped_unknown`), не ошибка
- [ ] Операторский пин, который не резолвится (typo/removed/ambiguous/missing path) → под `skills.strict`
- [ ] Проверка существования — на старте задачи (после clone + inventory scan), одним upfront resolution-проходом по пинам активного flow, ДО запуска узлов
- [ ] `strict: false` (дефолт) → warning + skip нерезолвнутого + continue (fail-open)
- [ ] `strict: true` → стоп задачи в `manual_action_required` с рапортом (не `failed`)
- [ ] **Config**: `skills:` блок → `dynamic: true` + `strict: false`; `scan_root` удалён (discovery авто), `exclude` выброшен; schema bump (v19), заменено outright (no migration)
- [ ] Flow-валидатор парсит пины + валидирует СТРУКТУРУ пина на preflight (существование отложено на task-start)
- [ ] Инвариант: скилл surface'ится как provider-neutral path (не Claude/Codex native tool), repo-контент недоверен, никогда не исполняется

---

## 12. Per-node model/reasoning/provider override в task front matter

**Файл:** `archive/done/task-node-model-override.md` · **Статус:** implemented 2026-06-27

Расширение task-ключа `nodes:` полями `model`/`reasoning`/`provider` — один дефолтный flow покрывает варианты модели/усилия без размножения flow-файлов.

- [ ] `task/model.py:NodeOverride` расширен `model: str|None`, `reasoning: str|None`, `provider: str|None` (рядом с `enabled`)
- [ ] Parser-валидация: неизвестные `reasoning`-значения, пустые строки
- [ ] Гейт валидирует ТОЛЬКО shape (форму)
- [ ] `core.node_overrides.resolve_node_overrides` — best-effort config-validated overlay (warn + skip невалидных полей; БЕЗ model ceiling — accepted simplification)
- [~] Override chain: Task node override (best-effort) → Flow node declaration (flow YAML) → Provider config default — прогон 4 (p0-04): **Flow-node-declaration → Provider-default уровень НАБЛЮДЁН** — flow YAML пинит planning=opus-4-8/high, impl/review/fixing=sonnet-5/xhigh, documentation=sonnet-5/medium, и глобальный конфиг-дефолт `claude-opus-4-8` перекрыт per-node; **task-front-matter уровень (`nodes.<id>.model`) ещё НЕ проверен** (задачи P0 его не задают); прогон 6 (p4-01-v2): flow-уровень повторно подтверждён на **codex-evaluator** — review перекрыл глобальный codex `gpt-5.5/high` → `gpt-5.4/xhigh` (`stages/review/…/request.json`)
- [x] Применяется на единственном node-fetch seam движка (`core/flow/engine_driver.py`), покрывая agent- И evaluator-узлы — прогон 4: override дошёл и до agent-узлов (planning/implementation/documentation), и до **evaluator-узла review** (sonnet-5/xhigh применился на review-verdict), т.е. seam покрыл оба класса
- [x] Fallback на невалидный override (provider не в конфиге, нераспознанный reasoning, невалидная model): структурный warning в node artifact log, skip поля, откат на flow-declared значение — задача НЕ аборится — прогон 10 (p4-05): `nodes.implementation.provider: gemini` (невалиден на конфиге claude/codex) → `level=warning detail="node 'implementation': provider 'gemini' not in agents.allowed ['claude', 'codex']; using the flow's provider" msg="task node override skipped"`, задача продолжилась и дошла до `done`
- [ ] Reasoning-валидация переиспользует `is_reasoning_supported`/`agents.allowed` (не новая «ceiling clamp» машинерия)
- [x] Эффективные (post-override) model/reasoning/provider записаны в prompt audit — прогон 4: `prompt-audit/timeline.jsonl` фиксирует ФАКТИЧЕСКИЕ per-node model/reasoning (opus/high, sonnet/xhigh×2, sonnet/medium), а не flow-declared/глобальные дефолты; `state.db node_lineage` отдельно не сверял
- [ ] Watch-mode compat: невалидный override деградирует мягко (не preflight-fatal, не блокирует очередь)
- [ ] Additive: нет нового task-ключа, нет bump task-схемы (`nodes:` уже в `ALLOWED_TASK_KEYS`)
- [ ] Провайдер-адаптеры (`codex.py`/`claude.py`) не менялись (chain схлопывается в `request.model or config.model`)
- [ ] Тесты: `NodeOverride` parse; engine-driver интеграция (override доходит до `_build_request`; невалидный откатывается мягко)

---

## 13. Task priority field (`priority: low | mid | high`)

**Файл:** `archive/done/task-priority.md` · **Статус:** accepted/implemented 2026-06-26

Опциональное поле `priority` в task-файле; планировщик сортирует ELIGIBLE-задачи по приоритету по убыванию.

- [ ] `task/model.py`: общий литерал `TaskPriority`, `DEFAULT_PRIORITY` (`mid`), `normalize_priority()`, `priority_rank()` — один источник правды для гейта/парсера/планировщика
- [ ] `priority` добавлен в `ALLOWED_TASK_KEYS`
- [ ] Гейт заполняет `NormalizedTask.priority` через `normalize_priority`; парсер round-trip'ит (legacy-манифесты → `mid`)
- [ ] `_scan_depends_on` переименован в `_scan_pending_meta`, возвращает `_PendingScan` (`task_id`, `depends_on`, `priority_rank`)
- [x] `watch_once` сортирует весь просканированный список по `(priority_rank, filename)`, сохраняя skip-WAITING / reject-BROKEN — прогон 2/3: **skip-WAITING** наблюдался устойчиво; **прогон 5 (diamond p0-05‖p0-06): priority-сортировка среди ≥2 eligible ПОДТВЕРЖДЕНА** — после мержа PR #5 обе `depends_on: p0-04` стали eligible в один poll, демон заклеймил **p0-05 (`high`)**, а p0-06 (`low`) оставил pending → high побеждает low среди одновременно-eligible. reject-BROKEN пока не проявлялся
- [ ] Priority rank: `high=0`, `mid=1`, `low=2` (меньше = раньше)
- [ ] **Fail-OPEN (не reject)**: неизвестная строка ИЛИ неверный тип → `mid` (нет `INVALID_FIELD_TYPE`) — сознательное исключение из fail-closed политики полей
- [ ] Дефолт при отсутствии — конкретный `mid` на модели (не `None`/tri-state)
- [x] `depends_on` всегда сильнее приоритета: `dependency_eligibility()` классифицирует ELIGIBLE/WAITING/BROKEN ДО сортировки — ранжируются только eligible — прогон 2: все `high`-задачи (p0-02/04/05) корректно WAITING, побежала только dep-free p0-01; демон логирует причину per-task («dependency X is pending (not yet run)» vs «dependency X PR is OPEN (unmerged)»)
- [ ] Single-active-task инвариант: приоритет — переупорядочивание очереди eligibility, не конкурентность
- [ ] Нет нового статуса, нет bump state-db схемы, нет config-ключа, нет CLI-флага

---

## 14. Telegram step-trace (live run progress)

**Файл:** `archive/done/telegram-step-trace.md` · **Статус:** implemented 2026-06-27 (config v21)

Односторонний best-effort live-фид прогресса в Telegram: одно сообщение на финиш каждого узла flow, под одним глобальным флагом.

- [ ] Config `telegram.trace: bool` (дефолт `false`) в `TelegramConfig`; парсинг в `config/loader.py`
- [ ] `Notifier` protocol получил `send_trace`/`send_progress`; no-op в `NullNotifier`; best-effort send в `notify/telegram.py`
- [ ] **Seam**: emit в `Orchestrator._engine_post_node` → `post_node` (`core/orchestrator.py`), НЕ в `record_run_observability` (там verdict ещё неизвестен)
- [ ] Гейт вызова на `telegram.trace`; при выключенном Telegram — `NullNotifier` → авто no-op
- [ ] Формат сообщения: `[task] <emoji> <node-id> → <outcome>`, где outcome — реальный `NodeOutcome.kind`: `done` (agent), `accept`/`rework` (evaluator), `pass`/`fail` (checks), `route:<label>` (explicit)
- [ ] Emoji: ✅ accept/done/pass, 🔁 rework, ❌ fail, ▶️ иначе
- [ ] Best-effort, fire-and-forget: НИКОГДА не блокирует и не пробрасывает исключение в пайплайн (send failure не влияет)
- [ ] Без секретов: только node id + outcome (нет diff/prompt/agent output)
- [ ] Провайдерный слой не трогается; нет новой persistence
- [ ] CONFIG_SCHEMA_VERSION bump 20 → 21 (additive, без миграционного кода)
- [ ] Gate-prompt interleaving: ведущий emoji визуально отделяет trace-строки от approve/deny промптов
- [ ] Тесты: node-finish триггерит `send_trace` при `trace: on`; no-op при off/`NullNotifier`; send failure не пробрасывается

---

## 15. Transient provider-failure recovery (API 5xx mid-task)

**Файл:** `archive/done/transient-provider-failure-recovery.md` · **Статус:** implemented 2026-06-27 (config v20, DB v13)

Три части одним изменением: A — bounded same-provider retry с backoff; симметричный Claude↔Codex fallback; B-lite — резюмируемая мягкая пауза при недоступности обоих провайдеров.

### A — bounded same-provider retry + backoff (Router)

- [ ] `providers/base.py:TRANSIENT_RETRYABLE = {PROVIDER_UNAVAILABLE, NETWORK_UNAVAILABLE}` (рядом с `FALLBACK_ELIGIBLE`); `TIMEOUT` и `RATE_LIMITED` ИСКЛЮЧЕНЫ
- [ ] `config/schema.py:RetryConfig(max_attempts, base_delay_s, max_delay_s)`; поле `retry` в `AgentsConfig`
- [ ] `config/loader.py` парсит `agents.retry`; дефолты `max_attempts=2`, `base_delay_s=2.0`, `max_delay_s=30.0`; блок опционален (отсутствует → дефолты, back-compat)
- [ ] `packaged/config.example.yaml` + `install/config_writer.py` содержат дефолтный блок `agents.retry`
- [ ] `router.py:run_stage` в `except ProviderError`: при `error_class in TRANSIENT_RETRYABLE` и остатке бюджета → повтор ТОГО ЖЕ провайдера с экспоненциальным backoff `min(base*2**k, max_delay_s)` ДО fallback
- [ ] Resume сессии при наличии, деградация в fresh + `diff_path` при провале resume (форма ветки `SESSION_UNAVAILABLE`)
- [ ] Бюджет повторов применяется к КАЖДОМУ провайдеру в `[primary, fallback]`; отдельный счётчик, НЕ вычитается из `max_stage_attempts`
- [ ] Каждая попытка пишет `provider_attempts` audit row
- [ ] `sleep: Callable[[float], None] = time.sleep` инъектируется в конструктор Router (рядом с `monotonic`) — детерминизм тестов
- [ ] Без jitter (однослотовый оркестратор, детерминированный backoff)
- [ ] `RATE_LIMITED`/`TIMEOUT`/quality-фейлы НЕ ретраятся

### Симметричный fallback

- [ ] `router.py:resolve_route` — когда резолвнутый primary = глобальный primary, цель fallback = второй разрешённый+сконфигурированный провайдер из `agents.allowed` (если ровно один другой), а не `None`
- [x] Симметрично в обе стороны Claude↔Codex; при единственном разрешённом провайдере → `fallback = None` → сразу зона B-lite
- [ ] Расширение правила PRE.1 отражено в `.agents/rules/architecture.md`

### B-lite — резюмируемая пауза

- [ ] `NodeInfraError` несёт `error_class: ErrorClass | None`
- [ ] `agent.py` / `evaluator.py` прокидывают `outcome.terminal_error.error_class` в `NodeInfraError`
- [ ] `orchestrator.py`: при `NodeInfraError` с транзиентным классом + исчерпанным окном A — НЕ `_fail()`, задача остаётся резюмируемой (`active`), прогон завершается без терминального перехода, время паузы пишется в `tasks.blocked_since` (`_park`)
- [ ] watch-loop + `recovery.py`: после cool-off переадмит; на рестарте `reconcile` резюмит единственную активную задачу с `current_node`
- [ ] Потолок `max_blocked` (дефолт 1 час) на resume: по истечении → `_fail()` (терминал), чтобы ничего не висело вечно
- [ ] БЕЗ нового статуса (переиспользуется `active` + restart-resume); `blocked_since` колонка, не статус `blocked`
- [ ] `DB_SCHEMA_VERSION` 12→13 (колонка `blocked_since`); `CONFIG_SCHEMA_VERSION` 19→20
- [ ] Резюм с чекпойнта без повторного коммита (идемпотентность через фингерпринты Git Manager)

### Инварианты

- [ ] Ретрай только _поднятого_ инфра-`ProviderError`, НИКОГДА quality (`status=failed` → fix-loop)
- [ ] Commit/push/PR только оркестратор и только post-node → повтор/резюм не делает двойной коммит
- [ ] Всё bounded + пишется в аудит; backoff в Router (ядро не учит синтаксис CLI)
- [ ] ⚠️ Phase 0 spike (верификация внутреннего ретрая CLI) НЕ проводился — дефолты задержек operator-tunable, требуют проверки против живого outage

---

## 16. Windows / Cross-Platform Support

**Файл:** `archive/done/windows-cross-platform-support.md` · **Статус:** implemented (dev loop + daemon) 2026-06-26

Полная работа на Windows: `worc run`, демон `watch`/`stop`/`restart`, pytest. Реализовано на реальной Windows 10 / Python 3.14; две гипотезы ADR оказались неверны.

### Stop-file IPC + платформенный split

- [ ] `process_control.py:stop_file_path()` helper; `stop_process()` пишет sentinel `orchestrator.stop`; `stop_file_requested()` probe
- [ ] watch loop опрашивает `stop_file_requested()` на каждом тике
- [ ] `process_control._can_signal` — платформенный split
- [ ] POSIX: `SIGTERM` (немедленное пробуждение) + poll `is_running` + эскалация `SIGKILL` после `--timeout`; стоп-файл тоже пишется (безвредный fallback)
- [ ] Windows: без `os.kill`; `stop` пишет sentinel → демон замечает между тиками, выходит, удаляет свой PID-файл; исчезновение = подтверждение (`_stop_via_pid_file`)
- [ ] Windows wedged-демон: после timeout `stop` чистит PID-файл и рапортует `timed_out` (accepted limitation — оператор гасит через Task Manager)
- [ ] `os.kill` cross-process на Windows падает winerror 87 (OpenProcess) → `is_running`/`stop_process` трактуют «no such process» (winerror 87) как dead (`_is_no_such_process`)

### OS-aware default env allowlist (`security/env.py`)

- [x] `default_allowed_environment` / `os_essential_env` — кросс-платформенная база + launch-essentials хост-ОС
- [ ] Windows добавляет: `SystemRoot`, `SystemDrive`, `windir`, `ComSpec`, `PATHEXT`, `TEMP`, `TMP`, `APPDATA`, `LOCALAPPDATA`, `HOMEDRIVE`, `HOMEPATH`, `NUMBER_OF_PROCESSORS`, `PROCESSOR_ARCHITECTURE`
- [x] POSIX добавляет: `TMPDIR`, `LD_LIBRARY_PATH`, `DYLD_LIBRARY_PATH`
- [x] Инсталлер пишет набор хост-ОС; hand-written конфиг без ключа откатывается на него; allowlist остаётся единственным env-гейтом
- [ ] `claude.exe` стартует с `%SystemRoot%` (иначе крэш 0xC0000409 STATUS_STACK_BUFFER_OVERRUN до stdout/stderr)
- [ ] WSL репортится как `Linux` (`platform.system()`) → получает POSIX-набор (корректно)

### Cross-platform чистота тестов/ядра

- [ ] `test_process_control.py`: `KILL = getattr(signal, "SIGKILL", 9)` sentinel + явные инъектируемые сигналы (нет bare `signal.SIGKILL`)
- [ ] `SkillRef.path` → `Path.as_posix()` (детерминированные reference-пути)
- [ ] `_install_atomic_write` использует `newline=""` (установленные/шаблонные файлы остаются LF, не CRLF)
- [ ] `test_recovery` персистит skill-путь через `as_posix()` (зеркалит прод)
- [ ] `fake_cli` фикстура: `.cmd`-лаунчер РАБОТАЕТ на Windows/Python 3.14 (гипотеза ADR неверна) — `conftest.py` НЕ менялся
- [x] Ядро уже чистое: pathlib, subprocess `shell=False`, tempfile, атомарный `os.replace`, env allowlist

### Отложено (не реализовано — в follow_ups)

- [ ] Windows CI runner matrix; `taskkill /F /PID` hard-kill backstop (частично закрыт позже в R1); `CTRL_BREAK_EVENT` Ctrl+C handler; macOS `_read_proc_start_time` recycling guard; `upgrade-config` репэйр OS-launch essentials для старых конфигов

---

## 17. Improvements intake — 9 usage-driven items

**Файл:** `archive/done/improvements.md` · **Статус:** all 9 implemented 2026-07-02

9 улучшений после реального использования `worc`. Пункты 1/3/5/9 углублены в authoring-contract ADR (см. секцию 18).

### 17.1 Supervisor finalize: technical debt / follow-ups

- [x] Per-flow opt-in `supervisor.emit_follow_ups`; существующий finalize-turn (БЕЗ доп. LLM-вызова) эмитит evidence-gated `follow_ups` массив в `summary.json` — прогон 2: 1 запись с `title`/`rationale`/`severity`/`paths`/`evidence`/`action_hint`
- [x] Секция «Technical debt / follow-ups» в `summary.md` (только при наличии evidence) — прогон 2: секция присутствует
- [ ] Пакетный `implementation` flow ставит флаг; research/prose flows — нет; schema захардкожена в `core/supervisor.py`
- [ ] Advisory-only: отсутствующий/malformed payload НЕ блокирует `summary.md`/публикацию; без секретов/полных diff'ов

### 17.2 Install ships `.worc/config.example.yaml`

- [x] `install` копирует пакетный `config.example.yaml` байт-в-байт в `.worc/config.example.yaml` (комментарии/форматирование сохранены)
- [ ] `install --reconfigure` обновляет файл из пакетной копии
- [x] Исполняемый `.worc/config.yaml` по-прежнему генерируется отдельно; тесты пинят byte-for-byte доставку

### 17.3 Flow-local supervisor/finalize prompts + fallback

- [x] Flow `supervisor:` блок: `role_file` (observe lens) + `finalize_role_file` (finalize lens), оба flow-dir-contained
- [x] Fallback observe: flow → `config.supervisor.role_file` → built-in; finalize: flow → built-in
- [x] Только текст перемещается в файлы; структурные схемы остаются в коде; отсутствие файлов не блокирует run

### 17.4 Remove stale historical comments

- [ ] Из `src/` + пакетных flows + не-архивных доков убраны: `supervise_impl`/`supervise_fix`, `summary-as-node`/`old summary provider`, датированные revision-заметки, `legacy ordering`
- [ ] Сохранены комментарии, объясняющие текущие инварианты; удалены только нарративы удалённой архитектуры

### 17.5 Canonical prompt-variable contract

- [x] `packaged/guide/flows/prompt-variables.md` (сеется в `.worc/guide/flows/`) — каждая переменная, кто её populate (agent/evaluator/supervisor), когда пуста, синтаксис `{?name}…{/name}`
- [x] Preflight anti-drift lint `lint_prompt_variables` защищает от дрейфа `ALLOWED_PROMPT_VARS` vs доки

### 17.6 Flow-owned prompt directories

- [x] `.worc/flows/<task_type>.yaml` — dispatch; промпты под `.worc/flows/<task_type>/...`; `role_file` указывает в подкаталог
- [x] `implementation` использует тот же паттерн владения, что и остальные built-ins
- [x] `install` копирует пакетные ассеты byte-for-byte, доставленные копии остаются active/editable

### 17.7 Remove `tasks/processing` lifecycle folder

- [x] Lifecycle упрощён до `pending`/`done`/`failed` + `.worc/tasks/rejected` (quarantine)
- [x] Новые install'ы НЕ создают `tasks/processing`; `state.db` статус — источник правды для «running»
- [x] Lookup/staging логика больше не спец-кейсит `processing`; тесты не требуют его как валидный on-disk state

### 17.8 Custom flow tutorial + best-practices

- [ ] Гайд в repo-доках + установленный `.worc/guide/`: минимальная структура, где YAML/role-файлы, регистрация `task_type`, как preflight/валидация ловит ошибки, инспекция rendered-промптов, инварианты/foot-guns
- [ ] Best-practices: security ceilings, `role_file` дисциплина, prompt-переменные, валидация, дебаг

### 17.9 Flexible prompt-variable substitution

- [x] `{?name}…{/name}` — санкционированный optional-var паттерн, принят в пакетных промптах
- [x] Preflight anti-drift lint (`referenced_variables` / `lint_prompt_variables`) — WARN, не fatal: verbatim `{name}` (code/JSON скобки) проходит
- [x] Flow-derived valid-set `valid_prompt_vars` — seam, который расширяет node-output channel (см. секцию 19)
- [x] `render_prompt` и `ALLOWED_PROMPT_VARS` НЕ изменены (фиксированное security-ядро)
- [x] Lint именует файл и токен для переменной вне allowlist

---

## 18. Prompt & supervisor authoring contract

**Файл:** `archive/done/prompt-and-supervisor-authoring-contract.md` · **Статус:** implemented 2026-07-02

Refinement ADR для 4 пунктов improvements (5, новая substitution-задача, 3, 1). Cluster A (commit `643c4a5`), Cluster B (commit `14791fe`).

### Cluster A — prompt-authoring contract

- [x] `{?name}…{/name}` — санкционированный паттерн для optional-переменной; принят в пакетных промптах (автор оборачивает всю клаузу, а не инлайнит голый `{name}`)
- [x] Preflight/validate-time lint в `core/flow/validator.py` — сканирует role-файлы каждого flow на `{name}`/`{?name}` токены вне `ALLOWED_PROMPT_VARS`, WARN (именует файл + токен), НЕ fatal (verbatim render — безопасный fallback)
- [x] Lint **flow-aware**: valid-set = core-переменные ∪ node-derived имена (seam для node-output channel)
- [x] `packaged/guide/flows/prompt-variables.md` доставляется через install guide copy в `.worc/guide/`; документирует имя, populating runner (agent/evaluator/supervisor), когда пусто, `{?name}` синтаксис
- [ ] Cross-links из `docs/configuration.md` и `B15-prompt-templates.md`
- [x] `render_prompt` и `ALLOWED_PROMPT_VARS` НЕ изменены (фиксированное security-ядро — только path/metadata, не тела/diff/логи/env/секреты)

### Cluster B task 3 — flow-local supervisor prompts

- [x] Flow `supervisor:` блок с явными ключами `role_file` (observe lens) + `finalize_role_file` (finalize emphasis); оба валидируются как flow-dir-contained
- [x] Fallback observe: flow `role_file` → `config.supervisor.role_file` → built-in hardcoded (3 шага)
- [x] Fallback finalize: flow `finalize_role_file` → built-in hardcoded (2 шага; глобальный finalize-prompt НЕ вводится — YAGNI)
- [ ] `core/supervisor.py:_base_prompt`/`_step_prompt`/`_finalize_prompt` fallback-цепочки
- [x] Observe lens в flow-собственном `supervisor.md`; finalize оживляет мёртвый `roles/summary.md` как flow-owned finalize-prompt (проверить, что никто вне супервизора его не читает)
- [x] Только ТЕКСТ переходит в файлы; структурные схемы (`memory_delta`, `follow_ups`) остаются в коде — автор не может сломать машинный контракт

### Cluster B task 1 — tech-debt / refactor signals

- [x] `supervisor.emit_follow_ups` — per-flow opt-in (дефолт `false`); пакетный `implementation` ставит, остальные нет
- [ ] При opt-in: `finalize()` turn (БЕЗ доп. LLM-вызова) с `{summary, follow_ups}` схемой (`_FINALIZE_SCHEMA`), пишет evidence-gated `follow_ups` массив в всегда-пишущийся `summary.json`
- [ ] Секция «Technical debt / follow-ups» в `summary.md`
- [ ] Каждая запись минимальна: `title`, `rationale`, `paths`, `evidence`, `severity`, `action_hint`
- [ ] Без opt-in: finalize остаётся free-text как сегодня (memory AC-S4 держится by construction)
- [x] Memory ортогонален: при enabled тот же turn дополнительно кормит `memory_delta` (другой артефакт/consumer/lifetime)
- [x] Нет `improvements.json`; отсутствующий/malformed structured output → сегодняшний `summary.md` fallback не трогается
- [x] Инвариант супервизора: advisory, read-only, no routing power, НИКОГДА не получает второй LLM-turn

---

## 19. Generic node-output prompt variables (`{<node_id>_path}`)

**Файл:** `archive/done/node-output-prompt-variables.md` · **Статус:** implemented 2026-07-02 (commit `cc13d22`)

Zero-config канал: вывод каждого agent-узла персистится в `<node_id>.out.md` и резолвится ниже по потоку как `{<node_id>_path}` через flow-derived allowlist.

- [x] Вывод каждого agent-узла (ВСЕГДА, uniform) персистится в `<artifacts>/<node_id>.out.md` (content = `structured_output["content"]` или `final_message`, через `_slot_content`)
- [x] `<node_id>.out.md` redaction-scrubbed (та же редакция, что memory/handoff writes), local/uncommitted, зарегистрирован как артефакт (audit/debug)
- [ ] Путь хранится через `Path.as_posix()` (кросс-платформенно)
- [x] `{<node_id>_path}` доступна ниже по потоку; эффективный allowlist = `ALLOWED_PROMPT_VARS ∪ {"<id>_path" для каждого node id активного flow}` (`valid_prompt_vars`)
- [x] `render_prompt` параметризован эффективным allowed-set (дефолт `ALLOWED_PROMPT_VARS` для back-compat); подставляет только имена из ПЕРЕДАННОГО allowlist, только path-значения
- [ ] `_VAR_RE` и `_BLOCK_RE` расширены до `[a-z0-9_-]+` (ids вроде `static-scan`/`pass2` резолвятся; camelCase-скобки в code/JSON проходят verbatim)
- [x] Только AGENT-узлы получают `{<node_id>_path}`; evaluator/checks/human сохраняют свои переменные (`review_path`, `checks_path`)
- [ ] Node id валидируется при загрузке против reserved core-prefix (`task`, `plan`, `diff`, `checks`, `review`, `repo`, `skills`, `memory`, `stage`, `subtask*`); коллизия → fatal flow error
- [x] Три спец-слота (`plan`/`summary`/`enriched_spec`) + side-effect переменные сохранены как есть; generic-канал живёт РЯДОМ
- [x] Один узел = один вывод; узел может дополнительно заполнить ОДИН спец-слот через `output_artifact` → тогда `.out.md` для него не пишется (нет дубля)
- [ ] Fan-in бесплатно: узел ссылается на несколько upstream (`{A_path}`, `{B_path}`, `{C_path}`; диамант `build ← [analyze, scan]`)
- [ ] Переменная непройденного узла пуста → cross-branch ссылки оборачиваются в `{?name}…{/name}`
- [ ] Cluster A lint flow-aware: valid-set = core ∪ node-derived → флагует `{X_path}`, не именующий ни один узел
- [x] Инвариант: значение всегда путь к Core-written артефакту, НИКОГДА inlined content

---

## 20. Sub-task context handoff (intra-task decompose)

**Файл:** `archive/done/subtask-context-handoff.md` · **Статус:** implemented 2026-07-02 (Block 4, поверх Cluster B)

Двухслойный «handoff brief» между back-to-back субтасками: детерминированный фактический пол + интерпретирующий brief супервизора, инжектится в implementation-узел преемника.

- [ ] **Детерминированный пол** (всегда, zero LLM): `Orchestrator._assemble_predecessor_context` + `GitManager.files_in_commit` собирают изменённые файлы предшественника, commit message, acceptance criteria, spec-указатель
- [ ] При отключённом супервизоре преемник работает на одном полу (accepted degradation, не ошибка)
- [ ] **Интерпретирующий brief** (при enabled супервизоре): `Supervisor.handoff(subtask_order)` — свой structured schema, на тёплой durable-сессии `__supervisor__`, flow-local `handoff_role_file`
- [ ] Три секции brief: **New surface area** / **Locked decisions** / **Open edges**
- [ ] Predecessor selection следует `subtask.depends_on` (integer orders), НЕ «все предыдущие»; обрабатывает intra-task diamond (subtask 3 ← [1,2]); валидирован ацикличным
- [ ] Инъекция через `{?predecessor_context}` условный блок в `implementation`-узел региона (не `planning`, который в `pre`-регионе); template-driven, без изменения flow YAML
- [ ] `predecessor_context` добавлен в `ALLOWED_PROMPT_VARS`; переиспользует `{?name}…{/name}` механизм
- [ ] `_predecessor_context()` резолвер в `agent.py` (рядом с `_memory_path()`): путь только когда decompose-регион активен, у текущего субтаска ≥1 `depends_on`, и шаблон ссылается на `{predecessor_context}`; проводка в `_prompt_variables()`
- [ ] Producer в `_fan_out_subtasks`: после `_commit_subtask` (commit_sha доступен) и до `reset_for_next_subtask`
- [ ] **Storage**: `logs/<task-id>/subtasks/NN-slug.handoff.md` — local, uncommitted, redaction-scrubbed (та же редакция, что memory writes); `Path.as_posix()`
- [ ] Никогда не пишется в memory-tiers (транзиентный, scoped к одной цепочке субтасков; другой retention horizon)
- [ ] Best-effort: отсутствующий/упавший brief НЕ фейлит субтаск (деградация к меньшему контексту)
- [ ] Advisory only: не ограничивает Core state machine/routing/provider
- [ ] Без config-изменений, без новой DB-таблицы (`subtasks` уже трекает order/slug/commit_sha)
- [ ] Deferred: soft cap на глубокие/широкие subtask-графы (last N / token-budget / summarise-summaries) — в follow_ups
- [ ] Out of scope V1: cross-task `depends_on` propagation; intra-task node handoff; persistence across instances/providers

---

## 21. Autonomous run — implementation-time decisions (addendum к 18/19/20)

**Файл:** `archive/done/autonomous-run-open-questions.md` · **Статус:** implementation log 2026-07-02 (branch `feat/prompt-supervisor-handoff`)

Лог решений/правок при автономной реализации трёх ADR (18/19/20). Дополняет их конкретными seam'ами и одним багфиксом, всплывшим на ревью.

- [ ] `core/flow/prompt_vars.py::node_output_vars` (новый) — деривация `{<id>_path}` для каждого agent-узла; `valid_prompt_vars = ALLOWED_PROMPT_VARS ∪ node_output_vars`; ОДИН helper потребляют и lint, и agent-runner
- [ ] `FlowRegistry.lint_all()` (параллельно `validate_all()`); в `cli.run_preflight` печатает НЕ-fatal `flow <name>: WARN — …`; НЕ вшит в `validate_all` (остаётся non-fatal)
- [ ] Lint скэнит node `role_file` (agent + evaluator) + flow-local `supervisor.{role_file, finalize_role_file, handoff_role_file}` против `_SUPERVISOR_PROMPT_VARS = {task_id, repo, repo_path}`
- [ ] Lint node-kind-aware: agent-файл против flow-derived set, evaluator-файл против static core set → evaluator с `{scan_path}` корректно флагается
- [ ] `{?…}` обёрнут только в `implementation/documentation.md` (`{plan_path}`/`{diff_path}` — единственный реальный dangle-риск); packaged flows lint clean (регресс)
- [ ] `postprocess.write_node_output` вызывается из `_engine_post_node` после `apply_output_artifact`; `agent.py:_node_output_paths` резолвит `{<id>_path}` СТАТЕЛЕСС через `candidate.exists()` (работает на resume и для спец-слот no-op)
- [ ] Node-output редакция через `redact_text(content, extra_secrets=_memory_extra_secrets())`, secret-set собирается ОДИН раз за run (в closure post-node); `structured_output["content"]` не adapter-redacted → скраб обязателен на записи
- [ ] Удалён мёртвый `packaged/flows/roles/summary.md`; добавлены `implementation/{supervisor,summary}.md`; `roles/supervisor.md` СОХРАНЁН (глобальный `config.supervisor.role_file` дефолт + observe-fallback для flow без `supervisor:` блока)
- [ ] `_finalize_prompt` = finalize lens (`finalize_role_file` → `_BUILTIN_FINALIZE`) + `## Task under review` + code-appended структурные секции; observe lens НЕ префиксится (тёплая сессия уже несёт observations)
- [ ] AC-S4: когда ни memory, ни `emit_follow_ups` не включены → finalize free-text (нет `output_schema`), как раньше; иначе структурный `{summary, …}` turn на ОДНОМ LLM-вызове (AC-W1)
- [ ] `parse_follow_ups` evidence-gated: запись без непустого `title` ИЛИ `evidence` молча дропается (никогда не raise); невалидный/отсутствующий `severity` → `medium`; схема захардкожена
- [ ] Handoff собирается ПЕРЕД запуском преемника (после `if unit.order in committed: continue`) — робастно на resume, обрабатывает диамант `3 ← [1,2]`
- [ ] `GitManager.files_in_commit(sha)` = `git diff-tree --no-commit-id --name-only -r <sha>`, safe argv (no shell, timeout), best-effort `[]`; только на конкретном `GitManager`, НЕ в `GitPort`
- [ ] Handoff-редакция один раз на write-site оркестратора: весь `header + floor + brief` через `redact_text(extra_secrets=_memory_extra_secrets())` до записи (единый chokepoint)
- [ ] **Багфикс**: handoff artifact-dir run-id коллизия — было фиксированное `_HANDOFF_RUN_ID = 999_998` → второй handoff в цепочке/диаманте → `FileExistsError` → тихая деградация в floor-only; исправлено на `node_run_id = _HANDOFF_RUN_ID_BASE (990_000) + subtask_order` (distinct dir на каждую границу); регресс на distinct id
- [ ] Остаточно (V1 accepted): subtask re-run на resume переиспользует id → floor-only для того re-run (benign, как sentinel finalize `0`)

---

## 22. Implementation roadmap — cross-cutting / integration checks

**Файл:** `archive/done/implementation-roadmap.md` · **Статус:** historical/closed (14 шагов, все реализованы)

Это документ-порядок сборки (index), не новый дизайн — каждый шаг = отдельный ADR из секций выше. Здесь фиксируются только сквозные интеграционные проверки, которые он подразумевает.

### Config schema version ledger (миграционная цепочка)

- [ ] `CONFIG_SCHEMA_VERSION` — единый глобальный integer с линейной `upgrade.py` цепочкой миграций
- [ ] Цепочка апгрейда работает end-to-end: 16→17 (tasks-dir) → 18 (queues) → 19 (skills) → 20 (transient) → 21 (telegram-trace) → 22 (gates) → 23 (log-mgmt) → 24 (memory)
- [ ] `upgrade-config` мёрджит новые template-ключи в старые конфиги (additive, без миграционного кода для optional-полей)
- [ ] Loader принимает отсутствующие/более низкие версии (толерантность к unknown-ключам)
- [ ] `DB_SCHEMA_VERSION` (12) НЕ бампится этими 14 шагами напрямую — только read-only helpers (`recent_tasks`, `find_open_pr_tasks`); фактический бамп до 13 сделал transient (`blocked_since`)

### Shared-seam сериализация (интеграция)

- [ ] Task-scan seam (`select_pending`/`watch_once`/`_scan_pending_meta`): priority sort → tasks-dir path → queue filter → list readout → next-task gate — все проходят в едином порядке без регрессий
- [ ] watch loop (редактируется 6 ADR: windows/priority/queues/transient/gates/memory) — все правки композируются, `watch_loop`/`watch_once` работают совместно
- [ ] `NodeOverride` / `_build_node_overrides` (queues + task-node-model-override) — оба additive поля сосуществуют
- [ ] Provider-error structuring (`NodeInfraError`/`errors.py`): `error_class` (transient) + `error_max_turns` structured field (gates) сосуществуют
- [ ] Supervisor + prompt-vars seam (skills `{skills_path}` + memory `{memory_path}` + node-output `{<id>_path}`) — per-node инъекция работает для всех потребителей
- [ ] `recent_tasks()` (list, шаг 6) переиспользуется консолью `worc top` (шаг 13) без дублирования

---

## 23. Memory subsystem V1 (`docs/backlog/memory/` + `orchestrator-memory.md`)

**Файлы:** вся папка `docs/backlog/memory/` + `archive/done/orchestrator-memory.md` · **Статус:** V1 implemented (config v24, merged в `main` PR #14), **disabled by default** (`schema` default `False`; fresh install пишет `enabled:true`)

Трёхтиерная память `.worc/memory/` (long-term / short-term / entities / audit): пишется раз за задачу на finalize (0 доп. LLM-вызовов), читается детерминированными capped-пакетами через `{memory_path}`. 5 фаз. Ниже — по группам acceptance-criteria (AC-*).

### Архитектура / модули (по фазам)

- [ ] Phase 01 Foundations: `memory/paths.py`, `_io`, `trust`, `records`, `service`, `audit` — redacted atomic writes, append-only hash-chained audit + snapshots/restore
- [ ] Phase 02 Write path: candidate delta + `apply_delta` funnel + write seams (supervisor finalize эмитит delta без доп. LLM-вызовов; deterministic failure seam)
- [ ] Phase 03 Read path: `memory/packet.py` PacketBuilder (детерминированный filter+ranking+caps+line backstop, empty→no file) + node-driven `{memory_path}` инъекция + packaged role-prompt refs
- [ ] Phase 04 Curation: `memory/derived.py` DerivedIndex + `memory/cleanup.py` CleanupJob (snapshot-first, bounded, never-promote) + `worc memory show/validate/compact/restore` + idle-gap hook в `watch_loop`
- [ ] Phase 05 Safety+eval: safety drills (redaction/poisoning/staleness/rollback) + offline-replay harness (`tests/eval/`, AC-O gate)
- [ ] `MemoryService` выполняет redact → validate → trust → merge/dedup → promote/quarantine → audit
- [ ] `DerivedIndex` роутит git через `run_process` (no-subprocess security guard)

### AC-S — Storage & config

- [x] AC-S1: с memory enabled завершённая задача оставляет заполненное `.worc/memory/` дерево (long-term/short-term/entities/audit), task-independent, gitignored — прогон 2: `short_term`/`entities`/`audit`/`quarantine` непусты; `long_term` пуст by-design (все finalize-кандидаты `agent-inferred` → quarantine, AC-SF2)
- [x] AC-S2: `.worc/memory/` никогда не коммитится и не появляется в PR-диффе; install сеет gitignore-запись
- [x] AC-S3: ничего memory-related НЕ пишется в `state.db`
- [ ] AC-S4: с memory **disabled** — никаких `.worc/memory/` записей, поведение byte-for-byte как сегодня (регресс)
- [x] AC-S5: `CONFIG_SCHEMA_VERSION` бампнут (→24); старый конфиг без memory-блока грузится с safe defaults (не fatal)
- [x] `MemoryConfig` добавлен/wired/parsed/defaulted; `packaged/config.example.yaml` документирует блок

### AC-W — Write path

- [x] AC-W1: память пишется ровно один раз за задачу, на finalization, с НУЛЁМ доп. LLM-вызовов сверх summary-turn супервизора (assert в тестах) — прогон 2 (runtime): `memory_delta` едет тем же finalize structured-turn'ом (`run-000000`), что и `summary`+`follow_ups`; отдельного LLM-вызова нет
- [ ] AC-W2: candidate delta с missing/invalid evidence отклоняется/квартинится, НИКОГДА не promote'ится в long-term молча
- [ ] AC-W3: failed/manual задача пишет short-term/failure память, но НЕ promote'ит в long-term (кроме явного сигнала оператора)
- [ ] AC-W4: задачи с external (web/MCP) контекстом по умолчанию quarantine-unless-code-validated

### AC-R — Read path

- [ ] AC-R1: каждый из planning/implementation/review/fixing получает packet-файл по `memory_path`; агенту НИКОГДА не отдаётся memory root
- [ ] AC-R2: каждый packet уважает hard caps (lines/bullets/lessons/entities/episodic)
- [ ] AC-R3: выбор packet детерминирован: те же входы → тот же packet (воспроизводимо в тестах)
- [x] AC-R4: узел без релевантной памяти получает пустой/минимальный packet, не сфабрикованный (empty→no file)

### AC-SF — Safety

- [ ] AC-SF1: secret-like строка из артефактов задачи НИКОГДА не попадает в `.worc/memory/` файл (redaction drill с planted secrets) — leak count **0**
- [ ] AC-SF2: `external-untrusted`/`agent-inferred` кандидат никогда не auto-promote'ится в durable long-term (poisoning drill)
- [ ] AC-SF3: каждая мутация даёт audit-строку с pre/post хэшами и rationale; лог append-only (hash-chained)
- [ ] AC-SF4: batch cleanup предваряется snapshot'ом, `restore` возвращает память в pre-cleanup состояние (rollback тест)
- [ ] AC-SF5: trust level назначен каждой записи и enforce'ится при promotion (low-trust не может вести себя как high-trust)

### AC-C — Curation

- [ ] AC-C1: `worc memory show | validate | compact | restore` существуют и работают с `--dry-run` планом перед выполнением
- [ ] AC-C2: фоновый cleanup запускается только когда нет активной задачи, в рамках бюджета, не задерживает pickup следующей задачи
- [ ] AC-C3: cleanup может demote/expire/quarantine/merge, но НИКОГДА не создаёт новый long-term lesson и НИКОГДА не правит code/docs/skills
- [ ] AC-C4: устаревшая entity (удалённый path/symbol) детектится и марк/квартинится cleanup'ом или validate

### AC-X — Cross-platform

- [ ] AC-X1: хранимые/сравниваемые path-строки в POSIX-форме (`as_posix()`); записи round-trip идентично на Windows и POSIX
- [ ] AC-X2: idle/cleanup control без `os.kill`/`signal` допущений; suite зелёный на Windows и POSIX

### AC-O — Outcome (gated eval baseline)

- [ ] AC-O1/O2: ≥10% сокращение токенов/wall-clock и ≥10% улучшение first-pass review/test на повторных hotspot'ах **[refine]** — baseline СИНТЕТИЧЕСКИЙ (greenfield)
- [ ] AC-O3: stale-contradiction rate <5%; secret-leak rate 0; external-only long-term promotions 0
- [ ] AC-O4: без vector/graph/SQLite инфры в V1 (V2–V4 gated измеренным lift'ом); offline-replay harness `tests/eval/harness.py` + memory-off vs memory-on baseline

### Audit remediation (F1–F6, суита зелёная)

- [ ] F1: write-time entity-path валидация
- [ ] F2: lesson existence cleanup
- [ ] F3: `extra_secrets` учтён
- [ ] F4: restore prune
- [ ] F5: частичные AC-S3/S4 добиты
- [ ] F6: annotations

---

## 24. Branch mode: run a task in an existing or current branch

**Файл:** `archive/done/branch-mode.md` · **Статус:** implemented (config v26, 2026-07-04)

Per-task `branch_mode: new|existing|current` (+ `branch_ref`) плюс downgrade-only per-task `publish: commit|push|pull_request`. Цель: продолжить/зациклить работу на существующей ветке (цепочка задач → одна ветка → один PR) без форка новой ветки на каждую задачу, и без обязательного открытия PR на каждом шаге.

### `new` (дефолт, create-from-base)

- [x] `branch_mode: new` явно в task front matter ведёт себя как дефолтное create-from-base поведение (fetch/checkout base/pull --ff-only/create) — прогон 7: p4-02 с `branch_mode: new` создал ветку от обновлённого `main` (после мержа PR #8)
- [x] Кастомный `branch_name` в `new`-режиме используется буквально (не auto-паттерн `{prefix}/{epoch}-...`) — прогон 7: `branch_name: "feat/p4-graph-chain"` → реальная ветка и PR #9 head именно `feat/p4-graph-chain`
- [ ] `task.normalized.json` отражает резолвнутые `branch_mode`/`branch_ref`/`publish` (проверено косвенно — прогон 7: `task.normalized.json` содержит `"branch_mode": "new", "branch_ref": null, "publish": null`)

### `existing` (продолжить именованную ветку)

- [ ] `branch_ref` должен существовать локально или на remote — иначе отказ на валидации (no auto-create)
- [x] `prepare_branch()`: fetch, затем checkout `branch_ref` — прогон 8: p4-03 (`branch_mode: existing, branch_ref: feat/p4-graph-chain`) отработал на этой ветке (`state.db node_runs`/ledger `branch=feat/p4-graph-chain`), без ошибок checkout
- [x] Множественные задачи подряд на одной `existing`-ветке накапливают коммиты без форка новой ветки — прогон 8: `git log origin/feat/p4-graph-chain` содержит ОБА коммита (`feat(p4-02-graph-algorithms)` + `feat(p4-03-query-layer)`) на одной ветке
- [ ] `reset_branch_to_base()`/фреш-rerun ЗАПРЕЩЁН в `existing` (только `rerun --continue`)
- [x] `terminal_cleanup()` в `existing` не удаляет ветку — прогон 8: ветка `feat/p4-graph-chain` и PR #9 живы после завершения p4-03 (не откачены/не удалены)
- [ ] Per-task `branch_name` в `existing`-режиме игнорируется + validation warning

### `current` (использовать текущий checkout как есть)

- [x] `current` — no-op относительно переключения: не чекаутит base, не делает `pull --ff-only`, не требует чистого дерева — прогон 13 (p4-08): оператор вручную сделал `git checkout feat/p4-graph-chain` до `worc run`; задача выполнилась на этой ветке без вмешательства оркестратора в checkout
- [x] `terminal_cleanup()` в `current` НЕ форсит checkout на base (дерево остаётся на рабочей ветке) — прогон 13: после `done` рабочее дерево target ОСТАЛОСЬ на `feat/p4-graph-chain` (`git branch --show-current` подтверждает), НЕ откачено на `main`
- [ ] Detached HEAD в `current` — отказ на валидации (нужен символьный branch ref), не тихая деградация
- [x] `current` под `watch`/autonomous — не запрещён, но выдаёт warning — прогон 13: warning появился и на ПРЯМОМ `worc run` (не только watch): `msg="branch_mode 'current' rides the working tree's live checkout — a poor fit for unattended watch; the task commits on whatever branch HEAD is on"`; задача не аборчена

### PR-reuse rules

- [x] `create_pr` переиспользует уже ОТКРЫТЫЙ (включая draft) PR для той же пары (head, base) вместо второго PR — прогон 8: p4-03 опубликовался в тот же PR **#9** (не #10); `gh pr view 9` показывает оба коммита p4-02+p4-03. Нюанс — **F27**: title/body PR НЕ обновляются при reuse, остаются от первой задачи цепочки
- [ ] `closed`/`merged` PR — НЕ переиспользуется, создаётся новый
- [ ] Множественные открытые матчи — переиспользуется самый новый + warning (не failure)

### Guard: working branch == base branch

- [ ] `head == pr_base` при PR-like policy → push проходит, `create_pr` пропускается (задокументированная причина в артефакте/логе), `auto_merge` становится no-op
- [ ] Отказ push (branch protection) на этом пути деградирует в обычный `NodeManualRequired` (существующий graceful-degrade, без нового кода)

### Publish downgrade-only cap

- [ ] Per-task `publish: commit` — останавливается после `commit_code`+`commit_audit` (без push, без PR)
- [x] Per-task `publish: push` — коммитит + пушит, но пропускает `create_pr` — прогон 10 (p4-05): `state.db publish_operations` содержит только `code_commit`+`audit_commit`+`push` (нет `pr`-op), ledger `pr_url: null`; PR #9 всё равно показывает новый коммит (динамический вид той же ветки, не действие оркестратора)
- [ ] `publish` — ТОЛЬКО downgrade (`min(flow_policy, task.publish)`); на flow без `publish`-узла (`publishing: none`) — no-op, не создаёт публикацию из ничего

### Safety invariant

- [ ] Деструктивные git-операции (`branch -D`, remote delete, reset-to-base, force-checkout-away) выполняются ТОЛЬКО при `branch_mode == new` — в `existing`/`current` ветка принадлежит оператору
