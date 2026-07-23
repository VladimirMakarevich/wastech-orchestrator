# Relocate private runtime state outside the working tree (deferred — out of scope)

Status: **out of scope / not scheduled** Date: 2026-07-23 Owner: Vladimir Makarevich Origin: shipped as task **WRI-005** (Milestone 2, defense in depth) of the `agent-worc-read-isolation` cluster; that cluster's implementation tasks are complete and its working folder is removed, so this proposal is archived here for traceability. It is **not planned** for implementation now — the orchestrator's private runtime state stays in-repo under `<repo>/.worc` for the time being.

This is a stake-in-the-ground design record, not an active spec. Nothing here overrides the hard invariants in [.agents/rules/](../../../.agents/rules/).

## Context (self-contained — the read-isolation cluster is gone)

The `agent-worc-read-isolation` cluster drew a boundary between three on-disk surfaces the orchestrator uses, so a coding-agent (Claude Code / Codex) launched in the repository working copy can read only what it needs:

- **`control_home` = `<repo>/.worc`** — the operator-editable control plane discovered relative to the repo: `config.yaml`, the packaged `guide/`, the seeded editable `flows/` (+ their role prompts), and `tools/` executables.
- **`private_home`** (today also `<repo>/.worc`) — private runtime state the agent must never read: `.env`, `state.db`, `logs/` + audit, the memory store, security reports, rejected task files, durable HITL/process-control data (PID/sentinel/children), and provider-attempt artifacts.
- **`exchange_root` = `<repo>/.worc-io`** — the redacted, agent-facing exchange holding only the current task's curated inputs (plan, diff, findings, checks input, HITL packet).

The cluster shipped (all landed on `main`): a typed, injected runtime layout naming those three surfaces (so no consumer rebuilds `repo_root / ".worc"`); the two-root private/exchange split with a redaction/path-safety publication boundary; provider process-tree quiescence proof; a frozen, provider-denied control plane and frozen agent-input bundles; Claude sandbox + tool-policy isolation and the Codex permission-profile isolation; and terminal-exchange sealing/restore. Crucially, **`control_home` and `private_home` both resolve to `<repo>/.worc` today** — the cluster made the split a _typed seam_, deliberately leaving the physical move of `private_home` out of tree as this separate, lower-priority, defense-in-depth task.

This proposal is that physical move. It was scoped as **defense in depth, not a containment boundary**: relocating private state reduces its discoverability and blast radius, but the real access control is each provider's independently verified deny policy (Codex profile / supported-host Claude sandbox; native-Windows strict Claude omits Bash). An unsafe provider mode can still read a known external absolute path, so relocation alone must never be sold as protection.

**Why deferred:** the typed-layout seam already lets every consumer read `private_home` from one place, so moving it later is mechanical and non-urgent; the in-repo `.worc` is gitignored and provider-denied, so the residual risk is bounded; and the move carries real cross-platform bootstrap/identity/migration cost (below) for a defense-in-depth-only gain. Revisit if private state in the working copy becomes a concrete problem (e.g. a provider mode that can read arbitrary absolute paths, or an operator requirement to keep the repo free of runtime state).

## Problem

The current `.worc` directory mixes two different responsibilities: the operator-editable control plane (discovered relative to the repository) and private runtime state. Moving all of `.worc` would break bootstrap: the orchestrator needs the repository-local config before it can discover any configured external location, and install/upgrade intentionally seed editable flows and tools into `.worc`. Conversely, leaving private state under the agent `cwd` increases discoverability and blast radius.

Relocation alone is not a hard cross-provider access control. Codex and supported-host Claude policies must deny the resolved external path independently; native-Windows strict Claude operation must continue to omit Bash. An unsafe provider mode can still read a known external absolute path.

## Required outcome

Keep `control_home = <repo>/.worc` for repository-local configuration, guide, flows, and tools. Move `private_home` to a configurable per-repository directory under the platform's user-state/data location. Keep `exchange_root = <repo>/.worc-io`. All consumers use the typed runtime layout and no longer infer private paths from `control_home`.

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
- Keep flows, roles, tools, guide, and `config.yaml` under `control_home`; the frozen-control-plane protection keeps the live control plane provider-denied and supplies frozen private execution inputs even after `private_home` moves.
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
- Cross-task worktrees/concurrency (see [concurrent-task-worktrees.md](concurrent-task-worktrees.md)).

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
