# WRI-003 — Enforce the boundary with Codex permission profiles

**Status:** open **Milestone:** 1 **Source:** [decision record](README.md) **Dependencies:** WRI-009, WRI-011, WRI-012

## Problem

The current provider always launches Codex with legacy `--sandbox` and injects `sandbox_workspace_write.network_access`. That cannot be combined with current Codex permission profiles: if `sandbox_mode` appears in any loaded config layer, `--sandbox` is passed, or a selected legacy profile sets it, Codex ignores `default_permissions`. The previous plan also assumed Codex had no read-deny or native Windows sandbox, then proposed custom Seatbelt/Landlock generation and an unconditional Windows failure. Those premises are obsolete.

The replacement must also close the authority paths that could silently supersede or bypass an orchestrator-owned policy. Task/config/flow `extra_args`, user/project/system Codex configuration, execpolicy allow-rules, hooks, local MCP/plugin servers, apps/computer use, custom subagents, fresh/resume argv differences, and network overrides all affect the effective boundary. `codex sandbox` proves the generated sandbox policy for a command; it does not by itself prove that every Codex tool/config surface is forced through that policy.

## Required outcome

Every Codex attempt uses a generated, attempt-scoped permission profile selected as the active `default_permissions`. The profile grants only the access the node needs, denies the private home, live/frozen control plane, resolved internal secret sources (including an explicit env file and the controlled Codex home), and configured sensitive paths, keeps the exchange read-only, and proves the effective policy with a no-model capability canary before starting `codex exec`.

Codex also runs with an orchestrator-controlled provider home and an explicitly minimized tool/config surface. Authentication and session state remain supported private state, but arbitrary personal/project config, rules, hooks, MCP/apps/plugins, computer use, and custom agent definitions do not join an autonomous orchestrator run. `security.denied_commands` is projected into an orchestrator-owned Codex execpolicy file so removing external rules does not weaken the repository's command ceiling.

The orchestrator uses Codex's supported cross-platform permission surface. It does not generate Seatbelt, Landlock, or Windows ACL policy itself and does not pin behavior to one exact CLI version. Permission profiles are currently beta, so a feature/capability probe is the compatibility contract.

## Policy mapping

| Orchestrator profile | Codex filesystem policy | Network |
| --- | --- | --- |
| `read-only` | `:minimal = read`; workspace roots plus resolved worktree gitdir/common dir = `read`; private/control homes, frozen control bundle, internal secret-source paths, and `security.denied_read_paths` = `deny`; current exchange = `read` | Disabled |
| `workspace-write` | `:minimal = read`; workspace roots = `write`; resolved worktree gitdir/common dir and current exchange = more-specific `read` grants; private/control homes, frozen control bundle, internal secret-source paths, and `security.denied_read_paths` = `deny` | Enabled only when the resolved node/provider policy requests it; otherwise disabled |

Use exact subtree rules for the private home and exchange. For configurable deny globs, either emit portable bounded forms or set and bound `glob_scan_max_depth`; an unbounded `**` that cannot be proven on Linux, WSL, or native Windows is rejected under strict isolation.

## In scope

- Replace the provider's legacy `--sandbox` and `sandbox_workspace_write.*` emission with a generated permission profile for both fresh `exec` and resume invocations.
- Update the config schema/upgrade path: migrate ordinary Codex `sandbox: read-only|workspace-write` settings to the provider-neutral `permission_profile` and reject legacy sandbox settings in strict mode. Preserve the existing operator-only `danger-full-access` escape only behind `strict_isolation: false`; that explicit unsafe branch uses no read-isolation claim and is unavailable to task/flow/`extra_args`.
- Select the generated profile without placing secrets or artifact contents in argv/config; keep generated policy artifacts in the private attempt directory and redact their request representation.
- Create/use a provider-owned Codex home under private state for orchestrator session/config/rules state. Do not silently copy authentication material: install/preflight must use a supported login/credential-store flow for that home and keep file credentials owner-only on POSIX and ACL-restricted on Windows.
- Use `--ignore-user-config` when supported so even the controlled home's base `config.toml` cannot override the attempt. Mark the exact project path `untrusted` through an orchestrator-owned CLI override so project `.codex/config.toml`, hooks, and rules are skipped. Separately disable live `AGENTS.md` discovery and inject WRI-011's frozen repository instruction manifest at controlled precedence; project trust and project-doc discovery are distinct Codex controls. Account for system/team/managed configuration through explicit inventory and validation; do not assume any one switch isolates every config layer.
- Generate the controlled home's only local execpolicy rules from `security.denied_commands` and prove representative exact/wrapped command matches with `codex execpolicy check`. Under strict isolation, reject any additional user/project/system/team allow-rule layer that could authorize execution outside the permission profile. Do not pass the repository-forbidden `--ignore-rules` shortcut.
- Disable Codex hooks and custom subagents/multi-agent for these provider attempts. Disable or positively inventory MCP servers, apps/plugins, computer-use tools, and any other non-shell tool capable of local filesystem access; strict isolation fails if an enabled surface cannot be shown to share the same boundary. Offline `web_search` remains disabled as today.
- Reserve/reject provider and flow `extra_args` that can select or replace authority owned by the adapter, including `-c`/`--config`, `-p`/`--profile`, `--sandbox`, `--add-dir`, `--ignore-user-config`, `--enable`/`--disable`, local/OSS provider selectors, permission-profile selectors, project trust, feature, tool, MCP, hook, agent, filesystem, and network overrides. Provider syntax stays in `providers/codex.py` or a provider-owned validator.
- Preserve `--ask-for-approval never`, current model/reasoning/output behavior, authentication through `CODEX_HOME`, and session resume semantics.
- Run a no-network, no-model preflight through `codex sandbox -P <generated-profile>` (or the equivalent supported CLI surface) against the actual generated policy. Prove direct and shell/interpreter-mediated private/Codex-home reads fail, repository/exchange reads succeed, exchange writes fail, and repository writes match the requested profile.
- Include symlink/junction/reparse aliases in the canary and reject hard-link/file-identity aliases from readable workspace paths into internal sensitive/control files; do not infer alias safety from a direct-path denial.
- Pair the sandbox canary with no-model inspection of effective configuration, active rule locations/decisions, enabled features, and `codex mcp list` (or supported equivalents). The preflight evidence must show that no unresolved local-filesystem-capable tool bypasses the profile.
- Classify a canary/policy denial as a security/policy result. It must not trigger provider fallback.
- Keep prompt hygiene only as defense in depth; it is not an acceptance criterion for filesystem enforcement.

## Cross-platform contract

- macOS, Linux, WSL, and native Windows are supported when the effective-policy canary passes.
- Native Windows uses the Codex native sandbox. Elevated mode is preferred; unelevated fallback is tested and reported, not presumed equivalent.
- WSL follows the Linux branch and is not conflated with native Windows.
- Preserve the current native-Windows standalone sandbox-helper discovery/PATH augmentation and the post-success false-success guard. The canary and real attempt use the same augmented environment and selected elevated/unelevated mode.
- Policy paths use the native absolute form Codex accepts: POSIX on macOS/Linux/WSL, drive-letter/UNC forms on native Windows. Persisted/displayed orchestrator paths remain POSIX-form per repository rules.
- Resolve `.git`/gitdir/common-dir through Git before profile generation. Linked-worktree metadata outside the workspace is granted read-only so `git status`/diff work without granting index/ref/hook writes.
- Path fixtures cover spaces, non-ASCII names, case differences where relevant, drive roots, UNC paths, symlinks, junctions, and reparse points.
- `strict_isolation` fails before model execution only when the requested effective policy cannot be demonstrated on the actual host/CLI. Windows itself is never the failure condition.

## Acceptance criteria

- [ ] No Codex attempt combines permission-profile configuration with legacy `sandbox_mode`, `--sandbox`, or `sandbox_workspace_write` settings owned by the orchestrator.
- [ ] Existing safe `sandbox` config values have a schema-upgrade path; an explicit operator full-access run under `strict_isolation: false` is loudly marked unisolated and never reported as satisfying this task.
- [ ] Read-only and workspace-write profiles enforce the mapping above on fresh and resumed attempts.
- [ ] Direct and indirect reads of the private home and configured denied paths fail; repository/exchange reads remain available; the exchange is not writable.
- [ ] Normal-clone and linked-worktree gitdir/common-dir metadata is readable but not writable in workspace-write; WRI-009 still verifies parent-held Git control state and the full staged set.
- [ ] Offline nodes have no network path, online nodes receive only their resolved network grant, and the old workspace-write network override is gone.
- [ ] Config and flow `extra_args` cannot remove, shadow, or replace the owned permission, config-isolation, workspace-root, or network settings.
- [ ] The provider-owned Codex home supports auth and fresh/resume sessions without exposing auth/config/session files to sandboxed commands or copying credentials into logs/artifacts.
- [ ] Project `.codex` config/rules/hooks are skipped as untrusted; live `AGENTS.md` discovery is independently disabled and replaced by WRI-011's frozen manifest; hooks and custom subagents are disabled; enabled MCP/app/plugin/computer-use surfaces are empty or proven to share the boundary.
- [ ] `security.denied_commands` has generated Codex execpolicy coverage, while external allow-rule layers cannot silently authorize out-of-sandbox execution.
- [ ] User/project/system/managed configuration cannot cause a false positive: sandbox and tool-surface preflights inspect the effective behavior after all remaining Codex layers have been applied.
- [ ] A missing/changed permission-profile surface or a failing canary produces a deterministic pre-model policy error under `strict_isolation`, never a best-effort run.
- [ ] Native Windows is covered as a supported branch; no source or documentation claims that Codex lacks a Windows sandbox.
- [ ] Windows helper launch failure cannot become a false success. A semantic profile/tool-surface failure is a non-fallback security/configuration error; an actual helper/process infrastructure failure keeps the existing infrastructure classification and any fallback is evaluated under that provider's own documented guarantee.

## Verification

- Table-driven argv/config tests for profiles, network modes, controlled-home auth/session behavior, project trust, disabled features/tools, fresh/resume, native/WSL path rendering, and every reserved-argument collision.
- Generated-policy tests for exact deny precedence, exchange read-only carving, portable globs, and no secret contents.
- Generated execpolicy and `codex execpolicy check` tests for direct/wrapped denied commands and absence/rejection of external allow layers.
- Real host no-model canary tests for direct/indirect denied reads, allowed/read-only controls, effective features/rules, and an empty/approved MCP inventory. Record Codex version, platform, native sandbox mode, and result.
- Fake-CLI integration tests prove wiring and error routing only; they do not count as OS-enforcement proof.
- WRI-006 Windows/Linux/macOS gate.

## Out of scope

- Hand-authored platform sandbox policy.
- Claude isolation and its platform-specific Bash contract (WRI-002).
- General Codex feature enablement for autonomous orchestrator runs; a separately reviewed surface can be added later only with equivalent containment evidence.

## Likely implementation areas

- src/wastech_orchestrator/providers/codex.py
- src/wastech_orchestrator/providers/artifacts.py
- src/wastech_orchestrator/security/forbidden_args.py or a provider-owned authority validator
- src/wastech_orchestrator/composition.py
- tests/providers/test_codex_command.py and test_codex_run.py
- tests/security/
- src/wastech_orchestrator/config/ and packaged config examples
- .github/workflows/ci.yml
- .agents/rules/security.md, docs/configuration.md, docs/operations.md, and packaged guide
