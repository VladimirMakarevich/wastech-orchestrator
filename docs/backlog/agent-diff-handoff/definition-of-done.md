# Definition of Done — Agent diff hand-off

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

- [ ] Every acceptance criterion in [acceptance-criteria.md](acceptance-criteria.md) passes, except the real-run half of AC-N4, which is a recorded follow-up (below).
- [ ] Everything in [out-of-scope.md](out-of-scope.md) stayed out — in particular `current.diff` is byte-identical to what the pre-change code writes for the same tree (a regression test pins it), and no adapter's argv changed for any route.
- [ ] Every blocking question in [questions.md](questions.md) is resolved.
- [ ] A request with none of the new fields renders today's prompt byte-for-byte, so a node that runs before the first edit, and a partial-diff fallback attempt, are provably unaffected.
- [ ] `summarize_diff` (supervisor packet, PR body) and the overview are computed by the one parser in `diff_overview.py`; the PR body of an existing fixture run is unchanged.
- [ ] Every packaged flow whose prompts name `{diff_path}` was checked, and each one either uses the new hand-off or says explicitly that it reads the whole change.
- [ ] The guide pages `packaged/guide/flows/prompt-variables.md` and `packaged/guide/flows/roles.md` describe `{change_overview_path}`, `{diff_base}` and the where-to-start sentence.
- [ ] The Windows / Linux / macOS CI matrix is green, including the native-Windows no-shell case.
- [ ] The PR description carries the breadcrumb: likely affects `worc_architecture.md` (the context hand-off between nodes) and `glossary.md` (change overview, diff base).

## After it lands (follow-up, not a merge gate)

- [ ] After a handful of real `implementation` runs in advanced mode, the problem's measurement is re-run and its result recorded (AC-N4): the share of shell-enabled Claude `review` runs that read the full diff **and** run git, against the 32/32 baseline. A result that did not move is a finding for the role prompts, not a reason to revert.

## When it lands

- [ ] This spec folder leaves `docs/backlog/` (deleted, or archived in one dated batch) and its row is removed from the backlog index — the code, the tests and the history become its record.

## Sign-off

- [ ] The user reviewed the result against [happy-path.md](happy-path.md).
