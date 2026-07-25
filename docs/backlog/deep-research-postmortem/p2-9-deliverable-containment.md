# P2.9 — keep intermediates out of the documentation PR

Priority: **P2** Status: **accepted (option 1 + 10g; option 2 withdrawn)** Date: 2026-07-25 Source: [postmortem.md](postmortem.md) DR-11

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
2. ~~**Most robust.** Stage only `resolved.required_files` for `repository_document` at publish.~~ **Withdrawn** — see the decision below.
3. **Middle.** Add an `intermediates` concept to the output policy that the containment guard permits and publish excludes (e.g. a `_scratch/` subdirectory inside the report dir).

## Decision (2026-07-25)

**Option 1 + [P3.10](p3-10-flow-and-config-hygiene.md)'s 10g. Option 2 is withdrawn; option 3 is kept as a fallback, not scheduled.**

Two facts about the current engine decided it:

- **There is no commit allowlist today, and `required_files` is not one.** Publish stages everything inside the report directory; `required_files` has exactly one use in the whole engine — [`publish.py:200`](../../../src/wastech_orchestrator/core/flow/nodes/publish.py), registering artifacts on the **private** path. It filters nothing and fails nothing, so the `output_policy.py:38` docstring calling these "the deliverables the flow must produce in that directory (checked at publish)" is inaccurate and should be corrected. Option 2 would therefore not restore an invariant — it would introduce a new restriction.
- **That restriction would be a hardcode on the publish path.** `required_files` is a fixed tuple per policy enum, not operator-authorable. An operator-authored flow on `repository_document` whose deliverable is `overview.md` + `data.csv` would pass containment, succeed, and commit **nothing** — silent loss on the one path where a mistake reaches a real branch. Free filenames are the correct default: the deliverable's name is the flow author's business, not the engine's.

What the run's actual symptom needs instead: the node must not write intermediates into the deliverable directory (option 1, a prompt edit), and whatever does ship must pass the target repository's own gates before the commit (10g — on `p9-09` it was `npm run format` that went red).

**Added scope, tracked in [P1.6](p1-6-citation-checker-strictness.md):** the one place where a filename genuinely must be known is [`checks.py:147`](../../../src/wastech_orchestrator/core/flow/nodes/checks.py) — the citation checker looks up a literal `sources.json` and, when the name differs, returns `uncheckable` and the gate silently does nothing. Replace the magic string with a checker-node field defaulting to `sources.json`. That is the whole declarative surface this area needs; a `deliverables:` schema is not.

Option 3 introduces its own naming convention (`_scratch/`) for a result the prompt edit already achieves. Revisit only if a node genuinely needs durable scratch space inside the repository.

## Acceptance

- A `deep_research` run commits its report and no intermediate: `architecture_design` writes nothing into the repository and runs `read-only`, its blueprint reaching `synthesis` through the [P2.8](p2-8-node-output-handoff.md) channel instead.
- Whatever the flow does commit has passed the target repository's own documentation gate before the commit (10g), so a stray file cannot turn the target's CI red.
- Filenames stay the flow author's choice: no engine path filters a commit by name, and a flow that names its deliverable anything at all still publishes it.
- The citation checker finds its manifest through a declared, defaulted field rather than a literal `sources.json` (tracked in [P1.6](p1-6-citation-checker-strictness.md)).

## Test

Integration: a `deep_research`-shaped fixture run commits only the report files, with `architecture_design` on `read-only` and its blueprint still resolvable by `synthesis`. Unit on the citation checker resolving a non-default manifest name.

## Scope / risk

Option 1 is a prompt edit (target copy + packaged) plus a profile downgrade — no engine risk. 10g is a flow-graph addition. Neither touches the publish path, which is the point: the one place where a mistake means the wrong bytes reach a real branch stays untouched.

Note that the containment guard itself is not at fault and should not be loosened: it correctly confined every write to the report directory. This item is about what a node is told to _write_, not about what publish _stages_.

## Depends on

[P2.8](p2-8-node-output-handoff.md) if option 1 is chosen — otherwise removing the write removes the blueprint entirely.
