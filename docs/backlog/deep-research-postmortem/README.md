# `deep_research` post-mortem campaign (2026-07-25)

Status: **in progress — steps 1-6 implemented (P0.1, P0.3, P0.2, P1.6, P1.5, P1.4); P2.8 piece 2 shipped early with P1.4; P1.7, P2.9, P3.10 open** Date: 2026-07-25 Owner: Vladimir Makarevich

This folder groups everything that came out of the post-mortem of `p9-09-full-solution-deep-audit`, the **first and only production run of the `deep_research` flow**, into a single campaign with one execution order. The analysis is in [postmortem.md](postmortem.md); the files below are the implementable tasks it produced.

These documents are design detail, not an implementation contract, and must not override the hard invariants in [../../../CLAUDE.md](../../../CLAUDE.md) / [../../../AGENTS.md](../../../AGENTS.md) / [../../../.agents/rules/](../../../.agents/rules/).

## Campaign-wide constraint: flow-agnostic by construction

Every item here was found through `deep_research`, and none of them may be implemented **for** `deep_research`. The orchestrator must serve arbitrary operator-authored flows — a flow this repository has never seen, with node ids, role files and deliverables it does not know — exactly as well as the packaged ones. Concretely, for every item below:

- **No code branches on a flow name, a node id, a role-file name, or a path convention.** No `if flow.name == "deep_research"`, no `docs/research/{task_id}/` baked into the engine, no node id treated as special. Behavior is selected by a declared field, never by an identity.
- **Every mechanism is reachable declaratively** — a flow YAML field, a `flow.defaults` entry, a role file, or a config key — so an operator gets it in their own flow without touching Python.
- **A flow that declares nothing still behaves sanely.** Defaults are chosen for the general case; a knob's absence is never a silent failure mode (that is the P0.1 lesson).
- **Fixed in-code lists are a smell.** Where a set of names must exist in code (allowed prompt variables, checker kinds, severity ranks), it is a documented allowlist with an operator-visible extension story, not an incidental tuple that a user flow silently falls outside of.

Item-specific traps this rules out: P1.4's subsystem taxonomy (target-only until it is expressed generically), P1.7's finalize lens (a `supervision.finalize_role_file` field, never a flow-name branch), P2.8's produced-file channel (a node-declared path, not a filename convention) and its footer slot (generic upstream-output, not a research-shaped slot), P1.6's citation statuses (checker-level, not flow-level), and the hardcoded `{repo}/docs/research/{task_id}/report.md` strings currently sitting in the packaged verifier and critic prompts — which are themselves an instance of the anti-pattern. P2.8 piece 2 (the evaluator node-output channel) has since shipped with P1.4, but removing those strings waits on P2.8 piece 1: `{synthesis_path}` still resolves to the node's chat sign-off, not the report.

## What the run showed

The run was technically flawless — 32 min, $12.93, 8 provider attempts, `stage_attempts=1` everywhere, zero fallbacks, retries, crashes, timeouts or permission denials, exchange clean, no rework loop. The deliverable it produced was worth roughly a fifth of what the task asked for.

Three failures compound, and they are independent of model quality:

1. **The pipeline detected its own weakness and threw the signal away.** `critical_review` filed a correct `medium` finding — the report's "no HIGH" verdict rests on subsystems it only spot-checked — and the engine returned `accept`, because `gate_severity` defaults to `high` and no flow sets it. Three unused rework rounds, zero operator signal, and a PR body stating all gates "passed".
2. **Nothing measured coverage.** `repository_analysis` opened 18% of the in-scope files and stopped at turn 34 of 400 with 112 of 120 timeout minutes left. Two release-blocking defects in the target repo — a CLI `bin` that is a silent no-op through the npm symlink, and a lint rule that reads arbitrary files outside the analyzed root — sat in files it never opened, in subsystems the report graded "walked".
3. **Information degraded at every edge.** Node outputs cross as paths, never content, and the path points at a chat sign-off rather than the artifact. The structuring node wrote a 19 821-byte blueprint; the writing node was handed a 4 042-byte pointer and never opened the blueprint. Downstream nodes re-read 407 071 B of source the first node had already walked, and produced zero new findings for 37% of producer spend.

Plus one defect that is actively corrupting data rather than losing signal: the secret redactor rewrites the ordinary identifier `tokens`, producing invalid JSON in `events.jsonl` and — because the same function redacts node outputs — corrupting the handoff channel. It has already shipped a phantom-defect explanation into a published PR body on a different task.

## Items, in priority order

| # | Item | What it does | Effort | Scope |
| --- | --- | --- | --- | --- |
| P0.1 | ✅ [Make a `medium` evaluator finding gate](p0-1-evaluator-gate-severity.md) | `gate_severity: medium` on `critical_review`; restore the deleted header comment; reconcile `critic.md` with the shipped rubric | **one line** | flow (+ packaged default) |
| P0.2 | ✅ [Surface an accepting evaluator's findings](p0-2-evaluator-findings-surfacing.md) | Pass `final_message` through so findings reach `summary.json` and the PR body; forward `outcome.findings` to the supervisor | one line + merge | orchestrator |
| P0.3 | ✅ [Fix the redaction false positive](p0-3-redaction-false-positive.md) | Align `_ASSIGNMENT` with the segment policy; redact decoded values, not the serialized line | small | orchestrator |
| P1.4 | ✅ [Split the analysis node, add a coverage gate](p1-4-audit-coverage-gate.md) | Three sequential analysis nodes with narrow remits + a `coverage_gate` evaluator that demands a traced property per subsystem (the read-only git grant is deferred — see the item) | new nodes/files | flow + role prompts |
| P1.5 | ✅ [Fix the research role prompts](p1-5-research-role-prompts.md) | Verifier rubric + full `sources.json` coverage + an under-claiming watch-item; drop the critic's false promises; class-sweep for producers | prompt edits | prompts (target + packaged) |
| P1.6 | ✅ [Make the cited line authoritative](p1-6-citation-checker-strictness.md) | Drop the `or` fallback (or emit `weak`); a missing snippet is `uncheckable`; publish `citation.json` on the pass path too | small | orchestrator |
| P1.7 | [Give `deep_research` its own finalize lens](p1-7-research-finalize-summary.md) | `supervision.finalize_role_file` + a no-fabrication rule + feed it the evaluator findings | prompt + flow | packaged flow |
| P2.8 | [Let a node's real output cross the edge](p2-8-node-output-handoff.md) | Publish the produced file, not the sign-off; ✅ give evaluators the node-output channel (shipped with P1.4); optionally an upstream footer slot | medium | orchestrator |
| P2.9 | [Keep intermediates out of the PR](p2-9-deliverable-containment.md) | Stop instructing `architecture_design` to write notes into the deliverable dir (+ 10g); no commit allowlist — filenames stay the flow author's choice | small | prompt + flow |
| P3.10 | [Flow and config hygiene](p3-10-flow-and-config-hygiene.md) | Unreachable `refinement`, an always-true gate, dead `resume_own_lineage`, supervisor cost, reasoning trim, target config re-sync | small | flow + config |

## Execution sequence

The order is mostly free; three dependencies are real.

| Order | Item | Depends on | Why |
| --- | --- | --- | --- |
| 1 | ✅ P0.1 | — | One line, largest single effect: it is what turns the critic from an expensive observer into a gate. |
| 2 | ✅ P0.3 | — | Independent, and the only item currently corrupting data. Must precede any content-inlining in P2.8. |
| 3 | ✅ P0.2 | — | Complements P0.1: P0.1 makes substantive findings gate, P0.2 makes sub-threshold ones visible. Shipping only P0.1 still hides them. |
| 4 | ✅ P1.6 | — | Defines what the citation gate actually promises, which P1.5 then writes into the verifier prompt. |
| 5 | ✅ P1.5 | P0.1, P1.6 | Needs the settled rubric (P0.1) and the settled guarantee (P1.6). |
| 6 | ✅ P1.4 | P0.1, **P2.8 piece 2** | A coverage gate whose findings cannot gate is decorative — and one that cannot read the analysis it grades is decorative twice over, so P2.8's evaluator node-output channel shipped with it. |
| 7 | P1.7 | P0.2 | The finalize lens needs findings to render. |
| 8 | P2.8 | P0.3 | Do not inline content through a redactor that mangles benign identifiers. |
| 9 | P2.9 | P2.8 | If the node stops writing the blueprint, the blueprint must still reach `synthesis` some other way. |
| 10 | P3.10 | — | Independent throughout; 10d resolves itself once P0.1 ships. |

A useful checkpoint after step 3: **re-run the same task** (`p9-09`) unchanged and diff the outcome. P0.1 + P0.2 alone should turn a silent `accept` into at least one rework round and a visible findings section, with no other change in the flow — the cheapest possible validation that the gating chain works end to end.

**Steps 1-6 are implemented** (see each item's own "Implemented" section for the decisions taken and the gaps found along the way). The checkpoint re-run has **not** been done yet, and it needs one manual step first: the packaged flows are what `worc install` copies, so a target repo's existing `.worc/flows/` still carries the old `gate_severity` default — and now also the old single `repository_analysis` node — and must be refreshed or hand-edited before the re-run measures anything.

## Not in this campaign

- **Model selection.** `claude-opus-4-8` → `claude-opus-5` at identical pricing is already [VF-16](../issues/runtime-validation-findings.md). Note only that for this flow the change must be made in `.worc/flows/deep_research.yaml` (seven pinned occurrences) — the node pins override `config.yaml` entirely, so editing the config alone does nothing.
- **Supervisor cadence and packet design.** Already designed in [../token-optimization/](../token-optimization/README.md) (P0/P1/P2). This run is additional evidence for it; P1.7 and P3.10c defer to those documents rather than restating them.
- **Defects in the target repo.** The false negatives the post-mortem found in `wastech-mdlint` (the CLI `bin` no-op, `SEC-003`'s path escape, and eight more) belong in that repo's own backlog. They appear in [postmortem.md](postmortem.md) only as evidence that the audit's headline conclusion was unsafe.

## Open

Two breadth passes over the un-audited surface (the four structured MCP tools; a requirement-by-requirement sweep of the target's `requirements/**`) had not returned when the post-mortem was written, so its false-negative count is a floor. This does not change any item above — the coverage argument for P1.4 is already made by the two confirmed release-blocking misses.
