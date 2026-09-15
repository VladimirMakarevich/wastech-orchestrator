<!-- When is this task TRULY done? Starts from the project's Definition of Done (AGENTS.md) and adds
     the task-specific gates. A checklist you can literally tick before calling it finished. -->

# Definition of Done — {{TASK_TITLE}}

## Project baseline (always)

- [ ] `ruff check .` and `ruff format --check .` pass.
- [ ] `mypy src` passes.
- [ ] `lint-imports` passes (the architectural import boundaries hold).
- [ ] `pytest` passes, and new or changed behavior ships with tests (`.agents/rules/testing.md`); provider, router and pipeline coverage uses deterministic fake CLIs, never the real binaries.
- [ ] The CI-only gates hold: `interrogate src`, `vulture`, `deptry src`.
- [ ] `python tools/mdlint.py` is green, and Markdown edited on this branch is Prettier-formatted (one paragraph per line).
- [ ] Every document that lives on this branch is updated in the same change — `.agents/rules/`, `README.md`, `docs/backlog/`, and the shipped operator-facing copies under `src/wastech_orchestrator/packaged/` (the guide, the flows and role prompts, `config.example.yaml`).
- [ ] The doc impact on the derived pages that live only on the documentation branch is noted in the PR description as a breadcrumb.
- [ ] No secrets in logs, `state.db` or artifacts; only allowlisted env vars reach a process.
- [ ] The hard invariants are intact (core knows no CLI syntax; no delegated publication; fallback only for provider infrastructure classes; the security envelope is not weakened).
- [ ] Commits and the PR carry **no agent-attribution trailer or footer**.

## Task-specific

- [ ] Every acceptance criterion in [acceptance-criteria.md](acceptance-criteria.md) passes.
- [ ] Everything in [out-of-scope.md](out-of-scope.md) stayed out.
- [ ] Every blocking question in [questions.md](questions.md) is resolved.
- [ ] `<task-specific gate — e.g. verified on Windows and on Linux; a real run reaches the new status; the config bump loads an existing installation>`.

## When it lands

- [ ] This spec folder leaves `docs/backlog/` (deleted, or archived in one dated batch) and its row is removed from the backlog index — the code, the tests and the history become its record.

## Sign-off

- [ ] The user reviewed the result against [happy-path.md](happy-path.md).
