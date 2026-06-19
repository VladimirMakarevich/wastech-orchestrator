# P1 Audit — review of implementation vs plan

Дата: 2026-06-19.

Проверка выполнена против двух планов:

- [p1-engine.md](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/flows/p1-engine.md)
- [p1-step-b-wiring-draft.md](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/flows/p1-step-b-wiring-draft.md)

Цель аудита: найти уже реализованные части, а также все подтверждённые пропуски, регрессии и недоведённые до конца места в cutover на flow engine.

## Итог

Cutover на engine действительно доведён до live path: orchestration идёт через `_engine_run` / `_run_phases`, `node_runs` и checkpointing работают, data-driven `output_artifact`/decomposition hooks есть, fan-out по подзадачам реализован, recovery path переписан.

Основные незакрытые места находятся не в базовом прохождении happy path, а на швах:

- lifecycle/error handling вокруг publish/finalize;
- восстановление контекста при resume;
- сохранение старых routing/session capabilities;
- синхронизация engine runtime state наружу в `tasks` / ledger / `status`;
- schema cutover старых `state.db`.

Ниже перечислены только подтверждённые находки.

## Findings

### 1. High — publish failure после pre-publish finalize ведёт к неконсистентному lifecycle task file

`PublishNodeRunner` сначала вызывает finalize hook, который переносит task-файл и пишет committed `summary.md`, и только потом делает `commit_code` / `commit_audit` / `push` / `create_pr` ([publish.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/nodes/publish.py:71)).  
Finalize в orchestrator действительно двигает task в lifecycle folder и пишет committed summary ([orchestrator.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:1411)).

Если после этого `push` или `create_pr` падает, верхний engine wrapper уходит в `_fail()` ([orchestrator.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:1432)). Но `_fail()` повторно вызывает `_finalize_task_artifacts(..., FAILED)` уже через старый `p.task_file`, тогда как исходный файл уже был перемещён в `tasks/done/`. `_relocate_task_file()` в этом случае возвращает `dest if dest.exists() else None`, то есть failure-path не гарантирует перенос в `tasks/failed/` ([orchestrator.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:1509)).

Фактический риск:

- failed attempt может остаться на ветке как `done`-артефакт;
- audit lifecycle и git history расходятся с терминальным статусом `FAILED`;
- это происходит на реальном error path после частично успешного publish.

### 2. High — task-level provider override (`task.agents`) потерян на engine path

Task model и router продолжают поддерживать per-stage provider override через frontmatter `agents` ([task/model.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/task/model.py:82), [router.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/routing/router.py:133)). Это также покрыто unit-тестами самого роутера ([test_route_resolution.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/tests/routing/test_route_resolution.py:40)).

Но flow runners на live path вызывают `resolve_route(stage)` без task override:

- [agent.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/nodes/agent.py:68)
- [evaluator.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/nodes/evaluator.py:45)

`RouterPort` в node layer это даже документирует: “P1 routes by stage from config” ([base.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/nodes/base.py:57)).

Это уже не просто недореализованный enhancement, а тихая потеря существующей capability:

- task может валидно задать `agents.review: claude`;
- router умеет это принять;
- engine path этот override не передаст вообще;
- реально исполнится route из config, а не из task.

### 3. High — `session_scope` / `lineage_affinity` пока декларативны, но не обеспечены runtime

Flow contract требует разные semantics для:

- `editing_lineage`;
- `fresh_disposable`;
- `resume_own_lineage`;
- `lineage_affinity` ([flow-contract.md](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/flows/flow-contract.md:158)).

Packaged implementation flow их декларирует, например `implementation` использует `editing_lineage`, а `fixing` — `editing_lineage + lineage_affinity: implementation` ([implementation.yaml](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/packaged/implementation.yaml:25)).

На runtime же в `_Pipeline` хранится только map `provider_id -> session_id` ([orchestrator.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:257)).  
Дальше runners просто читают `session_id` по primary provider:

- agent node: [agent.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/nodes/agent.py:358)
- evaluator node: [evaluator.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/nodes/evaluator.py:149)

После выполнения agent-runner снова обновляет ту же provider-keyed map ([agent.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/nodes/agent.py:400)).

Следствия:

- `fresh_disposable` evaluator может унаследовать session того же provider, если он уже был открыт авторским узлом;
- `lineage_affinity: implementation` не привязана к lineage узла implementation, а только косвенно к provider;
- `resume_own_lineage` как отдельная семантика не реализован;
- YAML и validator это принимают, но runtime контракт не исполняет.

### 4. High — schema cutover `state.db` до v7 не реализован, старые DB могут штамповаться в несовместимое состояние

Комментарии в `state_store.py` заявляют:

- v5: drop FK от `provider_attempts` к `stage_runs`;
- v6: drop `stage_runs`, rename `provider_attempts.stage_run_id -> node_run_id`;
- v7: drop `tasks.interrupted_status` ([state_store.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/state_store.py:32)).

Но `_migrate()` реально делает только добавление `tasks.current_node`, `tasks.flow_run_counters`, `tasks.flow_fingerprint` ([state_store.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/state_store.py:54)).  
`StateStore.open()` сначала прогоняет текущую `_SCHEMA`, потом без shape-migration штампует `PRAGMA user_version=7` ([state_store.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/state_store.py:347)).

Подтверждённая ручная репродукция:

1. Создан synthetic v6 DB с таблицей `provider_attempts(stage_run_id ...)`.
2. `StateStore.open()` успешно открыл DB и поставил актуальную schema version.
3. Первый `record_provider_attempt(...)` упал с `OperationalError: table provider_attempts has no column named node_run_id`.

Это значит:

- version gate формально проходит;
- реальный shape БД остаётся старым;
- запись новых engine audit rows ломается уже при обычной работе.

### 5. Medium — decomposition materialized, но `subtask_spec_path` не доведён до implementation/fixing

План и context-assembly требуют, чтобы edit-узлы в decomposition режиме получали `{subtask_spec_path}` и контекст “Active subtask N of M” ([context-assembly.md](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/flows/context-assembly.md:57)).

Что реализовано:

- subtask artifacts действительно пишутся как `NN-<slug>.md` ([decomposition.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/decomposition.py:170));
- `NodeInputs` содержит `subtask_spec_path` ([base.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/nodes/base.py:196));
- prompt variables умеют подставлять `subtask_spec_path` ([agent.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/nodes/agent.py:361)).

Что не доведено:

- `build_node_inputs()` получает `subtask_spec_path=None` по умолчанию ([wiring.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/wiring.py:106));
- `_engine_run()` его не вычисляет и не передаёт ([orchestrator.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:821));
- `_fan_out_subtasks()` перед каждым unit не переключает `inputs.subtask_spec_path` на активный spec-файл ([orchestrator.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:940));
- в `AgentRunRequest` нет отдельного поля под subtask-spec path, только `task/plan/diff/checks/review/skills` ([providers/base.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/providers/base.py:82));
- packaged role templates `implementation.md` / `fixing.md` вообще не используют `{subtask_spec_path}` ([implementation.md](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/packaged/roles/implementation.md:1), [fixing.md](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/packaged/roles/fixing.md:1)).

Итог: decomposition как control-flow есть, immutable subtask artifacts тоже есть, но ключевой per-subtask context в edit nodes до конца не wired.

### 6. Medium — engine loop budgets живут отдельно от `tasks.fix_iterations` и operator-facing state

`FlowEngine` ведёт бюджеты только в `FlowRunState.loop_counters`, включая глобальный `fix_iterations` через ключ `global_fix_iterations` ([engine.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/engine.py:342), [run_state.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/run_state.py:46)).

`StateStoreRunRecorder.save_checkpoint()` сохраняет только:

- `current_node`;
- `flow_run_counters`;
- `flow_fingerprint` ([recorder.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/recorder.py:39)).

При этом operator-facing surfaces всё ещё читают legacy counters из `tasks`:

- `StateStore.get_counters()` / `save_counters()` работают с `stage_attempts`, `test_fix_cycles`, `review_fix_cycles`, `fix_iterations` ([state_store.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/state_store.py:578));
- `_transition()` сохраняет именно `p.counters`, а не `FlowRunState` ([orchestrator.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:1703));
- ledger пишет `p.counters.fix_iterations` ([orchestrator.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:1781));
- CLI `status` печатает `task.fix_iterations` ([cli.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/cli.py:1063)).

Нигде на engine path нет синхронизации `FlowRunState.loop_counters` обратно в `p.counters` / `tasks.fix_iterations`. Поэтому:

- bounded termination самого engine работает;
- но ledger, `status`, `finalize`, `resume_manual`, `resume_cleanup` и прочие operator-facing поверхности могут показывать stale counters, обычно `0`.

### 7. Medium — planning-selected skills теряются при resume past planning

`_engine_apply_skills()` корректно резолвит planning-proposed skills, записывает их в `p.selected_skills` и обновляет `inputs.skill_paths` ([orchestrator.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:1081)).

Но `_Pipeline` сам документирует, что `selected_skills` “only set when the planning agent runs this process” и на resume past planning будет пустым ([orchestrator.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:262)).

Resume path:

- пересобирает `skill_inventory`, но не `selected_skills` ([orchestrator.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:706));
- `_restore_engine_inputs()` восстанавливает `plan`, `diff`, `review`, `checks`, но не skills ([orchestrator.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:754));
- `build_node_inputs()` берёт `skill_paths` только из `p.selected_skills` ([wiring.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/wiring.py:124)).

Следствие: fresh run доносит `skills_path` downstream, resume past planning уже нет. Чек-лист “planning + skills + recovery/resume” закрыт не полностью.

## Missing Coverage

Ниже не баги сами по себе, а подтверждённые пробелы в тестовом покрытии относительно P1 checklist.

### 1. Нет engine/integration-теста на `task.agents` override

Route override покрыт только на уровне самого `AgentRouter` ([test_route_resolution.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/tests/routing/test_route_resolution.py:40)).  
Не найдено ни одного orchestrator/engine теста, который проверяет, что frontmatter `agents:` реально влияет на live flow execution.

### 2. Нет теста на publish failure после finalize

Есть тест на failure path после branch creation, но до publish success ([test_orchestrator.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/tests/core/test_orchestrator.py:856)).  
Не найден сценарий, где:

- finalize уже перенёс task file;
- затем падает `push` или `create_pr`;
- afterwards проверяется, что task оказался в `tasks/failed/`, а не в `tasks/done/`.

### 3. Нет end-to-end теста на wiring `subtask_spec_path`

Есть allowlist на prompt variables ([test_prompts.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/tests/core/test_prompts.py:25)) и decomposition/recovery сценарии, но нет теста, который бы доказал, что активный subtask spec реально попадает в prompt или provider request.

### 4. Нет resume-теста на сохранение selected skills

Fresh-path skills покрыты тестом `test_planning_selected_skills_reach_downstream_stages` ([test_orchestrator.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/tests/core/test_orchestrator.py:1815)).  
Не найдено теста, где run прерывается после planning, затем `resume()` подтверждает сохранение `skill_reference_paths`.

### 5. Нет реального migration-теста schema v5/v6/v7

`test_db_schema_version.py` проверяет только stamping / refusal по `PRAGMA user_version` ([test_db_schema_version.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/tests/state/test_db_schema_version.py:25)).  
Нет теста на shape migration старой БД и последующую успешную запись `provider_attempts` / чтение без legacy колонок.

### 6. Нет runtime-теста на session isolation semantics

Validator проверяет декларацию `lineage_affinity` / `session_scope`, но не найдено тестов, доказывающих runtime behavior для:

- `fresh_disposable` не наследует editing session;
- `fixing` продолжает lineage `implementation`;
- `resume_own_lineage` имеет отдельную lineage.

## Secondary observations

Эти пункты замечены по пути, но в основной список findings не поднимались, потому что либо не тянут на P1-blocker, либо требуют отдельного решения/подтверждения.

### 1. `current_node` на terminal path не очищается, хотя комментарий говорит обратное

`_go_terminal()` содержит комментарий “`done` clears it”, но сам метод не обнуляет `current_node` / `flow_run_counters` / `flow_fingerprint` ([orchestrator.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:1481)).  
Это не обязательно функциональный баг, но может давать путаницу в `status`, где для завершённой задачи может продолжать печататься `node=<...>` ([cli.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/cli.py:1066)).

### 2. `commit_sha_after` у publish node используется как поле под PR URL

`PublishNodeRunner` записывает результат `create_pr(...)` в `complete_node_run(..., commit_sha_after=result_ref)` ([publish.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/nodes/publish.py:51)).  
Тесты это закрепляют как ожидаемое поведение ([test_flow_node_runners.py](/Users/a1234/Documents/GitHub/wastech-orchestrator/tests/core/test_flow_node_runners.py:830)). Формально это скорее naming mismatch audit-поля, чем runtime bug, но поле больше не означает только commit SHA.

## Overall assessment

Срез P1 в целом сделан: live flow engine уже заменил legacy `_drive`, decomposition fan-out работает, `node_runs` / checkpointing / post-node hooks / publish node / recovery wiring присутствуют и реально используются.

Но формулировка “ничего не потеряли при cutover” пока неверна. На данный момент подтверждены следующие незакрытые потери или недореализованные зоны:

- потеря task-level route override;
- нереализованные session semantics из flow contract;
- неполный schema cutover старых state DB;
- неполный decomposition context wiring;
- потеря selected skills на resume;
- десинхрон counters между engine и operator/audit surface;
- неконсистентный publish-failure lifecycle после finalize.

Практически это означает: happy path и большая часть новой engine architecture уже стоят на месте, но P1 нельзя считать полностью закрытым до устранения этих швов и добавления недостающих integration/resume/migration tests.

---

## Verification (2026-06-19)

Метод: повторная проверка каждой находки чтением кода (без сабагентов), пофайловая трассировка. На каждый пункт — вердикт (подтверждено / опровергнуто, с уточнением severity где нужно), решающее доказательство и краткий пошаговый план фикса. Все 7 findings, 6 missing-coverage и 2 secondary **подтверждены** (автор аудита заранее отфильтровал только подтверждённые). Добавленная ценность — уточнения по severity/границам (особенно F3 и F4) и дополнительные расхождения, найденные по пути (в конце).

### Findings — вердикты и планы

#### 1 — ПОДТВЕРЖДЕНО (High). Publish-failure после finalize → file застревает в `done/` при статусе FAILED

Трассировка: `_engine_finalize` ([orchestrator.py:972](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:972)) переносит task-файл в `tasks/done/` и обновляет в БД `source_path`, но **не** трогает `p.task_file` (поле `_Pipeline` ни разу не переприсваивается после конструктора). При падении `push`/`create_pr` сырой `GitCommandError` всплывает мимо `_engine_run` (тот ловит только `NodeManualRequired`/`NodeInfraError`) в `run_task` → `_fail` ([orchestrator.py:358](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:358)). `_fail` зовёт `_finalize_task_artifacts(FAILED)` через stale `p.task_file` (старый `processing/`-путь): `src` уже не существует, `dest=failed/` не существует → `_relocate_task_file` возвращает `None` ([orchestrator.py:1536-1537](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:1536)), и `_go_terminal(already_moved=False)` повторно получает `None`. Итог: файл остаётся в `done/`, статус — FAILED.

План фикса:

1. Держать `p.task_file` синхронным с фактическим расположением: после успешного переноса в `_move_task_file` присваивать `p.task_file = str(dest)`. Тогда `_fail`→`_finalize_task_artifacts(FAILED)` увидит файл в `done/` и перенесёт `done→failed`.
2. Снять идемпотентную блокировку audit-commit на fail-пути (см. Additional #1): либо сбрасывать publish-op `audit_commit` перед повторным `commit_audit` в `_fail`, либо — предпочтительно — **не уводить post-finalize publish-провал в терминальный `_fail`-с-рефинализацией**: трактовать падение push/create_pr ПОСЛЕ finalize как resumable infra-провал (git-операции узла идемпотентны через `publish_operations`), чтобы `resume` до-завершил push/PR без повторных коммитов и без расхождения lifecycle.
3. Тест (MC2): finalize перенёс файл в `done/` → `push`/`create_pr` бросает → задача НЕ остаётся `done`-артефактом при FAILED.

#### 2 — ПОДТВЕРЖДЕНО (High, feature-parity регрессия). `task.agents` теряется на engine path

Доказательство: `AgentRouter.resolve_route(stage, override=...)` полностью поддерживает task-override со swap-on-collision и `RouteSource.TASK_OVERRIDE` ([router.py:133-169](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/routing/router.py:133)); override валидируется на гейте (`check_task_route_override`, [validation_gate.py:224](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/task/validation_gate.py:224)) и хранится в `task.agents` ([task/model.py:83](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/task/model.py:83)). Но единственные два вызова `resolve_route` — [agent.py:68](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/nodes/agent.py:68) и [evaluator.py:45](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/nodes/evaluator.py:45) — передают только `stage`; `RouterPort` даже не экспонирует `override` ([base.py:54-61](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/nodes/base.py:54)); `NodeInputs` не несёт `task.agents` (только `model_for`/`reasoning_for`). Тихая потеря: задача декларирует `agents.review: claude`, гейт принимает, движок исполнит config-route.

План фикса:

1. Добавить `route_override: Mapping[Stage, ProviderId]` в `NodeInputs`, заполнять из `p.task.agents` в `build_node_inputs`.
2. Расширить `RouterPort` необязательным `override`; в `AgentNodeRunner.run`/`EvaluatorNodeRunner.run` звать `resolve_route(stage, self._in.route_override)`.
3. Обновить комментарий `RouterPort` (уже не «without a per-task override»).
4. Тест (MC1): задача с `agents.<stage>` на live-движке резолвится в `RouteSource.TASK_OVERRIDE`.

#### 3 — ПОДТВЕРЖДЕНО (но severity ниже High; в основном P2-deferred). `session_scope` рантаймом игнорируется

Доказательство: `session_scope`/`lineage_affinity` встречаются только в parsing/validator/schema/YAML — и ни разу в node-runner'ах. Request строится через `session_id=self._in.session_ids.get(route.primary.value)` ([agent.py:358](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/nodes/agent.py:358), [evaluator.py:149](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/nodes/evaluator.py:149)) — чисто provider-keyed; обратно пишет только `AgentNodeRunner._update_session` ([agent.py:400-406](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/nodes/agent.py:400)).

Уточнение границ (важно для приоритезации): provider-keyed map — это **legacy parity** ([wiring.py:140](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/wiring.py:140)), а durable lineage (`lineage_affinity`, `resume_own_lineage`) **явно отложен в P2.2** ([p1-engine.md:204](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/flows/p1-engine.md:204)). То есть это не P1-регрессия, а контракт, опережающий рантайм. Реальный P1-разрыв один: `fresh_disposable` (P1.3 заявлял его «через текущий механизм»), но не исполняется — `review` (fresh_disposable) читает `session_ids[provider]` и наследует editing-сессию `implementation`.

План фикса (узко под P1):

1. Зафиксировать границу: `lineage_affinity`/`resume_own_lineage` — это P2.2, в P1 не чинить.
2. Честно исполнить `fresh_disposable`: в построении request учитывать `node.session_scope` — для `fresh_disposable` `session_id=None` и пропускать `_update_session`. Закрывает наследование editing-сессии независимым evaluator'ом.
3. Альтернатива (если даже fresh_disposable откладываем): снять over-promise — задокументировать, что в P1 `session_scope` парсится/валидируется, а рантайм provider-keyed (YAML не должен обещать того, что движок не делает).
4. Тест (MC6): fresh_disposable не наследует editing-сессию.

#### 4 — ПОДТВЕРЖДЕНО (механизм реальный; severity ограничена greenfield). Стамп до v7 без reshape

Доказательство: `_SCHEMA` через `CREATE TABLE IF NOT EXISTS` не переформирует существующие таблицы; `_migrate` аддитивен — добавляет только `tasks.current_node/flow_run_counters/flow_fingerprint` ([state_store.py:54-67](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/state_store.py:54)); `_enforce_schema_version(writable=True)` при `current<7` штампует `user_version=7` без shape-миграции ([state_store.py:83-87](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/state_store.py:83)). `provider_attempts` уже имеет `node_run_id NOT NULL` ([state_store.py:150](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/state_store.py:150)), `record_provider_attempt` пишет в `node_run_id` ([state_store.py:752](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/state_store.py:752)) → на старой БД (`stage_run_id`) первый же insert падает `no column named node_run_id`. Репродукция аудита валидна.

Уточнение (меняет выбор фикса): по явной greenfield-политике (память «no migration» + комментарии state_store.py «локальный state.db пересоздаётся») деструктивные миграции v5/v6/v7 реализовывать **не нужно** — это было бы over-engineering против greenfield. Реальная зона риска — локальные dev-БД времён P0→P1 (v3/v4/…), которые `open()` молча штампует до v7 и роняет. Правильный фикс — fail-closed, а не миграции.

План фикса:

1. Сделать `open()` fail-closed: на writable-пути при `0 < user_version < DB_SCHEMA_VERSION` (был деструктивный bump, `_migrate` лишь аддитивен) — отказать с явным сообщением «удалите state.db / свежий workspace» (симметрично отказу для newer-DB) либо пересоздать БД. (Чисто аддитивные будущие bump'ы остаются мигрируемыми — различать аддитив/деструктив.)
2. Поправить вводящие в заблуждение комментарии [state_store.py:39-46](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/state_store.py:39): «renamed/dropped» описывают форму текущей `_SCHEMA`, а не действия `_migrate`.
3. Тест (MC5): pre-v7 БД со старой формой → `open()` чисто отказывает/пересоздаёт; последующий `record_provider_attempt` не падает.

#### 5 — ПОДТВЕРЖДЕНО (по факту тяжелее «Medium»). `subtask_spec_path` не доводится до edit-узлов

Доказательство: спеки пишутся как `NN-<slug>.md` ([decomposition.py:188](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/decomposition.py:188)); `NodeInputs.subtask_spec_path` есть ([base.py:208](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/nodes/base.py:208)) и подставляется в prompt-vars при decomposition ([agent.py:374-377](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/nodes/agent.py:374)); но `build_node_inputs` по умолчанию `subtask_spec_path=None` ([wiring.py:114](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/wiring.py:114)), `_engine_run` его не вычисляет, `_fan_out_subtasks` не переключает на активный spec ([orchestrator.py:940-961](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:940)); `AgentRunRequest` отдельного поля не имеет ([providers/base.py:82](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/providers/base.py:82)); роли `implementation.md`/`fixing.md` его не упоминают. См. также Additional #4 — роли не ссылаются и на `{subtask_order}`/`{subtask_count}`, поэтому per-subtask контекста в prompt нет вообще.

План фикса:

1. В `_fan_out_subtasks` перед каждым `phase(...)` присваивать `inputs.subtask_spec_path = logs/<task>/subtasks/NN-<slug>.md` активного unit'а; перед post-region phase — сброс в None.
2. Сослаться на `{subtask_spec_path}` (+ `{subtask_order}`/`{subtask_count}`) в packaged `roles/implementation.md` и `roles/fixing.md`. `subtask_spec_path` уже в `ALLOWED_PROMPT_VARS` (test_prompts allowlist) — изменений allowlist не нужно.
3. Отдельное поле в `AgentRunRequest` не требуется — канал через prompt достаточен (KISS).
4. Тест (MC3): в decomposition путь активного спека реально попадает в prompt/request узла implementation подзадачи N.

#### 6 — ПОДТВЕРЖДЕНО (Medium, observability — не корректность). Counters desync

Доказательство: движок инкрементит `FlowRunState.loop_counters` (`global_fix_iterations` + named loops + inline budgets) в `_charge_rework` ([engine.py:349-358](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/engine.py:349)) и чекпоинтит их в `tasks.flow_run_counters` ([recorder.py:43](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/recorder.py:43)). А `p.counters` (legacy `LoopCounters`) на engine-path не мутируется ни разу (нет `LoopController`/`enter_fixing`); `_transition` сохраняет `p.counters` ([orchestrator.py:1708](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:1708)), ledger пишет `p.counters.fix_iterations` ([orchestrator.py:1801](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:1801)), CLI печатает `task.fix_iterations` ([cli.py:1073](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/cli.py:1073)) — всё stale (обычно 0). Bounded-termination корректна (на loop_counters); врёт только operator/audit-витрина.

План фикса:

1. Единый источник правды (предпочтительно, greenfield): `status`/ledger/finalize читают из flow-checkpoint (`tasks.flow_run_counters`→`global_fix_iterations`/loops), legacy-колонки убрать с engine-path. Меньший патч: синкать `run_state.loop_counters[GLOBAL_FIX_KEY]→p.counters.fix_iterations` (+ `test_fix`/`review_fix`) перед `save_counters`/ledger.
2. На resume/rerun гидрировать operator-счётчики из flow-checkpoint, а не из stale legacy.
3. Тест: после N test-fix циклов `status`/ledger показывают N, не 0.

#### 7 — ПОДТВЕРЖДЕНО (Medium; потеря частичная). Selected skills теряются на resume past planning

Доказательство: `_engine_apply_skills` ставит `p.selected_skills`/`inputs.skill_paths` только когда planning исполняется в этом процессе ([orchestrator.py:1091-1092](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:1091)); `_Pipeline.selected_skills` сам документирует «empty on a resume past planning» ([orchestrator.py:262-266](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:262)); `_resume_task` пересобирает `skill_inventory`, но не `selected_skills` ([orchestrator.py:723](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:723)); `_restore_engine_inputs` восстанавливает diff/plan/review, но не skills ([orchestrator.py:754-768](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:754)); `build_node_inputs` берёт `skill_paths` из `p.selected_skills` ([wiring.py:131](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/wiring.py:131)). Уточнение (Additional #5): `_engine_apply_skills` дописывает skill-секцию в `plan.md` ([orchestrator.py:1093-1096](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:1093)), а `plan.md` на resume восстанавливается — то есть ссылки на skills выживают в тексте плана; теряется именно отдельный канал `{skills_path}`/`skill_reference_paths`.

План фикса:

1. Персистить выбор: в `_engine_apply_skills` писать `logs/<task>/selected_skills.json` (пути refs).
2. На resume читать его обратно в `p.selected_skills`/`inputs.skill_paths` (в `_restore_engine_inputs`/`_resume_task`), симметрично plan/diff/review.
3. Обновить doc-комментарий `_Pipeline.selected_skills`.
4. Тест (MC4): прерывание после planning → resume → request узла implementation несёт `skill_reference_paths`.

### Missing Coverage — вердикты

Все шесть **подтверждены**:

1. ПОДТВЕРЖДЕНО — `TASK_OVERRIDE` проверяется только в router-unit ([test_route_resolution.py:44](/Users/a1234/Documents/GitHub/wastech-orchestrator/tests/routing/test_route_resolution.py:44)); `agents=` в engine-тестах — это `AgentsConfig` для `drive_flow`, не task-override. Engine/orchestrator-теста нет.
2. ПОДТВЕРЖДЕНО — `test_failed_with_branch...` ([test_orchestrator.py:856](/Users/a1234/Documents/GitHub/wastech-orchestrator/tests/core/test_orchestrator.py:856)) покрывает другой сценарий (fail финализируется как success). Сценария «finalize→done, затем push/PR падает, проверяем failed/» нет.
3. ПОДТВЕРЖДЕНО — `subtask_spec_path` встречается в тестах только в allowlist ([test_prompts.py:38](/Users/a1234/Documents/GitHub/wastech-orchestrator/tests/core/test_prompts.py:38)); e2e нет.
4. ПОДТВЕРЖДЕНО — есть только fresh-path `test_planning_selected_skills_reach_downstream_stages` ([test_orchestrator.py:1815](/Users/a1234/Documents/GitHub/wastech-orchestrator/tests/core/test_orchestrator.py:1815)); resume-варианта нет.
5. ПОДТВЕРЖДЕНО — `test_db_schema_version.py` — только version-gate (stamp/adopt-0/refuse-newer); shape-migration старой БД нет.
6. ПОДТВЕРЖДЕНО — `session_scope` в тестах используется как значение поля при parsing/wiring; рантайм-теста изоляции (fresh не наследует / fixing продолжает lineage / resume_own_lineage) нет — и не может пройти, пока F3 не починен.

### Secondary observations — вердикты

1. ПОДТВЕРЖДЕНО (минор). `_go_terminal` не обнуляет `current_node`/`flow_run_counters`/`flow_fingerprint`; единственное обнуление — rerun-fresh reset ([state_store.py:546](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/state_store.py:546), который вдобавок чистит `node_runs`/`subtasks`/`publish_operations`). Комментарий «`done` clears it» ([orchestrator.py:1481-1482](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/orchestrator.py:1481)) неверен. CLI `status` читает checkpoint для всех задач ([cli.py:1050](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/cli.py:1050)) и печатает `node=` в т.ч. для DONE ([cli.py:1066](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/cli.py:1066)). Фикс: на терминале обнулять checkpoint-колонки (`save_flow_checkpoint(current_node=None,...)`) либо в CLI не печатать `node=` для терминальных статусов; и поправить комментарий.

2. ПОДТВЕРЖДЕНО (минор, naming). `PublishNodeRunner` пишет PR URL в `complete_node_run(..., commit_sha_after=result_ref)` ([publish.py:51-58,85](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/nodes/publish.py:51)); тест это фиксирует ([test_flow_node_runners.py:830](/Users/a1234/Documents/GitHub/wastech-orchestrator/tests/core/test_flow_node_runners.py:830)). Поле больше не означает только commit SHA. Фикс: добавить/переименовать поле `result_ref`/`pr_url` в `NodeRunRow` либо задокументировать перегрузку.

### Additional discrepancies (найдено по пути)

1. **Углубление F1: commit_audit идёт ДО push и идемпотентен.** В `_publish` порядок `commit_code → commit_audit → push → create_pr` ([publish.py:82-85](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/nodes/publish.py:82)); `commit_audit` дедуплицируется по `publish_operations` ([git_manager.py:525-527](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/git_manager.py:525)) и коммитит `tasks/done/<id>.md` ([git_manager.py:539-549](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/git_manager.py:539)). Значит к моменту падения push done-коммит уже в ветке, и `_fail` не сможет перекоммитить под `failed/` даже после исправления переноса файла. Расхождение git-истории — не «риск», а уже свершившийся факт; голого fix'а переноса недостаточно (см. F1 шаг 2).
2. **Нюанс F3: граница P1/P2.** provider-keyed session map — legacy parity; `lineage_affinity`/`resume_own_lineage` корректно отложены в P2.2. Реальный P1-разрыв — только неисполнение `fresh_disposable`. Аудит оценил весь пункт как High без этого различия.
3. **Нюанс F4: фикс — fail-closed, а не миграции.** Под greenfield-политикой реализовывать деструктивные v5/v6/v7-миграции не следует; комментарии state_store.py:39-46 вводят в заблуждение (описывают форму `_SCHEMA`, читаются как действия `_migrate`).
4. **Углубление F5: в decomposition prompt нет НИКАКОГО per-subtask контекста.** Роли `implementation.md`/`fixing.md` не ссылаются ни на `{subtask_spec_path}`, ни на `{subtask_order}`/`{subtask_count}`, хотя `agent.py:374-377` их заполняет — фан-аут гоняет sub_flow N раз с идентичным prompt. Это усиливает F5 (не «недовод контекста», а его полное отсутствие в prompt).
5. **Нюанс F7: потеря частичная.** skill-секция остаётся в восстанавливаемом `plan.md`; теряется отдельный канал `{skills_path}`/`skill_reference_paths`.
