# Implementation follow-ups

Append-only log of **implementation** next-steps and tech-debt discovered while building — distinct
from [product_backlog.md](product_backlog.md), which tracks *product features*. Use this for "we
deferred X; here's the hook and the context" items. Record new ones via `/sync-docs` as you create
them (date them absolute, link where they're referenced, mark status). Move an item to `done` (don't
delete) when addressed.

| Date | Item | Context & next step | Referenced in | Status |
|---|---|---|---|---|
| 2026-06-12 | **Schema migration runner** | Config `schema_version` and `state.db` `user_version` are stamped and gated (refuse-newer; adopt legacy `0`), but there is **no migration runner** for the `< current` case. When a backward-incompatible schema change first lands, add a versioned migration step (config and/or DB) and an `upgrade`/`doctor` command that backs up and migrates in place. The version gates are the hooks. | spec §22 "Versioning and compatibility"; [CHANGELOG.md](../../CHANGELOG.md); `config/loader.py` `_check_schema_version`; `state_store.py` `_enforce_schema_version` | backlog / not scheduled |
| 2026-06-12 | **Broken canonical-spec links** | The canonical spec was moved from `docs/orchestrator_final_plan.md` to `docs/implementation_stages/00_orchestrator_final_plan.md` (uncommitted docs reorg), but [CLAUDE.md](../../CLAUDE.md), [AGENTS.md](../../AGENTS.md), [README.md](../../README.md) and several docs still link the old path. Decide the final location, then fix the links repo-wide (or restore the old path). | CLAUDE.md, AGENTS.md, README.md, docs/** | candidate |
| 2026-06-12 | **Git-native pre-commit hook** | The Stop docs-sync gate (`.claude/hooks/docs_sync_gate.py`) only covers turns inside Claude Code. For commits made outside the agent, add a real `pre-commit` hook (tracked script + `core.hooksPath`) that enforces the same "code changed ⇒ docs/CHANGELOG changed" rule. | `.claude/hooks/docs_sync_gate.py`; [docs/rules/git-workflow.md](../rules/git-workflow.md) | candidate |
| 2026-06-12 | **Publish to (Test)PyPI** | Distribution is currently `pipx`/`pip` from a Git tag. To allow `pipx install --pre wastech-orchestrator`, add a publish step to the release workflow using `pypa/gh-action-pypi-publish` with **trusted publishing (OIDC, no tokens)** — TestPyPI for pre-releases first. | [.github/workflows/release.yml](../../.github/workflows/release.yml); [docs/RELEASING.md](../RELEASING.md) | candidate |
| 2026-06-12 | **CI test gate on PRs/pushes** | Only a *release* workflow exists (runs on `v*` tags). Add a `ci.yml` running ruff/mypy/pytest on pull requests and pushes to `main`, so regressions are caught before a release tag. | [.github/workflows/release.yml](../../.github/workflows/release.yml) | candidate |
