# Reliable stop: make POSIX `down --force` a pending graceful stop, not a timeout kill

**Status:** open **Priority:** P0 / critical **Source:** [2026-07-16 down-command analysis](../analysis/down-command-issue.md)

Most of the reliable-stop work is already shipped: `run_process` timeout/interrupt, daemon-exit cleanup, and `--force-full` use the recorded active-agent handle and reap the whole subtree. The remaining gap is narrower and live-confirmed: on POSIX, a timed-out soft `down --force` still tries to kill the watcher after the grace period. That is both semantically wrong for a "graceful first" stop and operationally broken because it can leave the active provider process alive in its own session.

## Problem

The live repro in `wastime-app-content` task `rework-ch04-calendars-2`, node `story_critic`, showed the console message:

```text
stop: watcher 54483 did not exit in 30s; sent SIGKILL
```

yet the provider artifact
`.../.worc/logs/rework-ch04-calendars-2/stages/story_critic/run-000286/1-claude/stdout.log`
kept receiving Claude output afterward, including a later heartbeat at `2026-07-16 18:48:24`.

That means the active agent survived the timeout escalation. The operator decision is that `--force` should stay purely graceful; the hard interrupt path already exists as `--force-full`. So this is not a "make the timeout longer" issue and not a "kill the whole subtree better" issue. It is a stop-contract issue:

- `down --force` is intentionally cooperative only until the current node boundary.
- If the grace timeout expires first, `down --force` should remain a pending graceful stop, not turn into a hard kill.
- The only command that should interrupt the running node immediately is `--force-full`.
- If soft timeout kills only the watcher, the task is parked mid-node with no daemon waiting for the result while the detached provider keeps running into its own artifacts.

## Root cause

The code already has the right ingredients:

- `cmd_stop()` passes `children_file` and `subtree_kill_fn=agent_process.kill_agent_subtree` into `stop_process()` (`cli.py`).
- `run_process()` launches provider subprocesses with `start_new_session=os.name != "nt"`, so on POSIX the agent leads its own session/group (`providers/process.py`).
- The POSIX hard rung `_stop_via_group_kill()` uses the recorded child handle and actually calls `subtree_kill_fn(...)` (`process_control.py`).

But the POSIX soft rung `_stop_via_signal()` does not stay graceful. On timeout it does:

```python
kill_fn(pid, kill_sig)
```

against the watcher PID, then reaps the PID file and returns success. Because the provider no longer shares the daemon's process group, this kill does not reach the active agent subtree. So the current soft-timeout behavior is wrong in two ways at once:

- it is no longer graceful, because it starts killing processes after the timeout;
- it is not even a complete hard stop, because it kills only the watcher and can leave the provider alive.

## Required outcome

A timed-out POSIX `down --force` must stay a soft stop:

- request graceful shutdown and wait for the current node boundary;
- if the timeout expires first, do **not** kill the watcher or the active agent subtree;
- keep the stop request pending (`stop_file`, PID/child handles intact) so the daemon can still observe it and exit on its own once the current node finishes;
- return control to the operator with an explicit "graceful stop still pending" message that points to `--force-full` as the immediate interrupt option;
- leave `--force-full` as the only hard-stop path.

The operator-visible contract after this change should be:

- `--force` = "finish the current node if possible, then stop";
- `--force-full` = "interrupt now and kill the subtree".

## Acceptance criteria

- [ ] A regression test proves that a POSIX soft-stop timeout does **not** send the post-timeout kill to the watcher PID and does **not** reap the recorded active-agent subtree.
- [ ] After a timed-out `down --force`, the stop request remains pending: PID file / stop sentinel / child handle survive until the daemon itself exits or the operator escalates with `--force-full`.
- [ ] The CLI message after soft-stop timeout says, in substance, that graceful shutdown is still pending and that `--force-full` is the way to interrupt immediately.
- [ ] The running node is allowed to finish naturally after the CLI has returned from the timed-out `down --force`, and the daemon exits cleanly at the next node boundary when it sees the already-pending stop request.
- [ ] `--force-full` behavior remains unchanged: it still interrupts immediately and reaps the active subtree.
- [ ] Duplicate-watcher protection remains intact while graceful shutdown is pending; a second watcher must not start against the still-live daemon.

## Out of scope

- Progress-based timeout extension or heartbeat-aware waiting; this task chooses the simpler "pending graceful stop" model instead.
- Changing timeout defaults.
- A new terminal `cancelled` task status; that remains a separate product decision.
- Full OS-level containment (`cgroup`, PID namespace, Windows Job Object); this task only closes the currently reachable recorded-child gap.
- The Windows owner E2E smoke for `--force-full`; that validation remains separate.

## Likely implementation areas

- `src/wastech_orchestrator/process_control.py`
- `src/wastech_orchestrator/cli.py`
- `src/wastech_orchestrator/providers/process.py`
- `tests/test_process_control.py`
- `tests/providers/test_subtree_kill_posix.py`
- `docs/operations.md` or `docs/backlog/follow_ups.md` for the updated `--force` vs `--force-full` contract

## Notes

- This task is the remaining slice of the older "Reliable stop: no orphaned agents" backlog item, after most of that work shipped.
- The live evidence matters here because the earlier claim that every stop route already reaps the whole agent subtree is no longer only a local-test question: the POSIX soft-timeout path has now been disproven by an actual run.
- The desired end state is intentionally asymmetric: `--force` stays soft even after timeout; `--force-full` owns all hard-stop semantics.
