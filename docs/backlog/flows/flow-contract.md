# Контракт flow: палитра узлов и схема (B)

Статус: **backlog / детальный дизайн (не запланировано)** Дата: 2026-06-17 Владелец: Vladimir Makarevich

Документ задаёт технический контракт flow-движка: типизированную палитру видов узлов, схему flow (YAML-структура + Markdown-промпты), семантику рёбер/циклов/decomposition и эталонный `implementation.yaml`, доказывающий, что текущий implementation-конвейер выражается **чистыми данными без доменного знания в движке**. Опирается на архитектуру из [index.md](index.md); потолок прав и валидатор операторских flow — в [security-ceiling.md](security-ceiling.md).

Критерий приёмки контракта (тест абстракции из index.md §11): `implementation`, `deep_research`, `security_audit` выражаются поверх одной минимальной палитры; если для какого-то поведения приходится добавлять спец-кейс в движок — палитра неверна.

## 1. Модель flow

Flow — это **направленный граф узлов** с объявленными рёбрами и потолком прав. Структура — в YAML; длинные промпты/роли/цели/инструкции — в Markdown-файлах, на которые узлы ссылаются через `*_file`. Движок:

- загружает и **валидирует** flow до запуска (целостность графа + потолок — см. [security-ceiling.md](security-ceiling.md));
- резолвит снапшот графа, персистит его с фингерпринтом;
- исполняет узлы и **сам** применяет переходы по рёбрам (узлы возвращают факты/вердикты, но не прыгают по графу);
- ведёт чекпоинт (завершённые узлы + счётчики циклов + `publish_operations`) для resume.

Верхнеуровневые поля flow:

```yaml
flow:
  name: implementation # уникальное имя
  task_type: implementation # диспетчеризация в точке входа
  permission_ceiling: workspace-write # потолок прав (см. C); узлы не могут выше
  output_policy: code_change # что и куда можно писать (словарь foundation)
  publishing: pull_request # терминальная политика публикации flow
  network_policy: default # сетевой доступ узлов
  defaults: { ... } # необязательные дефолты по виду узла
  nodes: [...] # узлы графа
  edges: [...] # объявленные рёбра
  budgets: { ... } # бюджеты циклов и глобальная страховка
  decomposition: { ... } # необязательная конструкция фан-аута
```

`output_policy` — скаляр, резолвящийся в foundation-овский `ResolvedOutputPolicy` (`target_repository_writes` + `control_workspace_writes` + `allowed_path_policy`). `publishing` — единый профиль-уровневый скаляр (`pull_request` / `documentation_pull_request` / `local_artifact` / `private_control_workspace_report` / `none`). Оба перечислены в [foundation](../outdated/workflow_execution_foundation.md) §4; flow выбирает значение, ядро его обеспечивает.

## 2. Палитра видов узлов

Пять видов. **Доменные** (`agent`, `evaluator`) конфигурируются данными; **core-owned** (`checks`, `hitl`, `publish`) — ссылка на код ядра: flow выбирает политику, но не механику.

### 2.1. `agent` (доменный)

Запуск CLI-провайдера (codex/claude) с ролью-промптом. Соответствует нынешним стадиям-авторам (refinement, planning, implementation, fixing).

```yaml
- id: implementation
  kind: agent
  role_file: roles/implementation.md # промпт/роль/цель/инструкции (MD)
  session_scope: editing_lineage # editing_lineage | fresh_disposable | resume_own_lineage
  lineage_affinity: null # id узла, чью editing-сессию переиспользовать (см. §5)
  permission_profile: workspace-write # ≤ permission_ceiling (clamp в C)
  model: null # пусто = дефолт провайдера
  reasoning: null # low|medium|high|xhigh|max (как в configuration.md)
  timeout_seconds: 7200
  output_schema: null # необязательная схема structured_output
  optional: false # допускается детерминированный skip (как refinement)
  hitl: { allow_question: false, allow_approval: false } # типизированные HITL-запросы из узла
  extra_args: [] # валидируется против forbidden_args (C)
```

- **Входы** — только провайдер-нейтральные пути к артефактам (task/plan/diff/…); тело задачи не встраивается в argv. Маппится на нынешний `AgentRunRequest`.
- **Выходы** — `final_message`, `structured_output`, нормализованный `session_id` (raw только в state.db), артефакты stdout/events (после redaction).
- **crew не поддерживается** — мультиагентность выражается узлами графа (несколько `agent`/`evaluator`-узлов с рёбрами), а не несколькими агентами внутри одного узла: так у каждого агента свой permission-профиль, session-lineage и чекпоинт, и шаг остаётся resume-able. Возможный будущий параллелизм — явная map-конструкция движка (как decomposition), не «crew»-узел.
- **dangerous-diff** — после любого `agent`-узла с `workspace-write` ядро **автоматически** прогоняет детерминированную классификацию опасного diff и, при необходимости, HITL-одобрение (`core/dangerous_diff.py`). Это core-owned guard: flow его не объявляет и **не может отключить** (security). Конфигурируема только политика одобрения в пределах, разрешённых C.

### 2.2. `evaluator` (доменный, read-only)

Общий примитив «оценка + bounded rework» для **in-flow** оценщиков. Один механизм для review / critic / verifier / test_quality / operator-defined — различается только `role`, промптом, session-политикой и бюджетом. Никогда не пишет в workspace. (Финальный summary + терминальный контроль — **не** evaluator-узел, а константный supervisor-слой оркестратора; см. ниже и [p2-implementation.md](p2-implementation.md) §P2.1.)

```yaml
- id: review
  kind: evaluator
  role: review # review | critic | verifier | test_quality | <operator-defined>
  role_file: roles/review.md
  session_scope: fresh_disposable # fresh_disposable | resume_own_lineage (никогда не editing_lineage автора)
  permission_profile: read-only # фиксировано read-only для evaluator (C)
  blocking: true # false = неблокирующий (как hybrid testing): исчерпание → continue
  max_rework_per_stage: 1 # локальный бюджет (per-instance, операторски-конфигурируемый)
  model: null
  reasoning: low
```

- **Вердикт** — строго валидируемый: `{ verdict: accept|rework, findings: [{ severity, reason, paths }] }`. `rework` требует ≥1 `medium`/`high`. Низкие — advisory, не блокируют. `accept` не содержит блокирующих.
- **Immutable** — каждая оценка — неизменяемый артефакт, неймспейснутый по source-run; локальный лимит **выводится подсчётом** применённых вердиктов, без отдельного мутабельного счётчика.
- **role open-ended**: механика одинакова; набор `role` расширяем под flow (review — обычный evaluator-узел, а не отдельная стадия). Никакого привилегированного `core/supervisor.py`-узла в графе нет.
- **Supervisor — не evaluator-узел, а константный слой.** Финальный summary + лёгкий терминальный контроль выполняет supervisor-слой оркестратора **поверх любого flow** (всегда, даже для degenerate-графа из одного агента). Он терминален (не может `rework`/переоткрыть задачу), пишет `summary.{md,json}` (или детерминированный fallback) и advisory-findings; конфигурируется через `config.yaml` (`supervisor: { model, reasoning, role_file }`) под тем же потолком (read-only, allowlist, containment). Заменяет и старый summary-провайдер, и блокирующие per-stage supervisor-узлы. Детали — [p2-implementation.md](p2-implementation.md) §P2.1.

### 2.3. `checks` (core-owned, детерминированный)

Детерминированный верификатор, исполняемый ядром. exit-коды авторитетны. Обобщён за пределы pytest/ruff: «вид проверки» выбирается данными, но реализован в ядре.

```yaml
- id: testing
  kind: checks
  checker: command_profile # command_profile | citation | dependency_scan (ядровые чекеры)
  discovery: { mode: agent_assisted, approve_command_changes: true } # как сейчас
```

- `command_profile` — нынешний `CheckRunner` + discovery + гейт одобрения смены набора команд (HITL) + commit-candidate mutation guard (core-owned). Guard действует, **пока в графе есть узел `checks`**, и flow его не отключает; **flow без узла `checks`** (например, один implement-агент) guard'а просто не имеет — это и есть «опционально», без ослабления security. Команды flow задавать **не может** — ими правит discovery+approval (C).
- `citation` / `dependency_scan` — детерминированные чекеры для research/audit (валидатор цитат по манифесту; запуск сканеров уязвимостей argv-списком). Делают `checks` пригодным для не-implementation flow без LLM.
- **Авторитет**: зелёный/красный результат `checks` — единственный publish-гейт; никакой evaluator его не переопределяет.

### 2.4. `hitl` (core-owned транспорт)

Типизированный человеческий ввод/одобрение через Telegram. Durable (переживает рестарт), fail-closed по таймауту.

```yaml
- id: approve_plan
  kind: hitl
  signal: approval # question | approval
  timeout_s: null # дефолт из telegram-конфига
```

Чаще HITL встроен в `agent`-узел (`hitl: {...}`) или в core-guard (dangerous-diff, смена набора проверок). Отдельный `hitl`-узел нужен для явной точки одобрения в графе. Механика (транспорт, durable-артефакты, redaction ответов) — ядро.

### 2.5. `publish` / `git` (core-owned)

Терминальная публикация — **исключительно силами оркестратора**. Flow выбирает политику; механика и идемпотентность — ядро.

```yaml
- id: publish
  kind: publish
  policy: pull_request # pull_request | documentation_pull_request | none | local_artifact | private_control_workspace_report
```

- Механика commit/push/PR/merge, scoped staging, защита base-ветки, идемпотентность (`publish_operations` фингерпринты + проверка remote) — `git_manager.py`, неизменяемо.
- Flow не может: коммитить в base, обойти идемпотентность, расширить staging за рамки `output_policy`.
- `none` (security_audit) / `private_control_workspace_report` — узел не трогает git, ядро сохраняет приватный отчёт под `<repo>/.worc/...`.

## 3. Рёбра, исходы, циклы

```yaml
edges:
  - { from: implementation, to: testing } # безусловный
  - { from: testing, to: review, outcome: pass }
  - { from: testing, to: fixing, outcome: fail, loop: test_fix }
  - { from: review, to: publish, outcome: accept } # summary + контроль даёт константный supervisor-слой перед publish
  - { from: review, to: fixing, outcome: rework, loop: review_fix }
```

- **`outcome`** — исход узла: `accept`/`rework` (evaluator), `pass`/`fail` (checks), `route:<label>` (явное ветвление), либо отсутствует (безусловное ребро). Набор возможных исходов узла объявлен; движок проверяет, что выбор ∈ объявленного набора (аналог crewAI `emit`). Ключ назван `outcome`, **не** `on`: YAML 1.1 трактует `on`/`off`/`yes`/`no` как булевы (ключ `on:` распарсился бы в `true`) — подтверждено в co-design (см. co-design/notes.md).
- **`budget` / `loop`** — `rework`-рёбра ограничены. `budget: N` — локальный лимит per-edge (для evaluator выводится подсчётом применённых вердиктов). `loop: <name>` — именованный цикловой счётчик (нынешние `test_fix_cycles`/`review_fix_cycles`).
- **Единый учёт** — любое прохождение `rework`/`fail`-ребра инкрементит **единый** глобальный `fix_iterations` ровно один раз (как в supervisor §rework: один путь учёта, без двойного счёта). Исчерпание глобального бюджета или любого локального → детерминированный `manual_action_required`. Движок гарантирует терминальность — бесконечный цикл невозможен.
- **Evaluator = bounded routing**: evaluator-узел выбирает только из объявленных исходящих рёбер текущего узла. Граф он не придумывает. (Константный supervisor-слой не маршрутизирует вовсе — он терминален.)

```yaml
budgets:
  global_fix_iterations: 20 # task-wide страховка
  test_fix: 10 # локальные цикловые лимиты
  review_fix: 10
```

`QualityAction` foundation маппится на это так: `continue` → ребро `accept`/`pass`; `enter_fixing`/`repeat_stage` → `rework`-ребро на объявленный узел; `stop_manual` → `manual_action_required`; `fail` → `failed`. Ядро — единственный, кто применяет переход.

## 4. session_scope и affinity

- `editing_lineage` — узлы-авторы (implementation, fixing) грузят и обновляют активную editing-сессию (durable lineage из ядра).
- `fresh_disposable` — evaluator (review, test_quality) и константный supervisor-слой получают свежую сессию, не читают и не пишут editing-lineage.
- `resume_own_lineage` — evaluator с многораундовым диалогом (research `critic`) продолжает **свою** сессию, независимую от автора.
- **Affinity — общий механизм**, объявляется во flow: `lineage_affinity: <node_id>` на узле-авторе означает «продолжить editing-сессию указанного узла». Ядро это гарантирует (durable sessions), flow только декларирует связь. В `implementation.yaml` это `lineage_affinity: implementation` на `fixing` — но это выбор данного flow, **а не захардкоженное правило `fixing→implementation`**: оператор волен связать любые два узла-автора или не связывать вовсе. Конфликтующий task-override провайдера/модели отвергается, пока affinity активна.

## 5. Decomposition — конструкция движка

Не вид узла, а **фан-аут/цикл над под-flow**: на каждую принятую подзадачу прогоняется один и тот же под-граф, последовательно, с общим глобальным бюджетом и коммитом на подзадачу (одна ветка). Модель нынешнего `core/decomposition.py`.

```yaml
decomposition:
  construct: fan_out_subtasks
  proposed_by: planning # узел, чей structured_output может предложить разбиение
  gate: { min: 2, max: 8, linear_depends_on: true } # детерминированный гейт принятия
  sub_flow: [implementation, testing_quality, testing, review, fixing]
  commit_each_subtask: true # KIND_SUBTASK_COMMIT на одной ветке
  shared_budget: global_fix_iterations # глобальный бюджет общий на все подзадачи
```

`execution_unit` = `(task_id, subtask_order)` (foundation): per-subtask счётчики циклов сбрасываются между подзадачами, глобальный `fix_iterations` накапливается. Если разбиение не принято — под-flow исполняется один раз для корневой задачи (`subtask_order = NULL`).

## 6. Контракт Markdown-файлов

Узел ссылается на MD (`role_file`, и т.п.). MD содержит роль/цель/инструкции/критерии — длинный текст, не структуру. Допускается явная минимальная интерполяция `{task_id}`, `{repo}` из входов на старте (как crewAI `inputs`); никакой логики в MD. Это даёт «структура — YAML, промпт — MD» и позволяет операторам править поведение, не трогая YAML-граф.

## 7. Эталон: `implementation.yaml` (доказательство абстракции)

Текущий implementation-конвейер (refinement → planning → implementation → testing → review → fixing → publish, с decomposition и **константным supervisor-слоем над графом**, дающим summary + терминальный контроль перед publish) — целиком данными:

```yaml
flow:
  name: implementation
  task_type: implementation
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  network_policy: default

  defaults:
    evaluator:
      {
        session_scope: fresh_disposable,
        permission_profile: read-only,
        max_rework_per_stage: 1,
      }

  nodes:
    - {
        id: refinement,
        kind: agent,
        role_file: roles/refinement.md,
        session_scope: fresh_disposable,
        permission_profile: read-only,
        optional: true,
        hitl: { allow_question: true },
      }
    - {
        id: planning,
        kind: agent,
        role_file: roles/planning.md,
        permission_profile: read-only,
        hitl: { allow_question: true, allow_approval: true },
      }
    - {
        id: implementation,
        kind: agent,
        role_file: roles/implementation.md,
        session_scope: editing_lineage,
        permission_profile: workspace-write,
      }
    - {
        id: testing_quality,
        kind: evaluator,
        role: test_quality,
        role_file: roles/testing.md,
        enabled: false,
        blocking: false,
      }
    - {
        id: testing,
        kind: checks,
        checker: command_profile,
        discovery: { mode: agent_assisted, approve_command_changes: true },
      }
    - { id: review, kind: evaluator, role: review, role_file: roles/review.md }
    - {
        id: fixing,
        kind: agent,
        role_file: roles/fixing.md,
        session_scope: editing_lineage,
        lineage_affinity: implementation,
        permission_profile: workspace-write,
      }
    - { id: publish, kind: publish, policy: pull_request }
    # supervise_impl / supervise_fix / summary-узлов в графе НЕТ:
    # summary + терминальный контроль даёт константный supervisor-слой
    # оркестратора перед publish (config.yaml: supervisor); см. §2.2, p2-implementation.md §P2.1.

  edges:
    - { from: refinement, to: planning }
    - { from: planning, to: implementation }
    - { from: implementation, to: testing_quality }
    - { from: testing_quality, to: testing, outcome: accept } # disabled → узел пропускается, идём на testing
    - { from: testing_quality, to: fixing, outcome: rework, budget: 1 } # non-blocking: исчерпание → continue
    - { from: testing, to: review, outcome: pass }
    - { from: testing, to: fixing, outcome: fail, loop: test_fix }
    - { from: review, to: publish, outcome: accept } # summary + контроль — константный supervisor-слой перед publish
    - { from: review, to: fixing, outcome: rework, loop: review_fix }
    - { from: fixing, to: testing_quality } # после фикса заново через test-quality → checks

  budgets: { global_fix_iterations: 20, test_fix: 10, review_fix: 10 }

  decomposition:
    construct: fan_out_subtasks
    proposed_by: planning
    gate: { min: 2, max: 8, linear_depends_on: true }
    sub_flow: [implementation, testing_quality, testing, review, fixing]
    commit_each_subtask: true
    shared_budget: global_fix_iterations
```

Что это доказывает (соответствие нынешнему поведению — проверяется адаптированным на движок интеграционным сьютом; golden-harness/паритет байт-в-байт отменён, см. [plan.md](plan.md)):

- каждая стадия-автор → `agent`-узел; supervisor (summary + терминальный контроль) → **константный слой оркестратора над flow**, не узел; review → `evaluator`-узел (не отдельная стадия); testing → `checks` + необязательный `evaluator` (`role=test_quality`); publishing → `publish`;
- fix-петли (test-driven, review-driven) → `rework`/`fail`-рёбра с единым глобальным `fix_iterations` и локальными цикловыми лимитами;
- decomposition → конструкция фан-аута над под-flow;
- dangerous-diff после implementation/fixing, durable-HITL, объявляемая во flow lineage-affinity (в этом flow fixing→implementation), idempotent publish, **константный supervisor-слой (summary + advisory) перед publish** — **не во flow-графе**, а ядровые/оркестраторные гарантии, на которые flow опирается.

**Движок при этом не знает ничего доменного**: он исполняет узлы, следует объявленным рёбрам в пределах бюджетов и зовёт ядро для core-owned узлов. Никакого `if task_type == implementation`.

## 8. Примеры: `deep_research.yaml` и `security_audit.yaml`

Те же два flow из [outdated/task_workflow_profiles.md](../outdated/task_workflow_profiles.md) §7–§8 — целиком данными на той же палитре, что и implementation (§7). Это доказательство, что палитра не заточена под один flow.

Заметка про права записи: research/audit держат `permission_ceiling: workspace-write`, потому что пишущие узлы (`synthesis`/`report`) создают файлы, **но** `output_policy` + path-containment + after-stage guard ограничивают, КУДА (research-dir / private-report-dir); исходники repo остаются read-only (`target_repository_writes: approved_document_only`/`none`). Sandbox разрешает запись, политика ограничивает путь — два слоя.

### 8.1. `deep_research.yaml`

`output_policy: repository_document`, `publishing: documentation_pull_request`. Результат — директория `docs/research/<task-id>/` с обязательным `report.md` + `sources.json`; код не меняется.

```yaml
flow:
  name: deep_research
  task_type: deep_research
  permission_ceiling: workspace-write
  output_policy: repository_document
  publishing: documentation_pull_request
  network_policy: research

  defaults:
    evaluator:
      { session_scope: fresh_disposable, permission_profile: read-only }

  nodes:
    - {
        id: refinement,
        kind: agent,
        role_file: roles/research/refinement.md,
        permission_profile: read-only,
        optional: true,
        hitl: { allow_question: true },
      }
    - {
        id: repository_analysis,
        kind: agent,
        role_file: roles/research/repository_analysis.md,
        permission_profile: read-only,
      }
    - {
        id: external_research,
        kind: agent,
        role_file: roles/research/external_research.md,
        permission_profile: read-only,
        optional: true,
      }
    - {
        id: architecture_design,
        kind: agent,
        role_file: roles/research/architecture_design.md,
        permission_profile: workspace-write,
      }
    - {
        id: synthesis,
        kind: agent,
        role_file: roles/research/synthesis.md,
        permission_profile: workspace-write,
      }
    - { id: citation_check, kind: checks, checker: citation }
    - {
        id: fact_verification,
        kind: evaluator,
        role: verifier,
        role_file: roles/research/verifier.md,
        blocking: false,
      }
    - {
        id: critical_review,
        kind: evaluator,
        role: critic,
        role_file: roles/research/critic.md,
        session_scope: resume_own_lineage,
        blocking: false,
      }
    - { id: publish, kind: publish, policy: documentation_pull_request }

  edges:
    - { from: refinement, to: repository_analysis }
    - { from: repository_analysis, to: external_research } # external_research optional → при детерминированном skip узел проходится насквозь к architecture_design
    - { from: external_research, to: architecture_design }
    - { from: architecture_design, to: synthesis }
    - { from: synthesis, to: citation_check }
    - { from: citation_check, to: fact_verification, outcome: pass }
    - { from: citation_check, to: synthesis, outcome: fail, budget: 1 } # citation-loop pinned 1 (v1); неисправимая цитата → claim помечается unverified, не падение задачи
    - { from: fact_verification, to: critical_review, outcome: accept }
    - { from: fact_verification, to: synthesis, outcome: rework, budget: 2 }
    - { from: critical_review, to: publish, outcome: accept }
    - { from: critical_review, to: synthesis, outcome: rework, budget: 3 } # bounded ping-pong

  budgets: { global_revision_iterations: 12 }
```

Что видно в данных как отличие от implementation: evaluator'ы `blocking: false` — на исчерпании бюджета flow **не** уходит в `manual_action_required`, а идёт по `accept`-ребру (→ publish) с остаточными расхождениями в разделе Open questions (профили §7); `critical_review` держит `resume_own_lineage` (помнит, что уже отмечал между раундами); `fact_verification` — двухслойная проверка: детерминированный `citation_check` (Layer 1, без LLM) + агент-verifier (Layer 2).

### 8.2. `security_audit.yaml`

`output_policy: private_control_workspace_report`, `publishing: none`. Отчёт пишется под gitignored `<repo>/.worc/security-reports/<task-id>/`; git не трогается, репозиторий остаётся byte-for-byte.

```yaml
flow:
  name: security_audit
  task_type: security_audit
  permission_ceiling: workspace-write
  output_policy: private_control_workspace_report
  publishing: none
  network_policy: advisories

  defaults:
    evaluator:
      { session_scope: fresh_disposable, permission_profile: read-only }

  nodes:
    - {
        id: scope,
        kind: agent,
        role_file: roles/audit/scope.md,
        permission_profile: read-only,
        hitl: { allow_question: true },
      }
    - {
        id: repository_analysis,
        kind: agent,
        role_file: roles/audit/repository_analysis.md,
        permission_profile: read-only,
      }
    - { id: dependency_scan, kind: checks, checker: dependency_scan }
    - {
        id: threat_analysis,
        kind: agent,
        role_file: roles/audit/threat_analysis.md,
        permission_profile: read-only,
      }
    - {
        id: finding_verification,
        kind: evaluator,
        role: verifier,
        role_file: roles/audit/verifier.md,
        blocking: false,
      }
    - {
        id: report,
        kind: agent,
        role_file: roles/audit/report.md,
        permission_profile: workspace-write,
      }
    - {
        id: private_storage,
        kind: publish,
        policy: private_control_workspace_report,
      }

  edges:
    - { from: scope, to: repository_analysis }
    - { from: repository_analysis, to: dependency_scan }
    - { from: dependency_scan, to: threat_analysis } # dependency_scan — evidence, безусловное ребро (не pass/fail-гейт)
    - { from: threat_analysis, to: finding_verification }
    - { from: finding_verification, to: report, outcome: accept }
    - {
        from: finding_verification,
        to: threat_analysis,
        outcome: rework,
        budget: 2,
      } # non-blocking: false-positives помечаются
    - { from: report, to: private_storage }

  budgets: { global_revision_iterations: 8 }
```

Здесь `checks`-узел показывает обе грани: `dependency_scan` собирает evidence (findings — данные, не гейт; ребро безусловное), тогда как `command_profile` в implementation и `citation` в research — гейтят (pass/fail). Гейтит ли результат — решает flow рёбрами, не движок.

### 8.3. Что это доказывает

Три flow (implementation + research + audit) выразились **одной палитрой** (`agent`/`evaluator`/`checks`/`hitl`/`publish` + рёбра + бюджеты), потребовав от ядра только: новые `checks`-чекеры `citation`/`dependency_scan`; `resume_own_lineage` (durable sessions); output/publishing/network-политики (foundation); приватное хранилище отчёта (audit). **Виды узлов и сам движок — без изменений.** Это и есть co-design тест абстракции ([index.md](index.md) §11): ноль доменного знания в движке.

## 9. Snapshot и resume

При старте движок резолвит граф в неизменяемый снапшот, считает `flow_fingerprint`, персистит. Recovery доверяет снапшоту и **не переразрешает** flow из живого конфига; проверяет целостность снапшота, существование видов узлов, и что текущие security-возможности не требуют расширить сохранённый потолок. Чекпоинт = `{ completed_nodes, current_node, loop_counters, publish_operations }`. Дедуп побочных эффектов (commit/scan/notify) — ядровые маркеры (`publish_operations`), не движок.

## 10. Зафиксированные решения по контракту

- **Палитра доказана** тремя flow (§7–§8); добавлять виды узлов в v1 не требуется. Палитра **расширяема санкционированным путём** (не произвольным узлом-кодом): отложенная фаза добавляет ceiling-bound вид `tool` — операторский subprocess под потолком, side-effect-free, JSON-контракт ([p5-custom-tool-nodes.md](p5-custom-tool-nodes.md)), вне v1.
- **`route` — метка ребра** `outcome: route:<label>` на узле-решателе; отдельного `route`-узла нет.
- **`review` — это `evaluator`** с `role=review`; блокирующие findings укладываются в вердикт, отдельного вида нет.
- **Supervisor — константный слой над flow, не evaluator-узел** (решение 2026-06-19): summary + терминальный advisory-контроль на уровне оркестратора, запускается поверх **любого** flow (включая degenerate из одного агента); конфигурируется через `config.yaml` (`supervisor: { model, reasoning, role_file }`) под потолком (read-only, allowlist, containment); не может `rework`/переоткрыть. Блокирующих per-stage supervisor-узлов и отдельного summary-провайдера в графе нет; блокирующие пер-стейдж гейты выражаются опциональными `review`/`test_quality`/operator-defined evaluator-узлами.
- **Условный пропуск узла — единый детерминированный предикат `when`** (резолвится из config/task на загрузке, не агентом); `optional`/`enabled`/`enabled_policy` сводятся к нему (примеры §7–§8 ещё в старой форме, мигрируют в P0).
- **Decomposition — только implementation в v1**; research/audit линейны.
- **Per-task оверрайды графа/узлов не поддерживаются**: flow — единственный источник графа и параметров узлов; задача несёт только идентичность/диспетчеризацию (`task_type`) + операционные входы (`contacts`, `prompt_audit`). Вариация = другой flow.
- **Файловая раскладка**: встроенные flow запакованы; операторские — в `.worc/flows/`; `config.yaml` = инфраструктура + дефолты провайдера, на которые узел падает при `null`.
- JSON-Schema flow + строгий фатальный валидатор фиксируются на P0.2/P0.3 ([security-ceiling.md](security-ceiling.md) §4).
