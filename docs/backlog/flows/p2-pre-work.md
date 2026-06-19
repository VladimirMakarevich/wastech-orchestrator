# Пред-работа к P2 (до старта второй фазы)

Статус: **backlog / инженерная спека (не запланировано к исполнению)** Дата: 2026-06-19 Владелец: Vladimir Makarevich

Набор задач, которые нужно закрыть **до** старта [P2](p2-implementation.md) (supervisor-слой → durable sessions → hybrid testing). Это уточнения flow-модели и распределения «ручек» между задачей / flow / конфигом — фундамент, на котором P2 наращивает узлы. Не путать с самим P2: здесь нет supervisor/durable/hybrid, только подготовка контракта.

Контекст — что уже сделано в этом направлении (2026-06-19, не входит в этот файл):

- Supervisor стал **константным слоем над flow** (не узлом); `supervise_impl`/`supervise_fix`/summary-узел убраны ([memory: supervisor-constant-layer], [flow-contract.md](flow-contract.md) §2.2).
- Глобальный `agents.skip_stages` убран (config v10); per-task `stages.<stage>.enabled: false` оставлен как санкционированное исключение ([memory: per-task-stage-skip-exception], [flow-contract.md](flow-contract.md) §10).

---

## Сводка приоритетов (финальная оценка до P2)

Все пункты ниже — **дизайн зафиксирован, код не начат** (если не указано иное). Детали — в соответствующих разделах.

| # | Задача | Сложность | Блокирует P2? |
| --- | --- | --- | --- |
| PRE.1 | `provider`/`model`/`effort` на узле flow | **High** | да — P2.2 durable провайдер-aware опирается на выбор |
| PRE.1a | config-aware валидация `provider ∈ agents.allowed` | **Medium** | часть PRE.1 (тянет вперёд отложенный [P4.2](p4-operator.md)) |
| PRE.2 | `auto_merge` task-wins | **Low** | нет — независимо |
| PRE.3 | сверка остатка per-task оверрайдов (`decompose`/`refined`/per-task `model`/`reasoning`/`agents`) | **Medium** | частично — нужна ясность контракта (можно отложить в P4) |
| PRE.4 | верификация Codex `exec resume` на реальном CLI | **Medium** | да — P2.2 affinity опирается на резюм; де-риск заранее |

### Открытые вопросы (уточнить до старта P2)

1. **`Stage`-enum и роутинг (под PRE.1).** Provider-на-узле пересекается со стадийно-ключённым роутингом — `Stage` сохранён до P4 ([memory: flow-engine-p1-build]). Тянем полный node-based routing сейчас (убрать `Stage`-маршрутизацию) **или** кладём `provider`-на-узле поверх `Stage` (минимальный путь), оставив удаление `Stage` на P4? Учесть: `Stage` ещё используется в резолвере skip-фактов (`config.<stage>_enabled` → `Stage(name)`, [orchestrator.py](../../../src/wastech_orchestrator/core/orchestrator.py) ≈1036).
2. **Судьба `config.yaml agents.routing`.** Если каждый узел задаёт `provider`, нужен ли `agents.routing` вообще? Предложение: оставить как дефолт при `provider: null` + источник fallback-цепочки. Зафиксировать.
3. **`git.auto_merge_allow_per_task` (под PRE.2).** Удалить ключ совсем или оставить как no-op до общей чистки? Config-bump в любом случае.
4. **Граница задачи (под PRE.3).** `decompose` → блок `decomposition:` во flow? `refined` → derived-вход (`derived.needs_refinement`)? per-task `model`/`reasoning`/`agents` — вычищать сейчас или отложить в P4? Что отложено — **явно пометить**, чтобы P2 не строился на двусмысленном контракте.
5. **Supervisor-слой (уточнить на старте P2.1, не блокирует пред-работу).** Запускается per-subtask или один раз в конце (взаимодействие с decomposition)? `summary` пишется всегда или остаётся config-тогл (`config.summary_enabled`)? Когда удаляем ставший мёртвым `evaluation_kind: final_handoff` из кода ([snapshot.py](../../../src/wastech_orchestrator/core/flow/snapshot.py)/validator)?

### Уже закрыто (не висит)

- Supervisor → константный слой над flow (решение); глобальный `skip_stages` убран **в коде** (config v10, сьют зелёный); per-task skip оставлен; доки синхронизированы.

---

## PRE.1 — Выбор провайдера/модели/effort на уровне узла flow

**Решение (2026-06-19):** каждый agent/evaluator-узел во flow YAML сам задаёт **кто** его исполняет (`provider: claude|codex`), **какая модель** (`model`) и **какой effort** (`reasoning`). Это убирает per-task `agents`-route оверрайд и переносит выбор провайдера со стадийно-ключённого `config.yaml agents.routing` **на узел**.

### Поведение

- Новое поле узла `provider` (агент/evaluator). `null` → дефолт-маршрут из `config.yaml` (обратная совместимость). Непустое → этот провайдер исполняет узел.
- `provider` валидируется против `agents.allowed` (`config.yaml`); неизвестный/не-allowed → фатально на загрузке/preflight.
- `model`/`reasoning` уже поля узла ([flow-contract.md](flow-contract.md) §2.1) — остаются; вместе с `provider` дают полную спецификацию «кто/модель/effort» на узел.
- Провайдер-fallback остаётся **только на инфраструктурные ошибки** (`binary_not_found`/`timeout`/`rate_limited`/…), никогда на провал качества — инвариант ядра не меняется.

### Touchpoints

- [`core/flow/schema.py`](../../../src/wastech_orchestrator/core/flow/schema.py) / [`snapshot.py`](../../../src/wastech_orchestrator/core/flow/snapshot.py) — поле `provider` в node-датаклассах + JSON-Schema (co-design `flow.schema.json`).
- [`core/flow/validator.py`](../../../src/wastech_orchestrator/core/flow/validator.py) — `provider ∈ agents.allowed` (требует config-aware валидации — сейчас отложена в [P4.2](p4-operator.md); PRE.1 тянет её часть вперёд, либо валидирует формат на загрузке + allowed-проверку на реестре/preflight).
- [`routing/router.py`](../../../src/wastech_orchestrator/routing/router.py) `resolve_route`, [`core/flow/wiring.py`](../../../src/wastech_orchestrator/core/flow/wiring.py) `build_stage_map` — выбор провайдера по полю узла, а не по `Stage`-ключу.
- [security-ceiling.md](security-ceiling.md) §3 — `provider` уже добавлен в allowlist (settable, ∈ `agents.allowed`).

### Открытый вопрос / зависимость

- **Пересечение с P4.** [memory: flow-engine-p1-build] фиксирует: `Stage`-enum **сохранён** для роутинга до P4 (роутер выбирает провайдера по `Stage`; полное удаление `Stage` + node-based routing — P4). Provider-на-узле — это и есть часть node-based routing. Решить: тянуть полное node-based routing вперёд (удалить `Stage`-маршрутизацию сейчас) **или** добавить `provider`-на-узле как дополнительный слой поверх `Stage` (минимальный путь), оставив удаление `Stage` на P4.

### Exit

Узел flow полностью задаёт исполнителя (`provider`/`model`/`reasoning`); per-task `agents`-route не нужен; fallback остаётся инфра-only.

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

---

## PRE.3 (связанное, к решению) — Сверка per-task оверрайдов дизайн↔код

Не отдельная новая задача, а фиксация существующего расхождения, которое PRE.1/PRE.2 частично закрывают. [index.md](index.md) §214 / [flow-contract.md](flow-contract.md) §10 декларируют «задача не патчит граф/параметры», но **код P1 всё ещё держит** на уровне задачи `model`/`reasoning`/`agents`-route/`refined`/`decompose`/`auto_merge` (`NormalizedTask`, [task/model.py](../../../src/wastech_orchestrator/task/model.py)).

Целевое распределение после PRE.1/PRE.2:

| Ручка | Целевой дом | Статус |
| --- | --- | --- |
| `provider`/`model`/`reasoning` | узел flow | PRE.1 |
| `auto_merge` | задача (task-wins) + config | PRE.2 |
| `stages.<>.enabled` | задача (санкц. исключение) | сделано (config v10) |
| `decompose` | блок `decomposition:` flow | к решению |
| `refined` | операционный вход → `derived.needs_refinement` | к решению |
| `agents`-route (per-task провайдер) | удаляется (→ узел, PRE.1) | PRE.1 |

Решить до P2: ландить ли остаток (`decompose`/`refined`/удаление per-task `agents`/`model`/`reasoning`) сейчас или отложить в P4 «операторская поверхность». Что отложено — явно пометить, чтобы P2 не строился на двусмысленном контракте.

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
