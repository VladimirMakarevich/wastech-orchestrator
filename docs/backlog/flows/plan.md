# Гранулярный план реализации flow-движка

Статус: **backlog / план (не запланировано к исполнению)** Дата: 2026-06-17 Владелец: Vladimir Makarevich

План реализации унификации из [index.md](index.md), контракта из [flow-contract.md](flow-contract.md) и потолка из [security-ceiling.md](security-ceiling.md). Адаптирует пять backlog-программ ([foundation](../outdated/workflow_execution_foundation.md), [supervisor](../outdated/supervisor_quality_gate.md), [durable sessions](../outdated/durable_sessions_and_fixing_affinity.md), [hybrid testing](../outdated/hybrid_agent_testing.md), [profiles](../outdated/task_workflow_profiles.md)) к новой модели.

## Объём v1 (зафиксирован)

v1 поставляет **всё**: движок + целевой `implementation` (с mandatory supervisor + durable sessions + hybrid testing) + `deep_research` + `security_audit` + операторская YAML-поверхность с фатальным валидатором (C). crew **не реализуется** (мультиагентность — узлами графа; см. [flow-contract.md](flow-contract.md) §2.1).

Фазы ниже — **порядок сборки по зависимостям внутри одного v1**, а не релизные гейты. «Целевой implementation» означает: конечное состояние v1 — полный целевой конвейер. При этом сборка проходит через **внутренний чекпоинт корректности движка** (P1.4): движок сначала воспроизводит текущее поведение как тест самого себя, и только потом P2 наслаивает целевые фичи. Паритетный `implementation.yaml` из P1.4 — тестовая фикстура, не поставляемый flow.

## Критический путь и принцип проверки

Критический путь: **P0 (контракты + валидатор) → P1 (движок)**. От них зависит всё. P2 (durable sessions) — на пути к P3 (critic с `resume_own_lineage`, affinity).

Анти-риск максимального объёма: палитру легко обнаружить неверной поздно (на P2/P3). Поэтому **co-design на бумаге в P0**: все три flow (`implementation`/`deep_research`/`security_audit`) расписываются как YAML до постройки движка; пробелы палитры всплывают до кода. Тест абстракции (index §11): три flow выражаются данными без доменного знания в движке.

Базы проверки корректности (golden-harness меняется по фазам, т.к. целевое поведение в коде ещё не существует):

- P1 — **существующие тесты** implementation анкерят неизменное ядро (git, checks, publish, decomposition, fix-петли) через движок;
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

co-design здесь: написать `implementation.yaml`, `deep_research.yaml`, `security_audit.yaml` как данные и прогнать через валидатор (исполнения ещё нет) — палитра проверена на бумаге.

## P1 — Движок + core-capability узлы (внутренний чекпоинт паритета)

Цель: движок исполняет граф; существующие капабилити ядра обёрнуты как узлы **без смены механики**; движок воспроизводит текущее поведение implementation.

- **P1.1 — Ядро движка** (адаптирует [flow-contract.md](flow-contract.md) §3). Исполнитель узлов; **engine-owned** применение переходов по рёбрам (узлы возвращают исход, не прыгают); bounded-loop бюджеты + единый `fix_iterations` + гарантия терминальности. Touchpoints: `core/flow/engine.py`; вытесняет `_drive`/диспетчер по статусам. Тесты: исход ∈ объявленных рёбер; исчерпание бюджета → `manual_action_required`. Exit: движок гоняет граф из снапшота.
- **P1.2 — Обобщение resume/checkpoint** (адаптирует [index.md](index.md) §6, durable §recovery). Чекпоинт = `completed_nodes + current_node + loop_counters + publish_operations`; lifecycle ужат до `pending→validated→running→(done|failed|manual)`; recovery доверяет снапшоту, не переразрешает. **Высокий риск** — опирается на существующую идемпотентность. Touchpoints: `state_store.py` (обобщить `stage_runs`→node-runs; сохранить `publish_operations`), `core/recovery.py`. Тесты: рестарт/`rerun --continue` по узлам; дедуп commit/push/PR. Exit: resume по произвольному графу идемпотентен.
- **P1.3 — Обёртки core-owned узлов** (без смены механики). `agent`→`providers`/`router`; `checks`→`CheckRunner`+discovery+approval; `hitl`→telegram+durable; `publish`→`git_manager`; decomposition-конструкция→текущая `core/decomposition.py`. Исполнение `agent`-узла **сохраняет** ядровую observability как сейчас: prompt_audit (рендеренный промпт + метаданные per-run в `logs/<task>/prompt-audit/`; global + per-task tri-state, task wins, без гейта), structured logging (logfmt/json + redaction-filter), heartbeat. Touchpoints: `core/flow/nodes/*.py` (тонкие адаптеры). Тесты: каждый узел даёт тот же результат, что прямой вызов; при `prompt_audit` включённом каждый `agent`-узел пишет запись аудита. Exit: капабилити вызываются через узлы.
- **P1.4 — Паритетный `implementation.yaml` + golden-harness**. Текущее поведение (без supervisor, in-memory сессии, review+checks как есть) как граф; **существующие тесты implementation зелены через движок**. Exit: движок воспроизводит текущий конвейер байт-в-байт на границе.
- **P1.5 — Удаление старого драйвера**. Снести `_drive`, реентри-диспетчер по статусам, `Stage`-как-конвейер, после того как P1.4 зелёный. Touchpoints: `core/orchestrator.py`, `core/state_machine.py`. Exit: одна модель исполнения; мёртвый код удалён.

## P2 — Целевые возможности implementation как узлы

Цель: на доказанном движке нарастить целевой implementation. Адаптирует три программы.

- **P2.1 — Supervisor как evaluator-узел** (адаптирует [supervisor](../outdated/supervisor_quality_gate.md)). Ландит **первым** в P2 (канонический порядок: supervisor стартует на ad-hoc fresh-запросах поверх in-memory editing-lineage из P1; durable формализует scopes/affinity вокруг уже существующего supervisor). `role=supervisor`, `fresh_disposable`, вердикт `accept|rework` + findings (блокировка по `medium/high`); `record_rework(...)` — единый путь учёта (никогда не двойной инкремент); `final_handoff` заменяет summary-провайдер (ядро пишет `summary.{md,json}` или fallback); recovery по `source_stage_run_id` в `implementing`/`fixing`. **Без** привилегированного `core/supervisor.py` — это узел. Touchpoints: `core/flow/nodes/evaluator.py`, `core/loop_control.py` (`record_rework`), `state_store.py` (immutable evaluations). Тесты: [supervisor §minimum tests](../outdated/supervisor_quality_gate.md#minimum-tests). Exit: mandatory supervisor после implementation/fixing работает как узел; нет отдельного summary-провайдера.
- **P2.2 — Durable sessions (ядровая возможность)** (адаптирует [durable sessions](../outdated/durable_sessions_and_fixing_affinity.md) целиком). Ландит **после** supervisor. Нормализованный lineage-стор (`EditingLineage` per `execution_unit`); resume для Claude (`--resume`) и Codex (`exec resume <id>`, парс `thread.started`); провайдер-aware per-attempt request; redaction raw session-id (только в `state.db`); `session_scope` (`editing_lineage`/`fresh_disposable`/`resume_own_lineage`); affinity `fixing.lineage_affinity → implementation`; `session_unavailable` → ретрай без resume (не тратит fix-итерацию). Touchpoints: `providers/codex.py`, `providers/claude.py`, `routing/router.py`, `state_store.py` (lineage-таблица), `providers/redaction.py`. Тесты: из [durable §minimum tests](../outdated/durable_sessions_and_fixing_affinity.md#minimum-tests). Exit: editing-lineage переживает рестарт, evaluator её не перетирает, affinity работает.
- **P2.3 — review как evaluator-узел** (адаптирует [flow-contract.md](flow-contract.md) §2.2). `role=review`, блокирующие findings → `rework`→fixing. Подтвердить, что review полностью укладывается в evaluator-вердикт. Touchpoints: `core/flow/nodes/evaluator.py`. Тесты: блокирующий review → fixing; чистый → summary. Exit: review — обычный evaluator, не отдельная стадия.
- **P2.4 — Hybrid testing evaluator + mutation guard** (адаптирует [hybrid testing](../outdated/hybrid_agent_testing.md)). `role=test_quality` перед `checks`, неблокирующий (исчерпание→continue); always-on commit-candidate mutation guard на узле `checks`; reuse `record_rework`; тесты пишет implementation-агент, не evaluator. Touchpoints: `core/flow/nodes/evaluator.py`, `core/flow/nodes/checks.py` (mutation guard), `core/loop_control.py`. Тесты: [hybrid §minimum tests](../outdated/hybrid_agent_testing.md#minimum-tests). Exit: опциональный test-quality + always-on guard.
- **P2.5 — Целевой `implementation.yaml` + тесты из спек**. Полный граф ([flow-contract.md](flow-contract.md) §7) собран; тесты из спек трёх программ. Exit: целевой implementation исполняется данными.

## P3 — Flows research + audit + их виды узлов

Цель: два не-implementation flow данными; доказательство обобщаемости. Адаптирует [profiles](../outdated/task_workflow_profiles.md).

- **P3.1 — Ядровые чекеры `checks`** (адаптирует profiles §7–§8). `citation` (детерминированный валидатор манифеста: path/line/snippet, `sources.json`, three outcomes verified/broken/uncheckable, без LLM); `dependency_scan` (argv-сканеры: pip-audit/osv-scanner/… со структурой finding). Touchpoints: `core/flow/nodes/checks.py` (checker-набор), `checks/`. Тесты: hallucinated-цитата→broken, битый манифест→uncheckable без падения; сканер argv+timeout. Exit: `checks` обобщён на research/audit без LLM.
- **P3.2 — Политики output/publishing/network** (адаптирует profiles §7,§8,§12,§13; foundation §4). `repository_document`+`documentation_pull_request`; `private_control_workspace_report`+`none`; приватный отчёт под `<repo>/.worc/security-reports/<task-id>/` (fail-closed если config внутри repo); path-containment write-only в research-dir; `network_policy` для `external_research`; after-stage сравнение выхода с политикой. Touchpoints: `core/flow/nodes/publish.py`, output-guardrails, `git_manager.py` (documentation PR). Тесты: research пишет только в свою директорию; audit оставляет repo byte-for-byte; отчёт не попадает в staging/commit/PR. Exit: политики обеспечены ядром, flow их только выбирает.
- **P3.3 — `deep_research.yaml`** (адаптирует profiles §7; полный пример — [flow-contract.md](flow-contract.md) §8.1). `refinement→repository_analysis→external_research(opt)→synthesis→citation_check→fact_verification(verifier)→critical_review(critic, resume_own_lineage)→publish`; citation-loop pinned 1 (v1); bounded critic ping-pong; на исчерпании — публикация с Open questions, не `fail`. Тесты: [profiles §16 research](../outdated/task_workflow_profiles.md#16-testing-requirements). Exit: research-flow данными.
- **P3.4 — `security_audit.yaml`** (адаптирует profiles §8; полный пример — [flow-contract.md](flow-contract.md) §8.2). `scope→repository_analysis→dependency_scan→threat_analysis→finding_verification(verifier)→report→private_storage`; publishing `none`. Тесты: [profiles §16 audit](../outdated/task_workflow_profiles.md#16-testing-requirements). Exit: audit-flow данными; **co-design тест зелёный — абстракция доказана тремя примерами**.

## P4 — Операторская поверхность (валидатор C в бою)

Цель: оператор пишет свой flow; потолок держится.

- **P4.1 — Приём операторских flow**. Реестр/раскладка операторских flow, `task_type`-диспетчеризация в них (поверх P0.4). Touchpoints: `core/flow/registry.py`. Тесты: операторский flow резолвится и исполняется. Exit: кастомный flow запускается.
- **P4.2 — Полный валидатор C на боевом пути** (адаптирует [security-ceiling.md](security-ceiling.md) §4–§5,§7). Deny-list в бою; модель угроз как тесты; recovery-перепроверка потолка (security только сужается). Тесты: каждая угроза из [security-ceiling §1](security-ceiling.md#1-модель-угроз) отбита; потолок не пробивается данными/задачей. Exit: операторский flow не может расширить права или переопределить core-действие.
- **P4.3 — Docs + housekeeping**. Обновить configuration/how-it-works/функциональную карту/likec4/follow-ups; перенести поглощённые пять backlog-доков в `docs/backlog/outdated/` со ссылкой на `flows/*`. Exit: docs синхронны; старые доки помечены устаревшими.

---

## Карта адаптации пяти программ

| Backlog-программа | Куда уходит | Что меняется vs оригинал |
| --- | --- | --- |
| [foundation](../outdated/workflow_execution_foundation.md) | P0 (словарь, снапшот, диспетчеризация) | Контракты сохранены дословно; «registry одного профиля + `runner_kind` + не трогать state machine» **удалено** (заменено движком из данных) |
| [supervisor](../outdated/supervisor_quality_gate.md) | P2.1 | Становится `evaluator`-узлом; привилегированный `core/supervisor.py` **удалён**; «mandatory» = присутствие узла в `implementation.yaml`; `record_rework`/final_handoff сохранены |
| [durable sessions](../outdated/durable_sessions_and_fixing_affinity.md) | P2.2 | Почти целиком ядровая возможность; узлы цепляются через `session_scope`; affinity объявляется во flow |
| [hybrid testing](../outdated/hybrid_agent_testing.md) | P2.4 | `evaluator`-узел перед `checks`; mutation guard — свойство узла `checks`; машинерия почти не меняется |
| [profiles](../outdated/task_workflow_profiles.md) | P3 (+ P3.2 политики) | «3 захардкоженных профиля + `runner_kind`» **удалено**; три flow = данные; семантика → новые чекеры `checks` + политики + ядровые потолки |

## Что удаляется и когда

- P1.5: `_drive`, реентри-диспетчер по статусам, `Stage`-как-конвейер.
- P2.2: отдельный summary-провайдер (никогда не вводится — заменён final_handoff-узлом); привилегированный supervisor-компонент.
- P0.4/P4.1: фрейминг profile-registry/`runner_kind` (никогда не строится — заменён flow-реестром).
- P4.3: пять backlog-доков → `docs/backlog/outdated/`.

## Риски

- **Resume по произвольному графу + дедуп side-effect** (P1.2) — самое тонкое; опора на существующий `publish_operations`, иначе риск дублей commit/push/PR.
- **Нет чистого golden-harness для целевого поведения** (P2+) — митигация: существующие тесты анкерят ядро (P1), спек-тесты — новые узлы (P2), co-design-тест — абстракцию (P3).
- **Длинный критический путь** на движке+валидаторе при максимальном объёме — митигация: co-design всех трёх flow на бумаге в P0, чтобы пробелы палитры всплыли до кода.
- **Палитра окажется неверной** — митигация: тест «ноль доменного знания в движке»; обнаруженный спец-кейс → пересмотр палитры, а не патч движка.

## Решения и остаточная детализация

Развилки дизайна закрыты ([index.md](index.md) §12, [flow-contract.md](flow-contract.md) §10, [security-ceiling.md](security-ceiling.md) §8): палитра доказана; decomposition только implementation; config↔flow раскладка (встроенные запакованы, операторские в `.worc/flows/`, `config.yaml` = инфра+дефолты); per-task оверрайды графа убраны (remap-машинерия **не строится**); state-store = родовой node_run + feature-таблицы; доверие операторскому flow — файловое; route=ребро, review=role, единый `when`, network бинарный, dangerous-diff core-fixed.

Остаётся как деталь реализации (не блокирует P0):

- Граница P2.1↔P2.2: минимальная in-memory editing-lineage, которую несёт P1, чтобы supervisor-rework→fixing был корректен до durable.
- Точная JSON-Schema flow + полные правила фатального валидатора — фиксируются в P0.2/P0.3.

## Ближайшие шаги (kickoff P0)

Развёртка ближайшего фронта работ — порядок и артефакты. Блокеров нет (см. выше); начинаем с co-design на бумаге, затем P0-слайсы по зависимостям.

- **Шаг 0 — Co-design на бумаге (первый артефакт P0, де-рискует всё).** ✓ **Выполнено** (артефакты в [co-design/](co-design/); три flow валидны структурно+графово, валидатор ловит нарушения, схема generic — см. [co-design/notes.md](co-design/notes.md); находка: ключ ребра `on`→`outcome`). Записать три эталонных flow как данные (`implementation.yaml` целевой — [flow-contract.md](flow-contract.md) §7; `deep_research.yaml` / `security_audit.yaml` — §8.1/§8.2), составить черновую `flow.schema.json` и прогнать файлы через неё (без исполнения). Цель: подтвердить, что палитра выражает три flow без доменного знания в движке; зафиксировать форму `when`-предиката, decomposition-блока и набора полей узла. **Артефакты**: 3 × `*.yaml` + `flow.schema.json` + заметка о пробелах (если что-то не выражается данными → пересмотр контракта до кода). **Гейт**: все три валидны по схеме; спец-кейсов под implementation в схеме нет.
- **Шаг 1 — P0.1 словарь** (`core/flow/contracts.py`): `run_kind`/`role`/`session_scope`/`QualityAction`→lifecycle/`output_policy`/`publishing`/`execution_unit`/фингерпринты. Без потребителей. **Гейт**: типы + тесты маппинга `QualityAction`→статусы.
- **Шаг 2 — P0.2 схема + снапшот** (`core/flow/schema.py`, `snapshot.py`): формализовать YAML-схему из Шага 0, резолв в неизменяемый снапшот графа + `flow_fingerprint`, персист. **Гейт**: эталонный `implementation.yaml` грузится в стабильный снапшот.
- **Шаг 3 — P0.3 фатальный валидатор** (`core/flow/validator.py`): целостность графа (резолв рёбер, выбор ⊆ объявленного, достижимость, бюджет на каждом `rework`/`fail`, терминальность) + потолок (clamp профиля, `forbidden_args`, словари policy, path-containment) + allowlist полей (unknown → fail-closed); переиспользует `security/*`. **Гейт**: каждый класс нарушения → фатальный отказ до ветки; валидный flow проходит.
- **Шаг 4 — P0.4 диспетчеризация** (`core/flow/registry.py`): packaged встроенные + `.worc/flows/`; `task_type`→flow (дефолт implementation, unknown→fail); без per-task оверрайдов графа. **Гейт**: задача резолвится в валидированный снапшот; не патчит граф.
- **Выход P0**: три flow грузятся/валидируются/персистятся как снапшоты; ничего не исполняется; поведение implementation не тронуто. → P1 (движок исполнения + обёртки core-owned узлов, внутренний паритет).

Сквозная витрина для проверки, что все части складываются вместе, — happy-path прогон `implementation` от установки до публикации — в [happy-path.md](happy-path.md).
