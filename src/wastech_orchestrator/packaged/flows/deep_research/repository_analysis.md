Investigate the repository at `{repo}` for evidence that bears on the research question.{?refinement_path} Work from the refined brief at {refinement_path} — cover every sub-question it lists.{/refinement_path}

## Where Evidence Lives

- **Plan of record** — if the project maintains one (a roadmap, requirements, design decisions, a glossary), read it: it defines what "correct" and "done" mean for this question.
- **Shipped code** — the relevant modules, APIs, configuration, and tests for the question at hand.
- **Delivery evidence** — git history is always present and authoritative: `git log` / `git show` reveal what each change actually did versus what it claims. Discrepancies here are prime findings. If prior orchestrator run logs happen to be present in the working tree (e.g. under `.worc/logs/`), use them as a supplement, but they are typically gitignored, so do not treat their absence as a finding.

## What To Look For

Record exact `path:line` for every observation you intend to make a claim about — these become the citations the synthesis must anchor, so they must point at text that is really there.

For a **plan-vs-implementation audit**, for each unit in scope (phase/milestone/epic):

1. **Exit-criteria coverage.** Read the unit's stated exit criteria and confirm the corresponding code exists and behaves as specified. Note anything marked done in the plan but thin, stubbed, or missing in code.
2. **Requirement conformance.** Trace each cited requirement to its implementation. Note silent divergences and undocumented scope cuts.
3. **Architecture invariants.** If the project documents its own invariants (e.g. in `.agents/rules/`, `CLAUDE.md`, `AGENTS.md`), check whether the code actually upholds them.
4. **Test adequacy.** Compare the unit's coverage priorities against what the tests actually exercise.
5. **Cross-unit gaps.** Follow whatever dependency chains the project tracks between units. Look for the specific failure mode: an earlier unit that needed something from a later one, was completed with a placeholder or partial seam, and was never revisited to close the gap once the later unit shipped.
6. **Weak code.** Note fragile logic, non-deterministic ordering, unhandled error paths, TODO/FIXME markers, and abstractions built ahead of a concrete need.

Read only; do not edit code or write files. Return the typed structured result required by the output schema.
