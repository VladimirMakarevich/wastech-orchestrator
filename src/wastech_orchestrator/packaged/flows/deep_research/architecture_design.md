From the repository and external evidence, work out the structure of the answer so the synthesis can present it with citations.

Base this on the repository analysis, which arrived as up to three passes over disjoint surfaces — read every report you were handed, and treat the union of them as the evidence base:

{?analysis_core_path}- core (central logic, rules and invariants, configuration, internal wiring): {analysis_core_path}
{/analysis_core_path}{?analysis_surfaces_path}- entry points and adapters (command line, APIs, packaging, integrations, generated schemas): {analysis_surfaces_path}
{/analysis_surfaces_path}{?analysis_docs_tests_path}- plan of record and the test suite: {analysis_docs_tests_path}
{/analysis_docs_tests_path}{?external_research_path}- external evidence: {external_research_path}
{/external_research_path}{?refinement_path}

Keep it scoped to the brief at {refinement_path}.{/refinement_path}

## What To Produce

Organize the evidence into the shape the deliverable needs, and capture the reasoning behind it. For a design/recommendation question that means the options, their trade-offs, and a recommended approach grounded in what the evidence actually supports.

For a **plan-vs-implementation audit**, organize the findings so each one is directly actionable:

- Group findings by whatever unit the project's own plan uses (phase, milestone, epic) and, within a unit, order by **severity** (high → medium → low), where severity reflects how far the shipped state diverges from the plan of record and the blast radius of the gap.
- For each finding capture: the exact `path:line` evidence; the plan clause it violates or falls short of; _why_ it matters (correctness, robustness, test-coverage, or architectural-drift consequence); and a concrete remediation with a pointer to where it should be closed.
- Call out **cross-unit dependency gaps** as their own group: an earlier unit left partial because it needed a later one, and never revisited. State the chain and what remains open.
- Separate confirmed defects from suspected-but-unverified ones so the synthesis can mark the latter honestly under Open questions.

**Confirm empirically what you can.** Unlike the analysis passes upstream you have a shell, so a claim that a pattern crashes, that a regular expression misbehaves on some input, that a command exits non-zero, or that a test does not cover what it says can often be settled in one command instead of argued from a citation — run the project's own test suite or its command-line entry point, or a one-liner (`node -e`, `python -c`) rather than creating a file to run. Record what actually happened, and mark a claim that rests on reading alone as exactly that. Never weaken, stub, or "fix" anything to make a reproduction work; if you have no shell, say so and rely on citations.

Keep enough reasoning that a reader can follow each judgment back to its evidence.

**Write no files at all — your answer is your closing message, and it is the whole blueprint.** That message is what the writing node downstream is handed, so a summary of your reasoning is not a substitute for it: everything you want in the deliverable — every grouping, every `path:line`, every reproduction result, every open question — has to be in the text you return, at full length. Nothing you leave out survives this step. Do not write a scratch or notes file anywhere in the repository: this run publishes what is in its deliverable directory, so a working file left there ships in the pull request as if it were part of the answer, and two documents that disagree are worse than one.
