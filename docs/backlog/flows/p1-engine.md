# P1 — Движок исполнения + core-capability узлы (внутренний чекпоинт паритета)

Статус: **backlog / инженерная спека (не запланировано к исполнению)** Дата: 2026-06-17 Владелец: Vladimir Makarevich

Детализация фазы P1 из [plan.md](plan.md). Цель фазы: движок исполняет граф из снапшота (P0), существующие капабилити ядра обёрнуты как узлы **без смены механики**, и движок **воспроизводит текущее поведение implementation байт-в-байт на границе** (golden-harness против существующих тестов). На вход берётся валидированный `FlowSnapshot` ([snapshot.py](../../../src/wastech_orchestrator/core/flow/snapshot.py) + [validator.py](../../../src/wastech_orchestrator/core/flow/validator.py)); на выход — единая модель исполнения, вытеснившая хардкодный `_drive`.

Опорные контракты: палитра и рёбра — [flow-contract.md](flow-contract.md) §2–§5; resume — [index.md](index.md) §6; инварианты ядра — [index.md](index.md) §9. Сквозная витрина — [happy-path.md](happy-path.md).

## Принцип проверки фазы (golden-harness)

P1 не вводит новых фич. Единственный критерий — **существующие тесты implementation зелены через движок** (P1.4). Текущий конвейер (`refinement → planning → implementation → testing → review → fixing → summary → publish`, fix-петли, decomposition) выражается как `implementation.yaml`-фикстура **без** supervisor / durable sessions / hybrid testing (это P2). Паритетный flow из P1.4 — тестовая фикстура, не поставляемый flow.

Это означает явный **двойной прогон в CI на время P1**: старый драйвер (`_drive`) и движок исполняют один и тот же набор сценариев; их наблюдаемые границы (статусы, артефакты, commit-SHA, publish-операции, ledger-записи) сравниваются. Двойной прогон снимается в P1.5 вместе со старым драйвером.

## Что уже сделано до P1 (вход фазы)

- **P0.1–P0.4** — словарь, схема+снапшот, фатальный валидатор, реестр (см. [plan.md](plan.md)).
- **P0.5 hardening** (закрыто в этой же смене): fail-closed на неизвестных полях во всех мэппингах ([snapshot.py](../../../src/wastech_orchestrator/core/flow/snapshot.py) `_reject_unknown`), валидация `checker` ∈ ядрового набора и `network_policy` ∈ `NetworkPolicy`, обёртка ошибок enum в `FlowLoadError` (`_enum`), namespace-проверка `when.fact` (`derived.`/`config.`), co-reachability терминала в валидаторе. Тесты — `tests/core/test_flow_snapshot.py` (секция «fail-closed hardening (P0.5)»), `tests/core/test_flow_validator.py::test_node_cannot_reach_terminal`.

Остаётся как **deferred** (см. [P4.2](p4-operator.md)): полная согласованность с `config.yaml` в валидаторе ([security-ceiling.md](security-ceiling.md) §4 «Согласованность с config.yaml») — `validate_flow` пока не принимает конфиг; реализуется, когда появится шринкнутая схема конфига (зависимость P1.2/P0.4-config-shrink).

---

## P1.1 — Ядро движка ✓ Выполнено

Статус (2026-06-17): реализовано в [`core/flow/engine.py`](../../../src/wastech_orchestrator/core/flow/engine.py) (`FlowEngine`, `NodeOutcome`/`NodeResult`/`NodeRunner`/`NodeContext`/`RunRecorder`/`Finding`/`FlowRunResult`) + [`core/flow/run_state.py`](../../../src/wastech_orchestrator/core/flow/run_state.py) (`FlowRunState`). Тесты — `tests/core/test_flow_engine.py` (10). **Отступление от буквы спеки (зафиксировано):** движок **не** переиспользует `LoopController` (тот несёт implementation-специфичные имена `test_fix`/`review_fix` — это нарушило бы тест абстракции P3 «ноль доменного знания в движке»). Вместо этого движок воспроизводит `>=`-семантику `LoopController` обобщённо над `FlowRunState.loop_counters`: именованные циклы — increment-then-check `>=` (паритет закрепляется golden-harness в P1.4), инлайн `budget: N` — check-then-increment (`budget: 1` = 1 rework, как у supervisor в packaged `implementation.yaml`); глобальный счётчик под ключом `global_fix_iterations`; эффективный cap = `min(flow_budget, config_cap)`. Старый `LoopController` остаётся для legacy `_drive` до P1.5.

Цель: исполнитель узлов с **engine-owned** применением переходов по рёбрам. Узлы возвращают исход (`NodeOutcome`), движок резолвит соответствующее ребро из `adjacency` снапшота и переходит. Узлы не прыгают по графу. Bounded-loop бюджеты + единый `fix_iterations` + гарантия терминальности.

### Touchpoints

- **Новый** [`core/flow/engine.py`](../../../src/wastech_orchestrator/core/flow/engine.py) — `class FlowEngine`.
- **Новый** `core/flow/run_state.py` — рантайм-чекпоинт (см. P1.2).
- **Новый** пакет `core/flow/nodes/` — диспетчер видов узлов (наполняется в P1.3).
- Вытесняет: [`core/orchestrator.py`](../../../src/wastech_orchestrator/core/orchestrator.py) `_run_unit` (≈1193, infinite-while dispatch-on-`Status`), `_enter_fixing` (≈1465), `_after_edit_target`, `_run_units_and_finish` (≈1030). Удаляются в P1.5.
- Переиспользует словарь: [`core/flow/contracts.py`](../../../src/wastech_orchestrator/core/flow/contracts.py) `QualityAction` → `quality_action_effect` → `LifecycleEffect`.

### Новые типы

```python
# core/flow/engine.py
@dataclass(frozen=True, slots=True)
class NodeOutcome:
    """Что узел вернул движку. Никогда не указывает следующий узел напрямую."""
    kind: Literal["accept", "rework", "pass", "fail", "done"] | str  # "route:<label>" допускается
    findings: tuple[Finding, ...] = ()        # для evaluator-узлов (P2)
    structured_output: Mapping[str, object] | None = None

@dataclass(frozen=True, slots=True)
class NodeResult:
    """Результат исполнения одного узла: исход + артефакты + сайд-эффекты, записанные ядром."""
    node_id: str
    outcome: NodeOutcome
    node_run_id: int                          # строка node_runs (P1.2)

class NodeRunner(Protocol):
    """Реализуется каждым видом узла в core/flow/nodes/*.py (P1.3)."""
    def run(self, node: FlowNode, ctx: NodeContext) -> NodeResult: ...
```

`FlowEngine` держит: `snapshot: FlowSnapshot`, `run_state: FlowRunState` (P1.2), реестр `dict[str, NodeRunner]` по `kind`, и `LoopController` ([`loop_control.py`](../../../src/wastech_orchestrator/core/loop_control.py)).

### Поведение

- **Цикл движка** заменяет infinite-while из `_run_unit`. Алгоритм: `current = entry_node`; пока узел не терминальный — резолвить `when` (детерминированный skip → запись `record_skip`, переход по единственному исходящему ребру), исполнить через `NodeRunner.run`, получить `NodeOutcome`, **выбрать ребро**: из `snapshot.adjacency[current]` найти ребро, чей `outcome` равен исходу узла (для безусловных — единственное ребро с `outcome=None`). Несоответствие исхода объявленным рёбрам — `InternalError` (валидатор P0.3 это уже исключил для well-formed flow; рантайм — assert).
- **Применение перехода — только движок.** `QualityAction` маппинг ([contracts.py](../../../src/wastech_orchestrator/core/flow/contracts.py) `QUALITY_ACTION_EFFECT`): `continue→accept/pass-ребро`, `enter_fixing/repeat_stage→rework-ребро`, `stop_manual→manual_action_required`, `fail→failed`. Движок — единственный, кто меняет статус задачи.
- **Бюджеты циклов.** Любое прохождение `rework`/`fail`-ребра инкрементит **единый** глобальный `fix_iterations` ровно один раз (через `LoopController.enter_fixing`, который уже делает один инкремент `fix_iterations` + один инкремент loop-специфичного счётчика). Per-edge `budget: N` и `loop: <name>` маппятся на существующие счётчики: `loop: test_fix → test_fix_cycles`, `loop: review_fix → review_fix_cycles`, инлайн `budget: N` → локальный per-edge счётчик в `run_state.loop_counters` с синтетическим ключом `f"{from}->{to}:{outcome}"` (см. §Решения). Исчерпание любого лимита → детерминированный `manual_action_required` + `failure_report.json`/`stuck.md` (переиспользуется `_write_failure_report`, ≈2403).
- **Терминальность гарантирована**: бесконечный цикл невозможен (валидатор требует budget/loop на каждом rework/fail-ребре; глобальный `fix_iterations`-cap — страховка). Это инвариант движка, не flow.
- **Single-slot, гейт валидации, branch-prep** остаются в `run_task`/`_drive`-обвязке (ядро); движок вызывается **после** prepare_branch и резолюции снапшота, заменяя только тело unit-петли.

### Соответствие текущему коду

- `_run_unit` dispatch-on-`Status` (IMPLEMENTING/TESTING/REVIEWING/FIXING) → обобщённый цикл по узлам графа. Маппинг статус→узел фиксируется паритетным flow (P1.4), не движком.
- `_enter_fixing` логика (инкремент, проверка cap, write_failure_report или write_fixing_context) → движок зовёт `LoopController.enter_fixing` на rework/fail-ребре; ветка stuck/continue идентична.
- `_after_edit_target` (TESTING vs REVIEWING после edit) → выражается рёбрами flow (`fixing → testing` или `fixing → review`), не кодом.

### Тесты

- `test_engine_follows_declared_edge` — исход узла резолвится в объявленное ребро; targets совпадают со снапшотом.
- `test_engine_outcome_not_in_edges_is_internal_error` — рантайм-assert (защита, валидатор уже ловит на загрузке).
- `test_engine_budget_exhaustion_goes_manual` — исчерпание `budget`/`loop`/`fix_iterations` → `manual_action_required` + failure-report записан.
- `test_engine_applies_transitions_not_nodes` — `NodeRunner` не может сменить статус; статус меняет только движок.
- `test_engine_when_skip_takes_single_edge` — `when=false` пропускает узел, идёт по единственному исходящему ребру, пишет `record_skip`.
- `test_engine_single_fix_iterations_increment` — одно прохождение rework-ребра = один инкремент `fix_iterations` (без двойного счёта).

### Exit

Движок гоняет граф из снапшота: исход ∈ объявленных рёбер; переходы применяет ядро; исчерпание бюджета → `manual_action_required`. Узлы-заглушки (реальные обёртки — P1.3).

### Решения (зафиксировано 2026-06-17)

- **Бюджеты flow ≤ config-страховка.** flow-`budgets` (`global_fix_iterations`, `test_fix`, `review_fix`) — значения, которыми движок параметризует `LoopController`; config `agents.max_fix_cycles`/`max_total_fix_iterations` — **верхняя неослабляемая страховка**. flow **не может** задать бюджет выше config-cap — это проверка потолка, добавляется в config-aware валидатор ([P4.2](p4-operator.md)). Согласуется с принципом потолка ([security-ceiling.md](security-ceiling.md)): оператор гибко настраивает петли per-flow, но админ-конфиг остаётся пределом.
- **Инлайн `budget: N` vs именованный `loop`: гибрид.** `run_state.loop_counters: dict[str, int]` (P1.2), ключ = имя loop (`test_fix`) **или** синтетический ключ ребра `(from,to,outcome)` для инлайн-бюджетов. Сохраняет оба смысла без правки packaged YAML (supervisor использует инлайн `budget: 1`, петли — именованные `loop`).

---

## P1.2 — Обобщение resume / checkpoint ◑ Слой персистенции выполнен (resume/recovery-тесты — в P1.4)

Статус (2026-06-17): реализовано **аддитивно** — `node_runs` и родовой `RUNNING` добавлены **рядом** с legacy `stage_runs`/гранулярными статусами, которые остаются до P1.5 (golden-harness P1.4 гоняет обе модели бок о бок, поэтому legacy `_drive` не должен сломаться раньше). Сделано: `state.db` **v4** ([state_store.py](../../../src/wastech_orchestrator/state_store.py)) — таблица `node_runs` + `NodeRunRow` + `record_node_run`/`complete_node_run`/`record_node_skip`/`get_node_runs` + колонки `tasks.current_node`/`flow_run_counters`/`flow_fingerprint` + `save_flow_checkpoint`/`get_flow_checkpoint` (всё через guarded `_migrate`); [`core/flow/recorder.py`](../../../src/wastech_orchestrator/core/flow/recorder.py) — `StateStoreRunRecorder` (реализует `RunRecorder`) + `hydrate_run_state` (resume доверяет сохранённому `flow_fingerprint`, не переразрешает flow); родовой `Status.RUNNING` ([state_machine.py](../../../src/wastech_orchestrator/core/state_machine.py), аддитивно); flow-нейтральный `ledger.write_failure_report` (опциональный `node_id`); движок резюмирует с гидратированного `current_node`. Тесты — `tests/state/test_node_runs.py`, `tests/core/test_flow_recorder.py`, `test_flow_engine.py::test_engine_resumes_at_current_node`. **Отложено в P1.4** (сцеплено с вплетением движка в `run_task`/`resume`): переписывание `RecoveryReconciler` на per-node-progress, decomposed-resume и `rerun --continue` по `interrupted_node`. Удаление гранулярных статусов/`stage_runs`/dispatch-on-status — в P1.5.

Цель: чекпоинт = `completed_nodes + current_node + loop_counters + publish_operations`; lifecycle ужат до родового `pending → validated → running → (done | failed | manual)`; recovery доверяет снапшоту и **не переразрешает** flow. **Высокий риск** (см. [plan.md](plan.md) §Риски): опора на существующую идемпотентность `publish_operations`.

### Touchpoints

- [`state_store.py`](../../../src/wastech_orchestrator/state_store.py): `DB_SCHEMA_VERSION` bump `3 → 4`; обобщить таблицу `stage_runs` → `node_runs` (см. ниже); **сохранить без изменений** `publish_operations`, `artifacts`, `provider_attempts`, `check_runs`, `subtasks`. Новые методы `record_node_run`/`complete_node_run`/`get_node_runs` рядом с существующими `record_stage_run` (≈588)/`complete_stage_run` (≈652)/`record_skip` (≈620).
- **Новый** `core/flow/run_state.py` — `FlowRunState` (рантайм-чекпоинт, гидратируется из `node_runs` + `tasks` + `publish_operations`).
- [`core/recovery.py`](../../../src/wastech_orchestrator/core/recovery.py): `RecoveryReconciler.reconcile` (≈57) и `reconcile_decomposed` (≈76) обобщаются с per-`Status` логики на per-node-progress; `RecoveryAction` (NONE/RESUME/CLEANUP/MANUAL) и `RecoveryPlan` сохраняются.
- [`core/state_machine.py`](../../../src/wastech_orchestrator/core/state_machine.py): `Status` сжимается до родового lifecycle (детали миграции — ниже); `ALLOWED_TRANSITIONS` упрощается.

### Изменения схемы (v4)

`stage_runs` → `node_runs` (обобщение, не новая таблица): переименовать `stage TEXT` → `node_id TEXT` + добавить `node_kind TEXT`; сохранить `subtask_order`, `route_*`, `provider_used`, `error_class`, `stage_attempts`, `commit_sha_before/after`, `started_at`/`finished_at`, `skipped`/`skip_reason`. Greenfield (миграции данных нет — локальный `state.db` пересоздаётся), но `CREATE TABLE`-определение и все методы обновляются.

```python
# state_store.py
@dataclass(frozen=True)
class NodeRunRow:                 # обобщает StageRunRow
    task_id: str
    node_id: str
    node_kind: str                # agent | evaluator | checks | hitl | publish
    subtask_order: int | None
    route_primary: str | None     # None для не-agent узлов
    route_fallback: str | None
    route_source: str | None
    ...                           # остальные поля как в StageRunRow

# core/flow/run_state.py
@dataclass
class FlowRunState:
    flow_fingerprint: str
    completed_nodes: list[str]
    current_node: str | None
    loop_counters: dict[str, int]   # именованные loop'ы + инлайн per-edge бюджеты (P1.1 OQ)
    # publish_operations читаются из state_store, не дублируются здесь
```

### Lifecycle (state_machine)

Родовой: `PENDING → VALIDATED → RUNNING → (DONE | FAILED | MANUAL_ACTION_REQUIRED)`. Прогресс внутри `RUNNING` — это `current_node` в `node_runs`, **не** отдельный `Status`. Удаляются implementation-специфичные статусы (`REFINING`/`PLANNING`/`IMPLEMENTING`/`TESTING`/`REVIEWING`/`FIXING`/`SUMMARIZING`/`READY_TO_PUBLISH`/`COMMITTING`/`PUSHING`/`CREATING_PR`). `is_terminal`/`is_active` сохраняются. `interrupted_status` (v3, для `rerun --continue`) → `interrupted_node` (id узла).

**Риск миграции**: `_resume_task` (≈734) сейчас делает dispatch-on-status с явными ветками на каждый статус; recovery `revive_task_for_continue` (state_store ≈533) хранит `interrupted_status`. Всё это переключается на `current_node`. Из-за greenfield миграции данных нет, но код recovery/rerun/continue переписывается целиком — самый тонкий участок фазы.

### Поведение

- **Чекпоинт пишется** после каждого узла (`complete_node_run`) и при каждом изменении `loop_counters`/перехода (как сейчас `save_counters`, state_store ≈574). `current_node` обновляется атомарно с `node_runs`.
- **Recovery доверяет снапшоту**: пересчитывает `flow_fingerprint` сохранённого снапшота (целостность), проверяет существование видов узлов, и что текущие security-возможности не требуют расширить сохранённый потолок ([security-ceiling.md](security-ceiling.md) §7). **Не** резолвит flow из живого конфига заново.
- **Decomposed-реконсиляция** (`reconcile_decomposed`, ≈76) сохраняет инвариант «подзадача done ⟺ commit_sha в БД И коммит на ветке»; resume-point = первая подзадача без верифицированного коммита. Обобщается с per-`Status` на «текущий узел под-flow + completed_nodes».
- **Дедуп побочных эффектов** — без изменений: `publish_operations` (фингерпринт + проверка remote) переиспользуется как механика узла `publish` (P1.3). Это то, чего нет у crewAI (см. [index.md](index.md) приложение).

### Тесты

- `test_resume_restarts_at_current_node` — рестарт продолжает с `current_node`; `completed_nodes` не переисполняются.
- `test_resume_dedups_publish_ops` — повторный commit/push/PR идемпотентен (фингерпринт совпал → no-op).
- `test_resume_arbitrary_graph_idempotent` — resume по графу с циклом (rework-ребро) не дублирует сайд-эффекты.
- `test_recovery_does_not_rereresolve_flow` — recovery не читает живой конфиг; использует сохранённый снапшот/фингерпринт.
- `test_recovery_decomposed_resume_point` — паритет с текущим `reconcile_decomposed` (по commit-SHA на ветке).
- `test_continue_revives_at_interrupted_node` — `rerun --continue` оживляет на `interrupted_node`, сохраняя ветку/счётчики/subtasks/publish-ops.
- `test_schema_v4_node_runs_roundtrip` — запись/чтение `node_runs`.

### Exit

Resume по произвольному графу идемпотентен; lifecycle родовой; recovery доверяет снапшоту. Существующие recovery-тесты (адаптированные на `node_runs`) зелены.

### Решения (зафиксировано 2026-06-17)

- **Форма `loop_counters` в чекпоинте**: `dict[str, int]`; ключ = имя loop'а (для именованных `loop:`) ИЛИ синтетический ключ `f"{from}->{to}:{outcome}"` (для инлайн `budget:`). Эта схема ключей персистится в `node_runs`/`FlowRunState` — фиксированная часть схемы v4.
- **`failure_report.json`/`stuck.md` — flow-нейтральный формат + опциональные секции**: базовые поля `{node_id, loop, counters, last_outcome}` всегда; implementation-специфичные секции (`last_check_log`, `last_review_findings`) пишутся **только** если соответствующие узлы исполнялись. Не падает на research/audit; implementation-детали сохраняются когда применимы. Реализуется при обобщении ledger'а в P1.2, первый не-implementation потребитель — P3.

---

## P1.3 — Обёртки core-owned узлов (без смены механики) ✓ Ядро выполнено (prompt_audit/heartbeat — в P1.4)

Статус (2026-06-17): реализован агентно-детерминированный костяк P1.3 — сборка промпта из `role_file` и три обёртки (agent/evaluator/checks), все тонкие адаптеры над инъектированными коллабораторами, юнит-тесты на фейках. Сделано: [`core/flow/prompt.py`](../../../src/wastech_orchestrator/core/flow/prompt.py) (`render_role_prompt`/`read_role_file` — `role_file` как источник шаблона, path-containment, `render_prompt` неизменён; в `ALLOWED_PROMPT_VARS` добавлен `repo` — единый allowlist); [`core/flow/nodes/`](../../../src/wastech_orchestrator/core/flow/nodes/) — `base.py` (`NodeServices`/`NodeInputs` контракты + `NodeInfraError`, коллабораторы как `Protocol`-порты), `agent.py` (`AgentNodeRunner` → router, строит `AgentRunRequest` из узла+inputs, infra-exhaustion → `NodeInfraError`), `evaluator.py` (`EvaluatorNodeRunner` `role=review` → accept/rework/done, паритет `_is_blocking`), `checks.py` (`ChecksNodeRunner` → `CheckRunner`, pass/fail, launch_failed → `CheckLaunchError`). Обёртки конструируются **на юнит** с `NodeServices`/`NodeInputs`, поэтому generic-движок (P1.1) не тронут. Тесты — `tests/core/test_flow_prompt.py`, `tests/core/test_flow_node_runners.py`. Сделано (P1.4 Step A, wiring-first):

- `publish.py` (`PublishNodeRunner` — `pull_request`/`documentation_pull_request` → idempotent git-последовательность `commit_code`+`commit_audit`+`push`+`create_pr` через `GitPublishPort`; `none`/`local_artifact`/private-report → без git, P3; finalize+auto_merge остаются на уровне обёртки P1.4).
- [`engine_driver.py`](../../../src/wastech_orchestrator/core/flow/engine_driver.py) (`build_node_runners` — per-kind реестр обёрток из `NodeServices`/`NodeInputs`; `drive_flow` — сборка `FlowEngine`+реестр+recorder и прогон одного юнита до `FlowRunResult`). Это шов «движок ↔ оркестратор»: обёртка `run_task` (Step B) строит `NodeServices`/`NodeInputs` из `_Pipeline`, резолвит снапшот через `FlowRegistry` и зовёт `drive_flow`.
- Запись review-артефакта + `{review_path}` в `EvaluatorNodeRunner` (паритет `_write_review`: `review/findings.json`+`summary.md`, `inputs.review_path`).
- Сквозной тест `tests/core/test_flow_engine_driver.py` (tiny-flow `refine→impl→testing→publish` через `drive_flow` с фейк-коллабораторами + реальный `StateStoreRunRecorder`/`StateStore` → `DONE`, `node_runs` записаны).

Сделано в Step B на текущий момент:

- **Dangerous-diff guard** в `AgentNodeRunner`: после `workspace-write`-узла ядро пишет diff (`{diff_path}`) и прогоняет `classify_dangerous_diff` (deletion/dependency) — core-owned, flow не отключает. Опасный diff требует durable human-approval через `HumanGate` (ключ `guardrail_interaction_path(cycle=fix_iterations)`); planning-преодобрение засчитывается; **reconsider-on-denial** (перезапуск с denial-контекстом → переклассификация → всё ещё опасно → `NodeManualRequired`) — порт `_run_edit_stage_with_guardrail`/`_resume_guardrail_answer`. `GitPublishPort`→`GitPort` (+`write_current_diff`/`changed_code_entries`). Тесты — approve→proceed, deny→reconsider-clean→proceed, deny→still-dangerous→manual.
- **Паритетная фикстура** `tests/core/flows/implementation_parity.yaml` (+`roles/*.md`): `refinement(when needs_refinement)→planning→implementation→testing(checks)→review(evaluator)→fixing→summary(agent, when summary_enabled)→publish`, петли `test_fix`/`review_fix`, `fixing→testing` безусловно, decomposition `[implementation,testing,review,fixing]`; **без** supervisor/testing_quality. Бюджеты крупные (cap = config). Тест `tests/core/test_implementation_parity_flow.py` (load+validate+форма).

Сделано: **встроенный HITL refinement/planning** в `AgentNodeRunner` — `HumanGate` ([human_gate.py](../../../src/wastech_orchestrator/core/flow/nodes/human_gate.py)) делает один durable round-trip (`start_ask`→persist `waiting`→`wait_for_answer`→`write_answer`), агент-обёртка парсит `parse_typed_stage_output`, на сигнал делает round-trip и перезапускает стадию с `human_input_path`, резюмирует persisted-interaction после рестарта; fail-closed (timeout/transport/invalid) → `NodeManualRequired`. Порт `NotifierPort` + `NodeServices.notifier`/`ask_timeout_s` + `NodeInputs.contacts`. Тесты — `tests/core/test_flow_node_runners.py` (секция embedded HITL: no-signal, question round-trip, timeout→manual).

**Остаётся в Step B:** standalone `hitl`-узел (низкий приоритет — `HitlNode` без текста вопроса); prompt_audit/heartbeat-наблюдаемость; **главный кусок** — реальная конструкция `NodeServices`/`NodeInputs` из `_Pipeline` + карта `node_id→Stage` + воспроизведение per-stage пост-обработки (refinement enriched spec; planning plan-write + decomposition + skills + subtask-specs) через flow для паритета артефактов; дремлющий `_drive_via_engine`-шов в `run_task`; golden-harness dual-run (`_drive` ↔ `FlowEngine` на fake-CLI сценариях); recovery-диспетчеризация.

Цель: каждый вид узла — тонкий адаптер к существующему ядру; вызов через узел даёт **тот же результат**, что прямой вызов. Observability `agent`-узла сохраняется как сейчас.

### Touchpoints (узел → существующий код)

- **Новый** `core/flow/nodes/agent.py` → [`routing/router.py`](../../../src/wastech_orchestrator/routing/router.py) `AgentRouter.resolve_route`/`run_stage` (≈133/≈171); строит `AgentRunRequest` ([providers/base.py](../../../src/wastech_orchestrator/providers/base.py)) из полей узла (`role_file`→prompt, `model`/`reasoning`/`permission_profile`/`timeout_seconds`/`extra_args`/`output_schema`/`session_scope`). dangerous-diff guard ([core/dangerous_diff.py](../../../src/wastech_orchestrator/core/dangerous_diff.py) `classify_dangerous_diff`) **автоматически** после `workspace-write`-узла — core-owned, flow не отключает.
- **Новый** `core/flow/nodes/checks.py` → [`check_runner.py`](../../../src/wastech_orchestrator/check_runner.py) `CheckRunner.run`; discovery + `approve_command_changes`-гейт + always-on mutation guard; exit-коды авторитетны (`CheckOutcome.passed`). `launch_failed` → инфра-путь (не quality-fail), как `_reresolve_on_launch_failure` сейчас.
- **Новый** `core/flow/nodes/hitl.py` → Telegram durable-транспорт (как `_run_typed_stage`, ≈1789, делает HITL round-trip).
- **Новый** `core/flow/nodes/publish.py` → [`git_manager.py`](../../../src/wastech_orchestrator/git_manager.py) `commit_code`/`commit_subtask`/`commit_audit`/`push`/`create_pr`/`merge_pr`; идемпотентность через `publish_operations` (фингерпринт), без изменений.
- **Новый** `core/flow/nodes/evaluator.py` — в P1.3 минимальная обёртка под `role=review` (паритет текущего review-стейджа); полный evaluator-примитив (supervisor/critic/verifier/test_quality, immutable-вердикты) — P2.1/P2.3.
- Observability сохраняется: prompt_audit (рендеренный промпт + метаданные per-run в `logs/<task>/prompt-audit/`, global+per-task tri-state, task wins, без гейта), structured logging (logfmt/json + redaction-filter), heartbeat — всё как в текущем `_run_stage` (≈1716).

### Поведение

- `agent`-узел маппит поля узла на `AgentRunRequest`; `session_scope` в P1.3 — только `editing_lineage`/`fresh_disposable` через текущий механизм (durable lineage-стор — P2.2; `--resume` Claude уже есть в [providers/claude.py](../../../src/wastech_orchestrator/providers/claude.py) `build_claude_argv`; Codex resume — P2.2). В P1.3 паритет означает in-memory сессию как сейчас.
- `checks`-узел: `checker: command_profile` — единственный нужный для паритета (`citation`/`dependency_scan` — P3.1).
- `evaluator`-узел `role=review`: блокирующие findings → исход `rework`; чистый → `accept`. Immutable-стор вердиктов — P2.

### Тесты

- `test_agent_node_equals_direct_router_call` — узел даёт идентичный `StageOutcome` прямому `router.run_stage`.
- `test_agent_node_writes_prompt_audit_when_enabled` — при `prompt_audit:true` каждый agent-узел пишет запись аудита.
- `test_checks_node_exit_code_authoritative` — exit≠0 → исход `fail`; launch_failed → инфра-путь, не fail.
- `test_publish_node_idempotent` — повторный вызов publish-узла → no-op через `publish_operations`.
- `test_dangerous_diff_runs_after_workspace_write_node` — guard срабатывает автоматически; flow его не объявляет.
- `test_review_evaluator_node_blocking_to_rework` — блокирующий review → `rework`-ребро.

### Exit

Капабилити вызываются через узлы; каждый узел = тот же результат, что прямой вызов. prompt_audit/logging/heartbeat сохранены.

### Сборка промпта agent-узла (зафиксировано 2026-06-17)

**Решение: prompt-машинерия остаётся ядром; меняется только источник шаблона.** Сегодня цепочка: `Stage → PromptTemplateStore.resolved(stage) → render_prompt(template, vars) → AgentRunRequest.prompt` ([core/prompts.py](../../../src/wastech_orchestrator/core/prompts.py), [orchestrator.py `_build_prompt`](../../../src/wastech_orchestrator/core/orchestrator.py) ≈2150). В flow-модели `Stage`-индексация (`PromptTemplateStore`, packaged `templates/prompts/<stage>.md`, `prompts.mode`) **уходит** вместе со `Stage` (P1.5); шаблон = содержимое `role_file` узла.

Что **остаётся фиксированным ядром** (security-критично, flow не ослабляет):

- `render_prompt(template, vars)` ([prompts.py](../../../src/wastech_orchestrator/core/prompts.py) ≈57) — «безопасный рендерер»: подставляет только токены из allowlist; неизвестные `{...}` (фигурные скобки кода/JSON) проходят насквозь без `KeyError`.
- `ALLOWED_PROMPT_VARS` ([prompts.py](../../../src/wastech_orchestrator/core/prompts.py) ≈37) — **только пути и метаданные**, никогда тело задачи/diff/логи/env/секреты (они в артефакт-файлах, на которые агент ссылается по пути, [flow-contract.md](flow-contract.md) §6).
- Сбор значений `_prompt_variables` ([orchestrator.py](../../../src/wastech_orchestrator/core/orchestrator.py) ≈2118) — инъекция путей артефактов.

Что **меняется**:

- **Источник шаблона = `role_file`** (резолвится относительно директории flow; path-containment проверяет валидатор P0.3 — без `..`/абсолютных путей). Обёртка узла: `AgentRunRequest.prompt = render_prompt(read(role_file), _prompt_variables(node, ...))`. packaged-flow поставляют `roles/*.md` рядом с YAML; операторский — в `.worc/flows/roles/`.
- **`prompts.mode` (append/replace) удаляется**: `role_file` — единственный авторитетный источник шаблона на узел; кастомизация = правка MD. Поглощает текущую секцию конфига `prompts.{templates_dir,mode}` ([index.md](index.md) §14.2).
- **Allowlist расширяется единый** (без per-flow наборов — иначе доменное знание в движке): добавляется `{repo}` (алиас `repo_path`, [flow-contract.md](flow-contract.md) §6) и пути выхода research/audit (`{research_dir}`/`{report_dir}`, P3.2). Неприменимые переменные → `None` → пустая строка (как сейчас).
- `evaluator`-узел использует **ту же** машинерию (`role_file` + `output_schema` для вердикта/findings). `checks`/`hitl`/`publish` — **без промпта** (не агентные).

Примеры (источник — `role_file`, инъектируются только пути):

| Узел (kind, role) | `role_file` | Инъектируемые vars | Output schema |
| --- | --- | --- | --- |
| `refinement` (agent) | `roles/refinement.md` | `{task_path}` | refinement-spec |
| `implementation` (agent) | `roles/implementation.md` | `{plan_path}`, `{skills_path}`, `{task_path}` | — |
| `fixing` (agent) | `roles/fixing.md` | `{diff_path}`, `{checks_path}`, `{review_path}` | — |
| `supervise_impl` (evaluator, supervisor) | `roles/supervisor.md` | `{task_path}`, `{diff_path}` | `{verdict, findings[]}` |
| `summary` (evaluator, final_handoff) | `roles/supervisor-final.md` | `{task_path}`, `{diff_path}` | summary-schema |
| `synthesis` (agent, research) | `roles/research/synthesis.md` | `{repo}`, `{research_dir}` | — |
| `testing` / `publish` / `hitl` | — (нет промпта) | — | — |

Пошаговый разбор сборки контекста для **всех** agent/evaluator-узлов `implementation` и `security_audit` (что заполнено на каждом шаге графа, какие пути инъектируются, session/права/output_schema per node) — в [context-assembly.md](context-assembly.md).

---

## P1.4 — Паритетный `implementation.yaml` + golden-harness

Цель: текущее поведение (без supervisor, in-memory сессии, review+checks как есть) выражено графом; существующие тесты implementation зелены через движок.

### Touchpoints

- **Новая фикстура** `tests/core/flows/implementation_parity.yaml` — НЕ packaged `implementation.yaml` (тот уже целевой, с supervisor/hybrid). Паритетный flow: `refinement(opt) → planning → implementation → testing(checks) → review(evaluator) → fixing → summary(agent) → publish`, с рёбрами fix-петель и decomposition, **без** supervise_impl/supervise_fix/testing_quality.
- Golden-harness: существующий набор интеграционных тестов pipeline (использует fake-CLI фикстуры, см. skill `fake-cli`) прогоняется и через старый `_drive`, и через `FlowEngine`; границы сравниваются.

### Поведение

- Паритетный flow воспроизводит `Stage`-конвейер ([providers/base.py](../../../src/wastech_orchestrator/providers/base.py) `Stage`): refinement→agent (optional, `when: derived.needs_refinement`), planning→agent, implementation→agent (editing_lineage), testing→checks, review→evaluator(review), fixing→agent (lineage_affinity:implementation), summary→agent, publishing→publish. fix-петли: `testing fail→fixing (loop test_fix)`, `review rework→fixing (loop review_fix)`.
- Decomposition-конструкция (P0 снапшот уже парсит `decomposition`) исполняется движком как фан-аут под-flow (паритет `core/decomposition.py` + `_run_units_and_finish`).

### Тесты

- Весь существующий pipeline-тест-сьют зелёный через движок (happy-path, fix-петли, decomposed, recovery, rerun/continue, manual-исходы).
- `test_parity_flow_matches_legacy_boundary` — статусы/артефакты/commit-SHA/publish-ops/ledger совпадают между `_drive` и `FlowEngine` на наборе сценариев.

### Exit

Движок воспроизводит текущий конвейер байт-в-байт на наблюдаемой границе.

---

## P1.5 — Удаление старого драйвера

Цель: одна модель исполнения; мёртвый код удалён. Делается **только** после зелёного P1.4.

### Touchpoints

- [`core/orchestrator.py`](../../../src/wastech_orchestrator/core/orchestrator.py): снести `_drive` (≈820), `_run_unit` (≈1193), `_enter_fixing` (≈1465), `_after_edit_target`, dispatch-on-status в `_resume_task` (≈734); `run_task`/`resume`-обвязка остаётся, но тело делегирует `FlowEngine`.
- [`core/state_machine.py`](../../../src/wastech_orchestrator/core/state_machine.py): удалить implementation-специфичные статусы и их `ALLOWED_TRANSITIONS`-рёбра (оставить родовой lifecycle из P1.2).
- [`providers/base.py`](../../../src/wastech_orchestrator/providers/base.py): `Stage`-перечисление в роли описания конвейера удаляется/депрекейтится (узлы графа теперь источник). Если `Stage` используется в артефакт-раскладке (`stages/<stage>/...`, [providers/artifacts.py](../../../src/wastech_orchestrator/providers/artifacts.py) `create_attempt_dir`) — заменить на `node_id`.
- Снять двойной CI-прогон из P1.

### Тесты

- Удалить тесты, дублирующие старый драйвер; оставить движковые. Сьют остаётся зелёным.
- `test_no_drive_symbol` (grep-guard) — `_drive`/`_run_unit` отсутствуют в кодовой базе.

### Exit

Одна модель исполнения; `_drive`/реентри-диспетчер/`Stage`-как-конвейер удалены.

---

## Сквозной обзор зависимостей P1

```text
P1.1 (engine core) ──┬─> P1.3 (node wrappers) ──> P1.4 (parity + golden) ──> P1.5 (delete legacy)
                     │
P1.2 (resume/checkpoint, schema v4) ──────────────┘   (P1.4 нужен resume по графу)
```

P1.1 и P1.2 можно вести параллельно (engine ↔ persistence), но P1.4 требует обоих + P1.3. P1.5 — строго последний.

## Контракт выхода P1 → P2

- Движок исполняет произвольный валидный граф; переходы — ядро; resume идемпотентен.
- `node_runs`/`FlowRunState`/`publish_operations` — стабильная основа для feature-таблиц P2 (`editing_lineage`, `evaluations`).
- evaluator-узел существует в минимальной форме (`role=review`); P2 наслаивает supervisor/critic/verifier/test_quality + immutable-вердикты + durable sessions поверх **доказанного** движка.
- Старый драйвер удалён — P2 не имеет двух моделей исполнения.
