# Implementation plan — Tracker connector

Builds [design.md](../design.md) toward [acceptance-criteria.md](../acceptance-criteria.md); the finish line is [definition-of-done.md](../definition-of-done.md).

## Strategy

Two independent tracks. **Phases 01, 02 and 08** are the worc-side contract items and land in this repository; they are small, need nothing from the connector, and are useful to any scripting or flow-authoring operator on their own. **Phases 03–05** build the connector's v1 in its own repository against worc exactly as it is today — no worc change is on their critical path, which is what lets the connector mechanics be proven first. **Phase 06** joins the tracks: the connector adopts `references:`, `pr_url` and the `rejected` section. **Phase 07** adds the optional triage path last, once real runs have shown what the report needs to carry; it is the only connector phase that waits for a worc phase (08, the report directory).

The critical path is 03 → 04 → 05 → a real end-to-end run. The biggest risk is not code but the boundary: a connector that quietly starts reading `state.db` or writing into `tasks/pending/` because it is easier — every connector phase carries that invariant explicitly.

## Phases

| # | Phase | Repository | Delivers | Depends on | Status |
| --- | --- | --- | --- | --- | --- |
| 01 | [`references:` task field appended to the PR body](01-worc-references-field.md) | worc (here) | FR-W1: gate validation, `NormalizedTask.references`, publish appends `## References`, guide updated | — | ☐ |
| 02 | [`pr_url` and a `rejected` section in `worc list --format json`](02-worc-list-json-pr-url.md) | worc (here) | FR-W2: the JSON entry carries `pr_url`; FR-W3: `--all` gains a `rejected` section from the ledger; the guide names the shape as the scripting contract | — | ☐ |
| 03 | [Connector skeleton: core, GitHub adapter (read side), gate, state, dry run](03-connector-skeleton.md) | connector | FR-C1, C2, C8, C13, C14: `watch --once --dry-run` lists what it would do against a real repository | — | ☐ |
| 04 | [Task builder and handoff](04-connector-builder-handoff.md) | connector | FR-C3, C4, C5, C6, C7: a gated issue becomes a promoted worc task, idempotently | 03 | ☐ |
| 05 | [Write-back and reconciliation](05-connector-writeback.md) | connector | FR-C9, C10, C11, C12: labels, comments, PR link, close on merge, failure path; first real end-to-end run | 04 | ☐ |
| 06 | [Adopt the worc contract](06-connector-adopt-contract.md) | connector | `Fixes #<n>` via `references:`, `pr_url` and the rejection reason from `worc list --all`, `close_on_merge` becomes a real choice | 01, 02, 05 | ☐ |
| 07 | [Optional triage](07-connector-triage.md) | connector (+ flow data) | FR-C15: `triage.enabled`, `install-flow`, the two-step path, the report read from `.worc-connect/triage/<id>/` | 04, 05, 08 | ☐ |
| 08 | [Configurable report directory, private policy included](08-worc-report-dir.md) | worc (here) | FR-W4: `flow.report_dir`, `{report_dir}` prompt variable, path validation, the private-policy allowance, `deep_research` prompts switched to the variable | — | ☑ |

Phases 01, 02 and 08 are independent of each other and can run in parallel with 03–05.

## Cross-cutting

- **Branching** — worc phases: a branch off `dev` (`feat/references-field`, `feat/list-json-pr-url`, `feat/flow-report-dir`); never commit to `dev` or `main` directly, and the PR targets `dev`. See `.agents/rules/git-workflow.md`. Connector phases: the connector repository's own branching, set up in phase 03.
- **Commits** — atomic, imperative subject, scoped staging only (never `git add .`), and **no agent-attribution trailer or footer** — in both repositories.
- **Gates per phase** — here: `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports`, `pytest`, plus `python tools/mdlint.py` when Markdown changed. Connector: its equivalents, set up in phase 03 before any feature code.
- **Tests** — new or changed behavior ships with tests; anything touching `gh` or `worc` is driven by a fake executable.
- **Docs** — worc phases update the packaged guide in the same phase; connector phases keep the connector README and configuration reference current.
- **Invariants** — no phase teaches worc a tracker's syntax, lets the connector run an agent, or lets it write anywhere in the clone but `tasks/preparing/`.

## Risks & mitigations

| Risk | Likelihood | Mitigation |
| --- | --- | --- |
| The connector drifts into worc's internals (`state.db`, `.worc/`, `tasks/pending/`) | medium | AC-7 and AC-N7 assert the only launches and the only path written; the reconcile reads `worc list --format json` and nothing else |
| A prompt injection in a labelled issue reaches an agent with write access | medium (by design the label is the perimeter) | gate is fail-closed and mandatory; `auto_merge` left unset by `init`; the caveat is in the README; the dangerous-diff gate and human PR review stay as worc ships them |
| Title sanitizer misses a token worc rejects → silent quarantine | low | AC-4 runs the generated file through worc's real gate; AC-E3 turns a quarantine into a visible `worc:failed` comment |
| Two processes race on `tasks/preparing/` | low | atomic temp+`os.replace` write; `promote` refuses to overwrite; the audit commit stages only the lifecycle file |
| Label-based state confuses humans who edit labels by hand | medium | rows are rebuilt from labels + `worc list` each tick, so a hand edit is adopted rather than fought; the operator guide says what each label means |
| `worc list --format json` shape changes under the connector | low | phase 02 names the shape as a contract in worc's guide; the connector pins a minimum worc version |
| The triage report needs a home the connector may read without touching `.worc/` | decided (Q-6) | phase 08 makes the report directory a flow property and lets the private policy name one outside `.worc/`; the publish-time leak check stays the guard, so a connector home that is not gitignored fails the triage task closed instead of leaking |
| A gate reject is invisible outside `.worc/` (no `tasks` row; reason only in the ledger and `.worc/logs/<id>/`) — the connector cannot name the reason without reading worc's private home | certain (verified) | Q-12 decided (b): phase 02 adds a `rejected` section to `worc list --format json --all` (FR-W3, D15); phase 06 reads the reason from it; before that the connector reports the reject without a reason and points at `worc status <id>` |
