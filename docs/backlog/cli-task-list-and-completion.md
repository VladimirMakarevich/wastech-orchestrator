# Task discovery: `worc list` + shell completion

Status: **proposed** (2026-06-25 — design + recommendation; not locked — see [§ Decision (recommended)](#decision-recommended)) Date: 2026-06-25 Owner: Vladimir Makarevich

Detail file for the backlog idea _"make picking a task easier from the command line — e.g. `worc list` to see the tasks, and Tab-completion so the operator starts typing a task and completes it instead of recalling the full id."_ It records the original sketch, what task selection looks like today (traced), why the pain is concentrated on the **id-based** commands, the design (a one-shot dependency-free `worc list` as the single read-surface + a `worc completion` script that is a thin wrapper over it), the rejected alternatives, and a phased plan ([§ План реализации](#план-реализации)). It also fixes the relationship to the bigger CLI backlog item — the [interactive operator console](cli-upgrade.md) — so the two do not collide.

## The idea (original)

> Think about how to simplify task selection — e.g. help the operator run `worc --tasks list` in the terminal and get a list of tasks. Also try adding "intellisense": help complete the task name instead of typing it in full — start typing, press Tab to complete. Important to account for the global CLI refactoring currently in the backlog.

The note bundles two distinct ergonomics needs under "task selection": **discovery** (what tasks exist?) and **input** (let me pick one without typing it whole). They are two faces of the same gap and share one backing data source. The design below splits them cleanly and aims them at this orchestrator specifically.

## TL;DR (findings)

1. **The selection pain is concentrated on the id-based commands.** Task selection is not uniform across the CLI: `run <task_file>` ([cmd_run](../../src/wastech_orchestrator/cli.py#L765)) takes a **file path** — the shell's own path completion already covers Tab there. `watch` takes **nothing** — it auto-scans `tasks/pending` ([select_pending](../../src/wastech_orchestrator/cli.py#L648)). The genuine friction is on `status [task_id]` ([cmd_status](../../src/wastech_orchestrator/cli.py#L1225)), `rerun <task_id>` ([cmd_rerun](../../src/wastech_orchestrator/cli.py#L830)), and `finalize <task_id>` ([cmd_finalize](../../src/wastech_orchestrator/cli.py#L925)), where the operator must recall and type an **exact** `task_id` (`^[a-z0-9][a-z0-9._-]{0,63}$` — [model.py:17](../../src/wastech_orchestrator/task/model.py#L17)). That is where completion pays off; that is what `worc --tasks list` is reaching for.
2. **There is no `list` command and no shell completion today.** The CLI is `argparse`, subcommand-based ([build_parser](../../src/wastech_orchestrator/cli.py#L112)), entry point `worc = wastech_orchestrator.cli:main` ([pyproject.toml:33-36](../../pyproject.toml#L33-L36)). No subcommand enumerates tasks; no bash/zsh completion, no `argcomplete`. So discovery means listing `tasks/pending` by hand or re-running `status`, and id input is fully manual.
3. **The data the list needs already exists — and is the exact read-surface the console backlog item already specified.** Active task: `find_active_tasks()` ([state_store.py:615](../../src/wastech_orchestrator/state_store.py#L615)). Pending queue: `select_pending()` over `tasks/pending` ([cli.py:648](../../src/wastech_orchestrator/cli.py#L648)), with the cheap front-matter read `_scan_pending_meta` ([cli.py:666](../../src/wastech_orchestrator/cli.py#L666)) for the id. Latest task: `latest_task()` ([state_store.py:604](../../src/wastech_orchestrator/state_store.py#L604)); single task: `get_task()` ([state_store.py:599](../../src/wastech_orchestrator/state_store.py#L599)). The only missing read helper is `recent_tasks(limit)` — **the same helper [cli-upgrade.md § Фаза 1](cli-upgrade.md#фаза-1--read-surface--live-монитор-worc-top) already lists as needed for `worc top`.** Building it here is shared, not duplicated.
4. **`--tasks list` fights the CLI shape; a `list` subcommand fits it.** Every verb today is a subcommand (`run`/`watch`/`status`/`rerun`/`finalize`/…). A global option that takes a value (`--tasks list`) is a different grammar than the rest and would be the only one of its kind. `worc list` mirrors `status` (a read-only snapshot) and reads naturally.
5. **Completion should be backed by `list`, not by a second enumeration path.** The robust pattern (kubectl, gh, docker) is a completion **script** whose dynamic parts shell out to the tool's own list command — here `worc list --format ids`. That keeps a single source of truth, needs **no new runtime dependency**, works in the operator's own shell regardless of any optional extras, and decouples completion from `argparse` internals.
6. **Task ids are shell-safe by construction.** `^[a-z0-9][a-z0-9._-]{0,63}$` contains no whitespace, quotes, or shell metacharacters, so feeding `worc list --format ids` output into `compgen` carries no injection risk — a property worth stating because the completion script is shell code we emit.

Net: this is **one small read view + a thin completion wrapper over it**, both reusing data access that already exists (plus one read helper the console item needed anyway). It is an additive, low-risk down-payment on [cli-upgrade.md](cli-upgrade.md), not a competitor to it.

## How task selection works today (traced)

| command | how a task is selected | source |
| --- | --- | --- |
| `run <task_file>` | **file path** (positional `.md`/`.json`); the id is read from front-matter at run time | [cmd_run](../../src/wastech_orchestrator/cli.py#L765) |
| `watch` | **nothing** — scans `tasks/pending` in deterministic filename order | [select_pending](../../src/wastech_orchestrator/cli.py#L648) |
| `status [task_id]` | **task_id** (optional); defaults to the active/latest task | [cmd_status](../../src/wastech_orchestrator/cli.py#L1225) |
| `rerun <task_id>` | **task_id** (required) — only a `failed` / `manual_action_required` task | [cmd_rerun](../../src/wastech_orchestrator/cli.py#L830) |
| `finalize <task_id>` | **task_id** (required) — record an operator-handled outcome | [cmd_finalize](../../src/wastech_orchestrator/cli.py#L925) |

Tasks live as files across the lifecycle dirs `tasks/{pending,processing,done,failed}` (git-tracked; [REPO_TASK_DIRS](../../src/wastech_orchestrator/cli.py#L81)) plus `tasks/rejected` under `.worc/`, and as rows in `state.db` keyed by `task_id` (`TaskRow` — [state_store.py:290](../../src/wastech_orchestrator/state_store.py#L290): `task_id`, `title`, `status`, `branch`, `updated_at`, …). The canonical statuses are `new`/`validated`/`preparing`/`running` (active) and `done`/`failed`/`manual_action_required` (terminal) plus `pending` ([state_machine.py:18](../../src/wastech_orchestrator/core/state_machine.py#L18)). The gap the operator feels: there is no single command to enumerate "what's queued / running / recently finished," and the id-based verbs offer no completion, so the operator copies ids by eye.

## Constraints that bound any solution

From [.agents/rules/architecture.md](../../.agents/rules/architecture.md) and the code as it stands:

1. **The core does not know the CLI.** `list` and `completion` live in the CLI layer ([cli.py](../../src/wastech_orchestrator/cli.py)); the only data-layer addition is a read query (`recent_tasks`) next to the existing read helpers. No logic enters `core/`.
2. **Read-only.** `list` opens the DB via `StateStore.open_readonly` ([state_store.py:505](../../src/wastech_orchestrator/state_store.py#L505)) — the exact path `status` already uses while a daemon may be active — and scans `tasks/pending` without mutating anything. Completion only reads. No new task status, no schema bump.
3. **No new dependency.** Completion is a generated shell script over `worc list`; it adds **no** Python runtime dependency (no `argcomplete`) and no optional extra. The hot path is untouched.
4. **No secrets.** `list` renders only already-stored, non-secret fields (`task_id`, `status`, `title`, `branch`) — the same class of data `status` already prints — and writes nothing new to logs.
5. **No shell interpolation of untrusted strings.** The emitted completion script is static text; its only dynamic input is `worc list --format ids` output, which is constrained to the `task_id` charset (TL;DR #6) and fed through `compgen` — no command string is built from user data.
6. **Single-slot invariant is respected by presentation.** `list` shows _one_ active task + the pending queue + recent terminal tasks — never implies parallel jobs.

## Design

### `worc list` — one-shot, read-only, scriptable

A new subcommand beside `status`. **Default (no flags)** prints a compact human overview in three sections, each from its existing source:

- **active** — `find_active_tasks()` (normally 0 or 1 by the single-slot invariant): `status  task_id  title  branch`.
- **pending** — `select_pending(tasks/pending)` + `_scan_pending_meta` for the id (a queued file has no DB row yet, so this section is **file-derived**; if the id is unreadable, show the filename).
- **recent** — `recent_tasks(limit)` (new read helper): the last N terminal tasks (`done`/`failed`/`manual_action_required`) by `updated_at`.

This is the same data [`worc top`](cli-upgrade.md#decision-recommended) will render live; `worc list` is its one-shot, dependency-free text sibling.

**Flags (minimal core):**

| flag | effect |
| --- | --- |
| _(none)_ | the three-section overview above |
| `--pending` | only the `tasks/pending` queue |
| `--recent [N]` | only recent terminal tasks (default N) |
| `--all` | every known task (DB rows across all statuses) |
| `--format {table,ids,json}` | `table` (default, human) / `ids` (one `task_id` per line, machine — for completion) / `json` (structured) |
| `--scope {rerun,status,finalize}` | filter ids to what the named command accepts (completion-facing; keeps the per-command rule in `list`, not in the shell script) |

`--format ids` emits **only** ids on stdout (errors/notes to stderr) so it can be consumed by the completion script and by plain scripting (`worc list --format ids | …`). `--scope rerun` restricts to rerun-eligible terminal tasks (`failed`/`manual_action_required`); `--scope status` is any known id; `--scope finalize` is the active/known id set `finalize` accepts. Everything beyond this minimal surface (queue priorities, rich filtering, watch-style `--follow`) is **deferred** — `--follow` specifically belongs to `worc top` in [cli-upgrade.md](cli-upgrade.md), not here.

**Edge cases:** no tasks at all → a one-line "no tasks" notice, exit 0; `.worc` not initialized → a clear "run `worc install`" message; a pending file with no parseable id → listed by filename; `--format ids` with nothing matching → empty stdout, exit 0.

### `worc completion bash|zsh` — a thin wrapper over `list`

A new subcommand that **prints a completion script** to stdout (the kubectl/gh pattern). The operator wires it once:

```bash
# zsh
source <(worc completion zsh)          # or: worc completion zsh > ~/.zsh/completions/_worc
# bash
source <(worc completion bash)         # or into ~/.bash_completion.d/
```

The script completes:

- **subcommand names and flags** — static, from the known parser surface.
- **task-id positionals** — dynamic: on Tab it calls `worc list --format ids --scope <command>` and feeds the result to `compgen`. `rerun <Tab>` → rerun-eligible ids; `status <Tab>` → any id; `finalize <Tab>` → finalize-eligible ids.
- **`run <Tab>`** — task files: `tasks/pending/*.{md,json}` (or fall back to the shell's default path completion).

Because completion calls `worc list`, the enumeration rules live in **one** place; the shell script stays dumb. No `argcomplete`, no registration hook beyond the one `source` line, no coupling to `argparse`.

## Rejected alternatives

- **`worc --tasks list` (global option taking a value).** The literal sketch. **Rejected:** it is a different grammar from every existing verb (all subcommands) and would be the only option-with-value of its kind; `worc list` fits the established shape (TL;DR #4).
- **`worc tasks list` (a `tasks` command group with sub-actions).** More extensible (`tasks list/show/rm`). **Rejected for now (YAGNI):** it introduces a nested-subparser grouping convention that exists nowhere else in the CLI, for a single action today. If `show`/`rm` ever justify a group, `list` can move under it without changing its output contract.
- **`argcomplete` as the completion engine.** The stdlib-adjacent way to bolt dynamic completion onto `argparse`. **Rejected:** it adds a runtime dependency, needs a per-shell `eval "$(register-python-argcomplete worc)"` registration, and couples completion to `argparse` internals — versus a self-contained script that reuses `worc list` with zero new deps.
- **A prompt_toolkit completer inside `worc shell`.** In-console Tab-completion. **Out of scope, not rejected:** it is a _different surface_ (the REPL, an optional `[shell]` extra) owned by [cli-upgrade.md](cli-upgrade.md); it would reuse the same `list` scopes when built. This item is about completion in the operator's own shell.
- **Folding this into cli-upgrade.md.** **Rejected:** that item is a heavier, interactive, optional-extra console; this is a small, dependency-free, scriptable read view + shell completion that is useful standalone and lands the shared read helper early. Kept as its own item, explicitly linked.

## Decision (recommended)

Add **`worc list`** — a one-shot, read-only, dependency-free task-enumeration subcommand beside `status`. Default output is a compact overview (active task + the `tasks/pending` queue + recent terminal tasks) from the existing read helpers plus a new `recent_tasks(limit)` query; flags `--pending` / `--recent` / `--all` / `--format {table,ids,json}` / `--scope {rerun,status,finalize}` cover focused and machine-readable output. Add **`worc completion bash|zsh`**, a generated completion script that completes subcommands/flags statically and **task-id positionals dynamically by shelling out to `worc list --format ids --scope <command>`** — single source of truth, **no new runtime dependency**, no `argcomplete`. Reject the literal `--tasks list` (wrong grammar) and the `tasks` command group (premature). `recent_tasks` is the exact read helper [cli-upgrade.md § Фаза 1](cli-upgrade.md#фаза-1--read-surface--live-монитор-worc-top) needs for `worc top`, so this is an **additive down-payment** on the console item, sharing its read-surface; nothing built here is discarded when the console lands. **No new task status, no schema bump** — only a read query, a presentation command, and a generated shell script.

Deliberately **out of scope** (YAGNI / greenfield-MVP): a `--follow` live view (that is `worc top` in [cli-upgrade.md](cli-upgrade.md)); in-console prompt_toolkit completion (the `worc shell` surface); queue priorities and richer filtering (separate backlog items); a `tasks` command group (revisit only if `show`/`rm` arrive).

## План реализации

Раздел на русском по просьбе владельца. Рекомендованное решение — **`worc list` (одноразовый read-only список) + `worc completion bash|zsh` (скрипт-обёртка над `list`)**. Деление на «Фазы» ниже — логическая структура работ, а не отдельные итерации/мержи. **Проверки и документация — один раз в самом конце** (`/run-checks`, затем `/sync-docs` и `prettier` по докам).

Целевой сквозной сценарий: оператор делает `worc list` → видит активную задачу + очередь `tasks/pending` + последние терминальные (id + title + статус); подключив `source <(worc completion zsh)`, набирает `worc rerun <Tab>` → шелл подставляет id из `worc list --format ids --scope rerun`; `worc run <Tab>` дополняет файлы из `tasks/pending/`.

### Зафиксированные решения (ответы на форки)

1. **Форма — субкоманда `worc list`, не `--tasks list` и не группа `worc tasks`.** Совпадает с субкоманд-паттерном CLI; ближайший аналог — `status`. См. [§ Rejected alternatives](#rejected-alternatives).
2. **Completion — скрипт `worc completion`, не `argcomplete`.** Динамические части зовут `worc list --format ids --scope <cmd>` (паттерн kubectl/gh). Без новой runtime-зависимости, без привязки к argparse, один источник правды.
3. **`list` — read-only, без bump схемы и без нового статуса.** Только новый read-хелпер `recent_tasks(limit)` рядом с `latest_task`/`find_active_tasks`.
4. **`recent_tasks` — общий с `worc top`.** Делаем здесь, переиспользуется консолью из [cli-upgrade.md](cli-upgrade.md); это аванс, а не дубль.
5. **Минимальная поверхность флагов.** `--pending` / `--recent [N]` / `--all` / `--format {table,ids,json}` / `--scope {rerun,status,finalize}`. `--follow`, приоритеты очереди, богатые фильтры — отложены.

### Фаза 1 — read-хелпер + `worc list`

- **[state_store.py](../../src/wastech_orchestrator/state_store.py)** — read-only `recent_tasks(limit) -> list[TaskRow]` рядом с `latest_task`/`find_active_tasks` ([state_store.py:604-620](../../src/wastech_orchestrator/state_store.py#L604-L620)): последние терминальные по `updated_at`. Без bump схемы.
- **[cli.py](../../src/wastech_orchestrator/cli.py)** — сабпарсер `list` + `cmd_list`: по умолчанию три секции (active через `find_active_tasks`, pending через `select_pending` + `_scan_pending_meta`, recent через `recent_tasks`); флаги `--pending`/`--recent`/`--all`/`--format`/`--scope`. `--format ids` печатает только id (ошибки в stderr). Чисто чтение через `StateStore.open_readonly`.
- **Тесты:** `recent_tasks` (порядок/лимит/только терминальные); `cmd_list` рендер из фейкового `state.db` + временного `tasks/pending` (одна активная + очередь + терминальные) без сети и без движка; `--format ids` даёт чистый список; `--scope rerun` отдаёт только `failed`/`manual_action_required`; пустой `state.db` → «нет задач», exit 0; pending-файл без id → по имени файла.

### Фаза 2 — `worc completion bash|zsh`

- **[cli.py](../../src/wastech_orchestrator/cli.py)** — сабпарсер `completion {bash,zsh}` + `cmd_completion`: печатает статический completion-скрипт на stdout. Статика — имена субкоманд/флагов; динамика — для id-позиционных зовёт `worc list --format ids --scope <cmd>` через `compgen`; для `run` — `tasks/pending/*.{md,json}`.
- **Тесты:** `worc completion zsh`/`bash` печатают непустой валидный скрипт, содержащий вызов `worc list --format ids` и имена субкоманд; (опц.) прогон сгенерированного скрипта в подоболочке на фейковом `worc`, отдающем фиксированный список id, проверяет подстановку. Без реального TTY.

### Инварианты (соблюдены)

- **Ядро не знает CLI**: `list`/`completion` — в `cli.py`; в данных только read-запрос `recent_tasks`. Логики в `core/` не добавляем.
- **Read-only / без секретов**: `open_readonly`; рендерим только `task_id`/`status`/`title`/`branch`; своих логов не пишем; без нового статуса и bump схемы.
- **Без новой зависимости**: completion — сгенерированный shell-скрипт над `worc list`; ни `argcomplete`, ни extra.
- **Без shell-интерполяции недоверенного**: скрипт статичен; единственный динамический вход — `worc list --format ids` (charset id'а), через `compgen`.
- **Один слот**: вид — одна активная + очередь + недавние терминальные; параллельных «jobs» не подразумевается.

### Связи и хвосты

- **`worc top` / `worc shell`** ([cli-upgrade.md](cli-upgrade.md)) — переиспользуют `recent_tasks` и формат секций; in-console completion (prompt_toolkit) — отдельная поверхность, не здесь.
- **`tasks` command group** (`show`/`rm`) — если появится потребность; `list` тогда переедет под группу без смены контракта вывода. Записать в [follow_ups.md](follow_ups.md), если решим двигаться.
- **`--follow` / live-вид** — это `worc top`, не `list`.
- **Queue priorities / richer task parsing** — уже в [README.md](README.md) «Open backlog»; `list` будет отражать порядок, который выберет планировщик.

### Проверки и документация

Всё — **один раз после всех фаз**.

- **Проверки:** `ruff check .`, `mypy src`, `pytest` (через `/run-checks`); затем `npx prettier@3 --write "**/*.md"` по затронутым докам.
- **Документация (`/sync-docs`):** [Functional Map](../functional/index.md) (блок CLI — добавить `list`/`completion` рядом с `status`/`run`/`watch`); [docs/operations.md](../operations.md) — операторская заметка про `worc list` (секции, `--format`, `--scope`) и установку completion (`source <(worc completion zsh)`); README — упоминание completion. При изменении топологии CLI — отметить в C4-модели [docs/likec4](../likec4), если применимо.
- **Backlog:** перевести статус этого файла в `accepted`/`done` при реализации; обновить строку в [README.md](README.md) «Open backlog» / «Open detail files».
