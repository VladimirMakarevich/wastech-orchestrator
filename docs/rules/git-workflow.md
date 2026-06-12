# Git workflow

There are two levels of git here: (A) how the orchestrator **itself** is developed; (B) how the orchestrator works with **target** repositories. Do not conflate them.

## A. Developing the orchestrator itself

- Branches off `main`: `feat/<short-description>`, `fix/<…>`, `docs/<…>`, `chore/<…>`.
- Atomic commits, imperative mood in the subject: `Add provider health preflight`.
- Before committing — `ruff check .`, `mypy src`, `pytest` (see [testing.md](testing.md)).
- Do not commit: `config.yaml`, `.venv/`, `workspace/`, `logs/`, `*.db`, secrets, the transient task folders `tasks/processing|done|failed|rejected/` (see `.gitignore`).
- PR into `main`; merge only after checks pass.
- Do not push to `main` directly.

## B. How the orchestrator manages a target repository (implementation contract)

This is a product invariant (see [orchestrator_final_plan.md §8, §13](../orchestrator_final_plan.md)):

- Branch prefix: **`agent/<task-id>-<slug>`**.
- Sequence: `git fetch` → checkout `base_branch` → `pull` → create the task branch.
- **Only the orchestrator (Git Manager) performs commit / push / PR**, not the agent provider.
- Publishing (`publishing`) happens only from the `ready_to_publish` status, when checks succeed and there are no blocking findings.
- Idempotency: a re-run does not create a second commit/push/PR; a stored operation fingerprint and a reconciliation of remote state are used.
- A direct push to `base_branch` is forbidden; the result goes through a PR (`gh pr create`).
- After terminal task handling, the Git Manager safely checks out `base_branch` before the Core can pick another pending task. If this cannot be proven safe, automatic continuation stops in `manual_action_required`.
- **Scoped staging:** stage only the agent's intended code paths via an explicit pathspec; **never `git add .`/`-A`**; `tasks/`/`logs/`/`workspace/` are always excluded from code commits (spec §21.1).
- **Footprint mode** (spec §21) is configurable: `external` (default, zero footprint), `in_repo` + local-exclude (`.git/info/exclude`, never committed), or `in_repo` + audit-commit (a separate orchestrator-made commit). The tracked-`.gitignore` mode is not supported. In audit mode the **orchestrator** makes the artifact commit; agents still never commit/push/PR.
- When the task was decomposed (spec §5.1): one local commit per subtask on the single branch, but still a **single PR** per parent task; subtask commits are idempotent (recorded `commit_sha`).
- The Pull Request body is the task summary (`summary.md`, spec §5.2) — the plain-language what / how / integration / why handoff.
- Auto mode is opt-in (`orchestrator.auto_mode.enabled`) and only starts the next task after a successful checkout back to `base_branch`.
- On an ambiguous branch state — `manual_action_required`, with no automatic actions.
