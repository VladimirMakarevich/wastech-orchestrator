# P1.5 — fix the research role prompts: verifier rubric, critic promises, producer sweep discipline

Priority: **P1** Status: **proposal** Date: 2026-07-25 Source: [postmortem.md](postmortem.md) DR-6, DR-1 (prompt half), DR-12 (prompt half)

## Problem

Three of the `deep_research` role prompts assert things that are not true, and one of them has no verdict rubric at all. The most expensive consequence is that the fact-verification gate cannot fail a conservatively written report, and the producers never sweep a finding's whole class.

## Evidence

### `verifier.md`

- The words `verdict`, `accept` and `rework` **do not appear**. There is no rubric; the engine derives the verdict from severities the prompt never defines.
- Line 1 states a falsehood: _"The deterministic citation check **has already confirmed** that cited locations exist."_ It was false for 2 of 41 entries, and `citation.json` was never handed to the node ([P1.6](p1-6-citation-checker-strictness.md)).
- The watch-list is four variants of "the report claims too much" (inflated severity, wrong category, closed gap, suspected-as-confirmed). There is **no instruction to look for what the report missed**, so a report with a "needs confirmation" tier and a "so severity is not inflated" section is unfalsifiable against it. `fact_verification` duly returned zero findings.
- No occurrence of `WebFetch`, `url`, `http`, or `sources.json`. The node had `network_access: true` and both web tools granted, made **zero** network calls, and "confirmed" from memory the two MDN URLs the deterministic checker had explicitly marked unfetched.
- It never opened `sources.json`, so its scope was set by the report's own inline citations — it structurally could not notice a citation the report chose not to surface. It reached 17 of 24 cited paths, missed three files backing active findings, and endorsed a "Verified remediated" section whose five citations it never opened — while self-reporting _"I've verified **every** finding in the report."_

### `critic.md`

- `:25-27` — _"severity **medium** or high marks a substantive weakness that should be reworked"_ is false under the shipped `gate_severity: high`, and _"let them carry into the report's Open questions"_ describes a mechanism that does not exist: `accept` routes straight to `publish`, and the report's `## Open questions` section was written by `synthesis` before the critic ran.
- `:14-15` — _"**You keep your own session across rounds**, so do not repeat a point you already raised"_ is inert on round 1, which under current defaults is the only round. The `request.json` argv contains no `--resume`.

### Producer prompts

The audit treated every finding as a singleton. It found one inert-option defect and stopped — a worse one sat in a sibling file. It found one duplicate-findings defect and stopped — exact duplicates sat in another rule. It found one unsafe-regex defect and stopped — an unescaped-interpolation crash sat 170 lines from the correct escaping helper. Nothing in the prompts asks for a class sweep.

## Change

### `verifier.md`

1. Delete the false assurance in line 1; replace with what the checker actually guarantees (the file exists, the line is in range, the snippet occurs somewhere in the file) and what it does not (the line is authoritative, the claim is true).
2. Require opening **every** entry in `sources.json`, and reporting any citation left unopened as a `low` finding against itself.
3. Make `uncheckable` sources the verifier's job: it has network access — fetch them.
4. Add a fifth watch-item for **under**-claiming: _"what should a full audit of this declared scope have found that is absent here? Name the subsystem and the property that was never checked."_
5. State the verdict rubric explicitly, matching whatever [P0.1](p0-1-evaluator-gate-severity.md) settles on.

### `critic.md`

6. Reconcile `:25-27` with the shipped gating behavior and delete the "carry into Open questions" clause.
7. Make `:14-15` conditional on an actual prior round rather than unconditional prose.

### Producer prompts (`repository_analysis`, or its successors from [P1.4](p1-4-audit-coverage-gate.md))

8. Replace "record each finding" with a class-sweep instruction: _"every finding is a pattern, not an instance — before recording it, grep the corpus for the whole class and record every site."_
9. Invite empirical confirmation where the node has the tools. `architecture_design` and `synthesis` both had `Bash` and `workspace-write` and used them only for a JSON-validity check and an `ls`; a three-line Node snippet would have confirmed the regex finding empirically instead of by citation.

## Acceptance

- `verifier.md` contains an explicit verdict rubric and at least one watch-item directed at omissions.
- A run's verifier opens every `sources.json` entry, or reports the ones it did not.
- An `uncheckable` external source is either fetched or reported as an open item — never silently "confirmed".
- No role prompt asserts a capability the node does not have (network, session continuity, shell) or a mechanism that does not exist.

## Test

Prompt-lint style: a test asserting that each packaged `deep_research` role file contains no reference to `{…_path}` variables the node cannot resolve, and that evaluator role files name a verdict rubric. Behavioral confirmation comes from the next real run, not from unit tests.

## Scope / risk

Both the target copies (`.worc/flows/deep_research/*.md`, which are deliberate specialized forks, all newer than packaged) and the packaged defaults. The `verifier.md` and `critic.md` changes are repo-agnostic and belong upstream. The class-sweep instruction is also repo-agnostic and is the single highest-value prompt edit in the set.

Risk: item 4 can make the verifier chatty — a verifier that always finds "something missing" is as useless as one that never does. Word it to require naming a _specific_ subsystem and property, so it cannot be satisfied by a generic complaint.

## Depends on

Items 5 and 6 depend on [P0.1](p0-1-evaluator-gate-severity.md) settling the rubric. Item 1 is cleaner after [P1.6](p1-6-citation-checker-strictness.md) defines what the checker actually promises. Items 2–4 and 8–9 are independent and can ship immediately.
