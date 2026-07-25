---
name: sync-docs
description: After a behavior/CLI/config/architecture change in wastech-orchestrator, bring the docs in sync with the code. Use after implementing a change (the Stop docs-sync gate reminds you), before a commit, or whenever the docs no longer match the code.
---

# sync-docs

Keep the documentation in lockstep with the code. Run this after changing behavior so the same change set updates the affected docs — never leave docs trailing the code.

## Step 0 — establish the scope: which branch are you on?

The repository has two documentation shapes (see [git-workflow.md](../../../.agents/rules/git-workflow.md) §A). Detect which one you are in by **the presence of the derived docs tree**, never by branch name — that stays correct in a worktree and in detached HEAD:

```bash
test -f docs/worc_architecture.md && echo main-scope || echo dev-scope
```

- **`dev-scope`** (no derived `docs/` tree) — sync only what is present on the branch: [.agents/rules/](../../../.agents/rules/), [README.md](../../../README.md), `docs/backlog/`, and everything under `src/wastech_orchestrator/packaged/`. The derived `docs/` documents (`worc_architecture.md`, `configuration.md`, `cookbook.md`, `glossary.md`, `operations.md`, `how-it-works.md`, `how-to.md`, `index.md`, `telegram.md`, `task-authoring.md`, `flow-authoring.md`, `analysis/`, `likec4/`) live on `main` only. **Do not create them here** — a CI guard rejects new `docs/` paths outside `backlog/` and `research/`. Instead do step 3 (the doc-impact note) so the refresh task on `main` has a breadcrumb.
- **`main-scope`** (the derived tree exists) — the full `docs/` refresh, driven by the diff merged from `dev`. Do **not** edit shared files here (`AGENTS.md`, `.agents/rules/`, `.claude/skills/`) — those edits flow through `dev`, or the branches diverge in content and conflict on every merge.

The mapping in step 2 is annotated with the scope each target belongs to: **[dev]** targets are in scope on both branches (they are shared files, edited on `dev`), **[main]** targets only in `main-scope`.

## Steps

1. **See what changed.** `git status --porcelain` and `git diff` (vs `HEAD`) for the working set. Classify each change and decide its documentation impact.
2. **Update the docs that match the change** (only the ones it actually affects, and only those in scope per step 0):

   > **Out of scope — do not touch:** `docs/likec4/` is regenerated weekly via reverse engineering and must not be edited here.

   > **Always also check `src/wastech_orchestrator/packaged/`** — the shipped, operator-facing docs (the `guide/` quickstarts, `config.example.yaml`, the built-in flows and their role prompts) are documentation too, and they are the copy the operator actually reads after `install`. They are routinely forgotten because they live under `src/`. They are present on **every** branch, so this applies in both scopes. For any change below, ask whether the matching packaged file also drifted and update it in the same change.
   - **CLI** (new/changed command or flag) → **[dev]** [README.md](../../../README.md); **[main]** [operations.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/operations.md), [cookbook.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/cookbook.md), and the **Entry points** section of [glossary.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/glossary.md) (add/remove/rename the term there too).
   - **Config schema** (`config.yaml` fields, defaults, validation) → **[dev]** the packaged `src/wastech_orchestrator/packaged/config.example.yaml` (the single example, validated by the round-trip test); **[main]** [configuration.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/configuration.md) and the **Configuration** section of [glossary.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/glossary.md).
   - **Packaged defaults** (built-in flows, role prompts, config templates, or any other file under `src/wastech_orchestrator/packaged/`) → **[dev]** update the affected file(s) there directly when the default behaviour or shipped content changes.
   - **Packaged operator guide docs** (the prose docs under `src/wastech_orchestrator/packaged/guide/` — `guide/README.md`, `guide/best-practices.md`, `guide/decision-guide.md`, `guide/flows/*.md`, `guide/config/*.md`, `guide/tasks/*.md`) → **[dev]** these mirror `docs/` for the operator who reads them after `install`; when a behavior/CLI/config/flow/vocabulary change touches something they describe, update them in lockstep. **Note:** `packaged/guide/` is `.prettierignore`d — match the existing one-paragraph-per-line style by hand and do **not** run Prettier on it.
   - **Architecture / invariants / contracts** → **[dev]** [.agents/rules/architecture.md](../../../.agents/rules/architecture.md); **[main]** [worc_architecture.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/worc_architecture.md).
   - **Flow graph or node vocabulary** (node ids, `task_type` dispatch, `session_scope`, node kinds, `OutputContract`, etc.) → **[main]** [glossary.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/glossary.md) **Flow vocabulary** section.
   - **Provider ids, error classes, or routing contracts** → **[main]** [glossary.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/glossary.md) **Providers** section.
   - **Task status machine or task language fields** (statuses, front-matter keys, lifecycle folders) → **[main]** [glossary.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/glossary.md) **Task language** section.
   - **Renamed or removed terms** (config keys, CLI flags, stage names) → **[main]** [glossary.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/glossary.md) **Legacy and renamed terms** section — add or update the entry there.
   - **Persisted state / schema versions** (config `schema_version`, `state.db` `user_version`, registry `version`) → **[main]** [operations.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/operations.md#upgrading-the-orchestrator) (the current schema versions are documented there).

3. **Leave a doc-impact note (`dev-scope` only).** For every **[main]** target the change would have touched, write one line in the PR description — "touched X, likely affects `configuration.md` / `worc_architecture.md`". That is the input the reverse-engineering task on `main` reads; no new mechanism, just the PR body.
4. **Report deferred work.** State anything you intentionally left for later (tech-debt, a next implementation step, a known gap) in your answer to the user, with the context and where it's referenced — it is the user's call whether it becomes a backlog item under [docs/backlog/](../../../docs/backlog/).
5. **Verify.** Run `/run-checks` (ruff, mypy, pytest) — the two `config.example.yaml` copies must still parse equal, and any doc-embedded examples must still load.

## Rules

- Update docs **in the same change** as the code, not "later". The Stop docs-sync gate (`.claude/hooks/docs_sync_gate.py`) blocks once when `src/` changed without any in-scope doc change; it detects the scope by the same marker file as step 0.
- Don't claim "docs updated" without naming the specific files you touched.
- If a change genuinely has **no** documentation impact (pure internal refactor, test-only), say so explicitly — that satisfies the rule; do not invent doc churn.
- Match the surrounding doc style; keep edits minimal and accurate. Don't introduce broken links. Link a `main`-only document by its absolute `blob/main/` URL, never by a relative path — the relative path is dangling on `dev`.
