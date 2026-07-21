# WRI-002 — Enforce Claude isolation with sandbox and tool policy

**Status:** open **Milestone:** 1 **Source:** [decision record](README.md) **Dependencies:** WRI-009, WRI-011, WRI-012

## Problem

Claude receives `Read`, `Glob`, and `Grep` for both permission profiles and also receives `Edit`, `Write`, and `Bash` for workspace-write. The current adapter projects public `security.denied_read_paths` only into `Read(...)` tool rules. It does not deny the private runtime, protect the exchange/Git control paths, enable Claude's current Bash sandbox, or prevent repository/user settings, hooks, MCP servers, plugins, custom agents, Chrome, or extra argv from adding an access path.

The previous plan therefore understated both sides of the current product contract: Claude Code now has OS-enforced Bash filesystem/network isolation on macOS, Linux, and WSL2, but not on native Windows; its sandbox protects only Bash and children, while built-in/MCP/hook/plugin tools remain governed by separate policy/configuration surfaces. A `Read` deny alone is neither the strongest available implementation nor a complete agent boundary.

## Required outcome

Every Claude fresh/resume attempt runs with one adapter-owned, non-weakening effective policy:

- All platforms deny built-in `Read` of the private home, live/frozen control plane, and every resolved internal secret source, and deny built-in `Write`/`Edit` of the exchange and resolved Git control directories.
- On macOS, Linux, and WSL2, workspace-write also enables Claude's supported Bash sandbox with `failIfUnavailable: true`, no unsandboxed-command escape, no excluded commands, private/internal `denyRead`, exchange/Git `denyWrite`, and network rules derived only from `request.network_access`.
- On native Windows, strict workspace-write omits `Bash` because the current Claude sandbox does not support that host. Read-only remains available, and workspace edits may still use `Edit`/`Write`. An operator may select the existing explicitly unsafe mode to retain unsandboxed Bash, but status/audit must say read-isolation is disabled. Provider fallback may handle a required-but-unavailable capability only through the existing infrastructure-error contract.
- All non-shell extension/configuration surfaces are disabled or positively inventoried before launch. No project/user/local setting, hook, MCP server, plugin, custom agent, skill/command, Chrome/IDE/remote-control integration, additional directory, or downloaded file may extend the readable surface.

Relocation in WRI-005 remains defense in depth; it is not used as proof of enforcement.

## In scope

### Tool and Bash filesystem policy

- Compute rules from the resolved typed layout, never from a bare `.worc` string; preserve both control and private denies after WRI-005 separates those roots.
- Deny every resolved internal secret-source path, including an explicit env file outside the private home and provider config/auth locations that an agent tool does not need; keep this separate from public `security.denied_read_paths`.
- Render absolute, platform-correct Claude tool globs and sandbox paths. Prove root/descendant and dotfile coverage; do not reuse the tool-rule `//path` syntax blindly for sandbox settings, whose absolute-path grammar is different.
- Prove deny matching through symlink/junction/reparse aliases and reject readable hard-link/file-identity aliases between workspace and internal sensitive/control files; a lexical deny alone is insufficient evidence.
- Add `Write`/`Edit` tool denies and sandbox `denyWrite` entries for the exchange plus normal/linked-worktree gitdir and common dir.
- On supported hosts set `sandbox.enabled: true`, `sandbox.failIfUnavailable: true`, `sandbox.allowUnsandboxedCommands: false`, and `sandbox.excludedCommands: []`. Reject weaker settings such as `enableWeakerNestedSandbox`, any excluded command, broad `allowRead`/`allowWrite`, unsafe Unix sockets, local binding, or network domains not produced from the request.
- Keep the parent-held exchange and Git-control manifests from WRI-001/009 as detection in depth. A mismatch is a non-fallback security-policy failure: discard the attempt result, quarantine contaminated evidence, and allow continue only from a separately verified clean snapshot.

### Configuration and tool-surface closure

- Supply the isolation settings through an adapter-owned private settings file (or an equivalently testable supported CLI surface), not repository `.claude/settings*.json`.
- Use the pinned CLI's safe/config-isolation mode that preserves authentication while disabling user/project/local customizations. Combine it with strict empty MCP configuration and an explicit built-in tool allowlist. The implementation must capability-test the exact interaction of `--safe-mode`, `--setting-sources`, `--settings`, `--strict-mcp-config`, and resume; do not assume their precedence from argv order.
- Inject WRI-011's frozen repository instructions through the controlled high-priority instruction layer; disabling live `CLAUDE.md`/customization discovery must not silently drop mandatory repository rules.
- Treat enterprise managed policy as part of the host trusted computing base because Claude does not let the invocation override it. Strict isolation must stop if resolved managed hooks/plugins/MCP/allow rules can weaken the boundary and cannot be positively shown safe; an operator-maintained allowlist may admit only audited non-agent-readable components.
- Reserve/reject every Claude `extra_args` surface that can replace or extend authority, including tools/permission mode, settings sources/file, MCP, plugins/URLs, agents, `--add-dir`, `--file`, Chrome/IDE/remote control/background/worktree, system prompt/context discovery, session selection, safe/bare modes, and permission-bypass flags. Enforce this in the provider adapter for config- and flow-supplied args; the Core must not learn Claude syntax.
- Replace the stale read-only `--permission-mode default` mapping after validating the intended noninteractive semantics against the supported CLI (`manual`/`dontAsk` or its current equivalent). Capability-check every orchestrator-owned enum/flag, including resume, before a model call; fake-CLI argv assertions cannot prove the installed parser accepts them.
- Apply the final policy at the Claude adapter boundary to agent, evaluator, and supervisor calls, with fresh/resume parity.

## Acceptance criteria

- [ ] Claude argv/settings contain absolute internal private-home/secret `Read` denies and exchange/Git `Write`/`Edit` denies; the exchange remains readable.
- [ ] On macOS/Linux/WSL2, a direct and shell/interpreter-mediated private read and exchange/Git write are blocked by the actual Bash sandbox; an unavailable sandbox fails closed and cannot fall through to the ordinary permission flow.
- [ ] On native Windows strict workspace-write exposes no Bash tool. Read-only and Edit/Write-only workspace operation are tested, while unsafe-mode Bash is unmistakably reported as unisolated.
- [ ] Private-home root, nested files, `.worc/.env`, explicit external env files, spaces/non-ASCII, Windows drive/UNC fixtures, and linked-worktree Git paths are covered without denying an unrelated parent directory.
- [ ] Both permission profiles and fresh/resume paths receive the same internal boundary, adjusted only for the documented platform/Bash capability.
- [ ] The selected permission modes are accepted by the supported Claude CLI and preserve the provider-neutral read-only/workspace-write ceiling; no test or shipped doc relies on the removed `default` enum.
- [ ] Config/flow `extra_args` cannot add a directory/file, replace policy/settings/tools, load MCP/plugins/agents, enable Chrome/IDE/remote/background/worktree surfaces, or weaken the sandbox.
- [ ] Effective settings/tool inventory proves user/project/local customizations are absent. Active managed configuration is either positively safe under an explicit trust policy or strict isolation stops before model execution.
- [ ] A Claude mutation of exchange or Git control state is detected from parent-held state before downstream use and cannot become provider success, infrastructure fallback, or automatic continue from the contaminated tree.
- [ ] Docs distinguish built-in tool policy, Bash OS sandbox, parent-side tamper detection, native-Windows restricted mode, and the operator-only unisolated branch.

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
- src/wastech_orchestrator/security/forbidden_args.py or a provider-owned authority validator
- src/wastech_orchestrator/composition.py
- tests/providers/test_claude_command.py and test_claude_run.py
- tests/security/
- .agents/rules/security.md, docs/configuration.md, docs/operations.md, and packaged guide
