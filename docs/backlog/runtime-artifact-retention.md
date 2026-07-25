# Runtime artifact retention: `logs clean` gaps and unbounded per-task accumulation

Status: **proposed** Date: 2026-07-25 Owner: Vladimir Makarevich

Two related gaps, reported from operating the orchestrator against a real target repo (`wastech-mdlint`). The first is a concrete, well-scoped defect in `worc logs clean`. The second is an open design question — there is no retention mechanism for per-task state at all, and nobody has decided what the policy should be.

## Part 1 — `worc logs clean` cannot reach the files at the root of `.worc/logs/`

### Operator report

After `worc logs clean`, three files survive in `.worc/logs/` and have to be emptied by hand:

- `completed.jsonl`
- `daemon-startup.log`
- `daemon.log`

### Current behavior (verified)

The command only ever walks **direct subdirectories** of the logs root. [`_task_log_dirs`](../../src/wastech_orchestrator/cli.py#L2261) returns `[p for p in logs_root.iterdir() if p.is_dir()]`, and [`_cmd_logs_clean`](../../src/wastech_orchestrator/cli.py#L2284) deletes only those dirs plus — under `--all` only — the ledger ([cli.py:2327](../../src/wastech_orchestrator/cli.py#L2327)). Every other _file_ at the root of `.worc/logs/` is unreachable by every flag combination (`clean`, `--keep N`, `--all`, `--yes`).

The three files are not equivalent, and the fix differs for each:

| File | Growth | Reachable by `logs clean`? |
| --- | --- | --- |
| `completed.jsonl` (the ledger) | **Append-only, no rotation and no size cap.** The only genuinely unbounded file of the three. | Yes, but **only** with `--all` — and nothing says so at the point of use |
| `daemon.log` | Bounded by a `RotatingFileHandler` — 10 MB × 5 backups ([logging.py:69](../../src/wastech_orchestrator/observability/logging.py#L69), [cli.py:217](../../src/wastech_orchestrator/cli.py#L217)) — so up to ~60 MB across `daemon.log` + `daemon.log.1…5` | **No.** Neither the live file nor any rotated backup |
| `daemon-startup.log` | Truncated on every spawn (`open("wb")`, [process.py:708](../../src/wastech_orchestrator/providers/process.py#L708)) — never grows across restarts, so it is small | **No.** It simply lingers forever after the last daemon exits |

So the report is accurate but the three cases have different root causes:

1. **`daemon.log` (+ its five rotated backups) — a real gap.** Up to ~60 MB the operator cannot reclaim through the CLI. Path: [cli_shell.py:130](../../src/wastech_orchestrator/cli_shell.py#L130).
2. **`daemon-startup.log` — cosmetic but confusing.** Self-truncating, so it is not a disk problem; it is a stale file that `clean` visibly refuses to remove. Path: [cli_shell.py:133](../../src/wastech_orchestrator/cli_shell.py#L133).
3. **`completed.jsonl` — partly a UX problem, partly a missing cap.** `worc logs clean --all` does delete it, and preserving the audit trail by default is a deliberate choice. But bare `clean` reports `(ledger kept)` without saying which flag would take it, and the ledger has no rotation or size cap of its own — "keep the audit trail forever" is a policy that works only until the file is large.

Worth noting what already works correctly: `.worc/logs/<task-id>/` per-task dirs are handled well, including `--keep N`, and [docs/operations.md](../operations.md#cleaning-up-logs-logs-clean) documents that surface accurately. This item is strictly about what sits _beside_ those dirs.

### Proposed direction

Not a full design — the shape is straightforward, the choices are the point:

- Make `logs clean` account for every entry in the logs root, not just the directories. The daemon logs are runtime noise, so removing them by default is defensible; the ledger is an audit trail and should stay behind `--all`.
- Alternatively (or additionally) add an explicit surface for them, so "clean" means "the logs root is clean" without special cases the operator has to learn.
- Give the ledger a rotation or size cap so retaining it by default stays sustainable.
- Refuse or warn while the watch daemon is live — deleting `daemon.log` from under an open `RotatingFileHandler` behaves differently on Windows (the file is held open) than on POSIX, and this repo treats cross-platform behavior as a hard invariant.
- Report what was skipped and why. The current silent survival is what made this look like a bug rather than a policy.

## Part 2 — no retention mechanism for old tasks or their context (open question)

This half is deliberately not a proposal. **There is no cleanup mechanism at all**, no command prunes any of it, and the right policy has not been decided.

### `tasks/done/` grows without bound

Measured in `wastech-mdlint`: **101 files, 588 KB** — roughly 50 completed tasks, each leaving `<task-id>.md` plus `<task-id>.summary.md`. Nothing prunes this directory; every completed task stays until the operator deletes it manually. Size is not yet the issue — discoverability is: `tasks/done/` is the operator's own history and it becomes unnavigable long before it becomes large.

Open questions: is `tasks/done/` an archive the operator curates, or runtime state the orchestrator may prune? Does a retention policy belong in config (age / count) or in an explicit `tasks clean` verb? Does the ledger already make the per-task summary redundant after some point, or is the summary the primary artifact and the ledger the index?

### Frozen per-task context accumulates one directory per task, forever

The bigger half. Under `private_home` (today the same `.worc/` as the control home, [runtime_layout.py:37](../../src/wastech_orchestrator/runtime_layout.py#L37)) each task gets its own permanent root:

- `control-bundles/<task-id>/` — the frozen control snapshot ([runtime_layout.py:45](../../src/wastech_orchestrator/runtime_layout.py#L45))
- `instruction-bundles/<task-id>/` — the canonical task packet, selected skill packages, and repo instruction files ([runtime_layout.py:53](../../src/wastech_orchestrator/runtime_layout.py#L53))
- `exchange-seals/<task-id>/seal-<NNNNNN>/` — sealed terminal-exchange snapshots, **multiple per task** ([runtime_layout.py:60](../../src/wastech_orchestrator/runtime_layout.py#L60))
- `exchange-quarantine/<task-id>/<NNNNNN>/` — tainted exchange evidence ([runtime_layout.py:66](../../src/wastech_orchestrator/runtime_layout.py#L66))

Two verified facts make this a design question rather than a small chore:

1. **The `shutil.rmtree(bundle_dir)` calls in [orchestrator.py](../../src/wastech_orchestrator/core/orchestrator.py) are wipe-before-re-freeze at task start, not terminal cleanup.** A bundle is only ever removed when _that same task id_ runs again. A task that runs once leaves its bundle permanently.
2. **`exchange_seal.py` has no retention, pruning, or cap logic of any kind** — and it keeps a numbered `seal-<NNNNNN>` per terminal transition, so a task that is re-run repeatedly accumulates seals within its own directory.

And none of these roots live under `.worc/logs/`, so `worc logs clean` will never reach them no matter how Part 1 is fixed.

**Timing note — this is the reason to decide now.** These roots do not exist on `main`; they are introduced by the WRI cluster on the current `feat/agent-worc-read-isolation` branch (confirmed: `git cat-file -p main:src/.../runtime_layout.py` contains none of the four constants, and `wastech-mdlint`'s `.worc/` has no such directories yet). So there is no accumulated mess to migrate and no operator expectation to break — the retention policy can ship _with_ the feature instead of being retrofitted after the first operator fills a disk. That window closes when the branch lands.

Open questions worth resolving before it does: are the frozen bundles audit evidence with a mandated retention, or a rerun cache that may be evicted? Does `rerun --continue` need only the latest seal, and if so can older seals be dropped on a successful terminal? Should retention be uniform across the four roots or per-root (quarantine is evidence of a security event and probably should not auto-delete)? And whichever way it goes, deleting from these roots must respect the WRI deny boundary — they are provider deny targets, so a cleanup path must not become a way to reach them.

### Adjacent, observed while investigating

`.worc/` also accumulates manual/migration backups that nothing prunes: three `config.yaml.bak-<timestamp>` files and eight `state.db*.bak*` files (~2.7 MB) in `wastech-mdlint`. Same class of problem, different origin — worth folding into whatever retention story emerges, but not a reason to widen this item.

## Out of scope

- The `logging.artifacts` level (`minimal`/`standard`/`full`) — that governs which per-attempt files are written inside a task dir, and it already works as documented in [docs/configuration.md](../configuration.md). It is not a retention mechanism for whole task trees and is not being changed here.
- `.worc-io/` itself — verified empty in `wastech-mdlint`; the exchange is sealed and removed at terminal as designed. The accumulation is in the _seals_, covered above.
- `.worc/memory/` — the memory store has its own `worc memory compact` curation surface.

## Acceptance criteria (sketch, for whoever picks this up)

- After the documented clean command, `.worc/logs/` contains nothing the operator has to remove by hand — or the command states explicitly what it kept and which flag removes it.
- No file under `.worc/logs/` grows without bound, the ledger included.
- Cleaning behaves correctly on Windows, Linux, and macOS with the watch daemon both running and stopped, and is tested for all of them.
- A decision is recorded (here or in an ADR) on retention for `tasks/done/` and for each of the four per-task private-home roots — even if the decision is "keep forever, documented" — before the WRI branch lands.
- The docs updated in the same change: [docs/operations.md](../operations.md#cleaning-up-logs-logs-clean), [docs/configuration.md](../configuration.md) if a config key appears, and the shipped `packaged/guide/` copy.
