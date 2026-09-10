# Phase 01 — `references:` task field appended to the PR body

- **Status:** ☐
- **Depends on:** none
- **Delivers:** FR-W1 — a task may carry `references:`; worc validates it fail-closed and appends the lines verbatim to the PR body it opens, under `## References`. No tracker semantics enter worc.

## Goal

Give any external producer of tasks one opaque channel into the PR body, so a connector can place its tracker's closing keyword or back-link without worc learning what it means. Moves AC-W1.

## Steps

1. `src/wastech_orchestrator/task/model.py` — add `"references"` to `ALLOWED_TASK_KEYS`; add `references: tuple[str, ...] = ()` to `NormalizedTask` with a comment stating the contract (opaque lines, appended to the PR body, never interpreted).
2. `src/wastech_orchestrator/task/validation_gate.py` — add `ValidationReason.INVALID_REFERENCES`; in Phase A, when the key is present: must be a list of 1..16 strings, each stripped non-empty, ≤ 200 characters, containing no `\n`/`\r`; the existing `scan_frontmatter` already rejects argv-shaped values (leading `-`, `;`, backtick, `|`, `$(`) — do not duplicate it. Populate `NormalizedTask.references`.
3. `src/wastech_orchestrator/core/flow/nodes/publish.py` — in `_publish`, when `will_open_pr` and the task carries references, render `## References\n\n- <line>\n…` and pass it to `git.create_pr(..., references_block=…)`. Not built under a `commit`/`push` cap. Locate how the runner reaches the task (`self._in` inputs) and add the field on that input dataclass rather than reaching into the store.
4. `src/wastech_orchestrator/git_manager.py` — `create_pr` gains `references_block: str | None = None`; write the annotated copy as `_body_with_notice` does (notice on top, references at the bottom — one helper producing `pr-body.md` under the task's artifacts), never rewriting the committed `summary.md`. Falls back to the original body on OSError, as today.
5. `src/wastech_orchestrator/packaged/guide/tasks/…` — document the field: shape, limits, "appended verbatim", the connector use case, and that worc does not interpret it.
6. Tests (see below), `ruff`, `mypy`, `lint-imports`, `pytest`, `python tools/mdlint.py`.

## Files touched

- `src/wastech_orchestrator/task/model.py`
- `src/wastech_orchestrator/task/validation_gate.py`
- `src/wastech_orchestrator/core/flow/nodes/publish.py`
- `src/wastech_orchestrator/git_manager.py`
- `src/wastech_orchestrator/packaged/guide/` (task-fields page)
- `tests/` (gate, publish node, git manager)

## Invariants in play

- The gate stays fail-closed: a malformed `references:` quarantines the task before any branch.
- Task content reaches processes as file content, never argv: the block goes into the body **file** handed to `gh pr create --body-file`, as the summary already does.
- The committed summary is frozen and content-verified; it is not rewritten.
- No tracker syntax, no URL parsing, no keyword recognition in worc.
- Cross-platform: the body copy is written with `newline=""` and UTF-8, as `_body_with_notice` does.

## Tests

- Gate: valid list; absent key; not a list; empty list; empty string; 201 characters; 17 entries; embedded newline; `-flag` (rejected by the injection scan with its own reason).
- Publish + git manager with a recorded `gh` runner: body ends with the block, summary bytes unchanged; with `publish: push` no block is built and no PR is opened; with both a notice and references the notice is first and the references last.

## Docs to sync in this phase

- Packaged guide task-fields page.
- `docs/backlog/tracker-connector/` — tick this phase.
- PR description breadcrumb: the derived task-authoring page on the documentation branch gains the field.

## Acceptance for this phase

- [ ] AC-W1 passes in full.
- [ ] `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports` and `pytest` are green; `python tools/mdlint.py` is green.
