# Phase 06 — Packaged prompts and guide

- **Status:** ☐
- **Depends on:** 05
- **Delivers:** the packaged flows use the new hand-off: the `implementation` roles start from the overview (review), from findings (fixing) and from per-file diffs (documentation); the content critics say explicitly that they read the whole change; the operator guide explains it; one fake-CLI run proves the end-to-end prompt.

## Goal

Implement FR-8 and D-6 in the shipped prompts and close the docs. Moves toward AC-8, AC-N4 (size half) and the DoD's packaged-flow and guide items.

## Steps

1. `packaged/flows/implementation/review.md`: replace "Review the current diff" with start-from-the-overview guidance in a `{?change_overview_path}` block; in the existing `{?subtask_spec_path}` block, judge the since-last-commit section; keep the rule that a cumulative diff may include prior-task files.
2. `packaged/flows/implementation/fixing.md` and `fixing.continue.md`: start from the review findings and the failed check log; use the overview to locate files; open the full diff only when a finding cannot be placed without it.
3. `packaged/flows/implementation/documentation.md`: replace "The diff showing exactly what changed is at `{diff_path}`" with the overview plus per-file diffs of what it documents, keeping `{diff_path}` as the fallback in the same block.
4. `packaged/flows/blog_article/tone_style.md`, `blog_article_revise/tone_style.md`, `content_chapter/story_critic.md`, `content_translate/en_critic.md`: one clause each — read the whole change at `{diff_path}`; the overview only sizes it.
5. Check every other packaged role prompt for `{diff_path}` or a "the diff" instruction and record the outcome in the PR.
6. `packaged/guide/flows/roles.md`: what a role is handed about the change (overview, base, full diff) and how the where-to-start sentence is chosen; when to name `{diff_path}` explicitly.
7. Add one fake-CLI integration test: an `implementation` run through `review` asserting the received prompt carries the overview path, the base and the sentence, and that the overview of a large fixture diff stays under 20 KB.

## Files touched

- `src/wastech_orchestrator/packaged/flows/implementation/review.md`
- `src/wastech_orchestrator/packaged/flows/implementation/fixing.md`
- `src/wastech_orchestrator/packaged/flows/implementation/fixing.continue.md`
- `src/wastech_orchestrator/packaged/flows/implementation/documentation.md`
- `src/wastech_orchestrator/packaged/flows/blog_article/tone_style.md`
- `src/wastech_orchestrator/packaged/flows/blog_article_revise/tone_style.md`
- `src/wastech_orchestrator/packaged/flows/content_chapter/story_critic.md`
- `src/wastech_orchestrator/packaged/flows/content_translate/en_critic.md`
- `src/wastech_orchestrator/packaged/guide/flows/roles.md`
- `tests/core/` (packaged-prompt rendering), `tests/fakes/` + an integration test

## Invariants in play

- Role prompts reference only allowlisted variables, optional ones inside `{?name}…{/name}` blocks.
- No prompt gives any node a mandate to commit, push or open a PR.

## Tests

- Render each changed prompt with and without an overview; assert the clauses AC-8 names.
- `worc validate-flow` on every packaged flow: no new warnings.
- The fake-CLI integration run described in step 7.

## Docs to sync in this phase

- `src/wastech_orchestrator/packaged/guide/flows/roles.md` (step 6); re-check `prompt-variables.md` from phase 05 against the final prompts.
- `docs/backlog/agent-diff-handoff/` — flip the plan statuses; the folder leaves the queue once merged.
- PR description breadcrumb: likely affects `worc_architecture.md` and `glossary.md` on the documentation branch.

## Acceptance for this phase

- [ ] AC-8 holds; the size half of AC-N4 holds in the integration test.
- [ ] `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports`, `pytest` and `python tools/mdlint.py` are green.
