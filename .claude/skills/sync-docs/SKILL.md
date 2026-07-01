---
name: sync-docs
description: After a behavior/CLI/config/architecture change in wastech-orchestrator, bring the docs and the follow-ups tracker in sync with the code. Use after implementing a change (the Stop docs-sync gate reminds you), before a commit, or whenever the docs no longer match the code.
---

# sync-docs

Keep the documentation in lockstep with the code. Run this after changing behavior so the same change set updates the affected docs and records any deferred work — never leave docs trailing the code.

## Steps

1. **See what changed.** `git status --porcelain` and `git diff` (vs `HEAD`) for the working set. Classify each change and decide its documentation impact.
2. **Update the docs that match the change** (only the ones it actually affects):

   > **Out of scope — do not touch:** `docs/functional/` and `docs/likec4/` are regenerated weekly via reverse engineering and must not be edited here.
   - **CLI** (new/changed command or flag) → [README.md](../../../README.md), [docs/operations.md](../../../docs/operations.md), [docs/cookbook.md](../../../docs/cookbook.md), and the **Entry points** section of [docs/glossary.md](../../../docs/glossary.md) (add/remove/rename the term there too).
   - **Config schema** (`config.yaml` fields, defaults, validation) → [docs/configuration.md](../../../docs/configuration.md), the packaged `src/wastech_orchestrator/packaged/config.example.yaml` (the single example, validated by the round-trip test), and the **Configuration** section of [docs/glossary.md](../../../docs/glossary.md).
   - **Packaged defaults** (built-in flows, role prompts, config templates, or any other file under `src/wastech_orchestrator/packaged/`) → update the affected file(s) there directly when the default behaviour or shipped content changes.
   - **Architecture / invariants / contracts** → [.agents/rules/architecture.md](../../../.agents/rules/architecture.md) and [docs/worc_architecture.md](../../../docs/worc_architecture.md).
   - **Flow graph or node vocabulary** (node ids, `task_type` dispatch, `session_scope`, node kinds, `OutputContract`, etc.) → [docs/glossary.md](../../../docs/glossary.md) **Flow vocabulary** section.
   - **Provider ids, error classes, or routing contracts** → [docs/glossary.md](../../../docs/glossary.md) **Providers** section.
   - **Task status machine or task language fields** (statuses, front-matter keys, lifecycle folders) → [docs/glossary.md](../../../docs/glossary.md) **Task language** section.
   - **Renamed or removed terms** (config keys, CLI flags, stage names) → [docs/glossary.md](../../../docs/glossary.md) **Legacy and renamed terms** section — add or update the entry there.
   - **Persisted state / schema versions** (config `schema_version`, `state.db` `user_version`, registry `version`) → [docs/operations.md](../../../docs/operations.md#upgrading-the-orchestrator) (the current schema versions are documented there).
   - **Any behavior change** → append to [docs/backlog/follow_ups.md](../../../docs/backlog/follow_ups.md) when the change defers work.

3. **Record deferred work.** Append anything you intentionally left for later (tech-debt, a next implementation step, a known gap) to [docs/backlog/follow_ups.md](../../../docs/backlog/follow_ups.md) with the date, context, and where it's referenced. If it's a product feature, cross-link it to `docs/backlog/product_backlog.md` instead of duplicating.
4. **Verify.** Run `/run-checks` (ruff, mypy, pytest) — the two `config.example.yaml` copies must still parse equal, and any doc-embedded examples must still load.

## Rules

- Update docs **in the same change** as the code, not "later". The Stop docs-sync gate (`.claude/hooks/docs_sync_gate.py`) blocks once when `src/` changed without any `docs/`/`.agents/` change.
- Don't claim "docs updated" without naming the specific files you touched.
- If a change genuinely has **no** documentation impact (pure internal refactor, test-only), say so explicitly — that satisfies the rule; do not invent doc churn.
- Match the surrounding doc style; keep edits minimal and accurate. Don't introduce broken links.
