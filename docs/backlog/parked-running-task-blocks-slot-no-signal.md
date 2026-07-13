# A stopped task stays `running` and silently blocks the slot — no operator signal, `stop` can't clear it

Status: **proposed** Date: 2026-07-13 Owner: Vladimir Makarevich

This is a design record for an operator-visibility gap found in a live `wastech-mdlint` session. The operator ran `worc shell` → `up` → the daemon reached the `planning` node of `p7-05-integration-tests-docs-2`, then **stopped it cleanly with `down`**. They then tried to start a different task and hit _"a task is active"_; `worc stop --force-full` reported _"no running watcher (no PID file)"_, yet `worc status` still showed the task as `running` (`node=planning`, `elapsed_since_update ≈ 6 min`). Nothing was actually running — no daemon, no agent process — but the task row held the single processing slot and no command surfaced why or how to clear it. The reconciliation tool exists (`finalize` cleared it in one shot), but the operator had no way to know that from the messages they saw.

## The problem

Every piece of this is working as currently designed; the gap is that the design gives the operator no signal to act on:

1. **`down`/`stop`/`--force-full` never transition the in-flight task.** They stop the _daemon_ (the PID file) only. By the recovery model, the single unfinished task is deliberately left `running` at its checkpoint so the **next** daemon start resumes it (`RecoveryReconciler.reconcile` → `RecoveryAction.RESUME`, [core/recovery.py:65-69](../../src/wastech_orchestrator/core/recovery.py#L65-L69)). A `running` row after a clean `down` is expected — it means "parked, awaiting resume", not "actively executing".
2. **A parked `running` row holds the one slot.** `acquire_slot` refuses any _other_ task while any task is active ([core/orchestrator.py:564-566](../../src/wastech_orchestrator/core/orchestrator.py#L564)), so a direct `worc run <other-file>` raises `SlotBusyError` — _"a task is active"_ — with no hint that the blocker is a parked, resumable task rather than a live run.
3. **`stop` looks like the fix but is the wrong tool.** With the daemon already down, `stop`/`--force-full` correctly reports _"no running watcher (no PID file)"_ — and it could never have helped: `stop` has no authority over a task row. The operator reasonably reads "I stopped it and force-killed, yet it's still running" as a bug.
4. **`status` does not distinguish "executing now" from "parked, no daemon".** It prints `status=running` in both cases ([cli.py](../../src/wastech_orchestrator/cli.py)), even when there is no PID file and no agent process — so `elapsed_since_update` quietly climbing is the only (easily-missed) tell.

Net effect: after a routine stop-mid-task the operator is stuck with a task that is neither finishing nor obviously stoppable, blocking the whole queue, and the one command that reconciles it (`finalize`, or `rerun --continue` to actually continue it, or simply restarting the daemon to let `resume()` finish it) is nowhere in the messages they hit. The correct recovery today is `worc finalize <id> --as failed` (or `rerun <id> --continue` / restart `watch` to resume), verified against the live repo.

This is adjacent to but distinct from the two open items it sits between: [shell-quit-safety-and-task-staging.md](shell-quit-safety-and-task-staging.md) (Part A makes `quit` loud when the daemon is _still running_; here the daemon is _already stopped_ and the survivor is the task row) and the 🔴 [Reliable stop: no orphaned agents](README.md) item (which is about orphaned agent _processes_; here the process is gone and the survivor is DB state).

## Constraints

- **Do not change the recovery model.** Leaving one unfinished task `running` for the next start to resume is the crash-recovery invariant ([core/recovery.py](../../src/wastech_orchestrator/core/recovery.py)); this is a signalling/observability fix, not a state-machine change. `stop` must stay daemon-only — it must not gain the authority to transition tasks.
- **Only the orchestrator drives state transitions** ([.agents/rules/architecture.md](../../.agents/rules/architecture.md)); any new "close it" affordance routes through the existing `finalize`/`rerun` paths and `state.db` stays the sole authoritative state — no hand SQL edits.
- **Read-only surfaces stay read-only.** A `status`/`top`/`list` label change must not open `state.db` for writing or probe anything it doesn't already (it already knows the PID-file/`has_active_task` state — [cli.py:1078](../../src/wastech_orchestrator/cli.py#L1078)).
- **Cross-platform** (Windows/macOS/Linux) and no new blocking prompt that fights the `worc shell` stdin reader (H1); reuse the existing non-interactive/`--yes` conventions.
- No secrets/diff/prompt content in any new message — task id, node id, and daemon-liveness are enough.

## Decision

_To be decided — options below; recommendation first. They are independent and composable._

**Option A (recommended): make stop tell the operator the slot is still held.** When `down`/`stop`/`restart` stops the daemon (or finds none) **and** a task is still active (`has_active_task`), print an explicit, actionable line — e.g. _"note: task `<id>` is still `running` (parked at `<node>`), holding the processing slot. It will resume on the next `up`/`watch`; to continue it now run `rerun <id> --continue`, or to close it run `finalize <id> --as failed`."_ This is the single highest-value fix: it turns the dead-end into a signposted decision at the exact moment the operator stops.

**Option B: label a parked task distinctly in the read-only views.** In `status`/`top`/`list`, when a row is `running` but there is no live daemon PID (and, where cheap, no recorded agent process), show `parked (no daemon)` (or `running (no daemon — resumes on next watch)`) instead of a bare `running`. Removes the "it says running but nothing runs" confusion at a glance. Pairs naturally with A.

**Option C: surface the blocker in the `SlotBusyError` message.** When `run`/the scheduler refuses because the slot is held, name the holder and its state — _"a task is active: `<id>` (parked at `<node>`, no daemon). Resume it (`up`/`rerun --continue`) or close it (`finalize`) before starting another."_ — instead of today's bare _"a task is active"_.

Recommendation: **A + B** — A signposts the recovery at stop time, B keeps the state honest in every monitor afterward. C is a cheap bonus wherever the refusal is raised.

## Open questions

- For Option B, is PID-file absence a sufficient "no daemon" signal, or should the view also check the recorded agent process (couples to the 🔴 Reliable-stop child-handle work)? Leaning PID-file-only for a first cut — cheap, already known, no new probing.
- Should Option A's message differ between soft `down` (task will resume cleanly) and `--force-full` (task was interrupted mid-step, may need `--continue`)? Probably one message covers both — the levers are identical.
- Is there appetite for a convenience verb (e.g. `worc cancel-active` / `finalize --active`) that reconciles "whatever is holding the slot" without the operator having to look up the id first? Out of scope here; note only.

## Implementation notes

- Stop paths: `cmd_stop`/`cmd_restart` and the shell `down`/`restart` forwards ([cli_shell.py:332-339](../../src/wastech_orchestrator/cli_shell.py#L332)) are where Option A's note attaches, gated on the existing `has_active_task` probe ([cli.py:1078](../../src/wastech_orchestrator/cli.py#L1078)).
- Read-only views: `cmd_status`, `worc top`, and `cmd_list` ([cli.py](../../src/wastech_orchestrator/cli.py)) already know the PID-file state — Option B is a formatting change keyed on `running` + no-live-daemon, no new writes.
- Slot refusal: `SlotBusyError` is raised in `run_task` via `acquire_slot` ([core/orchestrator.py:540-541,564-566](../../src/wastech_orchestrator/core/orchestrator.py#L540)); Option C enriches the message from the active `TaskRow` (id + `current_node`).
- Docs: the recovery recipe is now documented in [how-to.md](../../docs/how-to.md) §3 ("A stopped task still shows `running`") — keep it in sync if the messages/labels change.
- Tests: `tests/test_cli_stop.py` (the note on a busy-after-stop state), `tests/test_cli_top.py` / a `status` test (the `parked` label), and a `SlotBusyError`-message assertion.
