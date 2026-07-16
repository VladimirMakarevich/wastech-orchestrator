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

**First, decide which mode you need — fresh, restart-in-place, or `--continue`:**

- **Fresh** (default, `worc rerun <task-id>`): re-attempts the task **from base** as if new — resets the branch to base and runs the whole flow again. This is what a plain `rerun` does on a `new`-mode branch (the default), the only branch the orchestrator owns. Use it when the run **never produced usable work** — it died at pickup/flow-load or in an early stage (`refinement`/`planning`), or you simply want a clean attempt.
- **Restart in place** (also just `worc rerun <task-id>` — chosen automatically): when the task runs on an **operator-owned branch** (`branch_mode: existing`/`current`) and failed **before producing any work** (no flow checkpoint — e.g. the flow-drift case above), a plain `rerun` must not reset a branch it doesn't own, so it re-drives the whole flow **from the top on that branch as-is** — nothing is reset and any commits already on the branch are kept. You pass no flag; `rerun` detects the case and the confirmation reads `[restart] on branch '<branch>'`. (If the operator-owned run **did** produce work, a fresh `rerun` refuses and points you at `--continue` instead.)
- **`--continue`**: resumes **in place** — reuses the existing branch and re-enters at the stage that failed. Use this when the run **already produced code** you don't want to lose. This is section 1 above; see it for the dirty-tree and fix-budget details.

Not sure which? Preview without touching anything: `worc rerun <task-id> --dry-run` names the mode it would take (`fresh` / `restart` / `continue`) and writes nothing (add `--continue` to preview that mode).

**Solution (normal, non-shell mode):**

```bash
worc stop                          # rerun refuses while the watch daemon owns the clone; stop it first (a no-op if none runs)
worc rerun <task-id> --dry-run     # optional: preview the planned reconciliation, writes nothing
worc rerun <task-id>               # re-attempt (fresh, or restart-in-place on an operator branch); prompts y/N (add -y to skip)
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
- **Restart-in-place keeps the branch and the attempt history.** For a pre-checkpoint failure on an operator-owned branch, use `rerun` (not a manual re-queue via `worc run <file>`): it re-drives on the branch without resetting it and records the retry in the ledger as attempt 2 (`rerun_of` set), so the failure → retry chain stays auditable. A manual re-queue loses that linkage.
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
- **`finalize` needs an idle slot and no live daemon** — it runs terminal cleanup in the shared clone (`git checkout base` when the branch mode / `repo.checkout_base_on_cleanup` calls for it). With the daemon already stopped this is satisfied. `--as failed` keeps the task rerun-eligible; `--as abandoned` records it as manually abandoned.
- **Preview first if unsure:** `worc finalize <task-id> --as failed --dry-run` prints exactly what it will do (status transition, whether cleanup checks out base or stays on the branch, ledger record) and writes nothing.
- **`finalize` leaves an operator-owned branch alone** — it never deletes or resets a `branch_mode: existing`/`current` branch, and by default it does not even check out base for those modes (they stay on the branch). Only `new` mode returns to base by default; set `repo.checkout_base_on_cleanup` to override either way.
- **The fix is `finalize`/`rerun`/restart — never `stop`.** `stop` only manages the daemon; reaching for it here is the natural mistake this recipe exists to correct.

## 4. Track your operator flows (`.worc/flows/`) in git

**Problem:** `install` gitignores the whole `.worc/` runtime home as one unit — `state.db`, `logs/`, `workspace/`, `config.yaml`, and your editable `flows/` copies all disappear from `git status` together (see [Operations → Installation](operations.md#1-installation)). But `.worc/flows/` holds the actual behavior you hand-author: the flow YAML files and their prompts (see [Flow authoring](flow-authoring.md)). By default that content has no git history, produces no diff to review, and can't be shared with teammates or shipped through a PR — you have to pass files around some other way.

**Solution:** replace the blanket `.worc/` line in the repo's tracked `.gitignore` with a wildcard pattern that ignores everything under `.worc/` **except** `flows/`:

```gitignore
# Ignore the runtime home's contents (not the dir itself) so flows/ can be re-included:
# Git won't descend into a fully-excluded dir, so `.worc/` would make any !.worc/flows a no-op.
.worc/*
!.worc/flows/
```

Then track and commit the flows you want to keep:

```bash
git add .gitignore .worc/flows
git commit -m "chore: track .worc/flows in git"
```

**Details / caveats:**

- **Replace the line, don't add to it.** `install` (and `install --reconfigure`) append a single blanket `.worc/` line. Keep only one scheme — a blanket `.worc/` and a `.worc/*` + `!.worc/flows/` pair are mutually exclusive; the blanket form always wins if both are present (see below), so delete it once you switch.
- **Why the wildcard is needed, not just `!.worc/flows/`:** Git never descends into a directory that's already excluded, so a negation for a path _inside_ an excluded directory is a no-op — this is the classic gitignore gotcha the comment calls out. `.worc/*` ignores each direct child individually (so git still walks into `.worc/`), which lets `!.worc/flows/` re-include that one child and everything nested under it (the per-flow prompt folders, `roles/supervisor.md`, etc.) without an extra pattern per file.
- **Everything else under `.worc/` stays ignored.** `state.db`, `logs/`, `workspace/`, `config.yaml`, `checks/`, `tools/`, and the memory home never ride along just because `flows/` is now tracked.
- **The orchestrator won't fight you.** A parent-directory exclusion from _any_ source — tracked `.gitignore`, the untracked clone-local `.git/info/exclude`, or the global excludes file — blocks re-inclusion of its children, regardless of which file or line added it. Two mechanisms could otherwise reintroduce a blanket `.worc/` line and silently defeat your `!.worc/flows/` negation: the per-task-run safety net (`ensure_runtime_excludes()`) and `install --reconfigure`'s `.gitignore` writer. Both check whether a runtime-only path (`.worc/state.db`) is already ignored before appending anything, so once you've switched to the wildcard scheme they leave it alone.
- **Same trick works for other subdirectories** — e.g. add `!.worc/tools/` if you also want the packaged tool executables tracked.

See also: [flow-authoring.md](flow-authoring.md#where-flows-live) for where flows and prompts live, and [operations.md](operations.md#1-installation) for what `install` writes into `.worc/` by default.

## 5. Fix conflicting Codex installations on Windows

**Problem:** `codex --version` in a terminal reports one version, while `worc preflight` reports
another. A real task may then fail every shell command with an infrastructure error such as
`CreateProcessWithLogonW failed: 2`, `windows sandbox failed`, or `helper copy failed for
command-runner`, even though `codex sandbox ...` works when you run it by hand.

**Why this happens:** Codex can be installed by more than one independently updated surface: a
global npm package, a Node version manager, the Windows Codex app, or an IDE integration. On
Windows, a global npm install normally puts `codex.cmd` on `PATH`, while an app-managed standalone
package may expose a different `codex.exe`. A terminal can select the `.cmd` launcher through
`PATHEXT`; the orchestrator deliberately starts providers as an argv list without a shell, so the
Windows process launcher can instead select the later `.exe`. The CLI version, sandbox setup helper,
and `codex-command-runner.exe` may then come from different package roots.

Multiple installations are not inherently broken. The ambiguous bare command is the problem.

**Diagnose it from the same Windows account that runs `worc`:**

```powershell
where.exe codex
Get-Command codex -All | Format-Table CommandType, Name, Source
codex --version
worc preflight

# Optional: compare the Windows app and its app-managed standalone CLI.
Get-AppxPackage OpenAI.Codex | Select-Object Name, Version, Status
Get-Content "$env:USERPROFILE\.codex\packages\standalone\current\codex-package.json"
```

More than one `where.exe` result, or different versions from `codex --version` and `worc
preflight`, confirms ambiguous resolution. For a global npm install, locate its physical native
Windows executable instead of the top-level `codex.cmd` launcher:

```powershell
$npmRoot = npm.cmd root -g
$codexExe = Join-Path $npmRoot `
  '@openai\codex\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe'

Test-Path -LiteralPath $codexExe
& $codexExe --version
```

The exact package root may differ under nvm, pnpm, Yarn, or another CPU architecture. If the path
above is absent, find the manager-owned native executable and verify that its package also contains
the matching resources:

```text
<package-root>\bin\codex.exe
<package-root>\codex-resources\codex-windows-sandbox-setup.exe
<package-root>\codex-resources\codex-command-runner.exe
```

**Solution:** pin that physical executable in the target repository's `.worc/config.yaml`. Use
single-quoted YAML on Windows so backslashes remain literal:

```yaml
agents:
  providers:
    codex:
      command: 'C:\absolute\package-root\bin\codex.exe'
```

Then restart `worc watch` if it is running and verify both provider discovery and a real sandboxed
process:

```powershell
worc preflight

$pwsh = (Get-Command pwsh.exe -CommandType Application | Select-Object -First 1).Source
& $codexExe sandbox $pwsh -NoProfile -Command 'Write-Output OK'
```

The preflight version must now match `& $codexExe --version`, its `Windows sandbox helper` path
must belong to the same installation, and the smoke command must print `OK` with exit code `0`.

**Do not:**

- point `command` at `codex.cmd`; it requires a command shell, which the orchestrator intentionally
  does not use;
- copy sandbox helpers between releases or manually retarget the app-managed `current` junction;
- delete an old standalone release before pinning and verifying the replacement — the Windows app
  may own hardlinks or junctions into that release, and deleting it does not make `worc` select npm;
- weaken `workspace-write`, approvals, or `security.strict_isolation` to hide a packaging/path
  problem.

If you do not use the Windows Codex app, uninstall it through Windows Settings / Microsoft Store
only after the pinned npm executable passes the checks above. If you keep the app, it is safe to
leave its standalone CLI installed: the absolute `command` removes the ambiguity, and each updater
can continue managing its own files.

The same principle applies on Linux and macOS when Homebrew, npm, a version manager, and an IDE
expose different Codex installations: compare `type -a codex` / `which -a codex` with `worc
preflight`, then configure the absolute path to the intended executable. A POSIX launcher with a
valid shebang can run without a shell; the Windows-specific requirement is to avoid `.cmd` and use
the native `.exe`.

For the supported native Windows sandbox modes and upstream troubleshooting guidance, see the
[Codex Windows sandbox documentation](https://developers.openai.com/codex/windows).
