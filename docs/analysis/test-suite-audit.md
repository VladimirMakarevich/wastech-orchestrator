# Аудит тестового набора (test suite audit)

Дата начала: 2026-06-26. Автор: автономный проход Claude Code.

## Цель

Пройтись по всем тестовым файлам приложения и для каждого оценить:

- **Покрытие**: непокрытые участки кода или хрупкие места (fragile tests, неустойчивые к рефакторингу).
- **Актуальность**: устаревшие тесты или «тесты ради тестов» (низкая ценность, тавтология, дублирование, проверка моков вместо поведения).

## Методология

- Один тестовый файл за итерацию.
- Для каждого: читаю тест + соответствующий исходник, фиксирую находки.
- Категории находок:
  - 🔴 **Пробел** — важное поведение не покрыто / слабая проверка.
  - 🟡 **Хрупкость** — тест ломается при безобидном рефакторинге (привязка к внутренностям, строкам логов, порядку).
  - 🔵 **Низкая ценность** — тест ради теста, тавтология, дублирование, проверка мока.
  - 🟢 **OK** — тест адекватен.

## Прогресс

Всего тестовых файлов: **101**. Обработано: **0**.

---

## Сводка находок

(заполняется по ходу)

---

## Детальные результаты по файлам

### tests/check/test_check_runner.py → src/.../check_runner.py

Оценка: 🟢 **Сильный файл.** 18 тестов, реальное поведение через инъекцию `_FakeProc`/`which`/`monotonic`. Покрыты: vacuous pass, run-all без fail-fast, timeout vs launch-failure vs quality-failure, skip_if_unavailable (absent/present/partial), per-set timeout override, per-command cwd, argv без shell, selected=None→config, лог не перезаписывается, subtask-префикс, редактирование секретов в логе.

Находки:

- 🔵 Низкая ценность (мелочь): `test_argv_no_shell` дублирует то, что и так проверяется в других тестах через `fake.calls[0]["argv"]`; ценность как отдельного теста невелика, но он документирует инвариант — оставить.
- 🔴 Пробел: не покрыт набор с **несколькими checks внутри одного set** (`for check in cset.checks` гоняется только по 1 элементу везде). Стоит добавить тест на set с 2+ командами (порядок индексов/логов).
- 🔴 Пробел: футер лога `_append_stderr` (`timed_out: true`, `launch_error: ...`) не проверяется — в `test_timeout_is_quality_failure` и `test_required_launch_failure_is_infra` содержимое лога не читается. Стоит добавить assert на футер.
- 🟡 Хрупкость (приемлемая): `test_check_logs_*` привязан к точным строкам структурного лога (`command=npm`, `passed=true`, `duration_seconds=2.5`). Это и есть контракт лог-формата, но при смене формата сломается — допустимо.
- Непокрыто: реальный `run_with_heartbeat` (heartbeat_seconds=0 глушит) — покрывается в observability-тестах.

### tests/checks/test_checks_model.py → src/.../checks/model.py

Оценка: 🟢 **Сильный.** Чистые функции, нормализация (string/mapping/spec/ResolvedCheck), деривация имени из argv0 (POSIX/Windows basename), paths/timeout/skip/cwd, безопасность relpath, shell-метасимволы, denied-prefix. Хорошо подобранные негативы (`raises`).

Находки:

- 🔴 Пробел: `is_safe_relpath` — не покрыта ветка Windows-абсолютного пути (`C:\Windows`, последний `return not PureWindowsPath(...).is_absolute()`). Добавить кейс.
- 🔴 Пробел: `normalize_check_command` passthrough для уже-`ResolvedCheck` (строки 98-99) не тестируется.
- 🔵 Мелочь: `argv_matches_denied` — нормализация пробелов в denied-записи (`"git  commit"`) не проверена отдельно (хотя `.split()` это делает).
- Непокрыто: fallback `_name_from_argv` `... or head` (пустой basename).

### tests/checks/test_selection.py → src/.../checks/selection.py

Оценка: 🟢 **Сильный, чистая функция.** Покрыта вся трёхзначная логика: `None`→всё, `[]`→ничего, matched-subtree+always-on, md-only, unmatched→только always-on, unmatched без catch-all→ничего, union нескольких, пустой список sets. Комментарии фиксируют исторические корректировки (антирегресс). Glob-матчер вынесен в отдельный тест — правильно.

Находки:

- 🔵 Мелочь: дедуп по имени (`seen`) не покрыт — ни в одном кейсе нет двух sets с одинаковым именем. Ветка `cset.name not in seen` фактически всегда true. Низкий приоритет (валидация имён, вероятно, в config).

### tests/config/test_command_sets.py → config loader+validation (checks.command_sets)

Оценка: 🟢 **Сильный.** Happy-path монорепо + полный набор негативов семантической валидации: cwd-traversal, denied-команда, shell-метасимвол, пустой commands, timeout<=0, пустое имя set, толерантность к устаревшим ключам discovery/commands (fail-open).

Находки:

- 🔴 Пробел: валидируется только `cwd` на traversal; не проверяется валидация самих `paths` (напр. абсолютный/`..` glob в `paths`). Уточнить при чтении validation.py — возможно, дыра в самом коде, а не только в тесте.
- 🔵 Мелочь: `_issues` ловит только `ConfigError`; если валидатор когда-то начнёт бросать другой тип, тесты молча станут зелёными на ложном основании — приемлемо.

### tests/config/test_config_schema_version.py → config loader (schema_version gate)

Оценка: 🟢 **Сильный, фокусный.** Newer→refused, current/absent→load, non-int→rejected, packaged-пример декларирует и грузится. Использует `match=` на сообщениях (документирует контракт).

Находки:

- 🔵 Пробел (вероятно покрыт в test_upgrade.py): older schema_version (миграция вверх) здесь не тестируется — что ожидаемо, разделение ответственности.

### tests/config/test_loader.py → config loader

Оценка: 🟢 **Очень полный.** ~50 тестов: fail-closed структура (non-mapping/empty/unknown-key), толерантность к legacy-ключам (skip_stages, allow_review_skip, max_budget_usd, routing, auto_merge_allow_per_task, prompts-блок), max_turns (дефолт/sentinels none/max/null/case-insensitive/0/строка), auto_mode, poll_interval, footprint, checks command_sets+timeout, deletion_exempt_paths, reasoning-уровни, auto_merge\*, prompt_audit, denied gh pr merge, «все issues собираются».

Находки:

- 🟡 **Дефект имени теста (стейл):** `test_checks_timeout_defaults_to_1800` ассертит `== 7200` (строка 185). Имя лжёт — дефолт менялся (1800→7200), имя не обновили. Это «тест, который вводит в заблуждение читателя». Переименовать в `..._defaults_to_7200`. (Подтверждает, что значение по умолчанию таймаута = 7200, не 1800 — расхождение и с docstring соседнего файла? нет, тот про per-set.)
- 🔵 Мелочь: множество `from ... import ProviderId` внутри функций (строки 288, 301, 323) — дубль уже импортированного на уровне модуля (строка 12). Косметика, не влияет на надёжность.
- 🟢 Хорошие негативы с проверкой текста issue — устойчивы и информативны.

### tests/config/test_roundtrip.py → packaged config.example.yaml

Оценка: 🟢 **Ценный drift-guard.** Проверяет, что упакованный пример не теряет дефолтных denied_commands/allowed_environment (REPLACE-семантика — операторская ловушка) и грузится+валидируется без warnings. Прямо завязан на реальный инцидент с `USER`/`gh pr merge` (см. память).

Находки:

- 🟡 Хрупкость (намеренная): импорт приватных `_DEFAULT_ALLOWED_ENV`/`_DEFAULT_DENIED_COMMANDS` — сломается при переименовании. Для drift-guard оправдано, но стоит экспортировать их публично, раз на них завязан контракт.

### tests/config/test_upgrade.py → config/upgrade.py

Оценка: 🟢 **Сильный.** Семантика deep-merge: add missing top-level/subkey, never-overwrite-leaf, preserve operator-only keys, list verbatim, schema_version stamping, идемпотентность, packaged-template self-idempotent, и весь набор strip-legacy (discovery/commands, prompts, skip_stages, allow_review_skip, max_budget_usd по обоим провайдерам), render↔parse round-trip.

Находки:

- 🔴 Пробел: `render()` проверяется только на `startswith("# Regenerated by")` + round-trip; нет проверки сохранения комментариев/порядка ключей (если render это гарантирует). Также `parse_mapping` не тестируется на битом YAML.
- 🟢 Каждый strip-тест проверяет ещё и сохранение соседних operator-значений — защита от over-stripping. Отлично.

### tests/config/test_validation.py → config/validation.py

Оценка: 🟢 **Сильный, не хрупкий.** Строит невалидные конфиги через `dataclasses.replace` (а не YAML-строки) — устойчиво к рефакторингу парсера. Покрыто: global-primary (нет/несколько/не в allowed), max_total<max_fix_cycles, max_subtasks<2, запрет sandbox-bypass флагов (parametrize), full-access sandbox теперь НЕ config-ошибка (граница ответственности с preflight), claude skip-permissions, reasoning minimal (codex ok / claude reject / supervisor), poll_interval, telegram timeout/env-имена (parametrize).

Находки:

- 🔵 Уточнено (гипотеза снята): `paths` валидируются только на непустоту (validation.py:207-209), traversal/absolute НЕ отклоняется. **НО это не дыра** — `paths` используются лишь как glob для матчинга против repo-relative diff-путей (selection.py), а не для доступа к ФС; `/etc/**` или `../**` просто ни с чем не сmatчатся. `cwd` (который реально джойнится к ФС) валидируется через `is_safe_relpath`. Тестовый пробел безвреден. Финдинг закрыт.
- 🟢 Использование `replace()` + проверка текста issue — образец для остальных config-тестов.

### tests/core/test_cli_finalize.py → cli finalize

Оценка: 🟢 **Сильный интеграционный.** Реальный git-репозиторий (фикстуры `git_repo`/`git_run`), сидинг StateStore+ledger. Покрыто: failed/done/abandoned → коды выхода 1/0/2, ledger-запись (manual=true), источник pr_url (recorded publish op / verify_pr_state MERGED/OPEN), warn без URL, fail-closed на dirty-tree, --delete-branch, отказ при работающем daemon, идемпотентность (already finalized), dry-run ничего не пишет.

Находки:

- 🟡 Стейл-конфиг в тесте: `_write_config` пишет `checks:\n  commands: []` (строка 47) — это удалённый в v15 ключ, держится лишь на fail-open толерантности. Стоит обновить на `command_sets: {}`, иначе тест опирается на legacy-поведение, которое может быть убрано.
- 🔵 Мелочь: `verify_pr_state` мокается lambda — не проверяется, что finalize реально дёргает gh при отсутствии `--no-verify-pr` и реальном URL (мок прозрачен). Приемлемо для unit.
- 🟢 Маппинг кодов выхода на статус — ценная проверка контракта CLI.

### tests/core/test_cli_pipeline.py → cli run/watch

Оценка: 🟢 **Очень сильный, многоуровневый.** Unit (fake orchestrator): watch_once auto on/off, manual блокирует продолжение, resume-manual блокирует, dependency-gating (WAITING→skip+later independent runs, BROKEN→reject не съедает слот), watch_loop (refresh каждый тик + sleep между, poll=0→один проход). E2e через `main()` + `fake_cli` + реальный git: happy-path (структура коммитов, ledger, lifecycle-папка, маркеры operator-лога), in-repo audit footprint (раздельные коммиты feat/audit, `.worc/` не в git), status, rejected-task quarantine (frontmatter_missing), отказ при unmerged-dependency (код 2, без side-effect).

Находки:

- 🟡 Стейл-конфиг: тот же `checks:\n  commands: []` (legacy v15-ключ). Системно по нескольким core-тестам — вынесу в сводку.
- 🟡 Хрупкость (приемлемая): e2e ассертит точные строки маркеров (`"branch preparation started"` и т.д.) и subject-префиксы коммитов (`feat(task-300)`, `audit trail for task-300`) — контракт, но ломается при рерайте сообщений.
- 🟢 Тесты dependency-gating проверяют ещё и что слот не простаивает/не съедается — реальное поведение, не моки.

### tests/core/test_cli_rerun.py → cli rerun

Оценка: 🟢 **Сильный.** Реальный git + fake_cli. Покрыто: real-failure→flow-checkpoint, fresh failed→done (линковка ledger attempt=2/rerun_of, архивация attempt-1/, ребилд ветки от base), guards (unknown id, non-terminal/running, daemon running), резолв файла по id через lifecycle-папки (moved→находит, ambiguous→refuse+называет оба, truly-missing→refuse), dry-run ничего не пишет, --continue (refuse без checkpoint, revive+delegate в resume со сбросом terminal-маркеров и сохранением branch/fix_iterations).

Находки:

- 🟡 Стейл-конфиг: снова `checks:\n  commands: []` (системно).
- 🔵 `test_rerun_continue_revives_then_delegates_to_resume` мокает `Orchestrator.resume` — проверяет только делегирование + ревайв строки, но не реальный resume (он покрыт в test_recovery). Корректное разделение.
- 🟢 Тесты резолва файла по id (moved/ambiguous/missing) — отличное покрытие реального операторского сценария.

### tests/core/test_dangerous_diff.py → core/dangerous_diff.py (allowlist)

Оценка: 🟢 **Образцовый фокусный unit.** Чёткое разделение: базовое поведение классификатора — в test_hitl, здесь только allowlist. Покрыты тонкие edge-cases: exempt md-deletion, дефолтный gating без exemptions, mixed (гейтится только non-exempt), **dependency-манифест не exemptable даже под `**`** (остаётся risk=dependency), rename-away exempt, rename source→md гейтит исходное удаление, `exempted_deletions` репорт + пустой allowlist.

Находки:

- 🟢 Нет существенных пробелов. Rename-edge-cases (previous_path) — именно то, что обычно забывают. Отлично.

### tests/core/test_decomposition.py → core/decomposition.py

Оценка: 🟢 **Сильный.** Все reason-коды покрыты (gate-off, accepted, not-recommended×2 (decompose:false / None), n<2, n>max, malformed, forward-dep, self-dep, non-sequential orders). Артефакты: write+update index, immutability spec.md при повторной записи, rejected→ничего не пишет.

Находки:

- 🔴 Пробел: `depends_on` со ссылкой на несуществующий order (напр. `[5]` при n=2) или `[0]`/отрицательный — не покрыт; неясно, классифицируется ли как NON_LINEAR или MALFORMED. Добавить кейс.
- 🔵 Пробел: `update_subtask_index` для неизвестного order — поведение (no-op / ошибка) не зафиксировано.
- 🔵 Дублирующиеся `slug` между подзадачами (коллизия имён файлов `NN-slug.md`) — не проверено.

### tests/core/test_documentation_node.py → packaged implementation flow (documentation node)

Оценка: 🟢 **Хороший shape-test.** Пинит граф packaged-flow: загрузка+валидация, node-атрибуты (WORKSPACE_WRITE, role_file, нет artifact/hitl/when), routing-soundness (review→accept→documentation→publish, rework→fixing, publish единственный терминал), resume EDITING_LINEAGE с affinity=implementation (как fixing), запуск один раз после декомпозиции (вне sub_flow region), per-task disable не ломает routing. Рендер role-промпта — нет утечки незаменённых токенов.

Находки:

- 🟡 Хрупкость (намеренная и оправданная): тесты привязаны к точной структуре `implementation.yaml` (edges, sub_flow tuple). При легитимном рестракте flow придётся править — но это и есть защита инвариантов графа. Хорошие cross-references на смежные тесты исключают дублирование.
- 🟢 Без тавтологий; каждый assert защищает поведенческий инвариант.

### tests/core/test_flow_checkers.py → core/flow/checkers/{citation,dependency_scan}

Оценка: 🟢 **Сильный, двухслойный.** Чистый валидатор + node-dispatch. Citation: verified, hallucinated, snippet-mismatch, external-url→uncheckable-pass, malformed JSON→no-crash+uncheckable, missing→pass, path-traversal→broken, node pass/fail + артефакт citation.json. Dependency_scan: argv+timeout+env (никаких shell-строк), «evidence-not-gate» (vulns exit=1 И отсутствующий бинарь оба→passed), node записывает по строке на сканер.

Находки:

- 🔵 Пробел: citation с указанием `line` за пределами файла (EOF+1) или несовпадение line при совпадающем snippet — не покрыто; проверяется только snippet-content.
- 🟢 Инъекция fake `run_process` с `_default_unused_runner`, который бросает при вызове citation — элегантная проверка, что citation не запускает процессы. Хороший приём.

### tests/core/test_flow_contracts.py → core/flow/contracts.py

Оценка: 🟢 **Сильный, без воды.** YAML-safe enum-values (защита от YAML 1.1 boolean-ловушек on/off/yes/no/~), канонические значения enum, ExecutionUnit root/subtask, fingerprint (детерминизм + независимость от порядка ключей + чувствительность к payload + hex/64). Полная таблица истинности `resolve_network_access` (4 кейса: node-grant поверх policyless, node-optout бьёт granting-flow, наследование granting/policyless).

Находки:

- 🟢 Образцовый contract-test: truth-table полностью покрыта, fingerprint-инварианты строгие. Пробелов нет.
- 🔵 Мелочь: `test_canonical_enum_values` проверяет лишь подвыборку членов каждого enum — при добавлении нового члена с YAML-небезопасным значением поймает `test_enum_values_are_yaml_safe`, так что ок.

### tests/core/test_flow_deep_research.py → packaged deep_research flow via FlowEngine

Оценка: 🟢 **Высокоценный интеграционный.** Гоняет РЕАЛЬНЫЙ packaged-снапшот через реальный FlowEngine + реальные checks/evaluator-раннеры (citation, non-blocking critic self-cap, resume_own_lineage), фейки только agent/publish/hitl. Покрыто: citation-loop budget=1, external_research optional skip, happy-path (report.md+sources.json), non-blocking critic self-cap→publish DONE (не manual, ровно 3 rework), broken-citation→bounded rework→MANUAL, critic resume_own_lineage между раундами (session_id переносится).

Находки:

- 🟡 **Системная хрупкость (фейк-дрейф):** `_Store`/`_Router` — duck-typed с `# type: ignore[arg-type]`. mypy не проверяет соответствие фейков реальным протоколам `NodeServices`/`StateStore`. Если реальный интерфейс расширится новым методом, который движок начнёт звать, фейк молча не сломается на типах — тест может остаться зелёным на устаревшем контракте. Общая черта всего flow-suite — вынесу в сводку как рекомендацию (Protocol-классы + проверяемые фейки).
- 🟢 Драйв реального графа без `if task_type` — именно то, что должно тестироваться у engine-движка.

### tests/core/test_flow_engine.py → core/flow/engine.py

Оценка: 🟢 **Образцовый, ядро движка.** Графы строятся напрямую из schema-dataclasses (точная форма под каждый тест). Покрыто: следование рёбрам, resume в current_node (prior nodes не перезапускаются), undeclared outcome→EngineInternalError, **transitions-not-nodes** (runner-hijack current_node игнорируется — инвариант «движок владеет переходом»), when-skip берёт единственное ребро, post-node hook только для исполненных узлов, region-termination на границе, инкремент fix_iterations, budget-exhaustion (named loop), global-cap hard-stop, inline-budget allows N, inline-budget reset после forward-edge.

Находки:

- 🟢 Без пробелов и тавтологий. `HijackRunner` (попытка перехватить current_node) — отличная защита инварианта ceiling. Чёткое различение named-loop / global-cap / inline-budget — три разных лимита проверены раздельно.
- 🔵 Мелочь: `EngineInternalError` проверяется только для checks-узла; для evaluator/agent с undeclared outcome не дублируется — приемлемо (логика общая).

### tests/core/test_flow_engine_driver.py → core/flow/engine_driver.py (drive_flow)

Оценка: 🟡 **Тонко для драйвера.** Один happy-path: tiny flow (impl→testing→review→publish) через `drive_flow` с реальным `StateStoreRunRecorder` поверх настоящего SQLite + фейк-router/checks/git. Проверяет node_runs-запись и checkpoint (terminal node + fingerprint). Это хорошая «проводка», но...

Находки:

- 🔴 Пробел: специфичная для драйвера логика **декомпозиции** (прогон sub_flow по подзадачам + post-region фаза) здесь НЕ покрыта — только no-decomposition. `partition_decomposition` тестируется в test_documentation_node, но end-to-end драйв декомпозированного потока через `drive_flow` (region→post-region переход, per-subtask повторы) надо подтвердить в test_orchestrator/test_implementation_parity_flow. Если там его нет — реальный пробел в самой тяжёлой ветке драйвера.
- 🔴 Пробел: нет драйв-теста на fail/rework и terminal MANUAL через `drive_flow` (только через FlowEngine напрямую). Поведение recorder/checkpoint при не-DONE-исходе не проверено на этом уровне.
- 🟡 Та же фейк-дрейф проблема (router/checks/git без Protocol).

### tests/core/test_flow_node_runners.py → core/flow/nodes/\* (все раннеры)

Оценка: 🟢 **Самый сильный файл suite (1500+ строк).** Исчерпывающее поведенческое покрытие всех раннеров: agent (сборка request, матрица network_access, durable-сессии fresh_disposable/editing_lineage/affinity, infra-exhaustion→NodeInfraError, workspace-write diff, dangerous-diff guard: ask/approve/deny-reconsider-clean/deny-still-dangerous/exempt/non-exempt, read-only пропускает guard, prompt_audit on/off, embedded HITL round-trip + data-driven dispatch + timeout), standalone HITL (approve/deny/question/timeout/no-notifier/resume-persisted), evaluator (severity-mapping parametrize, medium non-blocking+carried, test_quality rework/self-cap/read-only/findings-artifact, review=ordinary-evaluator), checks (pass/fail/launch→manual/all-skipped→manual/partial-skip/empty-diff-vacuous/committed-when-clean, mutation-guard active/clean/no-checks-flow), publish (PR-sequence, requires-branch, requires-body, finalize-body, none-policy, git-fail-after-finalize→manual+failed-recorded).

Находки:

- 🟢 Эталон. Каждый тест пинит реальный инвариант, кросс-ссылки на спеку (P2.4, F1/MC2, #8). Negative-paths и exhaustion-семантика покрыты так же тщательно, как happy-path.
- 🟡 Та же системная фейк-дрейф (duck-typed `FakeStore`/`FakeGit`/`FakeRouter` с `# type: ignore`). Учитывая объём раннеров, завязанных на эти фейки, это главный системный риск suite.
- 🔵 `test_evaluator_maps_blocking_findings` parametrize не покрывает severity=`critical` в маппинге (есть отдельно в test_quality); и нет кейса с несколькими findings разной severity (берётся max?). Мелочь.
