# Runtime artifact retention: `logs clean` gaps and unbounded per-task accumulation

Status: **accepted 2026-07-27** (every fork closed in [Acceptance decisions](#acceptance-decisions-2026-07-27)) Date: 2026-07-25 Updated: 2026-07-27 (re-verified against `dev`; the four per-task roots have since merged to `main`) Owner: Vladimir Makarevich

Two related gaps, reported from operating the orchestrator against a real target repo (`wastech-mdlint`). The first is a concrete, well-scoped defect cluster in `worc logs clean`. The second was an open design question — there was no retention mechanism for per-task state at all — now settled as a two-mode policy: automatic cleanup of a successful task's run artifacts by default, with an operator switch that turns it off for run analysis.

**Proportion check, so the fix is prioritized honestly.** In `wastech-mdlint` a single per-task log dir is **5.7 MB** (`logs/p9-11-07-custom-missing-id`) and `logs/` totals **55 MB** — and those dirs are exactly what `logs clean` already handles well. The unreachable files at the logs root are ~0.4 MB today. So Part 1 is mostly a **correctness-and-legibility** defect (the command does not do what its name promises, and two files have no cap by design) rather than a disk-reclamation emergency; Part 2 is where the unbounded growth with no mechanism at all lives.

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
| `completed.jsonl` (the ledger) | **Append-only, no rotation and no size cap** ([ledger.py:96](../../src/wastech_orchestrator/ledger.py#L96)) — one JSON record per terminal task. Small in absolute terms (5 KB after 8 tasks) but monotone forever. | Yes, but **only** with `--all` — and nothing says so at the point of use |
| `daemon.log` | Bounded by a `RotatingFileHandler` — 10 MB × 5 backups ([logging.py:69](../../src/wastech_orchestrator/observability/logging.py#L69), [cli.py:217](../../src/wastech_orchestrator/cli.py#L217)) — so up to ~60 MB across `daemon.log` + `daemon.log.1…5` by design (216 KB and no backups yet in `wastech-mdlint`) | **No.** Neither the live file nor any rotated backup |
| `daemon-startup.log` | **No cap at all within a daemon session** — see below. 216 KB in `wastech-mdlint` after one session, i.e. a near-byte-for-byte duplicate of `daemon.log` (216 KB) | **No.** It also simply lingers forever after the last daemon exits |

**The `daemon-startup.log` finding is stronger than "a stale small file", and this is a correction to the first read of it.** [`spawn_detached`](../../src/wastech_orchestrator/providers/process.py#L708) redirects the child's stdout **and** stderr (`stderr=subprocess.STDOUT`) into `capture_path` for the daemon's **entire lifetime**, not just its startup window. The daemon's operator logger keeps a `StreamHandler` on stderr in addition to the rotating `--log-file` ([logging.py:60-78](../../src/wastech_orchestrator/observability/logging.py#L60)), so the startup log accumulates the very stream the rotating handler exists to cap. Measured in `wastech-mdlint`: `daemon-startup.log` = 216 089 B vs `daemon.log` = 216 000 B. It **is** truncated per spawn (`open("wb")`), so it never grows _across_ restarts — but a long-lived `watch` session has nothing bounding it, and it duplicates capped content. Its docstring's own claim ("the startup log holds only the daemon's own output") is true and beside the point: that output is unbounded.

So the report is accurate, and there are **four** distinct root causes, not three:

1. **`daemon.log` (+ its five rotated backups) — a real gap.** Up to ~60 MB the operator cannot reclaim through the CLI. Path: [cli_shell.py:130](../../src/wastech_orchestrator/cli_shell.py#L130).
2. **`daemon-startup.log` — an uncapped duplicate, not a cosmetic leftover.** Unreachable by `clean`, unbounded within a session, and byte-for-byte redundant with a file that _is_ capped. Path: [cli_shell.py:136](../../src/wastech_orchestrator/cli_shell.py#L136).
3. **`completed.jsonl` — partly a UX problem, partly a missing cap.** `worc logs clean --all` does delete it, and preserving the audit trail by default is a deliberate choice. But bare `clean` reports `(ledger kept)` without saying which flag would take it, and the ledger has no rotation or size cap of its own — "keep the audit trail forever" is a policy that works only until the file is large.
4. **`--all` is silently ignored when combined with `--keep N`.** The two flags are not mutually exclusive in argparse ([cli.py:637-647](../../src/wastech_orchestrator/cli.py#L637)), and the `--keep` branch returns before the ledger is ever considered ([cli.py:2299-2320](../../src/wastech_orchestrator/cli.py#L2299)). So `worc logs clean --keep 0 --all` prunes every task dir and keeps the ledger without a word — the operator asked for the ledger twice over and got neither a deletion nor a diagnostic.

**A fifth, adjacent defect: no active-run guard.** `_cmd_logs_clean`'s own docstring states "Running this while a task is active is unsupported" — and nothing enforces it. The mechanism already exists and is already used one screen away: `memory compact` refuses via `has_active_task(config)` ([cli.py:2424](../../src/wastech_orchestrator/cli.py#L2424)). `logs clean` calls it nowhere, so an operator can `rmtree` the artifact dir of a running task, and any fix that starts deleting the _live_ `daemon.log` makes this materially worse — on Windows the `RotatingFileHandler` holds the file open, so the delete fails (or, worse, the rotation target vanishes) where POSIX would unlink it happily. Cross-platform behavior is a hard invariant here, so the guard is part of the fix rather than a follow-up.

Worth noting what already works correctly: `.worc/logs/<task-id>/` per-task dirs are handled well, including `--keep N`, and [docs/operations.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/operations.md#cleaning-up-logs-logs-clean) documents that surface accurately. This item is strictly about what sits _beside_ those dirs.

### Proposed direction

The shape is straightforward and the choices were the point; each bullet below is now settled in [Acceptance decisions](#acceptance-decisions-2026-07-27) (RA-D1…RA-D5):

- Make `logs clean` account for every entry in the logs root, not just the directories. The daemon logs are runtime noise, so removing them by default is defensible; the ledger is an audit trail and should stay behind `--all`.
- Alternatively (or additionally) add an explicit surface for them, so "clean" means "the logs root is clean" without special cases the operator has to learn.
- Give the ledger a rotation or size cap so retaining it by default stays sustainable.
- Stop `daemon-startup.log` from being an uncapped duplicate — either cap it the way `daemon.log` is capped, or narrow it to the startup window it is named for (its whole justification is recovering a crash that happens _before_ `--log-file` is configured).
- Refuse while a task is active or the watch daemon is live, reusing `has_active_task` — deleting `daemon.log` from under an open `RotatingFileHandler` behaves differently on Windows (the file is held open) than on POSIX, and this repo treats cross-platform behavior as a hard invariant.
- Reject `--keep N --all` as a contradictory combination, or honor both — either is fine, silence is not.
- Report what was skipped and why. The current silent survival is what made this look like a bug rather than a policy.

## Part 2 — no retention mechanism for old tasks or their context

**There is no cleanup mechanism at all** and no command prunes any of it. The evidence below is what the policy in [Acceptance decisions](#acceptance-decisions-2026-07-27) was decided against; the open questions it lists are answered there, not here.

### `tasks/done/` grows without bound

Measured in `wastech-mdlint` on 2026-07-27: **117 files, 708 KB** — up from 101 files / 588 KB two days earlier, i.e. ~58 completed tasks, each leaving `<task-id>.md` plus `<task-id>.summary.md`. Nothing prunes this directory; every completed task stays until the operator deletes it manually. Size is not yet the issue — discoverability is: `tasks/done/` is the operator's own history and it becomes unnavigable long before it becomes large.

**One nuance that has to be settled before any policy is written: whether these files are tracked differs per target.** By design `tasks/done/` and `tasks/failed/` sit at the **repo root**, outside the gitignored `.worc/`, precisely so the audit commit captures the task file and its `<id>.summary.md` ([cli.py:117-133](../../src/wastech_orchestrator/cli.py#L117)). Where that design holds, "pruning" means a commit that deletes tracked files, git history keeps every byte anyway, and the only thing reclaimed is working-tree navigability. In `wastech-mdlint` the operator has gitignored the whole tree (`.gitignore:20: tasks/`, so `git ls-files tasks/done` is empty) and pruning there is free and irreversible. A retention verb must behave sanely in both worlds — and a config default that quietly deletes tracked, audit-committed files is not acceptable in the first one.

Questions the follow-up item owes an answer (this one does not — see RA-D8): is `tasks/done/` an archive the operator curates, or runtime state the orchestrator may prune? Does a retention policy belong in config (age / count) or in an explicit `tasks clean` verb? Does the ledger already make the per-task summary redundant after some point, or is the summary the primary artifact and the ledger the index?

### Frozen per-task context accumulates one directory per task, forever

The bigger half. Under `private_home` (today the same `.worc/` as the control home, [runtime_layout.py:37-38](../../src/wastech_orchestrator/runtime_layout.py#L37)) each task gets its own permanent root:

- `control-bundles/<task-id>/` — the frozen control snapshot ([runtime_layout.py:46](../../src/wastech_orchestrator/runtime_layout.py#L46))
- `instruction-bundles/<task-id>/` — the canonical task packet, selected skill packages, and repo instruction files ([runtime_layout.py:54](../../src/wastech_orchestrator/runtime_layout.py#L54))
- `exchange-seals/<task-id>/seal-<NNNNNN>/` — sealed terminal-exchange snapshots, **one per terminal transition** ([runtime_layout.py:61](../../src/wastech_orchestrator/runtime_layout.py#L61))
- `exchange-quarantine/<task-id>/<NNNNNN>/` — tainted exchange evidence ([runtime_layout.py:67](../../src/wastech_orchestrator/runtime_layout.py#L67))

Measured in `wastech-mdlint` after eight consecutive successful tasks: `control-bundles/` 448 KB, `instruction-bundles/` 168 KB, `exchange-seals/` 572 KB — ~1.2 MB total, ~150 KB per task, with exactly one `seal-000001/` per task (so multi-seal accumulation inside one task id needs a rerun to appear). `exchange-quarantine/` does not exist there at all.

Two verified facts make this a design question rather than a small chore:

1. **The `shutil.rmtree(bundle_dir)` calls in [orchestrator.py](../../src/wastech_orchestrator/core/orchestrator.py#L2100) are wipe-before-re-freeze at task start, not terminal cleanup.** A bundle is only ever removed when _that same task id_ runs again. A task that runs once leaves its bundle permanently.
2. **`exchange_seal.py` has no retention, pruning, or cap logic of any kind** — and it keeps a numbered `seal-<NNNNNN>` per terminal transition, so a task that is re-run repeatedly accumulates seals within its own directory.

And none of these roots live under `.worc/logs/`, so `worc logs clean` will never reach them no matter how Part 1 is fixed.

**Timing note — the ship-with-the-feature window has closed, and that changes the framing rather than the priority.** These roots were introduced by the read-isolation cluster, which **merged to `main` on 2026-07-25** (`61ef90f`, #39) — verified: `git cat-file -p main:src/wastech_orchestrator/runtime_layout.py` now contains all four constants, and `dev`'s copy is byte-identical to `main`'s. They also exist on disk in the live `wastech-mdlint` target. So retention for them is a **retrofit**, not a ship-with-the-feature decision. What survives from the original argument is the part that still matters: the accumulated volume is ~1.2 MB across eight tasks, so there is no mess to migrate and no established operator expectation — a policy adopted now costs nothing to adopt, and every additional operator run raises that price. The deadline was fictional; the cheapness is not.

**Adjacent item.** [private-home-footprint.md](private-home-footprint.md) covers _where_ these four roots live and whether the operator can interpret them. Both items were decided together on 2026-07-27, and its PH-D1 (all four roots move under one `.worc/runs/` parent) is what leaves this item **one** retention root to reason about instead of four — the decision RA-D6 below is written against that shape. The coupling runs both ways: because the roots have shipped, that rename orphans real directories in existing targets and nothing in the CLI can reach them, so the grouping change needs this item's cleanup path to exist. Implementing the grouping first is cheaper — then the verb is written against the final layout rather than ported to it.

The questions this raised are answered in RA-D6: the bundles are treated as a rerun/analysis cache that a **successful** terminal may evict (a failed one never may); `rerun` cannot target a `done` task at all, so no seal loses its restore consumer; retention is uniform across the roots with `exchange-quarantine/` as the one standing exception. The constraint that survives untouched by any of it: deleting from these roots must respect the read-isolation deny boundary — they are provider deny targets, so a cleanup path must not become a way to reach them.

### A fifth unpruned runtime root: `.worc/tasks/rejected/`

Created by `install` as part of `WORC_RUNTIME_DIRS` ([cli.py:133](../../src/wastech_orchestrator/cli.py#L133)) and written whenever a task file is quarantined — an operator `cancel` ([cli_shell.py:433](../../src/wastech_orchestrator/cli_shell.py#L433)) or a validation/decomposition rejection ([orchestrator.py:4153](../../src/wastech_orchestrator/core/orchestrator.py#L4153)). It deliberately lives under `.worc/` so rejected tasks are never swept into the audit commit, which also means nothing at the repo root reflects them and no verb prunes them. Empty in `wastech-mdlint` today, so this is inclusion-for-completeness rather than an observed problem — but whatever verb prunes `tasks/done/` should not pretend this directory does not exist.

### Adjacent, observed while investigating — and half of it is ours

`.worc/` also accumulates timestamped backups that nothing prunes. Re-measured in `wastech-mdlint` on 2026-07-27:

| Files | Count / size | Written by |
| --- | --- | --- |
| `config.yaml.bak-<UTC>` | 3 | **The orchestrator**, on every `install --reconfigure` ([cli.py:3862-3864](../../src/wastech_orchestrator/cli.py#L3862)) |
| `flows.bak-<UTC>/`, `tools.bak-<UTC>/` | none yet here | **The orchestrator**, same path ([cli.py:780](../../src/wastech_orchestrator/cli.py#L780), [cli.py:837](../../src/wastech_orchestrator/cli.py#L837)) — a full copy of the directory per refresh, so the largest of the three by far |
| `state.db*.bak*` | 13 files, 2.8 MB | The operator, by hand, before campaign runs |

This is a correction to the earlier reading of these as "manual/migration backups": the config and flows/tools backups are **orchestrator-written and never reclaimed**, which puts them inside this item's remit rather than beside it. `flows.bak-*` matters most — it is a whole-directory copy created by the same command the [`upgrade-flows`](upgrade-flows.md) item wants operators to run more often, so the more that item succeeds, the faster this grows. Only `state.db*.bak*` is genuinely the operator's own doing and none of the orchestrator's business.

## Out of scope

- The `logging.artifacts` level (`minimal`/`standard`/`full`) — that governs which per-attempt files are written inside a task dir, and it already works as documented in [docs/configuration.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/configuration.md). It is not a retention mechanism for whole task trees and is not being changed here.
- `.worc-io/` itself — verified present and empty in `wastech-mdlint`; the exchange is sealed and removed at terminal as designed. The accumulation is in the _seals_, covered above.
- `.worc/memory/` (33 MB in `wastech-mdlint`) — the memory store has its own `worc memory compact` curation surface.
- HITL artifacts (`hitl/*.json`) — they live inside the per-task artifact dir ([hitl.py:252](../../src/wastech_orchestrator/core/hitl.py#L252)), so `logs clean` already reaches them; they are not a separate root.
- Anything about _where_ the four per-task roots live or how they are named — that is [private-home-footprint.md](private-home-footprint.md).

## Acceptance decisions (2026-07-27)

**Implementation order** across both items — four changes, one hard dependency:

1. **Part 1 here (RA-D1…RA-D5).** Independent of everything else and first, because it is the defect the operator actually reported and it establishes the guard/reporting shape the retention verb then copies.
2. **The grouping (`private-home-footprint.md` PH-D1…PH-D4).** A mechanical path change plus the new guide page. Must precede step 3.
3. **RA-D6 — the retention switch and `worc runs clean`.** The one hard dependency: it is written against the single `.worc/runs/` parent, so if the grouping has not landed the verb is written against four roots and then ported to one.
4. **RA-D7 — keep-last-N for the orchestrator's own `.bak` artifacts.** Independent; last only because it touches the `install --reconfigure` path rather than the runtime one.

Steps 1 and 4 do not depend on 2 or 3 and may run in parallel with them. Note what the ordering does **not** buy: the orphaned old-layout directories in existing targets are removed by hand whichever order is chosen, because PH-D1 deliberately ships no migration code and `runs clean` addresses `runs/`, not the pre-rename roots beside it. That is a documentation obligation on step 2, not a sequencing problem.

### RA-D1 — bare `clean` sweeps the whole logs root; the ledger stays behind `--all`

`worc logs clean` accounts for **every** entry in `.worc/logs/`, not only the directories: per-task dirs, `daemon.log` and its rotated backups, and `daemon-startup.log`. The ledger remains the single exception and is removed only by `--all` — but the command must now name it: instead of the bare `(ledger kept)`, it says which flag takes it. The daemon logs are runtime noise and removing them by default is what the command's name already promises; the ledger is the audit trail and keeping it is a deliberate default, not an oversight to be discovered.

There is no back-compatibility cost to changing the default: the orchestrator is not deployed anywhere, so no operator has a habit built on the old behavior.

### RA-D2 — `--keep N --all` honors both flags

`--keep N` keeps the N most recently modified **task dirs** and removes the rest; `--all` additionally removes the ledger. Combined, they mean exactly that: keep N task dirs, remove the remaining task dirs, remove the daemon logs, remove the ledger. The one outcome that is forbidden is today's — accepting a flag and silently doing nothing with it.

### RA-D3 — refuse while a task is active or the daemon holds the log

`logs clean` calls `has_active_task(config)` and refuses, exactly the way `memory compact` already does ([cli.py:2424](../../src/wastech_orchestrator/cli.py#L2424)) — one mechanism, not a second one. This is what its own docstring has always claimed and never enforced. Separately, the live `daemon.log` is not deleted while the watch daemon is running: on Windows the `RotatingFileHandler` holds it open, so the unlink fails there while succeeding on POSIX, and a cleanup command that behaves differently per platform is not acceptable under this repo's cross-platform invariant. The message must distinguish the two refusals — an active task and a live daemon are different things for the operator to fix.

### RA-D4 — the ledger gets no cap; its growth is documented instead

`completed.jsonl` stays append-only with no rotation and no size cap, and the shipped guide states the growth rate in numbers (~630 B per terminal task, so ~6 MB at 10 000 tasks) plus the fact that `--all` is the only reclamation. Rationale: rotation of an audit trail either loses records or buys an atomic-rewrite path — with the daemon holding the same file open, and Windows replace semantics on top — and none of that is justified by 630 bytes per task. A cap is added when a measurement asks for one, not before.

### RA-D5 — `daemon-startup.log` stops growing at its source

Once the detached daemon has configured its `--log-file`, it drops its stderr `StreamHandler`. The startup log then contains exactly what it is named for — the pre-configuration crashes (argparse errors, import failures, preflight aborts) that would otherwise vanish into `DEVNULL` — and stops accumulating a byte-for-byte duplicate of the capped `daemon.log`.

Nothing is lost by this: the console tails the resolved daemon log, not the startup log ([cli_shell.py:620](../../src/wastech_orchestrator/cli_shell.py#L620)), and reads the startup log only when the liveness probe fails. And it is the only cross-platform-clean option — the fix lives inside the child process that owns the file descriptor, so no other process has to truncate or rotate a file that a running child holds open.

### RA-D6 — `runs/` retention: automatic on success by default, manual when switched off

Two modes, because there are two operators:

- **Automatic (default on).** When a task reaches a **successful** terminal, the orchestrator removes that task's own subtree from each `runs/` root. The ordinary user never has to learn any of this exists.
- **Manual (auto switched off).** `worc runs clean [--keep N] [--yes]` — the same shape and the same `has_active_task` guard as `logs clean` (RA-D3). This is the orchestrator-development mode: keep every run's frozen inputs and seals so a completed task can still be analyzed. The verb is available in both modes; the switch only decides whether the orchestrator also does it itself.

The switch belongs in the existing `logging:` block, which already owns "on-disk artifact retention" ([config.example.yaml:302](../../src/wastech_orchestrator/packaged/config.example.yaml#L302)); it needs a `schema_version` bump. Its exact key name is an implementation detail, not a decision here.

Four boundaries, all deliberate:

1. **Scope is `runs/` only** ([private-home-footprint.md](private-home-footprint.md) PH-D1 makes that one root). Per-task log dirs — the actual 5.7 MB/task — stay with `logs clean`, so the two commands keep disjoint territory and neither surprises the other.
2. **A failed, parked, or `manual_action_required` task is never touched**, in either mode. Automatic cleanup on failure would delete the evidence at the exact moment it is needed.
3. **`exchange-quarantine/` is never auto-deleted**, in either mode. It exists only when mutation detection caught an agent-side write to the read-only exchange — security evidence, and by construction never on the success path that triggers the automatic mode anyway. The manual verb needs an explicit opt-in to touch it.
4. **The ledger record survives.** After automatic cleanup the line in `completed.jsonl` and the `tasks/done/<id>.md` + `<id>.summary.md` pair are what remains of the task; that is the audit trail, and it is not what this decision reclaims.

**Verified: automatic cleanup on success cannot break `rerun --continue`.** `restore_for_continue` restores from the latest seal ([exchange_seal.py:356](../../src/wastech_orchestrator/core/flow/exchange_seal.py#L356)), and `rerun` refuses any status other than `failed` / `manual_action_required` / `running` ([orchestrator.py:1099](../../src/wastech_orchestrator/core/orchestrator.py#L1099)) — a `done` task is never a rerun target, so its seal has no restore consumer to lose. This is the fact that makes success-only cleanup safe rather than merely plausible, and a test must pin it: if `rerun` ever accepts `done`, this decision has to be revisited.

### RA-D7 — the orchestrator's own `.bak` artifacts are in scope

`config.yaml.bak-<UTC>`, `flows.bak-<UTC>/`, and `tools.bak-<UTC>/` are written by the orchestrator on every `install --reconfigure` and never reclaimed, so they belong to the same rule as everything else here: what we write, we clean up. They get a keep-last-N bound. `flows.bak-*` is the reason this is not cosmetic — it is a full copy of the flows directory per refresh, and the [`upgrade-flows`](upgrade-flows.md) item exists precisely to make that refresh more frequent.

`state.db*.bak*` (13 files, 2.8 MB in `wastech-mdlint`) stays out: the operator made those by hand and the orchestrator has no business deleting them.

### RA-D8 — deliberately out of scope, with reasons

- **A retention policy for `tasks/done/`.** These files live at the repo root and are git-tracked by design (the audit commit captures them), while `wastech-mdlint` has gitignored the whole tree — so "prune" means two materially different things depending on the target, and in the tracked case it is a decision about git history, not about runtime disk. It needs its own item, not a clause in this one. The 117 files / 708 KB observation stands as the trigger for that item.
- **`.worc/tasks/rejected/`.** Named here so it is not forgotten (it is the one runtime root with no owner), but empty in practice with no observed growth — inventing a policy for it now would be speculative.
- **A read surface for the seals** (`worc runs show`). Not required to make retention correct. It does become the natural follow-up to RA-D6's manual mode: an operator who switches automatic cleanup off in order to analyze runs still has only a hand-written `find` to reach the artifacts they just chose to keep. Tracked in [private-home-footprint.md](private-home-footprint.md) PH-D5.
- **Automatic cleanup of per-task log dirs.** Deliberately excluded from RA-D6 despite holding 97% of the volume — see boundary 1 above.

## Acceptance criteria (for whoever picks this up)

- After bare `worc logs clean`, `.worc/logs/` holds nothing but the ledger, and the command's output names the flag that removes that too.
- `logs clean --keep N --all` keeps N task dirs and removes the ledger; no flag combination is accepted and then ignored.
- `logs clean` refuses while a task is active (via `has_active_task`, not a second mechanism) and does not delete the live `daemon.log` while the watch daemon runs, with distinct messages for the two cases — tested on Windows path/handle semantics as well as POSIX.
- `daemon-startup.log` stops growing once the daemon has configured its `--log-file`, and a pre-configuration crash still lands in it — both asserted by test.
- The ledger's documented growth rate appears in the shipped guide; no cap is implemented.
- A successful terminal removes that task's subtree from every `runs/` root except `exchange-quarantine/`, and removes nothing else — asserted by a test that also proves a `failed` task's subtree survives untouched.
- With the switch off, nothing is removed automatically and `worc runs clean` removes the same set on demand, refusing while a task is active.
- A test pins the `rerun` status precondition that makes success-only cleanup safe, so a later widening of `rerun` fails loudly here instead of silently losing restore data.
- Any cleanup path into `runs/` is exercised by a test proving it does not become a read channel into the provider deny set.
- `install --reconfigure` keeps at most N of its own `config.yaml.bak-*` / `flows.bak-*` / `tools.bak-*` artifacts; `state.db*.bak*` is never touched.
- The docs updated in the same change: on `dev` the shipped `src/wastech_orchestrator/packaged/guide/` copy (the `.worc/` footprint page introduced by [private-home-footprint.md](private-home-footprint.md) PH-D4 documents what automatic cleanup removes and what survives) and `config.example.yaml` for the new switch + `schema_version` bump; a doc-impact note flags [docs/operations.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/operations.md#cleaning-up-logs-logs-clean) and [docs/configuration.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/configuration.md) for the `main` reconstruction pass.
