# WRI-003 — Enforce the boundary with Codex permission profiles

**Status:** implemented; **`AGENTS.md` discovery-disable rolled back by [VF-5](runtime-validation-findings.md) (2026-07-24)** **Milestone:** 1 **Source:** [decision record](README.md) **Dependencies:** WRI-009, WRI-011, WRI-012

> **Amended by VF-5 (2026-07-24).** The separate "disable live `AGENTS.md` discovery (`project_doc_max_bytes=0`) + inject the frozen instruction manifest" control is **rolled back**: Codex's native `AGENTS.md` project-doc discovery is now left **enabled** and nothing is injected — the agent reads the repo's root instructions itself, and the tracked root instruction files are write-denied for the run (immutable). Everything else here — the generated `[permissions.worc]` profile, `.codex` project-trust `untrusted`, `--ignore-user-config`, the `--disable` feature set, and the `codex sandbox -P` canary — is **unchanged**. See [runtime-validation-findings.md](runtime-validation-findings.md) VF-5.

> **Implementation note.** Delivered against codex-cli 0.144.4. Isolation is a generated, attempt-scoped permission profile (`[permissions.worc]`, selected as `default_permissions`; injected as one inline-table `-c` value so the operator's `CODEX_HOME` is never mutated) proven by a no-model `codex sandbox -P` canary before every `codex exec`. Two scoping decisions were taken with the operator:
>
> - **Execpolicy projection of `security.denied_commands` is out of scope for this change** (see [follow_ups](../follow_ups.md)). Those commands are already contained without it — push/PR need network (disabled in every profile) and a local `git commit` is caught by WRI-009's git-control fingerprint — and Codex's execpolicy rule surface (a Starlark DSL) plus its `codex exec` ingestion are underdocumented on the beta CLI. No `.rules` are generated.
> - **Native Windows is encoded fail-closed but empirically verified by the WRI-006 CI gate**, not on the (macOS) dev host: 0.144.4 marks the old `elevated_windows_sandbox`/`experimental_windows_sandbox` flags removed, and Codex itself refuses to run unsandboxed when its Windows sandbox cannot enforce a split policy — the canary surfaces that as `CAPABILITY_UNAVAILABLE`.

## Problem

The current provider always launches Codex with legacy `--sandbox`; a node with a network grant additionally injects `sandbox_workspace_write.network_access=true`, while offline nodes instead disable `web_search`. That cannot be combined with current Codex permission profiles: if `sandbox_mode` appears in any loaded config layer, `--sandbox` is passed, or a selected legacy profile sets it, Codex ignores `default_permissions`. The previous plan also assumed Codex had no read-deny or native Windows sandbox, then proposed custom Seatbelt/Landlock generation and an unconditional Windows failure. Those premises are obsolete.

The replacement must also close the authority paths that could silently supersede or bypass an orchestrator-owned policy. Task/config/flow `extra_args`, user/project/system Codex configuration, execpolicy allow-rules, hooks, local MCP/plugin servers, apps/computer use, custom subagents, fresh/resume argv differences, and network overrides all affect the effective boundary. `codex sandbox` proves the generated sandbox policy for a command; it does not by itself prove that every Codex tool/config surface is forced through that policy.

## Required outcome

Every Codex attempt uses a generated, attempt-scoped permission profile selected as the active `default_permissions`. The profile grants only the access the node needs, denies the private home, live/frozen control plane, resolved internal secret sources (including an explicit env file and the operator's resolved `CODEX_HOME`), and configured sensitive paths, keeps the exchange and the `tasks/` lifecycle tree read-only, and proves the effective policy with a no-model capability canary before starting `codex exec`.

Codex keeps authenticating through the operator's `CODEX_HOME` — credentials and login stay outside the orchestrator — while the config/tool surface is explicitly minimized by layers: user config ignored, the project layer untrusted, and every remaining layer inventoried. Arbitrary personal/project config, rules, hooks, MCP/apps/plugins, computer use, and custom agent definitions do not join an autonomous orchestrator run. `security.denied_commands` is projected into an orchestrator-owned Codex execpolicy rules layer so removing external rules does not weaken the repository's command ceiling. A dedicated orchestrator-controlled provider home is deferred hardening ([archived task](../archive/codex-controlled-provider-home.md)).

The orchestrator uses Codex's supported cross-platform permission surface. It does not generate Seatbelt, Landlock, or Windows ACL policy itself and does not pin behavior to one exact CLI version. Permission profiles are currently beta, so a feature/capability probe is the compatibility contract.

## Policy mapping

| Orchestrator profile | Codex filesystem policy | Network |
| --- | --- | --- |
| `read-only` | `:minimal = read`; workspace roots plus resolved worktree gitdir/common dir = `read`; private/control homes, frozen control bundle, internal secret-source paths, and `security.denied_read_paths` = `deny`; current exchange = `read` | Disabled |
| `workspace-write` | `:minimal = read`; workspace roots = `write`; resolved worktree gitdir/common dir, `tasks/` lifecycle tree, and current exchange = more-specific `read` grants; private/control homes, frozen control bundle, internal secret-source paths, and `security.denied_read_paths` = `deny` | Disabled — the existing validator rule forbidding a Codex workspace-write node with network access (F17b) stays in force; relaxing it is out of scope for this cluster |

Use exact subtree rules for the private home and exchange. For configurable deny globs, either emit portable bounded forms or set and bound `glob_scan_max_depth`; an unbounded `**` that cannot be proven on Linux, WSL, or native Windows is rejected under strict isolation.

## Provided by WRI-001 (and what stays core-side until this task)

WRI-001 built the exchange and a **core-side, detection-only** containment layer; WRI-003 supplies the OS-enforced Codex permission profile that makes the exchange read-only and denies the private/control roots:

- `providers/exchange.py.assert_orchestration_paths_contained` (before every `run_stage`) and `assert_exchange_current_task_only` (pre-launch) are containment/preflight assertions, not enforcement. WRI-003's generated profile is what actually grants the exchange as read-only and denies `<repo>/.worc`.
- The exchange root to keep read-only and the private/control root to deny are `<repo>/.worc-io` and `<repo>/.worc` (`EXCHANGE_HOME` in `providers/artifacts.py`); WRI-004 hands these over as typed `layout.exchange_root` / `private_home` fields, and `internal_denied_paths` carries the resolved secret sources.
- `build_exchange_manifest` (`providers/exchange.py`) is available for a parent-side pre/post integrity check paralleling the Claude one; a detected mutation is a non-fallback security result.

## In scope

- Replace the provider's legacy `--sandbox` and `sandbox_workspace_write.*` emission with a generated permission profile for both fresh `exec` and resume invocations.
- Update the config schema/upgrade path: migrate ordinary Codex `sandbox: read-only|workspace-write` settings to the provider-neutral `permission_profile` and reject legacy sandbox settings in strict mode. Preserve the existing operator-only `danger-full-access` escape only behind `strict_isolation: false`; that explicit unsafe branch uses no read-isolation claim and is unavailable to task/flow/`extra_args`.
- Select the generated profile without placing secrets or artifact contents in argv/config; keep generated policy artifacts in the private attempt directory and redact their request representation.
- Keep authentication, sessions, and the credential store in the operator's `CODEX_HOME`; deny sandboxed commands read access to that resolved home and never copy or log credential material. Supply generated rules/config through orchestrator-owned inputs rather than by mutating the operator home.
- Use `--ignore-user-config` when supported so the operator home's base `config.toml` cannot override the attempt. Mark the exact project path `untrusted` through an orchestrator-owned CLI override so project `.codex/config.toml`, hooks, and rules are skipped. Separately disable live `AGENTS.md` discovery and inject WRI-011's frozen repository instruction manifest at controlled precedence; project trust and project-doc discovery are distinct Codex controls. Account for system/team/managed configuration through explicit inventory and validation; do not assume any one switch isolates every config layer.
- Generate the orchestrator-owned execpolicy rules layer from `security.denied_commands` through a supported, capability-tested input surface and prove representative exact/wrapped command matches with `codex execpolicy check`. Under strict isolation, reject any additional user/project/system/team allow-rule layer that could authorize execution outside the permission profile. Do not pass the repository-forbidden `--ignore-rules` shortcut.
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
- Native Windows uses the Codex native sandbox as exposed by the current CLI. The concrete mode surface must be re-verified on a current Windows host before being encoded — the installed 0.144.4 marks the older `elevated_windows_sandbox`/`experimental_windows_sandbox` feature flags removed. Where mode differences still exist, they are tested and reported, not presumed equivalent.
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
- [ ] Authentication and fresh/resume sessions keep working through the operator's `CODEX_HOME` while sandboxed commands cannot read its auth/config/session files and no credential material is copied into logs/artifacts.
- [ ] Project `.codex` config/rules/hooks are skipped as untrusted; live `AGENTS.md` discovery is independently disabled and replaced by WRI-011's frozen manifest; hooks and custom subagents are disabled; enabled MCP/app/plugin/computer-use surfaces are empty or proven to share the boundary.
- [~] `security.denied_commands` Codex execpolicy coverage — **descoped** (operator-decided; see the note at the top of this doc and [follow_ups](../follow_ups.md)). The commands are already contained (network off in every profile, a local `git commit` caught by WRI-009's fingerprint, orchestrator-only commit/push/PR); external allow-rule layers are neutralized by `--ignore-user-config` + the untrusted project layer, and the no-model capability smoke records the effective `codex mcp list` inventory.
- [ ] User/project/system/managed configuration cannot cause a false positive: sandbox and tool-surface preflights inspect the effective behavior after all remaining Codex layers have been applied.
- [ ] A missing/changed permission-profile surface or a failing canary produces a deterministic pre-model policy error under `strict_isolation`, never a best-effort run.
- [ ] Native Windows is covered as a supported branch; no source or documentation claims that Codex lacks a Windows sandbox.
- [ ] Windows helper launch failure cannot become a false success. A semantic profile/tool-surface failure is a non-fallback security/configuration error; an actual helper/process infrastructure failure keeps the existing infrastructure classification and any fallback is evaluated under that provider's own documented guarantee.

## Verification

- Table-driven argv/config tests for profiles, network modes, controlled-home auth/session behavior, project trust, disabled features/tools, fresh/resume, native/WSL path rendering, and every reserved-argument collision.
- Generated-policy tests for exact deny precedence, exchange read-only carving, portable globs, and no secret contents.
- (Descoped: generated execpolicy / `codex execpolicy check` tests for denied commands — see the descope note above. External allow layers are covered by the `--ignore-user-config` + untrusted-project argv tests and the capability smoke's MCP-inventory evidence.)
- Real host no-model canary tests for direct/indirect denied reads, allowed/read-only controls, effective features/rules, and an empty/approved MCP inventory. Record Codex version, platform, native sandbox mode, and result.
- Fake-CLI integration tests prove wiring and error routing only; they do not count as OS-enforcement proof.
- WRI-006 Windows/Linux/macOS gate.

## Out of scope

- Hand-authored platform sandbox policy.
- Claude isolation and its platform-specific Bash contract (WRI-002).
- General Codex feature enablement for autonomous orchestrator runs; a separately reviewed surface can be added later only with equivalent containment evidence.
- Relaxing the Codex workspace-write network restriction — the existing validator rule stays in force; any future relaxation is its own reviewed change with exfiltration evidence.
- An orchestrator-controlled Codex provider home — deferred hardening ([archived task](../archive/codex-controlled-provider-home.md)).

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
