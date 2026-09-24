---
name: worc-night-run
description: Drive a queue of wastech-orchestrator tasks to completion autonomously, with nobody in the loop — run them one at a time, diagnose and clear blockers as they appear, retry through a bounded escalation ladder, abandon a task after 4 starts and move to the next, and leave a short per-task write-up under `.worc/night-runs/`. Use for unattended runs — overnight, over a weekend, while away — when the operator will not be there to answer a question. Author the tasks first with worc-task or worc-deco-task; this skill only runs what is already queued.
---

# worc-night-run

Take the tasks the operator queued, carry them through the orchestrator one at a time, and deal with whatever goes wrong yourself. The operator is asleep: there is nobody to ask, so every decision belongs to you — inside the boundaries below, which are not negotiable and do not relax because it is 3am and a task is stuck. Speak in the user's language (default to the language they wrote in).

**Setup, once per repository.** Claude Code discovers skills under `<repo>/.claude/skills/` only, while `worc install` delivers this one to `.worc/guide/skills/worc-night-run/`. Copy the folder across once — `mkdir -p .claude/skills && cp -R .worc/guide/skills/worc-night-run .claude/skills/` — and copy it again after a `worc upgrade-docs` brings a newer version.

## When to use

- The operator has tasks in the pending queue and is leaving: "run these overnight", "прогони задачи, пока я сплю", "take the queue and don't wake me".
- **Not for authoring.** Use **worc-task** for one task, **worc-deco-task** for one task split into ordered subtasks, and **worc-flow** / **worc-flow-role** / **worc-flow-tune** to change the pipeline. This skill runs what is already queued and never invents a task.
- **Not for a run you are watching.** The whole value here is the unattended loop and the write-up it leaves behind; a run with the operator in front of it needs neither.

## Arguments

`$ARGUMENTS` is the operator's list: task ids, task file paths, or nothing at all (= the whole pending queue, in the orchestrator's own order). It may also carry a budget — "until 07:00", "at most 5 tasks", "stop after six hours". Honor it and stop cleanly when it is spent.

Read `paths.tasks_dir` from `.worc/config.yaml` rather than assuming `tasks/`; it is only that until someone renames it. A task the operator names that is still a draft under `<tasks_dir>/preparing/` has to be promoted first (`worc promote <id>`) — the queue holds promoted files only.

Everything else you need about a field, a flow, or a config key is in the installed guide under `.worc/guide/`. Read it rather than guessing; this file is the loop, not the reference.

## Before the operator goes to sleep

Run the whole gate first and print a one-screen **night plan**. It is the last moment a human can fix something cheaply, so fail loudly here rather than discovering it at 2am. Do not ask for confirmation afterwards — autonomy is the point — but do stop outright if the gate says the night cannot work.

1. `worc preflight` must exit `0`, and `worc validate-flow --all` must exit `0`. A red preflight means every task will fail the same way; there is no night to run.
2. The processing slot must be free — there is one engine per clone. `worc list` shows an executing task as `running` and a stalled one as `parked (no daemon)`, and every mutating verb refuses with the owner named (`the watch daemon is running (pid N)` / `a 'run' is executing a task in this clone`). If anything owns the clone, stop and tell the operator to `worc stop` first. Never start a second engine.
3. `git status --porcelain` — nothing of the operator's may be **staged**. A path already in the index makes the very first task die on `refusing to start: … already staged in the index`, and unstaged clutter blocks the terminal checkout back to base between tasks.
4. `worc list --pending --format json` gives the queue with `rank`, `priority` and `queue`. `rank` is the orchestrator's own run order — follow it; do not invent your own.
5. Cross-check against `worc list --all --format json`. A pending **file** whose id already holds a terminal row is not startable with `worc run` (the gate answers `duplicate_task_id`); it is a `manual_action_required` leftover and is resumed with `worc rerun`, not started. This is the one lifecycle trap that will otherwise make you loop on the same file all night.
6. Name the settings that make a night run fail closed, and say so in the plan: `repo.branch_mode: current` (depends on the live checkout — a poor fit unattended), `telegram.enabled: false` while a queued task's flow carries a `hitl` node (that node fails closed with no transport), `orchestrator.auto_mode.confirm_next_task: true` without Telegram, `security.trust_level: strict` (more approval gates, and with no transport an approval is a stop), `auto_merge: true`.
7. `depends_on` is **merge**-gated, and this run does not merge anything. A queued task that depends on a task finishing tonight will not become eligible tonight. Name those tasks in the plan and leave them out of the queue instead of retrying them until morning.

## The loop

One task at a time — the orchestrator allows exactly one active task per clone, and so do you.

Start a task with `worc run <tasks_dir>/pending/<file>` (a **path**, not an id). A task runs for hours while a foreground command tool times out in minutes, so **start it in the background and poll** — never block on it. Between polls, `worc status <id>` reports `node=`, `fix_iterations=`, `elapsed_since_update_seconds=` and `last_error=`; that is enough to see progress without reading a single log.

The exit code is the verdict, and it is the most reliable signal in the system:

| Exit | Meaning | What you do |
| --- | --- | --- |
| `0` | `done` | Write the note. Next task. |
| `3` | Parked — every provider was transiently unavailable; the task is resumable at its checkpoint | **Does not spend an attempt.** Wait, then resume the same task. The orchestrator's own ceiling is `agents.retry.max_blocked_s` (default 6h); do not out-wait it. |
| `1` | `failed` | Diagnose, then climb one rung of the ladder. |
| `2` | `manual_action_required` — **or** a config/usage error | Disambiguate on stdout: a real terminal prints `<id>: manual_action_required`, while the error paths print `error: …` or `manual action required: …`. A config error is not a task failure — fix the environment or stop the session. |

If a task stops moving — `elapsed_since_update_seconds` far past the provider's `timeout_seconds` with no node change — treat it as wedged. `worc run` installs no cooperative stop, so the only handle is the background process you started: kill it, then resume the task with `worc rerun <id> --continue`. Record that you did.

## The escalation ladder — four starts per task

A task gets **four starts**, and the powers you may use widen one rung at a time. Never skip a rung: the broad powers exist for the case where the narrow ones have already been tried and demonstrably did not work. Record every action you take at rung 3 or 4 verbatim in the task's note.

**Rung 1 — start.** `worc run <file>`.

**Rung 2 — restart only, change nothing.** Read `.worc/logs/<id>/failure_report.json` (machine-readable: the failing `node_id`, the exhausted limit, the counters, the provider attempts), and `stuck.md` beside it for the human summary. Then `worc rerun <id> --continue --yes --non-interactive`. Add `--reset-fix-budget` when the report names an exhausted fix loop; add `--from <node>` to re-enter at a specific node when the checkpoint's node is the one that is broken. Touch nothing in the repository.

**Rung 3 — repair the environment, never the work.** Fix what is neither the task's deliverable nor the operator's intent: install the toolchain or dependencies that `checks.command_sets` needs and the host is missing; unstage foreign paths that crept into the index; clear an `orchestrator.pid` / `orchestrator.run` marker left by a hard kill (only after confirming no such process is alive); wait out a rate-limit window the provider reported. Then restart as in rung 2.

**Rung 4 — last resort, and only here.** When rungs 2 and 3 have both been tried and failed, you may touch operator-owned inputs — narrowly, mechanically, and on the record:

- a task file the validation gate refused, for the **mechanical** defect only (front-matter shape, an `id` that fails the pattern, an empty `## Description`). Never rewrite what the task asks for: the Description and the Acceptance criteria are the operator's contract, not yours;
- one node's role prompt under `.worc/flows/<task_type>/<node>.md`, to narrow or clarify a step that keeps failing the same way;
- a **non-security** key in `.worc/config.yaml` (a timeout, a budget). Never a `security.*` key.

An operator `rerun --continue` re-freezes the control plane and **adopts** an edit you made to a flow or a prompt. That is what lets rung 4 work at all, and it means the task did not run on the control plane the operator froze — say so explicitly in the note and in the morning report.

After the fourth start fails, stop. Leave the task in whatever terminal it reached — do **not** `finalize` it, and do not delete its branch or its artifacts; the evidence is what the operator reads in the morning. Write the note and take the next task.

## Stop early — the cases where retrying is wrong

**Abandon the task immediately, with no attempt to work around it,** when the failure is a security refusal. These are the system holding a line, and routing around one is worse than the task not shipping:

- error class `containment_unverified` or `capability_unavailable`;
- a new `.worc/runs/exchange-quarantine/<id>/` directory — an agent wrote to a surface it was told not to. Read `evidence.json`, quote it in the note, **never delete it**;
- `refusing to push: the push destination of 'origin' changed during this task`;
- an untrusted program-launching driver in the repository's local git config;
- staged paths outside the operation's allowlist, or a lifecycle file rewritten under the running task;
- a gate rejection of `injection_suspected`.

**Stop the whole session** when the host itself is the problem and every remaining task would fail identically: `authentication_failed` on every allowed provider, a preflight that has started failing, an unreachable remote, no disk space. Write the report and stop — a night of identical failures is worse than a short night.

## Never

- Never commit, push, or open a pull request for a task's work, and never "finish the job by hand". Publication belongs to the orchestrator alone; that invariant does not bend because a run stopped one step short.
- Never merge a pull request. The night produces open PRs; merging is the operator's morning decision. (`worc merge-task` also currently aborts on exactly the conflict it exists to resolve — so it is not a fallback either.)
- Never weaken the security envelope: no `security.*` edit, no `--dangerously*`, `--yolo`, `--ignore-rules`, `--permission-mode bypassPermissions`, or `--sandbox danger-full-access`, in any spelling, anywhere.
- Never delete `.worc/runs/exchange-quarantine/**`, nor the logs and run artifacts of a task that ended `failed` or `manual_action_required`. Nothing reclaims them automatically for exactly this reason.
- Never run two tasks at once, and never start one while another engine owns the clone.
- Never edit the task's own deliverable to make the checks pass. That is you doing the task instead of the orchestrator, and it produces a green run that shipped your work unreviewed.

## What you leave behind

Create one directory per session: `.worc/night-runs/<YYYYMMDD-HHMMSS>/`. It is inside the gitignored runtime home, so nothing here reaches a commit or a review diff, and no cleanup command touches it — it grows until the operator prunes it.

- **`<task-id>.md`** — the short write-up for one task: final status, the PR if there is one, how many starts it took, what blocked it, what you did about it, and anything the operator has to pick up. A clean task still gets a file, with one line saying there were no blockers. **Write it the moment the task reaches a terminal, not at the end of the night** — a session that dies at 4am must still leave a readable trail.
- **`report.md`** — the morning screen: the night plan as it was decided, a one-row-per-task table, and a short list of what needs a human.
- **`state.json`** — your own durable state: the queue, and per task the number of starts spent and the rung reached. Update it after every transition, and read it first if you are re-invoked — a resumed session continues the night instead of restarting it.

## Working discipline

The night is long and your context is not. Read `failure_report.json` and the tail of the failing node's output, never whole log trees. Write each note as you go and then let go of it. Keep the loop's working set small: the state file, the current task, and nothing else.

Report to the operator the way the night actually went — how many tasks shipped, which ones did not and why, which decisions you took at rung 3 or 4, and what is waiting for them. Do not describe a task as done because it produced a branch: `done` is what the orchestrator recorded, nothing else.
