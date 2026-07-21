# WRI-005 — Relocate the private home outside the agent's working tree

**Status:** open **Phase:** 2 (hard isolation) **Source:** [decision record](README.md); shares the runtime-home placement question with the [worktree decision record](../archive/concurrent-task-worktrees.md) **Dependencies:** WRI-004 (and WRI-001)

## Problem

While the private home lives inside the agent's `cwd`, no CLI flag can keep Codex (broad reads) or a `Bash`-capable Claude from reaching it — the Phase-1 denies are tool-scoped and best-effort. Only topology removes the home from the agent's reachable project tree, which is what turns the Phase-1 hygiene into a real cross-provider guarantee (together with WRI-006 for Codex kernel-level enforcement).

## Required outcome

The private home (`state.db`, `logs/`, `flows/`, the `memory/` store, `security-reports/`, `.env`) lives **outside** the agent's working tree; the only in-repo footprint is the gitignored exchange (`.worc-io/`). The out-of-tree location is configurable and cross-platform. This resolves, for the single-task case, the runtime-home placement question also raised by the worktree record.

## In scope

- Choose and implement an out-of-tree default location for the private home (e.g. a per-repo directory under a user data dir), overridable in config, resolved through the WRI-004 seam.
- Update git-ignore handling: with the home no longer inside the repo, the `.git/info/exclude` / root-`.gitignore` handling for `.worc/` changes; the exchange keeps its own ignore + scoped-staging exclusion.
- Update recovery, install, and upgrade paths, and any code that assumes `<repo>/.worc`.
- Cross-platform path handling (`pathlib` + `as_posix()` for stored/compared/displayed strings).

## Acceptance criteria

- [ ] With the home relocated, the repo working tree contains no private-home directory; only the gitignored exchange remains.
- [ ] All orchestrator functions (state, logs, memory, publish, recovery) work with the out-of-tree home on Windows, Linux and macOS.
- [ ] Install / upgrade / recovery updated; no code assumes `<repo>/.worc`.
- [ ] The exchange remains gitignored and never staged; the target repo's tracked `.gitignore` no longer needs a `.worc/` line when the home is external (behavior documented).
- [ ] Docs (operations, configuration, how-to) updated; `/sync-docs` clean.

## Verification

- Cross-platform path tests for the relocated home.
- A full run with a relocated home (fake CLIs), asserting the repo tree has no private-home dir and all artifacts resolve.
- Recovery tests reconciling state with an external home; install/upgrade tests.

## Out of scope

- The Codex OS-sandbox read-deny profile (WRI-006) — relocation alone is strong obscurity + removal from `cwd`, not a kernel guarantee against a known absolute path.
- Concurrency / worktrees (tracked separately in the [worktree decision record](../archive/concurrent-task-worktrees.md)).

## Likely implementation areas

- src/wastech_orchestrator/cli.py
- src/wastech_orchestrator/composition.py
- src/wastech_orchestrator/git_manager.py
- src/wastech_orchestrator/core/recovery.py
- src/wastech_orchestrator/config
- docs/operations.md, docs/configuration.md, docs/how-to.md
