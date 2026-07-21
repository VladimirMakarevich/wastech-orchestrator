# WRI-004 — Centralize the `.worc` home literal into one injectable seam

**Status:** open **Phase:** 2 (hard isolation) — prerequisite **Source:** [decision record](README.md), [follow_ups.md](follow_ups.md) **Dependencies:** WRI-001

## Problem

The `.worc` home is a hardcoded literal duplicated across the codebase. The two named constants — `WORC_HOME` in `cli.py` and `_WORC_HOME` in `core/orchestrator.py` — are already tracked as tech debt in [follow_ups.md](follow_ups.md), but they understate the problem: at least ~8 further sites hardcode the `.worc` string independently rather than referencing a constant. The load-bearing ones (each of which assumes `<repo>/.worc`):

- `memory/paths.py` — `MemoryLayout.for_repo` builds `Path(repo_root) / ".worc"` itself (the memory store's home, wholly independent of both constants). **If WRI-005 relocates the home but misses this, the memory store stays inside the repo.**
- `git_manager.py` — `RUNTIME_EXCLUDED_DIRS`, the `RUNTIME_GITIGNORE_LINES` `.worc/` line, and the `.worc/state.db` `check-ignore` probes (×2).
- `core/flow/output_policy.py` — `_PRIVATE_REPORT_DIR = ".worc/security-reports"`.
- `config/validation.py` — the `== ".worc"` / `startswith(".worc/")` guard.
- `config/loader.py` and `install/config_writer.py` — the quarantine default `./.worc/tasks/rejected`.

Phase-2 relocation (WRI-005) needs a single, injectable private-home path; these scattered literals make that unsafe.

## Required outcome

One source of truth for the private-home location, resolved through the composition/config seam, so the home can later be pointed outside the working tree without editing call sites. No behavior change in this task — the home stays at `<repo>/.worc/`; this only builds the seam.

## In scope

- Collapse the duplicated `.worc` literal into a single constant/config-resolved value.
- Thread the private-home path through composition so no consumer assumes `<repo>/.worc` — explicitly including `memory/paths.py`, `git_manager.py`, `output_policy.py`, `config/validation.py`, `config/loader.py`, and `install/config_writer.py`, not just the two named constants.
- Leave the default path exactly as today (`<repo>/.worc/`).

## Acceptance criteria

- [ ] A single source resolves the private-home path; the `WORC_HOME` / `_WORC_HOME` duplication is removed.
- [ ] All consumers obtain the home through the seam — memory store, git-ignore/exclusions, security-reports dir, config validation/loader, and install quarantine included; no path-construction site still hardcodes `.worc` (guard with grep / an import-linter or unit check; docstrings and packaged assets excepted).
- [ ] No functional change — the resolved default path is unchanged; the full suite is green.
- [ ] The corresponding [follow_ups.md](follow_ups.md) entry is closed/updated.

## Verification

- A test or lint asserting there is one definition of the home literal.
- Full suite green with the default path unchanged.

## Out of scope

- Actually moving the home outside the tree (WRI-005).
- Any read-deny mechanism.

## Likely implementation areas

- src/wastech_orchestrator/cli.py
- src/wastech_orchestrator/core/orchestrator.py
- src/wastech_orchestrator/composition.py
- src/wastech_orchestrator/config
- docs/backlog/follow_ups.md (close the duplicate-literal entry)
