# CODX-002 — Make offline Codex invocation fail closed

**Status:** done
**Priority:** P0
**Source finding:** CXP-03
**Dependencies:** CODX-001
**Officially supported CLI versions:** `codex` **≥ 0.144.4**

## Problem

For network_access=false the provider disables web search and sandbox network access, but Codex still
loads user configuration and may expose apps, MCP servers, browser/computer tools, plugins or hooks.
Those channels are not governed solely by the workspace sandbox network toggle. The orchestrator
therefore cannot prove that an offline node had no external I/O.

## Required outcome

Every Codex attempt runs from an orchestrator-controlled configuration boundary. An offline attempt
must expose no network-capable tool or connector, regardless of the user's normal Codex config,
account capabilities or project-local settings.

## In scope

- Introduce a controlled invocation/config layer for Codex attempts.
- Prevent uncontrolled user config or profiles from changing runtime capabilities.
- Preserve authentication without importing unrelated user configuration.
- Explicitly disable web, apps, MCP, browser, computer-use, plugins, hooks and equivalent external
  channels for offline nodes.
- Record a redacted effective-capability manifest for audit.
- Fail before model execution when the installed CLI cannot enforce the requested isolation.

## Acceptance criteria

- [x] Offline argv/config uses the supported equivalent of <code>--ignore-user-config</code>.
- [x] Authentication continues to work through the approved auth storage path.
- [x] A deliberately unsafe user config cannot enable web search, MCP, apps, browser, computer-use,
      hooks or plugins for an offline node.
- [x] Project-local rules/config cannot weaken the orchestrator security ceiling.
- [x] The effective-capability artifact states that external I/O is disabled without exposing
      credentials, tokens or secret paths.
- [x] Unsupported CLI versions fail in preflight/configuration, not during an agent turn.
- [x] Online nodes retain only capabilities explicitly granted by policy.
- [x] Fresh and resume invocations enforce the same boundary.
- [x] Cross-platform tests cover Windows, Linux and macOS path/config behavior.

## Verification

- Hermetic tests with a temporary CODEX_HOME containing hostile config, profiles, MCP and hooks.
- Command/config snapshot tests for offline and online nodes.
- Opt-in real-CLI smoke: an offline prompt attempts every external channel and none is available.
- Resume smoke confirming the same restrictions after session continuation.
- Full project quality gates.

## Out of scope

- Granting individual apps or MCP servers; tracked by CODX-008.
- Managing user credentials or performing Codex login.
- Building a network proxy.
- Treating prompt instructions as a security boundary.

## Likely implementation areas

- src/wastech_orchestrator/providers/codex.py
- src/wastech_orchestrator/providers/_adapter_base.py
- src/wastech_orchestrator/security
- src/wastech_orchestrator/providers/artifacts.py
- tests/providers and tests/security
- docs/configuration.md and docs/operations.md
