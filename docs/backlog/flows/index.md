# Унификация исполнения: flow-движок (архитектура)

Статус: **backlog / архитектура зафиксирована, детальный дизайн в работе (не запланировано)** Дата: 2026-06-17 Владелец: Vladimir Makarevich

Документ фиксирует архитектуру большого рефакторинга: убрать хардкодную модель исполнения и заменить её **универсальным flow-движком**, где любой агентский flow описывается данными (YAML + Markdown), а ядро остаётся тонким и фиксированным. Это backlog-архитектура, а не текущее поведение. Ничто здесь не отменяет [CLAUDE.md](../../../CLAUDE.md), [AGENTS.md](../../../AGENTS.md) и твёрдые инварианты из [.agents/rules/](../../../.agents/rules/) — наоборот, §9 показывает, что именно они образуют неизменяемый «потолок», внутри которого живут flow.

Решение принято; этап investigate (разбор crewAI) завершён — результаты в приложении. Детализация вынесена в отдельные документы: **[flow-contract.md](flow-contract.md)** — палитра видов узлов и схема flow с эталонным `implementation.yaml`; **[security-ceiling.md](security-ceiling.md)** — валидатор «потолка» прав для операторских flow; гранулярный план реализации — в [plan.md](plan.md).

## 1. Решение и принцип

Сегодня оркестратор исполняет задачу через модель, зашитую в Python: фиксированный драйвер-цикл (`_drive` в `core/orchestrator.py`), отдельные функции на каждую стадию, императивные fix-петли и `Stage`-перечисление, описывающее конвейер implementation. Таблица переходов (`ALLOWED_TRANSITIONS` в `core/state_machine.py`) уже декларативна, но всё остальное — код. Это работает только для одного исхода — «реализовать изменение и опубликовать».

**Принятое решение — полная унификация с операторскими flow как целью v1.** Хардкодная модель исполнения удаляется целиком. На её месте — универсальный flow-движок, исполняющий **декларативный граф из данных**. implementation, deep_research, security_audit перестают быть привилегированными режимами и становятся обычными flow (YAML+MD), которые поставляются как примеры; оператор может описать **любой** свой flow теми же средствами, не меняя код ядра.

Принцип, на котором держится вся архитектура:

> **Тонкое фиксированное ядро + весь домен как данные.** Ядро владеет инфраструктурой (Git, мониторинг папки, state-store/resume, запуск CLI, security, HITL-транспорт, нотификации) и неизменяемо. Всё доменное (какие узлы, в каком порядке, с какими промптами/моделями/гейтами) — данные flow. Ни один flow не привилегирован.

«ЛЮБОЙ flow» уточняется до операционно-безопасной формулировки: **любой flow, собранный из типизированной палитры видов узлов и рёбер с ограниченными циклами, под потолком прав, валидируемый как граф до запуска** (§3–§5). Полностью произвольный flow (узел = произвольный код **в процессе ядра / inline**) сознательно не поддерживается — это ровно та дыра, через которую агентские фреймворки получают RCE/sandbox-escape (см. приложение). Палитра и есть граница между «гибко» и «небезопасно». Это **не** запрещает санкционированное расширение палитры: операторский tool как **типизированный** вид узла, исполняемый **out-of-process под тем же потолком, что и агенты** (subprocess, argv-без-shell, timeout, env-allowlist, side-effect-free), — спроектирован отдельной отложенной фазой ([p5-custom-tool-nodes.md](p5-custom-tool-nodes.md)), вне v1. Отвергается именно in-process произвольный код, не ceiling-bound внешний tool.

Ранее рассмотренные и отклонённые альтернативы — «дополнить рядом с дублированием обвязки» и «запечатать implementation + общая оболочка» — оставляли две внутренние модели исполнения и дублировали security-критичный код. Отклонены в пользу единой модели. Эта архитектура также **сознательно разворачивает** ключевое решение [workflow execution foundation](../outdated/workflow_execution_foundation.md) («не строить произвольный YAML-движок, держать один встроенный профиль») — обоснование разворота и снятие связанного риска см. §8 и §11.

## 2. Линия core/flow — главное решение

Всё держится на одной границе: что фиксировано в ядре, а что — данные. Из разбора кода она проводится резко.

| Остаётся в ядре (фиксировано, владелец — оркестратор) | Становится данными flow (YAML + MD) |
| --- | --- |
| Мониторинг папки задач, валидационный гейт, single-slot, очередь (`PENDING`) | Какие узлы есть в графе и в каком порядке |
| Git: ветка / commit / push / PR / merge, scoped staging, reset-to-base, защита base-ветки (`git_manager.py`) | Виды узлов (`agent`/`checks`/`evaluator`/`hitl`/`publish`) и рёбра (`accept`/`rework`/`route`) |
| State-store + resume + идемпотентность `publish_operations` (`state_store.py`, recovery) | Промпты, роли, цели, инструкции (вынесены в MD-файлы) |
| Запуск CLI: argv-без-shell, обязательный timeout, env-allowlist (`providers/process.py`) | `model` / `reasoning` / `session_scope` / `permission_profile` на узле (в пределах потолка) |
| Security: forbidden-args, isolation-preflight, redaction, dangerous-diff (`security/`, `core/dangerous_diff.py`) | Бюджеты rework на рёбрах, точки HITL, output/publishing/network-политика flow |
| Провайдеры + router (fallback только на инфраструктурные ошибки), HITL-транспорт (Telegram), нотификации | Где происходит decomposition (фан-аут над под-flow) и его параметры |

Смысл в одной фразе: «убрать state-машину» ≠ «жить без state-машины». Раз resume обязателен (§6), нужны валидированные переходы и персистентный чекпоинт — то есть state-машина, но **декларативная (данные из конфига)**, а не хардкодная (код). Гарантии сохраняются при трёх условиях: переходы исполняет ядро; граф валидируется заранее; движок умеет не-агентные виды узлов.

Полный пофункциональный инвентарь — что именно остаётся в ядре, что становится данными, и куда переходит каждый раздел текущего конфига — в **§14** (парити-чек против текущей реализации, чтобы ничего не потерять при рефакторе).

## 3. Инвариант безопасности (потолок прав)

Раз операторские flow появляются уже в v1, безопасность нельзя отложить. Организующий инвариант, который прибивается гвоздями:

> **Данные flow выбирают граф и параметры узлов _в пределах потолка_. Они никогда не могут расширить права (FS / сеть / публикация) и никогда не могут переопределить core-owned действие.**

Следствия:

- YAML может поставить узел `publish` и выбрать политику публикации (`pull_request` / `documentation_pull_request` / `none`), но механика commit/push/PR и её идемпотентность — в ядре; flow не описывает «как», только «какую политику».
- YAML может задать узлу `permission_profile`, но не выше того, что разрешил оператор в доверенном конфиге; не может добавить запрещённый флаг (`--dangerously*`, `--yolo`, `danger-full-access`); не может заставить flow закоммитить напрямую в base.
- Потолок уже существует и неослабляем в коде (`security/forbidden_args.py` в два слоя, профиль-маппинг в адаптерах, `security/isolation.py`, `security/env.py`). Операторский YAML проектируется как **allowlist разрешённых полей**, а не как открытый словарь.
- Валидатор движка проверяет потолок и целостность графа на этапе загрузки flow — **фатально, до любого запуска и до создания ветки**.

Точный перечень разрешённых полей YAML, схема валидации и модель угроз для операторских flow — в [security-ceiling.md](security-ceiling.md).

## 4. Виды узлов (палитра)

Движок обязан поддерживать типизированный набор видов узлов. Если узел умеет только «звать CLI-агента», то checks, HITL, dangerous-diff и публикация невыразимы. Палитра делится на **доменные** узлы (поведение конфигурируется данными) и **core-owned** узлы (ссылка на код ядра; flow выбирает политику, но не механику).

| Вид узла | Агент? | Класс | Что инкапсулирует |
| --- | --- | --- | --- |
| `agent` | да | доменный | запуск CLI codex/claude с моделью/reasoning и промптом (один агент на узел; мультиагентность — узлами графа, не crew) |
| `evaluator` | агент, read-only | доменный | review / critic / verifier / test_quality (in-flow) — общий примитив «оценка + bounded rework» (`accept`/`rework`), immutable-вердикты, бюджеты циклов. (Supervisor — не evaluator-узел, а константный слой над flow, §8.) |
| `checks` | нет | core-owned | детерминированный `CheckRunner` (pytest/ruff/…), discovery, гейт одобрения смены набора команд, commit-candidate mutation guard (core-owned, действует при наличии узла `checks`); exit-коды авторитетны |
| `hitl` | нет (человек) | core-owned (транспорт) | типизированный человеческий ввод/одобрение через Telegram; durable, переживает рестарт |
| `publish` / `git` | нет | core-owned | ветка/commit/push/PR/merge/возврат — идемпотентно, **исключительно силами оркестратора** |

Сквозные гейты, не являющиеся отдельными узлами, но обязательные: dangerous-diff guardrail (детерминированная классификация + HITL-одобрение); provider-routing с fallback **только на инфраструктурные ошибки** (провал качества → `rework`, не на другого провайдера).

Полное определение каждого вида узла (входы/выходы, политики, опции конфигурации) и схема flow — в [flow-contract.md](flow-contract.md).

## 5. Граф, рёбра, циклы

- **Рёбра объявлены в данных и валидируются заранее.** Все возможные рёбра графа перечислены во flow; движок проверяет резолв всех рёбер, что выбор роутера ⊆ объявленного набора, и достижимость — до запуска. Агент не «прыгает» по статусам сам.
- **Evaluator = bounded routing.** Узел-роутер объявляет набор исходов; evaluator выбирает **только из заранее объявленных оператором рёбер**: `accept` → следующий узел; `rework` → возврат на объявленный узел (с учётом бюджета); `route` → выбор между объявленными ветками. Это сохраняет инвариант «агент не меняет workflow молча»: каждое ребро объявлено и аудируется. (Константный supervisor-слой §8 не маршрутизирует — он терминален.)
- **Ограниченные циклы.** Рёбра `rework` несут бюджет; движок обязан гарантировать терминальность (исчерпание бюджета → детерминированный `manual_action_required`, не бесконечный цикл). Бюджеты per-edge/per-evaluator и операторски-конфигурируемые; единый глобальный счётчик (аналог нынешнего `fix_iterations` в `core/loop_control.py`) сохраняется как страховка.
- **Decomposition — конструкция движка, а не вид узла.** Это фан-аут/цикл над под-flow: на каждую подзадачу прогоняется один и тот же под-граф, с общим глобальным бюджетом и последовательными коммитами на одной ветке (как сейчас в `core/decomposition.py`). Движку нужны не только узлы+рёбра, но и явная семантика цикла/map над под-flow. Точная форма — в [flow-contract.md](flow-contract.md).

## 6. Resume и идемпотентность

Resume обязателен и в основном **уже существует** — это не строится с нуля, а обобщается.

- Сегодня resume — это dispatch-on-status по implementation-статусам (`_resume_task`). Обобщается до «текущий узел + завершённые узлы + per-node run-записи + счётчики циклов + `publish_operations`».
- Фиксированный lifecycle ужимается до родового: `pending → validated → running → (done | failed | manual_action_required)`. Прогресс по узлам графа — данные.
- **Дедуп побочных эффектов остаётся ядром.** `publish_operations` (фингерпринты + проверка состояния remote) уже даёт идемпотентность commit/push/PR — именно то, чего нет у crewAI (см. приложение, 12.3). Переиспользуется как механика узла `publish`.
- Recovery доверяет персистентному снапшоту **разрешённого графа** и его фингерпринту (аналог `profile_fingerprint` из foundation) и **никогда не переразрешает** flow из живого конфига; проверяет только целостность снапшота, существование вида узлов и что текущие security-возможности не требуют расширить сохранённый потолок.

## 7. Контракт flow (YAML + Markdown)

Оператор описывает flow декларативно: **структура — в YAML** (узлы, виды, рёбра, точки ветвления, модели/reasoning, политики), **длинные промпты/роли/цели/инструкции — в Markdown** (узел ссылается на `*_file`). Концептуально (точная схема — в [flow-contract.md](flow-contract.md)):

```yaml
flow:
  name: implementation
  task_type: implementation # диспетчеризация в точке входа
  output_policy: code_change
  publishing: pull_request
  permission_ceiling: workspace-write
  nodes:
    - id: implementation
      kind: agent
      role_file: roles/implementation.md
      session_scope: editing_lineage
      model: ...
      reasoning: ...
    - id: review
      kind: evaluator
      role: review
      role_file: roles/review.md
      session_scope: fresh_disposable
    - id: testing
      kind: checks
    - id: publish
      kind: publish
  edges:
    - { from: implementation, to: testing }
    - { from: testing, to: review, outcome: pass }
    - { from: testing, to: fixing, outcome: fail, loop: test_fix }
    - { from: review, to: publish, outcome: accept }
    - { from: review, to: fixing, outcome: rework, loop: review_fix }
    # …
```

Операторский flow регистрируется через `task_type`; диспетчеризация в точке входа переиспользует резолюцию foundation (`task_type → flow`), а не вводит новый механизм. Встроенные flow поставляются запакованными; операторские лежат в `.worc/flows/`; `config.yaml` ужимается до инфраструктуры + дефолтов провайдера (узел с `null` в `model`/`reasoning` падает в дефолт провайдера).

## 8. Как пять backlog-программ растворяются в движке

Это прямой ответ на требование «реализовать их в рамках рефактора, адаптировав идеи к текущим требованиям». Почти весь _смысл_ программ выживает; умирает _машинерия отдельных компонентов и хардкод-фрейминг_.

- **[workflow execution foundation](../outdated/workflow_execution_foundation.md)** — её **контракты становятся словарём движка**: `run_kind`/`role`, evaluator-primitive, `QualityAction`, `session_scope`, `ResolvedExecutionPolicy`, resolved-snapshot + fingerprint, `execution_unit`, порядок precedence прав, output-policy-словарь. Умирает её центральное решение — «built-in registry одного профиля + `runner_kind`-диспетчеризация + не трогать implementation state machine». Этот документ сознательно разворачивает её non-goal «no arbitrary YAML-defined workflow graph» (снятие риска — §11).
- **[supervisor quality-gate](../outdated/supervisor_quality_gate.md)** — становится **константным supervisor-слоем над flow** (решение 2026-06-19): не узел графа, а слой оркестратора, который всегда запускается поверх **любого** flow, пишет `summary` и делает терминальный advisory-контроль (не может `rework`/переоткрыть). Конфигурируется через `config.yaml` (`supervisor: { model, reasoning, role_file }`) под потолком. Отдельный привилегированный `core/supervisor.py` и блокирующие per-stage supervisor-узлы **умирают**; блокирующие пер-стейдж гейты, если нужны, = опциональные `review`/`test_quality`-узлы. Отдельного summary-провайдера/summary-узла нет — это слой. Детали — [flow-contract.md](flow-contract.md) §2.2, [p2-implementation.md](p2-implementation.md) §P2.1.
- **[durable sessions and fixing affinity](../outdated/durable_sessions_and_fixing_affinity.md)** — в основном **ядровая возможность** (lineage-стор, provider resume, redaction session-handle), выживает почти целиком в ядре. Узлы подключаются через `session_scope` (`editing_lineage`/`fresh_disposable`/`resume_own_lineage`). Affinity «fixing продолжает сессию implementation» = flow объявляет, что узел fixing делит lineage с узлом implementation, а ядро это гарантирует.
- **[hybrid agent testing](../outdated/hybrid_agent_testing.md)** — опциональный `evaluator`-узел (`role=test_quality`) перед узлом `checks`. Машинерия почти не умирает — он и так спроектирован как инстанс evaluator-примитива. Commit-candidate mutation guard остаётся **ядровым свойством** узла `checks` (действует при наличии `checks`; flow без `checks` его не имеет).
- **[task workflow profiles](../outdated/task_workflow_profiles.md)** — крупнейший передел. «Registry / `runner_kind` / три захардкоженных профиля» **умирает**. implementation / deep_research / security_audit становятся **тремя примерами YAML+MD flow**. Их семантика выживает как: (а) новые доменные/core виды узлов (детерминированный `citation_check`, `dependency_scan`, `private_report`); (б) per-flow политики output/publishing/network/permission; (в) ядровые потолки (приватный отчёт security_audit мимо git, read-only для research). Это главный источник требований к палитре узлов.

## 9. Твёрдые инварианты ядра (flow не может ослабить)

- Git (ветка/commit/push/PR/merge/возврат на base/pull) — **только оркестратор**; провайдеры и flow Git не трогают.
- Security-политику flow ослабить не может (sandbox, allowlist env, запрещённые флаги) — даже операторский flow ограничен «потолком» прав (§3).
- Провайдеры не знают про flow: ядро зовёт их через `AgentProvider` готовым `AgentRunRequest`.
- Single-slot (одна активная задача), полная аудируемость, отсутствие утечки секретов в логи/SQLite/артефакты.
- Telegram-HITL и periodic git task sync сохраняются.
- Запуск CLI без shell-интерполяции пользовательских строк (список аргументов).

## 10. Что удаляется (старые подходы)

Удаляется **хардкод-машинерия и фрейминг**, но **не требования** — требования переоткрываются как flow.

Удаляем:

- фиксированный драйвер-цикл `_drive` и реентри-диспетчеризацию по статусам;
- `Stage`-перечисление в роли описания конвейера; implementation-специфичную трактовку `ALLOWED_TRANSITIONS`;
- фрейминг profile-registry / `runner_kind` / отдельных runner'ов на профиль;
- привилегированный `core/supervisor.py` как отдельный компонент (становится видом узла);
- описание deep_research/security_audit как захардкоженных профилей.

Сохраняем как flow/узлы/ядровые возможности: implementation-конвейер, supervisor, durable sessions, hybrid testing, deep_research, security_audit — всё это выражается данными поверх движка и фиксированного ядра.

## 11. Co-design как доказательство абстракции

Foundation поднимал реальный риск: generic-движок до того, как существуют ≥2 реальных workflow, закодирует неверные предположения. Этот риск снимается **co-design**: проектировать `implementation` + `deep_research` + `security_audit` как конкретные flow **одновременно** с контрактом движка. Тогда абстракция доказана тремя примерами, а не одним.

Тест дизайна (критерий приёмки контракта): **все три примера-flow выражаются чистыми данными поверх минимальной палитры примитивов, без доменного знания в движке.** Если при описании, например, fixing-affinity у implementation приходится добавлять спец-кейс в движок — палитра примитивов неверна и пересматривается. Эталонный `implementation.yaml` как первое доказательство — в [flow-contract.md](flow-contract.md).

## 12. Зафиксированные решения

Развилки дизайна закрыты 2026-06-17; остаточная детализация (JSON-Schema, полные правила валидатора) — на этапе P0.

- **Палитра минимальна и доказана**: `agent`/`evaluator`/`checks`/`hitl`/`publish` + рёбра + decomposition выразили три flow данными без доменного знания в движке.
- **Decomposition — только implementation в v1**; research/audit линейны. Конструкция (под-flow = упорядоченный список node-id, общий глобальный бюджет, per-subtask commit на одной ветке) общая, но используется одним flow.
- **Граница config↔flow**: встроенные flow (`implementation`/`research`/`audit`) запакованы, операторские — в `.worc/flows/`, `config.yaml` = инфраструктура + дефолты провайдера (см. §7, §14.3).
- **Per-task оверрайды графа/узлов убраны** — с санкционированными исключениями: задача несёт идентичность/диспетчеризацию/операционные входы; вариация = другой flow (см. §14.2). Исключения: (1) per-task `stages.<stage>.enabled: false` (выключить заранее объявленный skippable-узел; ограниченный валидируемый тумблер, не патч графа; глобальный `agents.skip_stages` убран в config v10); (2) per-task `auto_merge` (резолвится и в задаче, и в `config.yaml`, **задача побеждает** — публикационная политика, не граф). Выбор провайдера/модели/effort переезжает **на узел flow** (`provider`/`model`/`reasoning`), не в задачу. См. [flow-contract.md](flow-contract.md) §10 + [p2-pre-work.md](p2-pre-work.md).
- **State-store**: родовой `node_run` + ядровые `tasks`/`provider_attempts`/`artifacts`/`publish_operations`/`flow_snapshot`; feature-таблицы (editing_lineage, evaluations, subtasks) — feature-owned.
- **research/audit гейты**: `citation_check`/`dependency_scan` — ядровые детерминированные чекеры вида `checks`; `fact_verification`/`finding_verification`/`critical_review` — обычные `evaluator`-узлы.
- **Прочее** (route как метка ребра, review как `role`, единый предикат `when`, файловое доверие операторскому flow, бинарный `network_policy`, dangerous-diff core-fixed) — [flow-contract.md](flow-contract.md) §10, [security-ceiling.md](security-ceiling.md) §8.

## 13. Связанные документы

- **[flow-contract.md](flow-contract.md)** (B) — палитра видов узлов, схема flow YAML+MD, эталонный `implementation.yaml`, форма decomposition.
- **[security-ceiling.md](security-ceiling.md)** (C) — потолок прав операторских flow, allowlist полей, фатальный валидатор, модель угроз.
- **[plan.md](plan.md)** — гранулярный план реализации, учитывающий все условия и адаптацию пяти программ.
- Адаптируемые программы: [foundation](../outdated/workflow_execution_foundation.md), [supervisor](../outdated/supervisor_quality_gate.md), [durable sessions](../outdated/durable_sessions_and_fixing_affinity.md), [hybrid testing](../outdated/hybrid_agent_testing.md), [task workflow profiles](../outdated/task_workflow_profiles.md).

## 14. Парити-инвентарь: текущая реализация → новая модель

Полная сверка против текущего кода (функциональная карта `docs/functional/`, [configuration.md](../../configuration.md), CLI) — гарантия, что ни одна функция/конфиг/нюанс не теряется при переходе на flow-модель. Метки назначения: **[ядро]** сохраняется неизменным; **[flow]** становится данными; **[узел]** — вид узла; **[потолок]** — security-ceiling ([security-ceiling.md](security-ceiling.md)). Механики — в [flow-contract.md](flow-contract.md). Примечание: supervisor / durable sessions / hybrid testing — **не** текущая реализация, а добавляемые из поглощённых программ (§8); здесь инвентаризируется то, что в коде сейчас.

### 14.1. Сохраняемое ядро (неизменяемо)

- **Интейк** [ядро]: watch папки; валидационный гейт §19 (Phase A hard-reject: размер, строгий UTF-8, control-chars, обязательный непустой frontmatter, unknown-keys fail-closed, id-regex `^[a-z0-9][a-z0-9._-]{0,63}$`, injection-scan значений фронтматтера; Phase B completeness COMPLETE/NEEDS_ENRICHMENT — не отвергает); карантин в `.worc/tasks/rejected`; single-slot; очередь PENDING; duplicate-id из двух источников (state.db + ledger) с bypass для recovery-rerun.
- **CLI** [ядро]: `install` (детект провайдеров/чеков/PR/auto-mode, `--reconfigure`/`--dry-run`/`--skip-preflight`), `run`, `watch` (`--poll-seconds`, auto_mode, PID-guard, single-pass при poll≤0), `stop`/`restart` (SIGTERM→timeout→SIGKILL), `preflight`, `telegram-test`, `status`, `upgrade-config`, `upgrade-docs`, `install-templates`, `rerun` (`--continue`/`--force-reset-remote`/`--dry-run`/`-y`), `finalize` (`--as done|failed|abandoned`, `--pr-url`, `--note`, `--delete-branch`, `--no-verify-pr`). Exit-коды 0/1/2.
- **Watch-петля** [ядро]: на тике refresh_repo (fetch + pull base, best-effort) — periodic git task sync; auto_mode определяет подхват следующей pending-задачи когда слот свободен.
- **State-store** [ядро]: SQLite транзакционно (`BEGIN IMMEDIATE`→COMMIT/ROLLBACK), forward-only schema versioning (greenfield, без миграции данных); таблицы tasks/stage_runs/provider_attempts/check_runs/artifacts/publish_operations/subtasks → обобщаются в node-runs (§6).
- **Recovery/rerun/finalize** [ядро]: reconciler NONE/RESUME/CLEANUP/MANUAL; >1 активной → MANUAL; decomposed-реконсиляция по commit SHA на ветке (несоответствие → MANUAL); fresh rerun (archive `attempt-N`, reset-to-base, clear counters/subtasks/publish-ops) vs `--continue` (revive на `interrupted_status`, сохранить работу, reset pending HITL); finalize вне state-machine (без pipeline/commit, прямой set_status, перенос файла, ledger-запись).
- **Ledger** [ядро]: append-only `completed.jsonl` (id/title/status/branch/pr_url/auto_merged/merge_outcome/fix_iterations/decomposed/attempt/rerun_of/manual/note/validation_reason); источник dup-id и attempt-счёта.
- **worc-home** [ядро]: gitignored `<repo>/.worc/` (config.yaml, state.db, logs/, templates/, checks/-cache, tasks/rejected, orchestrator.pid); `tasks/{pending,processing,done,failed}` репо-трекаемые; перемещение task-файла + sidecar `<id>.summary.md`.
- **Артефакты** [ядро]: реестр с sha256, неизменяемая аллокация (`exist_ok=False`), per-attempt раскладка `stages/<stage>/[sub-NN/]run-<id>/<attempt>-<provider>/{request,stdout,stderr,events,result}`; архивация `attempt-N` на rerun; всё после redaction.
- **Git/publish** [ядро]: prepare_branch (fetch/checkout base/pull `--ff-only`/create `agent/<id>-<slug>`); code-commit (scoped staging, `:(exclude)tasks/`, **никогда** `git add .`); **task-scoped audit-commit** (только этот task + summary; опция sibling `<branch>-audit`); push (отказ пушить в base); PR (`gh`, body из summary); merge/auto_merge (strategy squash/merge/rebase, per-task gate, `wait_for_checks`=`--auto`); reset-to-base (+опц. delete remote/close PR); terminal cleanup только когда provably safe (unaccounted_dirty пуст); idempotency через `publish_operations` (фингерпринты + проверка remote, already-merged → idempotent success).
- **Провайдеры/router** [ядро]: `AgentProvider` (preflight/run), `AgentRunRequest`/`Result`; классификация ошибок; fallback **только инфра** (FALLBACK_ELIGIBLE; условный для authz/permission через profile-strictness; partial-diff passthrough без отката файлов; bounded `max_stage_attempts`, =1 блокирует fallback); argv-без-shell; обязательный timeout; env-allowlist.
- **Security (5 примитивов)** [ядро/потолок]: env-allowlist; forbidden-args (config + adapter, два слоя); injection-scan фронтматтера; isolation-preflight (до ветки); profile-strictness (fallback не слабее); denied_commands/denied_read_paths → CLI-deny (Claude `--disallowedTools`, Codex sandbox); redaction (3 слоя: extra_secrets из denied-read-paths, sensitive NAME=VALUE, token-паттерны GitHub/OpenAI/Slack/AWS/JWT/Bearer) на всех артефактах/логах/argv; dangerous-diff (deletion/dependency по basename-fnmatch) + HITL-одобрение.
- **HITL-транспорт (Telegram)** [ядро]: durable-интеракции (waiting/answered/consumed/transport_error), question/approval (inline-кнопки/ForceReply), polling getUpdates, timeout/transport fail-closed→manual, redaction, reset/consume на rerun/finalize.
- **Нотификации/observability** [ядро]: терминальные уведомления success/failure/manual (+NullNotifier при выключенном); структурное логирование logfmt/json + redaction-filter (последний барьер); heartbeat (elapsed) на долгих провайдер/check/git вызовах; prompt_audit (промпт+метаданные per-stage, global + per-task tri-state, без гейта).
- **Loop control** [ядро]: `test_fix_cycles`/`review_fix_cycles` (≤ `max_fix_cycles`, reset на pass), глобальный `fix_iterations` (≤ `max_total_fix_iterations`, **не** сбрасывается между подзадачами), исчерпание → manual + `failure_report.json`/`stuck.md`.

### 14.2. Становится данными flow

- **Стадии → узлы** [узел]: refinement/planning/implementation/fixing → `agent`; testing → `checks` (checker `command_profile`); review → `evaluator` (`role=review`); summary → **константный supervisor-слой** над flow (не узел; summary + advisory, §8); publishing → `publish`.
- **Маршрутизация** [flow]: per-stage `agents.routing` (primary+fallback) → провайдер/fallback на узле; per-task `route_override.<stage>` → override на узле (только из `agents.allowed`, коллизия с fallback меняет роли).
- **Модель/reasoning** [flow]: порядок резолюции `stages.<>.{model,reasoning}` → task-wide `model`/`reasoning` → provider-default — сохраняется как поля узла + дефолты.
- **Пропуск стадий** [flow + ядро]: per-task `stages.<>.enabled:false` (глобальный `agents.skip_stages` убран в config v10), `refined:true` → `when`-предикат на узле (`config.*_enabled`/`derived.needs_refinement`) → пропуск узла; аудит-трейл пропуска (`node_runs.skipped`) остаётся ядром.
- **Decomposition** [flow]: gate (config `decomposition.enabled` + per-task `decompose` tri-state; `max_subtasks`; `commit_per_subtask`; linear `depends_on`; reason-коды) → конструкция фан-аута (§5).
- **Политики** [flow]: output (`code_change`/`repository_document`/`private_control_workspace_report`), publishing (`pull_request`/`documentation_pull_request`/`none`), network — per-flow поля.
- **Промпты** [flow]: `prompts.{templates_dir,mode}` (append/replace; allowlisted-переменные — только пути/метаданные, не тело/diff/env/секреты) → role-MD узла + безопасная интерполяция.
- **Skills** [flow]: planning-selected repo skill references (`skills.{scan_root,exclude}`, gate-дублирующие исключаются) → контекст `agent`-узла.
- **Task-файл** [ядро]: несёт не-flow поля — `id`/`title`, `task_type` (диспетчеризация в flow), `contacts` (нотификации), `prompt_audit` (тогл аудита), опц. `pr_title`. Целевое распределение остальных ручек (решения 2026-06-19):
  - `provider`/`model`/`reasoning` (кто/модель/effort) → **поля узла flow** (не задача, не стадийно-ключённый `agents.routing`); `provider` ∈ `agents.allowed`.
  - `decompose` → блок `decomposition:` во flow; `refined` → операционный вход, питающий `derived.needs_refinement`.
  - **Санкционированные task-level исключения:** `stages.<stage>.enabled: false` (skip skippable-узла; flow-contract §10) и `auto_merge` (резолвится в задаче **и** в `config.yaml`, **задача побеждает**). Глобальный `agents.skip_stages` убран в config v10.
  - **Статус: дизайн опережает код.** P1 всё ещё держит `model`/`reasoning`/`agents`-route/`refined`/`decompose`/`auto_merge` на уровне задачи; перенос (provider-на-узел, auto_merge task-wins) — пред-работа к P2: [p2-pre-work.md](p2-pre-work.md).

### 14.3. Карта текущего конфига

| Раздел конфига | Куда переходит |
| --- | --- |
| `repo.*`, `orchestrator.{auto_mode,poll_interval_seconds}` | [ядро] |
| `agents.{allowed,max_stage_attempts,max_fix_cycles,max_total_fix_iterations}` | [ядро] (router/loop) + [flow] (бюджеты рёбер) |
| `agents.routing.*` | [flow] (per-node провайдер+fallback) |
| `agents.providers.<p>.{command,timeout_seconds,sandbox,permission_profile,extra_args,max_turns,max_budget_usd}` | [ядро]/[потолок] |
| `agents.providers.<p>.{model,reasoning}` | [flow] (дефолт, переопределяемый узлом) |
| `agents.allow_review_skip` (гейт per-task review-skip; глобальный `skip_stages` убран v10) | [flow] (`when` на узле) + [ядро] (гейт) |
| `agents.decomposition.*` | [flow] (конструкция decomposition) |
| per-task оверрайды (`model`/`reasoning`/`stages.*`/`agents`-route/`refined`/`decompose`/`auto_merge`) | **убрано** — flow источник; задача только `id`/`title`/`task_type`/`contacts`/`prompt_audit` |
| `security.*` | [потолок] |
| `validation.*` | [ядро] (интейк) |
| `checks.*` (discovery / commands / approve_command_changes) | [ядро] (узел `checks`) |
| `git.{create_pull_request,pr_base,footprint.*,auto_merge*}` | [ядро] (publish) + [flow] (publishing-политика) |
| `telegram.*` | [ядро] (HITL/notify) |
| `prompts.*` | [flow] (role-MD) |
| `skills.*` | [flow] (контекст agent) |
| `prompt_audit`, `schema_version` | [ядро] (observability / версионирование) |

Ничего из текущего не теряется: каждый пункт либо сохраняется в ядре, либо выражается данными flow под неослабляемым потолком.

## Приложение. Investigate crewAI (референс дизайна)

Разбирали (документация Flows, concepts agents/tasks/crews, human-feedback-in-flows; кодовая база `lib/crewai/src/crewai/`, ветка `main`): event-driven Flow (`@start`/`@listen`/`@router`), state management/персист, YAML-модель Crew/Agent/Task, детерминизм/гейты/человеческий ввод.

**Главная находка.** В crewAI две модели Flow: императивный декораторный DSL и более новый сериализуемый статический контракт `FlowDefinition` (`schema: crewai.flow/v1`, `from_yaml`/`to_yaml`/`validate_contract()`), в который DSL компилируется. Вторая близка к нашей цели «декларативный граф из конфига»: граф методов с типизированными действиями узлов (`code`, `tool`, `expression`, `each`, и `crew` — вызов команды агентов как узел) и роутером, объявляющим исходы через `emit`. Это сильный **референс формы контракта**, но не библиотека для зависимости: модель исполнения crewAI (in-process LLM-вызовы, нет Git/security) нам не подходит — берём форму, не код.

**Берём как образец формы.** (1) Engine-owned dispatch — переходы исполняет движок (`runtime`, `_condition_satisfied`), пользовательский код только возвращает решение; это в точности наш инвариант «переходы за ядром». (2) Router `emit` = объявленные рёбра, возврат = выбор — ровно наша «точка ветвления», контракт «выбор ∈ `emit`» переносим. (3) `FlowDefinition` как сериализуемый граф + `validate_contract()` → диагностики + JSON-Schema — структурный шаблон. (4) Узел-как-вызов-команды (`call: "crew"`) — доказывает, что «запустить агентов» это объявляемый тип узла, не инлайн-код. (5) YAML-модель агентов/задач: именованные ключи + ссылки по имени (`agent: <key>`, `context: [task_a]`) + длинный текст на узле — совпадает с нашим «YAML-структура + Markdown-промпты через `*_file`». (6) Один скаляр модели на агента. (7) Интерфейс персистенции `FlowPersistence` (`save_state`/`load_state`) + схема SQLite — минимальный шов для stage-чекпоинта.

**Вдохновляемся.** Паттерн «pending → persist → вернуть управление → resume по id» из human-feedback — концептуально наш durable Telegram-HITL. Чекпоинт-система Flow (`from_checkpoint`, `_completed_methods` → skip-and-dispatch) — правильная ментальная модель пошагового re-entry. Иерархический менеджер как роль supervisor (берём роль, не реализацию). `{variable}`-интерполяция из `inputs` — для инъекции `task_id`/repo в промпты. Function-guardrail контракт `(bool, payload)` — чистый детерминированный пост-валидатор.

**Не копируем (конфликт с инвариантами).** (1) `@persist`/`SQLiteFlowPersistence` восстанавливает только state и перезапускает с `@start`: `_restore_state` не заполняет `_completed_methods` → side-effects повторятся. Наш resume = durable stage-чекпоинт + восстановление `completed_stages` + **свои маркеры дедупликации** commit/scan/notify (`publish_operations`) — этого у crewAI нет. (2) `validate_contract()` намеренно поверхностный — нам нужен **строгий фатальный** валидатор (резолв рёбер, выбор ⊆ `emit`, достижимость). (3) Иерархический менеджер — неограниченный LLM-роутинг; у нас supervisor выбирает **только из объявленных рёбер**. (4) Crew-задача всегда агентная — не-агентный `checks` невыразим; наш смешанный конвейер строим сами. (5) Guardrail-ретрай возвращает провал тому же агенту — у нас провал тестов/ревью → `fixing`. (6) `human_input=True` — блокирующий `input()`, не durable; durable-HITL владеем сами. (7) **Ни одного нашего security-инварианта**: нет «Git только силами фреймворка», нет неослабляемого потолка (наоборот, `allow_code_execution`/`code_execution_mode="unsafe"` ослабляют — отсюда CVE на sandbox escape / RCE через prompt injection), нет argv-без-shell, нет env-allowlist. Всё это — наша зона; CVE-история crewAI — полезный каталог угроз. (8) Привязка структуры к Python-декораторам — мы хотим чисто декларативный YAML/MD без кода на узел. (9) Per-agent `temperature`/`reasoning` у crewAI не конфиг-поля — не прецедент; свои поля проектируем сами.

**Выводы для дизайна.** (1) Свой сериализуемый `FlowDefinition`-аналог (YAML+JSON-Schema) со строгим фатальным валидатором; переходы исполняет ядро. (2) Узел-роутер объявляет исходы; evaluator возвращает один из набора — bounded routing. (3) Типизированный набор видов узлов, минимум `agent`/`evaluator`/`checks`/`hitl`/`publish`. (4) Resume = durable stage-чекпоинт + восстановление `completed_stages` + свои маркеры дедупликации; не полагаемся на `@persist`. (5) HITL: pending/persist/resume-by-id на наш durable Telegram, персистенцию владеем сами. (6) Контракт агентов/задач: именованные ключи + ссылки по имени + длинный текст через `*_file`; модель/reasoning — свои поля. (7) Security — полностью наша зона; crewAI здесь только каталог того, чего **не** делать.
