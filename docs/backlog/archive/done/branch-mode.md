# Branch mode: run a task in an existing or current branch

Status: **implemented** (config v26, 2026-07-04) Date: 2026-07-04 Owner: Vladimir Makarevich

Today the orchestrator always creates a fresh task branch from the base branch. This adds a per-task `branch_mode` with three values — `new` (today's behavior, the default), `existing` (work in a named, already-existing branch), and `current` (work in whatever branch the working tree is on, without creating or switching) — plus a global `repo.branch_mode` default. It also adds a per-task, downgrade-only `publish` modulator so a single task can stop at a local commit (or push without a PR) without switching to a different flow. Branch mode governs _where_ git operations point; the flow still governs the graph shape (whether a `publish` node exists at all).

## The problem

The single branching strategy blocks three concrete workflows. (1) **Continue/refine a branch** — a task cannot pick up work already started in an existing branch (review fixups, iterating on top of someone else's or an earlier task's code). (2) **Chain of tasks on one branch** — running several tasks in sequence that accumulate into one feature branch is impossible; each task forks a new branch from base and opens its own PR. (3) **Local experiment** — there is no low-ceremony way to run a task directly in the current checkout and inspect the result locally without a branch fork and a PR. The base is hard-wired to `repo.base_branch` in `prepare_branch()`, and fresh rerun actively deletes the task branch (`reset_branch_to_base()`), so pointing a run at a branch the operator cares about is currently unsafe.

## Constraints

- **The core does not learn CLI syntax; only the orchestrator commits/pushes/opens PRs.** All git operations stay in `GitManager`; branch-mode logic lives there and in the validation gate, not in providers.
- **Node optionality is graph shape, not a config flag** (project invariant: "task doesn't patch the graph"). Whether publishing happens at all — the presence of a `publish` node — remains a flow-level decision (`PublishingPolicy`). A per-task flag may only _modulate_ what an existing node does at runtime, never add or remove a node. Precedent: `auto_merge` is already a per-task tri-state that modulates the `publish` node without changing the graph.
- **Cross-platform.** Branch/ref handling uses `pathlib`/`Path.as_posix()` for any stored or displayed ref path; no `os.kill`/signal assumptions.
- **No secrets** in logs, SQLite, or artifacts; git stderr stays redacted on the degrade path.
- **Fail-closed validation.** An invalid `branch_mode`/`branch_ref` is rejected at preflight, before a slot or branch is taken.

## Alternatives considered

| Option | Why rejected |
| --- | --- |
| Do nothing | Leaves all three workflows blocked; the operator's only escape is to hand-manage branches around the orchestrator, which fights the tool. |
| Use external `git worktree` / manual branch prep before `worc run` | Works for the "existing branch" case but not "current", is error-prone, and the orchestrator would still delete the branch on rerun. Worktrees are the deferred v2 _concurrency_ primitive ([concurrent-task-worktrees](../concurrent-task-worktrees.md)), not an ergonomics fix for branching. |
| Per-task **publish** flag instead of branch mode | Solves only the "local experiment / no PR" slice; does not let a task target an existing or current branch. Kept as a complementary knob (see Decision), not a replacement. |
| Configurable **base branch** for `new` mode (branch from `develop`) | Considered and explicitly cut — not one of the driving needs. `existing` mode covers "work off a non-main branch" by working _in_ it; forking a new branch _from_ an arbitrary base is out of scope. |
| Publishing controlled only at the flow level (pick a `local_artifact`/`none` flow for no-PR) | Architecturally cleanest but ergonomically poor: managing per-task publishing by maintaining parallel flows or hand-editing the graph is heavy. A downgrade-only per-task modulator (below) gives the ergonomics without breaking the graph-shape invariant. |

## Decision

Add a per-task `branch_mode: new | existing | current` (with `branch_ref` for `existing`), defaulting to a new global `repo.branch_mode` (itself defaulting to `new`), so nothing changes for existing users. `new` keeps today's create-from-base behavior. `existing` checks out a named, already-existing branch and works in it. `current` uses the working tree's current branch as-is, without creating, switching, requiring a clean tree, or pulling. Branch mode is orthogonal to publishing: the flow's `PublishingPolicy` still decides whether a `publish` node runs; branch mode only redirects where its commit/push/PR point. The cost of not doing this is that the three workflows above stay impossible or unsafe.

Complementing it, add a per-task **downgrade-only** `publish` modulator (`commit | push | pull_request`, default unset → the flow's policy). It is a _cap_, never an escalation: effective scope = `min(flow_policy, task.publish)` over the ranking `commit < push < pull_request`. It maps directly onto the existing `_publish()` sequence (commit_code → commit_audit → push → create_pr): `commit` stops after the commits, `push` stops before the PR. On a flow whose graph has no `publish` node (`publishing: none`/`local_artifact`), it is a no-op — it cannot manufacture publishing, so the "graph shape = flow" invariant holds. This is the same modulation shape as `auto_merge`. It also subsumes the deferred [Dry-run without push](../../README.md#open-backlog) idea for the per-task case.

Resolved sub-decisions: **(1)** `existing` requires `branch_ref` to already exist locally or on the remote — if it exists in neither, the task fails at validation (no auto-create). **(2)** `create_pr` reuses an already-open PR for the same head→base instead of failing, so a chain of tasks on one branch converges on a single PR — see [PR-reuse rules](#pr-reuse-rules) for the edge cases. **(3)** In `existing`/`current` mode a per-task `branch_name` is ignored (there is nothing to name); `branch_mode` takes precedence, and setting `branch_name` there is a validation warning. **(4)** Sub-tasks from decomposition inherit the parent's resolved working branch (they do not each re-resolve `branch_mode`). **(5)** `current` mode is a poor fit for unattended `watch`/autonomous admission (it depends on the operator's live checkout); it is not forbidden there, but it emits a warning.

## Safety invariant: never mutate a branch the orchestrator does not own

A branch is **orchestrator-owned only in `new` mode** — the mode in which the orchestrator created it. In `existing` and `current` mode the branch belongs to the operator, and every destructive git operation must be gated on ownership. Concretely:

- **Fresh rerun / `reset_branch_to_base()`** — today this deletes the task branch (`git branch -D`) and, when requested, the remote branch (`git push origin --delete`), then recreates it from base. In `existing`/`current` mode this is forbidden: the orchestrator must not delete the local branch, must not delete the remote branch, and must not reset it to base. A fresh (non-`--continue`) rerun in these modes is refused with a clear message directing the operator to `rerun --continue` (resume in place) or to clean up manually. Reset-to-base remains available only in `new` mode. (This dovetails with "dirty tree allowed": in `current` mode the tree is expected to carry unrelated work, so a destructive reset would also risk the operator's uncommitted changes.)
- **Terminal cleanup / `terminal_cleanup()`** — today it checks out `base_branch` after the task to leave a clean slot. In `new` mode this stays. In `current` mode the orchestrator must **not** force-checkout base — it leaves the tree on the working branch where the operator left it. In `existing` mode it likewise does not delete the branch; returning HEAD to base is acceptable (it was a clean checkout of that ref) but deletion is not. The single-slot "clean tree before next task" guarantee is preserved without destroying operator state.
- **Branch preparation / `prepare_branch()`** — `new`: unchanged (fetch, checkout base, `pull --ff-only`, create branch). `existing`: fetch, then check out the existing ref; if only a remote ref exists, create a local tracking branch from `origin/<ref>` — but never fast-forward or reset the operator's local branch beyond a plain checkout. `current`: a no-op with respect to switching — it uses `HEAD` as-is, does **not** check out base, does **not** `pull --ff-only`, and does **not** require a clean tree.
- **Rationale.** These are the exact seams (`reset_branch_to_base`, `terminal_cleanup`, `prepare_branch`) where the current code assumes it owns the branch. Making each one mode-aware is what turns "point a run at my branch" from dangerous into safe. The invariant is one sentence: destructive ops (`branch -D`, remote delete, reset-to-base, force-checkout-away) run **only** when `branch_mode == new`.

## Guard: working branch == base branch

The **working branch** is the branch the task commits on: the created branch (`new`), `branch_ref` (`existing`), or the current symbolic `HEAD` (`current`). The **PR base** is `git.pr_base`. A pull request cannot have identical head and base, so when the working branch resolves to the PR base (e.g. `current` while checked out on `main`, or `existing` pointed at `main`) and the flow is PR-like, a `main→main` PR is impossible.

Decision (operator's call): **allow the push, skip the PR** rather than refusing. When `head == pr_base` and the policy is PR-like, `_publish()` still runs `commit_code` + `commit_audit` + `push` (the push targets the base branch directly), and **skips `create_pr`**, recording a clear artifact/log line that the PR was skipped because head equals base. This is effectively an automatic, run-scoped downgrade of _only_ the PR step, keeping the push. `auto_merge` becomes a no-op in this case (there is no PR to merge).

This composes with the existing graceful-degrade path and needs no new failure handling: if the direct push to base is rejected by branch protection, `GitManager` already raises the resumable manual stop (`NodeManualRequired`), persists the redacted git stderr as a `publish_error` artifact, and leaves the local commit in place. So "push to a protected main" self-corrects into "committed locally, awaiting the operator," which is the safe outcome.

Edge case: in `current` mode a **detached HEAD** has no branch to push. That is rejected at validation (a symbolic branch ref is required), not silently degraded.

## PR-reuse rules

When a chain of tasks runs on one branch, `create_pr` must not open a second PR for the same head→base. Matching keys on the (head branch, PR base) pair, and only an **`open`** PR is reusable:

- **Open (including `draft`)** — reuse it; record its URL as the publish result and skip creation. A draft counts as open (it is just an unpublished-review state, not a closed one).
- **`closed` / `merged`** — not reusable; proceed to create a new PR.
- **Multiple open matches** — reuse the most recent and emit a warning (the operator likely opened an extra by hand); do not fail.

This extends publish idempotency from _per-rerun_ (already keyed in `state.db` publish operations) to _across tasks_ on a shared branch.

## Deferred / out of scope

- **Parallel/graph decomposition with per-subtask branches** — remains the separate deferred item; V1 decomposition is linear and, per sub-decision (4), sub-tasks inherit the parent's one working branch.
- **Configurable base branch for `new` mode** (branch from `develop`) — cut; `existing` covers working off a non-main branch by working _in_ it.

## Implementation notes

- `config/schema.py` — add a `BranchMode` enum and `RepoConfig.branch_mode` (default `NEW`); config version bump. Add a `PublishScope` enum for the per-task cap.
- `task/model.py` — add `branch_mode`, `branch_ref`, `publish` to `NormalizedTask` and `ALLOWED_TASK_KEYS`; resolve precedence (task over `repo.branch_mode`); enforce `branch_ref` required iff `existing`; ignore `branch_name` outside `new`.
- `git_manager.py` — split `prepare_branch()` by mode (create-from-base / checkout-existing incl. remote-tracking / use-current-no-switch); scope `branch_name()` to `new`; gate `reset_branch_to_base()` and `terminal_cleanup()` on ownership (`new` only); make `create_pr()` reuse an existing open head→base PR and skip when `head == pr_base`.
- `orchestrator.py` — `_prepare_branch` (~2329), `_go_terminal`/terminal cleanup (~2545), `plan_rerun` (refuse fresh reset in `existing`/`current`), and the `auto_merge` path (no-op when there is no PR).
- `core/flow/nodes/publish.py` — apply the per-task `publish` cap in `_publish()` and short-circuit `create_pr` on the head==base guard.
- Validation gate — validate `branch_mode`/`branch_ref`/`publish` (IO-free), plus the fetch-and-check ref-existence check for `existing` and the detached-HEAD check for `current`.
- Docs — update the functional git-workflow map, config reference, and task-file reference in the same change.
