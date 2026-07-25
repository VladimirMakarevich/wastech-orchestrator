# P2.9 — keep intermediates out of the documentation PR

Priority: **P2** Status: **proposal** Date: 2026-07-25 Source: [postmortem.md](postmortem.md) DR-11

## Problem

The `p9-09` commit contains three files where the task constraint said one: _"The only file written by this task is the audit findings document itself."_ The extra file is `report-structure.md`, an intermediate blueprint that `architecture_design` was **instructed** to write into the deliverable directory.

## Evidence

Commit `242a518`, 911 insertions across `report.md`, `sources.json` and `report-structure.md`.

The role prompt causes it. `.worc/flows/deep_research/architecture_design.md:33-36`: _"Under this flow's `repository_document` output policy, `{repo}/docs/research/{task_id}/` is the **only** writable path — organize any notes there and nowhere else."_ The node's structured result was already captured privately as a `node_output` artifact, so the in-repo copy is pure duplication.

The engine has no filename allowlist on the publish path:

- `agent.py:506-528` (`_apply_output_containment_guard`) is a **directory-containment** test (`within_subdir`), not a filename allowlist.
- `output_policy.py:63-68` defines `required_files=("report.md","sources.json")`, but that tuple is used only on the private-report path (`publish.py:200`), never as a commit filter.
- `publish.py:11-12` states the design intent: _"The documentation PR needs no special staging: the after-stage output guard already confined the writes to the report directory, so the existing scoped staging commits only those docs."_ Everything in the directory ships.

Independently notable: the shipped `report.md` is the **less honest** of the two documents. `report-structure.md:214` grades CLI + init as "Spot-checked at call sites"; `report.md:260` upgrades it to "walked; **no findings**". Synthesis strengthened a coverage claim without new evidence — which is a [P1.5](p1-5-research-role-prompts.md) prompt matter, but it is the reason the intermediate leaking is not purely cosmetic: the PR now contains two documents that disagree.

## Change

Pick one; they are alternatives, not a sequence.

1. **Cheapest, no engine change.** Have `architecture_design` return its structure through the output schema only and write nothing. It can then drop from `permission_profile: workspace-write` to `read-only`, which also removes an unused write grant. Requires [P2.8](p2-8-node-output-handoff.md) piece 1 or 2 if the blueprint must still reach `synthesis` in full — otherwise the same information loss that DR-4 describes gets worse, not better.
2. **Most robust.** Stage only `resolved.required_files` for `repository_document` at publish, mirroring what the private-report path already does. An intermediate may then exist in the working tree without shipping.
3. **Middle.** Add an `intermediates` concept to the output policy that the containment guard permits and publish excludes (e.g. a `_scratch/` subdirectory inside the report dir).

Option 2 is the one that makes the invariant hold regardless of what a role prompt tells a node to write, which is the right property for a publish gate. Option 1 is the one that can ship today.

## Acceptance

- A `deep_research` run's commit contains exactly the policy's `required_files`.
- A node writing an extra file into the report directory does not fail the run — it simply does not ship (option 2/3), or does not happen (option 1).
- The `repository_document` publish path and the private-report path agree on what "the deliverable" means.

## Test

Integration: a fixture run where an agent writes an extra file into the report directory produces a commit containing only `report.md` and `sources.json`. Unit on the staging filter for option 2.

## Scope / risk

Options 2 and 3 are orchestrator defaults touching the publish path — the one place where a mistake means the wrong bytes reach a real branch. Option 1 is a target-copy prompt edit with no engine risk.

Note that the containment guard itself is not at fault and should not be loosened: it correctly confined every write to the report directory. This is about what publish _stages_, not about what an agent may _write_.

## Depends on

[P2.8](p2-8-node-output-handoff.md) if option 1 is chosen — otherwise removing the write removes the blueprint entirely.
