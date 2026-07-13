# How-To

Practical, problem-first recipes for situations you actually run into while operating the orchestrator — one problem, one fix, growing over time as new cases come up. For the full command reference behind each recipe, see [Operations](operations.md); for everyday setup and workflows, see the [Cookbook](cookbook.md).

## 1. The orchestrator failed and the git tree is dirty — resume from where it stopped

**Problem:** A task run stopped with an error — `worc status`/`worc list` shows it as `failed` or `manual_action_required` — a crashed provider process, a rate limit, a missing tool on `PATH`, or a real quality issue you fixed by hand. The clone's working tree now has uncommitted changes left over from the interrupted attempt, and you don't want to lose that work or start the task over from scratch. You want to pick up exactly where it stopped.

**Solution:**

```bash
worc rerun <task-id> --continue
```

This resumes the task **in place**: the existing branch and all prior work are reused, and the pipeline re-enters at the stage that failed — the same recovery path crash recovery itself uses. If you'd rather see the plan first without touching anything:

```bash
worc rerun <task-id> --continue --dry-run
```

**Details / caveats:**

- `--continue` needs an **idle slot** (no other active task) and the `watch` daemon **stopped** — it drives the pipeline directly in the shared clone.
- Whether the dirty tree is tolerated depends on _where_ the task stopped. Once it has produced code (reached `review`, `testing`, `fixing`, or `publish`), the uncommitted changes are treated as the legitimate input to those stages and are committed into the task automatically (you'll see a `note:` about it). If it stopped earlier — at `planning`/`refinement` — a dirty tree is still refused as unaccounted work; resolve or stash it first.
- To re-enter at a **specific** node instead of the recorded one (for example, re-running `implementation` after fixing something by hand), add `--from <node>`: `refinement`, `planning`, `implementation`, `testing`, `review`, `fixing`, `publish`. This works even if the flow file itself changed since the checkpoint — the resume proceeds using whatever flow is currently on disk.

  ```bash
  worc rerun task-001 --continue --from implementation
  ```

- If the task had exhausted its fix-loop budget (`manual_action_required`, stuck at `max_fix_cycles`), resuming into that same loop asks interactively whether to reset it, e.g. `review_fix budget is exhausted (15/15). Reset it to allow further review→fixing rounds? [y/N]`. Answer `y`, or pass `--reset-fix-budget` to skip the prompt in a script, or `--no-reset-fix-budget` to decline and refuse the resume instead. This prompt is never skipped by `-y/--yes` — resetting a fix budget is a consequential decision on purpose.
- Each `--continue` attempt appends a new ledger record linked to the failed one, so the retry history stays visible.

See [Operations → Re-attempting a terminal task](operations.md#re-attempting-a-terminal-task-rerun) for the full set of `rerun` options, including the fresh, from-scratch mode.

## 2. A task failed with an error — how to re-run it

**Problem:** `worc status`/`worc list` shows a task as `failed` (or `manual_action_required`), and you want to re-run it. The cause can be anything — a crashed provider, a rate limit, a missing tool, a real quality issue, or an **environmental/transient** problem (for example, you edited the flows while the daemon was serving the queue, so the flow file was momentarily absent when the daemon picked the task up: `reason=unknown task_type 'implementation': no flow file implementation.yaml … Run `worc install` …`). Once the underlying cause is fixed, you re-run — no need to recreate the task file.

**First, decide which mode you need — fresh vs. `--continue`:**

- **Fresh** (default, `worc rerun <task-id>`): re-attempts the task **from base** as if new — resets the branch to base and runs the whole flow again. Use this when the run **never produced usable work** — it died at pickup/flow-load or in an early stage (`refinement`/`planning`), or you simply want a clean attempt. A transient failure like the flow-drift case above is a fresh rerun: nothing was built, and the flow is back on disk now.
- **`--continue`**: resumes **in place** — reuses the existing branch and re-enters at the stage that failed. Use this when the run **already produced code** you don't want to lose. This is section 1 above; see it for the dirty-tree and fix-budget details.

Not sure which? Preview without touching anything: `worc rerun <task-id> --dry-run` (add `--continue` to preview that mode).

**Solution (normal, non-shell mode):**

```bash
worc stop                          # rerun refuses while the watch daemon owns the clone; stop it first (a no-op if none runs)
worc rerun <task-id> --dry-run     # optional: preview the planned reconciliation, writes nothing
worc rerun <task-id>               # fresh re-attempt; prompts y/N interactively (add -y to skip)
```

**Solution (inside `worc shell`):**

```
down                               # stop the daemon — rerun is slot-guarded and refuses while it is up
rerun <task-id> --yes              # --yes is REQUIRED here: shell always runs rerun non-interactively, so an unconfirmed rerun is refused
up                                 # optional: resume serving the queue
```

**Details / caveats:**

- **Stop the daemon first.** `rerun` drives the pipeline directly in the shared clone, so it refuses while a live `watch` daemon owns it (`rerun: the watch daemon is running (pid …); stop it first`). Use `worc stop` (non-shell) or `down` (shell).
- **In `worc shell`, always pass `--yes`.** The console runs `rerun` non-interactively (a confirmation prompt would fight the REPL's own stdin reader), so a bare `rerun <task-id>` is refused with _"refusing without confirmation (non-interactive)"_. The `finalize` and `merge-task` verbs are slot-guarded the same way.
- **You don't move the task file.** `rerun` finds it automatically wherever it currently lives (`tasks/pending/`, `tasks/done/`, or `tasks/failed/`); leave the failed file in `tasks/failed/`.
- **Run it from the target repo** (the one holding `.worc/`), not from the orchestrator repo. `worc` is the short alias for `wastech-orchestrator` — the commands are identical.
- Get the exact id with `worc list --format ids --scope rerun` (lists only the `failed` / `manual_action_required` ids a rerun accepts).
- **Avoid the cause:** don't edit `.worc/flows/` while the daemon is serving the queue — stop it (`down` / `worc stop`), change the flows, then start again, so a task can't be picked up mid-edit.

## 3. A task was stopped but still holds the slot (`parked (no daemon)`) — free the stuck slot

**Problem:** You stopped a run mid-task (a `down` in `worc shell`, a `worc stop`, a `--force-full`, or the process just died), and now the task still owns the one processing slot even though nothing is executing, so starting anything else fails with _"a task is active"_. `worc stop`/`down` prints a note about this and points at the fix; `worc status`/`top`/`list` show the task as **`parked (no daemon)`**; and a new `worc run` names the blocking task in its refusal. If the process simply died (no clean `stop`), you may not have seen the note — the `parked (no daemon)` label is the tell.

**Why this happens:** `stop`/`down`/`--force-full` stop the **daemon**, not the task. By the recovery model, the single unfinished task is deliberately left `running` at its checkpoint so the _next_ `up`/`watch` can resume it — so a running row with no live daemon means "parked, awaiting resume", not "executing now" (which is exactly what `parked (no daemon)` reports). That parked row still holds the one processing slot, which is why a new task is refused. `stop` has no authority over a task row, so it can never clear it — it's the wrong tool for that, even though it now tells you which tool is right.

**Solution — first confirm nothing is actually running, then pick one of three:**

```bash
# 1. Confirm there's no live daemon and no agent process (from the target repo):
worc status                                   # shows `parked (no daemon)` + the stuck task id + node
ls .worc/*.pid 2>/dev/null                     # no output = no daemon
ps aux | grep -iE "wastech|worc|codex|claude" | grep -v grep   # empty = no agent running
```

Then, depending on what you want the task to do:

```bash
# A. Close it (you're done with this task / it's superseded):
worc finalize <task-id> --as failed -y         # records terminal, checks out base, frees the slot

# B. Continue it from where it stopped (you still want it finished):
worc rerun <task-id> --continue                 # resumes in place at the checkpoint

# C. Just let it finish (restart the daemon — resume() picks it up first):
worc watch                                      # or `up` in the shell
```

**Details / caveats:**

- **Verify "nothing is running" before finalizing.** If a `worc run`/daemon really is still alive in another terminal, don't finalize under it — stop that first (`worc stop`). The `ls .worc/*.pid` + `ps` checks above are the tell: no PID file **and** no `worc`/`codex`/`claude` process means the `running` is stale.
- **`finalize` needs an idle slot and no live daemon** — it runs terminal cleanup (`git checkout base`) in the shared clone. With the daemon already stopped this is satisfied. `--as failed` keeps the task rerun-eligible; `--as abandoned` records it as manually abandoned.
- **Preview first if unsure:** `worc finalize <task-id> --as failed --dry-run` prints exactly what it will do (status transition, base checkout, ledger record) and writes nothing.
- **`finalize` leaves an operator-owned branch alone** — it only checks out the base branch; it does not delete or reset `branch_mode: existing`/`current` branches shared across tasks.
- **The fix is `finalize`/`rerun`/restart — never `stop`.** `stop` only manages the daemon; reaching for it here is the natural mistake this recipe exists to correct.
