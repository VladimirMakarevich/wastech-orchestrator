# P1.4 — Cutover на движок (orchestrator wiring) — DESIGN DRAFT

Статус: **черновик дизайна, не закоммичен, к ревью**. Дата: 2026-06-18. Владелец: Vladimir Makarevich.

Дизайн финального шага P1: вплести `FlowEngine` в `run_task`/`resume` **как драйвер** (заместив `_drive`), выразив оставшуюся per-stage пост-обработку и decomposition данными во Flow, и удалить legacy. Узловой слой готов (9 коммитов: все виды узлов + durable HITL/approval + Step-0 правки).

> **ОТМЕНА golden-harness (greenfield).** Прода/релиза нет ⇒ доказывать паритет байт-в-байт с `_drive` не нужно — это migration-машинерия для миграции, которой нет. **Цель: не потерять ни одной возможности, переписав их под Flow.** Поэтому: **прямой cutover** (а не dual-run), а гарантия «ничего не потеряли» — **адаптация существующего интеграционного тест-сьюта на движок** + явный чек-лист возможностей (§6). Бывшие P1.4 (паритет) и P1.5 (удаление legacy) **сливаются в один cutover-шаг** (два драйвера держать незачем).

## 1. Что осталось переписать

Узлы-обёртки запускают капабилити, но legacy `_drive` делает между запусками агентов **пост-обработку**, которой обёртки пока не делают. Её надо выразить во Flow:

- **refinement** (`_refinement` ≈1046): из вывода агента пишется enriched-spec (`p.enriched_path`).
- **planning** (`_planning` ≈1065): пишется план (`p.plan_path`); резолвятся skills (`p.selected_skills`); решается decomposition (`decide_decomposition` → `DecompositionDecision`); пишутся subtask-spec файлы; подзадачи персистятся.
- **summary** (`_summary` ≈1295): из вывода агента пишется `summary.md`/`summary.json` (тело PR).
- **decomposition fan-out** (`_run_units_and_finish` ≈1030 + `_run_unit` + `_on_review_passed`): под-flow гоняется на каждую подзадачу, коммит на подзадачу, сброс per-subtask счётчиков, общий `fix_iterations` накапливается.

Две задачи: **(A) пер-стейдж пост-обработка/артефакты** и **(B) decomposition fan-out** (контрол-флоу, которого у движка нет). Цель — переписать чисто (data-driven), а не воспроизвести байт-в-байт.

## 2. Проблема A — пер-стейдж пост-обработка (data-driven, БЕЗ хардкода стадий)

**Принцип:** движок не знает доменных ролей. Доменное знание живёт в **(а) данных Flow** и **(б) задокументированных контрактах вывода**; **ядро владеет механиками**, но включает их **по объявленным данным**, не по `if stage == planning` (тот же принцип, что git/publish).

- **Артефакт-слоты.** Новое опц. поле на агент-узле `output_artifact: <slot>` (`enriched_spec` / `plan` / `summary` — словарь слотов = существующий allowlist prompt-переменных). После узла с `output_artifact: X` ядро generic-механизмом пишет структурированный вывод агента в слот `X`, регистрирует артефакт и прокидывает путь downstream (`{plan_path}` и т.п.). Никакой роль-логики — «вывод агента → объявленный слот». Слоты `diff`/`checks`/`review` уже так и заполняются (guard / checks / evaluator).
- **Decomposition — через данные.** Flow уже объявляет `decomposition.proposed_by: <node_id>` (любой узел) + `sub_flow` + `gate`. Ядро: после узла `proposed_by` читает из его `structured_output` **контрактные поля** `decompose`/`subtasks` (контракт задан `output_schema` узла), применяет числовой gate, фан-аутит. Имя «planning» нигде не сравнивается — цепляемся к `proposed_by` + контракту вывода.
- **Skills.** Опциональное контрактное поле `skills` в выводе planning-узла; ядро резолвит против инвентаря, если поле есть. Generic-механизм, не привязка к имени стадии.
- **Где живёт механика.** Ядро (не движок) владеет: запись артефакта-слота, gate decomposition, запись subtask-спеков, резолв skills. Триггер — объявленные данные Flow + контракт вывода. Извлекаем эти куски из `_refinement`/`_planning`/`_summary` в переиспользуемые core-функции, но зовём их **generic-образом по данным узла**, а не stage-keyed диспетчером.

**Отклонено:** `StageOutputHandler`, диспетчеризуемый по `Stage` (reuse-костыль ради паритета). Без паритета он не нужен и противоречит абстракции (возвращает хардкод стадий). Пишем data-driven сразу.

## 3. Проблема B — decomposition fan-out

Граф один и связный (`refinement→planning→[sub_flow]→summary→publish`); decomposition-блок задаёт **регион**. **Фан-аут на уровне `engine_driver`**, движок остаётся «гонит граф/регион до выхода»:

1. Драйвер партиционирует снапшот: **pre** (entry..`proposed_by`), **sub_flow** (узлы региона с внутренними петлями), **post** (до терминала).
2. Гонит **pre** (refinement→planning); из контракта вывода planning получает `DecompositionDecision`.
3. units = подзадачи или `[None]`; на каждую гонит **sub_flow**-регион проходом движка; между подзадачами `reset_for_next_subtask` + `commit_subtask` + персист.
4. После всех units — **post** (summary→publish).

**Расширение движка:** `FlowEngine` получает опц. `region: frozenset[str]` + `entry_override` и терминирует при ребре наружу региона (минимальная добавка к `run()`).

**Де-риск:** сначала cutover на НЕ-decomposed сценариях (units=`[None]`, граф целиком одним проходом), потом fan-out отдельным слайсом.

## 4. Билдер `_Pipeline` → `NodeServices`/`NodeInputs`

- Новый `core/flow/wiring.py`: `build_node_services(...)` (router/checks/git/notifier/store/repo_dir/artifacts_root/clock/ask_timeout_s + `stage_for_node`) и `build_node_inputs(p, flow_dir, ...)` (task/plan/diff/checks/review/skill paths, branch, pr_title, summary_body_path, contacts, session_ids).
- **`node_id→Stage` карта** остаётся, но **только как routing-данные** (роутер пока маршрутизирует по `Stage`). Это НЕ для нормализации статусов (она отменена вместе с harness). Живёт в реестре/диспетчере; уходит, когда роутинг станет node-based.

## 5. Cutover (бывшие P1.4 + P1.5 — один шаг)

- `run_task`/`resume`: вместо `_drive` — резолв снапшота (`FlowRegistry`/фикстура) → `wiring` строит services/inputs → `engine_driver.drive_flow` (+ decomposition-обвязка §3) → мап `FlowRunResult`→`PipelineResult`. **Обязательно:** ловить `NodeInfraError`→FAILED / `NodeManualRequired`→MANUAL (движок их пробрасывает) и писать начальный чекпоинт (fingerprint + entry) **до** entry-узла (иначе resume краша-на-entry = свежий запуск).
- **Удалить legacy в том же шаге:** `_drive`/`_run_unit`/`_enter_fixing`/`_after_edit_target`/dispatch-on-status; гранулярные статусы + их `ALLOWED_TRANSITIONS`; `stage_runs`; `Stage`-как-конвейер (раскладка `stages/<stage>` → `node_id`). `LoopController` (legacy) — туда же, движок уже несёт свою budget-логику.

## 6. Гарантия «ничего не потеряли» (вместо golden-harness)

**Адаптировать существующий интеграционный сьют** (fake-CLI, skill `fake-cli`) на движок: тесты проверяют НОВУЮ модель (`node_runs`, `RUNNING`), а не совпадение со старым драйвером. **Чек-лист возможностей — каждая = ≥1 зелёный сценарий:**

- refinement(opt, HITL) + enriched-spec; planning(HITL вопрос+approval) + plan + skills + decomposition-proposal
- decomposition: gate (min/max, linear-depends), fan-out, commit-per-subtask, общий `fix_iterations`, сброс per-subtask счётчиков
- implementation/fixing + diff; dangerous-diff guard (deletion/dependency) + approve/deny + reconsider
- testing(checks): discovery, approve_command_changes, mutation guard, launch-fail = инфра-путь
- review(evaluator): блокирующие findings → fixing
- fix-петли: test_fix / review_fix / глобальный cap → manual + failure-report
- summary(opt) + summary.md/json; publish: commit_code + commit_audit (finalize/перемещение task-файла) + push + create_pr + auto_merge; идемпотентность `publish_operations`
- HITL durable (persist/resume/timeout fail-closed); recovery/resume (single-slot, decomposed resume-point, cleanup); rerun fresh + `--continue`
- skip-stages (testing/review/summary/fixing); prompt_audit / logging / heartbeat / redaction; periodic git sync; telegram

`test_no_drive_symbol` (grep-guard) — `_drive`/`_run_unit`/`_enter_fixing` отсутствуют после cutover.

## 7. Recovery-диспетчеризация

`RecoveryReconciler` обобщается **напрямую** на node-based (без двойной ветки legacy/engine — legacy удалён): resume = `hydrate_run_state` (готово) + продолжить с `current_node`; decomposed-resume = первая подзадача без верифицированного коммита (через `node_runs` + `publish_operations`); `rerun --continue` → оживить на `current_node`.

## 8. Последовательность слайсов

1. `core/flow/wiring.py` (builder + routing-карта).
2. Data-driven пост-обработка (§2): `output_artifact`-слоты + извлечение core-функций (enriched/plan/summary write, decomposition gate, skills resolve) + чтение контракта вывода по `proposed_by`.
3. Вплетение движка в `run_task` как драйвера (НЕ-decomposed путь) + `NodeInfraError`/`NodeManualRequired`→терминал + начальный чекпоинт.
4. **Адаптация интеграционного сьюта** на движок (НЕ-decomposed: happy-path, fix-петли, HITL, dangerous-diff, skip-stages, manual). ← первый зелёный gate.
5. Региональный движок + decomposition fan-out (§3) + decomposed-сценарии.
6. recovery/resume/rerun + обобщение `RecoveryReconciler`.
7. **Удаление legacy** (`_drive`/гранулярные статусы/`stage_runs`/`Stage`-конвейер/legacy `LoopController`) + `test_no_drive_symbol`.
8. (опц., низкий приоритет) standalone `hitl`-узел; prompt_audit/heartbeat.

Замечание по §2.2-находкам ревью (по ходу): `_reset_loops_at` сделать полным (review-pass сбрасывает обе петли — проявится в fix-петлевых сценариях шага 4); глобальный `fix_iterations` на supervisor-budget рёбрах — by-design (P2-заметка).

## 9. Открытые развилки для ревью

1. **`output_artifact` как отдельное поле узла, или generic «вывод агента → `<node_id>.json`, downstream ссылается по node_id»?** (Рекомендация: явный `output_artifact`-слот — минимально, сохраняет существующий словарь prompt-переменных.)
2. **Движок-регион**: `FlowEngine.run(region=…, entry_override=…)` vs отдельный `run_region()` vs весь фан-аут в `engine_driver`? (Рекомендация: минимальный `region`-параметр + оркестрация в `engine_driver`.)
3. **`node_id→Stage` для роутинга** — оставить до node-based роутинга, или сразу перевести роутинг на узлы? (Рекомендация: оставить временно, перевод роутинга — отдельный слайс/P4.)
4. **Контракт вывода decomposition/skills** — фиксируем как часть `output_schema` узла (данные), не как доменную ветку. Подтвердить формулировку контракта (`decompose: bool`, `subtasks: [...]`, опц. `skills: [...]`).
