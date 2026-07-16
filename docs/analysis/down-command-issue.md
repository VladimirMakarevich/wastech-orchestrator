# Analysis: `down --force` soft-timeout kills the watcher but can leave the active agent alive on POSIX

Date: 2026-07-16

## Symptom

Live repro from `wastime-app-content` task `rework-ch04-calendars-2`, node `story_critic`, provider `claude`:

```text
worc> down --force
stop: watcher 54483 did not exit in 30s; sent SIGKILL
stop: note: task rework-ch04-calendars-2 is still running (parked at node story_critic) ...
```

But the provider artifact
`/Users/a1234/Documents/GitHub/wastime-app-content/.worc/logs/rework-ch04-calendars-2/stages/story_critic/run-000286/1-claude/stdout.log`
kept growing after that message and still showed Claude activity, including a later heartbeat:

```text
ts=2026-07-16 18:48:24,421 level=info task_id=rework-ch04-calendars-2 node_id=story_critic provider=claude attempt=1 timeout_seconds=7200 elapsed_seconds=240.0 msg="provider heartbeat"
```

That matters because `stdout.log` here is not the daemon log. It is the provider process's own stdout stream. If it keeps growing after `sent SIGKILL`, the active agent survived the stop.

## Expected behavior

`down --force` is only cooperative until the current flow-node boundary. It is allowed to wait while the active node is still running.

The bug is what happens after the grace timeout expires. At that point the stop ladder contract says the escalation must reap the running agent subtree too, not only the watcher process. Otherwise the watcher dies, but the in-flight provider keeps running detached and the task is left parked mid-node with no owner waiting for the result.

So the problem is not "30 seconds was too short". The problem is "timeout escalation is incomplete".

## Code-level root cause

The wiring is almost there:

- `cmd_stop()` passes both `children_file` and `subtree_kill_fn=agent_process.kill_agent_subtree` into `process_control.stop_process()` ([cli.py](../../src/wastech_orchestrator/cli.py)).
- Provider runs are launched with `start_new_session=os.name != "nt"` in `run_process()`, so on POSIX the agent leads its own session/group ([providers/process.py](../../src/wastech_orchestrator/providers/process.py)).
- The POSIX hard rung `_stop_via_group_kill()` uses the recorded child handle and actually calls `subtree_kill_fn(...)` ([process_control.py](../../src/wastech_orchestrator/process_control.py)).

But the POSIX soft rung `_stop_via_signal()` does not. On timeout it executes only:

```python
kill_fn(pid, kill_sig)
```

against the watcher PID, then removes the PID file and returns success.

Because the provider is no longer in the daemon's process group, killing only the watcher PID does not kill the active agent subtree. The watcher disappears, the task remains parked, and the provider keeps writing into its own artifacts until it exits on its own.

## Why the live logs match this root cause

The console transcript and the artifact behavior line up exactly with the code:

1. `down --force` waits 30 seconds for a cooperative node-boundary stop.
2. The timeout branch in `_stop_via_signal()` sends `SIGKILL` only to the watcher PID.
3. The watcher dies and prints `sent SIGKILL`.
4. The provider process survives because it runs in its own session/group.
5. Its `stdout.log` continues to receive heartbeats and output even though the daemon is already gone.
6. A second `down --force` then sees no PID file and reports `no running watcher`, even though the detached agent has only just finished or is still finishing.

## Required follow-up

The fix belongs in the POSIX soft-timeout path:

- thread the recorded child handle into `_stop_via_signal()`;
- on timeout, kill the watcher and then reap the recorded active-agent subtree with the same primitive used by `--force-full`, or factor that timeout escalation through a shared helper so the two paths cannot diverge again;
- add a regression test that a timed-out POSIX `down --force` kills the recorded agent subtree, including a grandchild that broke into its own group/session;
- add a behavior-level test that no further provider bytes appear after the timeout escalation completes.

Until that is fixed, the claim that every stop route already reaps the whole agent subtree is too strong: the POSIX `down --force` timeout rung is still a gap.
