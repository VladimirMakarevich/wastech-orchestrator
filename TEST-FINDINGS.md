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

## Проход 15 — задача `p5-01-classify-nodes` (2026-07-07), первый прогон с codex-primary

Прогон: `worc run` → `done`, ветка `feat/p5-compile`, PR [#11](https://github.com/VladimirMakarevich/wastech-mdlint/pull/11) (не смержен, `auto_merge:false`), `fix_iterations=0`. **Смена конфигурации перед прогоном** (по команде оператора): глобальный `primary` перенесён с claude на **codex `gpt-5.4`/`xhigh`** (claude → fallback + review), рабочие узлы флоу перепинены на codex (planning/implementation/fixing/documentation), review-узел перевёрнут на claude (`opus-4-8`/high). Флоу `implementation`: planning (codex, 356s) → implementation (codex, 255s) → testing (checks 4/4 green) → review (**claude** opus-4-8/high, 68s, `accept`, 0 findings) → documentation (codex→**fallback claude**, 107s) → publish. Diff чистый и в скоупе (4 файла, +257/−3: `graph-analysis.ts`+barrel+тест+doc). Отчёт: [docs/analysis/p5-01-classify-nodes-run-analysis.md](docs/analysis/p5-01-classify-nodes-run-analysis.md).

### F38 · codex `exec resume` собирается с `--cd`/`--sandbox`/`--json`/`--model`, которые codex 0.142.5 отвергает → все resume-узлы (supervisor, documentation, fixing) падают на codex и уходят в fallback · **HIGH** · уверенность HIGH · зона **orchestrator** · статус **VERIFIED FIXED в 0.8.9a3 (Проход 16)**

**VERIFIED FIXED (Проход 16, p5-02, 2026-07-07).** Фикс (переупорядочивание resume-argv, ADR [codex-primary-correctness.md](docs/backlog/codex-primary-correctness.md)) подтверждён в бою. `stages/documentation/run-000120/1-codex/request.json` `argv` теперь: `codex --ask-for-approval never exec --cd <dir> --sandbox workspace-write --json --output-last-message <file> resume [REDACTED] --model gpt-5.4 -c model_reasoning_effort="medium" -` — exec-опции стоят **до** `resume`, `--model`/`-c` после. documentation (resume-узел, валидная модель gpt-5.4) `provider_attempts`: `codex attempt=1 succeeded exit 0` (77s), **без fallback** (в Проходе 15 этот же узел падал `unsupported_version` → claude). Сигнатура supervisor-краша тоже сменилась (`unsupported_version` 0.17s → уже не argparse, см. F39). Ниже — исходное описание бага (Проход 15).

**Доказательство.** `stages/documentation/run-000113/1-codex/stderr.log` и все `stages/supervisor/run-*/1-codex/stderr.log`: `error: unexpected argument '--cd' found` / `Usage: codex exec resume <SESSION_ID> [PROMPT]`. `request.json` (documentation) `argv`: `codex --ask-for-approval never exec resume [REDACTED] --cd <dir> --sandbox workspace-write --json … --model gpt-5.4 -c model_reasoning_effort="medium" -`, `result.json`: `exit_code=2`, `error_class=unsupported_version`. `state.db provider_attempts` (join `node_runs`): `documentation codex attempt=1 error_class=unsupported_version` → `claude attempt=2 succeeded`. Supervisor-крашей в `provider_attempts` нет (supervisor — слой, не node_run), но в логе он крашит codex на КАЖДОМ шаге (×6: после planning/impl/testing/review/documentation + финальный summary), затем fallback на claude. **Контраст**: fresh-сессии codex (planning, implementation) — `succeeded` без флага resume; ломается только resume-путь.

**Корневая причина.** [providers/codex.py:146-159](src/wastech_orchestrator/providers/codex.py#L146-L159): адаптер добавляет `resume <SESSION_ID>` и затем НЕизменно дописывает `--cd/--sandbox/--json/--output-last-message` (и ниже `--model`, `-c model_reasoning_effort`). Комментарий утверждает «`codex exec resume <ID>` … accepts the same global security flags + exec options (verified on codex-cli 0.139.0)» — но у установленной **codex 0.142.5** грамматика подкоманды `resume` сузилась до `resume <SESSION_ID> [PROMPT]`, и `--cd` (первый же из дописанных флагов) отвергается парсером. Т.е. сломанное допущение о версии CLI. Вторично: `error_class=unsupported_version` — техничеки правдоподобен (наш argv несовместим с этой версией), но маскирует, что это argparse-ошибка нашего же построения argv, а не «CLI отсутствует/стар».

**Рычаг.** [providers/codex.py:146-159](src/wastech_orchestrator/providers/codex.py#L146-L159) — для resume-ветки собирать argv по грамматике 0.142.5: перенести глобальные флаги (`--cd`, `--sandbox`, approval) в положение, которое `exec resume` принимает (вероятно, ДО подкоманды или через `-c`-конфиг), а не после `resume <ID>`; проверить фактический `codex exec resume --help` на 0.142.5 и закрепить контракт версии (preflight-capability-probe уже есть рядом, [codex.py:300-310](src/wastech_orchestrator/providers/codex.py#L300)). Зона — orchestrator (пакетный адаптер, задевает ВСЕ репо на codex 0.142.x).

**Влияние.** На codex-primary ломается ВЕСЬ resume-путь: (1) supervisor-оверсайт де-факто выполняется на claude, а не codex — заявленная «codex-primary» оценка шагов не соблюдается; (2) documentation выполняется на claude; (3) при rework fixing-узел (resume implementation-lineage) тоже упал бы на codex. Сейчас замаскировано fallback'ом на claude (задача доехала до `done`), но при `agents.allowed: [codex]` или недоступном claude documentation + каждый supervisor-шаг = hard-fail. Плюс тихая деградация: 7 сожжённых codex-attempt'ов + латентность fallback на каждый resume.

### F39 · `supervisor` имеет `model` без `provider` → под codex-primary уводит `--model claude-opus-4-8` на codex (утечка claude-модели в codex) · **MEDIUM** · уверенность HIGH · зона **orchestrator** (+ target-config) · статус **CONFIRMED в Проходе 16; код-фикс есть, но НЕ закрыт в target-конфиге + пробел preflight-валидации OPEN**

**CONFIRMED (Проход 16, p5-02, 2026-07-07).** После починки F38 supervisor-codex теперь доходит до реального запуска и падает **на модели**, а не на argparse — точная причина видна в `stages/supervisor/run-000116/1-codex/stdout.log`: сначала «Model metadata for claude-opus-4-8 not found», затем `{"type":"error","status":400,"error":{"type":"invalid_request_error","message":"The 'claude-opus-4-8' model is not supported when using Codex with a ChatGPT account."}}` → `turn.failed`, `result.json` `exit_code=1 error_class=process_crashed`. Повторилось на КАЖДОМ из 6 supervisor-шагов → fallback на claude. **F39-код-фикс (поле `SupervisorConfig.provider` + валидация) присутствует в 0.8.9a3, но не эффективен здесь по двум причинам:** (1) target `config.yaml` НЕ задаёт `supervisor.provider` → всё ещё наследует `primary=codex`; (2) **`preflight` прошёл `ready`, не поймав унаследованный мисматч** — валидация, видимо, срабатывает только на ЯВНО заданный `supervisor.provider`, а не на наследование primary с claude-моделью. Пробел валидации — новый под-пункт, остаётся OPEN.

**Доказательство (Проход 15).** `.worc/config.yaml` блок `supervisor`: `model: claude-opus-4-8`, `reasoning: high` — **без `provider`**. Лог: `node_id=supervisor primary=codex fallback=claude source=config` → унаследован глобальный `primary=codex`. `stages/supervisor/run-000000/1-codex/request.json` `argv`: `… exec resume … --model claude-opus-4-8 -c model_reasoning_effort="high" -` — на codex ушла claude-модель. Падает раньше на `--cd` (F38), поэтому мисматч модели даже не доходит до валидации codex.

**Корневая причина.** Разрешение провайдера для supervisor-слоя берёт глобальный primary, когда в блоке нет явного `provider`, но `model` при этом задан claude-специфичный. Нет проверки согласованности «model ↔ provider» для supervisor (в отличие от flow-узлов, где `validate_flow_against_config` ловит несовместимость). Ср. [config/schema.py](src/wastech_orchestrator/config/schema.py) (`SupervisorConfig`) и точку разрешения supervisor-провайдера в оркестраторе.

**Рычаг.** Либо (а) `SupervisorConfig` получает собственный `provider` и валидацию model↔provider (как у flow-узлов) — **частично сделано в 0.8.9a3, но валидация не ловит унаследованный primary**, надо расширить проверку на случай «provider не задан → наследуется primary, а model чужого вендора»; либо (б) при смене глобального primary supervisor должен явно пиннить провайдера. Быстрый обход в target: добавить `provider: claude` в блок `supervisor` config.yaml. Зона — orchestrator (валидация/резолвинг), временный обход — target-config.

---

## Проход 16 — задача `p5-02-doc-profile` (2026-07-07), codex-primary на 0.8.9a3 (фикс F38/F39)

Прогон: `worc run` → `done`, ветка `feat/p5-compile` (`branch_mode: existing`), PR [#11](https://github.com/VladimirMakarevich/wastech-mdlint/pull/11) **reuse** (второй коммит, не смержен), `fix_iterations=0`. Версия оркестратора **0.8.9a3** (доставлен фикс ADR codex-primary-correctness). Флоу: planning (codex, 428s) → implementation (codex, 572s) → testing (4/4 green) → review (**claude** opus-4-8, 142s, `accept`) → documentation (**codex** 77s, БЕЗ fallback) → publish. supervisor крашил codex ×6 → claude (F39). Коммит p5-02 чистый: 4 файла (+386/−4: `compile/doc-profile.ts` +129, тест +230, `index.ts` +6, doc +25). Отчёт: [docs/analysis/p5-02-doc-profile-run-analysis.md](docs/analysis/p5-02-doc-profile-run-analysis.md). Итог: **F38 VERIFIED FIXED** (см. выше), **F39 подтверждён и не закрыт в конфиге** (см. выше), новая **F40**.

### F40 · `depends_on` (merge-гейт) и `branch_mode: existing` на общей ветке — взаимоисключающие механизмы цепочки; их совмещение заблокировало всю фазу P5 на шаге 2 · **MEDIUM** · уверенность HIGH · зона **target (task-authoring)** + orchestrator (нет предупреждения) · статус **OBSERVED / обойдено (снят depends_on)**

**Доказательство.** Первый запуск p5-02 (`b4c7du9wf`) немедленно отказан: `error: refusing to run p5-02-doc-profile: dependency 'p5-01-classify-nodes' PR is OPEN (unmerged)` (exit 2, задача осталась `pending`). Все p5-задачи (p5-02..p5-06) объявляли И `depends_on` на предшественников, И `branch_mode: existing` + `branch_ref: feat/p5-compile` (проверено по front matter каждой). PR #11 по замыслу общий для всей фазы и остаётся открытым до конца — поэтому merge-гейт `depends_on` блокирует всю цепочку начиная со 2-го шага.

**Корневая причина.** `depends_on` семантически = «зависимость должна быть **смержена** (приземлена в base)», тогда как `branch_mode: existing` на общей ветке = «коммиты накапливаются в одном открытом PR, мерж — в конце». Это два взаимоисключающих способа цепочки: shared-branch-порядок обеспечивается single-active-очередью + порядком запуска оператором, а `depends_on` тут лишний и вреден. В кампании p4 та же shared-branch-схема работала именно потому, что задачи `depends_on` НЕ объявляли.

**Рычаг.** Первично — target task-authoring: не совмещать `depends_on` с same-ref `branch_mode: existing` (для shared-branch цепочки `depends_on` убрать — сделано для p5-02..06). Вторично (orchestrator, УХ): при валидации задачи предупреждать/отклонять, когда `depends_on` указывает на задачу, чья ветка совпадает с собственным `branch_ref` (противоречивая конфигурация цепочки) — точка в validation_gate/задачном парсере. Зона — target + orchestrator.

---

## Проход 17 — задача `p5-03-describe-rules` (2026-07-07), чистый codex-primary (supervisor тоже codex)

Прогон: `worc run` → `done`, ветка `feat/p5-compile` (`branch_mode: existing`), PR [#11](https://github.com/VladimirMakarevich/wastech-mdlint/pull/11) **reuse** (3-й коммит), `fix_iterations=0`. **Смена конфигурации перед прогоном** (по команде оператора, вариант B): supervisor зафиксирован полностью на codex — `provider: codex`, `model: gpt-5.4`, `reasoning: xhigh` (закрытие F39 согласованной codex-конфигурацией вместо обхода claude). Флоу: planning (codex, 337s) → implementation (codex, 516s) → testing (4/4 green) → review (**claude** opus-4-8, 115s, `accept`) → documentation (codex, 80s) → publish. **Все 5 per-step supervisor-наблюдений прошли на codex без крашей** (F39 закрыт); НО **финальный supervisor-summary (finalize) крашнул codex** (F41) → fallback claude. Коммит p5-03 чистый: 4 файла (+598/−29: `compile/describe-rules.ts` +237, тест +238, `index.ts` +123, doc +29). Отчёт: [docs/analysis/p5-03-describe-rules-run-analysis.md](docs/analysis/p5-03-describe-rules-run-analysis.md).

**Обновление по F39.** Вариант B (явный `supervisor.provider: codex` + валидная модель `gpt-5.4`) **закрыл per-step supervisor-краш**: `provider_attempts`/лог — supervisor codex attempt=1 succeeded на всех 5 шагах наблюдения (planning/impl/testing/review/documentation), 0 фоллбэков, durable-сессия supervisor на codex отработала (resume_own_lineage на codex штатно). Остаётся orchestrator-side пробел preflight (не ловит унаследованный мисматч при provider=inherited) — он теперь неактуален для этого конфига, но валиден как защита.

### F41 · finalize-схема structured-output супервизора (`DELTA_OUTPUT_SCHEMA` + `_FOLLOW_UPS_SCHEMA`) не OpenAI-strict-совместима → supervisor-finalize крашит codex (тот же класс, что F24) · **MEDIUM** · уверенность HIGH · зона **orchestrator** · статус **VERIFIED FIXED в 0.8.9a4 (Проход 18)**

**VERIFIED FIXED (Проход 18, p5-04, 2026-07-07).** Живой codex-primary прогон с supervisor на codex подтвердил фикс: finalize-supervisor выполнился **на codex без fallback** — `stages/supervisor/run-000000/1-codex/result.json` `status=succeeded exit_code=0` (90s), каталога `2-claude/` НЕТ (фоллбэк не срабатывал). memory_delta **записан codex-супервизором**: `evaluations` `supervisor_final` = `{"summary_written":true,"memory_delta":true,"follow_ups":1}` + `memory_write` append (эпизод `ep_p5-04-synthesize`, entities `core-synthesize`/`core-compile-context`/`core-skill-frontmatter`/`llm001-rule`). Класс закрыт: за прогон 8 codex-evaluator review-проходов + finalize — 0 `invalid_json_schema`. Ниже — исходное описание (Проход 17).

**Доказательство.** `stages/supervisor/run-000000/1-codex/stdout.log` (finalize-вызов): `{"type":"error","error":{"type":"invalid_request_error","code":"invalid_json_schema","message":"Invalid schema for response_format 'codex_output_schema': In context=('properties','memory_delta','properties','lessons','items','properties','scope'), 'required' is required to be supplied and to be an array including every key in properties. Missing 'paths'.","param":"text.format.schema"},"status":400}` → `turn.failed`, `result.json` `exit_code=1 error_class=process_crashed`; `request.json` `--output-schema` присутствует, model `gpt-5.4`. Затем fallback на claude (attempt 2, succeeded, 129s). **Контраст**: 5 per-step supervisor-наблюдений на codex прошли (`run-000123..127/1-codex` succeeded) — observe-turn использует другую, совместимую схему; ломается только finalize с `memory_delta`.

**Корневая причина.** OpenAI structured-output (codex) в strict-режиме требует, чтобы у КАЖДОГО объекта `required` перечислял ВСЕ ключи `properties`. Нарушают: [memory/delta.py:110-118](src/wastech_orchestrator/memory/delta.py#L110-L118) — объект `scope` (`properties: paths/symbols/nodes`) вообще без `required`; тот же класс дефекта у родительского lesson-объекта ([delta.py:122](src/wastech_orchestrator/memory/delta.py#L122) `required` без `rationale`/`scope`/`evidence`/`trust_hint`) и у [_FOLLOW_UPS_SCHEMA supervisor.py:98-113](src/wastech_orchestrator/core/supervisor.py#L98) (`required` без `paths`/`action_hint`). Раньше не всплывало, т.к. supervisor-finalize всегда шёл на claude (claude strict-правило не применяет); codex-supervisor (вариант B) впервые его обнажил. Прямой родственник F24 (там codex-evaluator падал на `additionalProperties:false`).

**Рычаг.** [memory/delta.py:96-165](src/wastech_orchestrator/memory/delta.py#L96) (`DELTA_OUTPUT_SCHEMA`) и [core/supervisor.py:98-113](src/wastech_orchestrator/core/supervisor.py#L98) (`_FOLLOW_UPS_SCHEMA`): привести к OpenAI-strict — в каждом объекте `required` = все ключи `properties` (опциональность выражать через nullable-типы, а не через отсутствие в `required`). Либо общий codex-адаптерный «strict-ify» output-schema перед отправкой (как, вероятно, было сделано для F24). Зона — orchestrator (пакетные схемы).

**Влияние.** Замаскировано fallback'ом на claude (задача `done`, summary+memory_delta написаны claude). Но: (1) при `agents.allowed:[codex]` или недоступном claude finalize-summary + memory_delta + follow_ups **всегда** проваливались бы → нет PR-body/памяти; (2) на codex-supervisor каждый прогон = 1 сожжённая codex-попытка + латентность fallback на finalize; (3) memory_delta от codex-супервизора не пишется (его пишет claude-fallback) — на single-provider codex подсистема памяти на finalize нема.

**Резолюция (2026-07-07, ветка `feat/output-schema-codex-strict`).** ADR [supervisor-output-schema-codex-strict.md](docs/backlog/supervisor-output-schema-codex-strict.md), вариант A, расширен на весь класс: OpenAI-strict приведены ВСЕ codex-bound output-схемы — `DELTA_OUTPUT_SCHEMA` ([memory/delta.py](src/wastech_orchestrator/memory/delta.py)), `_FOLLOW_UPS_SCHEMA`/`_HANDOFF_SCHEMA`/`_finalize_schema` ([core/supervisor.py](src/wastech_orchestrator/core/supervisor.py)) и `_FINDINGS_SCHEMA` ([evaluator.py](src/wastech_orchestrator/core/flow/nodes/evaluator.py)) — в каждом объекте `required` = все ключи `properties`, опциональность через nullable-типы (парсеры уже null-толерантны, правок не потребовали). Регрессионный барьер: [tests/core/test_output_schema_strictness.py](tests/core/test_output_schema_strictness.py) теперь проверяет ОБА strict-инварианта (`additionalProperties:false` + `required`-полнота) по всему инвентарю схем. **Empirical codex re-run — ВЫПОЛНЕН в Проходе 18 (p5-04), finalize на codex без fallback — см. выше.**

---

## Проход 18 — задача `p5-04-synthesize` (2026-07-07), проверка F41 + F24 на codex-ревью

Прогон: `worc run` → `done`, ветка `feat/p5-compile` (`branch_mode: existing`), PR [#11](https://github.com/VladimirMakarevich/wastech-mdlint/pull/11) **reuse** (4-й коммит), **`fix_iterations=7`** (первый глубокий fix-loop кампании), ~2ч40м. Версия **0.8.9a4** (F41-фикс + guard-тест). **Смена конфигурации** (по команде оператора): рабочие узлы возвращены на claude — planning/implementation/fixing/documentation = **claude / claude-sonnet-5 / xhigh**; review перевёрнут обратно на **codex / gpt-5.4 / xhigh**; supervisor оставлен на **codex** (проверка F41); глобальный `primary` возвращён на claude. Флоу: planning (claude 652s) → implementation (claude 863s) → [testing → review(**codex**) → fixing(claude)] ×7 → review accept → documentation (claude 212s) → finalize supervisor (**codex**, 90s) → publish. Коммит p5-04: 9 файлов, **+1547/−17** (`synthesize.ts` +382, `compile-context.ts` +223, тесты 802 строки). Отчёт: [docs/analysis/p5-04-synthesize-run-analysis.md](docs/analysis/p5-04-synthesize-run-analysis.md). Итоги: **F41 VERIFIED FIXED** (см. выше), **F24 не воспроизводится** (codex-evaluator 8/8 succeeded), новая **F42** (калибровка codex-ревью).

**Обновление по F24.** Codex-evaluator (review) в этом прогоне отработал **8 попыток подряд, все `succeeded`** (`provider_attempts`: review/codex/succeeded ×8), 0 `process_crashed`/`invalid_json_schema` — тогда как в кампании p4 он падал 9/9 (F24). На 0.8.9a4 (strict-схемы, F41-фикс покрыл и `_FINDINGS_SCHEMA`) **F24 не воспроизводится**; codex-ревью не только запускается, но и даёт содержательные вердикты. F24 можно считать закрытым тем же фиксом.

### F42 · codex-as-reviewer (blocking evaluator) чрезмерно дотошен: 7 rework-циклов на одной задаче, дрейф от корректности к тест-полировке · **LOW–MEDIUM** · уверенность MEDIUM · зона **orchestrator (role-prompt/калибровка)** · статус **OPEN (наблюдение)**

**Доказательство.** `evaluations` p5-04: 7×`rework` → 1×`accept` (8 review-проходов). Содержание вердиктов (`findings_json`): итерации 1–4 — реальные корректностные HIGH (G6-honesty пустого readingOrder `synthesize.ts:renderReading…`; all-or-nothing `resolveCompileSettings` safeParse; per-field leniency `skill`/`sections`; `contentHash` без provenance-строки); итерации 5–7 — уже полнота тест-покрытия/ассертов и валидация границ (`Document Architecture` без unit-теста; routed missing-import не ассертит resolved path; `hubMinInDegree` принимает `0/-1/1.5`). Все чеки при этом всегда зелёные (`passed=false`=0). Итог: `fix_iterations=7`, diff вырос до +1547 (тесты +802), время ~2ч40м. Loop прогрессировал (каждый раунд — НОВОЕ, не «двигание ворот» на одном месте) и сошёлся на accept в пределах бюджета (`review_fix`≤15).

**Корневая причина.** review-роль ([packaged/flows/roles/implementation/review.md] / target `.worc/flows/.../review.md`) + `blocking: true` evaluator по умолчанию побуждают возвращать `rework` на КАЖДЫЙ найденный HIGH, включая полноту тестов и защитную валидацию входов — при мощной модели (codex gpt-5.4/xhigh) на большом узле (synthesize) это даёт длинный последовательный loop (по одному findings-батчу за проход). Не баг: система работает как задумано, но стоимость (время/токены/удвоение тестов) высока, а поздние итерации — diminishing returns.

**Рычаг.** Варианты (не срочно): (1) review-роль — просить группировать ВСЕ находки одного прохода в один батч (снижает число раундов), явно разделять blocking-корректность vs advisory-полнота; (2) `max_rework_per_stage` на review-узле как потолок глубины; (3) отдельный неблокирующий `testing_quality`-evaluator для coverage-замечаний, чтобы correctness-review не блокировал на тест-полировке. Зона — orchestrator (role-prompt + flow-node knob). Для теста ценно как раз наблюдать дефолтную калибровку.

**Влияние.** Качество результата высокое (реальные баги пойманы и починены, тесты усилены), но одна средняя задача = 7 fix-циклов / ~2ч40м / +802 строки тестов. На больших узлах дефолтный blocking-review может доминировать время/стоимость прогона. Сопутствующе: `state.db tasks.review_fix_cycles=0` при 7 фактических review-реворках (`fix_iterations=7` корректен) — счётчик review_fix, похоже, не персистится (мелкий audit-пробел, отдельно от F42).

**Рецидив (Проход 21, p6-01, 2026-07-09).** Тот же феномен при review=`high`: 6 rework-циклов, сошлось на 7-м review (в бюджете). БОЛЬШИНСТВО раундов прогрессивные (13+ реальных edge-case багов в домене glob/workspace/monorepo). Уточнения: (1) **аудит-пробел review_fix закрыт** — колонка `review_fix_total=6` (v14/F49) фиксирует глубину, `review_fix_cycles=0` корректно обнуляется при сходе; (2) обнаружен НОВЫЙ оттенок «двигание ворот» (review противоречит спеке/себе между раундами) → вынесен в отдельную **F43**; (3) апстрим-рычаг глубины — проактивное покрытие краёв в роли implementation (см. главный рычаг отчёта p6-01).

---

## Проход 19 — задача `p5-05-compile-config-cli` (2026-07-07), стабильность F41/F24 + F42 при review reasoning=high

Прогон: `worc run` → `done`, ветка `feat/p5-compile` (`branch_mode: existing`), PR [#11](https://github.com/VladimirMakarevich/wastech-mdlint/pull/11) **reuse** (5-й коммит), **`fix_iterations=1`**. Версия **0.8.9a4**. Конфигурация как в Проходе 18, но **review переведён на `codex/gpt-5.4/high`** (оператор снизил reasoning с xhigh). Флоу: planning (claude 696s) → implementation (claude 507s) → testing (4/4) → review (**codex** 171s, rework: реальный `--cwd`-баг) → fixing (claude 138s) → review (**codex** 223s, **accept**) → documentation (claude 177s) → finalize supervisor (**codex**, без fallback) → publish. Коммит: 13 файлов, **+420/−166** (config-schema strict `compile` +40, CLI compile, тесты config-v2 +82 / cli +79). Отчёт: [docs/analysis/p5-05-compile-config-cli-run-analysis.md](docs/analysis/p5-05-compile-config-cli-run-analysis.md). **Новых F нет** — подтверждения:

- **F41 стабилен**: finalize supervisor на codex `succeeded exit 0`, каталога `2-claude/` нет (без fallback); memory_delta записан codex-супервизором (`supervisor_final` `memory_delta:true`).
- **F24 стабилен**: codex-evaluator review 2/2 `succeeded` (rework→accept), 0 крашей; ни одного фоллбэка во всём прогоне (`provider_attempts`: planning/impl/fixing/documentation=claude, review=codex, все succeeded).
- **F42 — усиление наблюдения (review reasoning-эффект):** при review на `high` (не xhigh) loop сошёлся за **1 rework-цикл** (реальный `--cwd`-баг) против **7** у p5-04 (xhigh), а сами review-проходы 171–223s против 300–800s. Задача p5-05 меньше synthesize, поэтому не чистый A/B, но направление согласуется с рычагом F42: **снижение reasoning блокирующего review естественно укорачивает глубину/стоимость loop**. Кандидат-рычаг для F42 дополнить: reasoning review-узла как регулятор дотошности (`high` вместо `xhigh` по умолчанию для больших кодовых узлов).

---

## Проход 20 — задача `p5-06-compile-tests` (2026-07-07), ФИНАЛ фазы P5

Прогон: `worc run` → `done`, ветка `feat/p5-compile` (`branch_mode: existing`), PR [#11](https://github.com/VladimirMakarevich/wastech-mdlint/pull/11) **reuse** (6-й, последний коммит фазы), **`fix_iterations=1`**. Версия **0.8.9a4**, конфиг как в Проходе 19 (review codex/gpt-5.4/high). Флоу: planning (claude 355s) → implementation (claude 194s) → testing (4/4) → review (**codex** 90s, rework: тавтологичный CJK-budget тест) → fixing (claude 120s) → review (**codex** 98s, **accept**) → documentation (claude 88s) → finalize supervisor (**codex**, без fallback) → publish. Коммит: 5 файлов, **+96/−8** (тесты `compile-context`/`compile-synthesize`/`cli` + docs `06-compile-tests.md`/`index.md`). Отчёт: [docs/analysis/p5-06-compile-tests-run-analysis.md](docs/analysis/p5-06-compile-tests-run-analysis.md). **Новых F нет** — подтверждения: F41 finalize на codex `succeeded` без fallback; F24 codex-review 2/2 succeeded (содержательный rework про тавтологичный тест → accept); **0 фоллбэков во всём прогоне**; loop 1 цикл (как p5-05, review=high). Вся цепочка P5 (p5-01…p5-06, 6 коммитов) собрана в одном PR #11 на `feat/p5-compile`.

**Итог кампании codex-primary (проходы 15–20).** Все обнаруженные codex-primary баги закрыты и подтверждены в бою: **F38** (resume-argv, VERIFIED Проход 16), **F39** (supervisor provider/model — closed вариантом B, Проход 17), **F41** (finalize strict-схемы, VERIFIED Проход 18), **F24** (codex-evaluator strict — не воспроизводится с Прохода 18). Codex — полноценный основной провайдер во всех ролях (planning/impl/fixing/documentation при желании, review, supervisor per-step + finalize). Открытые orchestrator-side (в [follow_ups.md](docs/backlog/follow_ups.md), не блокеры): **F42** (калибровка/depth блокирующего review, регулируется reasoning), review_fix_cycles counter, F39-preflight (унаследованный мисматч), F40 (depends_on×branch_ref).

---

## Проход 21 — задача `p6-01-repo-scan-detection` (2026-07-09), фаза 6, первый прогон (claude-primary; review/supervisor codex)

Прогон: `worc run` → **`done`**, ветка `feat/p6-init` (`branch_mode: new` + override `branch_name`), PR [#12](https://github.com/VladimirMakarevich/wastech-mdlint/pull/12) (новый, 1 коммит `b1f8cad`, `auto_merged: false`), **`fix_iterations=6`** (review сошёлся на 7-м проходе, потолок 15 не достигнут). Версия оркестратора — **main** (со всеми мержами, вкл. content-flows PR #25; `DB_SCHEMA_VERSION=15`). **Перед прогоном потребовался greenfield-сброс `state.db` v13→v15** (деструктивный bump v15 «multiple editing lineages», коммит `a31e0fd`; сделан бэкап `.pre-p6.bak` — не баг, [[greenfield-mvp-no-migration]]). Флоу `implementation` (без `flow:` → дефолт), `refinement` не выполнялся (задача хорошо специфицирована): planning(claude opus/high 578s) → implementation(claude opus/high 562s) → [testing 4/4 → review(codex gpt-5.4/high)=rework → fixing(claude **sonnet-5**/xhigh)]×6 → testing → review=**accept** (findings=`[]`) → documentation(claude 236s) → finalize(supervisor codex 112s) → publish. **Ноль фоллбэков/ретраев/крашей** за 40 вызовов; checks 28/28 passed. Токены: supervisor 38.75M input (**88% прогона**, 24 вызова, advisory), review 5.33M (7). Отчёт: [docs/analysis/p6-01-repo-scan-detection-run-analysis.md](docs/analysis/p6-01-repo-scan-detection-run-analysis.md).

### F43 · review→fix «двигание ворот»: locked-decision задачи не трактуется как нерушимая (review предлагает fix против спеки, fixing исполняет, следующий review откатывает) · **MEDIUM** · уверенность HIGH · зона **orchestrator (role-prompts)** · статус **OPEN**

**Доказательство.** `evaluations` p6-01 (state.db): review #4 (id 16, `findings_json`) потребовал сделать fallback MDX-aware (`**/*.md` неполон — сканер собирает `.md`+`.mdx`), предложив вариант, меняющий спеко-константу; fixing #4 сменил на `**/*.{md,mdx}`; review #5 (id 20) откатил: «fallback больше не соответствует контракту P6.01 — требуется буквально `**/*.md`». `task.normalized.json` дословно: «`**/*.md` stays the fallback». Аналог: review #2→#6 по `noiseDirNames` (#2 просит учитывать tunable → fixing добавил ПУБЛИЧНЫЙ параметр → #6 (low) пометил как утечку внутреннего knob в public API).

**Корневая причина.** Роль review ([review.md](src/wastech_orchestrator/packaged/flows/implementation/review.md)) якорится на acceptance-критериях (стр.17,19), но при формулировке fix НЕ обязывает сохранять locked-константы; роль fixing буквально исполнила совет против спеки вместо «спека выигрывает». Это **НОВОЕ относительно F42**, которая утверждала «loop прогрессировал, не двигание ворот» — здесь goal-moving зафиксирован.

**Рычаг.** review-роль: fix, касающийся спеко-locked-константы, обязан её сохранять. fixing-роль ([fixing.md](src/wastech_orchestrator/packaged/flows/implementation/fixing.md)): при конфликте review-совета с явной locked-decision — спека выигрывает, конфликт surface'ить. Обе — packaged `src/wastech_orchestrator/packaged/flows/implementation/{review,fixing}.md` (все репо) или target `.worc/flows/implementation/{review,fixing}.md` (только mdlint).

**Влияние.** Устраняет ~1-2 холостых цикла из 6 (те, что туда-обратно, а не новые баги).

### F44 · packaged content-флоу (PR #25) фатально валятся на preflight/install в любом НЕ-контентном репо (требуют repo-специфичный tool `check_journey`) · **HIGH** · уверенность HIGH · зона **orchestrator** · статус **OPEN**

**Доказательство.** `worc preflight` на mdlint: `flow content_book/content_chapter/content_translate: FAIL` → `preflight: NOT ready`. Точное нарушение (воспроизведено `FlowRegistry.validate_all()`): `[config] node 'constraints': tool 'check_journey' not found under '.../.worc/tools' (expected an executable file at .worc/tools/check_journey)`. У mdlint нет ни `.worc/tools/`, ни блока `tools:`.

**Корневая причина.** Контент-флоу влиты в **packaged built-ins** (коммит `3fb23ad`, PR #25). Preflight/install фатально валидируют ВСЕ packaged-флоу через `validate_all()` ([cli.py:2126-2132](src/wastech_orchestrator/cli.py#L2126-L2132)). Контент-флоу требуют tool-узел `check_journey`, доставляемый `worc install` только для контент-репозитория. Гейт «unregistered tool = fatal» ([[p5-custom-tool-nodes-accepted]]) корректен для флоу оператора, но теперь бьёт по packaged-флоу в каждом репо.

**Рычаг.** [validator.py `validate_flow_against_config`](src/wastech_orchestrator/core/flow/validator.py#L104) / [registry.py `validate_all`](src/wastech_orchestrator/core/flow/registry.py#L106): не проваливать фатально packaged-флоу, чей tool не зарегистрирован в этом репо (WARN вместо FAIL для packaged), либо гейтить контент-флоу наличием `tools:`/`.worc/tools/`.

**Влияние.** Preflight и `worc install` красные на любом репо со свежим оркестратором без `check_journey` — затрагивает **всех** операторов, обновившихся после PR #25. `worc run` НЕ гейтит (`validate_all` только в preflight/install), поэтому p6-01 удалось провести в обход.

### F45 · preflight прячет текст нарушения флоу (печатает только первую строку сообщения валидатора) · **LOW-MEDIUM** · уверенность HIGH · зона **orchestrator (DX)** · статус **OPEN**

**Доказательство.** `worc preflight` печатает `flow content_book: FAIL — flow validation failed (1 violation(s)):` без самой строки нарушения. Причина: [cli.py:2132](src/wastech_orchestrator/cli.py#L2132) — `f"flow {name}: FAIL — {error.splitlines()[0]}"` берёт только первую строку, а реальные нарушения идут ПОСЛЕ `\n` ([validator.py:91](src/wastech_orchestrator/core/flow/validator.py#L91)). Оператор не видит, ЧТО не так (пришлось воспроизводить вручную для F44).

**Рычаг.** [cli.py:2132](src/wastech_orchestrator/cli.py#L2132): печатать все строки нарушения с отступом, не `.splitlines()[0]`.

**Влияние.** Диагностируемость preflight; без фикса F44-класс проблем невидим оператору.

### F46 · supervisor constant-layer доминирует по токенам (advisory-only, ~88% input прогона, усилен фикс-циклами) · **MEDIUM (стоимость)** · уверенность HIGH · зона **target-config (+ дизайн-заметка orchestrator)** · статус **OPEN**

**Доказательство.** Агрегат токенов по `result.json`: supervisor **38 753 207 input** (35.49M cached), 24 вызова — против review 5.33M/7, implementation 4.5K/1, planning 4.4K/1. 88% всего input-объёма. Target-конфиг переопределяет supervisor на `codex/gpt-5.4/xhigh`, тогда как packaged-дефолт ([config.example.yaml:234-238](src/wastech_orchestrator/packaged/config.example.yaml#L234-L238)) = `claude/opus-4-8/medium`.

**Корневая причина.** Supervisor проверяет КАЖДЫЙ завершённый шаг ([[supervisor-constant-layer]]) с ре-ингестом растущего контекста; при xhigh на codex и глубокой review-петле (24 шага) — доминирующая стоимость при advisory-only (не может делать rework). Глубина петли (F42/F43) мультиплицирует число supervisor-вызовов.

**Рычаг.** Target `.worc/config.yaml` → `supervisor.reasoning: medium` (вернуть к дефолту) — дешёвый выигрыш без кода. Дизайн-опция (orchestrator, обсуждаемо): не запускать supervisor на детерминированных `testing`-узлах.

**Влияние.** Снижение reasoning supervisor + сокращение фикс-циклов (главный рычаг F42) кратно срезают токен-стоимость прогона.

**Обновление (Проход 22, p6-02).** Рычаг ФАКТИЧЕСКИ ПРИМЕНЁН и ВАЛИДИРОВАН: между p6-01 и p6-02 конфиг supervisor сменён `codex/gpt-5.4/xhigh` → `claude/opus-4-8/medium` (packaged-дефолт; тест-дорожка не трогала — внешнее изменение оператора). supervisor input упал **38 753 207 → 2 922** (~13 000×). Уточнение root cause: доминирующий множитель — не reasoning, а **provider resume-поведение** (codex ре-ингестит контекст, claude — дельта) → вынесено в **F47**. **Статус F46: закрыт сменой конфига на дефолт.**

---

## Проход 22 — задача `p6-02-rule-inference` (2026-07-09), фаза 6, вторая задача (branch existing, PR reuse)

Прогон: `worc run` → **`done`**, ветка `feat/p6-init` (`branch_mode: existing` + `branch_ref`, поверх коммита p6-01 `b1f8cad`), PR [#12](https://github.com/VladimirMakarevich/wastech-mdlint/pull/12) **переиспользован** (F27, +1 коммит, title/body от p6-01), **`fix_iterations=3`** (review сошёлся на 4-м проходе, чище p6-01). `depends_on` не задан → dependency-гейт не сработал (зависимость от p6-01 через общую ветку). Флоу `implementation`, refinement пропущен. Провайдеры: claude-primary, review codex/high, **supervisor теперь claude/medium** (см. F46/F47). 0 фоллбэков/крашей, checks 16/16. Отчёт: [docs/analysis/p6-02-rule-inference-run-analysis.md](docs/analysis/p6-02-rule-inference-run-analysis.md).

**Ключевые наблюдения относительно прежних F:** **F43 НЕ воспроизвёлся** (findings review 3 циклов прогрессивные и разные, без «двигания ворот» → thrash был эпизодом p6-01, не систематикой); **F42 рецидив слабее** (3 цикла vs 6 у p6-01 — глубина падает по мере накопления модулей); **F46 закрыт** сменой конфига (см. выше).

### F47 · codex при session-resume ре-ингестит весь растущий контекст (codex CLI replay'ит rollout), claude резюмит дельтой — затрагивает ВСЕ codex resume-узлы (supervisor/documentation/fixing), не только supervisor · **MEDIUM** · уверенность HIGH (симптом+механизм) / MEDIUM (долларовая величина, cached) · зона **orchestrator (выбор провайдера для resume-ролей / providers/codex.py)** · статус **OPEN — требует investigation: есть ли у codex delta/server-side resume**

**Доказательство (реальные `usage.input_tokens` per-call, обе задачи, одна durable-сессия, resume=True).** p6-01 supervisor `1-codex` (session `cec99ca35f90`): 3 599 990 / 69 051 / 278 744 / 322 503 / 366 849 / 509 479 … → растёт с историей, 24 вызова = **38 753 207** input. p6-02 supervisor `1-claude` (session `fba9df1ac646`): 3 / 2 777 / 1 / 1 / 1 … → дельта, 15 вызовов = **2 922** input. (Разница провайдеров — следствие смены конфига supervisor между прогонами, см. F46.)

**Механизм (почему codex шлёт ВСЁ, а не дельту).** Адаптер использует НАТИВНЫЙ codex-resume, а не сам пере-отправляет транскрипт: [codex.py:180-185](src/wastech_orchestrator/providers/codex.py#L180-L185) строит `codex exec [exec-options] resume <SESSION_ID>`, prompt читается со stdin. Значит перезагрузку делает **сам codex CLI**: при `resume <id>` он реконструирует весь локальный rollout сессии и переотправляет его в API как input каждый ход → `input_tokens` растёт с историей (O(шагов²) на постоянном слое). Смягчение: codex-prompt-caching — на p6-01 supervisor **35.49M из 38.75M это `cached_input_tokens`** (~92%), т.е. в долларах разрыв меньше, чем в объёме (свежие токены codex ~3.27M против claude ~2.9K + codex reasoning 125K). Claude-resume, наоборот, держит сессию серверно и шлёт дельту (input ~1). Открытый вопрос для оркестратора: есть ли у codex CLI режим resume без полного replay (server-side thread) — если нет, это врождённое свойство stateless-resume codex, и рычаг только «выбор провайдера».

**Распространение на ОБЫЧНЫЕ узлы (не только supervisor).** Комментарий в самом адаптере [codex.py:338-339](src/wastech_orchestrator/providers/codex.py#L338-L339) перечисляет resume-узлы: **«supervisor, documentation, rework, fixing»**. Т.е. на codex-primary конфиге (как кампания P5) editing-lineage узлы **implementation→fixing→documentation** резюмят одну сессию и на каждом фикс-цикле codex так же реконструирует растущий rollout — плата за resume есть у ЛЮБОГО codex-узла, не только у supervisor (у supervisor хуже всего из-за частоты — вызов на каждом шаге). На этом прогоне эти узлы шли на claude, поэтому эффект виден только на supervisor (единственная codex-resume роль на p6-01).

**Рычаг.** (1) Держать `supervisor.provider: claude` (packaged-дефолт) для resume-тяжёлых высокочастотных ролей — уже применено. (2) Понять первопричину: probe codex CLI на наличие server-side/delta-resume; если нет — задокументировать, что codex-resume узлы платят replay (в основном cached), и это фактор при выборе codex-primary для длинных петляющих задач. (3) Возможная оптимизация в `providers/codex.py` — не resume-ить высокочастотный advisory-слой, а слать компактный контекст. Не нарушает provider-абстракцию (выбор в конфиге).

**Влияние.** ~13 000× разница ОБЪЁМА input-токенов supervisor (codex 38.75M vs claude 2 922); в долларах меньше из-за 92% cached, но свежие токены + reasoning + латентность растут по шагам. Ключево: то же поведение — у codex implementation/fixing/documentation на codex-primary конфиге, что делает codex дороже claude на длинных фикс-петлях. Объясняет и уточняет F46.

---

## Проход 23 — задача `p6-03-interactive-prompts` (2026-07-09), фаза 6, третья задача — ПЕРВЫЙ оператор-driven HITL за кампанию

Прогон: `worc run` → **`done`**, ветка `feat/p6-init` (`branch_mode: existing`), PR [#12](https://github.com/VladimirMakarevich/wastech-mdlint/pull/12) **reuse** (3-й коммит), **`fix_iterations=5`**, ~2ч12м (из них ~9.5 мин — ожидание HITL-ответа). Провайдеры: claude-primary, review codex/high, supervisor claude/medium (baseline). 0 фоллбэков/крашей, checks 24/24. Отчёт: [docs/analysis/p6-03-interactive-prompts-run-analysis.md](docs/analysis/p6-03-interactive-prompts-run-analysis.md). **Новых F нет.**

**Веха — первый оператор-driven HITL (положительно).** planning-агент поймал реальное противоречие спеки: промпт `language` перечислен в задаче, но нигде не определён (нет в glossary/тест-фикстурах P6.05/шагах скилла P8.02, не мапится на config-ключ). Задал `kind=question`/`risk=clarification` через telegram (`hitl/planning.json`, `telegram_message_id=250`); оператор ответил (option a — выкинуть `language`, делать 3 промпта); planning **возобновил ту же сессию** (`result.session_id=session:7cf6f793c227` в обоих запусках planning#1/#2, 75s vs 807s) → задача собрала корректную версию. Первый HITL за всю кампанию с ОТВЕТОМ оператора (раньше — только auto-resolve `kind=approval` в autonomous, прогон 5). Подтверждает §4 (session-resume на HITL re-entry) + §14 (telegram round-trip) + 8h-timeout/60s-heartbeat. Не баг — образцовое использование planning-autonomy.

**Относительно прежних F:** **F47/F46 подтверждены 3-й раз** (supervisor на claude = 5 460 input / 21 вызов, дёшев); **F42 рецидив** (5 циклов, прогрессивные, горячая зона — existing-config overwrite/merge/skip + prompter); **F43 НЕ воспроизвёлся** 2-й прогон подряд (thrash — эпизод p6-01). Target-сторона (не наша дорожка): убрать неопределённый промпт `language` из спеки p6-03/roadmap.

---

## Проход 24 — задача `p6-04-config-writer-schema` (2026-07-09), фаза 6, четвёртая задача — ПЕРВЫЙ терминал НЕ-`done` за фазу 6 (`manual_action_required`)

Прогон: `worc run` (фон, вернул exit 2) → истинный итог **`manual_action_required`**, узел `review`, петля `review_fix`, `limit_exhausted=max_fix_cycles`, **`fix_iterations=15`** (потолок), `finished_at=2026-07-09T22:55:15Z`. Ветка `feat/p6-init` (`branch_mode: existing`), **PR не создан** (`pr_url=null`), HEAD ветки остался на p6-03 (`788e9f2`). Провайдеры: claude-primary (opus-4-8/high), review codex/gpt-5.4/high, **fixing claude/sonnet-5/xhigh** (per-node override), supervisor claude/opus-4-8/medium (baseline). Отчёт: [docs/analysis/p6-04-config-writer-schema-run-analysis.md](docs/analysis/p6-04-config-writer-schema-run-analysis.md).

**Что произошло (хронология из `node_runs`/`provider_attempts`).** planning#62 → implementation#63 (реально собрала фичу: `config-writer.ts` + 12 файлов / 929 вставок, локально `npm test` 428 passed — из `implementation.out.md`) → затем 15 циклов `review_fix`: review#65→rework, fixing#66 **succeeded** (322K events, ~4 мин, реальная правка), review#68→rework, fixing#69 **succeeded** (~7 мин), review#71→rework, **fixing#72 FAILED за ~2с** — и **все последующие fixing (72,75,78,…,105 — 12 штук) FAILED за 2-3с**, тогда как review (codex) каждый раз отрабатывал 4-7 мин и переоткрывал те же blocking-findings. Потолок 15 достигнут → `MANUAL_ACTION_REQUIRED`.

**Корень (одной фразой):** на fixing#72 (~21:54Z) claude-подписка упёрлась в **five-hour session limit / out_of_credits (HTTP 429)**; оркестратор **не распознал это как rate-limit** и продолжил крутить петлю с fixing-узлом, который стал мгновенным no-op'ом — 12 «мёртвых» фикс-циклов дожгли потолок. Побочные находки: провалившийся fixing «протекает» как `done` (F49), а пост-мортем-артефакт скрыл реальную причину (F50). Целевой репозиторий: работа p6-04 осталась **staged, но не закоммичена** в рабочем дереве `feat/p6-init` (terminal cleanup заблокирован — «working tree has unaccounted changes»), task-файл остался в `tasks/pending/` — ожидаемо для `manual_action_required`, но дерево грязное и это заблокирует p6-05, пока не разрешено оператором.

**Относительно прежних F:** **F42 рецидив** (даже 2 реальных фикс-цикла не сошлись; горячая точка — тот же CI-workflow judgment-call, что имплементатор явно пометил на решение оператора); **F47/F46 подтверждены 4-й раз** (supervisor claude/medium: 46 вызовов / 2 786 input — дёшев даже на длинной петле); **F45 родственник — F50** (пост-мортем-репорт тоже прячет текст). **F44 не проверялся отдельно** (preflight в этом прогоне не гейтил — задача была запущена ранее в обход).

### F48 · Claude session-limit (HTTP 429 / `five_hour` / `out_of_credits`) классифицируется как `task_failure` вместо `RATE_LIMITED` — РЕЦИДИВ content-rework F1, вторая независимая репродукция (теперь на code-task, узел `fixing`) · **P0 / HIGH** · уверенность HIGH · зона **orchestrator (`providers/claude.py`)** · статус **OPEN**

**Доказательство.** [.worc/logs/p6-04-config-writer-schema/stages/fixing/run-000105/1-claude/result.json] — `"error_class": "task_failure"`, `"failure_subtype": "success"`, `"message": "the provider completed without satisfying the task (success)"`, `final_message="You've hit your session limit · resets 1:30am (Europe/Warsaw)"`, `usage.output_tokens=0`. `stdout.log` того же прогона содержит `{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","resetsAt":1783639800,"rateLimitType":"five_hour","overageStatus":"rejected","overageDisabledReason":"out_of_credits"...}}` и терминальный `{"type":"result","subtype":"success","is_error":true,"api_error_status":429,...,"result":"You've hit your session limit..."}`. **`stderr.log` = 0 байт.** В `state.db provider_attempts` fixing#72..#105 — все `status=failed, error_class=task_failure, exit_code=1`.

**Корневая причина.** claude-адаптер распознаёт rate-limit только по **stderr-сигнатуре** ([claude.py:102-103](src/wastech_orchestrator/providers/claude.py#L102-L103): regex `rate limit|\b429\b|too many requests|quota exceeded|overloaded`). Но `claude` CLI отдаёт session-limit **структурно в stdout** (`rate_limit_event` + `result`-событие с `is_error:true`/`api_error_status:429`), stderr пустой → сигнатура не срабатывает. Парсер [`parse_stream_json` (claude.py:366-390)](src/wastech_orchestrator/providers/claude.py#L366-L390) на этом событии считает `succeeded = (not is_error) and subtype=="success"` → `False`, `failure_subtype = subtype` → `"success"`, и **полностью игнорирует `api_error_status`/`rate_limit_event`** → downstream получает generic quality-`task_failure`. `RATE_LIMITED` входит в [`FALLBACK_ELIGIBLE` (base.py:66-79)](src/wastech_orchestrator/providers/base.py#L66-L79), `task_failure` — нет; значит мисклассификация одновременно и гасит fallback, и не запускает defer/park.

**Рычаг.** [`parse_stream_json` (claude.py:366-390)](src/wastech_orchestrator/providers/claude.py#L366-L390): при `api_error_status==429` (или `rate_limit_event.status=="rejected"`, или тексте session-limit) отдавать `ErrorClass.RATE_LIMITED` (infra), а не generic quality-fail. Точь-в-точь **P0 F1 из AUDIT-content-rework-run-2026-07-10.md — предложено, НЕ построено** ([[content-rework-run-session-limit-audit]]); теперь есть вторая независимая репродукция вне контент-флоу → приоритет подтверждён.

**Влияние.** Любой claude-узел (здесь — `fixing`), упёршийся в session-limit подписки, помечается как «агент не справился с задачей» вместо «инфра-лимит». Каскад: нет fallback на codex, нет park/defer, петля продолжает крутиться (см. **F49**). Затрагивает всех операторов на claude-подписке; проявляется на любой длинной задаче, пересекающей 5-часовое окно.

### F49 · session-limited `fixing`-узел «протекает» как `done` и крутит `review_fix`-петлю до `max_fix_cycles` вместо fallback/park — усилитель F48 · **P0 / HIGH** · уверенность HIGH · зона **orchestrator (`core/flow/nodes/agent.py` + router fallback; следствие F48)** · статус **OPEN**

**Доказательство.** `state.db node_runs`: fixing#72,75,78,81,84,87,90,93,96,99,102,105 — все `status=failed, outcome=done, provider_used=claude, error_class=task_failure`, каждый ~2-3с; между ними review#74..#107 `succeeded, outcome=rework` (codex, 4-7 мин, переоткрывает те же findings). Итог `failure_report.json`: `limit_exhausted=max_fix_cycles, counters.review_fix=15`. Т.е. **12 фикс-циклов не сделали НИЧЕГО**, но каждый засчитан петлёй как состоявшийся ход → потолок `max_fix_cycles=15` дожжён (target `.worc/config.yaml:25`).

**Корневая причина.** agent-узел поднимает `NodeInfraError` (терминал → park) **только если `outcome.result is None`** ([agent.py:333-337](src/wastech_orchestrator/core/flow/nodes/agent.py#L333-L337)). Session-limit вернул НЕ-`None` result (терминальное `result`-событие `subtype:"success"`, is_error), поэтому инфра-исключение не взводится; quality-`task_failure` **не** входит в `FALLBACK_ELIGIBLE` → router возвращает его как есть (codex не пробуется), а [`_agent_outcome` (agent.py:694-703)](src/wastech_orchestrator/core/flow/nodes/agent.py#L694-L703) безусловно мапит в `NodeOutcome("done")`. Движок берёт forward-edge fixing→review; review находит blocking → [`_charge_rework` (engine.py:282-299)](src/wastech_orchestrator/core/flow/engine.py#L282-L299) инкрементит `review_fix` — и так ×15. Ремонт F48 (→`RATE_LIMITED`) чинит корень: (а) router уходит в fallback на codex (`FALLBACK_ELIGIBLE`), (б) при исчерпании fallback `outcome.result is None` → `NodeInfraError` → park. Прямой рычаг F49: провалившийся `fixing` (любой terminal-error на fix-узле) не должен считаться productive `done`-циклом. Это P0 **F5/F6 из content-rework audit** (park + пауза очереди при rate-limit) — предложено, НЕ построено.

**Рычаг.** (1) Через F48: переклассификация в `RATE_LIMITED` включает уже существующие ветки fallback→codex и infra-park. (2) Прямо: в [agent.py:333-337](src/wastech_orchestrator/core/flow/nodes/agent.py#L333-L337)/[`_agent_outcome`](src/wastech_orchestrator/core/flow/nodes/agent.py#L694-L703) — терминально-провалившийся fix-узел не отдаёт `done` (park или отдельный `fail`-outcome без инкремента productive-петли). (3) F5/F6: park задачи + пауза очереди при `RATE_LIMITED` до `resetsAt`.

**Влияние.** Одна упершаяся в лимит попытка fix превращается в гарантированный прожёг всех оставшихся фикс-циклов (здесь 12) с полноценными codex-review-прогонами на каждом (см. cost ниже) — деньги/время сожжены, диагноз замаскирован под «review-thrash». Без F48+F49 любая claude-задача, поймавшая session-limit в фазе fixing, детерминированно доходит до `max_fix_cycles`.

### F50 · `failure_report.json`/`stuck.md` жёстко пишут `last_review_findings=None`/`final_diff=""` — пост-мортем-артефакт скрывает реальную причину (были 3 blocking-findings + 929-строчный staged diff) · **P2 / LOW-MEDIUM (DX/диагностируемость)** · уверенность HIGH · зона **orchestrator (`core/flow/recorder.py`)** · статус **OPEN** · родственник **F45**

**Доказательство.** `stuck.md` p6-04: «## Last blocking review findings\n\n(none)» и «## Final diff\n\n```diff\n```» (пусто); `failure_report.json`: `"last_review_findings": [], "final_diff": ""`. Но фактический последний review [review/run-000107/1-codex/result.json] вернул **3 findings (2 blocking, 1 medium)** в `structured_output`, а рабочее дерево содержит **929 вставок / 12 файлов** staged. Оператор, открыв stuck-артефакт, видит «петля исчерпана, findings нет, diff пуст» — противоречит реальности и не наводит на session-limit.

**Корневая причина.** [`FlowRecorder.write_failure_report` (recorder.py:48-70)](src/wastech_orchestrator/core/flow/recorder.py#L48-L70) хардкодит `last_check_log=None, last_review_findings=None, final_diff=""`, хотя нижележащий [`write_failure_report` (ledger.py:147-215)](src/wastech_orchestrator/ledger.py#L147-L215) полноценно принимает эти поля. Данные доступны: последний verdict — в `state.db evaluations.findings_json`, diff — из рабочего дерева/`current.diff`.

**Рычаг.** [recorder.py:48-70](src/wastech_orchestrator/core/flow/recorder.py#L48-L70): передавать в `write_failure_report` последний evaluator-verdict (из `evaluations`) и текущий diff вместо `None`/`""`. Дёшево, ломает только тексты двух артефактов. Смежно с [[reliable-stop-adr]]-инвариантом «нет молчаливого fail без диагноза» — здесь диагноз есть, но пустой.

**Влияние.** Диагностируемость терминалов `manual_action_required`: без фикса каждый разбор такого прогона требует ручного восстановления findings/diff из per-node артефактов и `state.db` (как в этом пост-мортеме). Не влияет на исход прогона, влияет на скорость и точность разбора.

---
