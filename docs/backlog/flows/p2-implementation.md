# P2 — Целевые возможности implementation как узлы

Статус: **backlog / инженерная спека (не запланировано к исполнению)** Дата: 2026-06-17 (ревизия 2026-06-19) Владелец: Vladimir Makarevich

Детализация фазы P2 из [plan.md](plan.md). Цель: на **доказанном** движке (P1) нарастить целевой implementation, адаптировав три поглощённые программы — [supervisor](../outdated/supervisor_quality_gate.md), [durable sessions](../outdated/durable_sessions_and_fixing_affinity.md), [hybrid testing](../outdated/hybrid_agent_testing.md). База проверки фазы: **тесты из спек трёх программ** покрывают новые узлы (адаптированный на движок интеграционный сьют из P1 анкерит неизменное ядро — golden-harness отменён, см. [plan.md](plan.md)).

> **Ревизия архитектуры supervisor (2026-06-19).** Зафиксировано: flow — **полностью конфигурируемый граф любых узлов**; оператор вправе описать ЛЮБОЙ flow и любое число агентов в YAML — вплоть до одного implement-агента без проверок, ревью и чеков. **Единственная константа — supervisor, который живёт отдельным слоем НАД flow** (на уровне оркестратора, не узлом графа): он всегда запускается в конце, пишет `summary` и делает лёгкий терминальный контроль (advisory). Отдельных блокирующих per-stage supervisor-узлов (`supervise_impl`/`supervise_fix`) в графе **больше нет** — кому нужны блокирующие пер-стейдж гейты, добавляет опциональные `review`/`test_quality` (или operator-defined) evaluator-узлы в YAML. Детали — P2.1; целевой граф — P2.5; зеркальные правки контракта — [flow-contract.md](flow-contract.md) §2.2/§4/§7, [security-ceiling.md](security-ceiling.md) §3.

Канонический порядок ландинга ([memory: quality-program-canonical-order]): **supervisor → durable → hybrid**. Supervisor стартует первым (даёт константный слой + общий evaluator-примитив на in-memory editing-lineage, которую несёт P1); durable формализует scopes/affinity вокруг уже существующего primitive; hybrid — последний неблокирующий слой.

Вход: целевой evaluator-примитив ([flow-contract.md](flow-contract.md) §2.2), session_scope ([flow-contract.md](flow-contract.md) §4), packaged [implementation.yaml](../../../src/wastech_orchestrator/core/flow/packaged/implementation.yaml) (сейчас — P1-parity форма: refinement → planning → implementation → testing → review → fixing → summary → publish, без supervisor/hybrid/durable; P2 наслаивает целевые узлы, P2.5 приводит файл к целевому графу).

---

## P2.1 — Постоянный supervisor-слой + evaluator-примитив

Ландит **первым** в P2 — даёт сразу две вещи: (1) константный **supervisor-слой над flow** и (2) общий **evaluator-примитив**, который переиспользуют опциональные in-flow evaluator'ы (`review` — P2.3, `test_quality` — P2.4) и P3 (`verifier`/`critic`).

### Архитектура: supervisor — константа НАД flow (не узел)

- **Supervisor — не узел графа.** Он живёт отдельным слоем на уровне **оркестратора** и запускается **всегда**, поверх любого flow (даже если flow — это один implement-агент без проверок). Это единственная доменная константа; сам **движок остаётся без доменного знания** ([flow-contract.md](flow-contract.md) §7) — он исполняет граф, supervisor его не касается.
- **Функции:** пишет `summary.{md,json}` и делает **лёгкий терминальный контроль** (см. «advisory» ниже). Заменяет собой и старый summary-провайдер, и старые узлы `supervise_impl`/`supervise_fix` — **отдельного summary-провайдера и блокирующих supervisor-узлов в графе нет**.
- **Терминальный:** supervisor идёт прямо перед core-owned `publish`; он **не может** выдать `rework`, переоткрыть задачу или уйти в петлю. Кому нужны блокирующие пер-стейдж гейты — добавляет опциональные `review`/`test_quality`/operator-defined evaluator-узлы **в** flow (они умеют `rework` → `fixing`).
- **Конфигурируем оператором, но не задачей и не flow-графом.** Модель, `reasoning`/effort и промпт (`role_file`) задаются в `config.yaml` (новая секция `supervisor:`), валидируются под тем же потолком, что и узлы: `model`/`reasoning` ∈ allowlist, `role_file` — path-containment, `permission_profile` принудительно `read-only` ([security-ceiling.md](security-ceiling.md) §3). Так supervisor остаётся гибко настраиваемым, но не уезжает во flow-граф и не ослабляется задачей.

### Advisory — что это, кому, где

«Advisory» = вывод, который **записывается и показывается человеку, но не меняет маршрут движка** (не уводит в `fixing`, не переоткрывает задачу). Поскольку supervisor терминален, любые его findings advisory по **позиции** (а не по severity):

- **Кто получает:** человек — автор задачи / ревьюер. Через (1) тело PR (summary становится описанием PR) и (2) Telegram-уведомление о завершении. Движок его для маршрутизации **не потребляет**.
- **Где сохраняется:** (1) артефакт `summary.{md,json}` — основной носитель, advisory-замечания отдельной секцией («caveats / follow-ups»); (2) immutable-строка в таблице `evaluations` (вердикт + `findings_json`) — для аудита/recovery; raw-данные остаются в `state.db`.
- **Где используется:** PR-описание + Telegram (для человека) + аудит-трейл. **Не** используется для rework/reopen; если замечание важно — человек заводит новую задачу.

### Touchpoints

- [`core/flow/nodes/evaluator.py`](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py) (создан минимально в P1.3 для review) — носитель **evaluator-примитива** (вердикт + findings) для in-flow ролей `review`/`test_quality`. Константный supervisor переиспользует те же типы вердикта/findings, но исполняется не как узел, а терминальным проходом оркестратора.
- **Новый метод** [`core/loop_control.py`](../../../src/wastech_orchestrator/core/loop_control.py) `record_rework(...)` рядом с `enter_fixing` (≈63) — **единый** путь учёта rework для **in-flow петель** (`test_fix`, `review_fix`); никогда не двойной инкремент `fix_iterations`. Константный supervisor его **не зовёт** (он не делает rework).
- [`state_store.py`](../../../src/wastech_orchestrator/state_store.py): **новая feature-таблица** `evaluations` (immutable-вердикты), schema bump. Локальный лимит rework in-flow evaluator'ов **выводится подсчётом** применённых вердиктов (как supervisor §rework), без отдельного мутабельного счётчика. Терминальный вердикт supervisor пишется сюда же — для аудита.
- [`core/orchestrator.py`](../../../src/wastech_orchestrator/core/orchestrator.py) (≈1345, `_publish`-обвязка): **константный supervisor-проход** перед `publish` — свежий read-only проход, синтезирует summary + advisory; переиспользует существующую summary-запись (`summary.{md,json}` или детерминированный fallback). **Отдельный summary-провайдер не вводится никогда.**
- `config.yaml`: **новая секция** `supervisor: { model, reasoning, role_file }`; валидируется под потолком (provider-allowlist + path-containment + `read-only`).
- Recovery: терминальный вердикт неймспейснут по `execution_unit` (`(task_id, subtask_order)`); resume перед `publish` не дублирует summary, если вердикт уже записан. In-flow evaluator-вердикты неймспейснуты по `source_node_run_id`.

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
    node_id: str | None           # узел-evaluator; None = терминальный supervisor-слой
    source_node_run_id: int | None # какой node_run оценивался (None для терминального слоя)
    subtask_order: int | None
    verdict: str                  # accept | rework  (терминальный supervisor — всегда accept)
    findings_json: str            # сериализованные Finding[]
    created_at: str
    # UNIQUE неявно: append-only; in-flow лимит выводится COUNT(verdict='rework')
```

Вердикт строго валидируется ([flow-contract.md](flow-contract.md) §2.2): для **in-flow** evaluator'ов `rework` требует ≥1 `medium`/`high`; `accept` не содержит блокирующих; `low` — advisory. Для **терминального supervisor** вердикт всегда `accept` (он не маршрутизирует), findings — advisory.

### Поведение

- **In-flow evaluator'ы** (`review`/`test_quality`) — read-only, `fresh_disposable`. Исход `accept` → следующий узел; `rework` → ребро на `fixing`. Прохождение rework-ребра зовёт `record_rework` → один инкремент `fix_iterations` (тот же путь, что `enter_fixing` для test-петли — без двойного счёта). Локальный per-instance лимит (`max_rework_per_stage`/`budget`) — `COUNT` rework-вердиктов; превышение учитывается движком как исчерпание budget-ребра.
- **Константный supervisor** — свежий read-only проход на уровне оркестратора перед `publish`: синтезирует принятый итог, пишет `summary.{md,json}`, прикрепляет advisory-findings. **Не может** выдать rework / переоткрыть задачу / запустить петлю. Запускается **всегда**, независимо от формы flow (включая degenerate `implementation → publish`).

### Тесты

Адаптированные на новую модель (из [supervisor §minimum tests](../outdated/supervisor_quality_gate.md#minimum-tests)):

- `test_supervisor_runs_above_any_flow` — supervisor пишет summary даже для degenerate-flow (один implement-агент, без checks/review).
- `test_supervisor_terminal_cannot_rework` — терминальный supervisor пишет summary, вердикт всегда `accept`, не может выдать rework/переоткрыть.
- `test_supervisor_advisory_persisted_and_surfaced` — advisory-findings попадают в `summary.{md,json}` + `evaluations`, отражаются в PR-описании; маршрут не меняют.
- `test_supervisor_config_from_config_yaml` — `model`/`reasoning`/`role_file` берутся из `config.yaml` и валидируются под потолком (read-only, allowlist, containment).
- `test_record_rework_single_increment` — один rework in-flow evaluator'а = один `fix_iterations` (нет двойного счёта с test-петлёй).
- `test_evaluation_immutable_and_counted` — вердикты append-only; in-flow лимит = COUNT, не мутабельный счётчик.
- `test_supervisor_recovery_terminal_verdict` — resume перед publish не дублирует summary, если терминальный вердикт уже записан.

### Exit

Supervisor — постоянный конфигурируемый слой над **любым** flow (summary + advisory), не узел; блокирующих supervisor-узлов в графе нет; отдельного summary-провайдера нет; evaluator-примитив готов для in-flow `review`/`test_quality`.

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

- `editing_lineage` (implementation/fixing) — грузят и обновляют активную editing-сессию из стора. `fresh_disposable` (evaluator, supervisor) — свежая сессия, не читает/не пишет lineage. `resume_own_lineage` (research critic, P3) — продолжает **свою** сессию.
- **Affinity — общий YAML-механизм.** `lineage_affinity: <node_id>` на узле-авторе означает «продолжить editing-сессию указанного узла»; ядро это гарантирует (durable sessions), **flow только декларирует связь** ([flow-contract.md](flow-contract.md) §4). В packaged [implementation.yaml](../../../src/wastech_orchestrator/core/flow/packaged/implementation.yaml) это используется так: `lineage_affinity: implementation` на `fixing` (≈54) → fixing продолжает editing-сессию узла implementation. **Это не захардкоженное правило `fixing→implementation`, а то, что объявил данный flow** — оператор волен связать любые два узла-автора (или не связывать вовсе). Валидатор проверяет, что `lineage_affinity` указывает на существующий `agent`-узел с `editing_lineage` ([validator.py](../../../src/wastech_orchestrator/core/flow/validator.py) ≈165). Конфликтующий per-attempt провайдер/модель отвергается, пока affinity активна.
- **`session_unavailable`** (потерян транскрипт/провайдер сбросил сессию): ретрай без resume (fresh), **не тратит** fix-итерацию. Отличается от quality-fail (тот тратит). Новый error-подкласс или флаг в router-обработке.
- **Lineage переживает рестарт**: editing_lineage регидратируется до вызова провайдера (recovery, [happy-path.md](happy-path.md) §6). Evaluator/supervisor её **не перетирают** (fresh_disposable).

### Тесты

Из [durable §minimum tests](../outdated/durable_sessions_and_fixing_affinity.md#minimum-tests):

- `test_claude_resume_uses_lineage_session` — `--resume` берёт session_id из lineage-стора.
- `test_codex_exec_resume_parses_thread_started` — Codex `exec resume <id>`, парс `thread.started`.
- `test_affinity_resumes_declared_node_session` — узел с `lineage_affinity: X` продолжает editing-сессию узла X (в packaged-флоу: fixing → implementation).
- `test_evaluator_fresh_disposable_does_not_touch_lineage` — evaluator/supervisor не читают/не пишут editing_lineage.
- `test_session_unavailable_retries_without_resume_no_fix_iteration` — потеря сессии → fresh-ретрай, `fix_iterations` не растёт.
- `test_raw_session_id_redacted_in_artifacts` — raw session-id только в state.db; в логах/argv/артефактах — redacted.
- `test_lineage_survives_restart` — editing_lineage регидратируется на resume.
- `test_conflicting_provider_override_rejected_under_affinity`.

### Exit

Editing-lineage переживает рестарт, evaluator/supervisor её не перетирают, объявленная во flow affinity работает; raw session-id не утекает.

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

---

## P2.4 — Hybrid testing evaluator + mutation guard

`role=test_quality` перед `checks`, **неблокирующий** (исчерпание → continue); **опциональный** commit-candidate mutation guard как свойство узла `checks`; reuse `record_rework`; тесты пишет implementation-агент, не evaluator.

### Touchpoints

- [`core/flow/nodes/evaluator.py`](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py) — `role=test_quality`, `blocking: false` — **конфигурируемый опциональный evaluator-узел** на уровне YAML (`when: config.hybrid_testing`, по умолчанию выключен).
- [`core/flow/nodes/checks.py`](../../../src/wastech_orchestrator/core/flow/nodes/checks.py) — commit-candidate mutation guard как core-owned свойство узла `checks` (детект, что чек ничего не намусорил в рабочем дереве перед commit-кандидатом). Опирается на [`check_runner.py`](../../../src/wastech_orchestrator/check_runner.py) `CheckOutcome`.
- [`core/loop_control.py`](../../../src/wastech_orchestrator/core/loop_control.py) — reuse `record_rework` (P2.1).

### Поведение

- `test_quality` оценивает качество тестов, написанных implementation-агентом (не пишет тесты сам). Неблокирующий: исход `rework` ограничен `budget`; на исчерпании flow идёт по `accept`-ребру (→ checks), **не** в manual.
- **mutation guard — опционален через отсутствие `checks`.** Это core-owned свойство узла `checks`: пока узел `checks` присутствует — guard действует и flow его не отключает (часть security-стойки, [security-ceiling.md](security-ceiling.md) §3). Но **flow без узла `checks`** (например, один implement-агент или любой граф без проверок) **guard'а не имеет и работает без него** — это и есть «опционально». Security не ослабляется: убрать guard при наличии `checks` нельзя; убрать сам `checks` — можно (это выбор формы flow, а не обход гейта).

### Тесты

Из [hybrid §minimum tests](../outdated/hybrid_agent_testing.md#minimum-tests):

- `test_test_quality_non_blocking_exhaustion_continues` — исчерпание budget → continue к checks, не manual.
- `test_test_quality_rework_to_fixing` — блокирующий findings → fixing.
- `test_mutation_guard_active_when_checks_present` — guard срабатывает на узле checks (когда `checks` есть) независимо от прочего flow.
- `test_flow_without_checks_has_no_mutation_guard` — flow без узла `checks` валиден и исполняется без guard'а.
- `test_test_quality_does_not_write_tests` — evaluator read-only; тесты пишет implementation.

### Exit

Опциональный test-quality-узел + mutation guard, действующий при наличии `checks` и отсутствующий без него.

---

## P2.5 — Целевой `implementation.yaml` + тесты из спек

Полный граф исполняется данными; тесты из спек трёх программ зелены. Целевой граф ([flow-contract.md](flow-contract.md) §7): refinement → planning → implementation → testing(`checks`) → review(evaluator) → fixing → publish, с двумя fix-петлями (`test_fix`, `review_fix`), опциональным `testing_quality`-узлом и decomposition. **`supervise_impl`/`supervise_fix` и summary-узел в графе отсутствуют** — supervisor (summary + advisory) делает константный слой оркестратора перед `publish` (P2.1).

### Touchpoints

- Packaged [implementation.yaml](../../../src/wastech_orchestrator/core/flow/packaged/implementation.yaml) — P2.5 наращивает P1-parity форму до целевого графа: добавляет опциональный `testing_quality`-узел и mutation-guard-свойство `checks`; **не** добавляет supervisor-узлы (их роль ушла в константный слой).
- Константный supervisor-проход оркестратора (P2.1) встраивается в финал любого flow перед `publish`.
- Тест-сьют объединяет durable + hybrid + review spec-тесты на одном flow + supervisor-слой поверх.

### Тесты

- `test_target_implementation_full_graph` — целевой граф исполняется end-to-end (happy + rework-петли test/review + decomposition) с константным supervisor-слоем сверху, как в [happy-path.md](happy-path.md) §4.
- `test_minimal_flow_implement_only` — degenerate flow (`implementation → publish`) валиден и исполняется; supervisor-слой всё равно пишет summary.
- Все spec-тесты P2.1–P2.4 зелены на packaged `implementation.yaml`.

### Exit

Целевой implementation исполняется данными; три программы растворены: durable/hybrid — в узлах и ядре, supervisor — в константном слое над flow.

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
- **session_scope evaluator/supervisor.** Валидатор P0.3 запрещает `editing_lineage` для evaluator ([validator.py](../../../src/wastech_orchestrator/core/flow/validator.py) ≈215); константный supervisor — тоже `fresh_disposable`. P2.2 не должен это ослаблять.
- **Конфигурация supervisor.** Секция `config.yaml:supervisor` валидируется тем же потолком, что и узлы (read-only, allowlist `model`/`reasoning`, containment `role_file`). Задача и flow её не переопределяют ([security-ceiling.md](security-ceiling.md) §2 порядок authority).
