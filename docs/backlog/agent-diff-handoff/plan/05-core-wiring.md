# Phase 05 — Core wiring

- **Status:** ☑
- **Depends on:** 02, 03, 04
- **Delivers:** the one behavior change — every agent and evaluator request after the first edit carries the overview, the base and the where-to-start sentence chosen for its route; flows can name `{change_overview_path}` and `{diff_base}`; a resumed task hands off the same thing.

## Goal

Wire D-3, D-4, D-5 and D-6 through the node runners. Moves toward AC-3, AC-4, AC-5, AC-9, AC-E2, AC-E6, AC-N5, AC-N6.

## Steps

1. Add `change_overview_path` and `diff_base` to `NodeInputs` (`src/wastech_orchestrator/core/flow/nodes/base.py`).
2. In `core/flow/nodes/agent.py` `_apply_post_edit_guard` and `_reconsider`, publish the private overview with `publish_file` (relpath `change-overview.txt`, `prompt_secrets`) beside the diff and set both inputs from the `CurrentDiff` result.
3. Create `src/wastech_orchestrator/core/flow/change_guidance.py`: `change_guidance(*, has_shell: bool, decomposed: bool) -> str`, the two sentences from design "Provider & prompt surface" plus the decomposed clause naming the since-last-commit section; the shell variant names only `git diff <base> -- <path>`, says to quote paths with spaces and not to inspect `[denied]` paths.
4. In `agent.py` and `core/flow/nodes/evaluator.py` `_build_request`, when `diff_path` is set: pass `change_overview_path`, `diff_base`, and `change_guidance(has_shell=router.route_always_grants_shell(...), decomposed=ctx.subtask_order is not None)`.
5. Add `change_overview_path` and `diff_base` to `build_path_context` (`core/flow/context_paths.py`) and to `ALLOWED_PROMPT_VARS` (`core/prompts.py`); confirm `valid_prompt_vars` (`core/flow/prompt_vars.py`) picks them up and `worc validate-flow` does not warn on them.
6. In `core/orchestrator.py` `_restore_engine_inputs`, republish the private `change-overview.txt` when present and restore `diff_base` from its header line.

## Files touched

- `src/wastech_orchestrator/core/flow/nodes/base.py`
- `src/wastech_orchestrator/core/flow/nodes/agent.py`
- `src/wastech_orchestrator/core/flow/nodes/evaluator.py`
- `src/wastech_orchestrator/core/flow/change_guidance.py` (new)
- `src/wastech_orchestrator/core/flow/context_paths.py`
- `src/wastech_orchestrator/core/prompts.py`
- `src/wastech_orchestrator/core/orchestrator.py`
- `tests/core/test_flow_node_runners.py`
- `tests/core/test_orchestrator.py`
- `tests/core/` (prompt rendering, guidance)

## Invariants in play

- The core asks the router for capability and names only `git` verbs; it imports no concrete adapter (`lint-imports`).
- Task content still reaches the provider by path only; `{diff_base}` is metadata, not content.
- No capability granted: the sentence describes the shell a node already has.

## Tests

- Before any edit: no new field set, footer unchanged (AC-4).
- After `implementation`: `review`, `fixing`, `documentation` requests carry all three; `request.json` and the prompt audit show them (AC-3, AC-N5).
- Codex-only route → shell sentence; Claude `read-only` and mixed routes → no-shell sentence (AC-5); native-Windows shell answer → no-shell sentence.
- A decomposed subtask gets the decomposed clause.
- Reconsider pass rewrites and republishes both files (AC-E6).
- Resume after `testing`: the resumed `review` sees the same overview content and `diff_base` (AC-E2).
- A fixture flow using `{?change_overview_path}…{/change_overview_path}` renders empty before the edit and filled after, with no validator warning (AC-9).

## Docs to sync in this phase

- `src/wastech_orchestrator/packaged/guide/flows/prompt-variables.md` — two new rows (populated by agent and evaluator; empty until the first workspace-write edit) and a sentence on the footer's where-to-start line.

## Acceptance for this phase

- [ ] AC-3, AC-4, AC-5, AC-9, AC-E2, AC-E6, AC-N5, AC-N6 hold.
- [ ] `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports`, `pytest` and `python tools/mdlint.py` are green.
