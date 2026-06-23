# Interactive operator console (non-blocking, live, attended)

Status: **proposed** (2026-06-23 — design + recommendation; not locked — see [§ Decision (recommended)](#decision-recommended)) Date: 2026-06-23 Owner: Vladimir Makarevich

Detail file for the backlog idea _"can we make working through the terminal more comfortable — not block operator input while a task runs, the way Claude Code does in the terminal?"_ It records the original sketch, what the orchestrator's terminal experience already looks like (traced), why the generic "add a REPL" answer is half-aimed here, the improved design (an attended **console that is a client over the existing daemon**, not a second engine host), the rejected alternatives, and a phased plan ([§ План реализации](#план-реализации)).

## The idea (original)

> Can we make working through the terminal more comfortable — not block operator input, the way Claude Code does in the terminal?

The original note answered generically: a foreground CLI command always blocks the shell until it exits; what Claude Code does is closer to an interactive REPL/TUI that owns stdin and runs work in the background. So: keep the plain commands, add an interactive mode (`mytool shell` → `run` / `jobs` / `cancel` / `logs`), built on [`prompt_toolkit`][1] with `patch_stdout()` so background log lines print _above_ the prompt; spawn external work with [`asyncio.create_subprocess_exec`][2]; reach for [Textual][3] if a full panel/TUI is wanted. The one firm rule it landed on is right and survives intact: **exactly one layer may read stdin — the REPL/TUI; background work must never call `input()`.**

That is sound generic CLI advice. The improvement below is to **aim it at this orchestrator specifically** — because three of the sketch's load-bearing assumptions are false here, and getting them wrong would fight the architecture rather than extend it.

## TL;DR (findings)

1. **The orchestrator is already non-blocking — for the path that matters.** `watch` is a long-running daemon ([cli.py:1096-1161](../../src/wastech_orchestrator/cli.py#L1096-L1161)) that scans `tasks/pending`, runs one task to completion, sleeps, repeats ([watch_loop](../../src/wastech_orchestrator/cli.py#L723-L762)); `stop`/`restart`/`status` are one-shot commands you issue from any other shell while it runs ([cli.py:1164-1224](../../src/wastech_orchestrator/cli.py#L1164-L1224)); and the coding agents run as **fully-captured subprocesses** (stdin `DEVNULL`, stdout streamed to a file, stderr piped — [providers/process.py:15-17,62](../../src/wastech_orchestrator/providers/process.py#L15-L17)), so they never occupy the operator's terminal. The only genuinely blocking command is `worc run <file>` ([cli.py:765-791](../../src/wastech_orchestrator/cli.py#L765-L791)) — and that is the deliberate one-shot/CI path. **So the missing thing is not "unblock stdin." It is a single attended place to watch the queue + the one active task + live progress and issue control commands, without juggling terminals and re-running `status`.**
2. **There is no "jobs" plural — the slot is single by invariant.** `acquire_slot` refuses a second task whenever `find_active_tasks()` shows another non-terminal task ([orchestrator.py:408-410](../../src/wastech_orchestrator/core/orchestrator.py#L408-L410)); the slot is DB-driven (a non-terminal `tasks.status`), not a lock file. So the sketch's `asyncio.create_task` fan-out of N parallel jobs **directly contradicts** the architecture. The real model is: **one active task + a FIFO-ish queue of pending files + a tail of recent terminal tasks.** That is what a console shows.
3. **A console must be a _client_ over the daemon, never a second engine host.** The biggest design error available is running the pipeline _inside_ the REPL process (the sketch's `asyncio.to_thread(blocking_command)`). `run_task` is a deeply synchronous, single-slot, durable-state pipeline ([orchestrator.py:365-406](../../src/wastech_orchestrator/core/orchestrator.py#L365-L406)); hosting it in a second process would duplicate the daemon and the slot logic. The right shape: the console **spawns or attaches to the `watch` daemon** (the only engine host), **polls `state.db` read-only** (`StateStore.open_readonly`, the same path `status` uses — [state_store.py:236](../../src/wastech_orchestrator/state_store.py#L236)), **tails the daemon's log file**, and **enqueues** by dropping task files into `tasks/pending` ([pending_dir](../../src/wastech_orchestrator/cli.py#L635-L637)). The engine stays where it is.
4. **Cancellation is honestly limited — and the console must say so.** There is no mid-stage cancel: the only graceful interruption is the daemon's `SIGTERM`-between-ticks, which lets the in-flight task finish its current stage ([process_control.py:1-9](../../src/wastech_orchestrator/process_control.py#L1-L9), [watch_loop honoring `stop_event`](../../src/wastech_orchestrator/cli.py#L740-L761)). So a console `cancel` can only **de-queue a pending file** or **stop/restart the daemon between ticks** — it cannot kill a running agent mid-stage. True mid-task cancellation (cooperative flag + subprocess termination) is a separate, larger feature.
5. **HITL is already Telegram, not stdin.** Human-in-the-loop questions go out over Telegram and the task waits on the reply or a timeout ([core/hitl.py](../../src/wastech_orchestrator/core/hitl.py), [notify/telegram.py](../../src/wastech_orchestrator/notify/telegram.py)). The sketch's "post an event to the UI loop and prompt there" is the right _shape_, but bridging HITL into the console is a distinct feature; it must not be entangled with the first cut.
6. **The command set already exists — the console is a dispatcher.** `status`, `finalize`, `rerun`, `stop`, `restart` (and `prs`/`merge-task` once [orchestrator-driven-pr-merge.md](orchestrator-driven-pr-merge.md) lands) are the verbs. A console reuses these `cmd_*` functions; it adds no orchestration logic, keeping "the core does not know the CLI" intact and the surface DRY.

Net: ~70% of this is **presentation + process supervision** over machinery that already exists (the daemon, the PID file, read-only `state.db`, file logging, the lifecycle dirs, the one-shot `cmd_*` verbs). The genuinely new code is a small **prompt_toolkit event loop** that (a) tails a log stream above a live prompt and (b) supervises/attaches a `watch` child.

## How the operator interacts today (traced)

1. **`worc run <file>`** — runs exactly one task file synchronously to completion and returns ([cli.py:765-791](../../src/wastech_orchestrator/cli.py#L765-L791) → [run_task](../../src/wastech_orchestrator/core/orchestrator.py#L365-L406)). Blocks the shell. This is the CI/script path and stays as-is.
2. **`worc watch`** — the daemon. With `poll_interval > 0` it loops: refresh `base_branch`, `watch_once` (resume any in-flight task, then pick one pending task when the slot is free), sleep ([watch_loop](../../src/wastech_orchestrator/cli.py#L723-L762), [watch_once](../../src/wastech_orchestrator/cli.py#L679-L720)). `poll_interval <= 0` is a single pass. It writes a PID file `orchestrator.pid` ([process_control.py:36,76-77](../../src/wastech_orchestrator/process_control.py#L36-L77)) so `stop`/`restart` can find it. Tasks flow through the lifecycle dirs `tasks/{pending,processing,done,failed}` + `tasks/rejected` ([cli.py:79-89](../../src/wastech_orchestrator/cli.py#L79-L89)).
3. **`worc stop` / `worc restart`** — read the PID file, send `SIGTERM` (escalate to `SIGKILL` after a timeout), which the daemon's `SIGTERM` handler turns into a `threading.Event` the loop polls **between ticks** ([cli.py:1164-1224](../../src/wastech_orchestrator/cli.py#L1164-L1224), [process_control.py:192-279](../../src/wastech_orchestrator/process_control.py#L192-L279)). Graceful, never mid-stage.
4. **`worc status`** — read-only, DB-only report of the active or latest task ([cli.py:1225-1282](../../src/wastech_orchestrator/cli.py#L1225-L1282)), via `StateStore.open_readonly`. It is a **one-shot snapshot of one task** — re-run it to refresh, and it does not show the queue.
5. **Logging** — configured once at startup to stderr (logfmt/JSON) or, with `--log-file`, a rotating file ([configure_logging](../../src/wastech_orchestrator/cli.py#L640-L645)). Agent stdout goes to artifact files, not the console. So a separate process can observe progress two ways: poll `state.db` read-only, and tail the daemon's `--log-file`.

The gap the operator actually feels: to follow a run they keep one shell on `worc watch` (or its log) and another re-running `worc status`, and they enqueue by hand-copying files into `tasks/pending`. There is no single attended surface.

## Constraints that bound any solution

From [.agents/rules/architecture.md](../../.agents/rules/architecture.md) and the code as it stands:

1. **Single processing slot.** At most one active task. The console shows _one_ active task + a queue + recent terminal tasks — never parallel jobs. Mutating one-shot verbs (`finalize`/`rerun`/`merge-task`) assume an idle slot, so the console must refuse them (or defer to enqueue) while the daemon holds the slot — the same guard those commands already apply.
2. **Only the orchestrator does commit / push / PR / merge.** The console issues none of these directly; it enqueues files and invokes existing `cmd_*` verbs. No new git/network capability is introduced.
3. **The core does not know the CLI.** The console lives in the CLI layer ([cli.py](../../src/wastech_orchestrator/cli.py)) and dispatches to existing `cmd_*` functions; it adds no logic to `core/`.
4. **One stdin reader.** The prompt*toolkit loop is the \_only* stdin reader; the daemon child runs with stdin not inherited (it already does — [providers/process.py:16](../../src/wastech_orchestrator/providers/process.py#L16)). Background log lines print via `patch_stdout()` so they never corrupt the input line.
5. **No secrets in logs/artifacts.** The console only **renders** existing redacted log/DB content; the `RedactionFilter` and stderr-redaction already in place are unchanged. The console writes nothing new to logs.
6. **No new dependency in the hot path.** `prompt_toolkit` (and any later Textual) ships as an **optional extra** (`pip install wastech-orchestrator[shell]`); the daemon and headless/CI installs never import it. `worc shell` degrades to a clear "install the extra" message if it is absent.
7. **Read-only DB access is safe concurrently.** The console polls via `StateStore.open_readonly` — the exact path `status` already uses while a daemon may be active ([state_store.py:236](../../src/wastech_orchestrator/state_store.py#L236)); it never opens a writable connection.

## Design

### Shape: a console client over the daemon (not an engine host)

```text
┌─ worc shell (prompt_toolkit, owns stdin) ─────────────────┐
│  • renders a live header: active task + node, queue, recent│
│  • tails the daemon log → prints above the prompt          │
│  • dispatches commands → existing cmd_* / enqueue          │
│           │ spawns or attaches                             │
│           ▼                                                │
│   worc watch (the ONLY engine host — unchanged)            │
│     run_task → flow engine → captured agent subprocesses   │
│           │ writes                                         │
│           ▼                                                │
│   state.db  +  --log-file  +  tasks/pending|processing|... │
└────────────────────────────────────────────────────────────┘
```

The console never imports the engine. It supervises a `watch` **child** (or attaches to a detached one), reads `state.db` read-only, tails the log file, and enqueues files. This is the whole architectural decision; everything else is ergonomics.

### CLI surface (two deliverables, shippable independently)

- **`worc top`** _(Phase 1 — the read-only core, also usable standalone)_ — a live, auto-refreshing, **read-only** monitor: a header (active `task_id` / current node / attempt, from the flow checkpoint) + the pending queue (scanned from `tasks/pending` with the same lightweight front-matter read `watch` uses — [select_pending](../../src/wastech_orchestrator/cli.py#L648-L676)) + a tail of recent terminal tasks (`latest_task` / a small `recent_tasks` query) + the last N daemon log lines. No stdin beyond `q` to quit. Polls `state.db` read-only on an interval. This alone delivers most of the "see what's happening without blocking" value and carries near-zero architectural risk. (Equivalently surfaced as `worc status --follow` if a line-based refresh is preferred over a full-screen view.)
- **`worc shell`** _(Phase 2 — interactive control on top of the monitor)_ — a `prompt_toolkit` `PromptSession` with `patch_stdout()`. The live header/log stream from `top` renders above the prompt; the prompt accepts commands that map onto existing verbs:

  | console command | maps to | notes |
  | --- | --- | --- |
  | `run <file>` / `enqueue <file>` | copy file into `tasks/pending` | **non-blocking**: the daemon picks it up next tick; foreground blocking `run` stays the bare CLI command |
  | `ps` / `jobs` | the `top` view | one active + queue + recent — never parallel |
  | `status [<id>]` | `cmd_status` | one-shot snapshot inline |
  | `logs [<id>]` | tail artifact stdout / daemon log | read-only |
  | `prs` / `merge-task <id>` | `cmd_prs` / `cmd_merge_task` | once [orchestrator-driven-pr-merge.md](orchestrator-driven-pr-merge.md) lands; slot-guarded |
  | `finalize` / `rerun` | `cmd_finalize` / `cmd_rerun` | slot-guarded (refuse while a task is active) |
  | `up` / `down` / `restart` | spawn child / `cmd_stop` / `cmd_restart` | daemon lifecycle |
  | `cancel <id>` | de-queue pending file, or `down` | **best-effort only** — see § Cancellation |
  | `quit` / Ctrl-D | stop the supervised child (if spawned), exit | detached daemons are left running |

  `worc shell` is a **dispatcher**: each command calls an existing `cmd_*` function or a thin enqueue/tail helper. It adds no orchestration logic.

### Daemon: spawn vs attach

On start, `worc shell` checks the PID file ([read_pid_record](../../src/wastech_orchestrator/process_control.py#L97-L120)):

- **A live daemon is recorded** → **attach**: tail its `--log-file`, poll its `state.db`, leave it running on exit. (Detached/unattended runs keep using bare `worc watch`.)
- **No live daemon** → offer to **spawn** one as a managed child (`worc watch --log-file <known path>`, launched as an argv list, no shell — same no-shell-out invariant as everywhere else), capture its output into the `patch_stdout` stream, and `down` it on `quit`. Spawning requires the daemon to log to a file the console knows; the console picks that path and passes `--log-file`.

This gives the "one process to sit in" feel (spawn) without precluding the detached daemon (attach).

### Live view: where the data comes from

- **Active task + node**: read-only `state.db` poll — `find_active_tasks()` + the flow checkpoint (`current_node` / counters) the orchestrator already persists ([state_store.py:615-620](../../src/wastech_orchestrator/state_store.py#L615-L620)).
- **Queue**: scan `tasks/pending` with `select_pending` (reused).
- **Recent terminal**: `latest_task` plus a small new read-only `recent_tasks(limit)` query (no schema change).
- **Progress lines**: tail the daemon `--log-file` (already redacted, structured). Agent stdout artifact files are tailed on demand by `logs <id>`.

No new tables, no schema bump — purely new **read** views + presentation.

### Cancellation (honest scope)

`cancel <id>` is best-effort by construction:

- **Pending** (file in `tasks/pending`, no active run) → move the file out (e.g. into `tasks/rejected`); the daemon simply never picks it up.
- **Active** → there is no mid-stage cancel; the console offers `down`/`restart`, which stops the daemon **between ticks** after the current stage finishes (the existing `SIGTERM`→event path). The console must **say this plainly** rather than imply an instant kill.

True mid-task cancellation (a cooperative cancel flag threaded through the flow engine + terminating the agent subprocess via the `process_control` kill path) is a **separate, larger feature** — explicitly out of scope here (§ Out of scope).

### Dependency & packaging

Add `prompt_toolkit` under a new `shell` optional-dependency group in [pyproject.toml](../../pyproject.toml) (`[project.optional-dependencies] shell = [...]`). `worc top`/`worc shell` import it lazily and, if missing, exit with `install wastech-orchestrator[shell]`. The daemon, the engine, and all non-console commands never import it — the hot path stays lean.

## Rejected alternatives

- **B — in-process engine REPL (the sketch verbatim).** Run `run_task` inside the REPL via `asyncio.to_thread`, fan out "jobs" as `asyncio.create_task`. **Rejected:** it makes a second engine host competing with the daemon, contradicts the single-slot invariant (the "jobs" plural does not exist), and re-implements slot/resume/PID logic the daemon already owns. The console-as-client (above) reuses all of it.
- **Textual full TUI now.** A panel/log/job-table TUI is genuinely nice, but it is a heavier dependency and more surface than the first cut needs. **Deferred:** `prompt_toolkit` + `patch_stdout` covers "live logs above a prompt" with far less. Revisit Textual only if operators want panes/mouse/hotkeys (§ Out of scope).
- **Mid-stage task cancellation in the first cut.** Killing a running agent and unwinding the flow safely needs a cooperative cancel contract + subprocess termination + state cleanup — a feature in its own right. **Deferred** to a follow-up; the first cut is honest about de-queue + graceful daemon stop only.
- **HITL-over-stdin in the console.** Bridging Telegram HITL prompts into the console prompt is desirable but a separate feature with its own routing/timeout semantics. **Deferred**; Telegram stays the HITL channel for now.
- **A daemon control _socket_/IPC.** A Unix-socket RPC between console and daemon would enable richer live control. **YAGNI for now:** read-only `state.db` polling + log tailing + file enqueue + signals cover the first cut without a new protocol. Revisit if the console needs to push commands into a running task.

## Decision (recommended)

Add an **attended operator console that is a client over the existing `watch` daemon, not a second engine host.** Ship in two independently-useful pieces: **`worc top`** (a read-only, auto-refreshing live monitor — active task + node, the `tasks/pending` queue, recent terminal tasks, a tail of the daemon log; polls `state.db` read-only) and **`worc shell`** (a `prompt_toolkit` + `patch_stdout` REPL layering command dispatch onto that view — `enqueue`/`status`/`logs`/`prs`/`merge-task`/`finalize`/`rerun`/`up`/`down`/`restart`/best-effort `cancel`). The console **spawns or attaches to** the daemon, **enqueues** by dropping files into `tasks/pending`, **slot-guards** mutating verbs, and reuses the existing `cmd_*` functions. `prompt_toolkit` is an **optional `[shell]` extra**; the daemon never imports it. **No new task status, no schema bump** — only new read views + presentation.

This reframes the original idea: the orchestrator is _already_ non-blocking (daemon + captured subprocesses + one-shot control verbs); what is missing is a single attended surface to watch the single-slot queue and drive control — so the upgrade is **presentation + process supervision**, not a new execution model.

Deliberately **out of scope** (YAGNI / greenfield-MVP): an in-process engine REPL (Option B); parallel "jobs" (single-slot invariant); a full Textual TUI (revisit if panes/hotkeys are wanted); **mid-stage task cancellation** (separate feature — cooperative cancel + subprocess kill); HITL-over-stdin (Telegram stays the channel); a daemon control socket/IPC.

## Building blocks (kept from the original sketch)

The library choices the original note surfaced are the right ones for the recommended design:

- **`prompt_toolkit` + `patch_stdout()`** — the input layer that lets daemon log lines print above a live prompt without corrupting it. ([python-prompt-toolkit][1])
- **`asyncio.create_subprocess_exec`** — for spawning/supervising the `watch` child and streaming its output (argv list, no shell). ([Python docs][2])
- **Textual** — the deferred richer option if a full panel TUI is ever wanted. ([Textual][3])

Trimmed skeleton (the console event loop — _supervise + tail + dispatch_, not run the engine):

```python
# cli_shell.py (illustrative)
import asyncio
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

async def tail_daemon(log_path, child=None):
    """Stream the daemon's log file (or the spawned child's output) above the prompt."""
    ...  # follow log_path; print(line) — patch_stdout keeps the prompt intact

async def main(config):
    session = PromptSession("worc> ")
    child = await spawn_or_attach_watch(config)      # spawn `worc watch --log-file <p>` OR attach to PID file
    tail = asyncio.create_task(tail_daemon(log_path, child))
    with patch_stdout():
        while True:
            try:
                line = (await session.prompt_async()).strip()
            except (EOFError, KeyboardInterrupt):
                break
            cmd, *rest = line.split() or [""]
            if cmd in {"enqueue", "run"}:   enqueue(rest[0])          # copy into tasks/pending
            elif cmd in {"ps", "jobs"}:     render_view(open_readonly(db))  # 1 active + queue + recent
            elif cmd == "status":           cmd_status(...)
            elif cmd in {"down"}:           cmd_stop(...)             # SIGTERM between ticks
            elif cmd in {"quit", "exit"}:   break
            # … finalize / rerun / prs / merge-task / cancel (best-effort) …
    tail.cancel()
    await stop_child_if_spawned(child)
```

[1]: https://python-prompt-toolkit.readthedocs.io/ "Prompt Toolkit — Read the Docs"
[2]: https://docs.python.org/3/library/asyncio-subprocess.html "asyncio Subprocesses — Python docs"
[3]: https://textual.textualize.io/ "Textual"

## План реализации

Раздел на русском по просьбе владельца. Рекомендованное решение — **операторская консоль как клиент над существующим демоном `watch` (не второй хост движка)**: `worc top` (read-only live-монитор) + `worc shell` (prompt_toolkit-REPL поверх него). Деление на «Фазы» ниже — логическая структура работ, а не отдельные итерации/мержи. **Проверки и документация — один раз в самом конце** (`/run-checks`, затем `/sync-docs` и `prettier` по докам) — см. [§ Проверки и документация](#проверки-и-документация).

Целевой сквозной сценарий: оператор запускает `worc shell` → консоль поднимает (или подхватывает) демон `watch`, логи демона текут **над** промптом → оператор делает `enqueue task.md` (файл падает в `tasks/pending`, демон берёт его на следующем тике, ввод не блокируется) → `ps` показывает одну активную задачу + очередь + недавние терминальные → `down`/`quit` корректно гасит дочерний демон между тиками.

### Зафиксированные решения (ответы на форки)

1. **Консоль — клиент, не хост движка.** Движок остаётся только в демоне `watch`; консоль читает `state.db` read-only (`StateStore.open_readonly`), тейлит лог-файл демона, кладёт файлы в `tasks/pending`, диспатчит существующие `cmd_*`. Вариант B (in-process REPL с `to_thread`) отклонён — см. [§ Rejected alternatives](#rejected-alternatives).
2. **Один слот, не «jobs».** Вид — одна активная задача + очередь + недавние терминальные. Никакого параллельного запуска.
3. **Два независимо полезных артефакта.** `worc top` (read-only, фаза 1) и `worc shell` (интерактив, фаза 2). `top` ценен сам по себе.
4. **`prompt_toolkit` — опциональный extra `[shell]`.** Демон и headless/CI его не импортируют; `worc top`/`shell` без него падают с понятным сообщением.
5. **Без нового статуса задачи и без bump схемы** — только новые read-вью + презентация.
6. **Отмена — best-effort и честно названная.** `cancel` снимает только pending-файл или гасит демон между тиками; mid-stage cancel — отдельная фича (см. [§ Out of scope](#decision-recommended)).

### Фаза 1 — read-surface + live-монитор `worc top`

- **[state_store.py](../../src/wastech_orchestrator/state_store.py)** — read-only `recent_tasks(limit) -> list[TaskRow]` рядом с `latest_task`/`find_active_tasks` ([state_store.py:604-620](../../src/wastech_orchestrator/state_store.py#L604-L620)). Без bump схемы.
- **[cli.py](../../src/wastech_orchestrator/cli.py)** — сабпарсер `top` + `cmd_top`: периодический read-only-опрос `state.db` (активная задача + чекпойнт ноды), скан `tasks/pending` через `select_pending`, недавние через `recent_tasks`, хвост лог-файла демона; перерисовка раз в N секунд; выход по `q`. Чисто чтение. (Опционально: тот же вывод как `status --follow`.)
- **Тесты:** `recent_tasks` (порядок/лимит); `cmd_top` рендер из фейкового `state.db` + временного `tasks/pending` (одна активная + очередь + терминальные) без сети и без движка.

### Фаза 2 — интерактивная консоль `worc shell` + supervise/attach демона

- **[pyproject.toml](../../pyproject.toml)** — новая группа `[project.optional-dependencies] shell = ["prompt_toolkit>=3"]`. Ленивая загрузка; при отсутствии — выход с подсказкой `install wastech-orchestrator[shell]`.
- **`cli_shell.py`** (новый модуль в пакете) — `PromptSession` + `patch_stdout()`: рендер шапки/лога из фазы 1 над промптом; диспетчер команд на существующие `cmd_*` + тонкие хелперы `enqueue`/`tail`; **spawn-or-attach** демона через `read_pid_record` ([process_control.py:97-120](../../src/wastech_orchestrator/process_control.py#L97-L120)) — живой демон подхватываем (tail лога + read-only DB, на выходе оставляем), иначе поднимаем `worc watch --log-file <path>` дочерним процессом (argv-список, без shell) и гасим на `quit` через `cmd_stop`.
- **[cli.py](../../src/wastech_orchestrator/cli.py)** — сабпарсер `shell` + `cmd_shell` (вход в loop). Команды: `enqueue/run <file>` (копия в `tasks/pending`), `ps/jobs`, `status [<id>]`, `logs [<id>]`, `prs`/`merge-task <id>` (после [orchestrator-driven-pr-merge.md](orchestrator-driven-pr-merge.md)), `finalize`/`rerun`, `up`/`down`/`restart`, `cancel <id>` (best-effort), `quit`. **Slot-guard:** мутационные команды (`finalize`/`rerun`/`merge-task`) отказывают при активной задаче (`find_active_tasks`) — как и сами эти команды сегодня.
- **Тесты:** диспетч `enqueue` → файл в `tasks/pending`; `ps` рендер; slot-guard (`finalize` при активной → refuse); spawn/attach логика через фейковый PID-файл и фейковый `watch` (скилл `fake-cli` для дочернего процесса); `cancel` pending → файл уехал в `tasks/rejected`; graceful `down` → `SIGTERM` пути `cmd_stop`. prompt_toolkit-loop тестируем через инъекцию заранее заданного списка строк (не реальный TTY).

### Инварианты (соблюдены)

- **Один слот**: вид — одна активная + очередь; мутационные команды slot-guard'ятся.
- **Коммит/пуш/PR/merge — только оркестратор**: консоль лишь enqueue'ит файлы и зовёт существующие `cmd_*`; новых git/сетевых возможностей нет.
- **Ядро не знает CLI**: консоль живёт в слое `cli.py`, логики в `core/` не добавляет.
- **Один читатель stdin**: prompt_toolkit-loop; дочерний демон stdin не наследует; фоновые логи — через `patch_stdout()`.
- **Без секретов**: консоль только рендерит уже редактированные лог/DB-данные; своих логов не пишет.
- **Без shell-интерполяции**: дочерний `watch` запускается argv-списком.
- **Без новой зависимости в hot-path**: `prompt_toolkit` — extra `[shell]`, демон его не импортирует.

### Связи и хвосты

- **`prs`/`merge-task`** ([orchestrator-driven-pr-merge.md](orchestrator-driven-pr-merge.md)) — консоль их подхватит как обычные команды, когда они появятся; зависимость по порядку не жёсткая (без них просто нет этих двух verb'ов).
- **Mid-stage cancel** — отдельный backlog-айтем: кооперативный cancel-флаг через флоу-движок + терминация субпроцесса агента (путь `process_control` kill). Записать в [follow_ups.md](follow_ups.md).
- **HITL-в-консоли** — мост Telegram-HITL ([core/hitl.py](../../src/wastech_orchestrator/core/hitl.py)) в промпт консоли; отдельная фича. Записать в [follow_ups.md](follow_ups.md).
- **Textual-TUI** — богатый вариант с панелями/хоткеями, если понадобится. Записать в [follow_ups.md](follow_ups.md).
- **Control-socket/IPC** — если консоли понадобится пушить команды в активную задачу (а не только enqueue + сигналы). Записать в [follow_ups.md](follow_ups.md).

### Проверки и документация

Всё — **один раз после всех фаз**.

- **Проверки:** `ruff check .`, `mypy src`, `pytest` (через `/run-checks`); затем `npx prettier@3 --write "**/*.md"` по затронутым докам.
- **Документация (`/sync-docs`):** [Functional Map](../functional/index.md) (блок CLI/демона — добавить `top`/`shell` рядом с `watch`/`status`/`stop`/`restart`; отметить, что консоль — клиент демона, не хост движка); [docs/operations.md](../operations.md) — операторская заметка про `worc top`/`worc shell` (spawn/attach, enqueue в `tasks/pending`, slot-guard, честные границы `cancel`); [docs/configuration.md](../configuration.md)/README — extra `[shell]` и `pip install wastech-orchestrator[shell]`; при изменении топологии — модель C4 в [docs/likec4](../likec4).
- **Отложенные хвосты** — в [follow_ups.md](follow_ups.md) (см. § Связи и хвосты): mid-stage cancel, HITL-в-консоли, Textual-TUI, control-socket.
