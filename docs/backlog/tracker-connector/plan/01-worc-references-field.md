# Phase 01 — `references:` task field appended to the PR body

- **Status:** ☐
- **Depends on:** none
- **Delivers:** FR-W1 — a task may carry `references:`; worc validates it fail-closed and appends the lines verbatim to the PR body it opens, under `## References`. No tracker semantics enter worc.

## Goal

Give any external producer of tasks one opaque channel into the PR body, so a connector can place its tracker's closing keyword or back-link without worc learning what it means. Moves AC-W1.

## Steps

1. `src/wastech_orchestrator/task/model.py` — add `"references"` to `ALLOWED_TASK_KEYS`; add `references: tuple[str, ...] = ()` to `NormalizedTask` with a comment stating the contract (opaque lines, appended to the PR body, never interpreted).
2. `src/wastech_orchestrator/task/validation_gate.py` — add `ValidationReason.INVALID_REFERENCES`; in `_check_field_types` (the `depends_on` / `subtasks` blocks are the template), when the key is present: must be a list of 1..16 strings, each stripped non-empty, ≤ 200 characters, containing no `\n`/`\r`. `_check_field_types` runs **before** `scan_frontmatter` in `_validate_fields`, so these shape faults report `INVALID_REFERENCES`, while a leading `-`, `;`, backtick, `|`, `$(` or a forbidden flag shape in an entry is left to the scan (it already recurses into lists) and reports `INJECTION_SUSPECTED` — do not duplicate those tokens. Populate `NormalizedTask.references` in the `NormalizedTask(...)` construction at the end of `_validate_fields`.
3. `src/wastech_orchestrator/core/flow/nodes/base.py` + `core/flow/wiring.py` — `NodeInputs` gains `references: tuple[str, ...] = ()`, filled from `p.task.references` in `build_node_inputs` (it already reads `p.task.task_type` and `p.task.contacts`). `src/wastech_orchestrator/core/flow/nodes/publish.py` — in `_publish`, when `will_open_pr` and `self._in.references` is non-empty, render `## References\n\n- <line>\n…` and pass it to `git.create_pr(..., references_block=…)`. Not built under a `commit`/`push` cap (the existing `scope` early returns already sit before `create_pr`).
4. `src/wastech_orchestrator/git_manager.py` — `create_pr` gains `references_block: str | None = None`, applied at the same point as `notice` (before `_find_open_pr`), so both the `gh pr create` path and the reused-PR path (`_append_reused_pr_body` reads the same `body_path`) carry it. One helper replaces `_body_with_notice`: notice on top, original body, references at the bottom, written as `pr-body.md` under the task's artifacts with `newline=""`; the `summary.md` finalize wrote is never rewritten. Falls back to the original body on OSError, as today.
5. `src/wastech_orchestrator/packaged/guide/README.md` ("Front-matter fields" table) and `packaged/guide/tasks/task-rich.md` (the all-fields example) — document the field: shape, limits, "appended verbatim under `## References`", the connector use case, and that worc does not interpret it.
6. Tests (see below), `ruff`, `mypy`, `lint-imports`, `pytest`, `python tools/mdlint.py`.

## Files touched

- `src/wastech_orchestrator/task/model.py`
- `src/wastech_orchestrator/task/validation_gate.py`
- `src/wastech_orchestrator/core/flow/nodes/base.py`, `src/wastech_orchestrator/core/flow/wiring.py`
- `src/wastech_orchestrator/core/flow/nodes/publish.py`
- `src/wastech_orchestrator/git_manager.py`
- `src/wastech_orchestrator/packaged/guide/README.md`, `src/wastech_orchestrator/packaged/guide/tasks/task-rich.md`
- `tests/task/test_validation_gate.py`, `tests/core/test_flow_node_runners.py` (its fake git already records `pr_notice` — record the block the same way), `tests/git/test_git_manager.py`

## Invariants in play

- The gate stays fail-closed: a malformed `references:` quarantines the task before any branch.
- Task content reaches processes as file content, never argv: the block goes into the body **file** handed to `gh pr create --body-file`, as the summary already does.
- The committed summary is frozen and content-verified; it is not rewritten.
- No tracker syntax, no URL parsing, no keyword recognition in worc.
- Cross-platform: the body copy is written with `newline=""` and UTF-8, as `_body_with_notice` does.

## Tests

- Gate: valid list; absent key; not a list; empty list; empty string; 201 characters; 17 entries; embedded newline (all `INVALID_REFERENCES`); `-flag` and `a; b` (`INJECTION_SUSPECTED` from the scan, which runs after the shape check).
- Publish + git manager with a recorded `gh` runner: body ends with the block, summary bytes unchanged; with `publish: push` no block is built and no PR is opened; with both a notice and references the notice is first and the references last; with an open PR already on the head, the appended body carries the block.

## Docs to sync in this phase

- `packaged/guide/README.md` front-matter table and `packaged/guide/tasks/task-rich.md`.
- `docs/backlog/tracker-connector/` — tick this phase.
- PR description breadcrumb: the derived task-authoring page on the documentation branch gains the field.

## Acceptance for this phase

- [ ] AC-W1 passes in full.
- [ ] `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports` and `pytest` are green; `python tools/mdlint.py` is green.
