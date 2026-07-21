# WRI-005 — Relocate private runtime state outside the working tree

**Status:** open **Milestone:** 2 (defense in depth) **Source:** [decision record](README.md); shares runtime placement context with the tracked [worktree decision record](../archive/concurrent-task-worktrees.md) **Dependencies:** WRI-002, WRI-003, WRI-004, WRI-007, WRI-010

## Problem

The current `.worc` directory mixes two different responsibilities:

- An operator-editable control plane discovered relative to the repository: `config.yaml`, packaged guide copies, flows/role prompts, and tools.
- Private runtime state: `.env`, `state.db`, logs/audit, memory, security reports, rejected files, durable HITL/process-control data, and provider attempt artifacts.

Moving all of `.worc` would break bootstrap: the orchestrator needs the repository-local config before it can discover any configured external location, and install/upgrade intentionally seed editable flows and tools into `.worc`. Conversely, leaving private state under the agent `cwd` increases discoverability and blast radius.

Relocation alone is not a hard cross-provider access control. Codex and supported-host Claude policies must deny the resolved external path independently; native-Windows strict Claude operation must continue to omit Bash. An unsafe provider mode can still read a known external absolute path.

## Required outcome

Keep `control_home = <repo>/.worc` for repository-local configuration, guide, flows, and tools. Move `private_home` to a configurable per-repository directory under the platform's user-state/data location. Keep `exchange_root = <repo>/.worc-io`. All consumers use the typed WRI-004 layout and no longer infer private paths from `control_home`.

## Bootstrap contract

1. Resolve the repository and `control_home` using the existing CLI discovery rules.
2. Load and validate `<control_home>/config.yaml`; configuration cannot depend on values from the private `.env` to discover `private_home`.
3. Resolve `private_home` from an explicit absolute config override or a deterministic platform default keyed by the canonical repository path (human-readable repo name plus collision-resistant hash).
4. Validate that `private_home` is outside the repository/worktree and is not a symlink/junction/reparse escape.
5. Load the default private `.env` (or explicit `--env-file`) into the parent process under the existing allowlist/redaction contract and add its resolved path to the internal provider deny policy even when it is outside `private_home`.
6. Construct the store, runtime services, recovery, logs, memory, process control, providers, and exchange lifecycle from the resolved typed layout.

Use the platform-appropriate user-state/data API rather than hardcoded `~/.local`, `~/Library`, or `%APPDATA%` strings. Persisted/displayed paths use POSIX form; actual filesystem calls use native `Path` objects.

## In scope

- Add an explicit config field for the absolute private-runtime override and bump/upgrade the configuration schema; update both packaged and repository example configs.
- Define the platform default and repository identity/hash behavior for Windows, macOS, and Linux/WSL.
- Migrate runtime consumers: state DB, task logs/audit, memory, `.env`, security reports, rejected files, HITL, supervisor files, PID/sentinel/children records, and provider-attempt state.
- Classify the install-created `.worc/workspace/` directory, which no code writes today: remove it from `WORC_RUNTIME_DIRS` or migrate it with the private state — do not leave an unclassified in-repo runtime path.
- Keep flows, roles, tools, guide, and `config.yaml` under `control_home`; WRI-010 keeps the live control plane provider-denied and supplies frozen private execution inputs even after `private_home` moves.
- Preserve `.worc/` ignore/exclude handling because the control plane remains in the repo. Add/retain the separate `.worc-io/` ignore and scoped-staging protection.
- Update install, upgrade, preflight, recovery, cleanup, watch/down, status, diagnostics, and config-env-file discovery in the same implementation. Update every tool that documents or reads the in-repo private layout in the same change, including the repository's own `.claude/skills/analyze-task-run` skill and the operator guide.
- Define greenfield adoption clearly: no production DB migration is promised, but install/upgrade must detect an old in-repo private-state layout and refuse ambiguous split-brain use with an actionable message.

## Acceptance criteria

- [ ] The repository contains `.worc` control files and the current `.worc-io` exchange, but no private DB/log/memory/secret/process-control state.
- [ ] The bootstrap order above works before private `.env` loading and cannot be redirected by agent/task/flow content.
- [ ] Every private consumer obtains its path from `layout.private_home`; every control consumer uses `layout.control_home`; no generic `worc_home` alias remains at security-sensitive call sites.
- [ ] An explicit private-home override must be absolute, resolves outside the repository/worktree, and passes symlink/junction/reparse validation on every platform.
- [ ] Relocation changes the default env-file path but never rewrites an explicit operator env-file; either form is independently denied to providers.
- [ ] Repository clones with the same basename receive distinct deterministic defaults; moving a clone has documented identity/adoption behavior.
- [ ] `.worc/` and `.worc-io/` remain ignored and excluded from staging; documentation does not claim the in-repo control plane disappeared.
- [ ] Relocation does not drop the independent provider deny/frozen-bundle protection for live `control_home`.
- [ ] Windows/macOS/Linux install, upgrade, run, recovery, watch/down, logs cleanup, memory, and HITL tests pass with external private state.
- [ ] Security documentation calls relocation defense in depth and does not claim that a moved path is protected without the provider's independently verified policy.

## Verification

- Platform-injected default-path and repository-identity tests, plus native filesystem tests on all three CI OSes.
- Full fake-provider run with external private state, asserting each artifact class lands in the intended root.
- Config schema/upgrade/round-trip tests and old-layout split-brain refusal tests.
- Recovery, watch/down PID/sentinel, logs cleanup, HITL, memory, install, and upgrade tests.
- Symlink/junction/reparse, drive/UNC, spaces/non-ASCII, read-only, and path-normalization cases.

## Out of scope

- Treating topology as provider containment.
- Moving the repository-local editable control plane.
- Cross-task worktrees/concurrency.

## Likely implementation areas

- src/wastech_orchestrator/cli.py
- src/wastech_orchestrator/composition.py
- src/wastech_orchestrator/config/
- src/wastech_orchestrator/providers/artifacts.py
- src/wastech_orchestrator/core/recovery.py and orchestrator.py
- src/wastech_orchestrator/process_control.py and src/wastech_orchestrator/memory/
- .claude/skills/analyze-task-run/
- tests/config/, tests/core/, tests/providers/
- docs/operations.md, docs/configuration.md, docs/how-to.md, and packaged guide
