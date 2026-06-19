# Пред-работа к P2 (до старта второй фазы)

Статус: **backlog / инженерная спека (не запланировано к исполнению)** Дата: 2026-06-19 Владелец: Vladimir Makarevich

Набор задач, которые нужно закрыть **до** старта [P2](p2-implementation.md) (supervisor-слой → durable sessions → hybrid testing). Это уточнения flow-модели и распределения «ручек» между задачей / flow / конфигом — фундамент, на котором P2 наращивает узлы. Не путать с самим P2: здесь нет supervisor/durable/hybrid, только подготовка контракта.

Контекст — что уже сделано в этом направлении (2026-06-19, не входит в этот файл):

- Supervisor стал **константным слоем над flow** (не узлом); `supervise_impl`/`supervise_fix`/summary-узел убраны ([memory: supervisor-constant-layer], [flow-contract.md](flow-contract.md) §2.2).
- Глобальный `agents.skip_stages` убран (config v10); per-task `stages.<stage>.enabled: false` оставлен как санкционированное исключение ([memory: per-task-stage-skip-exception], [flow-contract.md](flow-contract.md) §10).

---

## Сводка приоритетов (финальная оценка до P2)

**Статус (2026-06-19): PRE.1 / PRE.1a / PRE.2 / PRE.3 — РЕАЛИЗОВАНЫ в коде** (config schema v11; node-based routing; `providers.<p>.primary`; чистая задача; auto_merge task-wins; сьют зелёный, ruff+mypy чистые). PRE.4 — верификация на реальном CLI (Codex доступен локально, `codex-cli 0.139.0`); остаётся ручной де-риск перед P2.2. Детали — в соответствующих разделах.

| # | Задача | Сложность | Блокирует P2? | Статус |
| --- | --- | --- | --- | --- |
| PRE.1 | `provider`/`model`/`effort` на узле flow | **High** | да — P2.2 durable провайдер-aware опирается на выбор | ✅ реализовано |
| PRE.1a | config-aware валидация `provider ∈ agents.allowed` | **Medium** | часть PRE.1 (тянет вперёд отложенный [P4.2](p4-operator.md)) | ✅ реализовано (preflight `_check_flow_providers`) |
| PRE.2 | `auto_merge` task-wins | **Low** | нет — независимо | ✅ реализовано |
| PRE.3 | чистая задача: убрать остаток per-task оверрайдов (`decompose`/`refined`/per-task `model`/`reasoning`/`agents`) | **Medium** | да — контракт «чистой задачи» под P2 | ✅ реализовано |
| PRE.4 | верификация Codex `exec resume` на реальном CLI | **Medium** | да — P2.2 affinity опирается на резюм; де-риск заранее | ⏳ ручная проверка (бинарь доступен) |

### Решения по вопросам контракта (зафиксировано 2026-06-19)

1. **`Stage`-enum (под PRE.1) — РЕШЕНО (2026-06-19):** роутинг (кто исполняет узел) становится **node-based сейчас** — следствие удаления `agents.routing` (вопрос 2): узел задаёт `provider`, иначе глобальный primary; стадийно-ключённого роутинга нет. Сам `Stage`-enum **остаётся только ради skip-фактов** (`config.<stage>_enabled` → `Stage(name)`, [orchestrator.py](../../../src/wastech_orchestrator/core/orchestrator.py) ≈1036) — его полное удаление + остаточная Stage-логика переносятся в **P4**. _Комментарий: трогаем только маршрутизацию (делаем node-based); сам enum — технический долг, дочищаем в P4, это не блокирует P2._
2. **Судьба `config.yaml agents.routing` — РЕШЕНО (2026-06-19):** `agents.routing` (стадийный primary/fallback) **удаляется**. Вместо неё в `config.yaml providers` ровно один провайдер помечается `primary: true` (валидатор: **строго один** глобальный primary ∈ `agents.allowed`). Узел с `provider: null` → глобальный primary; провайдер-fallback (инфра-ошибка) → тоже глобальный primary — **единственный** fallback-таргет, без per-stage цепочек (если падает сам primary → infra-error/manual).
3. **`git.auto_merge_allow_per_task` (под PRE.2) — РЕШЕНО (2026-06-19):** ключ **удаляется** из конфига. Глобальный `git.auto_merge` остаётся (включить auto-merge для всех задач инстанса); per-task `auto_merge` **побеждает** (PRE.2), поэтому отдельный гейт-разрешение не нужен. Config-bump.
4. **Граница задачи (под PRE.3) — РЕШЕНО (2026-06-19):** из задачи убирается **всё** — `decompose` (решает flow-`decomposition:`), `refined` (refinement-skip определяется completeness-классификацией, `derived.needs_refinement` без флага), per-task `model`/`reasoning`/`agents` (переезжают на узел, PRE.1). Задача несёт только идентичность/диспетчеризацию + два санкционированных исключения: `stages.<>.enabled` (skip) и `auto_merge` (task-wins). Это и есть «чистая задача» из [index.md](index.md) §214 — теперь реализуется, не только декларируется.
5. **Supervisor-слой — РЕШЕНО (2026-06-19):** модель — **постоянный слой-наблюдатель весь цикл задачи** (стартует на старте задачи, проверяет **каждый** завершённый шаг read-only через свою сессию `resume_own_lineage` ~1 вызов LLM/шаг, advisory — не блокирует; блокировка = in-flow `review`/`test_quality`). (a) **один** supervisor на весь цикл задачи **и всех подзадач**; `summary` + advise — только при закрытии **всей задачи**, не подзадачи. (b) `config.summary_enabled` **убирается**; summary пишется **всегда**. (c) мёртвый `evaluation_kind: final_handoff` удаляется **в P2.1**. _Будущее: когда добавим память оркестратора — supervisor становится владельцем/контролёром памяти, обогащает контексты конкретных узлов._ Полная модель — [flow-contract.md](flow-contract.md) §2.2 + [p2-implementation.md](p2-implementation.md) §P2.1.

### Уже закрыто (не висит)

- Supervisor → константный слой над flow (решение); глобальный `skip_stages` убран **в коде** (config v10, сьют зелёный); per-task skip оставлен; доки синхронизированы.

---

## PRE.1 — Выбор провайдера/модели/effort на уровне узла flow

**Решение (2026-06-19):** каждый agent/evaluator-узел во flow YAML сам задаёт **кто** его исполняет (`provider: claude|codex`), **какая модель** (`model`) и **какой effort** (`reasoning`). Это убирает per-task `agents`-route оверрайд и переносит выбор провайдера со стадийно-ключённого `config.yaml agents.routing` **на узел**.

### Поведение

- Новое поле узла `provider` (агент/evaluator). Непустое → этот провайдер исполняет узел. `null` → **глобальный primary** (`config.yaml providers.<p>.primary: true`, ровно один).
- `provider` валидируется против `agents.allowed` (`config.yaml`); неизвестный/не-allowed → фатально на загрузке/preflight.
- **`agents.routing` удаляется** (вопрос 2 решён): стадийного primary/fallback нет; дефолт и единственный fallback-таргет — глобальный primary.
- `model`/`reasoning` уже поля узла ([flow-contract.md](flow-contract.md) §2.1) — остаются; вместе с `provider` дают полную спецификацию «кто/модель/effort» на узел.
- Провайдер-fallback остаётся **только на инфраструктурные ошибки** (`binary_not_found`/`timeout`/`rate_limited`/…) и ведёт на глобальный primary; никогда на провал качества — инвариант ядра не меняется.

### Touchpoints

- [`core/flow/schema.py`](../../../src/wastech_orchestrator/core/flow/schema.py) / [`snapshot.py`](../../../src/wastech_orchestrator/core/flow/snapshot.py) — поле `provider` в node-датаклассах + JSON-Schema (co-design `flow.schema.json`).
- [`core/flow/validator.py`](../../../src/wastech_orchestrator/core/flow/validator.py) — `provider ∈ agents.allowed` (требует config-aware валидации — сейчас отложена в [P4.2](p4-operator.md); PRE.1 тянет её часть вперёд, либо валидирует формат на загрузке + allowed-проверку на реестре/preflight).
- [`routing/router.py`](../../../src/wastech_orchestrator/routing/router.py) `resolve_route`, [`core/flow/wiring.py`](../../../src/wastech_orchestrator/core/flow/wiring.py) `build_stage_map` — выбор провайдера по полю узла `provider`, иначе глобальный primary; стадийный роутинг убирается.
- [`config/schema.py`](../../../src/wastech_orchestrator/config/schema.py) / [`config/loader.py`](../../../src/wastech_orchestrator/config/loader.py) — **убрать `agents.routing`/`RouteConfig`**; добавить `providers.<p>.primary` (валидатор: **ровно один** primary ∈ `agents.allowed`). Config-bump.
- [security-ceiling.md](security-ceiling.md) §3 — `provider` уже добавлен в allowlist (settable, ∈ `agents.allowed`).

### Зависимость (уточнено вопросами 1–2)

- Provider-routing становится **node-based уже сейчас** (следствие удаления `agents.routing`): узел задаёт `provider`, иначе глобальный primary. `Stage`-enum остаётся **только** для skip-фактов до P4; полное удаление `Stage` и любой остаточной Stage-логики — P4.

### Exit

Узел flow полностью задаёт исполнителя (`provider`/`model`/`reasoning`); per-task `agents`-route не нужен; fallback остаётся инфра-only.

**Реализовано (2026-06-19):** поле `provider` на `AgentNode`/`EvaluatorNode` ([schema.py](../../../src/wastech_orchestrator/core/flow/schema.py) + snapshot parse + `flow.schema.json`); `RouteConfig`/`agents.routing` удалены (config v11, legacy-ключ tolerate+strip); `agents.providers.<p>.primary` + валидатор «ровно один primary ∈ allowed» ([validation.py](../../../src/wastech_orchestrator/config/validation.py)); `AgentRouter.resolve_route(stage, provider)` → primary=node.provider|global-primary, fallback=global-primary (или нет, если узел уже на нём); `RouteSource` = `config`|`flow_node`. PRE.1a: фатальный preflight `_check_flow_providers` (`provider ∈ agents.allowed`) до ветки. Stage-enum оставлен под skip-факты до P4.

---

## PRE.2 — `auto_merge`: и в задаче, и в конфиге; задача побеждает

**Решение (2026-06-19):** `auto_merge` резолвится на двух уровнях — `config.yaml git.auto_merge` (дефолт инстанса) и per-task `auto_merge`. **Если задано в задаче — побеждает значение из задачи** (и `true`, и `false`).

### Поведение

- Резолюция: `task.auto_merge` задан (`true`/`false`) → он и применяется; иначе → `config.git.auto_merge`.
- Это **снимает** нынешний операторский гейт `git.auto_merge_allow_per_task` (сейчас per-task `true` honored только при включённом гейте, [orchestrator.py](../../../src/wastech_orchestrator/core/orchestrator.py) ≈1760).

### Замечание (документировать, не блокировать)

Auto-merge обходит человеческое ревью PR — но это **workflow/publishing-политика, а не потолок sandbox/approvals**, и задача — доверенный вход уровня оператора (тот же владелец, что и `config.yaml`). Поэтому это **не** ослабление твёрдого security-инварианта: если оператор понимает, что задача мелкая, и доверяет системе, он вправе смержить без ручной проверки. **Возможность не блокируем.** Нынешний комментарий «задача не может выдать себе merge-права» ([orchestrator.py](../../../src/wastech_orchestrator/core/orchestrator.py) ≈1758) снимается вместе с гейтом `auto_merge_allow_per_task`.

Достаточно **doc-note** в [operations.md](../../operations.md): auto-merge пропускает человеческое ревью — ответственность за решение «мержить без ручной проверки» на операторе (он же автор задачи). Никакого фатального гейта.

### Touchpoints

- [`core/orchestrator.py`](../../../src/wastech_orchestrator/core/orchestrator.py) `_auto_merge_on` (≈1758–1764) — новая precedence «task-wins».
- [`config/schema.py`](../../../src/wastech_orchestrator/config/schema.py) — судьба `git.auto_merge_allow_per_task` (удалить / превратить в hard-ceiling). Config-bump при удалении.
- [`task/model.py`](../../../src/wastech_orchestrator/task/model.py) `auto_merge` — поле остаётся.
- [configuration.md](../../configuration.md) / [operations.md](../../operations.md) — обновить разделы auto-merge при ландинге кода.

### Exit

`auto_merge` задаётся в конфиге и в задаче; значение задачи побеждает; возможность не блокируется; в operations.md — doc-note про ответственность оператора.

**Реализовано (2026-06-19):** `git.auto_merge_allow_per_task` удалён (config v11, tolerate+strip); `_auto_merge_on` → `task.auto_merge` побеждает, иначе `config.git.auto_merge` ([orchestrator.py](../../../src/wastech_orchestrator/core/orchestrator.py)); doc-note добавлен в [operations.md](../../operations.md) и [configuration.md](../../configuration.md).

---

## PRE.3 — Чистая задача: убрать остаток per-task оверрайдов (РЕШЕНО 2026-06-19)

[index.md](index.md) §214 / [flow-contract.md](flow-contract.md) §10 декларировали «задача не патчит граф/параметры», но код P1 всё ещё держит на уровне задачи `model`/`reasoning`/`agents`-route/`refined`/`decompose` (`NormalizedTask`, [task/model.py](../../../src/wastech_orchestrator/task/model.py)). **Решение: убрать всё** — задача становится «чистой» (только идентичность/диспетчеризация + два исключения). Аспирация §214 теперь реализуется, а не только декларируется.

| Ручка | Целевой дом | Статус |
| --- | --- | --- |
| `provider`/`model`/`reasoning` | узел flow | убрать из задачи (PRE.1) |
| `agents`-route (per-task провайдер) | узел flow | убрать из задачи (PRE.1) |
| `decompose` | блок `decomposition:` flow (gate решает) | **убрать из задачи** |
| `refined` | нет — refinement-skip по completeness (`derived.needs_refinement` без флага) | **убрать из задачи** |
| `auto_merge` | задача (task-wins) + config | оставить (PRE.2) |
| `stages.<>.enabled` | задача (санкц. исключение) | оставить (config v10) |

Итог: задача несёт `id`/`title`/`contacts`/`prompt_audit`/`pr_title` + `stages.<>.enabled` + `auto_merge`. Touchpoints: `task/model.py` `NormalizedTask` (срезать поля), `task/validation_gate.py` (frontmatter-валидация), парсер, использования в `core/orchestrator.py`/роутинге, тесты, доки §214/§10. Ничего не откладываем в P4 — задача чистится здесь.

**Реализовано (2026-06-19):** `NormalizedTask` срезан до чистого набора; `StageParams` оставляет только `enabled`; `ALLOWED_TASK_KEYS` = `{id,title,pr_title,auto_merge,prompt_audit,contacts,stages}`; gate убрал route-override/model/reasoning/`refined`/`decompose` (reason `invalid_route_override` удалён); `parser.py` сериализация обновлена; `_decomposition_gate_on()` = `config.agents.decomposition.enabled`; `derived.needs_refinement` = «не COMPLETE» (completeness-only, без `task.refined`). `ROUTABLE_STAGES` удалён. Доки (worc/README, decision-guide, task-authoring, cookbook, configuration, functional-map) синхронизированы.

---

## PRE.4 — верификация Codex `exec resume` (де-риск для P2.2)

**Проверено на реальном CLI (2026-06-19, `codex-cli 0.139.0`):** подкоманда существует — `codex exec resume [SESSION_ID] [PROMPT]`. Резюмирует сессию по `SESSION_ID` (UUID/thread-name) либо `--last` (самая свежая), принимает follow-up `PROMPT` (или `-` из stdin), поддерживает `-c key=value`. Codex-адаптер уже извлекает `session_id` из событийного потока ([codex.py](../../../src/wastech_orchestrator/providers/codex.py) `parse_events`), так что P2.2 durable-affinity может его передать в `exec resume`.

**Важно для P2.2:** комментарий в адаптере «no --resume equivalent in the Codex CLI» ([codex.py](../../../src/wastech_orchestrator/providers/codex.py) ≈200) **устарел** — resume есть; проводку `session_scope=resume_own_lineage`/`lineage_affinity` через `codex exec resume` делает P2.2 (не входит в pre-work). Полноценный e2e-прогон (реальная сессия + резюм) требует аутентификации Codex и квоты — оставлен оператору как ручной шаг перед стартом P2.2.

---

## Порядок и связь с планом

```text
P1 (готово) → [supervisor-слой + skip-removal: готово 2026-06-19]
            → PRE.1 (provider-на-узле) ─┐
            → PRE.2 (auto_merge task-wins) ─┼─→ зафиксировать остаток PRE.3
                                            ┘
            → P2 (supervisor evaluator-примитив → durable → hybrid)
```

PRE.1 — самый весомый (тянет часть P4 node-based routing); PRE.2 — точечный + security-фиксация; PRE.3 — решение о границе задачи. После них контракт flow стабилен, и P2 наращивается без переезда полей.
