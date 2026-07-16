# Security rules

The source of truth is the code (`src/wastech_orchestrator/security/`). These rules must not be violated; the configuration validator is required to reject unsafe settings.

## Isolation

1. The agent's workspace is confined to a dedicated clone/worktree (`workspace/repo`).
2. Agents are **forbidden** to commit, push, merge, or create PRs — only the orchestrator does that.
3. Under `strict_isolation: true`, the inability to enable the required isolation fails preflight with an error.

## Environment and secrets

4. Processes receive **only allowlisted** env variables (see `security.allowed_environment` in the config).
5. Secret files (`.env`, `secrets/**`, …) **inside the agent's workspace** are excluded from reading by the agent and from logging. Distinct concern: the orchestrator may load its **own** `<repo>/.worc/.env` (or an explicit `--env-file`) into the **parent** process environment at startup (`env_file.load_env_file`, `override=False` so a real exported var wins) — that file is gitignored and lives outside the agent workspace. Loading it never widens what children receive (rule #4 still gates every child by `allowed_environment`) and its values are still scrubbed from artifacts by the redaction net.
6. Secrets, tokens, and the full process environment are **not stored** in SQLite, logs, or artifacts. The request artifact stores a **redacted** representation. The deterministic minimal summary (§5.2 fallback) links to the already-redacted `logs/<id>/current.diff` and shows only a `git diff --stat` (file paths + counts, no patch body) — it never inlines a raw diff into the committed `summary.md`.
7. Git credentials and agent credentials are configured outside the orchestrator.

## Command execution

8. CLIs are run **without shell interpolation** of user-supplied strings (argument list). Task content reaches providers **only as file paths** in `AgentRunRequest` (`task_path`, `plan_path`, …); no task field is ever used to build the CLI argv, environment, command path, working paths, or security settings.
9. The task ID, branch name, and paths go through strict normalization (protection against path traversal and injection). The task `id` must match `^[a-z0-9][a-z0-9._-]{0,63}$`; normalization is **reject, don't sanitize** — a value that changes under normalization is rejected. The §19 validation gate rejects a broken/unsafe task before any branch or provider run, quarantining it to `tasks/rejected/`.
10. Flags that disable approvals/sandbox/hook-trust wholesale (`--dangerously*`, `--yolo`, `--ignore-rules`, including Claude `--dangerously-skip-permissions`) are **absolutely forbidden** by the configuration validator — they cannot be enabled through a task, through `extra_args`, or through a flow node. Codex `extra_args` is a closed allowlist: `--add-dir`, sandbox/profile/feature selectors, and arbitrary `-c`/`--config` keys are rejected at config/flow load and again before spawn. Typed provider full-access modes remain operator-selectable only behind `strict_isolation: false`; Codex `danger-full-access` additionally requires online nodes and explicitly opts out of denied-read enforcement. 10a. **Check commands are operator-authored, never agent- or flow-supplied (§1.2).** The quality-gate commands come only from `checks.command_sets` in `config.yaml` — there is no discovery, no agent proposal, and no flow node that supplies a command; which sets run is a deterministic pure function of the task diff and each set's `paths` globs. Every command still passes the argv validator (an argv list, no shell, no forbidden/denied/install verbs) and each `cwd` is validated against `..`/absolute traversal so it cannot escape the clone. A quality failure routes to `fixing` (never fallback); a _required_ toolchain that cannot launch (a non-`skip_if_unavailable` set) leaves the gate incomplete and sends the task to `manual_action_required` — the gate can never silently pass. A `skip_if_unavailable` set whose toolchain is absent is recorded loudly as skipped (never counted as passed) and **blocks `git.auto_merge`**: an incomplete gate is never auto-merged.
10b. **Every Codex attempt uses the controlled invocation boundary.** The adapter injects
`--ignore-user-config`, strict config, an untrusted project config, an isolated home containing only
orchestrator-generated forbidden execpolicy rules, and — in isolated modes — a permission profile
carrying every denied read glob. Apps/MCP/browser/computer-use/plugins/hooks and equivalent external channels are denied.
An offline node denies profile network and web search; an online node gains only those two channels.
An existing file auth store is hard-linked without copying contents; paths/credentials are never
recorded. Codex `< 0.144.4`, a rule that does not evaluate to forbidden, or a host sandbox that
cannot enforce a native read denial fails preflight. `danger-full-access` fails when offline or
when `strict_isolation` remains enabled; with the explicit opt-out, denied reads are not enforced.

## Action blacklist

11. A global blacklist of forbidden commands and paths (`security.denied_commands`, `denied_read_paths`) is applied before any run.
12. A direct push to `base_branch` is forbidden; publishing happens only through a PR.
13. Staging in the target repo is a **scoped** explicit pathspec that excludes `tasks/`/`logs/`/`workspace/`; blanket `git add .`/`-A` is forbidden, so orchestration and task artifacts never enter a code commit. In audit-footprint mode (spec §21) only the orchestrator — never an agent — makes the separate artifact commit.
14. The implemented output guardrail requires Telegram approval for tracked-file deletion and dependency manifest/lock changes after `implementation`/`fixing`. Approval is correlated to the configured chat and exact prompt, persisted in redacted form, and fails closed. Ordinary diffs and routine orchestrator commit/push/PR remain automatic. 14a. **Approval policy (`security.trust_level`, operator-only + per-task override).** A deterministic (diff-shape) knob sets **which** changes raise the §14 gate — never whether a raised gate can auto-proceed (it always fails closed) and never the hard ceiling (env-allowlist, `bypassPermissions`/`--dangerously-*` ban, `cwd` containment) which no level can lower. `strict` gates every deletion/rename or dependency-manifest edit; `auto` (fresh-install default) turns the diff-shape gate off so only a `protected_paths` match asks. The dataclass fallback for an absent block is `strict`. A task may override with front-matter `trust_level: strict|auto`. 14b. **`security.protected_paths` (always-ask floor, config-only).** A list of repo-relative globs (same dialect as `checks.command_sets[].paths`, validated against `..`/absolute/`~` traversal) whose files require approval on **any** change at **any** `trust_level` — the floor no level can lower. It is "always ask a human," not a hard-deny "never change" (that would be a ceiling). A task can never widen or narrow it.
15. Bot token and chat id are environment-only. They must not enter handles, SQLite, logs, provider argv, or artifacts. Task `contacts` are plain-text mentions only and cannot select a chat.

## Control layer

16. The Pull Request and CI remain the mandatory control layer **by default**: the orchestrator always publishes through a PR and never weakens it. Opt-in auto-merge ([configuration.md](../../docs/configuration.md), `git.auto_merge`) is implemented but **off by default** (`git.auto_merge: false`), affects **only** the publish step, and cannot weaken the security policy — the mid-pipeline dangerous-diff approval gate (§14) still fires, auto-merge runs only after checks pass with no blocking findings, it respects branch protection (never `--admin`; a merge failure → `manual_action_required`), and a per-task `auto_merge: true` is honored only when the operator set `git.auto_merge_allow_per_task` (a per-task `false` always opts out).
