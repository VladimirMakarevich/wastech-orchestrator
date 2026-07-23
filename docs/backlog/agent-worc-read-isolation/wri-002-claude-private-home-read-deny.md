# WRI-002 — Enforce Claude isolation with sandbox and tool policy

**Status:** implemented **Milestone:** 1 **Source:** [decision record](README.md) **Dependencies:** WRI-009, WRI-011, WRI-012

Shipped in `providers/claude.py`: the read-only mode moved to the documented `dontAsk` (dropping the legacy `default` alias); a `SandboxCapability` host seam (`default_sandbox_probe`, macOS/Linux-avail/Linux-missing-deps/native-Windows via `shutil.which`) + a `resolve_claude_tools` decision helper that keeps Bash + emits the OS Bash-sandbox on macOS/Linux-avail, drops Bash on native-Windows strict, raises a new pre-model `ErrorClass.CAPABILITY_UNAVAILABLE` on Linux-missing-deps strict, and keeps unsandboxed Bash under `strict_isolation: false`; a `--tools` hard allowlist + `--strict-mcp-config` (kept `--setting-sources ""`, deliberately **not** `--safe-mode` to preserve the F37 `allow_native_memory` opt-in); a private `--settings` OS-sandbox JSON (`build_sandbox_settings` — `denyRead`/`denyWrite`/`credentials` deny-only, no `mask`/`tlsTerminate`/`enableWeakerNestedSandbox`, plain-absolute paths); `//`-anchored `Read`/`Write`/`Edit` tool denies projecting `InternalDenyPolicy.denied_paths` (excluding the F37-owned `~/.claude`) and the `AgentRunRequest.write_guard` exchange/gitdir/common-dir/hooks/`tasks/` Write/Edit denies; a `_find_reserved_claude_args` authority-flag validator; and a host-aware `isolation_reasons`. Cross-cutting: `ProviderWriteGuardPolicy` (leaf `runtime_layout.py`) + `GitManager.resolve_control_paths` (normal-clone + linked-worktree) threaded per workspace-write attempt in the node runner's WRI-009 bracket; `deny_policy` constructor-injected on `BaseCliProvider`; `ErrorClass.CAPABILITY_UNAVAILABLE` with a host-verified conditional fallback (`router.fallback_allowed` + injected `isolation_checks`/`_can_isolate`) routing to `manual_action_required` when no fallback qualifies; and parent-held pre/post-attempt exchange-manifest detection (`providers/exchange.diff_exchange_manifests` + `exchange_publish.capture_exchange_manifest`/`assert_exchange_unchanged`) in the agent + evaluator brackets. See [follow_ups.md](../follow_ups.md) for the shipped entry and the deferred items (managed-policy automated inspection, supervisor exchange detection, the host-verify smokes under WRI-006).

## Problem

Claude receives `Read`, `Glob`, and `Grep` for both permission profiles and also receives `Edit`, `Write`, and `Bash` for workspace-write. The current adapter projects public `security.denied_read_paths` only into `Read(...)` tool rules. It does not deny the private runtime, protect the exchange/Git control paths, enable Claude's current Bash sandbox, or prevent repository/user settings, hooks, MCP servers, plugins, custom agents, Chrome, or extra argv from adding an access path.

The previous plan therefore understated both sides of the current product contract: Claude Code now has OS-enforced Bash filesystem/network isolation on macOS, Linux, and WSL2, but not on native Windows; its sandbox protects only Bash and children, while built-in/MCP/hook/plugin tools remain governed by separate policy/configuration surfaces. A `Read` deny alone is neither the strongest available implementation nor a complete agent boundary.

## Required outcome

Every Claude fresh/resume attempt runs with one adapter-owned, non-weakening effective policy:

- All platforms deny built-in `Read` of the private home, live/frozen control plane, and every resolved internal secret source, and deny built-in `Write`/`Edit` of the exchange and resolved Git control directories.
- On macOS, Linux, and WSL2, workspace-write also enables Claude's supported Bash sandbox with `failIfUnavailable: true`, no unsandboxed-command escape, no excluded commands, private/internal `denyRead`, exchange/Git `denyWrite`, and network rules derived only from `request.network_access`.
- On native Windows, strict workspace-write omits `Bash` because the current Claude sandbox does not support that host. Read-only remains available, and workspace edits may still use `Edit`/`Write`. An operator may select the existing explicitly unsafe mode to retain unsandboxed Bash, but status/audit must say read-isolation is disabled. A required-but-unavailable host capability is a new deterministic pre-model `CAPABILITY_UNAVAILABLE` infrastructure error class, raised before any model invocation; the router may fall back only to a provider whose effective isolation for the node is same-or-stricter. A failed policy/sandbox proof on a supported host remains a non-fallback security result.
- All non-shell extension/configuration surfaces are disabled or positively inventoried before launch. No project/user/local setting, hook, MCP server, plugin, custom agent, skill/command, Chrome/IDE/remote-control integration, additional directory, or downloaded file may extend the readable surface.

Relocation in WRI-005 remains defense in depth; it is not used as proof of enforcement.

## Provided by WRI-001 (and what stays core-side until this task)

WRI-001 built the exchange and a **core-side, detection-only** containment layer; WRI-002 supplies the actual OS/tool enforcement that makes the exchange read-only for Claude:

- `providers/exchange.py.assert_orchestration_paths_contained` (called before every agent/evaluator/supervisor `run_stage`) and `assert_exchange_current_task_only` (pre-launch) are containment/preflight assertions, not access control. WRI-002's built-in `Write`/`Edit` denies for the exchange + Git dirs, plus the supported-host Bash sandbox `denyWrite`, are what actually stop a mutation.
- `providers/exchange.py.build_exchange_manifest` (file type, link count, digest, …) is the ready-made primitive for the required pre/post-attempt exchange integrity check; a mutation it detects is a non-fallback policy violation, and the changed copy must not be consumed downstream.
- The exchange root and the private/control roots WRI-002 must deny are `<repo>/.worc-io` and `<repo>/.worc` (see `EXCHANGE_HOME` in `providers/artifacts.py`); WRI-004 will hand these over as typed `layout.exchange_root` / `private_home` fields.

## In scope

### Tool and Bash filesystem policy

- Compute rules from the resolved typed layout, never from a bare `.worc` string; preserve both control and private denies after WRI-005 separates those roots.
- Deny every resolved internal secret-source path, including an explicit env file outside the private home and provider config/auth locations that an agent tool does not need; keep this separate from public `security.denied_read_paths`.
- Render absolute, platform-correct Claude tool globs and sandbox paths. Prove root/descendant and dotfile coverage; do not reuse the tool-rule `//path` syntax blindly for sandbox settings, whose absolute-path grammar is different.
- Prove deny matching through symlink/junction/reparse aliases and reject readable hard-link/file-identity aliases between workspace and internal sensitive/control files; a lexical deny alone is insufficient evidence.
- Add `Write`/`Edit` tool denies and sandbox `denyWrite` entries for the exchange plus normal/linked-worktree gitdir and common dir. The settings schema nests the filesystem rules as `sandbox.filesystem.denyRead`/`denyWrite`/`allowRead`/`allowWrite` and network rules under `sandbox.network.*` — use the real keys in code, tests, and docs.
- Prove the exchange/Git `denyWrite` entries actually override the sandbox's built-in linked-worktree allowance: the current sandbox deliberately permits writes to the main repository's shared `.git` directory (except `hooks/` and `config`) when the cwd is a linked worktree. If the built-in allow cannot be overridden, record the residual and fail strict isolation.
- Evaluate `sandbox.credentials` (file/env `deny` entries) as the purpose-built surface for env-file and secret-source protection; never admit `mask` entries or `network.tlsTerminate`, which authorize credential injection through the sandbox proxy.
- On supported hosts set `sandbox.enabled: true`, `sandbox.failIfUnavailable: true`, `sandbox.allowUnsandboxedCommands: false`, and `sandbox.excludedCommands: []`. Reject weaker settings such as `enableWeakerNestedSandbox`, any excluded command, broad `allowRead`/`allowWrite`, unsafe Unix sockets, local binding, or network domains not produced from the request.
- Keep the parent-held exchange and Git-control manifests from WRI-001/009 as detection in depth. A mismatch is a non-fallback security-policy failure: discard the attempt result, quarantine contaminated evidence, and allow continue only from a separately verified clean snapshot.

### Configuration and tool-surface closure

- Supply the isolation settings through an adapter-owned private settings file (or an equivalently testable supported CLI surface), not repository `.claude/settings*.json`.
- Use the pinned CLI's safe/config-isolation mode that preserves authentication while disabling user/project/local customizations. Combine it with strict empty MCP configuration and an explicit built-in tool allowlist. The implementation must capability-test the exact interaction of `--safe-mode`, `--setting-sources`, `--settings`, `--strict-mcp-config`, and resume; do not assume their precedence from argv order.
- Account for cross-scope settings merge: sandbox filesystem/network arrays merge across user/project/local scopes rather than replace, so the effective-settings inventory must prove no foreign array entry (for example a user-scope `allowRead` or `excludedCommands`) widened the adapter policy after source isolation.
- Compose with the existing native-memory config-dir denies (`_native_memory_deny_tools`, `allow_native_memory` — F37): the adapter-owned policy must preserve that behavior without duplicating or contradicting those rules.
- Inject WRI-011's frozen repository instructions through the controlled high-priority instruction layer; disabling live `CLAUDE.md`/customization discovery must not silently drop mandatory repository rules.
- Treat enterprise managed policy as part of the host trusted computing base because Claude does not let the invocation override it. Strict isolation must stop if resolved managed hooks/plugins/MCP/allow rules can weaken the boundary and cannot be positively shown safe; an operator-maintained allowlist may admit only audited non-agent-readable components.
- Reserve/reject every Claude `extra_args` surface that can replace or extend authority, including tools/permission mode, settings sources/file, MCP, plugins/URLs, agents, `--add-dir`, `--file`, Chrome/IDE/remote control/background/worktree, system prompt/context discovery, session selection, safe/bare modes, and permission-bypass flags. Enforce this in the provider adapter for config- and flow-supplied args; the Core must not learn Claude syntax.
- Replace the deprecated read-only `--permission-mode default` mapping after validating the intended noninteractive semantics against the supported CLI (`manual`/`dontAsk` or its current equivalent). The installed 2.1.210 omits `default` from the documented choices but still accepts it as a hidden legacy alias, so this is hardening against enum drift, not an outage fix. Capability-check every orchestrator-owned enum/flag, including resume, before a model call; fake-CLI argv assertions cannot prove the installed parser accepts them.
- Apply the final policy at the Claude adapter boundary to agent, evaluator, and supervisor calls, with fresh/resume parity.

## Acceptance criteria

- [x] Claude argv/settings contain absolute internal private-home/secret `Read` denies and exchange/Git `Write`/`Edit` denies; the exchange remains readable. (`test_internal_read_denies_seal_read_write_edit`, `test_write_guard_denies_write_edit_but_keeps_exchange_readable`, `test_build_sandbox_settings_shape_is_hardened`.)
- [~] On macOS/Linux/WSL2 the settings enable the Bash sandbox with `failIfUnavailable` + private `denyRead`/exchange-Git `denyWrite`, and an unavailable sandbox fails closed pre-model (`CAPABILITY_UNAVAILABLE`) rather than falling through. **The actual OS-sandbox block of a direct/shell-mediated read+write is a WRI-006 native host smoke** — the deterministic suite proves the wiring + fail-closed path only.
- [x] On native Windows strict workspace-write exposes no Bash tool; read-only and Edit/Write-only operation are tested; the unsafe (`strict_isolation: false`) branch keeps Bash and is reported unisolated by the preflight verdict. (`test_native_windows_workspace_write_omits_bash`, `test_non_strict_isolation_keeps_bash_on_unsandboxed_host`.)
- [x] Private-home root, nested files, `.worc/.env`, explicit external env files, spaces/non-ASCII, Windows drive/UNC fixtures, and linked-worktree Git paths are covered without denying an unrelated parent directory. (`test_internal_deny_globs_handle_spaces_unicode_and_drive_paths`, `test_resolve_control_paths_linked_worktree_splits_gitdir_and_common`.)
- [x] Both permission profiles and fresh/resume paths receive the same internal boundary, adjusted only for the documented platform/Bash capability (the argv builder is fresh/resume-agnostic apart from `--resume`).
- [x] The selected permission modes (`dontAsk`/`acceptEdits`) are accepted by the supported Claude CLI 2.1.217 and preserve the read-only/workspace-write ceiling; no test or shipped doc relies on the legacy `default` alias.
- [x] Config/flow `extra_args` cannot add a directory/file, replace policy/settings/tools, load MCP/plugins/agents, enable Chrome/IDE/remote/background/worktree surfaces, or weaken the sandbox. (`test_reserved_extra_args_are_rejected`, `test_claude_reserved_extra_arg_is_flagged`.)
- [~] `--setting-sources ""` + `--strict-mcp-config` + the reserved-args validator prove user/project/local customizations + MCP are absent by construction. **Automated offline inspection of active enterprise managed configuration + an operator trust-allowlist is deferred** (documented as trusted-computing-base) — see follow_ups.
- [x] A Claude mutation of exchange or Git control state is detected from parent-held state before downstream use and cannot become provider success, infrastructure fallback, or automatic continue from the contaminated tree. (`test_agent_exchange_mutation_is_detected_from_parent_state`; WRI-009 git bracket.)
- [x] Docs distinguish built-in tool policy, Bash OS sandbox, parent-side tamper detection, native-Windows restricted mode, and the operator-only unisolated branch (security.md §3/§3a/§13a, configuration.md, operations.md, packaged guide).

## Verification

- Table-driven argv/settings tests over permission profiles, session state, network policy, and injected POSIX/Windows/WSL paths.
- Provider runtime tests for every reserved flag in split and `--flag=value` forms, including repeated/value-consuming options.
- Fake-CLI tests proving request/footer paths remain exchange-only and the effective config inventory is checked before launch. These prove wiring, not OS enforcement.
- Native macOS/Linux/WSL2 sandbox smokes for direct/wrapped reads and writes. If Claude exposes no credential-free sandbox runner, run the smallest authenticated provider smoke in a protected integration lane and record that normal deterministic CI cannot independently prove the host boundary.
- Native-Windows tests proving strict mode removes Bash rather than silently running it unsandboxed.
- Mutation tests for content change, add/delete/rename, hard-link or symlink/junction substitution, NTFS named streams, and timestamp-only noise; only content/layout/identity changes violate the manifest.
- WRI-006 cross-platform gate.

## Out of scope

- Inventing a custom Claude OS sandbox for native Windows.
- Claiming hooks/MCP/plugins are covered by Bash sandbox rules.
- Codex enforcement (WRI-003).
- Relocating private runtime state (WRI-005).

## Likely implementation areas

- src/wastech_orchestrator/providers/claude.py
- src/wastech_orchestrator/providers/base.py and routing/router.py (`CAPABILITY_UNAVAILABLE` classification and same-or-stricter fallback gate)
- src/wastech_orchestrator/security/forbidden_args.py or a provider-owned authority validator
- src/wastech_orchestrator/composition.py
- tests/providers/test_claude_command.py and test_claude_run.py
- tests/security/
- .agents/rules/security.md, docs/configuration.md, docs/operations.md, and packaged guide
