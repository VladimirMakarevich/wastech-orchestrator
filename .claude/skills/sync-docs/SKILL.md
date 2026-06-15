---
name: sync-docs
description: After a behavior/CLI/config/architecture change in wastech-orchestrator, bring the docs (including the docs/functional map and the docs/architecture C4 model), CHANGELOG, and follow-ups tracker in sync with the code. Use after implementing a change (the Stop docs-sync gate reminds you), before a commit, or whenever the docs no longer match the code.
---

# sync-docs

Keep the documentation in lockstep with the code. Run this after changing behavior so the same change
set updates the affected docs and records any deferred work — never leave docs trailing the code.

## Steps

1. **See what changed.** `git status --porcelain` and `git diff` (vs `HEAD`) for the working set.
   Classify each change and decide its documentation impact.
2. **Update the docs that match the change** (only the ones it actually affects):
   - **CLI** (new/changed command or flag) → [README.md](../../../README.md),
     [docs/operations.md](../../../docs/operations.md), [docs/cookbook.md](../../../docs/cookbook.md).
   - **Config schema** (`config.yaml` fields, defaults, validation) → [docs/configuration.md](../../../docs/configuration.md),
     **both** `config.example.yaml` copies (repo-root and `src/wastech_orchestrator/templates/`, kept
     identical — the round-trip test compares them), and §11 of the canonical spec.
   - **Architecture / invariants / contracts** → [docs/rules/architecture.md](../../../docs/rules/architecture.md),
     the canonical spec (`docs/implementation_stages/00_orchestrator_final_plan.md`), and
     [docs/codex_git_orchestrator_architecture.md](../../../docs/codex_git_orchestrator_architecture.md).
   - **Persisted state / schema versions** (config `schema_version`, `state.db` `user_version`,
     registry `version`) → the spec's "Versioning and compatibility" section (§22).
   - **Functional map (`docs/functional/`)** — when a block's boundary, contract (signatures, enums,
     statuses, error codes), entry point, integration, data store, or an internal check/branch changes,
     or a responsibility appears/disappears → update the affected `blocks/<id>-*.md` (and its
     cross-links), `block-registry.md`, `index.md`, and `system-flows.md` per
     [docs/functional/CONVENTIONS.md](../../../docs/functional/CONVENTIONS.md) — evidence-based,
     `file:line` anchors, and keep the mermaid diagrams in sync with the code. When a **pipeline
     stage's** order, optionality, agent, or the ping-pong changes, also update the flow docs
     `docs/functional/flows/coding/` (the `S01`–`S08` stage docs + `flows/coding/index.md`).
   - **Architecture-as-Code (`docs/architecture/*.likec4`)** — when the high-level structure changes
     (a block-component added/removed, a cross-block relationship changed, a new external system or
     data store) → update the LikeC4 model (elements, relationships, views) to match the block registry.
     It is hand-authored (no `file:line`); validate with `likec4 dev`. See
     [docs/architecture/README.md](../../../docs/architecture/README.md).
   - **Any behavior change** → a `[Unreleased]` entry in [CHANGELOG.md](../../../CHANGELOG.md)
     (Keep-a-Changelog: Added / Changed / Fixed / Removed).
3. **Record deferred work.** Append anything you intentionally left for later (tech-debt, a next
   implementation step, a known gap) to [docs/backlog/follow_ups.md](../../../docs/backlog/follow_ups.md)
   with the date, context, and where it's referenced. If it's a product feature, cross-link it to
   `docs/backlog/product_backlog.md` instead of duplicating.
4. **Verify.** Run `/run-checks` (ruff, mypy, pytest) — the two `config.example.yaml` copies must
   still parse equal, and any doc-embedded examples must still load. If you touched `docs/functional/`,
   confirm its links resolve (see the link-check snippet in `docs/functional/CONVENTIONS.md`); if you
   touched the C4 model, run `likec4 dev docs/architecture` once so LikeC4 validates it.

## Rules

- Update docs **in the same change** as the code, not "later". The Stop docs-sync gate
  (`.claude/hooks/docs_sync_gate.py`) blocks once when `src/` changed without any `docs/`/`CHANGELOG.md` change.
- Don't claim "docs updated" without naming the specific files you touched.
- If a change genuinely has **no** documentation impact (pure internal refactor, test-only), say so
  explicitly — that satisfies the rule; do not invent doc churn.
- Match the surrounding doc style; keep edits minimal and accurate. Don't introduce broken links.
