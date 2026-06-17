# P2 — Целевые возможности implementation как узлы

Статус: **backlog / инженерная спека (не запланировано к исполнению)** Дата: 2026-06-17 Владелец: Vladimir Makarevich

Детализация фазы P2 из [plan.md](plan.md). Цель: на **доказанном** движке (P1) нарастить целевой implementation, адаптировав три поглощённые программы — [supervisor](../outdated/supervisor_quality_gate.md), [durable sessions](../outdated/durable_sessions_and_fixing_affinity.md), [hybrid testing](../outdated/hybrid_agent_testing.md). База проверки фазы: **тесты из спек трёх программ** покрывают новые узлы (golden-harness P1 анкерит неизменное ядро).

Канонический порядок ландинга ([memory: quality-program-canonical-order]): **supervisor → durable → hybrid**. Supervisor стартует на in-memory editing-lineage, которую несёт P1; durable формализует scopes/affinity вокруг уже существующего supervisor; hybrid — последний неблокирующий слой.

Вход: целевой evaluator-примитив ([flow-contract.md](flow-contract.md) §2.2), session_scope ([flow-contract.md](flow-contract.md) §4), packaged [implementation.yaml](../../../src/wastech_orchestrator/core/flow/packaged/implementation.yaml) (уже содержит supervise_impl/supervise_fix/testing_quality — в P1 они не исполнялись, паритет шёл через тестовую фикстуру).

---

## P2.1 — Supervisor как evaluator-узел

Ландит **первым** в P2. `role=supervisor`, `fresh_disposable`, вердикт `accept|rework` + findings (блокировка по `medium`/`high`). Привилегированного `core/supervisor.py` **нет** — это узел. final_handoff заменяет summary-провайдер.

### Touchpoints

- [`core/flow/nodes/evaluator.py`](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py) (создан минимально в P1.3 для review) — расширяется на `role=supervisor` + `evaluation_kind=final_handoff`.
- **Новый метод** [`core/loop_control.py`](../../../src/wastech_orchestrator/core/loop_control.py) `record_rework(...)` рядом с `enter_fixing` (≈63) — **единый** путь учёта rework (никогда не двойной инкремент `fix_iterations`).
- [`state_store.py`](../../../src/wastech_orchestrator/state_store.py): **новая feature-таблица** `evaluations` (immutable-вердикты), schema bump. Локальный лимит rework **выводится подсчётом** применённых вердиктов (как supervisor §rework), без отдельного мутабельного счётчика.
- Ядро пишет `summary.{md,json}` (или детерминированный fallback) на final_handoff — переиспользует существующую summary-запись из `_publish`-обвязки ([core/orchestrator.py](../../../src/wastech_orchestrator/core/orchestrator.py) ≈1345); **отдельный summary-провайдер не вводится никогда**.
- Recovery: вердикт неймспейснут по `source_node_run_id` (обобщение `source_stage_run_id`); resume в состоянии `implementing`/`fixing` восстанавливает последний вердикт.

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
    node_id: str                  # узел-evaluator
    source_node_run_id: int       # какой node_run оценивался
    subtask_order: int | None
    verdict: str                  # accept | rework
    findings_json: str            # сериализованные Finding[]
    created_at: str
    # UNIQUE неявно: append-only, лимит выводится COUNT(verdict='rework')
```

Вердикт строго валидируется ([flow-contract.md](flow-contract.md) §2.2): `rework` требует ≥1 `medium`/`high`; `accept` не содержит блокирующих; low — advisory.

### Поведение

- supervise_impl/supervise_fix — read-only evaluator, `fresh_disposable`. Исход `accept` → следующий узел; `rework` → ребро на `fixing` (инлайн `budget: 1`). Прохождение rework-ребра зовёт `record_rework` → один инкремент `fix_iterations` (тот же путь, что `enter_fixing` для test/review-петель — без двойного счёта).
- Локальный per-stage лимит (`max_rework_per_stage`, дефолт 1) — `COUNT` rework-вердиктов для данного `(node_id, source_node_run_id)`; превышение учитывается движком как исчерпание budget-ребра → manual.
- final_handoff (`summary`): свежий read-only проход, синтезирует принятый итог; **не может** выдать rework/переоткрыть задачу; ядро валидирует и пишет `summary.{md,json}`.

### Тесты

Из [supervisor §minimum tests](../outdated/supervisor_quality_gate.md#minimum-tests), адаптированные на узел:

- `test_supervisor_rework_on_high_finding_to_fixing` — high-finding → `rework` → fixing.
- `test_supervisor_accept_no_blocking` — accept без блокирующих → следующий узел.
- `test_supervisor_low_finding_advisory_not_blocking` — low → accept.
- `test_record_rework_single_increment` — один rework = один `fix_iterations` (нет двойного счёта с test/review-петлями).
- `test_evaluation_immutable_and_counted` — вердикты append-only; лимит = COUNT, не мутабельный счётчик.
- `test_final_handoff_writes_summary_cannot_rework` — final_handoff пишет summary, не может выдать rework.
- `test_supervisor_recovery_by_source_node_run` — resume в fixing восстанавливает последний вердикт.

### Exit

Mandatory supervisor после implementation/fixing работает как узел; нет отдельного summary-провайдера. («Mandatory» = присутствие узла в `implementation.yaml`; «выключить» = отредактировать flow.)

---

## P2.2 — Durable sessions (ядровая возможность)

Ландит **после** supervisor. Нормализованный lineage-стор; resume для Claude и Codex; provider-aware per-attempt request; redaction raw session-id; `session_scope`; affinity `fixing → implementation`; `session_unavailable` → ретрай без resume (не тратит fix-итерацию).

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

- `editing_lineage` (implementation/fixing) — грузят и обновляют активную editing-сессию из стора. `fresh_disposable` (evaluator) — свежая сессия, не читает/не пишет lineage. `resume_own_lineage` (research critic, P3) — продолжает **свою** сессию.
- **Affinity** `lineage_affinity: implementation` на `fixing` ([packaged implementation.yaml](../../../src/wastech_orchestrator/core/flow/packaged/implementation.yaml) ≈65) → ядро гарантирует, что fixing продолжает editing-сессию узла implementation. Конфликтующий per-attempt провайдер/модель для fixing отвергается, пока affinity активна (валидатор уже проверяет, что `lineage_affinity` указывает на agent с `editing_lineage`, [validator.py](../../../src/wastech_orchestrator/core/flow/validator.py) ≈165).
- **`session_unavailable`** (потерян транскрипт/провайдер сбросил сессию): ретрай без resume (fresh), **не тратит** fix-итерацию. Отличается от quality-fail (тот тратит). Новый error-подкласс или флаг в router-обработке.
- **Lineage переживает рестарт**: editing_lineage регидратируется до вызова провайдера (recovery, [happy-path.md](happy-path.md) §6). Evaluator её **не перетирает** (fresh_disposable).

### Тесты

Из [durable §minimum tests](../outdated/durable_sessions_and_fixing_affinity.md#minimum-tests):

- `test_claude_resume_uses_lineage_session` — `--resume` берёт session_id из lineage-стора.
- `test_codex_exec_resume_parses_thread_started` — Codex `exec resume <id>`, парс `thread.started`.
- `test_fixing_affinity_resumes_implementation_session` — fixing продолжает сессию implementation.
- `test_evaluator_fresh_disposable_does_not_touch_lineage` — evaluator не читает/не пишет editing_lineage.
- `test_session_unavailable_retries_without_resume_no_fix_iteration` — потеря сессии → fresh-ретрай, `fix_iterations` не растёт.
- `test_raw_session_id_redacted_in_artifacts` — raw session-id только в state.db; в логах/argv/артефактах — redacted.
- `test_lineage_survives_restart` — editing_lineage регидратируется на resume.
- `test_conflicting_provider_override_rejected_under_affinity`.

### Exit

Editing-lineage переживает рестарт, evaluator её не перетирает, affinity работает; raw session-id не утекает.

### Решения (зафиксировано 2026-06-17)

- **Codex `exec resume` — сначала верификация на реальном CLI.** P2.2 Codex-resume **гейтится** на проверке реального CLI-контракта (точная форма `codex exec resume <id>` vs флаг; формат события `thread.started`) **до** реализации; результат фиксируется fake-CLI фикстурой (skill `fake-cli`). Почему: durable-affinity `fixing → implementation` опирается на резюм; ошибка в контракте сломала бы ядровую возможность и дала бы ложно-зелёные фикстуры.

---

## P2.3 — review как evaluator-узел

`role=review`, блокирующие findings → `rework` → fixing. Подтвердить, что review полностью укладывается в evaluator-вердикт (не отдельная стадия). В P1.3 уже создана минимальная review-обёртка; P2.3 приводит её к полному evaluator-контракту (immutable-вердикты, findings-схема как у supervisor).

### Touchpoints

- [`core/flow/nodes/evaluator.py`](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py) — `role=review` использует тот же путь вердикта/findings, что supervisor; разница только в промпте (`role_file`) и бюджете (`loop: review_fix`).

### Тесты

- `test_review_blocking_to_fixing` — блокирующий review → fixing (loop review_fix).
- `test_review_clean_to_next` — чистый review → следующий узел (summary).
- `test_review_is_ordinary_evaluator` — review использует evaluator-механику, не спец-стадию.

### Exit

review — обычный evaluator, не отдельная стадия.

---

## P2.4 — Hybrid testing evaluator + mutation guard

`role=test_quality` перед `checks`, **неблокирующий** (исчерпание → continue); always-on commit-candidate mutation guard на узле `checks`; reuse `record_rework`; тесты пишет implementation-агент, не evaluator.

### Touchpoints

- [`core/flow/nodes/evaluator.py`](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py) — `role=test_quality`, `blocking: false` ([packaged implementation.yaml](../../../src/wastech_orchestrator/core/flow/packaged/implementation.yaml) ≈42, узел `testing_quality`, `when: config.hybrid_testing`).
- [`core/flow/nodes/checks.py`](../../../src/wastech_orchestrator/core/flow/nodes/checks.py) — always-on commit-candidate mutation guard как свойство узла `checks` (детект, что чек ничего не намусорил в рабочем дереве перед commit-кандидатом). Опирается на [`check_runner.py`](../../../src/wastech_orchestrator/check_runner.py) `CheckOutcome`.
- [`core/loop_control.py`](../../../src/wastech_orchestrator/core/loop_control.py) — reuse `record_rework` (P2.1).

### Поведение

- `test_quality` оценивает качество тестов, написанных implementation-агентом (не пишет тесты сам). Неблокирующий: исход `rework` ограничен `budget: 1`; на исчерпании flow идёт по `accept`-ребру (→ checks), **не** в manual.
- mutation guard — core-owned свойство `checks`, всегда включён; flow не объявляет и не отключает ([security-ceiling.md](security-ceiling.md) §3).

### Тесты

Из [hybrid §minimum tests](../outdated/hybrid_agent_testing.md#minimum-tests):

- `test_test_quality_non_blocking_exhaustion_continues` — исчерпание budget → continue к checks, не manual.
- `test_test_quality_rework_to_fixing` — блокирующий findings → fixing.
- `test_mutation_guard_always_on` — guard срабатывает на узле checks независимо от flow.
- `test_test_quality_does_not_write_tests` — evaluator read-only; тесты пишет implementation.

### Exit

Опциональный test-quality + always-on guard.

---

## P2.5 — Целевой `implementation.yaml` + тесты из спек

Полный граф ([flow-contract.md](flow-contract.md) §7, = packaged [implementation.yaml](../../../src/wastech_orchestrator/core/flow/packaged/implementation.yaml)) исполняется данными; тесты из спек трёх программ зелены.

### Touchpoints

- Packaged [implementation.yaml](../../../src/wastech_orchestrator/core/flow/packaged/implementation.yaml) — уже содержит целевой граф (supervise_impl/supervise_fix/testing_quality/summary-final_handoff). P2.5 — это интеграционный прогон всего графа через движок с реальными узлами P2.1–P2.4.
- Тест-сьют объединяет supervisor + durable + hybrid spec-тесты на одном flow.

### Тесты

- `test_target_implementation_full_graph` — целевой граф исполняется end-to-end (happy + rework-петли всех трёх триггеров + decomposition), как в [happy-path.md](happy-path.md) §4.
- Все spec-тесты P2.1–P2.4 зелены на packaged `implementation.yaml`.

### Exit

Целевой implementation исполняется данными; три программы растворены в узлах.

---

## Сквозной обзор зависимостей P2

```text
P1 (engine + evaluator(review) + node_runs)
   └─> P2.1 supervisor (record_rework, evaluations table, final_handoff)
        └─> P2.2 durable sessions (editing_lineage table, codex resume, affinity)
             ├─> P2.3 review→full evaluator
             └─> P2.4 hybrid test_quality + mutation guard
                  └─> P2.5 целевой implementation.yaml + spec-тесты
```

## Контракт выхода P2 → P3

- evaluator-примитив полон: supervisor/review/test_quality + final_handoff + immutable-вердикты + `record_rework`. P3 переиспользует тот же примитив для `verifier`/`critic` (research/audit) без новой механики.
- durable sessions включают `resume_own_lineage` — нужен research `critic` (P3.3).
- Целевой implementation доказан → P3 доказывает **обобщаемость** на не-implementation flow (research/audit).

## Пересечения для ревью (потенциальные противоречия)

- **Единый учёт rework.** `record_rework` (P2.1) и `enter_fixing` (P1.1, test/review-петли) должны делить **один** инкремент `fix_iterations`. Риск двойного счёта на ребре `supervise_fix → fixing` (supervisor) пересекается с `testing fail → fixing` (checks) в одной петле. Тест `test_record_rework_single_increment` это анкерит; при реализации свести оба пути к одному helper в `loop_control`.
- **session_scope evaluator.** Валидатор P0.3 уже запрещает `editing_lineage` для evaluator ([validator.py](../../../src/wastech_orchestrator/core/flow/validator.py) ≈215). P2.2 не должен это ослаблять — evaluator всегда `fresh_disposable`/`resume_own_lineage`.
