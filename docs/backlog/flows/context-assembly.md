# Формирование контекста agent-узлов: implementation и security_audit

Статус: **backlog / инженерная спека (не запланировано к исполнению)** Дата: 2026-06-17 Владелец: Vladimir Makarevich

Пошаговый разбор, как для **каждого** agent/evaluator-узла двух flow собирается контекст провайдера (`role_file` → промпт → `AgentRunRequest`). Дополняет решение из [p1-engine.md §P1.3](p1-engine.md) (prompt-машинерия — ядро, источник шаблона — `role_file`). Узлы `checks`/`hitl`/`publish` **не агентные** — у них нет промпта (отмечены явно).

Опора на код: `AgentRunRequest` ([providers/base.py](../../../src/wastech_orchestrator/providers/base.py) ≈82), `render_prompt`/`ALLOWED_PROMPT_VARS` ([core/prompts.py](../../../src/wastech_orchestrator/core/prompts.py)), `_prompt_variables`/`_build_prompt` ([core/orchestrator.py](../../../src/wastech_orchestrator/core/orchestrator.py) ≈2118/≈2150). Графы — packaged [implementation.yaml](../../../src/wastech_orchestrator/core/flow/packaged/implementation.yaml) и [security_audit.yaml](../../../src/wastech_orchestrator/core/flow/packaged/security_audit.yaml).

## Общий конвейер сборки (один для всех agent/evaluator-узлов)

Когда движок доходит до узла (P1.1), обёртка узла собирает контекст в фиксированном порядке:

1. **`when`-гейт.** Если предикат (`derived.*`/`config.*`) ложен — узел детерминированно пропускается (`record_skip`), контекст не собирается. Иначе — дальше.
2. **Источник шаблона = `role_file`.** Ядро читает MD-файл узла (резолв относительно директории flow; path-containment проверил валидатор). Это роль/цель/инструкции — длинный текст, без логики.
3. **Резолв allowlisted-переменных** (`_prompt_variables`, ядро). **Только пути и метаданные**: `{task_id}`, `{repo}`/`{repo_path}`, `{task_path}`, `{plan_path}`, `{diff_path}`, `{checks_path}`, `{review_path}`, `{skills_path}`, `{subtask_*}`, и для research/audit `{research_dir}`/`{report_dir}`. **Никогда** — тело задачи/diff/логи/env/секреты: они в артефакт-файлах, на которые агент ссылается **по пути**.
4. **`render_prompt(role_text, vars)`** (безопасный рендерер): подставляет только токены из allowlist; неприменимые → пустая строка; неизвестные `{...}` (скобки кода/JSON) проходят насквозь без ошибки.
5. **Детерминированные суффиксы.** Для подзадачи добавляется `Active subtask N of M; spec: <path>`. Больше ничего не дописывается.
6. **Сборка `AgentRunRequest`** (ядро):
   - `prompt` — результат (4)+(5);
   - `session_id` — из lineage-стора по `session_scope` (P2.2): `editing_lineage` грузит активную сессию execution_unit; `fresh_disposable` → `None` (свежая); `resume_own_lineage` → своя сессия;
   - `permission_profile` — поле узла, **clamp** к `permission_ceiling` (evaluator всегда `read-only`);
   - `model`/`reasoning` — поле узла или дефолт провайдера при `null`;
   - `timeout_seconds`, `extra_args` (прошли `find_forbidden_args`), `output_schema`;
   - артефакт-пути (`task_path`/`plan_path`/`diff_path`/`check_artifacts_path`/`review_artifacts_path`/`human_input_path`/`skill_reference_paths`) — заполнены теми, что уже существуют на этот момент графа;
   - в flow-модели поле `stage` → `node_id` (P1.5).
7. **Запуск** через router (primary→fallback только на инфра-ошибки) → провайдер (argv-без-shell).
8. **Пост-обработка ядра**: redaction артефактов/логов, запись `prompt_audit` (рендеренный промпт + метаданные, если включён), **dangerous-diff guard** автоматически после `workspace-write`-узла, чекпоинт `node_run`.

Ниже для каждого узла — что специфично на шагах 2–6. Какие пути **заполнены** зависит от позиции в графе: `task_path` есть всегда; `plan_path` — после planning; `diff_path` — после edit-узла (implementation/fixing); `checks_path` — после `checks`; `review_path` — после review; `skills_path` — если planning выбрал skills.

---

## A. Flow `implementation`

Порядок happy-path: refinement → planning → implementation → supervise_impl → testing_quality → **testing** (checks) → review → [fixing → supervise_fix]\* → summary → **publish**.

### A1. `refinement` (agent, read-only) — `when: derived.needs_refinement`

- **Гейт**: исполняется только если интейк-классификация дала NEEDS_ENRICHMENT (иначе детерминированный skip).
- **session/права**: `fresh_disposable`, `read-only`; model/reasoning — дефолт провайдера; HITL `allow_question: true`.
- **role_file** `roles/refinement.md`: «обогатить задачу до полной однозначной спецификации; не править код; вернуть типизированный structured-output; ставить human_input только при неустранимой неоднозначности».
- **Интерполируемые пути**: `{task_path}` (исходный task .md). При повторе с ответом — `human_input_path` (durable HITL-ответ).
- **output_schema**: refinement-spec (обогащённая спецификация).
- **AgentRunRequest** (ключевое): `prompt`=role+task_path, `permission_profile=read-only`, `session_id=None`, `task_path` задан, `human_input_path` при повторе.
- **После узла**: structured-output может нести HITL-вопрос → durable Telegram round-trip, узел перезапускается с `human_input_path`.

### A2. `planning` (agent, read-only) — HITL question+approval

- **session/права**: `fresh_disposable`, `read-only`; HITL `allow_question: true, allow_approval: true`.
- **role_file** `roles/planning.md`: «краткий план из задачи и обогащённой спеки; не править код; при decomposition вернуть упорядоченные подзадачи; human_input для существенного уточнения/одобрения».
- **Интерполируемые пути**: `{task_path}`, обогащённая спека (через task/plan артефакт), `{repo}`.
- **output_schema**: plan + опц. предложение decomposition (`decompose`, `subtasks[]` с order/title/slug/acceptance/depends_on — гейт в `core/decomposition.py`).
- **Доп. контекст**: planning **выбирает** repo-skills → их пути позже попадают в `{skills_path}` для edit-узлов.
- **AgentRunRequest**: `permission_profile=read-only`, `session_id=None`, `task_path` задан, `output_schema` = plan-схема.
- **После узла**: HITL-approval плана; гейт decomposition; запись immutable spec-файлов подзадач.

### A3. `implementation` (agent, **workspace-write**, `editing_lineage`)

- **session/права**: `editing_lineage` (грузит/создаёт editing-сессию execution_unit), `workspace-write`.
- **role_file** `roles/implementation.md`: «реализовать задачу в рабочем дереве по плану; минимальное focused-изменение; если human_input зафиксировал отказ от опасного изменения — убрать/переделать».
- **Интерполируемые пути**: `{plan_path}` (план из A2), `{task_path}`, `{skills_path}` (выбранные в A2 SKILL.md — advisory, read-only), при подзадаче — `{subtask_spec_path}` + суффикс «Active subtask N of M».
- **output_schema**: нет (edit-узел пишет в дерево, не structured).
- **AgentRunRequest**: `permission_profile=workspace-write`, `session_id` = editing-сессия (P2.2), `plan_path`/`task_path`/`skill_reference_paths` заданы.
- **После узла**: **dangerous-diff guard** (core, авто): классификация diff (deletion/dependency) → при необходимости HITL-approval; затем чекпоинт. Создаётся `diff_path` для следующих узлов.

### A4. `supervise_impl` (evaluator, role=supervisor, read-only, `fresh_disposable`)

- **session/права**: `fresh_disposable` (свежая сессия, **не** трогает editing-lineage автора), `read-only` (жёстко для evaluator).
- **role_file** `roles/supervisor.md`: «оценить diff против задачи/плана read-only; вернуть вердикт accept|rework + findings с severity; rework только при ≥1 medium/high».
- **Интерполируемые пути**: `{diff_path}` (из A3), `{task_path}`, `{plan_path}`.
- **output_schema**: `{ verdict: accept|rework, findings: [{severity, reason, paths}] }` (строго валидируется ядром).
- **AgentRunRequest**: `permission_profile=read-only`, `session_id=None`.
- **После узла**: вердикт — immutable-артефакт (`evaluations`, P2.1). Исход `accept` → A5; `rework` → ребро на `fixing` (инлайн `budget:1`), `record_rework` инкрементит единый `fix_iterations`.

### A5. `testing_quality` (evaluator, role=test_quality, read-only, **неблокирующий**) — `when: config.hybrid_testing`

- **Гейт**: только если `config.hybrid_testing` включён.
- **session/права**: `fresh_disposable`, `read-only`; `blocking: false`.
- **role_file** `roles/testing.md`: «оценить качество тестов, написанных implementation-агентом; не писать тесты; вердикт accept|rework».
- **Интерполируемые пути**: `{diff_path}` (тесты — часть diff), `{task_path}`.
- **output_schema**: вердикт + findings.
- **Неблокирующий**: исчерпание `budget:1` → идём по `accept`-ребру (→ testing), **не** в manual.

### A6. `testing` (checks, `command_profile`) — **нет промпта**

Детерминированный `CheckRunner` (pytest/ruff/…); discovery + approve_command_changes (HITL) + always-on mutation guard; exit-коды авторитетны. Создаёт `checks_path`. Исход `pass`→review, `fail`→fixing (`loop: test_fix`).

### A7. `review` (evaluator, role=review, read-only, `fresh_disposable`)

- **session/права**: `fresh_disposable`, `read-only`.
- **role_file** `roles/review.md`: «ревью текущего diff против задачи и плана; findings с severity; блокирующее — то, что должно измениться до merge».
- **Интерполируемые пути**: `{diff_path}`, `{checks_path}` (результаты тестов), `{task_path}`, `{plan_path}`.
- **output_schema**: вердикт + findings (блокирующие → rework).
- **После узла**: создаётся `review_path`. `accept`→summary; блокирующее → fixing (`loop: review_fix`).

### A8. `fixing` (agent, **workspace-write**, `editing_lineage`, `lineage_affinity: implementation`)

- **session/права**: `editing_lineage` с **affinity → implementation** — **продолжает editing-сессию** узла implementation (durable resume того же провайдера/сессии, P2.2), `workspace-write`.
- **role_file** `roles/fixing.md`: «устранить упавшие checks и/или блокирующие review-findings из контекст-файлов; минимальное изменение; учесть отказ опасного изменения из human_input».
- **Интерполируемые пути**: `{diff_path}`, `{checks_path}` (что упало), `{review_path}` (блокирующее), `{skills_path}`.
- **AgentRunRequest**: `permission_profile=workspace-write`, `session_id` = **сессия implementation** (affinity); конфликтующий per-attempt провайдер/модель отвергается, пока affinity активна.
- **После узла**: dangerous-diff guard; обновляется `diff_path`; → supervise_fix.

### A9. `supervise_fix` (evaluator, role=supervisor, read-only, `fresh_disposable`)

Идентично A4 (тот же `roles/supervisor.md`, тот же output_schema), но оценивает diff **после фикса**. `accept` → возврат на testing_quality (повторная оценка) → checks; `rework` → fixing (`budget:1`).

### A10. `summary` (evaluator, **final_handoff**, read-only, неблокирующий) — `when: config.summary_enabled`

- **session/права**: `fresh_disposable`, `read-only`, `evaluation_kind: final_handoff`, `blocking: false`.
- **role_file** `roles/supervisor-final.md` (≈ `summary.md`): «связный итог принятого изменения: что/как/интеграция/почему; не править код».
- **Интерполируемые пути**: `{task_path}`, `{plan_path}`, `{diff_path}`.
- **output_schema**: summary-схема (what/how/integration/why).
- **Особое**: final_handoff **не может** выдать rework/переоткрыть задачу; ядро валидирует и пишет `summary.{md,json}` (или детерминированный fallback). Заменяет старый summary-провайдер.

### A11. `publish` (publish, `pull_request`) — **нет промпта**

Core-owned: scoped commit + task-scoped audit-commit + push + PR + (опц.) auto-merge; идемпотентно через `publish_operations`.

---

## B. Flow `security_audit`

Порядок (линейный, без decomposition): scope → repository_analysis → **dependency_scan** (checks) → threat_analysis → finding_verification → report → **private_storage**. `output_policy: private_control_workspace_report`, `publishing: none`, `network_policy: advisories`. Все agent-узлы — `fresh_disposable` (durable editing-lineage здесь не используется; репозиторий не редактируется, кроме записи приватного отчёта).

### B1. `scope` (agent, read-only) — HITL question

- **session/права**: `fresh_disposable`, `read-only`; HITL `allow_question: true`.
- **role_file** `roles/audit/scope.md`: «определить границы аудита из задачи; уточнить охват через human_input при неоднозначности; не править код».
- **Интерполируемые пути**: `{task_path}`, `{repo}`.
- **output_schema**: scope-спецификация (что в охвате/вне).
- **Сеть**: `network_policy: advisories` — доступ только к advisory-источникам (узлы read-only).

### B2. `repository_analysis` (agent, read-only)

- **session/права**: `fresh_disposable`, `read-only`.
- **role_file** `roles/audit/repository_analysis.md`: «проанализировать репозиторий в границах охвата; собрать факты для модели угроз; не править код».
- **Интерполируемые пути**: `{repo}`, `{task_path}`, scope-вывод (по пути артефакта B1).
- **output_schema**: структурированные наблюдения (поверхности атаки, чувствительные точки).

### B3. `dependency_scan` (checks, `dependency_scan`) — **нет промпта**

Детерминированные argv-сканеры (pip-audit/osv-scanner/…) через `run_process` (argv-без-shell, timeout, env-allowlist). Findings — **evidence**, не гейт; эмитит `outcome: pass` (скан выполнился). Создаёт checks-артефакт (путь идёт в `{checks_path}` для threat_analysis).

### B4. `threat_analysis` (agent, read-only)

- **session/права**: `fresh_disposable`, `read-only`.
- **role_file** `roles/audit/threat_analysis.md`: «построить модель угроз из анализа репозитория и findings сканера; оценить severity/эксплуатируемость; не править код».
- **Интерполируемые пути**: `{repo}`, repository_analysis-вывод, `{checks_path}` (findings dependency_scan), `{task_path}`.
- **output_schema**: список угроз (severity, путь, обоснование).
- **После узла**: → finding_verification.

### B5. `finding_verification` (evaluator, role=verifier, read-only, **неблокирующий**)

- **session/права**: `fresh_disposable`, `read-only`; `blocking: false`.
- **role_file** `roles/audit/verifier.md`: «верифицировать угрозы threat_analysis read-only; пометить false-positives; вердикт accept|rework».
- **Интерполируемые пути**: threat_analysis-вывод, `{repo}`.
- **output_schema**: вердикт + помеченные findings.
- **Неблокирующий**: `rework` → threat_analysis (`budget:2`) для перепроверки; исчерпание → continue (false-positives просто помечены), **не** manual.

### B6. `report` (agent, **workspace-write**)

- **session/права**: `fresh_disposable`, **`workspace-write`** — но `output_policy: private_control_workspace_report` + path-containment ограничивают запись **только** в `{report_dir}` (`<repo>/.worc/security-reports/<task-id>/`); исходники repo остаются read-only (два слоя: sandbox разрешает запись, политика ограничивает путь).
- **role_file** `roles/audit/report.md`: «составить отчёт из верифицированных угроз; писать только в директорию отчёта».
- **Интерполируемые пути**: `{report_dir}` (целевая приватная директория), верифицированные findings (B5), `{repo}`.
- **output_schema**: нет (пишет файлы отчёта).
- **После узла**: **after-stage guard** — фактический выход сравнивается с `output_policy`; запись вне `{report_dir}` → отказ. dangerous-diff guard применим (workspace-write), но изменений в src нет.

### B7. `private_storage` (publish, `private_control_workspace_report`) — **нет промпта**

Core-owned: сохраняет отчёт под gitignored `<repo>/.worc/security-reports/<task-id>/`; **git не трогается** (`publishing: none`), репозиторий byte-for-byte; **fail-closed** если config внутри repo (отчёт не попадает в staging/commit/PR).

---

## Сводка контраста двух flow

| Аспект | implementation | security_audit |
| --- | --- | --- |
| editing-lineage | implementation/fixing (`editing_lineage` + affinity) | нет (все agent — `fresh_disposable`) |
| пишущие узлы | implementation/fixing (код) | report (только приватный отчёт) |
| гейт checks | `command_profile` (pass/fail гейтит) | `dependency_scan` (evidence, не гейтит) |
| evaluator-исход на исчерпании | supervisor blocking → manual | verifier non-blocking → continue |
| публикация | PR (+auto-merge) | none (приватный отчёт мимо git) |
| сеть | default | advisories |
| dangerous-diff guard | срабатывает (workspace-write правит код) | срабатывает на report, но src не меняется |

Во всех случаях движок **не знает** доменного: он читает `role_file`, инъектирует allowlisted-пути, собирает `AgentRunRequest` тем же ядровым кодом и зовёт router/провайдера. Разница между flow — целиком в данных (узлы, role_file, политики, рёбра).
