# Гранулярный план реализации flow-движка

Статус: **в работе (P0 + P1 частично реализованы)** Дата: 2026-06-17 Владелец: Vladimir Makarevich

Главная идея заключается в следующем: уйти от фиксированных stages в оркестраторе и сделать максимально гибкую систему, где можно было бы описать ЛЮБОЙ пользовательский flow - к примеру security audit, documentation review, building article, implement feature, deep research и так далее. Для каждого узла-агента должна быть возможностьуказывать промт, агента, effort и другие детали. И ВАЖНО сохранить все возможности старой системы, сделав их только гибче и адаптировать к этому глобальному рефакторингу.

> **ОБНОВЛЕНИЕ 2026-06-18 — golden-harness / паритет байт-в-байт с `_drive` ОТМЕНЁН.** Greenfield, прода нет, релиза не было ⇒ доказывать побайтовое совпадение со старым драйвером — оверинжиниринг (migration-машинерия для миграции, которой нет). **Цель не «совпасть с `_drive`», а «не потерять ни одной возможности, переписав их под Flow».** P1.4 = **прямой cutover** (движок становится драйвером в `run_task`), а гарантия «ничего не потеряли» — **адаптация существующего интеграционного тест-сьюта на движок** (каждая возможность из чек-листа = ≥1 зелёный сценарий). Legacy `_drive` удаляется в том же шаге (P1.4 + P1.5 сливаются — двух драйверов держать незачем). Где ниже сказано «golden-harness / двойной прогон / байт-в-байт» — читать в этом ключе; детали в [p1-step-b-wiring-draft.md](p1-step-b-wiring-draft.md). (Употребления «repo byte-for-byte» в audit-flow и «единый учёт / без двойного счёта `fix_iterations`» — это **другое** и в силе.)

План реализации унификации из [index.md](index.md), контракта из [flow-contract.md](flow-contract.md) и потолка из [security-ceiling.md](security-ceiling.md). Адаптирует пять backlog-программ ([foundation](../outdated/workflow_execution_foundation.md), [supervisor](../outdated/supervisor_quality_gate.md), [durable sessions](../outdated/durable_sessions_and_fixing_affinity.md), [hybrid testing](../outdated/hybrid_agent_testing.md), [profiles](../outdated/task_workflow_profiles.md)) к новой модели.

## Объём v1 (зафиксирован)

v1 поставляет **всё**: движок + целевой `implementation` (с константным supervisor-слоем + durable sessions + hybrid testing) + `deep_research` + `security_audit` + операторская YAML-поверхность с фатальным валидатором (C). crew **не реализуется** (мультиагентность — узлами графа; см. [flow-contract.md](flow-contract.md) §2.1).

Фазы ниже — **порядок сборки по зависимостям внутри одного v1**, а не релизные гейты. «Целевой implementation» означает: конечное состояние v1 — полный целевой конвейер. При этом сборка проходит через **внутренний чекпоинт корректности движка** (P1.4): движок сначала воспроизводит текущее поведение как тест самого себя, и только потом P2 наслаивает целевые фичи. Паритетный `implementation.yaml` из P1.4 — тестовая фикстура, не поставляемый flow.

## Критический путь и принцип проверки

Критический путь: **P0 (контракты + валидатор) → P1 (движок)**. От них зависит всё. P2 (durable sessions) — на пути к P3 (critic с `resume_own_lineage`, affinity).

Анти-риск максимального объёма: палитру легко обнаружить неверной поздно (на P2/P3). Поэтому **co-design на бумаге в P0**: все три flow (`implementation`/`deep_research`/`security_audit`) расписываются как YAML до постройки движка; пробелы палитры всплывают до кода. Тест абстракции (index §11): три flow выражаются данными без доменного знания в движке.

Базы проверки корректности (целевое поведение в коде ещё формируется):

- P1 — **существующий интеграционный тест-сьют** implementation, **адаптированный на движок**, гарантирует, что ни одна возможность не потеряна (git, checks, publish, decomposition, fix-петли, HITL, recovery, rerun); это **cutover**, а не сравнение со старым драйвером (greenfield — паритета байт-в-байт с `_drive` не требуется);
- P2 — **тесты из спек** трёх программ покрывают новые узлы (supervisor, durable, hybrid);
- P3 — **co-design тест трёх flow** — финальный гейт абстракции.

## Сквозные условия (на каждом срезе)

- Твёрдые инварианты ([index.md](index.md) §9): git только ядро; потолок неослабляем; провайдеры не знают про flow; single-slot; нет утечки секретов; argv-без-shell; Telegram-HITL и periodic git sync.
- Schema versioning: forward-only, greenfield — миграций нет (локальный `state.db` пересоздаётся). Один общий bump `state.db` при обобщении схемы (P1.2) и `config`/flow-схемы по необходимости.
- Аудит: `run_kind`/`role`/`execution_unit`/`flow_fingerprint`/`execution_policy_fingerprint` — на каждом run.
- Тесты + docs-sync в той же смене (`ruff`/`mypy`/`pytest`; функциональная карта, likec4, configuration, how-it-works, follow-ups).

---

## P0 — Контракты и валидатор (без исполнения, без смены поведения)

Цель: словарь, схема flow и фатальный валидатор существуют и тестируются на flow-файлах; ничего не исполняется. Адаптирует контрактный слой [foundation](../outdated/workflow_execution_foundation.md) + [security-ceiling.md](security-ceiling.md).

- **P0.1 — Провайдер-нейтральный словарь** (адаптирует foundation §3–§7). `run_kind ∈ {stage, evaluator}` + `role`; `session_scope`; `QualityAction` → lifecycle; `output_policy`/`publishing`; `execution_unit = (task_id, subtask_order)`; два фингерпринта. Touchpoints: новый `core/flow/contracts.py`. Тесты: `QualityAction`→статусы; `evaluator`-run не становится `Stage`. Exit: словарь доступен, без потребителей.
- **P0.2 — Схема flow + снапшот** (адаптирует [flow-contract.md](flow-contract.md) §1, §7, §9). YAML-схема (nodes/edges/budgets/policies/decomposition) + JSON-Schema + резолв в неизменяемый снапшот графа + `flow_fingerprint`. Touchpoints: `core/flow/schema.py`, `core/flow/snapshot.py`. Тесты: парс/резолв эталонного `implementation.yaml`; фингерпринт стабилен. Exit: flow-файл грузится в снапшот.
- **P0.3 — Фатальный валидатор** (адаптирует [security-ceiling.md](security-ceiling.md) §4). Целостность графа (резолв рёбер, выбор ⊆ объявленного, достижимость, бюджет на каждом `rework`/`fail`, нет бесконечного цикла) + потолок (clamp `permission_profile`, `forbidden_args`, словари policy, path-containment) + allowlist полей (unknown → fail-closed). Touchpoints: `core/flow/validator.py`; переиспользует `security/forbidden_args.py`, `security/isolation.py`, `security/env.py`. Тесты: каждый класс нарушения → фатальный отказ; валидный flow проходит. Exit: валидатор отвергает небезопасный/битый flow до ветки.
- **P0.4 — Диспетчеризация `task_type` → flow** (адаптирует foundation §1, profiles §2). Реестр flow: запакованные встроенные (`implementation`/`research`/`audit`) + операторские в `.worc/flows/`; `config.yaml` ужимается до инфраструктуры + дефолтов провайдера. `task_type` дефолтит в `implementation`, неизвестный → fail до side-effects. Per-task оверрайды графа/узлов **не строятся** — задача несёт только `id`/`title`/`task_type`/`contacts`/`prompt_audit`. Touchpoints: `core/flow/registry.py`, валидация задачи. Тесты: missing→implementation; unknown→fail; задача не выбирает flow из прозы; задача не патчит граф. Exit: задача резолвится в валидированный flow-снапшот, персистится с фингерпринтом.

- **P0.5 — Hardening (закрытие дыр дизайн↔код)** ✓ **Выполнено**. Ревью «выполненного» P0 выявило, что несколько зафиксированных в [security-ceiling.md](security-ceiling.md) §3–§4 и [co-design/notes.md](co-design/notes.md) требований не были реализованы при формализации JSON-Schema → Python. Закрыто: **fail-closed на неизвестных полях** во всех мэппингах ([snapshot.py](../../../src/wastech_orchestrator/core/flow/snapshot.py) `_reject_unknown` — центральный механизм потолка-как-allowlist, ранее лишние ключи молча игнорировались); валидация `checker` ∈ ядрового набора и `network_policy` ∈ нового `NetworkPolicy`-enum; обёртка ошибок enum в `FlowLoadError` (`_enum`, ранее сырой `ValueError`); namespace-проверка `when.fact` (`derived.`/`config.`); co-reachability терминала в [validator.py](../../../src/wastech_orchestrator/core/flow/validator.py). Тесты: `tests/core/test_flow_snapshot.py` (секция «fail-closed hardening (P0.5)»), `test_flow_validator.py::test_node_cannot_reach_terminal`. **Отложено** (зависит от шринкнутой схемы конфига): config-aware валидация ([security-ceiling.md](security-ceiling.md) §4 «Согласованность с config.yaml») — `validate_flow` пока config-free; реализуется в [P4.2](p4-operator.md).

co-design здесь: написать `implementation.yaml`, `deep_research.yaml`, `security_audit.yaml` как данные и прогнать через валидатор (исполнения ещё нет) — палитра проверена на бумаге.

Детальные инженерные спеки фаз вынесены в отдельные файлы (touchpoints с сигнатурами, новые типы/таблицы, тесты по именам, exit, открытые вопросы). Ниже — сводка и ссылки; единый источник по каждой фазе — её файл.

## P1 — Движок + core-capability узлы → [p1-engine.md](p1-engine.md)

Цель: движок исполняет граф; капабилити ядра обёрнуты как узлы **без смены механики**; движок принимает на себя текущее поведение implementation (cutover; существующий тест-сьют адаптируется на движок, без сравнения со старым драйвером).

- **P1.1** Ядро движка (`core/flow/engine.py`): engine-owned переходы, bounded-loop бюджеты, единый `fix_iterations`, терминальность.
- **P1.2** Обобщение resume/checkpoint (schema v4, `stage_runs`→`node_runs`, родовой lifecycle, recovery не переразрешает). **Высокий риск.**
- **P1.3** Обёртки core-owned узлов (тонкие адаптеры к `router`/`CheckRunner`/telegram/`git_manager`); observability сохранена. Сборка промпта (`role_file`→render→`AgentRunRequest`) пошагово по узлам — [context-assembly.md](context-assembly.md).
- **P1.4** Cutover: движок — драйвер в `run_task`; per-stage пост-обработка и decomposition fan-out выражены во Flow (data-driven); существующий интеграционный сьют адаптирован на движок (чек-лист возможностей вместо golden-harness).
- **P1.5** Удаление старого драйвера (`_drive`, реентри-диспетчер, гранулярные статусы, `stage_runs`, `Stage`-как-конвейер) — в том же cutover-шаге, что и P1.4 (двух драйверов не держим).

## Пред-работа к P2 (уточнения flow-контракта) → [p2-pre-work.md](p2-pre-work.md)

Закрыть **до** P2: provider/model/effort на уровне узла flow (PRE.1); `auto_merge` task-wins (PRE.2, + security-фиксация); сверка остатка per-task оверрайдов (PRE.3). Уже сделано в этом направлении (2026-06-19): supervisor → константный слой над flow; глобальный `agents.skip_stages` убран (config v10), per-task skip оставлен.

## P2 — Целевые возможности implementation как узлы → [p2-implementation.md](p2-implementation.md)

Цель: на доказанном движке нарастить целевой implementation; адаптировать три программы. Порядок: supervisor → durable → hybrid.

- **P2.1** Supervisor как **константный слой над flow** (config.yaml: model/effort/role_file; summary + терминальный advisory-контроль, не узел) + evaluator-примитив (`record_rework`, immutable `evaluations`) для in-flow `review`/`test_quality`.
- **P2.2** Durable sessions (lineage-стор, Codex `exec resume`, Claude `--resume`, объявляемая affinity — в impl-flow `fixing→implementation`, `session_unavailable`-путь).
- **P2.3** review как обычный конфигурируемый evaluator-узел (опционален, удаляем).
- **P2.4** Hybrid `test_quality` (опциональный, неблокирующий) + mutation guard на `checks` (действует при наличии узла `checks`).
- **P2.5** Целевой packaged `implementation.yaml` + тесты из спек трёх программ.

## P3 — Flows research + audit → [p3-research-audit.md](p3-research-audit.md) ✓ **Выполнено (2026-06-21)**

Цель: два не-implementation flow данными; co-design тест абстракции — финальный гейт. **Все четыре пункта реализованы; co-design тест зелёный** (`tests/core/test_flow_security_audit.py::test_codesign_all_three_flows_generic`). Плюс закрыт остаток P0.4: `task_type` на чистой задаче → диспетч flow в оркестраторе (unknown → `failed` до ветки).

- **P3.1** ✓ Ядровые чекеры `citation` / `dependency_scan` (без LLM) — `core/flow/checkers/`, диспетч по `ChecksNode.checker`.
- **P3.2** ✓ Политики output/publishing/network — `core/flow/output_policy.py` + after-stage write-containment guard (`AgentNodeRunner`) + private-report publish (fail-closed) + `network_policy` end-to-end (request → Codex/Claude sandbox; отсутствие = нет сети).
- **P3.3** ✓ `deep_research.yaml` исполняется (roles/research/\*, `resume_own_lineage` critic через `node_lineage` `state.db` v10, `config.external_research` из network-grant).
- **P3.4** ✓ `security_audit.yaml` исполняется (roles/audit/\*, private report, `publishing: none`, repo byte-for-byte); **co-design тест зелёный — абстракция доказана тремя flow**.

Отложено в P4 (записано в [follow_ups.md](../follow_ups.md), 2026-06-21): пер-узловые observability-пути (удаление `Stage`-enum), Codex network на read-only-узлах, durable-сессия supervisor через `node_lineage`.

## P4 — Операторская поверхность → [p4-operator.md](p4-operator.md)

Цель: оператор пишет свой flow; потолок держится.

- **P4.1** Приём операторских flow (подключить `FlowRegistry` к боевому пути; preflight валидирует все flow).
- **P4.2** Config-aware валидатор + модель угроз как тесты + recovery-перепроверка (закрывает отложенный из P0.5 пункт согласованности с `config.yaml`).
- **P4.3** Docs + housekeeping (functional map, likec4, configuration, follow-ups; пять программ → `outdated/`).

## P5 — Кастомные tool-узлы (ОТЛОЖЕНО, вне v1) → [p5-custom-tool-nodes.md](p5-custom-tool-nodes.md)

Цель: операторский Python/исполняемый tool как **типизированный** вид узла `tool`, исполняемый **out-of-place под потолком** (subprocess через `run_process`, side-effect-free, allowlisted-контекст — JSON stdin/stdout, как CC-hooks). **Не начинать до стабилизации P1–P4**; задача сейчас — лишь сохранить швы (диспетч движка по `kind` как реестр, per-kind таблицы валидатора, переиспользуемый сбор контекста), чтобы P5 «вставился» без переписывания. Не нарушает инвариант «нет узла-кода»: `tool` — ceiling-bound вид (как `agent` запускает CLI под потолком), а не in-process произвольный код.

---

## Карта адаптации пяти программ

| Backlog-программа | Куда уходит | Что меняется vs оригинал |
| --- | --- | --- |
| [foundation](../outdated/workflow_execution_foundation.md) | P0 (словарь, снапшот, диспетчеризация) | Контракты сохранены дословно; «registry одного профиля + `runner_kind` + не трогать state machine» **удалено** (заменено движком из данных) |
| [supervisor](../outdated/supervisor_quality_gate.md) | P2.1 | Становится **постоянным слоем-наблюдателем над flow** (оркестратор, config.yaml: model/effort/role_file): живёт весь цикл задачи, проверяет каждый шаг (своя `resume_own_lineage`, advisory), summary+advise при закрытии всей задачи; привилегированный `core/supervisor.py` и блокирующие per-stage supervisor-узлы **удалены**; `record_rework` сохранён для in-flow петель; блокирующие пер-стейдж гейты = опциональные `review`/`test_quality`-узлы |
| [durable sessions](../outdated/durable_sessions_and_fixing_affinity.md) | P2.2 | Почти целиком ядровая возможность; узлы цепляются через `session_scope`; affinity объявляется во flow |
| [hybrid testing](../outdated/hybrid_agent_testing.md) | P2.4 | `evaluator`-узел перед `checks`; mutation guard — свойство узла `checks`; машинерия почти не меняется |
| [profiles](../outdated/task_workflow_profiles.md) | P3 (+ P3.2 политики) | «3 захардкоженных профиля + `runner_kind`» **удалено**; три flow = данные; семантика → новые чекеры `checks` + политики + ядровые потолки |

## Что удаляется и когда

- P1.5: `_drive`, реентри-диспетчер по статусам, `Stage`-как-конвейер.
- P2.1: отдельный summary-провайдер (никогда не вводится — заменён константным supervisor-слоем); привилегированный supervisor-компонент; блокирующие per-stage supervisor-узлы (`supervise_impl`/`supervise_fix`) в графе не вводятся.
- P0.4/P4.1: фрейминг profile-registry/`runner_kind` (никогда не строится — заменён flow-реестром).
- P4.3: пять backlog-доков → `docs/backlog/outdated/`.

## Риски

- **Resume по произвольному графу + дедуп side-effect** (P1.2) — самое тонкое; опора на существующий `publish_operations`, иначе риск дублей commit/push/PR.
- **Потеря возможности при cutover** (нет dual-run-страховки) — митигация: адаптированный интеграционный сьют + явный чек-лист возможностей (каждая = ≥1 зелёный сценарий) как gate; спек-тесты — новые узлы (P2), co-design-тест — абстракцию (P3).
- **Длинный критический путь** на движке+валидаторе при максимальном объёме — митигация: co-design всех трёх flow на бумаге в P0, чтобы пробелы палитры всплыли до кода.
- **Палитра окажется неверной** — митигация: тест «ноль доменного знания в движке»; обнаруженный спец-кейс → пересмотр палитры, а не патч движка.

## Решения и остаточная детализация

Развилки дизайна закрыты ([index.md](index.md) §12, [flow-contract.md](flow-contract.md) §10, [security-ceiling.md](security-ceiling.md) §8): палитра доказана; decomposition только implementation; config↔flow раскладка (встроенные запакованы, операторские в `.worc/flows/`, `config.yaml` = инфра+дефолты); per-task оверрайды графа убраны (remap-машинерия **не строится**); state-store = родовой node_run + feature-таблицы; доверие операторскому flow — файловое; route=ребро, review=role, единый `when`, network бинарный, dangerous-diff core-fixed.

Остаётся как деталь реализации (не блокирует P0):

- Граница P2.1↔P2.2: минимальная in-memory editing-lineage, которую несёт P1, чтобы supervisor-rework→fixing был корректен до durable.
- Точная JSON-Schema flow + полные правила фатального валидатора — зафиксированы в P0.2/P0.3 (+ P0.5 hardening).
- **Config-aware валидация отложена в [P4.2](p4-operator.md)**: `validate_flow` config-free, пока нет шринкнутой схемы конфига; согласованность `model`/`reasoning`/провайдеров/`permission_ceiling`/бюджетов с `config.yaml` ([security-ceiling.md](security-ceiling.md) §4) добавляется на боевом пути реестра.
- **Соответствие flow-`budgets` ↔ config-cap** (`agents.max_*`): источник авторитетности фиксируется в [P1.1](p1-engine.md) (предложение: flow-бюджет ≤ config-страховка).

## Ближайшие шаги (kickoff P0)

Развёртка ближайшего фронта работ — порядок и артефакты. Блокеров нет (см. выше); начинаем с co-design на бумаге, затем P0-слайсы по зависимостям.

- **Шаг 0 — Co-design на бумаге (первый артефакт P0, де-рискует всё).** ✓ **Выполнено** (артефакты в [co-design/](co-design/); три flow валидны структурно+графово, валидатор ловит нарушения, схема generic — см. [co-design/notes.md](co-design/notes.md); находка: ключ ребра `on`→`outcome`). Записать три эталонных flow как данные (`implementation.yaml` целевой — [flow-contract.md](flow-contract.md) §7; `deep_research.yaml` / `security_audit.yaml` — §8.1/§8.2), составить черновую `flow.schema.json` и прогнать файлы через неё (без исполнения). Цель: подтвердить, что палитра выражает три flow без доменного знания в движке; зафиксировать форму `when`-предиката, decomposition-блока и набора полей узла. **Артефакты**: 3 × `*.yaml` + `flow.schema.json` + заметка о пробелах (если что-то не выражается данными → пересмотр контракта до кода). **Гейт**: все три валидны по схеме; спец-кейсов под implementation в схеме нет.
- **Шаг 1 — P0.1 словарь** (`core/flow/contracts.py`): ✓ **Выполнено** (модуль + `tests/core/test_flow_contracts.py`; ruff / mypy `src` / pytest — полный набор зелёный; находка: ребро `implementing→fixing` для `ENTER_FIXING` добавит supervisor в P2, сейчас FIXING достижим из testing/review). `run_kind`/`role`/`session_scope`/`QualityAction`→lifecycle/`output_policy`/`publishing`/`execution_unit`/фингерпринты. Без потребителей. **Гейт**: типы + тесты маппинга `QualityAction`→статусы.
- **Шаг 2 — P0.2 схема + снапшот** (`core/flow/schema.py`, `snapshot.py`): ✓ **Выполнено** (модули + `tests/core/test_flow_snapshot.py`, 30 тестов; ruff / mypy `src` / pytest полный набор зелёный; находка: в `implementation.yaml` 15 рёбер, не 14). Frozen-dataclasses для палитры узлов + `FlowDoc`; `load_flow(path) → FlowSnapshot` с `MappingProxyType` для `nodes_by_id`/`adjacency`/`budgets`; `flow_fingerprint` = SHA-256 над raw `flow:` dict (key-order independent). `FlowDefaults` применяются при резолве (evaluator-ноды получают явные значения из `defaults.evaluator`). Все три co-design flow (`implementation`, `deep_research`, `security_audit`) грузятся в снапшоты. **Гейт**: эталонный `implementation.yaml` грузится в стабильный снапшот.
- **Шаг 3 — P0.3 фатальный валидатор** (`core/flow/validator.py`): ✓ **Выполнено** (модуль + `tests/core/test_flow_validator.py`, 24 теста; ruff / mypy `src` / pytest полный набор зелёный). `Violation(category, message)` + `FlowValidationError(violations)` — коллект всех нарушений до выброса. Граф: резолв рёбер, outcome ⊆ allowed-per-kind (`{accept,rework}` для stage_output, `{None}` для final_handoff, `{pass,fail}` для checks, `{None}` для остальных; `route:*` всегда разрешён), unbounded rework/fail, named loops в budgets, single-entry + reachability, terminal. Потолок: evaluator всегда `read-only` и никогда `editing_lineage`; `permission_profile` ≤ `permission_ceiling`; `extra_args` → `find_forbidden_args`; `role_file` без traversal (`..`/абсолютный путь). Три co-design flow проходят без нарушений. **Гейт**: каждый класс нарушения → фатальный отказ до ветки; валидный flow проходит.
- **Шаг 4 — P0.4 диспетчеризация** (`core/flow/registry.py`): ✓ **Выполнено** (модуль + `src/wastech_orchestrator/core/flow/packaged/` (3 YAML) + `tests/core/test_flow_registry.py`, 17 тестов; ruff / mypy `src` / pytest полный набор зелёный). `FlowRegistry(operator_flows_dir)` с двумя слоями поиска: оператор→`<operator_flows_dir>/<task_type>.yaml`, fallback→`packaged/<task_type>.yaml`; `task_type=None` → `"implementation"`; unknown → `FlowResolutionError` до side-effects; несоответствие `task_type` в YAML ← имя файла → `FlowResolutionError`; `validate_flow` вызывается всегда перед возвратом. **Гейт**: задача резолвится в валидированный снапшот; не патчит граф.
- **Шаг 5 — P0.5 hardening** (`snapshot.py`, `validator.py`, `contracts.py`): ✓ **Выполнено** (fail-closed на неизвестных полях, `checker`/`network_policy`-enum, обёртка enum-ошибок, namespace `when.fact`, co-reachability; тесты в `test_flow_snapshot.py`/`test_flow_validator.py`; ruff / mypy `src` / pytest полный набор зелёный). Закрывает расхождения дизайн↔код, выявленные при ревью «выполненного» P0. **Гейт**: каждый класс нарушения отвергается; три flow проходят.
- **Выход P0**: три flow грузятся/валидируются/персистятся как снапшоты; неизвестные поля отвергаются fail-closed; ничего не исполняется; поведение implementation не тронуто. → **[P1](p1-engine.md)** (движок исполнения + обёртки core-owned узлов, внутренний паритет).

Сквозная витрина для проверки, что все части складываются вместе, — happy-path прогон `implementation` от установки до публикации — в [happy-path.md](happy-path.md).
