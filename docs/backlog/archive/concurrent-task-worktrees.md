# Backlog: Concurrent task processing via git worktrees

Status: **deferred / not scheduled (v2)** Date: 2026-06-26 Owner: Vladimir Makarevich

Stake-in-the-ground decision record, not a spec. It captures why `git worktree` is the chosen primitive for _future_ parallel task processing, why it is explicitly **not** the answer to working-copy isolation, and what must be true before any of it is built. Parked here for traceability; linked from the backlog [README](../README.md). Nothing here overrides the hard invariants in [.agents/rules/](../../../.agents/rules/).

## The problem

The orchestrator processes **one task at a time**. A single processing slot is enforced through task status in the SQLite store (`find_active_tasks()` / `acquire_slot()` in `core/orchestrator.py`; `>1` active → `manual_action_required` in `core/recovery.py`). Independent pending tasks wait in deterministic filename order even when they touch unrelated parts of the repo and the operator would happily run several at once. There is no throughput lever: wall-clock for N tasks is the sum of N runs.

This is a deliberate v1 boundary — "concurrency and worktrees are v2" ([architecture.md](../../../.agents/rules/architecture.md)) — surfacing now because the queue is real and the operator wants parallelism.

Two motives are routinely conflated and must be separated:

- **A — isolation:** "don't let the orchestrator disturb my working copy." It checks out branches in place at `repo.local_path`.
- **B — parallelism:** "run several independent tasks at once," one working tree per task.

This record concerns **B**. **A is out of scope and is considered solved** (see Alternatives).

## What today's model assumes (and worktrees break)

Everything runs **in-place** in one working tree at `config.repo.local_path`: all git commands (`git_manager.py`) and every agent run (`core/flow/nodes/agent.py`) share that one `cwd`. State is monolithic and lives inside that one root:

- a single `.worc/state.db` (SQLite) — the slot and the whole state machine are built around one database;
- a single `tasks/{pending,processing,done,failed}/` lifecycle tree — files are physically moved between subdirectories;
- the `.worc/` runtime home (logs, workspace, flows) sits inside the repo and is untracked.

`git worktree` materializes only **tracked** files into a new tree, so `.worc/` and `tasks/` do **not** appear in a fresh worktree; they remain shared via the main checkout. That shared mutable state — not the git mechanics — is the hard part.

## Constraints

- **Single-active-task invariant is load-bearing.** Recovery, terminal cleanup, and "`>1` active → `manual_action_required`" all assume one slot. Parallelism relaxes this invariant and every dependent assumption must move with it.
- **No shared mutable working copy between active agents** — the whole point of worktree isolation; two agents must never write the same tree.
- **One SQLite state.db** today; concurrent task processes must not corrupt it.
- **Terminal cleanup returns the repo to `repo.base_branch` before the next task starts.** With worktrees the lifecycle becomes "remove the worktree," not "checkout base in _the_ tree."
- **Provider commit/push/PR remains forbidden**; only the orchestrator publishes — unchanged by concurrency.
- **Security invariants are unchanged and per-worktree processes inherit them:** allowlisted env only, no shell interpolation of user strings, no secrets in logs/DB/artifacts.
- **Provider subscription capacity is finite.** Two or more parallel Codex/Claude sessions can exhaust the five-hour/weekly allowance — see the [runtime provider capacity gate](runtime_provider_capacity_gate.md).

## Alternatives considered

| Option | Verdict |
| --- | --- |
| Do nothing — stay single-active-task | The v1 default. Zero risk, zero throughput gain. Correct until the queue and provider headroom justify the cost below. |
| Dedicated clone for **isolation** (motive A) | **Adopted for A, not B.** Pointing `repo.local_path` at a clone the orchestrator owns gives full isolation (separate `.git`, `.worc/`, `state.db`) with zero code. This removes isolation as a reason to want worktrees at all. |
| Worktree for **isolation only** (single task, side directory) | Rejected. Adds the shared-`.worc/` problem with none of the parallelism payoff; a dedicated clone is strictly simpler. |
| N independent clones for **parallelism** | Workable but worse than worktrees for the same repo: N object stores, N `fetch`es, remote/config drift. Worktrees share one object store and one fetch — the right primitive when the parallel branches are all of one repo. |
| **Worktree-per-task concurrency** (motive B) | **Chosen direction for v2.** Right primitive; cost is relaxing the core invariant and relocating shared state (below). |

## Decision

Adopt `git worktree` as the **future** primitive for concurrent task processing, and **only** for parallelism (motive B); isolation is handled by a dedicated clone and is not a reason to build this. Defer the work as v2-scale: it touches CLI, Core, Git Manager, State Store, and recovery, and inverts the single-active-task safety assumption. The cost of the alternatives is the reason: "do nothing" leaves no throughput lever, and N clones waste disk and fetches and drift apart.

Building it is gated on three hard prerequisites; until all three hold, concurrency stays off and the single-slot invariant stands:

1. **A mandatory provider capacity gate.** The [runtime capacity gate](runtime_provider_capacity_gate.md) moves from optional to a precondition — parallel sessions must not be admitted past available headroom.
2. **Shared state taken out of per-worktree trees.** A decided home for `state.db`, the `.worc/` runtime, and the `tasks/` lifecycle so concurrent tasks neither corrupt the database nor race the same lifecycle move.
3. **The slot generalized to N with a task→worktree mapping,** with recovery reconciling the live worktree set against task status on restart.

## Open questions

- **State.db topology:** one central database with cross-process locking, or one per worktree with a reconciliation step? The slot and state machine assume a single DB today.
- **Runtime-home placement:** where do `.worc/` and `tasks/` live so they are neither duplicated into each worktree nor raced? A central location outside all worktrees, or a per-task partition?
- **Degree of concurrency N:** fixed config value, or derived from the capacity gate's reported headroom?
- **Dependency ordering:** how do `depends_on`, `auto_merge`, and decomposition interact with a parallel scheduler? This overlaps the _parallel and graph decomposition_ backlog item and likely needs a real scheduler, not filename order.
- **Check contention:** parallel `checks` (tests/builds) compete for CPU, ports, and test databases. Operator responsibility, or orchestrator-managed serialization of conflicting check sets?
- **Crash recovery:** orphaned and half-created worktrees (`git worktree prune`), and reconciling the worktree set with DB state after an unclean stop.

## Implementation notes

Pointers for whoever picks this up — not a spec:

- `git_manager.py`: `prepare_branch()` would create the per-task worktree; `terminal_cleanup()` would remove it (`git worktree add/remove/prune`) instead of checking out base in one shared tree. Path resolution is already worktree-safe via `git rev-parse --git-path` (`ensure_runtime_excludes()`).
- `core/orchestrator.py` `acquire_slot()` + `state_store.find_active_tasks()`: relax the single slot to N, keyed by a task→worktree-path mapping.
- `state_store.py`: the single SQLite store is the crux — resolve the topology open question before writing code.
- Runtime home: relocate/centralize `.worc/` and the `tasks/` lifecycle so they are not inside per-task worktrees.
- `core/recovery.py`: reconcile the active task set against live worktrees on restart; prune orphans.
- [runtime_provider_capacity_gate.md](runtime_provider_capacity_gate.md): hard prerequisite, not an optional companion.
