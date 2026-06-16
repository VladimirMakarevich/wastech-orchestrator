---
id: task-pr-title-override
title: "Add an optional pr_title task field to override the generated PR title"
refined: true          # detailed enough to plan directly — skip refinement
decompose: false       # one coherent change — single branch, single PR
contacts:
  - "@t_i_2_3"
model: claude-sonnet-4-6    # task-wide default model for stages not overridden below
reasoning: max
stages:
  planning:
    model: claude-opus-4-8
    reasoning: high
  review:
    model: codex
    reasoning: high
---

## Description

Today the orchestrator derives the pull-request title from the task `title`: at the publishing
stage it calls `GitManager.create_pr(..., title=p.task.title)`
([orchestrator.py](src/wastech_orchestrator/core/orchestrator.py), the `_publish` step), and
`create_pr` passes that straight to `gh pr create --title`
([git_manager.py](src/wastech_orchestrator/git_manager.py)). There is no way to make the PR title
differ from the task title.

Add a new **optional** front-matter field **`pr_title`** (type `string | null`) that lets a task
state the PR title explicitly:

- When `pr_title` is present and non-empty, the orchestrator uses it **verbatim** as the PR title.
- When `pr_title` is absent, `null`, or empty/whitespace-only, behavior is **unchanged** — the PR
  title is the task `title`.

The override affects **only the PR title**. It must not change the branch name
(`agent/<task-id>-<slug>`, where the slug is derived from `title`), the commit message
(`feat(<id>): <title>`), the summary, or any report — those keep deriving from `title` exactly as
they do now.

The field threads through the existing task schema and is consumed only at publishing:

1. Add `pr_title` to `ALLOWED_TASK_KEYS` and to the `NormalizedTask` dataclass
   ([task/model.py](src/wastech_orchestrator/task/model.py)), defaulting to `None`.
2. Populate it in the parser / validation gate
   ([task/validation_gate.py](src/wastech_orchestrator/task/validation_gate.py)), normalizing
   empty/whitespace to `None` (mirror how `model`/`reasoning` use `frontmatter.get(...) or None`),
   and add a type check (`pr_title must be a string`) alongside the other field-type checks.
3. At the publishing stage, use `p.task.pr_title` for the PR title when set, otherwise fall back to
   `p.task.title`. This is the only consumption site — keep the change to the `title=` argument of
   the `create_pr` call.

`pr_title` is also accepted as a key in JSON tasks (it flows through the same front-matter map).

For a decomposed task (`decompose: true`) the pipeline still produces a single PR at the end, so
`pr_title`, if set on the parent task, applies to that one PR.

## Acceptance criteria

- [ ] A task with `pr_title: "Custom release title"` opens a PR whose title is exactly
      `Custom release title` (verified via the `title=` argument passed to `create_pr` /
      `gh pr create --title`), while the task `title` is something different.
- [ ] A task with **no** `pr_title` (or `pr_title: null`, or an empty/whitespace value) opens a PR
      whose title equals the task `title` — i.e. current behavior is unchanged.
- [ ] With `pr_title` set, the branch name (`agent/<id>-<slug>`), the commit message
      (`feat(<id>): <title>`), and the summary still derive from `title`, not from `pr_title`.
- [ ] The validation gate rejects a flag-shaped `pr_title` (e.g. a value starting with `--`) with
      reason `injection_suspected`, consistent with how the existing front-matter scan treats
      `title`.
- [ ] A non-string `pr_title` (e.g. a number or a list) is rejected with `invalid_field_type`.
- [ ] `pr_title` is accepted in a `.json` task and behaves identically to the Markdown front-matter
      form.
- [ ] Unit tests cover: override present, override absent/empty (fallback to `title`), flag-shaped
      rejection, and the wrong-type rejection.

## Constraints

- **Only the orchestrator opens PRs** — keep all PR/title logic in the core/`GitManager`; do not move
  any of it into a provider (architecture invariant). Providers must not learn about `pr_title`.
- Do **not** change the branch-slug derivation, the commit-message format, or the summary — the
  override is strictly the PR title.
- **Do not weaken the security policy.** `pr_title` must be passed as an argument-list value (no
  shell interpolation), and must remain subject to the existing front-matter injection scan
  (`scan_frontmatter`); add no path that lets front matter inject CLI flags or bypass approvals.
- No secrets in the field; no new runtime dependencies.
- The canonical field name is **`pr_title`** — do not invent an alternative.
- Update the docs and `CHANGELOG.md` `[Unreleased]` entry **in the same change** (use `/sync-docs`):
  the front-matter field tables in `docs/task-authoring.md` and the authoring docs under
  `worc/` (`README.md` table, and an example if appropriate), plus `docs/configuration.md` only if a
  PR-title default is referenced there. Record any deferred work in
  [docs/backlog/follow_ups.md](docs/backlog/follow_ups.md).
- Add/update tests with the behavior, per the project testing rules; run `ruff`, `mypy`, `pytest`
  before finishing.
