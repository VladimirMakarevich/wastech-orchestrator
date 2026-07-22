# Git workflow

There are two levels of git here: (A) how the orchestrator **itself** is developed; (B) how the orchestrator works with **target** repositories. Do not conflate them.

## A. Developing the orchestrator itself

- Branches off `main`: `feat/<short-description>`, `fix/<…>`, `docs/<…>`, `chore/<…>`.
- Atomic commits, imperative mood in the subject: `Add provider health preflight`.
- Before committing — `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports`, `pytest` (see [testing.md](testing.md)); `interrogate src` / `vulture` / `deptry src` are the further CI gates. Install the local mirror once with `pre-commit install && pre-commit install --hook-type pre-push` (per-commit: ruff/format/mypy/import-linter; per-push: interrogate/vulture/deptry/pytest). CI runs `ruff format --check .`; run `ruff format .` to fix.
- Keep docs in sync **in the same change** as the code: when behavior, the CLI, config, or architecture changes, update the affected docs (README, operations, configuration, cookbook, architecture) — use `/sync-docs`. **This includes the shipped, operator-facing docs under `src/wastech_orchestrator/packaged/`** (the `guide/` quickstarts, `config.example.yaml`, the built-in flows / role prompts) — the copy the operator reads after `install`, and the half most often forgotten because it lives under `src/`. Record deferred work in [../../docs/backlog/follow_ups.md](../../docs/backlog/follow_ups.md). The Stop docs-sync gate (`.claude/hooks/docs_sync_gate.py`) blocks once when `src/` changed without any `docs/`/`.agents/` change.
- Do not commit: `config.yaml`, `.venv/`, `workspace/`, `logs/`, `*.db`, secrets, the transient task folders `tasks/processing|done|failed|rejected/` (see `.gitignore`).
- **Gitignored `.md` files are not project documentation.** Anything under a gitignored path (e.g. `.archive/`, `.worc/` in a target repo) may still be physically present and readable on disk, but is not tracked, not shared, and not the current source of truth — do not read it as if it were live docs, cite it as an authority, or link to it from a tracked doc. Check `git ls-files` / `git check-ignore -v` before treating any `.md` as current. This applies the same way when the orchestrator works inside a target repository's own `.worc/` tree.
- PR into `main`; merge only after checks pass.
- Do not push to `main` directly.

## B. How the orchestrator manages a target repository (implementation contract)

This is a product invariant (see [architecture.md](architecture.md):

- Default task branch: **`repo.branch_prefix/<task-id>-<slug>`** (`worc/...` by default). A validated task `branch_name` may override the full branch name for project/customer conventions.
- Sequence: `git fetch` → checkout `base_branch` → `pull` → create the task branch.
- **Only the orchestrator (Git Manager) performs commit / push / PR**, not the agent provider.
- Publishing (`publishing`) happens only from the `ready_to_publish` status, when checks succeed and there are no blocking findings.
- Idempotency: a re-run does not create a second commit/push/PR; a stored operation fingerprint and a reconciliation of remote state are used.
- A direct push to `base_branch` is forbidden; the result goes through a PR (`gh pr create`).
- After terminal task handling, the Git Manager safely checks out `base_branch` before the Core can pick another pending task, then runs `git fetch` + `pull --ff-only` on it to refresh; the `watch` loop repeats that refresh every `orchestrator.poll_interval_seconds` (default 300) so git-pushed tasks are discovered. If the checkout cannot be proven safe, automatic continuation stops in `manual_action_required`.
- **Scoped staging and trusted Git control state (WRI-009):** stage only the agent's intended code paths via an explicit pathspec; `tasks/`/`logs/`/`workspace/`/`.worc/`/`.worc-io/` are always excluded from code commits (spec §21.1). A merge-resolution path may need `git add -A`, but before **every** commit the complete existing index is proven to contain only that operation's exact allowlist (`GitManager.assert_staged_allowed`), and the audit-commit lifecycle file is verified byte-identical to the WRI-011 frozen task packet before it is staged. Provider changes to the index, HEAD/refs, config, operation markers, or hooks are detected: the orchestrator fingerprints Git control state before each workspace-write attempt and re-verifies it after WRI-012 proves the provider tree quiescent — any drift is a non-fallback `manual_action_required` violation. Target-repository hooks/filters cannot execute in an orchestrator Git subprocess: every git command runs with a private empty `core.hooksPath` plus editor/pager/signing/fsmonitor neutralization and `--no-textconv --no-ext-diff` on diffs, and an untrusted repo-local clean/smudge/filter/diff driver stops the run in `manual_action_required`. An ignored path is not safe if an agent force-staged it — the staged-set proof, not the ignore rule, is the boundary. The provider-side read-only gitdir grant / write-deny projection (Codex/Claude) lands with WRI-002/003.
- **Footprint mode** (spec §21) is configurable: `in_repo` + audit-commit (**default** — the task + its `summary.md` in the repo, stored via a separate orchestrator-made `tasks/` commit; `logs/` stays local, never committed), `in_repo` + local-exclude (`.git/info/exclude`, never committed), or `external` (zero footprint). The tracked-`.gitignore` mode is not supported. In audit mode the **orchestrator** makes the `tasks/` commit; agents still never commit/push/PR.
- When the task was decomposed (spec §5.1): one local commit per subtask on the single branch, but still a **single PR** per parent task; subtask commits are idempotent (recorded `commit_sha`).
- The Pull Request body is the task summary (`summary.md`, spec §5.2) — the plain-language what / how / integration / why handoff.
- Auto mode is opt-in (`orchestrator.auto_mode.enabled`) and only starts the next task after a successful checkout back to `base_branch` and the post-cleanup refresh.
- On an ambiguous branch state — `manual_action_required`, with no automatic actions.
