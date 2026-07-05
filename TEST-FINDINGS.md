# TEST-FINDINGS — сквозные находки прогона оркестратора на wastech-mdlint

Журнал находок сквозного тестирования оркестратора на реальном проекте `wastech-mdlint`. Нумерация `F<N>` **сквозная**. Находки F1–F18 (проходы 1–5, задачи P0) заархивированы в [docs/analysis/archive/TEST-FINDINGS.md](docs/analysis/archive/TEST-FINDINGS.md) и остаются в силе; этот root-файл — активный журнал кампании P4, продолжающий нумерацию с **F19** (проход 6). Ссылки на F1–F18 валидны и в `TEST-CHECKLIST.md`.

Формат записи: **категория · серьёзность · уверенность · доказательство · корневая причина · рычаг (file:line) · зона (target/orchestrator) · статус**.

---

## Проход 6 — задача `p4-01-context-graph-model-v2` (2026-07-04)

Прогон: `worc run` → `done`, PR [#8](https://github.com/VladimirMakarevich/wastech-mdlint/pull/8) (не смержен, `auto_merge:false`), `fix_iterations=0`. Флоу `implementation`: refinement (skip, per-task) → planning (claude opus-4-8/high) → implementation (claude) → testing (checks) → review (codex gpt-5.4/xhigh) → documentation (claude) → publish. Отчёт: [docs/analysis/p4-01-context-graph-model-v2-run-analysis.md](docs/analysis/p4-01-context-graph-model-v2-run-analysis.md).

---

### F19 · review-evaluator фактически no-op: прозаические находки теряются, вердикт fail-open в `accept` · **CRITICAL** · уверенность HIGH · зона **orchestrator** · статус **RESOLVED (2026-07-04)**

**Доказательство.**

- `state.db evaluations` id=85: `node_id=review, kind=in_flow_verdict, verdict=accept, findings_json=[]`. `node_runs.review.outcome=accept`. `tasks.review_fix_cycles=0`.
- А в `stages/review/run-000053/1-codex/last-message.txt` codex-ревьюер написал **ТРИ `high` blocking**-находки (все верифицированы в фактическом коде PR #8):
  1. `build-context-graph.ts:141-148` — тип ребра `link.anchor?.length>0 ? "anchor" : "link"` без валидации фрагмента по heading-slug целевого документа → `[x](b.md#missing)` создаёт ложное anchor-ребро (нарушает AC «anchor = heading-slug match»);
  2. `build-context-graph.ts:68-72,80` — `extractDefinedIds` берёт ID только из table-колонок; «(+ headings)» из AC #3 осознанно отложено в P4.04 (спорное scope-суждение) → heading-объявленные ID теряются;
  3. `build-context-graph.ts:125` — идентичность узлов из `[...documents.keys()]`, а рёбра из `document.path` (стр. 111/142) → при абсолютных ключах `loadDocuments()` узлы/рёбра разъезжаются, ломая repo-relative POSIX-инвариант.
- Флоу пропустил `fixing` и ушёл review → documentation → publish; PR #8 открыт как `done` с 3 известными багами (false-green). Супервайзер (evaluations id=86) прочитал `accept` и выдал уверенный синтез «clean run, no interventions needed, each green on the first pass» — то есть отмыл false-green в убедительное резюме.

**Корневая причина.** Рассинхрон контракта «промпт↔экстрактор». Роль-промпт ревью ([packaged/flows/roles/…] / target `.worc/flows/roles/`) просит находки **прозой** («Your findings are consumed by a downstream LLM agent… one entry per finding: severity, path, what's wrong, fix») — без JSON/схемы. Но evaluator извлекает находки **только из `structured_output`**, которого при `output_schema=None` не бывает ни у одного провайдера, и прозы-парсера нет → находок ноль → вердикт `accept`. Проходит в fake-CLI юнит-тестах (они кладут `structured_output.findings`), мёртв на живых провайдерах.

**Рычаг.**

- [core/flow/nodes/evaluator.py:178](src/wastech_orchestrator/core/flow/nodes/evaluator.py#L178) — `output_schema=None` («evaluators parse findings directly; no provider schema enforced»).
- [core/flow/nodes/evaluator.py:98,138,268-275](src/wastech_orchestrator/core/flow/nodes/evaluator.py#L268) — `_extract_findings` читает только `structured_output["findings"]`.
- Оба провайдера пишут схему и заполняют `structured_output` только при заданном `output_schema`: [providers/claude.py:260,330](src/wastech_orchestrator/providers/claude.py#L260), [providers/codex.py:165,246,325](src/wastech_orchestrator/providers/codex.py#L165).
- Варианты фикса (любой fail-**closed**): (1) передать evaluator'у findings-схему (severity/path/what/fix) и требовать её в роль-промпте — оба провайдера умеют `--json-schema`/`--output-schema`; (2) добавить прозы-парсер по `final_message`/`last-message.txt` при пустом `structured_output`, а при невозможности распарсить — `rework`/`manual`, никогда не тихий `accept`.

**Smoke-тест (2026-07-04, codex 0.139.0).** Выбран путь (1) structured-схема, fail-closed. Smoke подтвердил: codex под `--output-schema` при **активных tools** (`command_execution` `cat sample.md`) отдаёт валидный `{"findings":[…]}` (#15451 НЕ воспроизводится, gpt-5.4 не режется), НО через **last-message-файл**, а не через поле `output` терминального события → `codex.py:204-246` надо доучить парсить last-message как JSON в `structured_output`. Детали и решение — в ADR [run-quality-gating-hardening.md](docs/backlog/archive/done/run-quality-gating-hardening.md).

**Влияние.** Единственный качественный гейт после checks — no-op; кросс-провайдерное ревью ловит реальные баги, а оркестратор их выбрасывает и публикует. Это самый крупный рычаг прогона.

**Исправлено (2026-07-04).** `evaluator.py` теперь задаёт обязательную findings-схему (`_FINDINGS_SCHEMA`) в `output_schema`; отсутствующий/неразобранный `findings`-массив — fail-**closed** (`EvaluatorInfraError` → `manual_action_required`), никогда тихий `accept`. `codex.py` доучен парсить last-message-файл как JSON, когда терминальное событие не несёт `output` (подтверждено smoke-тестом на 0.139.0). Роль-промпт ревью обновлён под схему. Детали — [run-quality-gating-hardening.md](docs/backlog/archive/done/run-quality-gating-hardening.md).

---

### F20 · `current.diff` неполон: выпадают untracked-новые файлы и файлы с NUL рендерятся «Binary files differ» · **HIGH** · уверенность HIGH · зона **orchestrator** · статус **RESOLVED (2026-07-04)**

**Доказательство.**

- `current.diff` артефакта: 7 файлов, **тест-файла нет**; ядро `build-context-graph.ts` показано как `Binary files differ` (содержимое скрыто).
- Фактический коммит ветки (`git diff ce946f6..feat/p4-01-context-graph-model-v2 --stat`): **8 файлов, включая `packages/core/test/build-context-graph.test.ts` (+163 строки)**; `build-context-graph.ts | Bin 5412 -> 11342 bytes`.
- Причина «binary»: базовый blob P3 (`58dbb7f`) содержит **3 NUL-байта** (`` `${document.path}\x00${target}` `` — NUL-делимитер в легаси-dedup-ключе); новый blob NUL-free (задача сняла dedup). git видит NUL в базовой стороне → весь дифф файла бинарный.

**Корневая причина.** [git_manager.py:1173](src/wastech_orchestrator/git_manager.py#L1173) `write_current_diff` = `git diff <base_branch>` (база vs рабочее дерево). До publish в недекомпозированном прогоне ничего не закоммичено, а `git diff <base>` **(а)** не включает untracked-файлы → новый тест-файл выпадает; **(б)** без `--text` рендерит NUL-детектированные файлы как «Binary files differ». Ревью-узел получает этот `{diff_path}` (evaluator.py:175) ДО publish — то есть с неполным диффом (в этот раз codex компенсировал чтением рабочего дерева read-only-песочницей). Тот же неполный дифф идёт в тело PR и failure-report.

**Рычаг.** [git_manager.py:1173](src/wastech_orchestrator/git_manager.py#L1173): включать untracked-файлы (напр. `git add -N`/intent-to-add перед diff, либо явно перечислять untracked) и передавать `--text`, чтобы NUL-содержащие файлы всё равно рендерились текстом. Прецедент уже есть — комментарий 1159-1168 фиксирует прошлый фикс understatement (`HEAD`→`base`). Зона orchestrator (каждый репо).

**Влияние.** Ревью, зависящее строго от `{diff_path}`, не видит НИ ОДНОГО нового файла (для этой задачи — весь тест-файл + новую логику) и не видит содержимое файлов с NUL. Деградирует и человеко-обзор PR.

**Исправлено (2026-07-04).** `write_current_diff` временно `git add --intent-to-add` untracked-файлы перед diff и сразу `git reset` (без постоянной мутации индекса), плюс `--text` на самом diff. Детали — [run-quality-gating-hardening.md](docs/backlog/archive/done/run-quality-gating-hardening.md).

---

### F21 · planning в `--permission-mode plan` уводит развилки в `AskUserQuestion`/`ExitPlanMode` мимо оркестраторского `human_input` · **MEDIUM** · уверенность HIGH · зона **orchestrator** · статус **RESOLVED (2026-07-04, live-подтверждение allowlist-гейта — follow-up)**

**Доказательство.**

- `stages/planning/…/request.json` argv: `--permission-mode plan --allowedTools Read,Glob,Grep` (в `--disallowedTools` только git/gh/secrets; `AskUserQuestion`/`ExitPlanMode` НЕ запрещены).
- `stages/planning/…/result.json` `structured_output.human_input = null` (при том что схема содержит контракт `human_input`).
- `final_message`: «The plan is complete and submitted… awaiting your approval via ExitPlanMode»; `content`/`plan.md`: «written to `~/.claude/plans/…`», «AskUserQuestion went unanswered; recommended defaults applied».
- Две реальные развилки (замороженные имена полей `path/from/to` vs спека; id-ref без колонки references) агент вынес через нативный `AskUserQuestion`, ответа в headless нет → применил свои дефолты, а `human_input` оставил null → оркестратор не встал на MANUAL_ACTION_REQUIRED.

**Корневая причина.** [providers/claude.py:74](src/wastech_orchestrator/providers/claude.py#L74) — профиль `read-only → ("plan", …)`. Plan mode Claude Code активирует интерактивный UX (ExitPlanMode/AskUserQuestion/`~/.claude/plans/`), который перехватывает кларификации мимо структурного `human_input`.

**Рычаг.** [providers/claude.py:74](src/wastech_orchestrator/providers/claude.py#L74): либо режим `default` + whitelist `Read,Glob,Grep` (правки всё равно заблокированы отсутствием Edit/Write в allowlist, но plan-UX не активируется → развилки идут в `human_input`); либо оставить plan mode, но добавить `AskUserQuestion`/`ExitPlanMode` в `--disallowedTools` (стр. ~249). Проверить, что `default`+allowlist действительно блокирует Write/Edit. Зона orchestrator.

**Влияние.** HITL-гейт planning де-факто недостижим для claude → реальные операторские развилки авто-дефолтятся молча. В этот раз дефолты вышли разумные (урона нет), но механизм не сработал.

**Исправлено (2026-07-04).** `providers/claude.py`: `read-only → ("default", ("Read","Glob","Grep"))` (было `"plan"`). Юнит-тест подтверждает Edit/Write отсутствуют в `--allowedTools`; живая проверка, что реальный `claude`-процесс действительно отказывает в записи под этим режимом, вынесена в follow-up (не блокирует статус находки). Детали — [run-quality-gating-hardening.md](docs/backlog/archive/done/run-quality-gating-hardening.md), [follow_ups.md](docs/backlog/follow_ups.md) 2026-07-04.

---

### F22 · codex-evaluator не отдаёт usage/токены (`0` в result.json) — пробел cost-наблюдаемости · **LOW** · уверенность HIGH · зона **orchestrator** · статус **RESOLVED (2026-07-04)**

**Доказательство.** `stages/review/…/result.json` codex: `usage` пуст, суммарно `in=0 out=0` при 510s работы; claude-узлы usage отдают (planning out=34266, impl out=36810 и т.д.). Итог по прогону считается без стоимости самого дорогого по времени узла.

**Корневая причина / рычаг.** Парсинг usage в [providers/codex.py:211-255](src/wastech_orchestrator/providers/codex.py#L211) не извлекает токены из codex `--json`-потока (в отличие от claude.py:333). Рычаг: снять usage из терминального события codex. Зона orchestrator.

**Влияние.** Нельзя оценить стоимость codex-узлов (review/любой codex-node) — слепое пятно в бюджете/аналитике.

**Исправлено (2026-07-04).** `parse_events` теперь читает `usage` прямо из терминального события (`turn.completed`/`result`), по аналогии с `claude.py`. Детали — [run-quality-gating-hardening.md](docs/backlog/archive/done/run-quality-gating-hardening.md).

---

### F23 · пре-существующий NUL-делимитер в P3 `build-context-graph.ts` делал файл git-binary · **LOW (informational)** · уверенность HIGH · зона **target (mdlint)** · статус RESOLVED-BY-TASK

**Доказательство.** Базовый blob `58dbb7f` (из PR #7, P3): 3 NUL-байта в строковом ключе `` `${document.path}\x00${target}` ``. Задача p4-01-v2 сняла старый `(from,to)`-dedup → новый файл NUL-free. То есть проблема пре-существующая (не вина прогона) и **попутно устранена** этой задачей; после мержа #8 будущие диффы файла станут текстовыми. Связано с F20 (следствие для артефакта диффа). Отдельного действия по оркестратору не требует; отмечено как контекст к F20.

---

## Проход 7 — задача `p4-02-graph-algorithms` (2026-07-04), первая задача branch-mode chain-теста

Прогон: `worc run` → `done`, ветка `feat/p4-graph-chain` (общая ветка цепочки p4-02..p4-08, `branch_mode: new` + кастомный `branch_name`), PR [#9](https://github.com/VladimirMakarevich/wastech-mdlint/pull/9) (не смержен). Флоу `implementation`: planning (claude opus-4-8/high, 180s) → implementation (claude, 195s) → testing (checks, 7s, 160/160 green) → review (**codex gpt-5.4/xhigh crashed на attempt 1 → fallback claude**, 76s) → documentation (claude, 77s) → publish (55s). `fix_iterations=0`. Отчёт: [docs/analysis/p4-02-graph-algorithms-run-analysis.md](docs/analysis/p4-02-graph-algorithms-run-analysis.md).

---

### F24 · Регресс от сегодняшнего фикса F19: `_FINDINGS_SCHEMA` без `additionalProperties:false` — 100%-детерминированный краш codex на ЛЮБОМ evaluator-узле (review/verifier/critic/testing_quality) · **HIGH** · уверенность HIGH · зона **orchestrator** · статус **OPEN**

**Доказательство.**

- `state.db provider_attempts`: `node_run_id=60 (review), provider=codex, attempt=1, status=NULL, error_class=process_crashed, exit_code=NULL`; следом `attempt=2, provider=claude, status=succeeded, exit_code=0`. `node_runs.review: stage_attempts=2, route_fallback=claude`.
- `stages/review/run-000060/1-codex/stdout.log` (и `events.jsonl`, идентично): `{"type":"turn.failed","error":{"message":"{\n  \"type\": \"error\",\n  \"error\": {\n    \"type\": \"invalid_request_error\",\n    \"code\": \"invalid_json_schema\",\n    \"message\": \"Invalid schema for response_format 'codex_output_schema': In context=(), 'additionalProperties' is required to be supplied and to be false.\",\n    \"param\": \"text.format.schema\"\n  },\n  \"status\": 400\n}"}}` — codex CLI 0.139.0 передаёт `--output-schema` в OpenAI Responses API как strict `response_format`, а тот **требует** `additionalProperties: false` на КАЖДОМ object-узле схемы (включая вложенные).
- `stages/review/run-000060/1-codex/output-schema.json` (буквально записанный на диск оркестратором файл, который codex получил через `--output-schema`): `{"type": "object", "properties": {"findings": {"type": "array", "items": {"type": "object", "properties": {...}, "required": [...]}}}, "required": ["findings"]}` — **ни на верхнем уровне, ни на вложенном `items`-объекте нет `additionalProperties`**.
- Источник этого литерала — [core/flow/nodes/evaluator.py:57-78](src/wastech_orchestrator/core/flow/nodes/evaluator.py#L57) (`_FINDINGS_SCHEMA`), добавленный СЕГОДНЯ как фикс **F19** (см. проход 6 выше, статус `RESOLVED (2026-07-04)`) — до этого evaluator вообще не передавал `output_schema`, поэтому баг не мог проявиться.
- Контрастная проверка: `core/hitl.py` (`_HUMAN_INPUT_SCHEMA`:35, `typed_output_schema`:108/118, `_SUBTASK_SCHEMA`:55) — везде и на верхнем, и на вложенных object-уровнях стоит `"additionalProperties": False`. Т.е. паттерн в кодовой базе известен и соблюдается везде, КРОМЕ нового `_FINDINGS_SCHEMA`.
- Второстепенная деталь в том же `stderr.log` (не причина краша, но шум): `ERROR rmcp::transport::worker: worker quit with fatal: Transport channel closed, when AuthRequired(...mcp.figma.com...)` — codex CLI на этой машине пытается поднять локально сконфигурированный Figma-MCP без авторизации; не влияет на исход (turn уже упал раньше по schema-ошибке), но добавляет ~1-2с шума в каждый codex-вызов.

**Корневая причина.** `_FINDINGS_SCHEMA` (введён сегодняшним фиксом F19, обязателен для ЛЮБОГО evaluator-узла) не следует уже установленной в `hitl.py` конвенции — не проставляет `"additionalProperties": false` ни на верхнем object, ни на вложенном `items`-object. OpenAI Structured Outputs (через которые codex CLI реализует `--output-schema`) отвергает такую схему 400-кой ДО начала turn — это не флуктуация модели и не проблема качества, а гарантированный краш на каждом вызове.

**Рычаг.** [core/flow/nodes/evaluator.py:57-78](src/wastech_orchestrator/core/flow/nodes/evaluator.py#L57) — добавить `"additionalProperties": False` на оба object-уровня `_FINDINGS_SCHEMA` (верхний и `items`), по образцу `hitl.py`. Стоит также добавить регрессионный юнит-тест, который валидирует КАЖДУЮ константу-схему в кодовой базе (`_FINDINGS_SCHEMA`, `_HUMAN_INPUT_SCHEMA`, `_SUBTASK_SCHEMA`, `typed_output_schema(...)`) на наличие `additionalProperties: false` на каждом object-узле рекурсивно — smoke-тест из ADR `run-quality-gating-hardening.md`, валидировавший фикс F19, использовал СВОЙ упрощённый пример-схему (`cat sample.md` с `command_execution` tools), а не буквально `_FINDINGS_SCHEMA` — поэтому не поймал этот регресс.

**Влияние.** Сегодня замаскировано fallback'ом на claude (review всё равно доехал до `accept` за 2 попытки, +5.5с и один сожжённый provider-attempt из `agents.retry.max_attempts=2`). Но: (1) в конфигурации с ЕДИНСТВЕННЫМ разрешённым провайдером = codex (`agents.allowed: [codex]`) это будет `manual_action_required` на КАЖДОЙ задаче с evaluator-узлом — полная неработоспособность; (2) в этом целевом репо review принудительно запиннен на codex (`.worc/flows/implementation.yaml:93-96`, «per-node override: review runs on Codex»), значит на ВСЕХ p4-02..p4-08 в текущей цепочке review будет крашиться и падать на fallback идентично — систематическая, а не разовая находка; (3) даже с фоллбэком — это тихая деградация стоимости/латентности на каждом прогоне с codex-evaluator, которую легко не заметить.

---

### F25 · `depends_on` не переживает переименование зависимости при abandon+retry-под-новым-id — постоянная блокировка без диагностики · **MEDIUM** · уверенность HIGH · зона **orchestrator** · статус **OPEN**

**Доказательство.**

- Живой отказ (до правки): `worc run tasks/pending/p4-02-graph-algorithms.md` (с исходным `depends_on: [p4-01-context-graph-model]`) → `error: refusing to run p4-02-graph-algorithms: dependency 'p4-01-context-graph-model' is manual_action_required (unmerged)`, exit 2, ДО каких-либо git/branch-операций.
- `logs/completed.jsonl`: `p4-01-context-graph-model` → `final_status=manual_action_required, outcome=abandoned` (первая попытка, брошена оператором); `p4-01-context-graph-model-v2` → `final_status=done, pr_url=.../pull/8` (ретрай **под другим task id**, реально смёржен). Задача `p4-02` в исходном task-файле ссылалась на первый (заброшенный) id.
- После правки `depends_on` на верный id (`p4-01-context-graph-model-v2`) запуск прошёл штатно (гейт `ELIGIBLE`).

**Корневая причина.** [core/orchestrator.py:722-743](src/wastech_orchestrator/core/orchestrator.py#L722) `_resolve_dependency` резолвит **буквально** по строке id из `depends_on`; когда оператор абандонит задачу и перезапускает её под НОВЫМ id (а не через `rerun` того же id — единственный путь, которым движок сам восстановил бы связь), ничто не переносит/не предупреждает о том, что старый id — «мёртвый конец», а новый id — его фактическая замена. Любой другой pending-task, который ссылается на старый id, застревает в `WAITING`/явном `refuse` НАВСЕГДА, без специфичной диагностики «этот id заброшен, возможно вы имели в виду `<related-id>-v2`».

**Рычаг.** Вариантов фикса несколько, ни один не применён: (1) `task-authoring`/операторская дисциплина — документировать явно, что abandon+retry-под-новым-id требует ручной правки `depends_on` у ВСЕХ зависимых pending-задач (самый дешёвый фикс, но не защищает от забывчивости); (2) `_resolve_dependency` при обнаружении `abandoned`-статуса зависимости могла бы поискать в ledger более позднюю запись с тем же `title` и статусом `done`/PR merged и хотя бы предупредить (не автосвязывать — слишком неявно, но подсказать) — рычаг [core/orchestrator.py:722-743](src/wastech_orchestrator/core/orchestrator.py#L722); (3) `worc list`/`worc status` могли бы поверхностно показывать «N pending задач ссылаются на заброшенный id X» как advisory-предупреждение при `abandoned`.

**Влияние.** Не крашит и не портит данные — но тихо и НАВСЕГДА блокирует зависимые задачи после ЛЮБОГО abandon+retry-под-новым-id, если оператор не помнит вручную обновить каждый `depends_on`. В этой кампании поймано только потому, что мы явно ждали живой отказ на следующем шаге цепочки; в автономном `watch`-режиме это осело бы как тихий вечный `WAITING` без чёткого «почему» на поверхности (сообщение в логе есть, но диагностировать первопричину «id переименован при ретрае» пришлось вручную по ledger).

---

### F26 · `depends_on`-merge-gate не интегрирован с `branch_mode: existing/current` chain-continuation — цепочка задач на одной неслитой ветке структурно несовместима с межзадачным `depends_on` · **MEDIUM** · уверенность HIGH · зона **orchestrator** · статус **OPEN (design gap, by-design workaround: убрать depends_on-на-соседей в chain-задачах)**

**Доказательство.** Живой, предсказанный ДО запуска отказ: `worc run tasks/pending/p4-03-query-layer.md` (исходный `depends_on: [p4-02-graph-algorithms]`, `p4-02` — `done`, PR #9 **открыт**, не смержен) → `error: refusing to run p4-03-query-layer: dependency 'p4-02-graph-algorithms' PR is OPEN (unmerged)`, exit 2, ДО каких-либо git/branch-операций. Код `p4-02` физически уже присутствует в ветке `feat/p4-graph-chain`, которую `p4-03` собирается продолжить через `branch_mode: existing` — зависимость на самом деле удовлетворена «по конструкции» (общая ветка), но гейт этого не видит.

**Корневая причина.** `depends_on` ([core/orchestrator.py:745-763](src/wastech_orchestrator/core/orchestrator.py#L745) `_dependency_merged`) считает `done`-зависимость готовой ТОЛЬКО если её записанный PR смёржен (или PR не открывался вовсе — «local-commit mode»). Это предположение верно для модели «каждая задача = своя ветка = свой PR», но не для новой ADR-функциональности branch-mode (`archive/done/branch-mode.md`, «Chain of tasks on one branch»): если несколько задач умышленно копят коммиты на ОДНОЙ неслитой ветке (через `existing`/`current` + PR-reuse), их взаимный `depends_on` навсегда виснет в `WAITING`, потому что общий PR по определению остаётся открытым до конца цепочки. Два механизма выражения «B зависит от A» (merge-gate для раздельных PR vs физическое продолжение ветки) не знают друг о друге.

**Рычаг.** Не чинили (осознанно, см. ниже) — варианты на будущее: (1) документировать явно в `branch-mode.md`/`task-authoring.md`, что задачи внутри одной branch-mode-цепочки должны выражать порядок ЛИБО через `depends_on` (тогда без chain — раздельные PR/мержи), ЛИБО через `branch_mode: existing/current` (тогда `depends_on` на chain-соседей нужно убрать — порядок гарантирует оператор/scheduler своей последовательностью запуска), но не оба одновременно; (2) `_dependency_merged` могла бы дополнительно резолвить ELIGIBLE, если у зависимости и у текущей задачи совпадает эффективный working branch (значит код уже физически доступен независимо от merge-статуса общего PR) — рычаг [core/orchestrator.py:745-763](src/wastech_orchestrator/core/orchestrator.py#L745).

**Workaround, применённый в этой кампании.** `depends_on`-на-соседей-по-цепочке убран из `p4-03..p4-08` (оставлен только уже-смёрженный внешний `p4-01-context-graph-model-v2` там, где он был) — порядок гарантируется тем, что задачи запускаются оператором строго по очереди. Задокументировано здесь, чтобы находка не терялась за самим воркараундом.

**Влияние.** Без этой находки chain-тест был бы структурно невозможен «из коробки» — любая вторая задача цепочки с интра-chain `depends_on` отказывала бы навечно, пока цепочка не закончится и PR не смёржен (что противоречит самой идее «копить на одной ветке без промежуточных мержей»). Не баг в смысле «неверное поведение» — оба механизма работают каждый сам по себе корректно; проблема на стыке двух ADR.

---

## Проход 8 — задача `p4-03-query-layer` (2026-07-04), второй шаг branch-mode chain-теста

Прогон: `worc run` → `done`, `branch_mode: existing` + `branch_ref: feat/p4-graph-chain`, PR **переиспользован** — [#9](https://github.com/VladimirMakarevich/wastech-mdlint/pull/9) (тот же, не новый), теперь содержит коммиты и p4-02, и p4-03. Флоу идентичен p4-02: review на codex снова упал (2/2, см. F24) → fallback claude → `accept`. `fix_iterations=0`. Отчёт: [docs/analysis/p4-03-query-layer-run-analysis.md](docs/analysis/p4-03-query-layer-run-analysis.md).

---

### F27 · PR-reuse не обновляет title/body — переиспользованный PR остаётся с метаданными ПЕРВОЙ задачи цепочки · **LOW** · уверенность HIGH · зона **orchestrator** · статус **OPEN**

**Доказательство.** `gh pr view 9`: `title="P4.02 — Graph algorithms (topo-sort, components, cycles)"`, `body` = буквально summary p4-02 (никаких следов p4-03). Но `commits`: обе — `feat(p4-02-graph-algorithms): …` И `feat(p4-03-query-layer): …`; `git log origin/feat/p4-graph-chain` показывает оба коммита на общей ветке. Т.е. PR физически содержит работу двух задач, но заголовок/описание рассказывают только про первую.

**Корневая причина.** [git_manager.py:992-1015](src/wastech_orchestrator/git_manager.py#L992) `create_pr`: при найденном открытом PR (`_find_open_pr`) метод просто возвращает его URL (`reused`) и записывает `pr`-op — не вызывает `gh pr edit` для обновления title/body под новую задачу. Это соответствует ADR (PR-reuse rules описывают только «reuse the URL», не обновление метаданных), но для оператора-ревьюера это создаёт риск: PR с заголовком «P4.02» на самом деле может нести 6 задач цепочки (`p4-02..p4-07`) — заголовок вводит в заблуждение о реальном объёме.

**Рычаг.** [git_manager.py:1012-1015](src/wastech_orchestrator/git_manager.py#L1012) — на пути `reused is not None` можно (не обязательно) добавить best-effort `gh pr edit <url> --body <аккумулированное summary>` или хотя бы дописывать в body секцию «также включает: <task_id>» на каждый reuse. Не блокирующий фикс — chain и без этого работает корректно, только описание неполное.

**Влияние.** Косметическое/наблюдаемость, не функциональный баг — PR-reuse, коммиты, диффы и чек-гейты работают верно. Риск — человеческий ревью PR по заголовку недооценит объём изменений на длинной цепочке.

**Подтверждено на всей цепочке (2026-07-05).** До конца прогона (p4-02..p4-08, 7 задач в одном PR #9) title/body остались от p4-02 на всём протяжении — не только промежуточное наблюдение на 2 задачах.

**Обсуждённые варианты фикса (design discussion, 2026-07-05, ничего не применено):**

1. **Полная перегенерация title/body из summary последней задачи.** Проще всего технически (один `gh pr edit --title --body` на пути `reused is not None`), но каждый reuse ЗАТИРАЕТ описание предыдущих задач цепочки — итоговый PR всё равно расскажет только про последнюю задачу, а не про весь накопленный диапазон.
2. **Append-секция на каждый reuse** (`## <task_id> — <title>` дописывается под уже существующим body, по аналогии с changelog). Сохраняет полную историю цепочки без потерь. Требует идемпотентности — секцию нужно keyed'ить по `task_id`, чтобы `rerun` той же задачи не дублировал запись.
3. **Полная регенерация body из ВСЕХ task-summary этой ветки** (запрос в ledger/`state.db` по `branch`, не по одной задаче) — самое честное отражение состояния PR на любой момент, но требует агрегатора и решения конфликта: если оператор сам вручную правил PR-описание между задачами, полная регенерация это стирает, append (вариант 2) — нет.

**Рекомендация (не решение — обсуждение).** Вариант 2 (append, keyed по task id) — дешёвый, идемпотентный, не рискует затереть оператора. Общий трейд-офф любого варианта — лишний `gh pr edit` вызов на каждую задачу цепочки (мелкий доп. API-write). Ждём решения, прежде чем трогать код.

---

## Проход 14 — cross-run синтез всей фазы P4 (2026-07-05, read-only)

Сквозной разбор всех 8 задач кампании (`p4-01-v2` + `p4-02..p4-08`) — не по одной, а трендами/паттернами. Три отчёта: [синтез фазы](docs/analysis/p4-phase-synthesis.md) (Часть A), [качество промптов по узлам](docs/analysis/p4-prompt-quality-per-node.md) (Часть B), [аудит памяти](docs/analysis/p4-memory-subsystem-audit.md) (Часть C). Ничего не запускалось и не менялось. Находки F28–F37. Итог инфраструктуры: 32 agent/publish-прогона зелёные с 1-й попытки; единственная нестабильность — детерминированный codex-review-краш (F24, 9/9); весь код фазы закрыт (все AC реализованы+покрыты, 3 бага F19 исправлены человеком до мержа PR #8).

---

### F28 · Кросс-вендорное ревью не исполнилось НИ РАЗУ: фактический ревьюер всей кампании — claude (тот же вендор, что имплементер) · **MEDIUM** · уверенность HIGH · зона **orchestrator** · статус **OPEN**

**Доказательство.** `.worc/flows/implementation.yaml:93` пиннит review на codex явным комментарием «cross-provider review». Но `provider_attempts`: во всех 8 задачах codex-attempt-1 = `process_crashed` (F24), attempt-2 = claude `succeeded`. `prompt-audit/timeline.jsonl` + review-2 `request.json` argv: фоллбэк идёт на `--model claude-opus-4-8 --effort high` (собственный дефолт claude, НЕ declared `gpt-5.4`). Имплементер — `claude-sonnet-5`. То есть каждое ревью кампании = opus-4-8 ревьюит sonnet-5, один вендор. (Исключение — p4-01-v2, где codex отработал, но это был no-op F19: `findings=[]`.)

**Корневая причина.** Прямое следствие F24 (codex детерминированно крашится) + штатного фоллбэка: declared `gpt-5.4` неприменима к claude, поэтому фоллбэк берёт claude-конфиг-дефолт. Механизм фоллбэка отрабатывает корректно — но задекларированная кросс-**вендорная** независимость ревью (ради которой codex и пиннили) достигается 0/9.

**Рычаг.** Первично — починить F24 ([core/flow/nodes/evaluator.py:57-78](src/wastech_orchestrator/core/flow/nodes/evaluator.py#L57), `additionalProperties:false`), чтобы codex-ревью реально бежало. Пока F24 открыт, «cross-provider review»-комментарий в flow вводит в заблуждение — стоит либо чинить F24, либо честно задокументировать, что фоллбэк-ревью — same-vendor.

**Влияние.** Ревью не бесполезно (opus > sonnet по классу, находит реальные баги — см. p4-05), но задекларированная и оплачиваемая (провайдер-пин + сожжённый codex-attempt каждый прогон) независимость ревью — фикция на всей кампании.

---

### F29 · Рассинхрон словаря `evidence.type`: `file`/`commit` не распознаются trust-классификатором → 18/21 уроков навсегда `agent-inferred` · **MEDIUM-HIGH** · уверенность HIGH · зона **orchestrator** · статус **OPEN**

**Доказательство.** Фактические типы доказательств во всех карантинных уроках `.worc/memory/quarantine/pending.jsonl`: **`file`: 32, `check`: 3, `commit`: 1**. `assign_trust` ([memory/lifecycle.py:24-54](src/wastech_orchestrator/memory/lifecycle.py#L24)) распознаёт только `_REPO={repo,repo_doc,code,config,doc}` и `_ARTIFACT={artifact,check,diff,test,plan}` — токены `file` и `commit` не входят ни в один класс → грунтуют «ничего» → `agent-inferred`. Результат: 18 уроков (все с `file`/`commit`-доказательствами) → `agent-inferred` (недурабельный) → карантин навсегда, `long_term/` пуст.

**Корневая причина.** `DELTA_OUTPUT_SCHEMA` ([memory/delta.py:119](src/wastech_orchestrator/memory/delta.py#L119)) оставляет `evidence.type` свободной строкой, роль-промпт `summary.md` не задаёт словарь → супервайзер естественно пишет `file`/`commit`, а детерминированный классификатор их молча топит. Урок с доказательством `{"type":"file","ref":"…/query.ts"}` — репо-обоснованный по смыслу — деградирует до недурабельного только из-за токена.

**Рычаг.** [memory/lifecycle.py:24-28](src/wastech_orchestrator/memory/lifecycle.py#L24) — добавить `file→_REPO`, `commit→_ARTIFACT` (или нормализовать); и/или enum-ограничить `evidence.type` в [memory/delta.py:119](src/wastech_orchestrator/memory/delta.py#L119) + задать словарь в `summary.md`. Код-фикс первичен.

**Влияние.** Управляемая память не может накопить НИ ОДНОГО durable-урока из репо-обоснованных находок — это главная причина пустоты `long_term/` (а не «V1 не промоутит»). V2-промоушен, гейтящийся на измеренном lift, будет измерять пустоту.

---

### F30 · Рекуррентность ключуется по дословному `subject` → реально повторившийся урок не промоутится · **MEDIUM** · уверенность HIGH · зона **orchestrator** · статус **OPEN**

**Доказательство.** Урок про prettier-baseline-drift записан в 3 задачах (p4-01, p4-06, p4-07) — реальный 3× повтор. Но `subject` каждый раз иной: `"npm run format baseline"` / `"repo-wide Prettier drift"` / `"prettier baseline drift"`. `_derive_id(kind, subject)` = `ltm_`+hash(`kind:normalize_subject(subject)`) ([memory/service.py:562](src/wastech_orchestrator/memory/service.py#L562)), `normalize_subject` = только lower+trim ([lifecycle.py:79](src/wastech_orchestrator/memory/lifecycle.py#L79)) → 3 разных `memory_id` (`ltm_7ef2a85afddd`/`ltm_b13f8fdfeeb2`/`ltm_6019dbb25218`), `seen_task_ids` не накапливается → `recurrence=1 < promote_min_tasks=2` каждый раз, аудит-`rationale` «held short-term: awaiting recurrence (1/2 tasks)». Эти 3 — `artifact-backed` (durable), совпади `subject` — 2-я задача дала бы `recurrence=2` → промоушен.

**Корневая причина.** Ключ дедупа/рекуррентности предполагает стабильный `subject`, но его пишет LLM-супервайзер, и формулировка дрейфует. `should_promote` ([lifecycle.py:84-107](src/wastech_orchestrator/memory/lifecycle.py#L84)) корректен — до него просто не доходит накопленный повтор.

**Рычаг.** [memory/service.py:562](src/wastech_orchestrator/memory/service.py#L562) / [lifecycle.py:79](src/wastech_orchestrator/memory/lifecycle.py#L79) — более устойчивый ключ (напр. `kind`+нормализованные `scope.paths`, или fuzzy-match subject), чтобы семантически один урок дедуплицировался.

**Влияние.** Единственный класс промоутируемых уроков (`artifact-backed` с рекуррентностью) не промоутится даже при реальном повторе — второй замок на пустой `long_term/` (вместе с F29).

---

### F31 · Узел `review` не получает пакет памяти; блок `{memory_path}` в `review.md` мёртв · **LOW-MEDIUM** · уверенность HIGH · зона **orchestrator** · статус **OPEN**

**Доказательство.** `grep` по `request.json`: у planning/implementation/fixing в промпте есть memory-бриф, у review — нет. Evaluator-раннер `_prompt_variables` ([core/flow/nodes/evaluator.py:289-300](src/wastech_orchestrator/core/flow/nodes/evaluator.py#L289)) не содержит ключа `memory_path` и не строит пакет, тогда как agent-раннер это делает ([nodes/agent.py:534,596-600](src/wastech_orchestrator/core/flow/nodes/agent.py#L534)). Поэтому `{?memory_path}`-блок в `review.md:48` всегда пуст, а reviewer-preference-ранжирование `packet.py` (`_REVIEWER_PREF_NODES={review,fixing}`, [memory/packet.py:41](src/wastech_orchestrator/memory/packet.py#L41)) для review не срабатывает.

**Корневая причина / рычаг.** Пакет памяти прокидывает только agent-раннер; evaluator-раннер не был подключён, хотя `packet.py` спроектирован обслуживать review. [evaluator.py:289](src/wastech_orchestrator/core/flow/nodes/evaluator.py#L289) — прокинуть `memory_path`+build_packet, либо убрать мёртвый блок из `review.md`.

**Влияние.** Ревью — узел, которому «recurring reviewer expectations» пригодились бы больше всего (в карантине есть reviewer-kind уроки), но он памяти не видит. Наблюдаемый эффект сегодня мал (память всё равно пуста, F29/F30), но при их починке review останется единственным узлом без пакета.

---

### F32 · Вход ревью (`{diff_path}`) не отражает изменение задачи: кумулятивный (chain) + pre-documentation дифф → ложные находки и нерезолвимые line-refs · **MEDIUM** · уверенность HIGH · зона **orchestrator** · статус **OPEN**

**Доказательство.** `write_current_diff = git diff <base_branch>` ([git_manager.py:1173](src/wastech_orchestrator/git_manager.py#L1173)). На общей ветке цепочки `base=main`, поэтому review получает КУМУЛЯТИВНЫЙ дифф всех предыдущих задач: p4-07 review видел 35 файлов при ~5 изменённых задачей; в p4-07-диффе файлы p4-03/04/05 помечены `new file mode`. Три следствия наблюдались: (1) **ложная находка** — p4-06 review: «index.ts newly exports the full P4.02–P4.05 surface … broader than the P4.06 plan step», тогда как `git show 276b9cb -- index.ts` = ровно 2 строки (шаг плана); остальные экспорты добавлены p4-02..05. (2) **повторяющийся ложный «phase-doc не обновлён»** (p4-04 medium, p4-05 low) — review бежит ДО documentation, который phase-doc и обновляет; при идентичной ситуации p4-06 review это НЕ флагнул (тот же opus/high) → доказанная непоследовательность/шум. (3) **line-refs не резолвятся** — p4-06 находки цитируют `coverage.ts:529-539` при файле в 97 строк (реальная логика — стр. 79-86); это ни исходные строки, ни чистые diff-офсеты.

**Корневая причина.** Ревью в свежей сессии (без памяти о том, что изменила ИМЕННО эта задача) судит по диффу `<base>..worktree`, который в chain-режиме кумулятивен, а всегда — pre-documentation. Роль-промпт не оговаривает ни то, ни другое.

**Рычаг.** Кодовый — [git_manager.py:1173](src/wastech_orchestrator/git_manager.py#L1173): давать ревью/документации ИНКРЕМЕНТАЛЬНЫЙ дифф задачи (набор изменённых задачей файлов / диапазон коммитов задачи), а не `<base>..worktree`. Промпт — `.worc/flows/implementation/review.md`: «дифф может быть кумулятивным/pre-doc — суди по плану задачи, не флагай prior-task код как scope drift и doc-обновления; цитируй source-path+symbol».

**Влияние.** Ревью тратит внимание на чужой код, выдаёт фактически ложные находки (уже случилось) и нерезолвимые line-refs — снижает и качество гейта, и полезность находок для fixing-агента.

---

### F33 · Инвариант «sort every output array» без исключения для упорядоченных последовательностей — вероятный источник единственного blocking-бага кампании · **LOW-MEDIUM** · уверенность MEDIUM · зона **orchestrator** · статус **OPEN**

**Доказательство.** `implementation.md` (## Hard Invariants): «**Determinism**: sort every output array before returning or rendering it». Единственный `blocking` кампании (p4-05, verdict=`rework`) — переприменение: агент написал `readingOrder.map(relativize).sort(byPath)`, а `readingOrder` — топологический порядок. Ревью: «the extra `.sort(byPath)` silently overwrites the topological order with an alphabetical one». `review.md:23` зеркалит абсолютное правило в blocking-списке.

**Корневая причина.** Промпт-инвариант сформулирован абсолютно, без различения path-keyed массивов (сортировать) и осмысленных последовательностей (не сортировать). Агент честно применил его ко всему.

**Рычаг.** target [.worc/flows/implementation/implementation.md:15](/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/implementation.md) + [review.md:23](/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/review.md) — локальный дрифт (блок Hard Invariants — target-кастомизация). Добавить оговорку про topological/reading/ranked order.

**Влияние.** Один реальный high-баг (пойман и починен fix-циклом), но паттерн систематичен: абсолютное правило провоцирует over-sorting осмысленных последовательностей.

---

### F34 · planning-промпт ссылается на несуществующие «core primitives» (`graph/build.ts`, `markdown/parse.ts`, `llm/budget.ts`) · **LOW** · уверенность HIGH · зона **orchestrator** · статус **OPEN**

**Доказательство.** target `planning.md` (секция Roadmap And Architecture) перечисляет к переиспользованию `packages/core/src/{markdown/parse.ts, graph/build.ts, llm/budget.ts}`. Проверка репо: 3 из 4 путей не существуют (фактически `parse-document.ts`, `build-context-graph.ts`; директории `llm/` нет), корректен только `discovery/`. opus не обманулся (нашёл верные модули + пакет памяти нёс правильный `build-context-graph.ts` — память де-факто исправила промпт).

**Корневая причина / рычаг.** Локальный дрифт: project-специфичный список вписан в target-копию `planning.md` и устарел относительно фактического v2-монорепо. Packaged `planning.md` — generic, этих путей не содержит. Рычаг: [.worc/flows/implementation/planning.md](/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/planning.md) — заменить на реальные пути или сделать generic.

**Влияние.** Слабый планировщик был бы уведён на несуществующие файлы; сейчас opus+память компенсируют, но это латентный misdirect и симптом отсутствия проверки актуальности кастомизированных промптов.

---

### F35 · Рецидив NUL-делимитеров: `graph-algorithms.ts` и `graph.e2e.test.ts` в PR #9 — git-binary, ревью не видит · **LOW** · уверенность HIGH · зона **target (+ orchestrator observability)** · статус **OPEN**

**Доказательство.** `git show feat/p4-graph-chain:packages/core/src/graph/graph-algorithms.ts` содержит NUL в ключе `` `${edge.from}\x00${edge.to}` `` (стр. 42); `graph.e2e.test.ts` — NUL в `edgeSortKey`. git видит оба файла как binary. Это рецидив анти-паттерна F23 (пре-существующий NUL в P3). Фикс F20 (`--text`) **подтверждён рабочим** — `current.diff` p4-02/p4-08 рендерит эти файлы как ТЕКСТ, но NUL невидим даже в текстовом диффе, поэтому ревью (claude-fallback) их не поймало.

**Корневая причина / рычаг.** target-код использует NUL как join-делимитер (в отличие от `query.ts:62`, где пробел). Оркестраторный угол: нет гейта на committed control-байты, а ревью не видит NUL даже с `--text`. Рычаг: target-код (заменить NUL на пробел) + опционально preflight/`checks`-проверка на control-байты в диффе.

**Влияние.** 2 из 47 файлов PR #9 (включая весь e2e-тест p4-08) не ревьюятся через `git diff`/GitHub. Функционально безвредно (ключи самосогласованы), но подрывает человеко-ревью и merge/diff-инструменты.

---

### F36 · Абсолютные host-пути в эпизодах памяти + невоспроизводимая редакция (2 из 8) · **LOW** · уверенность HIGH · зона **orchestrator** · статус **OPEN**

**Доказательство.** `.worc/memory/short_term/recent.jsonl`: `artifact_paths` эпизодов `ep_p4-02..ep_p4-07` = буквально `/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/logs/...`, а `ep_p4-01-v2`/`ep_p4-08` = `[REDACTED]/.worc/logs/...`. Один и тот же безобидный путь: в 6 записан как есть, в 2 — отредактирован.

**Корневая причина.** Эпизод строится с абсолютным путём ([core/orchestrator.py:2117](src/wastech_orchestrator/core/orchestrator.py#L2117)), не relativized (хотя `records.py` декларирует POSIX repo-relative). Набор redaction-литералов харвестится в рантайме (`_memory_extra_secrets`, [orchestrator.py:2047](src/wastech_orchestrator/core/orchestrator.py#L2047)) из env-секретов + `.env`/`secrets/**`, поэтому зависит от преходящего состояния процесса — в 2 прогонах какой-то литерал совпал с префиксом пути и вычистил его.

**Рычаг.** [core/orchestrator.py:2117](src/wastech_orchestrator/core/orchestrator.py#L2117) — хранить `.worc`-относительный путь. Влияние низкое (локальный путь, не credential), но редакция невоспроизводима для идентичных данных — сигнал для security-чокпоинта.

---

### F37 · Теневая нативная память Claude Code: спаунящиеся агенты читают/пишут `~/.claude/projects/<target>/memory/` вне изоляции, редакции и аудита · **HIGH** · уверенность HIGH · зона **orchestrator** · статус **OPEN**

**Доказательство (firsthand).** Директория `~/.claude/projects/-Users-a1234-Documents-GitHub-wastech-mdlint/memory/` существует и содержит карточки с фазы P0 по p4-06: `MEMORY.md` (индекс, 3 записи), `p0-04-tsconfig-src-cleanup.md`, `p0-complete-config-deferrals.md`, `p4-06-grp-coverage-idref.md` (создан 5 июля 00:19 — во время прогона p4-06). `stages/implementation/run-000089/1-claude/events.jsonl` p4-06 (строки 393-399) буквально: `Read` `…/memory/MEMORY.md` → `Write` `…/memory/p4-06-grp-coverage-idref.md` («File created successfully at: /Users/a1234/.claude/projects/…») → `Edit` `…/memory/MEMORY.md` («has been updated»). Нативная память читается/инъектируется во ВСЕХ 8 задачах (10-15 упоминаний `.claude/projects/-Users…` в `events.jsonl` каждой); запись — в p4-06 (и ранее в P0), т.е. недетерминированно. Карточка `p4-06-…md` несёт в frontmatter нередактированный `originSessionId: c99cbf29-95b9-4a51-a420-1b6325ab5d21`.

**Корневая причина.** Оркестратор спаунит `claude` с активной нативной памятью Claude Code (нативный memory-system-prompt инъектируется, memory-директория авто-подхватывается по `cwd`) и не конфайнит `Write`/`Edit` рабочим деревом: `implementation`-узел идёт с `--allowedTools Read,Glob,Grep,Edit,Write,Bash`, а `--disallowedTools` запрещает лишь чтение `.env`/`secrets/**` и git/gh — ничто не мешает `Write` в `/Users/a1234/.claude/…`. `CLAUDE_CONFIG_DIR` в allowlist `security.allowed_environment` прокидывается в домашний конфиг оператора.

**Рычаг.** [providers/claude.py](src/wastech_orchestrator/providers/claude.py) (конфигурация спауна) — отключить нативную память для спаунящихся агентов (изолированный `CLAUDE_CONFIG_DIR`/settings) и/или конфайнить `Write`/`Edit` рабочим деревом (`--disallowedTools` на путях вне репо / `--add-dir`-контур).

**Влияние.** (1) Пробой изоляции: агент пишет durable-файлы в `~/.claude/` оператора, вне рабочего дерева, `current.diff`, commit и знания оркестратора; накапливается через все задачи всех кампаний (уже с P0). (2) Вне редакции/аудита — наблюдается утечка session-id; в общем случае туда уедет что угодно. (3) Параллельно управляемой `.worc/memory/` работает вторая, неуправляемая — все её poisoning-защиты бессмысленны рядом с сырой нативной. По иронии именно нативная память захватила корректный, детальный урок P4.06, который управляемая подсистема застряла квартинировать (F29/F30). Полный разбор — Часть C.

---
