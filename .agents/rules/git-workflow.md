# Git workflow

There are two levels of git here: (A) how the orchestrator **itself** is developed; (B) how the orchestrator works with **target** repositories. Do not conflate them.

## A. Developing the orchestrator itself

- Branch off `main`: `feat/…`, `fix/…`, `docs/…`, `chore/…`. Never push to `main` directly — land changes through a PR, merged only after checks pass.
- Atomic commits with an imperative subject (`Add provider health preflight`).
- Before committing, run the gate: `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports`, `pytest` (CI also runs `interrogate` / `vulture` / `deptry`). Install the local mirror once with `pre-commit install && pre-commit install --hook-type pre-push`.
- Keep docs in sync **in the same change** as the code — including the shipped operator-facing docs under `src/wastech_orchestrator/packaged/`.
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
