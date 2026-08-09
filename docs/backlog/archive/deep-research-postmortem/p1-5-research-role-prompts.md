# P1.5 — fix the research role prompts: verifier rubric, critic promises, producer sweep discipline

Priority: **P1** Status: **implemented** (2026-07-26) Date: 2026-07-25 Source: [postmortem.md](postmortem.md) DR-6, DR-1 (prompt half), DR-12 (prompt half)

## Implemented

All nine items. Items 1, 5 and 6 had already landed as the [P0.1](p0-1-evaluator-gate-severity.md) / [P1.6](p1-6-citation-checker-strictness.md) halves and were extended, not rewritten; items 2, 3, 4, 7, 8 and 9 are this change. Item 8 landed against the three analysis passes [P1.4](p1-4-audit-coverage-gate.md) created, which is why P1.4 was taken first — writing it into `repository_analysis.md` and then splitting that file would have meant propagating the same paragraph twice.

**Item 2 — the manifest sets the scope, not the report's prose.** The verifier's `{?checks_path}` block now says outright that `citation.json` carries one entry per manifest source, so it is the verifier's _scope_: it reports how many citations exist, including ones the report chose not to surface. On top of that the prompt requires opening the manifest itself for the claim behind each entry, and reporting the entry ids it never reached as a `low` finding against its own pass, so the report's coverage claim cannot rest on an unopened citation. The manifest is referred to as "the `sources.json` beside the report, unless this flow names the file differently" rather than as a second hardcoded absolute path — the one already in line 1 is removed by [P2.8](p2-8-node-output-handoff.md) piece 1 and this change deliberately does not add another.

**Item 3 — an external source is fetched, never recalled.** Stated as its own paragraph, because the run "confirmed" two MDN urls from parametric memory with zero network calls while holding both web tools. A failed fetch, a moved page, or a quote absent from the retrieved page is a finding; a node with no working web tool must report the entry unresolved. The tool is named by capability ("the web tool this node has been granted") rather than as `WebFetch`, since the same flow runs on either provider and their tool names differ.

**Item 4 — the under-claiming watch-item, bounded as the risk note demands.** It must name a specific subsystem **and** the specific property that was never checked; the prompt gives the shape of an acceptable finding and explicitly rejects "coverage could be deeper" so a chatty verifier cannot satisfy it with a generic complaint. It also names the exact trap of the run this came from: a "needs confirmation" tier and a paragraph explaining why the severities are not inflated are wording, not evidence, and a report that examined a fifth of its scope can be entirely accurate and still unsafe to act on.

**Item 7 — conditional on what the model can actually see.** There is no prompt variable for "which round is this" (`rework_report_path` reaches the footer, not the renderer), and inventing one to encode a round number would be a new engine surface for one sentence. Instead the clause is conditional on the session itself: _if you can see your own earlier round(s) in this conversation_, treat it as a re-review and spend the round on what remains; if you see none — a first pass, or a session that could not be resumed — review the report whole. That is also robust to the `resume_own_lineage` finding in [P3.10](p3-10-flow-and-config-hygiene.md): whichever way that resolves, the prompt stays true.

**Item 8 — the class sweep, in all three analysis prompts.** "Every finding is a pattern, not an instance": grep the corpus for the whole class before recording one, and record every site. The paragraph carries the three concrete singletons from the run (the inert option with a worse sibling, the duplicate-detection bug with exact duplicates elsewhere, the unsafe regex 170 lines from the correct escaping helper) because a rule with its evidence attached survives editing better than a rule without.

**Item 9 — empirical confirmation, in the two nodes that can.** `architecture_design` and `synthesis` hold `Bash` and `workspace-write` and used them for a JSON-validity check and an `ls`. Both prompts now say a claim that something crashes, exits non-zero, or mishandles an input can be settled with one command, and ask for the report to distinguish what was reproduced from what rests on reading alone. Two guards on the wording: run a one-liner (`node -e`, `python -c`) rather than creating a file, since anything written in the report directory ships in the pull request — which keeps this from working against [P2.9](p2-9-deliverable-containment.md) — and never weaken, stub or "fix" anything to make a reproduction work.

Anti-drift tests, in `tests/core/test_flow_snapshot.py` beside the packaged-gate test P0.1 added: each research evaluator prompt must state the verdict _mechanism_ (and must not mention `gate_severity`, so a threshold cannot be copied into prose where it goes stale), and `verifier.md` must carry the omission watch-item, the manifest scope, and the fetch duty. The prompt-variable lint over every packaged flow (`test_packaged_flows_lint_clean`) is the other half of this item's Test section, and it now also covers evaluator role files.

Not done, deliberately: the hardcoded `{repo}/docs/research/{task_id}/report.md` in `verifier.md` and `critic.md` stays until P2.8 piece 1 publishes the produced file, because `{synthesis_path}` currently resolves to the node's chat sign-off. Also untouched: the boilerplate line "Return the typed structured result required by the output schema", which several `contract: none` nodes across other flows carry although no schema is set for them — a repo-wide wording sweep outside this item (the three new analysis prompts say what is actually true instead).

Target-only remainder: this document's scope names the target copies under `.worc/flows/deep_research/*.md` (deliberate specialized forks, all newer than packaged). They are not in this repository, so only the packaged defaults changed; a target's forks need the same six edits, or the refresh that P1.4's remainder already requires will overwrite the fork instead of merging with it.

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
