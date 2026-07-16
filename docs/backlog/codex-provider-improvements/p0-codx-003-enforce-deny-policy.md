# CODX-003 — Enforce denied commands and denied read paths

**Status:** open
**Priority:** P0
**Source finding:** CXP-01
**Dependencies:** CODX-001, CODX-002
**Officially supported CLI versions:** `codex` **≥ 0.144.4**

## Problem

SecurityConfig.denied_commands and denied_read_paths are not projected into the Codex runtime.
Denied file contents are harvested for output redaction, but Codex can still read the files.
Likewise, the agent can execute git commit/push/PR commands even though only the orchestrator may
publish.

## Required outcome

Codex attempts must be unable to execute every configured denied command or read every configured
denied path. Enforcement must be fail closed and cross-platform; redaction or prompt instructions
do not count as enforcement.

## In scope

- Translate denied commands into an orchestrator-owned Codex execution policy.
- Enforce denied read paths at a boundary that covers direct shell reads and indirect reads through
  interpreters, scripts and tools.
- Combine default and operator-defined policy without allowing task-level weakening.
- Apply identical policy to fresh and resume attempts.
- Add preflight validation for policies the current host/CLI cannot enforce.
- Preserve secret harvesting as defense in depth after access is denied.

## Acceptance criteria

- [ ] Default git commit, git push, gh pr create and gh pr merge commands are blocked for Codex.
- [ ] Custom denied commands are blocked without requiring provider-specific syntax from the user.
- [ ] .env and secrets/** are unreadable to Codex by default.
- [ ] Custom denied paths support documented relative-path and glob semantics.
- [ ] Alternate command paths, shell wrappers and interpreter-based reads do not trivially bypass
      the policy.
- [ ] Task prompt and extra_args cannot remove or supersede generated restrictions.
- [ ] Failure to construct/enforce the policy stops the attempt before the model runs.
- [ ] A blocked operation is reported as a policy denial and does not trigger infrastructure
      fallback.
- [ ] Generated policy artifacts do not contain secret file contents.
- [ ] Claude behavior and orchestrator-owned publish operations remain unchanged.

## Verification

- Table-driven command-denial tests for default and custom commands.
- Real workspace tests for direct and indirect reads of every denied-path pattern.
- Fresh/resume parity tests.
- Host matrix tests or opt-in smokes on Windows and POSIX.
- Negative tests proving allowed git inspection commands still work.
- Full security/redaction/provider test suites and project gates.

## Out of scope

- Preventing the orchestrator's own GitManager from commit/push/PR operations.
- Content-based data-loss prevention outside configured denied paths.
- Treating post-run redaction as access control.
- Weakening the default deny list for compatibility.

## Likely implementation areas

- src/wastech_orchestrator/providers/codex.py
- src/wastech_orchestrator/security
- src/wastech_orchestrator/config/schema.py and validation.py
- tests/providers/test_codex_command.py
- tests/providers/test_codex_run.py
- tests/providers/test_redaction_sinks.py
- docs/configuration.md and docs/operations.md
