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
