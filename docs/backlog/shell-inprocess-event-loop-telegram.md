# `worc shell` runs verbs on its own event loop, so every Telegram call from them fails

Status: **proposed** Date: 2026-07-26 Owner: Vladimir Makarevich

A task started from the interactive console executes **in the console's process, on the thread running the REPL's asyncio loop**. The Telegram client is a synchronous wrapper that owns a short-lived loop per call (`asyncio.run`), and `asyncio.run` refuses to run inside a running loop. Every Telegram send from such a task therefore fails deterministically — the progress pings are swallowed with a warning, and a HITL gate parks the task to `manual_action_required` for a transport reason that has nothing to do with Telegram's health.

Observed on the `WastimeApp` target while a task ran from `worc shell`:

```
ts=2026-07-26 23:11:19,545 level=warning task_id=restructure-ch16-questions2
error="asyncio.run() cannot be called from a running event loop" msg="send_trace send failed"
```

`worc preflight` reports `telegram: OK (bot=@w_orc_bot, chat=…, polling ready)` at the same time — the transport is healthy; the call site is wrong.

## Root cause

The whole orchestrator contains exactly two `asyncio.run` call sites, and they are nested:

1. The REPL: [`cli_shell.py:632`](../../src/wastech_orchestrator/cli_shell.py#L632) — `asyncio.run(_loop())` drives the `prompt_toolkit` session and the log tailer.
2. The Telegram client: [`telegram.py:589`](../../src/wastech_orchestrator/notify/telegram.py#L589) (and `send_prompt` [:661](../../src/wastech_orchestrator/notify/telegram.py#L661), `poll_reply` [:732](../../src/wastech_orchestrator/notify/telegram.py#L732)) — `python-telegram-bot` 21+ is async, so each synchronous entry point wraps its coroutine in its own `asyncio.run`.

Between them sits [`_run_cli_command`](../../src/wastech_orchestrator/cli_shell.py#L307), which dispatches a console verb by calling `ctx.run_cli(argv)` — `cli.main` — **inline** ([cli_shell.py:316](../../src/wastech_orchestrator/cli_shell.py#L316)). Nothing moves that work off the loop thread, so a task driven by `rerun` (or any other in-process verb) runs its entire node graph inside the REPL's event loop, and the first Telegram call raises `RuntimeError: asyncio.run() cannot be called from a running event loop`.

The daemon path is unaffected: `up` / `watch` spawn a separate process, `cli.py` has no event loop at all, and Telegram works there normally. This is why the same task notifies correctly under `watch` and silently stops notifying when driven from the console.

Verbs that run in-process today: `rerun`, `stop`, `restart`, and the `_FORWARD_VERBS` set — `status`, `tasks`, `prs`, `merge-task`, `finalize`, `list`, `clear` ([cli_shell.py:71](../../src/wastech_orchestrator/cli_shell.py#L71)). Only the task-executing ones reach the notifier, but the defect is structural, not per-verb.

## Blast radius

| Surface | Behavior today |
| --- | --- |
| `send_trace` / `send_notification` | [`_safe_send`](../../src/wastech_orchestrator/notify/telegram.py#L277) catches the `RuntimeError`, logs one warning, returns `False`. The message is lost; the run continues. |
| `ask_human` / `start_ask` | The same exception is caught at [`start_ask`](../../src/wastech_orchestrator/notify/telegram.py#L179) → warning + `AskHandle(delivered=False)`. |
| `wait_for_answer` | Sees `delivered=False` and returns `AskResult(answered=False, failure="transport_error")` immediately ([telegram.py:222](../../src/wastech_orchestrator/notify/telegram.py#L222)). |
| A HITL node | [`_require_answer`](../../src/wastech_orchestrator/core/flow/nodes/hitl.py#L110) raises `NodeManualRequired("human input failed (transport_error)")` — the task parks to `manual_action_required` without a human ever seeing the question. |

So the failure is loud where it matters (the HITL gate fails closed) and silent where it does not (progress pings) — but in both cases the reported cause points at the transport, while the transport is fine. An operator reading `transport_error` next to a green `telegram: OK` has no path to the real explanation.

## Proposed direction

Two independent layers; both are worth having, and the first one is the actual fix.

1. **Do not run a nested CLI command on the REPL's loop thread.** In [`_run_cli_command`](../../src/wastech_orchestrator/cli_shell.py#L307), hand `ctx.run_cli(argv)` to a worker thread (`asyncio.to_thread` in the async dispatch path, or an explicit executor) and await the result. Beyond fixing Telegram, this stops a long `rerun` from freezing the prompt and the log tailer for the duration of the task — today the console is dead while the task runs. Watch out for: `SystemExit` still has to be caught on the worker side and mapped to `ShellResult` exactly as now; `patch_stdout()` must stay in force while the worker prints; `dispatch` is currently sync and is called from the loop, so the seam has to stay testable headless (the existing `run_cli` injection point makes that straightforward).

2. **Make the Telegram client loop-safe regardless of caller.** The notifier can be invoked from anywhere, so it should not assume a bare thread:

   ```python
   def _run_sync(factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
       try:
           asyncio.get_running_loop()
       except RuntimeError:
           return asyncio.run(factory())
       with ThreadPoolExecutor(max_workers=1) as pool:
           return pool.submit(lambda: asyncio.run(factory())).result()
   ```

   Pass a **factory**, not a coroutine object — a coroutine cannot be awaited twice, and the fallback path must be able to construct it inside the worker. Apply to all five entry points of `_HttpTelegramClient` (`send_message`, `get_me`, `get_chat`, `send_prompt`, `poll_reply`) so no call site is left to remember the rule. Note `poll_reply` blocks for the ask timeout; on the worker-thread path that must stay interruptible by the same deadline it uses today.

## Acceptance criteria

- A task run with `rerun` from inside `worc shell` delivers its Telegram trace and notification messages, and a HITL gate reaches a human — the same as under `watch`.
- The REPL stays responsive while an in-process verb runs: the prompt accepts input and the daemon-log tail keeps printing.
- A unit test drives `_HttpTelegramClient` (fake bot) from inside a running event loop and asserts the send succeeds rather than raising.
- A shell test asserts the nested CLI runs off the REPL loop thread (e.g. the injected `run_cli` records `asyncio.get_running_loop()` raising in its own thread).
- `SystemExit` handling, exit codes, and the `--non-interactive` forwarding for `rerun`/`stop`/`restart` are unchanged — covered by the existing `tests/test_cli_shell.py` cases.
- No behavior change for the daemon path (`up`/`watch`), which never had a loop.

## Out of scope

- Making the orchestrator async. The fix is about *where* synchronous work runs, not about converting the core.
- The single-bot-token long-poll constraint (409 Conflict) and the daemon-owned poll broker — a separate, larger item already noted under the [`worc listen`](archive/worc-listen-remote-control.md) entry.
- Telegram formatting, redaction, and the ask protocol itself; all unchanged here.
