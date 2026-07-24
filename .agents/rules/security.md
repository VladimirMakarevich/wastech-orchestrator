# Security rules

The source of truth is the code (`src/wastech_orchestrator/security/`).

## MANDATORY for any changes in the orchestrator

Security mechanisms must not unnecessarily limit the orchestrator’s functionality or degrade the user experience. When choosing between different approaches, priority is given to preserving existing capabilities, usability, and predictable behavior. Restrictions are introduced only when required by significant risks or mandatory requirements, and then only as the least restrictive solution that provides the necessary level of protection. **This is a first-class requirement, not a soft preference — do not silently trade functionality away for isolation.**

**Every isolation, sandbox, or provider-lockdown mechanism MUST ship with an operator-controlled way to relax or fully disable it.** Hard-wiring an always-on restriction with no operator opt-out is not acceptable, even when disabling it reduces security — that is the operator’s decision to make. Such an escape hatch is **operator-config only** (never reachable from a task, `extra_args`, or a flow node — that boundary is the actual hard invariant). Its default is a deployment-posture decision the operator owns: the safer value is the norm, but a specific hatch may deliberately ship defaulting to the relaxed behavior when that is the intended out-of-the-box posture (as `disable_read_isolation` does — see rule #3). Either way, when the escape hatch is in effect the orchestrator must honor it and run with the reduced isolation rather than refuse. This does not extend to the non-negotiable fundamentals that carry no isolation trade-off — argv-not-shell launching, no secrets in logs/DB/artifacts, path-identity validation, and the PR control layer — which stay in force regardless.

## Isolation

1. The agent works only inside its dedicated workspace clone/worktree.
2. Agents may never commit, push, merge, or open PRs — only the orchestrator does.
3. Under `strict_isolation` (the default) a provider may not read or mutate the orchestrator's private runtime or control plane. Read-isolation MUST have an operator escape hatch — `disable_read_isolation` (operator-config only; **defaults to `true` — read-isolation is off out of the box**, a deliberate deployment-posture choice that departs from the § MANDATORY default-safe guidance) — that, when in effect, restores the provider's native project-instruction/config discovery (e.g. Claude again loads `CLAUDE.md` + project settings/hooks/MCP/skills) and lifts the private read-deny projection, at the cost of that isolation. Set it `false` to keep read-isolation on. `strict_isolation` is the master switch and always wins toward relaxation: `strict_isolation: false` forces read-isolation off (i.e. `disable_read_isolation` is treated as `true` regardless of its own value), while under `strict_isolation: true` the operator may still set `disable_read_isolation: true` to relax read-isolation on its own. Effective value: `disable_read_isolation OR NOT strict_isolation`. Never settable through a task, `extra_args`, or a flow node.

## Environment and secrets

4. Processes receive only allowlisted environment variables (`security.allowed_environment`).
5. Secret files inside the agent's workspace are excluded from the agent's reads and from logs.
6. No secrets, tokens, or process environment are stored in SQLite, logs, or artifacts; a committed artifact keeps only a redacted representation.
7. Git credentials and agent credentials are configured outside the orchestrator.

## Command execution

8. CLIs are launched as an argument list, never through a shell, and no task content is ever interpolated into argv, the environment, or paths — task content reaches a provider only as file paths.
9. Every dynamic identifier that becomes a path component (task id, branch name, node id) is strictly validated to prevent path traversal and injection — reject, don't sanitize — before any branch, directory, or provider run.
10. Full-access / permission-bypass sandbox modes are not hard-forbidden but gated by `strict_isolation`: rejected by default, allowed only when the operator turns strict isolation off. Quality-gate check commands are operator-authored in config only — never proposed by an agent or a flow — and still pass the argv validator.

## Action blacklist

11. A global blacklist of forbidden commands and read paths (`security.denied_commands`, `denied_read_paths`) is enforced before every run.
12. A direct push to `base_branch` is forbidden; publishing happens only through a PR.
13. Staging is scoped to an operation-specific allowlist and never lets orchestration/task artifacts into a code commit; providers may not mutate Git control state.
14. Sensitive changes — tracked-file deletions, dependency-manifest/lock edits, and operator-protected paths — require human approval that fails closed. An operator trust level (`security.trust_level`) sets which changes raise the gate and `security.protected_paths` is an always-ask floor; a task can never weaken either, nor the hard ceiling.
15. The bot token and chat id are environment-only and must never enter logs, storage, provider argv, or artifacts.

## Control layer

16. The Pull Request (and CI) is the mandatory control layer by default; the orchestrator always publishes through a PR. Opt-in auto-merge is off by default, affects only the publish step, and can never weaken the security policy.
