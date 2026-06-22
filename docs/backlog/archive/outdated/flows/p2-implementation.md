# P2 — Целевые возможности implementation как узлы

Статус: **backlog / инженерная спека (запланировано к исполнению)** Дата: 2026-06-17 (ревизия 2026-06-19) Владелец: Vladimir Makarevich

Детализация фазы P2 из [plan.md](plan.md). Цель: на **доказанном** движке (P1) нарастить целевой implementation, адаптировав три поглощённые программы — [supervisor](../archive/outdated/supervisor_quality_gate.md), [durable sessions](../archive/outdated/durable_sessions_and_fixing_affinity.md), [hybrid testing](../archive/outdated/hybrid_agent_testing.md). База проверки фазы: **тесты из спек трёх программ** покрывают новые узлы (адаптированный на движок интеграционный сьют из P1 анкерит неизменное ядро — golden-harness отменён, см. [plan.md](plan.md)).

> **Ревизия архитектуры supervisor (2026-06-19).** Зафиксировано: flow — **полностью конфигурируемый граф любых узлов**; оператор вправе описать ЛЮБОЙ flow и любое число агентов в YAML — вплоть до одного implement-агента без проверок, ревью и чеков. **Единственная константа — supervisor, который живёт отдельным слоем НАД flow** (на уровне оркестратора, не узлом графа): стартует при старте задачи, **живёт весь цикл и проверяет каждый завершённый шаг** (read-only, своя `resume_own_lineage`-сессия, advisory — не блокирует), а при закрытии всей задачи пишет `summary` + advises. Отдельных блокирующих per-stage supervisor-узлов (`supervise_impl`/`supervise_fix`) в графе **больше нет** — кому нужны блокирующие пер-стейдж гейты, добавляет опциональные `review`/`test_quality` (или operator-defined) evaluator-узлы в YAML. Детали — P2.1; целевой граф — P2.5; зеркальные правки контракта — [flow-contract.md](flow-contract.md) §2.2/§4/§7, [security-ceiling.md](security-ceiling.md) §3.

Канонический порядок ландинга ([memory: quality-program-canonical-order]): **supervisor → durable → hybrid**. Supervisor стартует первым (постоянный слой-наблюдатель + общий evaluator-примитив; своя сессия пока in-memory, durable — в P2.2); durable формализует scopes/affinity (включая durable `resume_own_lineage` для supervisor'а); hybrid — последний неблокирующий слой.

Вход: целевой evaluator-примитив ([flow-contract.md](flow-contract.md) §2.2), session_scope ([flow-contract.md](flow-contract.md) §4), packaged [implementation.yaml](../../../src/wastech_orchestrator/core/flow/packaged/implementation.yaml) (сейчас — P1-parity форма: refinement → planning → implementation → testing → review → fixing → summary → publish, без supervisor/hybrid/durable; P2 наслаивает целевые узлы, P2.5 приводит файл к целевому графу).

---

## P2.1 — Постоянный supervisor-слой + evaluator-примитив

Ландит **первым** в P2 — даёт сразу две вещи: (1) константный **supervisor-слой над flow** и (2) общий **evaluator-примитив**, который переиспользуют опциональные in-flow evaluator'ы (`review` — P2.3, `test_quality` — P2.4) и P3 (`verifier`/`critic`).

### Архитектура: supervisor — постоянный наблюдатель НАД flow (не узел)

- **Не узел графа.** Живёт отдельным слоем на уровне **оркестратора**; **движок остаётся без доменного знания** ([flow-contract.md](flow-contract.md) §7) — он исполняет граф, supervisor его не касается. Единственная доменная константа: есть у **каждой** задачи при любой форме flow (даже один implement-агент без проверок).
- **Жизненный цикл:** стартует **при старте задачи**, **живёт весь цикл** (все шаги + все подзадачи), завершается перед `publish`. **Один** supervisor на задачу (охватывает подзадачи).
- **Проверяет каждый завершённый шаг** read-only: после завершения каждого узла оркестратор даёт supervisor посмотреть результат шага (≈один вызов LLM/шаг), копя контекст в **своей сессии `resume_own_lineage`** (независима от editing-lineage авторов). _В P1 own-session — in-memory; durable `resume_own_lineage` (переживает рестарт) формализуется в [P2.2](#p22--durable-sessions-ядровая-возможность)._
- **Advisory, не блокирует.** Не выдаёт `rework` / не переоткрывает / не маршрутизирует — пошаговые наблюдения и финал записываются и показываются человеку, но маршрут движка не меняют. Блокирующие пер-стейдж гейты — опциональные in-flow `review`/`test_quality`/operator-defined evaluator-узлы (умеют `rework` → `fixing`). Заменяет старый summary-провайдер и узлы `supervise_impl`/`supervise_fix` — их в графе нет.
- **Финал — один на задачу.** При закрытии **всей задачи** (все подзадачи готовы) синтезирует накопленный контекст → `summary` + advise. **Не** per-subtask. `summary` пишется **всегда** (`config.summary_enabled` убирается).
- **Конфиг оператора, не задачи/не flow-графа.** Модель, `reasoning`/effort и промпт (`role_file`) — в `config.yaml` (новая секция `supervisor:`), под тем же потолком, что узлы: `model`/`reasoning` ∈ allowlist, `role_file` — path-containment, `permission_profile` принудительно `read-only` ([security-ceiling.md](security-ceiling.md) §3).
- **Будущее (вне scope P2):** когда добавим память оркестратора — supervisor становится **владельцем/контролёром памяти**, обогащая контексты конкретных узлов. Поэтому он и спроектирован как сквозной наблюдатель, а не финальный проход.

### Advisory — что это, кому, где

«Advisory» = вывод, который **записывается и показывается человеку, но не меняет маршрут движка** (не уводит в `fixing`, не переоткрывает задачу). И пошаговые наблюдения supervisor, и его финальные findings — advisory **по конструкции** (он не маршрутизирует вовсе):

- **Кто получает:** человек — автор задачи / ревьюер. Через (1) тело PR (summary становится описанием PR) и (2) Telegram-уведомление о завершении. Движок его для маршрутизации **не потребляет**.
- **Где сохраняется:** (1) артефакт `summary.{md,json}` — основной носитель, advisory-замечания отдельной секцией («caveats / follow-ups»); (2) immutable-строка в таблице `evaluations` (вердикт + `findings_json`) — для аудита/recovery; raw-данные остаются в `state.db`.
- **Где используется:** PR-описание + Telegram (для человека) + аудит-трейл. **Не** используется для rework/reopen; если замечание важно — человек заводит новую задачу.

### Touchpoints

- [`core/flow/nodes/evaluator.py`](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py) (создан минимально в P1.3 для review) — носитель **evaluator-примитива** (вердикт + findings) для in-flow ролей `review`/`test_quality`. Supervisor переиспользует те же типы findings, но исполняется не как узел, а оркестраторным хуком.
- **Новый метод** [`core/loop_control.py`](../../../src/wastech_orchestrator/core/loop_control.py) `record_rework(...)` рядом с `enter_fixing` (≈63) — **единый** путь учёта rework для **in-flow петель** (`test_fix`, `review_fix`); никогда не двойной инкремент `fix_iterations`. Supervisor его **не зовёт** (он не делает rework).
- [`core/orchestrator.py`](../../../src/wastech_orchestrator/core/orchestrator.py): **post-node хук** (рядом с `_engine_post_node` ≈1046) — после **каждого** завершённого узла зовёт supervisor-наблюдение (read-only, в своей `resume_own_lineage`-сессии). **Финальный** `summary` + advise — при терминале **всей задачи** (`_publish`-обвязка ≈1345), синтез накопленного контекста; переиспользует существующую summary-запись (`summary.{md,json}` или детерминированный fallback). `config.summary_enabled` **убирается** — summary пишется всегда. Отдельный summary-провайдер не вводится.
- [`state_store.py`](../../../src/wastech_orchestrator/state_store.py): **новая feature-таблица** `evaluations` (immutable), schema bump. Хранит: пошаговые supervisor-наблюдения, финальный supervisor-итог, и in-flow evaluator-вердикты. Локальный лимит rework in-flow evaluator'ов **выводится подсчётом** вердиктов, без мутабельного счётчика.
- [`core/flow/snapshot.py`](../../../src/wastech_orchestrator/core/flow/snapshot.py)/[`validator.py`](../../../src/wastech_orchestrator/core/flow/validator.py): **удалить мёртвый** `evaluation_kind: final_handoff` (supervisor — не узел; решение P.5c).
- `config.yaml`: **новая секция** `supervisor: { model, reasoning, role_file }`; валидируется под потолком (provider-allowlist + path-containment + `read-only`).
- Recovery: supervisor own-session регидратируется (durable — P2.2); финальный summary не дублируется на resume; пошаговые наблюдения неймспейснуты по `(execution_unit, source_node_run_id)`. In-flow evaluator-вердикты — по `source_node_run_id`.

### Новые типы / схема

```python
# core/flow/engine.py (или contracts.py)
@dataclass(frozen=True, slots=True)
class Finding:
    severity: Literal["low", "medium", "high"]
    reason: str
    paths: tuple[str, ...] = ()

# state_store.py — новая таблица (immutable, append-only)
@dataclass(frozen=True)
class EvaluationRow:
    task_id: str
    node_id: str | None           # in-flow evaluator-узел; None = supervisor-слой
    source_node_run_id: int | None # какой шаг/node_run оценивался
    subtask_order: int | None
    kind: str                     # in_flow_verdict | supervisor_step | supervisor_final
    verdict: str                  # accept|rework (in-flow); advisory (supervisor — не маршрутизирует)
    findings_json: str            # сериализованные Finding[]
    created_at: str
    # append-only; in-flow лимит = COUNT(kind='in_flow_verdict' AND verdict='rework')
```

Вердикт строго валидируется ([flow-contract.md](flow-contract.md) §2.2): для **in-flow** evaluator'ов `rework` требует ≥1 `medium`/`high`; `accept` не содержит блокирующих; `low` — advisory. **Supervisor** маршрут не выбирает — его пошаговые наблюдения (`supervisor_step`) и финал (`supervisor_final`) всегда advisory.

### Поведение

- **In-flow evaluator'ы** (`review`/`test_quality`) — read-only, `fresh_disposable`. Исход `accept` → следующий узел; `rework` → ребро на `fixing`. Прохождение rework-ребра зовёт `record_rework` → один инкремент `fix_iterations` (тот же путь, что `enter_fixing` для test-петли — без двойного счёта). Локальный per-instance лимит (`max_rework_per_stage`/`budget`) — `COUNT` rework-вердиктов; превышение учитывается движком как исчерпание budget-ребра.
- **Supervisor** — постоянный read-only наблюдатель на уровне оркестратора: после **каждого** завершённого шага получает его результат и копит контекст в своей `resume_own_lineage`-сессии (≈один вызов LLM/шаг); при закрытии **всей задачи** (все подзадачи) синтезирует `summary` + advise (всегда, не per-subtask). **Не может** выдать rework / переоткрыть задачу / маршрутизировать. Запускается **всегда**, при любой форме flow (включая degenerate `implementation → publish`).

### Тесты

Адаптированные на новую модель (из [supervisor §minimum tests](../archive/outdated/supervisor_quality_gate.md#minimum-tests)):

- `test_supervisor_runs_above_any_flow` — supervisor работает + пишет summary даже для degenerate-flow (один implement-агент, без checks/review).
- `test_supervisor_observes_each_completed_step` — после каждого завершённого узла идёт read-only supervisor-наблюдение в его `resume_own_lineage`-сессии.
- `test_supervisor_advisory_never_reworks` — supervisor не может выдать rework / переоткрыть / маршрутизировать; наблюдения и финал advisory.
- `test_supervisor_summary_once_per_whole_task_not_subtask` — при decomposition summary пишется один раз при закрытии всей задачи, не на каждую подзадачу.
- `test_summary_always_written_no_config_gate` — `config.summary_enabled` удалён; summary пишется всегда.
- `test_supervisor_config_from_config_yaml` — `model`/`reasoning`/`role_file` из `config.yaml`, валидируются под потолком (read-only, allowlist, containment).
- `test_supervisor_own_session_not_editing_lineage` — supervisor в `resume_own_lineage`, не читает/не пишет editing-lineage авторов.
- `test_record_rework_single_increment` — один rework in-flow evaluator'а = один `fix_iterations` (нет двойного счёта с test-петлёй).
- `test_evaluation_immutable_and_counted` — вердикты/наблюдения append-only; in-flow лимит = COUNT, не мутабельный счётчик.
- `test_supervisor_recovery` — resume не дублирует финальный summary; накопленный контекст регидратируется (durable — P2.2).

### Exit

Supervisor — постоянный конфигурируемый слой-наблюдатель над **любым** flow (per-step advisory весь цикл + финальный summary/advise один на задачу, всегда), не узел; блокирующих supervisor-узлов в графе нет; `final_handoff` удалён; evaluator-примитив готов для in-flow `review`/`test_quality`.

> **Статус: ✓ Реализовано (2026-06-19).** [`core/supervisor.py`](../../../src/wastech_orchestrator/core/supervisor.py) (константный слой: per-step `observe` в своей `resume_own_lineage`-сессии + финальный `finalize`-summary, advisory, встроен в оркестратор — post-node-хук + `_engine_finalize`); `evaluations`-таблица (immutable, schema v8) + `EvaluationRow`/`record_evaluation`/`count_rework_verdicts` в [`state_store.py`](../../../src/wastech_orchestrator/state_store.py); `record_rework` в [`core/loop_control.py`](../../../src/wastech_orchestrator/core/loop_control.py) (единый global-fix инкремент, зовётся движком); `config.yaml: supervisor:{model,reasoning,role_file}` (схема/лоадер/валидатор под потолком); `EvaluationKind`/`final_handoff` и summary-узел/`config.summary_enabled` удалены (`summary` убран из `SKIPPABLE_STAGES`); enriched `Finding(severity/reason/paths)`. Тесты: `tests/core/test_supervisor.py` + supervisor-e2e в `tests/core/test_orchestrator.py` (ruff/mypy/pytest зелёные, 1141). Открыто: durable `resume_own_lineage` (P2.2); полный re-sync line-ref'ов функциональной карты — в конце P2 (см. follow_ups).

---

## P2.2 — Durable sessions (ядровая возможность)

Ландит **после** supervisor. Нормализованный lineage-стор; resume для Claude и Codex; provider-aware per-attempt request; redaction raw session-id; `session_scope`; affinity (объявляемая во flow связь lineage между узлами); `session_unavailable` → ретрай без resume (не тратит fix-итерацию).

### Touchpoints

- [`providers/codex.py`](../../../src/wastech_orchestrator/providers/codex.py): добавить resume — `exec resume <id>` + парс `thread.started` в `parse_events` (≈233). **Сейчас Codex resume не поддерживает** (явный комментарий ≈200; `session_id` парсится ≈257, но информационно). `build_codex_argv` (≈153) расширяется на resume-режим.
- [`providers/claude.py`](../../../src/wastech_orchestrator/providers/claude.py): resume уже есть — `--resume <session_id>` в `build_claude_argv` (≈295); `session_id` извлекается в `parse_stream_json` (≈354). Нужно подключить к lineage-стору, а не к in-memory передаче.
- [`routing/router.py`](../../../src/wastech_orchestrator/routing/router.py): `run_stage` (≈171) — provider-aware per-attempt request (resume vs fresh); `session_unavailable`-путь.
- [`state_store.py`](../../../src/wastech_orchestrator/state_store.py): **новая таблица** `editing_lineage` (нормализованный lineage per `execution_unit`); raw session-id хранится **только** здесь.
- [`providers/redaction.py`](../../../src/wastech_orchestrator/providers/redaction.py): redaction raw session-id в артефактах/логах/argv (нормализованный id наружу, raw — в state.db). `redact_text`/`redact_mapping` (≈94/≈108) + `read_denied_secrets`.
- [`core/flow/contracts.py`](../../../src/wastech_orchestrator/core/flow/contracts.py) `SessionScope` (уже есть: `EDITING_LINEAGE`/`FRESH_DISPOSABLE`/`RESUME_OWN_LINEAGE`) — теперь с реализацией.

### Новые типы / схема

```python
# state_store.py — editing_lineage feature-таблица
@dataclass(frozen=True)
class EditingLineageRow:
    task_id: str
    subtask_order: int | None      # execution_unit = (task_id, subtask_order), contracts.ExecutionUnit
    provider: str                  # claude | codex (lineage provider-bound)
    raw_session_id: str            # НИКОГДА не покидает state.db; redacted во всех артефактах
    updated_at: str
    # ключ: (task_id, subtask_order) — одна активная editing-сессия на execution_unit
```

### Поведение

- `editing_lineage` (implementation/fixing) — грузят и обновляют активную editing-сессию из стора. `fresh_disposable` (in-flow evaluator) — свежая сессия, не читает/не пишет lineage. `resume_own_lineage` — продолжает **свою** сессию: research critic (P3) **и supervisor-слой** (своя сессия на весь цикл задачи).
- **Affinity — общий YAML-механизм.** `lineage_affinity: <node_id>` на узле-авторе означает «продолжить editing-сессию указанного узла»; ядро это гарантирует (durable sessions), **flow только декларирует связь** ([flow-contract.md](flow-contract.md) §4). В packaged [implementation.yaml](../../../src/wastech_orchestrator/core/flow/packaged/implementation.yaml) это используется так: `lineage_affinity: implementation` на `fixing` (≈54) → fixing продолжает editing-сессию узла implementation. **Это не захардкоженное правило `fixing→implementation`, а то, что объявил данный flow** — оператор волен связать любые два узла-автора (или не связывать вовсе). Валидатор проверяет, что `lineage_affinity` указывает на существующий `agent`-узел с `editing_lineage` ([validator.py](../../../src/wastech_orchestrator/core/flow/validator.py) ≈165). Конфликтующий per-attempt провайдер/модель отвергается, пока affinity активна.
- **`session_unavailable`** (потерян транскрипт/провайдер сбросил сессию): ретрай без resume (fresh), **не тратит** fix-итерацию. Отличается от quality-fail (тот тратит). Новый error-подкласс или флаг в router-обработке.
- **Lineage переживает рестарт**: editing_lineage регидратируется до вызова провайдера (recovery, [happy-path.md](happy-path.md) §6). In-flow evaluator её **не трогает** (`fresh_disposable`); supervisor работает в **своей** `resume_own_lineage`-сессии, editing-lineage авторов не перетирает.

### Тесты

Из [durable §minimum tests](../archive/outdated/durable_sessions_and_fixing_affinity.md#minimum-tests):

- `test_claude_resume_uses_lineage_session` — `--resume` берёт session_id из lineage-стора.
- `test_codex_exec_resume_parses_thread_started` — Codex `exec resume <id>`, парс `thread.started`.
- `test_affinity_resumes_declared_node_session` — узел с `lineage_affinity: X` продолжает editing-сессию узла X (в packaged-флоу: fixing → implementation).
- `test_evaluator_fresh_disposable_does_not_touch_lineage` — in-flow evaluator (`fresh_disposable`) и supervisor (`resume_own_lineage`) не читают/не пишут editing_lineage авторов.
- `test_session_unavailable_retries_without_resume_no_fix_iteration` — потеря сессии → fresh-ретрай, `fix_iterations` не растёт.
- `test_raw_session_id_redacted_in_artifacts` — raw session-id только в state.db; в логах/argv/артефактах — redacted.
- `test_lineage_survives_restart` — editing_lineage регидратируется на resume.
- `test_conflicting_provider_override_rejected_under_affinity`.

### Exit

Editing-lineage переживает рестарт, evaluator/supervisor её не перетирают, объявленная во flow affinity работает; raw session-id не утекает.

> **Статус: ✓ Реализовано (2026-06-19).** `editing_lineage`-таблица (schema v9, raw session-id **только** там) + `EditingLineageRow`/`get_editing_lineage`/`upsert_editing_lineage`/`clear_editing_lineage` ([state_store.py](../../../src/wastech_orchestrator/state_store.py)); Codex `exec resume <id>` + парс `thread.started.thread_id` ([codex.py](../../../src/wastech_orchestrator/providers/codex.py), контракт PRE.4); Claude `--resume` подключён к стору; durable `session_scope` в [`agent.py`](../../../src/wastech_orchestrator/core/flow/nodes/agent.py) (`_resume_session_id`/`_persist_session` через стор — in-memory `session_ids`-карта удалена); `lineage_affinity` реализован общей editing-сессией на execution*unit (validator отвергает конфликтующий explicit `provider`); `session_unavailable` → fresh-ретрай того же провайдера в [`router.py`](../../../src/wastech_orchestrator/routing/router.py) (новый `ErrorClass.SESSION_UNAVAILABLE`, не fallback, не тратит fix-итерацию); redaction raw session-id во всех артефактах/argv/логах + `normalized_session_id` в `result.json` ([redaction.py](../../../src/wastech_orchestrator/providers/redaction.py)). Тесты: `tests/routing/test_session_unavailable.py`, durable-кейсы в `tests/core/test_flow_node_runners.py` + `test_flow_validator.py` + `test_supervisor.py`, `editing_lineage` в `tests/state/test_state_store.py`, Codex resume/redaction в `tests/providers/test_codex*{command,parsing,run}.py`(ruff/mypy/pytest зелёные). **Отложено:** durable`resume_own_lineage` для supervisor-слоя (его собственная сессия пока in-memory — переживает один цикл, но не рестарт; нет блокирующего теста, finalize идемпотентен на resume) — см. follow_ups.

### Решения (зафиксировано 2026-06-17)

- **Codex `exec resume` — сначала верификация на реальном CLI.** P2.2 Codex-resume **гейтится** на проверке реального CLI-контракта (точная форма `codex exec resume <id>` vs флаг; формат события `thread.started`) **до** реализации; результат фиксируется fake-CLI фикстурой (skill `fake-cli`). Почему: durable-affinity опирается на резюм; ошибка в контракте сломала бы ядровую возможность и дала бы ложно-зелёные фикстуры.

---

## P2.3 — review как evaluator-узел

`role=review` — **обычная конфигурируемая agent-role на уровне YAML**: задаётся узлом `kind: evaluator, role: review`, опциональна (`when: config.review_enabled`), полностью удаляема (нет review-узла → нет review). Блокирующие findings → `rework` → fixing. В P1.3 уже создана минимальная review-обёртка; P2.3 приводит её к полному evaluator-контракту (immutable-вердикты, findings-схема как у in-flow evaluator'ов).

### Touchpoints

- [`core/flow/nodes/evaluator.py`](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py) — `role=review` использует тот же путь вердикта/findings, что и прочие in-flow evaluator'ы; разница только в промпте (`role_file`) и бюджете (`loop: review_fix`).

### Тесты

- `test_review_blocking_to_fixing` — блокирующий review → fixing (loop review_fix).
- `test_review_clean_to_next` — чистый review → следующий узел.
- `test_review_is_ordinary_evaluator` — review использует evaluator-механику, не спец-стадию; flow без review-узла валиден.

### Exit

review — обычный конфигурируемый evaluator-узел в YAML, не отдельная стадия и не константа; легко добавляется и убирается из flow.

> **Статус: ✓ Реализовано (2026-06-19).** Полный evaluator-контракт уже даёт P2.1 (унифицированный [`evaluator.py`](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py): immutable `in_flow_verdict`, severity-findings, accept/rework). `review` — узел `kind: evaluator, role: review` в packaged [implementation.yaml](../../../src/wastech_orchestrator/core/flow/packaged/implementation.yaml), опционален (`when: config.review_enabled`), блокирующий → `rework` по ребру `review_fix`, полностью удаляем. Тесты: `test_review_is_ordinary_evaluator` (review = обычный evaluator, immutable-вердикт) + существующие `test_evaluator_maps_blocking_findings` (blocking→rework / clean→accept), e2e `test_review_blocking_then_fix` и `test_skip_review_commits_without_review` (удаляемость).

---

## P2.4 — Hybrid testing evaluator + mutation guard

Детали: [p2.4-hybrid-testing.md](p2.4-hybrid-testing.md).

> **Статус: ✓ Реализовано (2026-06-19).** Неблокирующий evaluator самокапируется через COUNT собственных `in_flow_verdict`-строк (`max_rework_per_stage`): первый блокирующий проход → `rework`, на исчерпании бюджета → `accept` (→ continue), **не** manual ([`core/flow/nodes/evaluator.py`](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py)). Mutation guard — core-owned свойство узла `checks`: snapshot рабочего дерева до/после прогона, зелёный-но-грязнящий чек fail-closed → manual ([`core/flow/nodes/checks.py`](../../../src/wastech_orchestrator/core/flow/nodes/checks.py)). Узел `testing_quality` в packaged `implementation.yaml` ещё не добавлен — это P2.5 (рост packaged-файла к целевой форме); поведение и тесты готовы.

---

## P2.5 — Целевой `implementation.yaml` + тесты из спек

Детали: [p2.5-target-yaml.md](p2.5-target-yaml.md).

> **Статус: ✓ Реализовано (2026-06-19), с правкой решения.** Mutation guard на `checks` (P2.4) активен в packaged-графе. **Опциональный `testing_quality`-узел в дефолтный packaged-флоу НЕ добавлен** — решение 2026-06-19: опциональность узла = форма графа, а не конфиг-флаг. Изначально я завёл `agents.hybrid_testing` + `when: config.hybrid_testing` + ветку в `_engine_facts`, но это «прибивание узла гвоздями к оркестратору» — флаг/факт/ветка убраны. `test_quality`-evaluator (неблокирующий self-cap) остаётся generic-возможностью рантайма evaluator'а (P2.4): оператор включает его, **вписав узел в свой флоу-YAML**; reference-граф — в [co-design/implementation.yaml](co-design/implementation.yaml). Дефолтный packaged-флоу остаётся лёгким (7 узлов, без +1 LLM-прохода на задачу). Тест `test_minimal_flow_implement_only` (degenerate `implementation→publish`, supervisor пишет summary) зелёный; spec-тесты P2.1–P2.4 зелены.

---

## Сквозной обзор зависимостей P2

```text
P1 (engine + evaluator(review) + node_runs)
   └─> P2.1 supervisor-слой + evaluator-примитив (record_rework, evaluations table, config.yaml supervisor)
        └─> P2.2 durable sessions (editing_lineage table, codex resume, объявляемая affinity)
             ├─> P2.3 review → full evaluator-узел (опциональный, YAML)
             └─> P2.4 hybrid test_quality (опциональный, YAML) + mutation guard на checks
                  └─> P2.5 целевой implementation.yaml + spec-тесты
```

## Контракт выхода P2 → P3

- evaluator-примитив полон: in-flow `review`/`test_quality` + immutable-вердикты + `record_rework`. P3 переиспользует тот же примитив для `verifier`/`critic` (research/audit) без новой механики.
- константный supervisor-слой доказан над implementation → P3 наследует его поверх research/audit flow без изменений (он не зависит от формы графа).
- durable sessions включают `resume_own_lineage` — нужен research `critic` (P3.3).
- Целевой implementation доказан → P3 доказывает **обобщаемость** на не-implementation flow (research/audit).

## Пересечения для ревью (потенциальные противоречия)

- **Единый учёт rework.** `record_rework` (P2.1) обслуживает **только in-flow петли** (`test_fix`, `review_fix`) и делит **один** инкремент `fix_iterations`. Константный supervisor rework не делает, поэтому риск двойного счёта на ребре `supervise_fix → fixing` (был в старой модели) **снят вместе с удалением supervise-узлов**. Остаётся свести `testing fail → fixing` и `review rework → fixing` к одному helper в `loop_control`; анкерит `test_record_rework_single_increment`.
- **session_scope evaluator/supervisor.** Валидатор P0.3 запрещает `editing_lineage` для evaluator ([validator.py](../../../src/wastech_orchestrator/core/flow/validator.py) ≈215); in-flow evaluator — `fresh_disposable`, supervisor — `resume_own_lineage` (своя сессия, не editing-lineage авторов). P2.2 не должен это ослаблять.
- **Конфигурация supervisor.** Секция `config.yaml:supervisor` валидируется тем же потолком, что и узлы (read-only, allowlist `model`/`reasoning`, containment `role_file`). Задача и flow её не переопределяют ([security-ceiling.md](security-ceiling.md) §2 порядок authority).
