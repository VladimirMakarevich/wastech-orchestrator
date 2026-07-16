From the repository and external evidence, work out the structure of the answer so the synthesis can present it with citations.

{?repository_analysis_path}Base this on the repository analysis at {repository_analysis_path}.{/repository_analysis_path}{?external_research_path} Fold in the external evidence at {external_research_path}.{/external_research_path}{?refinement_path} Keep it scoped to the brief at {refinement_path}.{/refinement_path}

## What To Produce

Organize the evidence into the shape the deliverable needs, and capture the reasoning behind it. For a design/recommendation question that means the options, their trade-offs, and a recommended approach grounded in what the evidence actually supports.

For a **plan-vs-implementation audit**, organize the findings so each one is directly actionable:

- Group findings by whatever unit the project's own plan uses (phase, milestone, epic) and, within a unit, order by **severity** (high → medium → low), where severity reflects how far the shipped state diverges from the plan of record and the blast radius of the gap.
- For each finding capture: the exact `path:line` evidence; the plan clause it violates or falls short of; *why* it matters (correctness, robustness, test-coverage, or architectural-drift consequence); and a concrete remediation with a pointer to where it should be closed.
- Call out **cross-unit dependency gaps** as their own group: an earlier unit left partial because it needed a later one, and never revisited. State the chain and what remains open.
- Separate confirmed defects from suspected-but-unverified ones so the synthesis can mark the latter honestly under Open questions.

Keep enough reasoning that a reader can follow each judgment back to its evidence. Under this flow's `repository_document` output policy, `{repo}/docs/research/{task_id}/` is the **only** writable path — organize any notes there and nowhere else. A write anywhere outside it (repo root, source tree, a scratch file) fails validation, so do not create one. Return the typed structured result required by the output schema.
