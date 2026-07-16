# CODX-001 — Close authority-expanding Codex extra_args

**Status:** done
**Priority:** P0
**Source finding:** CXP-02
**Dependencies:** none
**Officially supported CLI versions:** `codex` **≥ 0.144.4**

## Problem

Provider-level and node-level extra_args are appended after the orchestrator's sandbox and network
overrides. The current validator rejects obvious bypass flags but still permits options such as
additional writable directories, arbitrary config overrides, profiles and feature switches.
Consequently a task or operator config can expand filesystem, network or tool authority beyond the
effective node policy.

## Required outcome

Codex extra arguments become a typed, fail-closed extension point that cannot weaken approvals,
sandboxing, writable/readable scope, network restrictions, rules, environment isolation or external
tool restrictions.

## In scope

- Define the Codex-specific allowlist of harmless extra arguments.
- Parse both tokenized and equals forms of <code>-c</code>/<code>--config</code>.
- Reject authority-expanding CLI flags and config keys at config validation and immediately before
  process spawn.
- Cover provider defaults and per-node overrides.
- Produce actionable configuration errors that identify the rejected option without echoing secrets.
- Update configuration documentation and examples.

## Acceptance criteria

- [x] <code>--add-dir</code> is rejected from provider and node extra_args.
- [x] Sandbox and sandbox_permissions overrides cannot select or approximate full disk access.
- [x] Network and web-search overrides cannot enable access denied by the node.
- [x] Profiles, user config selectors, rule bypasses and arbitrary feature enablement cannot expand
      authority.
- [x] All supported syntactic forms, including repeated <code>-c</code>, split values and
      <code>--config=key=value</code>, are handled deterministically.
- [x] The same validation runs during config load and in CodexProvider immediately before spawn.
- [x] Benign allowlisted arguments still work for fresh and resume invocations.
- [x] Error messages and artifacts contain no secret config values.
- [x] Existing Claude argument behavior is unchanged.

## Verification

- Unit tests for every known authority-expanding flag/config key in the supported Codex CLI
  contract (`>= 0.144.4`).
- Property/table-driven tests for alternate argument spellings and ordering.
- Command-builder tests proving task extra_args cannot override fixed security options.
- Full ruff, mypy, lint-imports and pytest gates.

## Out of scope

- Enabling new external Codex capabilities; tracked by CODX-008.
- Replacing the global SecurityConfig schema.
- Automatically repairing unsafe operator configuration.
- Provider SDK/API backends.

## Likely implementation areas

- src/wastech_orchestrator/security/forbidden_args.py
- src/wastech_orchestrator/config/validation.py
- src/wastech_orchestrator/providers/codex.py
- tests/security, tests/config and tests/providers
- docs/configuration.md
