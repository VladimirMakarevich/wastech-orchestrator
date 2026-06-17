# P3 — Flows research + audit + их виды узлов

Статус: **backlog / инженерная спека (не запланировано к исполнению)** Дата: 2026-06-17 Владелец: Vladimir Makarevich

Детализация фазы P3 из [plan.md](plan.md). Цель: два не-implementation flow данными; **доказательство обобщаемости** движка. Адаптирует [task workflow profiles](../outdated/task_workflow_profiles.md). База проверки фазы: **co-design тест трёх flow** — финальный гейт абстракции ([index.md](index.md) §11): три flow выражаются данными без доменного знания в движке.

Вход: полный evaluator-примитив (P2, включая `resume_own_lineage`), packaged [deep_research.yaml](../../../src/wastech_orchestrator/core/flow/packaged/deep_research.yaml) и [security_audit.yaml](../../../src/wastech_orchestrator/core/flow/packaged/security_audit.yaml) (уже грузятся/валидируются в P0; в P3 — исполняются).

Заметка про права записи ([flow-contract.md](flow-contract.md) §8): research/audit держат `permission_ceiling: workspace-write` (пишущие узлы `synthesis`/`report` создают файлы), но `output_policy` + path-containment + after-stage guard ограничивают **куда**; исходники repo остаются read-only. Sandbox разрешает запись, политика ограничивает путь — два слоя.

---

## P3.1 — Ядровые чекеры `checks`

`citation` (детерминированный валидатор манифеста, без LLM) и `dependency_scan` (argv-сканеры). Делают `checks` пригодным для research/audit без LLM. Валидатор P0.5 уже принимает `checker ∈ {command_profile, citation, dependency_scan}` ([snapshot.py](../../../src/wastech_orchestrator/core/flow/snapshot.py) `_CHECKER_KINDS`).

### Touchpoints

- [`core/flow/nodes/checks.py`](../../../src/wastech_orchestrator/core/flow/nodes/checks.py) — диспетч по `checker`; `command_profile` (P1.3) + `citation` + `dependency_scan`.
- **Новый** `checks/citation.py` (или `core/checks/`) — детерминированный валидатор манифеста: читает `sources.json`, проверяет `path`/`line`/`snippet`, три исхода `verified`/`broken`/`uncheckable`, **без LLM**.
- **Новый** `checks/dependency_scan.py` — argv-сканеры (`pip-audit`/`osv-scanner`/…) через [`providers/process.py`](../../../src/wastech_orchestrator/providers/process.py) `run_process` (argv-без-shell, обязательный timeout, env-allowlist); структура finding.
- Переиспользует [`check_runner.py`](../../../src/wastech_orchestrator/check_runner.py) `CheckOutcome` (exit-коды авторитетны для гейтящих чекеров).

### Поведение

- `citation` (research, гейтит): на synthesis-выходе валидирует цитаты по манифесту. Hallucinated-цитата → `broken`; битый манифест → `uncheckable` **без падения**. Исход `pass`/`fail` гейтит synthesis-петлю ([packaged deep_research.yaml](../../../src/wastech_orchestrator/core/flow/packaged/deep_research.yaml) ≈72, `citation_check → synthesis outcome:fail budget:1`).
- `dependency_scan` (audit, **не** гейтит — собирает evidence): запускает сканеры argv-списком; findings — данные, не гейт. Но эмитит `outcome: pass` (скан выполнился), чтобы `checks` оставался единообразно pass/fail и в движке не было спец-кейса «этот checker не гейтит» (co-design находка #3, [co-design/notes.md](co-design/notes.md)). Гейтит ли результат — решает flow рёбрами ([packaged security_audit.yaml](../../../src/wastech_orchestrator/core/flow/packaged/security_audit.yaml) ≈54, `dependency_scan → threat_analysis outcome:pass`).
- Команды/сканеры flow задавать **не может** ([security-ceiling.md](security-ceiling.md) §3) — ядровой набор + discovery/approval.

### Тесты

Из [profiles §16](../outdated/task_workflow_profiles.md#16-testing-requirements):

- `test_citation_hallucinated_to_broken` — несуществующая цитата → `broken`.
- `test_citation_malformed_manifest_uncheckable_no_crash` — битый `sources.json` → `uncheckable`, не падение.
- `test_citation_verified_pass` — валидный манифест → `pass`.
- `test_dependency_scan_argv_with_timeout` — сканер запускается argv + timeout; findings структурированы.
- `test_dependency_scan_emits_pass_not_gate` — скан выполнился → `pass` (гейтинг решают рёбра flow).

### Exit

`checks` обобщён на research/audit без LLM.

---

## P3.2 — Политики output / publishing / network

`repository_document` + `documentation_pull_request`; `private_control_workspace_report` + `none`; приватный отчёт под `<repo>/.worc/security-reports/<task-id>/`; path-containment write-only в research-dir; `network_policy` для external research; after-stage сравнение выхода с политикой.

### Touchpoints

- [`core/flow/nodes/publish.py`](../../../src/wastech_orchestrator/core/flow/nodes/publish.py) — политики `documentation_pull_request` / `private_control_workspace_report` / `none` (P1.3 покрыл `pull_request`).
- [`git_manager.py`](../../../src/wastech_orchestrator/git_manager.py) — documentation PR (тот же `create_pr` ≈598, но scoped staging на docs-пути).
- **Новый** output-guardrails модуль — path-containment + after-stage сравнение выхода с `output_policy` (резолвится из [contracts.py](../../../src/wastech_orchestrator/core/flow/contracts.py) `OutputPolicy` в foundation-овский `ResolvedOutputPolicy`: `target_repository_writes` + `control_workspace_writes` + `allowed_path_policy`).
- `network_policy` ([contracts.py](../../../src/wastech_orchestrator/core/flow/contracts.py) `NetworkPolicy`, добавлен в P0.5) — бинарные уровни `advisories`/`research`; отсутствие = нет сети.

### Поведение

- **research**: `output_policy: repository_document` → пишет только в `docs/research/<task-id>/` (обязательные `report.md` + `sources.json`); код не меняется (`target_repository_writes: approved_document_only`). `publishing: documentation_pull_request`.
- **audit**: `output_policy: private_control_workspace_report` → отчёт под gitignored `<repo>/.worc/security-reports/<task-id>/`; `publishing: none` → git не трогается, repo byte-for-byte. **fail-closed** если config внутри repo (отчёт не должен попасть в staging/commit/PR).
- **after-stage guard**: после пишущего узла ядро сравнивает фактический выход с политикой; запись вне разрешённых путей → отказ (path-containment + scoped staging, [git_manager.py](../../../src/wastech_orchestrator/git_manager.py) `staged_pathspec` ≈447).

### Тесты

- `test_research_writes_only_research_dir` — synthesis пишет только в свою директорию; запись вне → отказ.
- `test_audit_leaves_repo_byte_for_byte` — audit не трогает git; repo неизменён.
- `test_private_report_not_in_staging_commit_pr` — приватный отчёт не попадает в staging/commit/PR.
- `test_private_report_fail_closed_if_config_in_repo`.
- `test_network_policy_off_by_default` — без `network_policy` сети нет.

### Exit

Политики обеспечены ядром, flow их только выбирает.

---

## P3.3 — `deep_research.yaml`

`refinement → repository_analysis → external_research(opt) → architecture_design → synthesis → citation_check → fact_verification(verifier) → critical_review(critic, resume_own_lineage) → publish`. citation-loop pinned 1 (v1); bounded critic ping-pong; на исчерпании — публикация с Open questions, **не** `fail`.

### Touchpoints

- Packaged [deep_research.yaml](../../../src/wastech_orchestrator/core/flow/packaged/deep_research.yaml) — исполняется через движок (узлы P3.1/P3.2 + evaluator P2).
- Роли MD: `roles/research/*.md` (refinement/repository_analysis/external_research/architecture_design/synthesis/verifier/critic).

### Поведение (отличия от implementation, видны в данных)

- evaluator'ы `blocking: false` ([deep_research.yaml](../../../src/wastech_orchestrator/core/flow/packaged/deep_research.yaml) ≈52/≈60): на исчерпании бюджета flow идёт по `accept`-ребру (→ publish) с остаточными расхождениями в разделе Open questions, **не** в `manual_action_required`.
- `critical_review` — `session_scope: resume_own_lineage` (≈58): помнит, что отмечал между раундами (durable sessions P2.2).
- `fact_verification` — двухслойная проверка: детерминированный `citation_check` (Layer 1, без LLM, P3.1) + агент-verifier (Layer 2).
- citation-loop pinned 1 (`citation_check → synthesis outcome:fail budget:1`, ≈73): неисправимая цитата → claim помечается unverified, не падение задачи.
- **Нет decomposition** ([flow-contract.md](flow-contract.md) §10: decomposition только implementation в v1; research линеен).

### Тесты

Из [profiles §16 research](../outdated/task_workflow_profiles.md#16-testing-requirements):

- `test_research_happy_path_produces_report_and_sources`.
- `test_research_non_blocking_exhaustion_publishes_with_open_questions` — исчерпание budget → publish, не manual.
- `test_critic_resume_own_lineage_across_rounds`.
- `test_research_external_research_optional_skip` — `when: config.external_research` false → узел пропущен, проход насквозь.
- `test_citation_loop_pinned_one`.

### Exit

Research-flow данными.

---

## P3.4 — `security_audit.yaml`

`scope → repository_analysis → dependency_scan → threat_analysis → finding_verification(verifier) → report → private_storage`; publishing `none`.

### Touchpoints

- Packaged [security_audit.yaml](../../../src/wastech_orchestrator/core/flow/packaged/security_audit.yaml) — исполняется через движок.
- Роли MD: `roles/audit/*.md`.

### Поведение

- `dependency_scan` (P3.1) — evidence, эмитит `pass`; ребро `dependency_scan → threat_analysis outcome:pass` (≈54).
- `finding_verification` — `blocking: false` (≈40): false-positives помечаются, не блокируют; `rework → threat_analysis budget:2`.
- `private_storage` — `publishing: none`, `output_policy: private_control_workspace_report` (P3.2): отчёт под `.worc/security-reports/`, git не трогается.

### Тесты

Из [profiles §16 audit](../outdated/task_workflow_profiles.md#16-testing-requirements):

- `test_audit_happy_path_writes_private_report`.
- `test_audit_repo_unchanged` — repo byte-for-byte после audit.
- `test_finding_verification_marks_false_positives_non_blocking`.
- **`test_codesign_all_three_flows_generic`** — финальный гейт: три flow исполняются на одной палитре; в движке нет `if task_type == ...`.

### Exit

Audit-flow данными; **co-design тест зелёный — абстракция доказана тремя примерами**.

---

## Сквозной обзор зависимостей P3

```text
P2 (полный evaluator + durable resume_own_lineage)
   ├─> P3.1 checkers (citation, dependency_scan)
   ├─> P3.2 policies (output/publishing/network + path-containment)
   │     └─> P3.3 deep_research.yaml ──┐
   │     └─> P3.4 security_audit.yaml ─┴─> co-design тест (финальный гейт абстракции)
```

P3.1 и P3.2 — параллельны (checkers ↔ policies); оба нужны обоим flow.

## Контракт выхода P3 → P4

- Абстракция доказана тремя flow данными — движок без доменного знания. P4 открывает ту же поверхность операторам (операторский flow = тот же контракт).
- Все политики/чекеры/потолки — ядровые; flow их только выбирает. P4 проверяет, что **операторский** flow не может их обойти.

## Пересечения для ревью (потенциальные противоречия)

- **`permission_ceiling: workspace-write` у research/audit** при read-only-намерении для repo. Разрешается двумя слоями (sandbox разрешает запись, `output_policy` + path-containment ограничивают путь). Убедиться, что after-stage guard (P3.2) — единственный механизм, отделяющий «можно писать в research-dir» от «нельзя трогать src». Это не должно быть выражено новым видом узла или спец-кейсом движка.
- **`blocking: false` семантика на исчерпании** (research/audit) vs **`manual_action_required` на исчерпании** (implementation). Разница — свойство узла (`blocking`), не движка: движок на исчерпании budget-ребра у `blocking:false`-evaluator идёт по `accept`-ребру; у `blocking:true` — в manual. Анкерить тестом `test_research_non_blocking_exhaustion_publishes_with_open_questions`.
