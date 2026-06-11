# Security rules

The source of truth is [orchestrator_final_plan.md §12](../../orchestrator_final_plan.md). These rules must not be violated; the configuration validator is required to reject unsafe settings.

## Isolation

1. The agent's workspace is confined to a dedicated clone/worktree (`workspace/repo`).
2. Agents are **forbidden** to commit, push, merge, or create PRs — only the orchestrator does that.
3. Under `strict_isolation: true`, the inability to enable the required isolation fails preflight with an error.

## Environment and secrets

4. Processes receive **only allowlisted** env variables (see `security.allowed_environment` in the config).
5. Secret files (`.env`, `secrets/**`, …) are excluded from reading by the agent and from logging.
6. Secrets, tokens, and the full process environment are **not stored** in SQLite, logs, or artifacts. The request artifact stores a **redacted** representation.
7. Git credentials and agent credentials are configured outside the orchestrator.

## Command execution

8. CLIs are run **without shell interpolation** of user-supplied strings (argument list).
9. The task ID, branch name, and paths go through strict normalization (protection against path traversal and injection).
10. Options that bypass the sandbox/permissions are **forbidden** by the configuration validator; they cannot be enabled through a task or through `extra_args`.

## Action blacklist

11. A global blacklist of forbidden commands and paths (`security.denied_commands`, `denied_read_paths`) is applied before any run.
12. A direct push to `base_branch` is forbidden; publishing happens only through a PR.
13. Irreversible/dangerous actions require human approval (HITL via Telegram).

## Control layer

14. The Pull Request and CI remain a mandatory control layer — the orchestrator does not replace them and does not auto-merge (in the first version).
