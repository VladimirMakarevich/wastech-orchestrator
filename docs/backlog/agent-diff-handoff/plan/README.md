# Implementation plan — Agent diff hand-off

Builds [design.md](../design.md) toward [acceptance-criteria.md](../acceptance-criteria.md); the finish line is [definition-of-done.md](../definition-of-done.md).

## Strategy

Bottom-up, so every phase lands inert until the one that wires it in. The pure parser (01) and the shell-capability question (02) have no callers that change behavior; the Git Manager then writes the overview (03) while nothing reads it; the provider-side request fields and footer (04) render nothing until the core sets them; the core wiring (05) is the single phase that changes what an agent is handed; the packaged prompts and guide (06) then use it. The critical path is 01 → 03 → 05 → 06. The biggest risk is in 03: the overview must describe exactly what `current.diff` describes — same base, same paths, same moment — while `current.diff` itself stays byte-identical; that phase opens with the regression test that pins the diff.

## Phases

| # | Phase | Delivers | Depends on | Status |
| --- | --- | --- | --- | --- |
| 01 | [Diff overview parser](01-diff-overview-parser.md) | `diff_overview.py`: per-file parse + bounded render; `summarize_diff` delegates, PR body unchanged | — | ☐ |
| 02 | [Every-provider shell check](02-every-provider-shell-check.md) | `every_provider_grants_shell` + `AgentRouter.route_always_grants_shell` | — | ☐ |
| 03 | [Git Manager writes the overview](03-git-manager-overview.md) | `change-overview.txt` beside `current.diff`: full-SHA base, since-last-commit section, `[denied]`, redaction | 01 | ☐ |
| 04 | [Request fields and footer](04-request-fields-and-footer.md) | `AgentRunRequest` fields, footer rendering, containment, `request.json`, partial-diff clearing in the router | — | ☐ |
| 05 | [Core wiring](05-core-wiring.md) | Nodes publish and pass the overview, the base and the guidance sentence; prompt variables; recovery | 02, 03, 04 | ☐ |
| 06 | [Packaged prompts and guide](06-packaged-prompts-and-guide.md) | Rewritten `implementation` prompts, explicit whole-diff critics, guide pages, fake-CLI run | 05 | ☐ |

01, 02 and 04 are independent and may land in any order.

## Cross-cutting

- **Branching** — a branch off `dev` (`feat/agent-diff-handoff`); never commit to `dev` or `main` directly, and the PR targets `dev`. See `.agents/rules/git-workflow.md`.
- **Commits** — one per phase, imperative subject, scoped staging only (never `git add .`), and **no agent-attribution trailer or footer**.
- **Gates per phase** — `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports`, `pytest`, plus `python tools/mdlint.py` when Markdown changed.
- **Tests** — new or changed behavior ships with tests; provider, router and pipeline coverage uses deterministic fake CLIs.
- **Docs** — the documents on this branch are updated in the same phase as the code, including the shipped operator-facing copies under `src/wastech_orchestrator/packaged/`.
- **Invariants** — no phase weakens the security envelope, teaches the core a provider's CLI syntax, or hands a node a publication mandate.

## Risks & mitigations

| Risk | Likelihood | Mitigation |
| --- | --- | --- |
| The overview and `current.diff` disagree on the path set (untracked files, `.worc/` excludes, renames) | medium | 03 renders the whole-task section **from the diff text** it just wrote, never from a second git query; the since-last-commit section is the only part taken from git, and its test asserts it is a subset of the whole-task paths. |
| `current.diff` changes by accident while 03 edits `write_current_diff` | low | 03's first commit is a byte-identity regression test on a fixture tree, written before the change. |
| The guidance sentence steers a content critic away from the whole diff | medium | 06 adds an explicit "read the whole change" clause to each packaged critic, and AC-8 asserts it. |
| An agent ignores the guidance and still reads both | medium | Not a failure (FR-3 keeps the diff); measured after landing (DoD follow-up) and fixed in prompts, not in code. |
| The partial-diff fallback leaves a stale overview beside a new diff | low | 04 clears the three fields in the router; AC-E1 pins it. |
| Changing the git protocol's `write_current_diff` signature breaks fakes across the suite | medium | 03 adds a new result type and updates the protocol and every fake in the same commit; `mypy src` catches a missed implementer. |
