# Реестр функциональных блоков

Статусы: `discovered` — обнаружен, не исследован; `in-progress` — анализируется; `documented` —
исследован и задокументирован; `needs-review` — поведение нельзя восстановить однозначно; `excluded`
— рассмотрен, но не самостоятельный блок.

Все 27 функциональных блоков (B01–B27) исследованы и имеют статус `documented`. Каждый блок описан в
отдельном файле в `blocks/` и подтверждён ссылками на исполняемый код и тесты.

> Помимо блоков есть слой **потоков исполнения** — документы `S01`–`S08` (по стадии конвейера) и обзор
> в [flows/coding/index.md](./flows/coding/index.md). Они описывают «что происходит на шаге» и
> ссылаются на блоки. Рядом в будущем появятся другие потоки (`flows/deep_research/` и т. п.).

---

## Интерфейс и управление запуском

### B01 — CLI и операторские команды

- **Назначение:** разбор аргументов, диспетчеризация подкоманд, коды возврата; тонкие драйверы
  команд `run`, `status`, `preflight`, `telegram-test`, `rerun`, `finalize`.
- **Точки входа:** [cli.py main](../../src/wastech_orchestrator/cli.py#L1497), [build_parser](../../src/wastech_orchestrator/cli.py#L114), `cmd_run`/`cmd_status`/`cmd_preflight`/`cmd_telegram_test`/`cmd_rerun`/`cmd_finalize`.
- **Зависимости:** B06 (оркестратор), B05/B04 (загрузка и обнаружение конфигурации), B07 (read-only `status`), B25 (изоляция в preflight), B26 (telegram-test/preflight).
- **Статус:** `documented` · [файл](./blocks/B01-cli-and-operator-commands.md)

### B02 — Демон watch и планирование задач

- **Назначение:** периодическое обнаружение pending-задач и подача их в оркестратор по одной;
  демонизация с PID-файлом, грейсфул-остановка по `SIGTERM`, защита от второго демона.
- **Точки входа:** [cmd_watch](../../src/wastech_orchestrator/cli.py#L1160), `cmd_stop`, `cmd_restart`, [watch_loop](../../src/wastech_orchestrator/cli.py#L807)/[watch_once](../../src/wastech_orchestrator/cli.py#L778); [process_control.py](../../src/wastech_orchestrator/process_control.py).
- **Зависимости:** B06 (`resume`, `acquire_slot`, `run_task`, `refresh_repo`), B05.
- **Статус:** `documented` · [файл](./blocks/B02-watch-daemon-and-scheduling.md)

### B03 — Установщик и развёртывание проекта

- **Назначение:** `init` (скелет каталогов/шаблонов/worc/config), `install` (мастер → генерация
  валидной `config.yaml` → привязка), `upgrade-config`/`upgrade-docs`/`install-templates`.
- **Точки входа:** [cmd_init](../../src/wastech_orchestrator/cli.py#L464), [cmd_install](../../src/wastech_orchestrator/cli.py#L1430), [install/wizard.run_wizard](../../src/wastech_orchestrator/install/wizard.py), [install/config_writer.build_and_validate](../../src/wastech_orchestrator/install/config_writer.py), [install/detect.py](../../src/wastech_orchestrator/install/detect.py).
- **Зависимости:** B05 (валидация сгенерированного конфига), B04 (привязка repo→config), B19 (git-пробы), B25 (denied-команды в дефолтах).
- **Статус:** `documented` · [файл](./blocks/B03-installer-and-scaffolding.md)

### B04 — Реестр привязок и обнаружение конфигурации

- **Назначение:** персистентный стор `repo-root → config.yaml` и разрешение пути конфигурации по
  приоритету (`--config` → `./config.yaml` → привязка реестра).
- **Точки входа:** [install/registry.py](../../src/wastech_orchestrator/install/registry.py) (`bind`/`lookup`/`unbind`), [cli.resolve_config_path](../../src/wastech_orchestrator/cli.py#L549).
- **Зависимости:** `platformdirs`, B03 (запись привязки при `install`).
- **Статус:** `documented` · [файл](./blocks/B04-install-registry-and-config-discovery.md)

### B05 — Конфигурация: схема, загрузка, валидация, апгрейд

- **Назначение:** типизированная модель конфигурации, парсинг YAML (fail-closed), семантическая
  валидация (§11/§21.4), миграция ключей между версиями схемы.
- **Точки входа:** [config/loader.load_config/loads_config](../../src/wastech_orchestrator/config/loader.py), [config/validation.validate_config](../../src/wastech_orchestrator/config/validation.py), [config/upgrade.py](../../src/wastech_orchestrator/config/upgrade.py), [config/schema.py](../../src/wastech_orchestrator/config/schema.py).
- **Зависимости:** B25 (`find_forbidden_args` в валидации), B23 (`checks.model` предикаты), PyYAML.
- **Статус:** `documented` · [файл](./blocks/B05-configuration.md)

---

## Ядро оркестрации

### B06 — Конвейер оркестратора

- **Назначение:** детерминированный драйвер одной задачи от валидации до публикации и терминальной
  очистки; вызывает только Router, Check Runner и Git Manager; контекст агентам — только путями.
- **Точки входа:** [Orchestrator](../../src/wastech_orchestrator/core/orchestrator.py#L294), [run_task](../../src/wastech_orchestrator/core/orchestrator.py#L350), `resume`, `rerun_task`/`continue_task`, `finalize_task`, фабрика [build_orchestrator](../../src/wastech_orchestrator/core/orchestrator.py#L2594).
- **Зависимости:** почти все блоки ядра и исполнения (см. index).
- **Статус:** `documented` · [файл](./blocks/B06-orchestrator-pipeline.md)

### B07 — Машина состояний и State Store

- **Назначение:** канонические статусы и допустимые переходы (§8); персистентное состояние в SQLite,
  единый слот обработки, транзакции, версионирование схемы БД, read-only режим.
- **Точки входа:** [core/state_machine.py](../../src/wastech_orchestrator/core/state_machine.py) (`Status`, `assert_transition`, `is_active`/`is_terminal`), [state_store.StateStore](../../src/wastech_orchestrator/state_store.py) (`open`/`open_readonly`, `transaction`, `set_status`, `find_active_tasks`, `update_task`, …).
- **Зависимости:** `sqlite3`; используется B06 и B22.
- **Статус:** `documented` · [файл](./blocks/B07-state-machine-and-store.md)

### B08 — Ledger и отчёты о провале

- **Назначение:** append-only журнал терминальных исходов (`completed.jsonl`); генерация
  `failure_report.json`/`stuck.md` и компактного `summary.{md,json}` при отсутствии агента.
- **Точки входа:** [ledger.Ledger](../../src/wastech_orchestrator/ledger.py) (`append`/`records`/`has_task_id`), `write_failure_report`, `write_minimal_summary`.
- **Зависимости:** только stdlib (`json`); читается B16/B06 (дедуп id), B06 (счётчик попыток).
- **Статус:** `documented` · [файл](./blocks/B08-ledger-and-failure-reports.md)

### B09 — Контроль циклов исправления

- **Назначение:** счётчики циклов (test/review fix), правила перехода в `fixing` и определение
  «застревания» по лимитам.
- **Точки входа:** [core/loop_control.py](../../src/wastech_orchestrator/core/loop_control.py) (`FixLoop`, `LoopController`, `LoopCounters`).
- **Зависимости:** конфигурация `agents.*` лимиты; используется B06.
- **Статус:** `documented` · [файл](./blocks/B09-fix-loop-control.md)

### B10 — Восстановление и возобновление

- **Назначение:** сверка персистентного состояния на старте и решение, что делать с незавершённой
  задачей (ничего / пометить manual / завершить очистку / возобновить со стадии).
- **Точки входа:** [core/recovery.py](../../src/wastech_orchestrator/core/recovery.py) (`RecoveryReconciler.reconcile`, `RecoveryAction`, `RecoveryPlan`).
- **Зависимости:** B07 (состояние), B22 (состояние git); используется B06 (`resume`).
- **Статус:** `documented` · [файл](./blocks/B10-recovery-and-resume.md)

### B11 — Декомпозиция задачи

- **Назначение:** решение о разбиении задачи на сабтаски по структурированному выводу planning;
  спецификации сабтасков и их файловые артефакты; индекс прогресса.
- **Точки входа:** [core/decomposition.py](../../src/wastech_orchestrator/core/decomposition.py) (`decide_decomposition`, `SubtaskSpec`, `write_subtask_artifacts`, `update_subtask_index`).
- **Зависимости:** B15/B12 (структурированный вывод planning), B07 (`subtasks`); используется B06.
- **Статус:** `documented` · [файл](./blocks/B11-task-decomposition.md)

### B12 — HITL и типизированный вывод стадий

- **Назначение:** долговечные взаимодействия «человек в контуре» (persist/resume) и парсинг/валидация
  структурированного вывода стадий (включая сигнал запроса ввода человека).
- **Точки входа:** [core/hitl.py](../../src/wastech_orchestrator/core/hitl.py) (`write_waiting_interaction`, `load_interaction`, `parse_typed_stage_output`, `stage_output_schema`, `consume_pending_interactions`, …).
- **Зависимости:** B26 (транспорт), B20/B21 (артефакты, redaction); используется B06.
- **Статус:** `documented` · [файл](./blocks/B12-hitl-and-typed-output.md)

### B13 — Инвентарь и выбор навыков (skills)

- **Назначение:** read-only сканирование `SKILL.md` в репозитории; резолвинг навыков, предложенных
  planning (агент не может выбрать путь, которого скан не нашёл), и дедуп против инструкций оператора.
- **Точки входа:** [core/skills.py](../../src/wastech_orchestrator/core/skills.py) (`SkillInventoryScanner`, `resolve_planning_skills`, `compute_skill_dedup`).
- **Зависимости:** B25 (`denied_read_paths`); используется B06 (planning).
- **Статус:** `documented` · [файл](./blocks/B13-skill-selection.md)

### B14 — Классификация «опасного» диффа

- **Назначение:** чистый классификатор изменений (удаления файлов, правки манифестов/локов
  зависимостей) → требование согласования человеком.
- **Точки входа:** [core/dangerous_diff.py](../../src/wastech_orchestrator/core/dangerous_diff.py) (`classify_dangerous_diff`, `DangerousDiff`).
- **Зависимости:** вход — `changed_code_entries()` из B22; используется B06 (guardrail) совместно с B12.
- **Статус:** `documented` · [файл](./blocks/B14-dangerous-diff-guardrail.md)

### B15 — Шаблоны промптов и их рендеринг

- **Назначение:** разрешение шаблонов стадий (упакованные дефолты + оверрайды оператора) и рендеринг
  с аллой-листом переменных (только метаданные и пути к артефактам).
- **Точки входа:** [core/prompts.py](../../src/wastech_orchestrator/core/prompts.py) (`PromptTemplateStore`, `render_prompt`), [templates/prompts/](../../src/wastech_orchestrator/templates/prompts/).
- **Зависимости:** B05 (`prompts.*`); используется B06.
- **Статус:** `documented` · [файл](./blocks/B15-prompt-templates.md)

---

## Вход задачи

### B16 — Модель задачи, парсинг и шлюз валидации

- **Назначение:** модель `NormalizedTask`; парсинг `.md`/`.json` (фронтматтер+тело, отказ на
  дубль-ключах); шлюз §19 (жёсткие проверки + классификация полноты), карантин при отказе.
- **Точки входа:** [task/parser.py](../../src/wastech_orchestrator/task/parser.py) (`read_task_source`, `load_normalized`, `write_normalized`, `slugify`), [task/validation_gate.ValidationGate](../../src/wastech_orchestrator/task/validation_gate.py), [task/model.py](../../src/wastech_orchestrator/task/model.py).
- **Зависимости:** B25 (`scan_frontmatter`), B05 (лимиты), B08+B07 (дедуп id); используется B06.
- **Статус:** `documented` · [файл](./blocks/B16-task-parsing-and-validation-gate.md)

---

## Исполнение и провайдеры

### B17 — Router агентов и политика fallback

- **Назначение:** выбор провайдера для стадии (конфиг + валидированный task-override), запуск с
  fallback только при инфраструктурных ошибках, подсчёт попыток, передача частичного диффа.
- **Точки входа:** [routing/router.AgentRouter](../../src/wastech_orchestrator/routing/router.py) (`resolve_route`, `run_stage`), [routing/snapshots.py](../../src/wastech_orchestrator/routing/snapshots.py) (`SnapshotHook`).
- **Зависимости:** B18 (вызов `run`), B25 (`is_same_or_stricter`), B05; используется B06.
- **Статус:** `documented` · [файл](./blocks/B17-agent-router-and-fallback.md)

### B18 — Адаптеры провайдеров и контракт (Codex/Claude)

- **Назначение:** контракт `AgentProvider`; трансляция `AgentRunRequest` в argv CLI, запуск,
  парсинг вывода, классификация ошибок в `ErrorClass`; `preflight`.
- **Точки входа:** [providers/base.py](../../src/wastech_orchestrator/providers/base.py), [providers/claude.ClaudeCodeProvider](../../src/wastech_orchestrator/providers/claude.py), [providers/codex.CodexProvider](../../src/wastech_orchestrator/providers/codex.py), [providers/errors.classify](../../src/wastech_orchestrator/providers/errors.py).
- **Зависимости:** B19 (запуск), B20 (артефакты), B21 (redaction), B25 (env/forbidden/isolation); вызывается только B17.
- **Статус:** `documented` · [файл](./blocks/B18-agent-providers.md)

### B19 — Безопасный запуск подпроцессов

- **Назначение:** единый примитив запуска: argv-список (без shell), аллой-лист окружения, таймаут,
  stdin-текст, потоковая запись stdout в файл, захват stderr.
- **Точки входа:** [providers/process.run_process](../../src/wastech_orchestrator/providers/process.py).
- **Зависимости:** B25 (`build_child_env`); используется B18, B22, B24, B03/B04 (git-пробы).
- **Статус:** `documented` · [файл](./blocks/B19-subprocess-runner.md)

### B20 — Файловая раскладка артефактов запусков

- **Назначение:** детерминированная (never-overwrite) раскладка артефактов на диске; запись
  request/result, sha256, архивирование артефактов задачи при rerun.
- **Точки входа:** [providers/artifacts.py](../../src/wastech_orchestrator/providers/artifacts.py) (`task_artifact_dir`, `create_attempt_dir`, `archive_task_artifacts`, `sha256_file`).
- **Зависимости:** stdlib; используется B18, B06.
- **Статус:** `documented` · [файл](./blocks/B20-artifact-layout.md)

### B21 — Редактирование секретов (redaction)

- **Назначение:** сквозное вычищение секрето-подобных строк (паттерны токенов + чувствительные
  присваивания) из текста/словарей; сбор секретов из `denied_read_paths`.
- **Точки входа:** [providers/redaction.py](../../src/wastech_orchestrator/providers/redaction.py) (`redact_text`, `redact_mapping`, `read_denied_secrets`).
- **Зависимости:** stdlib; используется B18, B22, B27, B06, B26.
- **Статус:** `documented` · [файл](./blocks/B21-secret-redaction.md)

---

## Git

### B22 — Операции git и GitHub (Git Manager)

- **Назначение:** все git/gh-операции через argv без shell: ветка `agent/<id>-<slug>`, scoped-стейджинг
  (никогда `git add .`), commit/push/PR/merge с идемпотентностью, footprint/excludes, снапшоты
  рабочего дерева, терминальная очистка.
- **Точки входа:** [git_manager.GitManager](../../src/wastech_orchestrator/git_manager.py) (`prepare_branch`, `commit_code`/`commit_subtask`/`commit_audit`, `push`, `create_pr`, `merge_pr`, `terminal_cleanup`, `capture`/`partial_change_since`, …), `append_runtime_excludes`.
- **Зависимости:** B19, B21, B07 (publish_operations), B25 (env); используется B06, B17 (SnapshotHook), B01.
- **Статус:** `documented` · [файл](./blocks/B22-git-manager.md)

---

## Проверки (quality gate)

### B23 — Обнаружение и резолвинг проверок

- **Назначение:** определить запускаемый набор проверок (детерминированно по «уликам» репозитория
  или доверять `checks.commands`; опционально — агентский fallback), кэшировать профиль по
  fingerprint, инвалидировать при изменении; повторный резолвинг при launch-ошибке.
- **Точки входа:** [checks/resolver.CheckResolver](../../src/wastech_orchestrator/checks/resolver.py) (`resolve`/`reresolve`), [checks/diagnostics.py](../../src/wastech_orchestrator/checks/diagnostics.py), [checks/inspect.py](../../src/wastech_orchestrator/checks/inspect.py), [checks/detect.py](../../src/wastech_orchestrator/checks/detect.py), [checks/probe.py](../../src/wastech_orchestrator/checks/probe.py), [checks/validate.py](../../src/wastech_orchestrator/checks/validate.py), [checks/store.py](../../src/wastech_orchestrator/checks/store.py), [checks/fingerprint.py](../../src/wastech_orchestrator/checks/fingerprint.py), [checks/agent.py](../../src/wastech_orchestrator/checks/agent.py).
- **Зависимости:** B18 (агентский fallback), B19 (пробы), B25; используется B06, B01, B03.
- **Статус:** `documented` · [файл](./blocks/B23-check-discovery.md)

### B24 — Выполнение проверок (стадия testing)

- **Назначение:** запуск разрешённых проверок по порядку (argv без shell, аллой-лист env, таймаут),
  остановка на первом провале, логи, различение launch-ошибки и качественного провала.
- **Точки входа:** [check_runner.CheckRunner.run](../../src/wastech_orchestrator/check_runner.py) → `CheckOutcome`.
- **Зависимости:** B19, B25 (env), B21; используется B06 (`testing`).
- **Статус:** `documented` · [файл](./blocks/B24-check-execution.md)

---

## Безопасность

### B25 — Принуждение политики безопасности

- **Назначение:** примитивы неослабляемой политики: аллой-лист переменных окружения, запрет
  bypass-флагов, скан инъекций во фронтматтере, префлайт изоляции провайдеров, ранжирование строгости
  профилей разрешений (для условного fallback).
- **Точки входа:** [security/env.build_child_env](../../src/wastech_orchestrator/security/env.py), [security/forbidden_args.find_forbidden_args](../../src/wastech_orchestrator/security/forbidden_args.py), [security/injection.scan_frontmatter](../../src/wastech_orchestrator/security/injection.py), [security/isolation.check_isolation](../../src/wastech_orchestrator/security/isolation.py), [security/profiles.is_same_or_stricter](../../src/wastech_orchestrator/security/profiles.py).
- **Зависимости:** stdlib; используется B18, B19, B22, B24, B17, B16, B05, B06, B01.
- **Статус:** `documented` · [файл](./blocks/B25-security-policy.md)

---

## Интеграции и сквозные сервисы

### B26 — Уведомления и транспорт HITL (Telegram)

- **Назначение:** контракт `Notifier`; реализация Telegram: отправка коррелированного запроса,
  поллинг ответа с таймаутом, fire-and-forget уведомления; `NullNotifier` при выключенном/непрописанном
  транспорте; preflight Telegram.
- **Точки входа:** [notify/interface.py](../../src/wastech_orchestrator/notify/interface.py) (`Notifier`, `NullNotifier`, `AskResult`/`AskHandle`), [notify/telegram.py](../../src/wastech_orchestrator/notify/telegram.py) (`build_notifier`, `check_telegram_preflight`).
- **Зависимости:** `python-telegram-bot`, B21, B05 (`telegram.*`); используется B06, B12, B01.
- **Статус:** `documented` · [файл](./blocks/B26-notifications-telegram.md)

### B27 — Наблюдаемость: логирование и heartbeat

- **Назначение:** структурированное логирование без секретов (logfmt/json, ротация файла, фильтр
  redaction, контекстная привязка) и heartbeat-сообщения во время долгих блокирующих операций.
- **Точки входа:** [observability/logging.py](../../src/wastech_orchestrator/observability/logging.py) (`configure_logging`, `bind`, `RedactionFilter`), [observability/progress.run_with_heartbeat](../../src/wastech_orchestrator/observability/progress.py).
- **Зависимости:** B21 (фильтр redaction); используется B06, B18, B22, B24, B01.
- **Статус:** `documented` · [файл](./blocks/B27-observability.md)

---

## Рассмотрено, но не выделено в отдельный блок (`excluded`)

- **`providers/errors.py`** — включён в B18 (правило классификации ошибок адаптеров).
- **`routing/snapshots.py`** — включён в B17 (контракт частичного диффа Router↔Git).
- **`checks/model.py`, `checks/profile.py`, `checks/schema_validate.py`, `checks/discovery_factory.py`, `checks/fingerprint.py`** — части B23 (модели/схемы/фабрика обнаружения проверок).
- **`templates/`, `worc/` (markdown)** — данные пакета, поставляемые B03; не исполняемый код.
- **`__init__.py` пакетов, `__main__.py`** — реэкспорт/обёртки точек входа (отражены в B01).
