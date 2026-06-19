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
