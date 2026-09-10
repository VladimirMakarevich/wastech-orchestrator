# Definition of Done — Tracker connector

Two repositories are involved, so the baseline applies twice: this repository's gates for the worc-side phases, and the connector repository's own gates (to be set up in its first phase, mirroring these) for the rest.

## Project baseline (worc-side phases, always)

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

## Connector baseline (connector phases)

- [ ] The connector repository has the same shape of gates from its first commit: lint + format, type check, import-boundary contract (core never imports an adapter), tests on a Windows + Linux matrix, dependency hygiene.
- [ ] Every test that touches `gh` or `worc` drives a fake executable; the real binaries are never launched in CI.
- [ ] The connector's README carries the auto-merge caveat and the "issue text is untrusted" note in plain words.

## Task-specific

- [ ] Every acceptance criterion in [acceptance-criteria.md](acceptance-criteria.md) passes — `AC-W*` here, `AC-C*` in the connector repository.
- [ ] Everything in [out-of-scope.md](out-of-scope.md) stayed out: no code-host adapter in worc, no tracker knowledge in worc, no connector state in `.worc/` or `state.db`, no new worc config key.
- [ ] Every blocking question in [questions.md](questions.md) is resolved (there are none at drafting time; the non-blocking ones have a recorded default).
- [ ] One real end-to-end run is recorded: a labelled issue on a throwaway GitHub repository, `worc watch` and `worc-connect watch` side by side, from label to merged PR to closed issue, with `triage.enabled: false`.
- [ ] `references:` is exercised by that run once phase 06 lands: the PR body ends with `Fixes #<n>` and GitHub closes the issue on merge with `close_on_merge: false`.
- [ ] A run on Windows completes the same cycle against the fake `gh` and fake `worc` (the CI matrix) — a real Windows run is recorded if a host is available.

## When it lands

- [ ] The connector repository exists and carries the connector half of this spec (problem, requirements, design, plan phases 03–07) as its own backlog; this folder shrinks to the worc-side items or leaves `docs/backlog/` entirely once phases 01–02 have merged, and its row is removed from the backlog index — the code, the tests and the history become its record.
- [ ] A "code host adapters" row is added to the backlog index when this folder leaves it, so the other axis is not lost.

## Sign-off

- [ ] The user reviewed the result against [happy-path.md](happy-path.md).
