# What the orchestrator leaves in your repository

Everything the orchestrator writes lives in two places: the gitignored `.worc/` home (its own state, never committed) and the `tasks/` lifecycle tree at the repository root (committed, because it is the audit trail). This page says what each directory is, when it appears, whether its presence is normal, and what may be deleted.

Two rules hold for the whole `.worc/` home:

- **The agent can never read it.** Every path below is in the orchestrator's internal read-deny set, projected into each provider's own sandbox/tool policy. The agent sees only the curated `.worc-io/` exchange.
- **It is gitignored as a whole**, so nothing here shows up in `git status` or reaches a commit.

## `.worc/` at a glance

| Path | What it is | Safe to delete? |
| --- | --- | --- |
| `config.yaml` | Your configuration. The one file you edit by hand. | No — it is the install. |
| `config.example.yaml` | Commented reference copy, never read at runtime. | Yes (`worc install --reconfigure` restores it). |
| `flows/`, `tools/` | Editable copies of the built-in flows, their role prompts, and the executables `tool` nodes resolve against. Yours to edit. | No — a flow a task names must exist here. |
| `guide/` | This documentation, copied in by `install`. | Yes (`worc upgrade-docs` restores it). |
| `logs/<task-id>/` | Per-task artifacts: the rendered prompts, per-attempt provider output, `current.diff`, `summary.md`, the local-only `summary.json` (the same summary plus follow-ups and what the supervisor layer spent), check logs, HITL records. The biggest thing here by far — megabytes per task. | Yes — `worc logs clean`. |
| `logs/daemon.log`, `logs/daemon-startup.log` | The `watch` daemon's operator trace (rotating, 10 MB × 5 backups) and the raw stream of a console-spawned daemon, kept so a startup crash is recoverable. | Yes — `worc logs clean` takes them, once no daemon is running. |
| `logs/completed.jsonl` | The **ledger**: one append-only JSON record per terminal task. The audit index of everything that has run. | Only with `worc logs clean --all`. See below. |
| `runs/` | Per-task private runtime state, keyed by task id. Four roots — the section below explains each. | Automatically, or with `worc runs clean`. |
| `memory/` | The persistent, repo-scoped memory store (when `memory.enabled`). | Curate it with `worc memory compact` / `worc memory clear`, not by hand. |
| `security-reports/` | Deliverables of a flow whose output policy keeps its report private (a security audit) rather than committing it. | Yours — read them first; nothing else reclaims them. |
| `workspace/`, `tasks/rejected/`, `state.db*` | Scratch space, quarantined task files that failed the validation gate, and the authoritative task database. | `state.db` is the source of truth — never delete it while tasks are in flight. |
| `orchestrator.pid`, `orchestrator.stop`, `orchestrator.children` | Process control for the `watch` daemon: its recorded PID, the stop sentinel `worc stop` writes, and the agent handles a hard stop needs to reap. | No — `worc stop` manages them. A stale `.pid` after a crash is cleared by the next `stop`. |
| `git-null-hooks/` | A deliberately empty directory every orchestrator-run `git` command uses as its hooks path, so no repository hook runs in an orchestrator git process. | No — it must exist and stay empty. |
| `.env` | Your secrets. Never committed, never logged, never passed to an agent. | No. |
| `config.yaml.bak-*`, `flows.bak-*`, `tools.bak-*` | Snapshots taken by `worc install --reconfigure` before it refreshes those files. The three newest of each are kept and older ones are pruned automatically. | Yes. Backups you name yourself (`config.yaml.bak-before-upgrade`) are never pruned — only the timestamped ones the orchestrator wrote. |

At the repository root, outside `.worc/`: `tasks/pending/`, `tasks/preparing/`, `tasks/done/`, `tasks/failed/`. These are deliberately **not** in `.worc/` — a finished task's file and its `<task-id>.summary.md` are committed there as the human-readable audit trail. Nothing prunes them; they are yours to curate.

## `runs/` — the per-task runtime roots

Each of these is a directory of `<task-id>/` subdirectories. They exist so a task's inputs cannot change under it mid-run and so a finished task can still be analyzed.

### `runs/control-bundles/<task-id>/`

**What writes it:** the orchestrator, at task start. It freezes the exact control inputs the flow references — the flow YAML, the role prompts, each tool's complete launch set — into an immutable snapshot and binds every step of the run to that copy, so editing `.worc/flows/` mid-run cannot change what the task is executing.

**Normal?** Yes, one per task that ran. It is re-frozen if the same task id runs again.

### `runs/instruction-bundles/<task-id>/`

**What writes it:** the orchestrator, at task start. Same idea for the *agent inputs*: the validated task file, the skill packages the task selected, and the repository instruction files (`AGENTS.md`, `AGENTS.override.md`, `CLAUDE.md`). The agent receives redacted copies through the exchange; this is the unredacted original the orchestrator verifies against.

**Normal?** Yes, one per task that ran.

### `runs/exchange-seals/<task-id>/seal-<NNNNNN>/`

**What writes it:** the orchestrator, at every terminal transition — including a successful one.

**A `seal-*` on a task that finished `done` is the expected outcome, not a sign of trouble.** When a task ends, the orchestrator archives a checksum-verified copy of the agent-facing exchange (`task.md`, `plan.md`, `current.diff`, per-stage findings, the supervisor's `supervisor/packet.json`) and then removes the live `.worc-io/<task-id>/` directory so the next task cannot see it. The seal is that archive — a record of *what the agent last saw*. `manifest.json` inside it names the outcome it was sealed at (`"final_status": "done"` for a clean run).

Because the in-repository exchange is removed at the end, the newest seal is the only surviving copy of a finished task's agent-facing plan and diff. That is what makes it worth keeping when you are analyzing runs, and it is what automatic cleanup removes when you are not.

**Normal?** Yes. One per terminal transition, so a task re-run several times accumulates numbered seals under its own id.

### `runs/exchange-quarantine/<task-id>/<NNNNNN>/`

**This is the one whose existence means something happened.** The exchange is a read-only surface for the agent; if the orchestrator's tamper check finds that an agent changed it, the tree is moved here as evidence together with the expected and observed file manifests (`evidence.json`). It is never sealed and never reused to resume a task.

**Normal?** **No.** On healthy runs this directory does not exist at all. If it does, read `evidence.json` — an agent wrote to a surface it was told not to.

**Never deleted automatically**, in either retention mode. It is security evidence, and a cleanup command must not be the thing that removes it. Delete it by hand once you have read it, or pass `worc runs clean --include-quarantine`.

## Retention: what is removed, and when

Per-task state accumulates one directory per root per task, so there are two modes — one for running work, one for analyzing it.

**Automatic (the default, `logging.clean_runs_on_success: true`).** When a task finishes **successfully**, the orchestrator removes that task's own subtree from `control-bundles/`, `instruction-bundles/`, and `exchange-seals/`. Nothing else is touched. You never have to know these directories exist.

What survives a successful task, and is the audit trail: its record in `logs/completed.jsonl`, its `tasks/done/<task-id>.md` and `<task-id>.summary.md` pair, its row in `state.db`, and its artifacts under `logs/<task-id>/`.

**A task that ended `failed`, `manual_action_required`, or is parked is never touched**, in either mode. Automatic cleanup on failure would delete the evidence at the exact moment you need it.

**Manual (`logging.clean_runs_on_success: false`).** Nothing is removed automatically — every run keeps its frozen inputs and its seals, so a completed task can still be analyzed. Reclaim on demand:

```
worc runs clean                        # every task's runs/ subtree
worc runs clean --keep 5               # keep the 5 most recent tasks
worc runs clean --include-quarantine   # also take the tainted-exchange evidence
```

`worc runs clean` is available in both modes; the config switch only decides whether the orchestrator also does it itself. Like `worc logs clean`, it refuses while a task is active — run it when the orchestrator is idle.

The two commands keep disjoint territory: **`worc runs clean` handles `runs/`, `worc logs clean` handles `logs/`.** Per-task log dirs hold almost all of the disk (megabytes per task, against roughly 150 KB per task in `runs/`), and they are never removed automatically — reclaiming them stays an explicit decision.

### The ledger is never capped

`logs/completed.jsonl` grows by roughly 630 bytes per terminal task and is never rotated, compacted, or trimmed — about 6 MB after 10 000 tasks. That is deliberate: it is an append-only audit trail, and the only way to reclaim it is `worc logs clean --all`, which deletes it outright. If you want to keep the history, archive the file before running that.

## After upgrading: the old per-task directories

Earlier versions kept these four roots as direct children of `.worc/` — `.worc/control-bundles/`, `.worc/instruction-bundles/`, `.worc/exchange-seals/`, `.worc/exchange-quarantine/`. They now live under `.worc/runs/`. Nothing is migrated: the content is private runtime state for tasks that already finished, so it is not worth converting.

If your `.worc/` shows both shapes, the ones directly under `.worc/` are leftovers from before the upgrade and no command reaches them. **Delete those four directories by hand** — `worc runs clean` only ever touches `.worc/runs/`.
