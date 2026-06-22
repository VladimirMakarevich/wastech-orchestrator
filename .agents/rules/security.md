# Security rules

The source of truth is the code (`src/wastech_orchestrator/security/`); see the [Functional Map](../../docs/functional/index.md). These rules must not be violated; the configuration validator is required to reject unsafe settings.

## Isolation

1. The agent's workspace is confined to a dedicated clone/worktree (`workspace/repo`).
2. Agents are **forbidden** to commit, push, merge, or create PRs — only the orchestrator does that.
3. Under `strict_isolation: true`, the inability to enable the required isolation fails preflight with an error.

## Environment and secrets

4. Processes receive **only allowlisted** env variables (see `security.allowed_environment` in the config).
5. Secret files (`.env`, `secrets/**`, …) are excluded from reading by the agent and from logging.
6. Secrets, tokens, and the full process environment are **not stored** in SQLite, logs, or artifacts. The request artifact stores a **redacted** representation. The deterministic minimal summary (§5.2 fallback) links to the already-redacted `logs/<id>/current.diff` and shows only a `git diff --stat` (file paths + counts, no patch body) — it never inlines a raw diff into the committed `summary.md`.
7. Git credentials and agent credentials are configured outside the orchestrator.

## Command execution

8. CLIs are run **without shell interpolation** of user-supplied strings (argument list). Task content reaches providers **only as file paths** in `AgentRunRequest` (`task_path`, `plan_path`, …); no task field is ever used to build the CLI argv, environment, command path, working paths, or security settings.
9. The task ID, branch name, and paths go through strict normalization (protection against path traversal and injection). The task `id` must match `^[a-z0-9][a-z0-9._-]{0,63}$`; normalization is **reject, don't sanitize** — a value that changes under normalization is rejected. The §19 validation gate rejects a broken/unsafe task before any branch or provider run, quarantining it to `tasks/rejected/`.
10. Flags that disable approvals/sandbox/hook-trust wholesale (`--dangerously*`, `--yolo`, `--ignore-rules`, including Claude `--dangerously-skip-permissions`) are **absolutely forbidden** by the configuration validator — they cannot be enabled through a task, through `extra_args`, or through a flow node. The **structured** full-access mode (Codex `--sandbox danger-full-access` / the `sandbox` field; Claude `--permission-mode bypassPermissions`) is **not** absolutely forbidden — the orchestrator imposes no hard refusal; it is the operator's choice and risk. It is instead **gated by `strict_isolation`** (rule #3): with the default `strict_isolation: true` it is rejected at preflight (provider config _and_ flow-node `extra_args`); the operator opts in by setting `strict_isolation: false`. 10a. **Check-command discovery is fail-closed and proof-driven (§1.2).** The commands the quality gate runs are re-resolved **only on infrastructure proof** — a check that fails to _launch_ (bounded to once per task), a changed config/CI fingerprint, or low-confidence detection — **never** because a check _reported_ failures (that would let the gate quietly rewrite its own command until it passes; a quality failure routes to `fixing`). A change to the _set_ of check commands is a **sensitive change**: it is written to the resolved profile and requires human approval on first use (fail-closed on denial, timeout, or no notifier; the first-ever set is auto-approved and recorded). The agent-assisted discovery proposal is untrusted: machine config outranks repo prose, and every proposal still passes the argv validator (no shell, no forbidden/denied/install verbs), a launch probe, and the approval gate before it can run.

## Action blacklist

11. A global blacklist of forbidden commands and paths (`security.denied_commands`, `denied_read_paths`) is applied before any run.
12. A direct push to `base_branch` is forbidden; publishing happens only through a PR.
13. Staging in the target repo is a **scoped** explicit pathspec that excludes `tasks/`/`logs/`/`workspace/`; blanket `git add .`/`-A` is forbidden, so orchestration and task artifacts never enter a code commit. In audit-footprint mode (spec §21) only the orchestrator — never an agent — makes the separate artifact commit.
14. The implemented output guardrail requires Telegram approval for tracked-file deletion and dependency manifest/lock changes after `implementation`/`fixing`. Approval is correlated to the configured chat and exact prompt, persisted in redacted form, and fails closed. Ordinary diffs and routine orchestrator commit/push/PR remain automatic.
15. Bot token and chat id are environment-only. They must not enter handles, SQLite, logs, provider argv, or artifacts. Task `contacts` are plain-text mentions only and cannot select a chat.

## Control layer

16. The Pull Request and CI remain the mandatory control layer **by default**: the orchestrator always publishes through a PR and never weakens it. Opt-in auto-merge ([configuration.md](../../docs/configuration.md), `git.auto_merge`) is implemented but **off by default** (`git.auto_merge: false`), affects **only** the publish step, and cannot weaken the security policy — the mid-pipeline dangerous-diff approval gate (§14) still fires, auto-merge runs only after checks pass with no blocking findings, it respects branch protection (never `--admin`; a merge failure → `manual_action_required`), and a per-task `auto_merge: true` is honored only when the operator set `git.auto_merge_allow_per_task` (a per-task `false` always opts out).
