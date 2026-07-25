# Git workflow

There are two levels of git here: (A) how the orchestrator **itself** is developed; (B) how the orchestrator works with **target** repositories. Do not conflate them.

## A. Developing the orchestrator itself

### Three long-lived branches

`feat/… → dev → main → release`.

- **`dev`** — the working branch. Cut every `feat/…`, `fix/…`, `chore/…` branch off `dev` and merge it back into `dev` (squash is fine, and preferred). It carries **no derived documentation**: under `docs/` it holds only `backlog/` (the task queue work is implemented from). Never push to `dev` directly — land changes through a PR, merged only after checks pass.
- **`main`** — integration plus documentation. It receives `dev` through merge commits and is the **only** branch where the derived `docs/` tree is written; that refresh is its own task (branch `docs/…` off `main`, PR back into `main`), driven by the merged `dev` diff. Never commit code directly on `main`.
- **`release`** — published stable versions. `main → release` by merge commit, then `git tag vX.Y.Z` here; the package release and the documentation site publish from this branch.

### Two hard rules

1. **Never merge `main → dev`** (nor `release → dev`). It puts the derived documentation back on `dev` and the whole arrangement has to be rebuilt. If a fix ever lands on `main` or `release` first, port it to `dev` with `git cherry-pick`, never with a merge. A CI guard rejects any PR into `dev` whose source is `main` or `release`.
2. **`dev → main` must be a real merge commit.** A squash merge creates no merge commit, so the merge base never advances and every later merge re-proposes deleting the documentation; rebase-merge is likewise wrong for a long-lived branch pair. On GitHub choose "Create a merge commit" (squash is disabled on `main` in branch protection).

Both rules exist because the two branches diverge by one recorded seal commit (`git merge -s ours dev` on `main`); anything that resets the merge base or copies docs back into `dev` destroys it.

### Everyday hygiene

- Atomic commits with an imperative subject (`Add provider health preflight`).
- Before committing, run the gate: `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports`, `pytest` (CI also runs `interrogate` / `vulture` / `deptry`). Install the local mirror once with `pre-commit install && pre-commit install --hook-type pre-push`.
- Keep docs in sync **in the same change** as the code, scoped to the branch you are on: on `dev` that is `.agents/rules/`, `README.md`, `docs/backlog/`, and the shipped operator-facing docs under `src/wastech_orchestrator/packaged/`; the derived `docs/` refresh happens on `main`. Do not add anything under `docs/` on `dev` outside `backlog/` (and `docs/research/`, which a `deep_research` run produces) — a CI guard rejects it.
- Documents that live on `main` only are linked by absolute URL (`https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/<file>`) from any file shared with `dev`, so the link resolves from either branch. Never edit a shared file (`AGENTS.md`, `.agents/rules/`, `.claude/skills/`) on `main`: those edits flow through `dev`, otherwise the two branches diverge in content and conflict on every merge.
- Do not commit: `config.yaml`, `.venv/`, `workspace/`, `logs/`, `*.db`, secrets, or the transient task folders (see `.gitignore`).
- Gitignored `.md` files (e.g. under `.archive/`, or a target repo's `.worc/`) are not project documentation — do not treat them as current, cite them, or link to them. Check `git ls-files` / `git check-ignore -v` first.

## B. How the orchestrator manages a target repository

This is a product invariant (see [architecture.md](architecture.md)).

- **Only the orchestrator (Git Manager) commits, pushes, and opens PRs** — never the agent provider.
- Default task branch: `repo.branch_prefix/<task-id>-<slug>` (`worc/…` by default); a validated task `branch_name` may override it.
- Branch setup: `git fetch` → checkout `base_branch` → `pull` → create the task branch.
- A direct push to `base_branch` is forbidden; the result always goes through a PR, whose body is the task summary.
- Publishing happens only from the `ready_to_publish` status, when checks succeed and there are no blocking findings.
- Idempotent: a re-run never creates a second commit / push / PR.
- Stage only the agent's intended code paths; `tasks/`/`logs/`/`workspace/`/`.worc/`/`.worc-io/` are always excluded from code commits, and never `git add .` / `git add -A` for a code commit. Target-repo hooks/filters must not run inside an orchestrator git command, and any provider tampering with git control state stops the run in `manual_action_required`.
- The git footprint mode is configurable (in-repo audit-commit by default, local-exclude, or external/zero-footprint); orchestration and task artifacts never enter a code commit.
- A decomposed task makes one local commit per subtask on a single branch, but still one PR per parent task.
- After a task reaches a terminal status, the Git Manager checks out and refreshes `base_branch` before the next task can start; any ambiguous branch state stops in `manual_action_required` with no automatic actions.
- Auto mode (`orchestrator.auto_mode.enabled`) is opt-in and only starts the next task after a clean return to `base_branch`.
