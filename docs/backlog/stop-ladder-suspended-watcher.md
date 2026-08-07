# The stop ladder cannot stop a suspended watcher

Status: **implemented 2026-08-07** Date: 2026-08-07 (revised the same day after a code walkthrough) Owner: Vladimir Makarevich

All three parts shipped together. Three as-built notes, none of them design changes: the ladder's flag docstring also lost a stale "(with timeout escalation)" claim about `--force` that the pending-stop contract had already made false; the `--force-full` help string now says "idle or busy" so the fix is visible without reading the docstring; and the suspended-watcher message keeps offering `--force-full` beside the `kill -CONT` advice rather than replacing it, since a watcher that re-stops still needs the hard rung. `read_process_state` is public (not `_`-prefixed) because `cli.py` calls it; tests pin it through `monkeypatch` rather than a new injected seam, matching how `stop_process` is already faked in `test_cli_stop.py`.

A live operator incident on 2026-08-07 (`wastech-mdlint`) left a watcher that **no form of `stop` could stop** — neither `wastech-orchestrator stop`, nor `stop --force-full`, nor the console's `down --force-full`. All three printed the same timeout line and left the daemon holding its PID file, which also blocks any replacement watcher from starting. The operator had no documented way out; recovery took a hand-sent `kill -CONT`.

Two independent defects combine into that dead end, plus one message that actively misdirects. Each is small; the third is only worth doing alongside the first two.

## The incident, in one paragraph

`worc restart` stopped the previous watcher, wrote its own PID into `.worc/orchestrator.pid` (it runs `cmd_watch` in-process — [`cli.py:3406`](../../src/wastech_orchestrator/cli.py)), and was then **suspended** — `ps` showed state `T` with a zsh parent and no children. Ctrl-Z or a background stdin read (SIGTTIN) both produce that state; the trigger was not recorded, and the fix does not depend on which it was. A suspended process runs no loop, so it never observed the `orchestrator.stop` sentinel, and the SIGTERM the soft stop sent it simply sat pending. Both tasks in flight were already terminal, so nothing was active. `kill -CONT <pid>` resumed it, the pending SIGTERM fired immediately, and it exited through the confirmed-terminal branch — PID file, sentinel and children file all reaped, state consistent.

## A — `--force-full` does not escalate when no task is active

**Problem.** [`_resolve_stop_level`](../../src/wastech_orchestrator/cli.py) tests activity **before** it tests the flags:

```python
if not has_active_task(config):
    return _StopDecision(proceed=True, level="soft")  # cli.py:3246
if force_full:
    return _StopDecision(proceed=True, level="full")  # cli.py:3248 — unreachable when idle
```

So on an idle daemon `--force-full` silently becomes a soft stop and `_stop_via_group_kill` never runs. The reasoning behind the early return is sound for a healthy daemon — with nothing in flight there is no agent to interrupt, so soft is enough — but it treats "no active task" as "soft will work", and those are different claims. An idle daemon that is wedged, suspended, or stuck in a syscall is exactly the case where the operator needs the hard rung, and it is exactly the case where the ladder removes it.

**Proposed design.** Move the `force_full` test above the activity probe. `--force-full` then always means "full", idle or busy; every other rung keeps its current behavior, including the idle-means-no-prompt shortcut. Roughly a three-line reorder plus a docstring correction — the docstring at `cli.py:3239` states the current (wrong) precedence and must change with it.

Note what this is **not**: it is not a new hard rung and it does not make hard stops easier to reach by accident. `--force-full` is already the explicit, documented, hardest form; this only stops the ladder from quietly discarding it.

**This overturns a recorded decision, so change the test deliberately.** [`test_idle_stops_soft_no_prompt_for_any_form`](../../tests/test_cli_stop.py) asserts today's behavior in so many words — `assert decision.level == "soft"  # idle: ordinary stop even with --force-full` (`test_cli_stop.py:53`) — over the whole flag matrix. That is a decision, not an oversight, and the argument above ("no active task" is not "soft will work") is what overturns it. Split the test rather than patching it: idle + no flag / `--force` stays `soft`; idle + `--force-full` becomes `full`.

## B — a soft stop cannot wake a stopped process

**Problem.** [`_stop_via_signal`](../../src/wastech_orchestrator/process_control.py) probes liveness, writes the sentinel, sends `term_sig`, then polls (`process_control.py:588`). Against a process in state `T` every one of those steps succeeds and none of them accomplish anything: `is_running` uses `os.kill(pid, 0)`, which reports a stopped process as alive; the sentinel is a file the process is not running to read; and the SIGTERM at `process_control.py:618` is queued, not delivered, until something resumes the process. The poll then runs out the full timeout and reports `timed_out`, which is honest but unhelpful — the stop is not slow, it is impossible.

**Proposed design.** Pair `SIGCONT` with `term_sig` on the POSIX path. `SIGCONT` to a process that is not stopped is a no-op by definition, so this needs no detection, no new dependency and no platform branch beyond the POSIX one it already sits in — which matters here, because detecting state `T` properly is _not_ cheap: `_read_proc_start_time` already documents that non-Linux POSIX has no dependency-free process-state source, and this module may not shell out. `SIGCONT` sidesteps that constraint entirely.

**Order: `term_sig` first, then `SIGCONT`** — not the reverse. Both orders work against a plain Ctrl-Z, but `SIGTTIN` (one of the two candidate triggers) re-arms itself, which leaves `SIGCONT`-then-`SIGTERM` a real window: the woken process can retry its terminal read on one core and re-enter state `T` while the other core is still executing the `kill` for `SIGTERM` — and the SIGTERM queues again, restoring exactly the dead end this fixes. Sending `SIGTERM` first closes the window by construction: it is already pending when the kernel makes the process runnable, so it is delivered on the first return to userspace. `SIGCONT` discards pending **stop** signals only, so a queued `SIGTERM` is untouched by it.

```python
try:
    kill_fn(pid, term_sig)
    if cont_sig is not None:
        kill_fn(pid, cont_sig)  # wake a stopped daemon so the queued term_sig is delivered
except OSError as exc:  # raced to exit between the probe and either signal
    ...
```

Both calls sit inside the existing `try`, so an `ESRCH` from the `SIGCONT` lands in the already-written raced-to-exit branch — no new branch appears.

**`SIGCONT` must be resolved defensively, like `SIGKILL` already is.** `signal.SIGCONT` does not exist on Windows, so naming it directly in a signature default would raise at **import time** there — a cross-platform invariant break, and one that no POSIX test would catch. Use the pattern already in `stop_process` (`kill_sig: int = getattr(signal, "SIGKILL", signal.SIGTERM)`, `process_control.py:388`): `cont_sig: int | None = getattr(signal, "SIGCONT", None)`, with the mirrored `CONT = getattr(signal, "SIGCONT", 18)` sentinel in the test module beside its existing `KILL`.

**What this buys is a _clean_ exit, not merely a faster one.** A resumed daemon takes the pending SIGTERM through `StopController`, finishes its node, and reaps its own PID file, sentinel and children file — the confirmed-terminal branch, exactly how the live incident ended once `kill -CONT` was sent by hand. A (`--force-full`) reaches the same wedged daemon by SIGKILL, where `stop` reaps the handles on the process's behalf and recovery is deferred to the next `resume()`. Both end the dead end; only B ends it tidily. That is the real reason to ship B even though A alone unblocks the operator.

Windows is unaffected — there is no SIGSTOP, and `_stop_via_pid_file` sends no signals.

**Do _not_ mirror this into `_stop_via_group_kill`.** SIGKILL reaches a stopped process regardless, so a `SIGCONT` there cannot change any observable outcome — it would be code that is unreachable by construction, untestable through the `kill_fn` seam, and a standing target for `vulture` and the over-engineering review. Symmetry is not worth a permanent no-op on the hard path.

**Test shape.** `kill_fn` is already an injected seam and `FakeProcess` already records every signal, so the assertion is on the recorded call sequence — `fake.signals == [TERM, CONT]` — plus two regressions: an `ESRCH` on the first signal still yields `already_dead`, and the Windows path (`can_signal=False`) still sends no signal at all.

## C — the timeout message names the rung that A just disabled

**Problem.** [`_timed_out_stop_message`](../../src/wastech_orchestrator/cli.py) tells the operator to "retry with `--force-full` to interrupt now and reap the agent subtree" (`cli.py:3304`). With defect A in place and no active task, that retry provably does nothing — the operator runs it, gets a byte-identical message, and has no next move. The message is also the only operator-facing account of the state, and it never mentions that the process might be stopped rather than busy.

**Proposed design.** Once A lands the advice becomes true again, so most of this closes itself. What remains is worth one line: on Linux, where `/proc/<pid>/stat` field 3 is free to read, name the state when it is `T` ("watcher <pid> is suspended (state T); resume it with `kill -CONT <pid>`"). Elsewhere, no detection and no claim. Lowest value of the three; do not do it alone.

**The `/proc` read belongs in `process_control`, not in `cli`.** That module already owns the only `/proc/<pid>/stat` parser in the codebase (`_read_proc_start_time`), including its Linux gate and its never-raises contract; a second, independent parser in `cli.py` would put OS plumbing in the layer that is supposed to own only wording and exit codes. Add a sibling `read_process_state(pid) -> str | None` there and let `_timed_out_stop_message` merely phrase what it returns. The module stays print-free — the string is still built at the call site.

**State `D` is deliberately out of scope.** It is the one state where `--force-full` also fails (SIGKILL does not reach a task in uninterruptible sleep), so naming it would genuinely save an operator a wasted retry — but no `D`-state incident has been observed here, and inventing the message for a hypothetical is exactly the speculation this repository's review pass rejects. Add it the first time a run actually hits it.

## Scope / risk

All three are confined to the stop path — `_resolve_stop_level` and `_timed_out_stop_message` in `cli.py`, `_stop_via_signal` (plus, for C, a `read_process_state` sibling) in `process_control.py`. No flow, schema, config key or provider is touched, and the security envelope is unchanged: A restores access to a rung the operator already had to ask for by name, and B adds one wake-up signal alongside the SIGTERM that path already sends.

**A widens an existing group-kill exposure; say so rather than discover it later.** With A in place `--force-full` reaches `_stop_via_group_kill` on an **idle** daemon too, and that path is `killpg(getpgid(pid), SIGKILL)`. If the daemon does not lead its own group, that kills whatever group it is in. The mitigation already exists — `ensure_own_process_group()` runs in `cmd_watch` (`cli.py:3192`) — but it is best-effort and swallows `EPERM` by design. The case it does not cover is a non-interactive shell, where there is no job control to give a background job its own group:

```bash
#!/bin/bash
worc watch &             # no job control in a script: same process group as the script
worc stop --force-full   # after A this is reachable while idle → killpg takes the script with it
```

The risk class is not new (the identical shape exists today against a busy daemon) and `--force-full` is a rung the operator must name explicitly, whose documented meaning is "kill the group". So: accepted, not mitigated further — recorded here so the PR states it rather than a later incident finding it.

**Also in the same change** (the branch-scoped doc rule): the `_resolve_stop_level` docstring at `cli.py:3239`, which states the wrong precedence today; the POSIX-soft description inside the `stop_process` docstring and the `_stop_via_signal` docstring in `process_control.py`; the `orchestrator.pid` / `orchestrator.stop` row in `packaged/guide/footprint.md`, the only shipped operator-facing text about these handles, which should gain the way out of "the PID file survived and no watcher can start"; and a breadcrumb in [main-docs-reconstruction-notes.md](main-docs-reconstruction-notes.md) — the stop ladder is described on `main`, most likely in `operations.md`. The console needs no change: `down` forwards its flags verbatim into `cmd_stop` (`cli_shell.py:357`), so it inherits A for free.

A and B are independent and either can ship alone, but shipping only B leaves `--force-full` broken for every idle-daemon failure that `SIGCONT` does not happen to cure, and shipping only A leaves the stop path taking a needless timeout against a suspended process — and reaching for SIGKILL where a clean shutdown was available. Ship both.

**Not proposed, so it is not re-raised:** a `worc stop --kill` / `kill -9` convenience rung. `--force-full` already is that rung once A is fixed, and a second hard form would have to re-answer the agent-subtree reaping question that `_stop_via_group_kill` already answers correctly.
