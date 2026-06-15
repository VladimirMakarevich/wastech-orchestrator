# B22 — Операции git и GitHub (Git Manager)

## Назначение

Единственный компонент, который коммитит, пушит и открывает Pull Request — агенты этого не делают
никогда. Все git/gh-вызовы идут через безопасный раннер как argv-список (без shell, без интерполяции
пользовательских строк) с аллой-листом окружения. Реализует инварианты «только оркестратор делает
commit/push/PR» и «запуск без shell-интерполяции».

## Ответственность

- Поток веток: `fetch` → checkout `base_branch` → `pull` → создать/переиспользовать
  `agent/<task-id>-<slug>` ([git_manager.py:278-293](../../../src/wastech_orchestrator/git_manager.py#L278)).
- **Scoped-стейджинг** (§21.1): только код-пути + `:(exclude)tasks/…` — **никогда** `git add .`
  ([git_manager.py:526-549](../../../src/wastech_orchestrator/git_manager.py#L526)).
- Три режима footprint (external / in_repo+exclude_local / in_repo+commit) и runtime-excludes
  ([git_manager.py:383-438](../../../src/wastech_orchestrator/git_manager.py#L383)).
- Идемпотентные commit/push/PR/merge через `publish_operations` + проверку удалённого состояния
  ([git_manager.py:559-748](../../../src/wastech_orchestrator/git_manager.py#L559)).
- Терминальная очистка к `base_branch`, когда это доказуемо безопасно ([git_manager.py:805-829](../../../src/wastech_orchestrator/git_manager.py#L805)).
- Реализация `SnapshotHook` для захвата частичных изменений ([git_manager.py:442-462](../../../src/wastech_orchestrator/git_manager.py#L442)).

## Границы блока

### Входит в ответственность блока

- Все git/gh-операции, идемпотентность публикации, footprint/excludes, снимки рабочего дерева,
  редакция stderr/диффов перед записью.

### Не входит в ответственность блока

- **Когда** коммитить/пушить/делать PR и переходы статусов — это [B06](./B06-orchestrator-pipeline.md).
- **Безопасность запуска процессов** — это [B19](./B19-subprocess-runner.md).
- **Правила редакции** — [B21](./B21-secret-redaction.md) (Git Manager их применяет).
- **Аллой-лист окружения** — [B25](./B25-security-policy.md).
- **Форма контракта частичных изменений** — это [B17/snapshots](./B17-agent-router-and-fallback.md); Git Manager его реализует.
- **Запись `check_runs`/ledger** — это [B07](./B07-state-machine-and-store.md)/[B08](./B08-ledger-and-failure-reports.md).

## Точки входа

- `GitManager(...)` ([git_manager.py:200](../../../src/wastech_orchestrator/git_manager.py#L200)); конструируется в `build_orchestrator` ([orchestrator.py:2617](../../../src/wastech_orchestrator/core/orchestrator.py#L2617)).
- Ветка/диффы/публикация/очистка: `prepare_branch`, `reset_branch_to_base`, `delete_branch`,
  `commit_code`/`commit_subtask`/`commit_audit`, `push`, `create_pr`, `merge_pr`,
  `write_current_diff`/`cumulative_committed_diff`/`diff_stat`, `terminal_cleanup`,
  `preflight_footprint`/`ensure_exclude_local`/`ensure_runtime_excludes`.
- Read-пробы: `unaccounted_dirty_paths`, `remote_branch_exists`, `recorded_pr_url`, `verify_pr_state`,
  `refresh_base`, `commit_on_branch`.
- `SnapshotHook`: `capture`, `partial_change_since` (вызывает [B17](./B17-agent-router-and-fallback.md)).
- Модульная функция `append_runtime_excludes(repo_root, *, tracked=False)` ([git_manager.py:100](../../../src/wastech_orchestrator/git_manager.py#L100)) — [B03 init/install](./B03-installer-and-scaffolding.md).

## Входные данные и состояние

`OrchestratorConfig` (repo, footprint, security, auto_merge), `StateStore` (для `publish_operations`),
`artifacts_root`. Внутреннее состояние — текущая активная задача `_ActiveTask` (для путей частичных
диффов) и аллой-лист окружения, построенный один раз в конструкторе.

## Основной сценарий (публикация успешной задачи)

1. `commit_code` — scoped-стейджинг код-путей и один коммит (или текущий HEAD, если менять нечего).
2. `commit_audit` — при `tracking=commit` отдельный коммит только `tasks/` (lifecycle + `summary.md`);
   `logs/` не коммитится.
3. `push` — пуш `agent/<id>-<slug>` в `origin` (отказ пушить в base).
4. `create_pr` — `gh pr create` с телом из `summary.md`.
5. (опц.) `merge_pr` — `gh pr merge --<strategy> [--auto]`.
6. `terminal_cleanup` — checkout `base_branch`, если дерево безопасно.

Все шаги идемпотентны: повторный вызов после рестарта проверяет `publish_operations` и/или удалённое
состояние и не дублирует операцию.

## Альтернативные сценарии

### Частичные изменения (SnapshotHook)
`capture` снимает HEAD/porcelain/diff-checksum; `partial_change_since` при изменившемся диффе пишет
`logs/<task>/partial/NNN.diff` и возвращает `PartialChange` (без отката) ([git_manager.py:451-475](../../../src/wastech_orchestrator/git_manager.py#L451)).

### Rerun (сброс ветки)
`reset_branch_to_base`: checkout base, опц. удалить удалённую ветку (закрывает PR), force-delete
локальную ветку — чтобы свежий `prepare_branch` пересоздал её от текущего base ([git_manager.py:295-316](../../../src/wastech_orchestrator/git_manager.py#L295)).

### Уже смерженный PR
`merge_pr` при неуспехе с маркером «already merged/not open/was merged» считает это идемпотентным
успехом (`"merged"`), иначе — `GitCommandError` ([git_manager.py:739-748](../../../src/wastech_orchestrator/git_manager.py#L739)).

## Проверки и ограничения

- **argv-список, без shell**; stderr всегда редактируется ([git_manager.py:225-254](../../../src/wastech_orchestrator/git_manager.py#L225)).
- **Никогда `git add .`** — только явный pathspec + `:(exclude)` для артефактных каталогов ([git_manager.py:526-538](../../../src/wastech_orchestrator/git_manager.py#L526)).
- Отказ пушить напрямую в `base_branch` (§12.12) → `GitCommandError` ([git_manager.py:654-658](../../../src/wastech_orchestrator/git_manager.py#L654)).
- `merge_pr` **никогда** не использует `--admin`/force, ровно одна попытка (защита веток сохраняется) ([git_manager.py:733-737](../../../src/wastech_orchestrator/git_manager.py#L733)).
- footprint-preflight: отказ старта, если репозиторий уже трекает путь, который footprint обязан
  держать вне git → `ManualActionRequired` ([git_manager.py:398-419](../../../src/wastech_orchestrator/git_manager.py#L398)).
- Окружение — только аллой-лист; git/gh-креды настраиваются вне оркестратора ([git_manager.py:217](../../../src/wastech_orchestrator/git_manager.py#L217)).
- `verify_pr_state`/`recorded_pr_url`/`refresh_base`/`fetch` — best-effort (не поднимают ошибку).

## Результат

Создание/переключение веток; коммиты (SHA); пуш; URL PR; маркер merge; `CleanupOutcome`; диффы на
диске; `PartialChange`. Идемпотентные маркеры записываются в `publish_operations` ([B07](./B07-state-machine-and-store.md)).

## Побочные эффекты

- Мутации git (ветки, коммиты), сеть (`fetch`/`pull`/`push`/PR/merge через `gh`).
- Файлы: `logs/<task>/current.diff`, `partial/NNN.diff`, `publish/terminal-cleanup.json`,
  записи в `.git/info/exclude` или `.gitignore`.
- Строки `publish_operations` в State Store (идемпотентность).
- Heartbeat-лог во время долгих операций.

## Ошибки и граничные случаи

- Проваленный обязательный git/gh → `GitCommandError`.
- footprint трекает запрещённый путь → `ManualActionRequired`.
- Заблокированный merge (защита ветки/конфликт) → `GitCommandError` (Core делает `manual_action_required`, PR остаётся открытым).
- Грязное дерево при очистке → `CleanupOutcome(safe=False)`; статус становится `manual_action_required` при успешной публикации.

## Связи

### Использует

- [B19 — Запуск подпроцессов](./B19-subprocess-runner.md) — `run_process`.
- [B21 — Redaction](./B21-secret-redaction.md) — редакция stderr и диффов; `read_denied_secrets`.
- [B25 — Security](./B25-security-policy.md) — `build_child_env`.
- [B07 — State Store](./B07-state-machine-and-store.md) — `publish_operations` (идемпотентность).
- [B27 — Наблюдаемость](./B27-observability.md) — heartbeat и логирование.
- [B17/snapshots](./B17-agent-router-and-fallback.md) — типы `WorkingTreeSnapshot`/`PartialChange`.

### Используется в

- [B06 — Конвейер](./B06-orchestrator-pipeline.md) — весь поток git и публикации.
- [B17 — Router](./B17-agent-router-and-fallback.md) — как `SnapshotHook` (снимок/частичный дифф).
- [B03 — Установщик](./B03-installer-and-scaffolding.md) — `append_runtime_excludes`.
- [B01 — CLI](./B01-cli-and-operator-commands.md) — read-пробы через план rerun/finalize в [B06](./B06-orchestrator-pipeline.md).

## Место в общей системе

Git Manager — выход системы в Git/GitHub. Он держит инвариант «публикует только оркестратор», изолирует
ядро от git-синтаксиса и делает публикацию устойчивой к падениям (идемпотентность) и безопасной
(scoped-стейджинг, отказ пушить в base, без `--admin`).

## Подтверждение в коде

- [git_manager.py:225-271](../../../src/wastech_orchestrator/git_manager.py#L225) — argv-запуск, редакция stderr, `_git_checked`/`_gh`.
- [git_manager.py:278-438](../../../src/wastech_orchestrator/git_manager.py#L278) — поток веток, footprint, runtime-excludes.
- [git_manager.py:479-643](../../../src/wastech_orchestrator/git_manager.py#L479) — scoped-стейджинг, идемпотентные коммиты, audit-commit.
- [git_manager.py:647-748](../../../src/wastech_orchestrator/git_manager.py#L647) — push/PR/merge (идемпотентные, без `--admin`).
- [git_manager.py:805-868](../../../src/wastech_orchestrator/git_manager.py#L805) — терминальная очистка + артефакт.
- Тест: [tests/git/test_git_manager.py](../../../tests/git/test_git_manager.py) — ветка `agent/<id>-<slug>`, отсутствие `git add .`, footprint/excludes, идемпотентность push/PR/merge, already-merged, отказ пуша в base, редактированный дифф, терминальная очистка.
