# `deep_research` post-mortem campaign (2026-07-25)

Status: **archived 2026-08-09 — all eleven items implemented (P0.1, P0.3, P0.2, P1.6, P1.5, P1.4, P1.7, P1.4a, P2.8, P2.9, P3.10); P2.8 piece 3 and P3.10's 10c are deliberately out. What remains is the checkpoint re-run, which needs a target-repo `.worc/` refresh first — see [follow_ups.md](follow_ups.md)** Date: 2026-07-25 Owner: Vladimir Makarevich

Residue from the items that have landed — open decisions, watch items, the `.worc/` refresh every target repo needs, and what the `main` docs refresh must pick up — is collected in [follow_ups.md](follow_ups.md).

This folder groups everything that came out of the post-mortem of `p9-09-full-solution-deep-audit`, the **first and only production run of the `deep_research` flow**, into a single campaign with one execution order. The analysis is in [postmortem.md](postmortem.md); the files below are the implementable tasks it produced.

These documents are design detail, not an implementation contract, and must not override the hard invariants in [../../../CLAUDE.md](../../../../CLAUDE.md) / [../../../AGENTS.md](../../../../AGENTS.md) / [../../../.agents/rules/](../../../../.agents/rules/).

## Campaign-wide constraint: flow-agnostic by construction

Every item here was found through `deep_research`, and none of them may be implemented **for** `deep_research`. The orchestrator must serve arbitrary operator-authored flows — a flow this repository has never seen, with node ids, role files and deliverables it does not know — exactly as well as the packaged ones. Concretely, for every item below:

- **No code branches on a flow name, a node id, a role-file name, or a path convention.** No `if flow.name == "deep_research"`, no `docs/research/{task_id}/` baked into the engine, no node id treated as special. Behavior is selected by a declared field, never by an identity.
- **Every mechanism is reachable declaratively** — a flow YAML field, a `flow.defaults` entry, a role file, or a config key — so an operator gets it in their own flow without touching Python.
- **A flow that declares nothing still behaves sanely.** Defaults are chosen for the general case; a knob's absence is never a silent failure mode (that is the P0.1 lesson).
- **Fixed in-code lists are a smell.** Where a set of names must exist in code (allowed prompt variables, checker kinds, severity ranks), it is a documented allowlist with an operator-visible extension story, not an incidental tuple that a user flow silently falls outside of.

Item-specific traps this rules out: P1.4's subsystem taxonomy (target-only until it is expressed generically), P1.7's finalize lens (a `flow.supervisor.finalize_role_file` field, never a flow-name branch), P2.8's produced-file channel (a node-declared path, not a filename convention) and its footer slot (generic upstream-output, not a research-shaped slot), P1.6's citation statuses (checker-level, not flow-level), and the hardcoded `{repo}/docs/research/{task_id}/report.md` strings that sat in the packaged verifier and critic prompts — which were themselves an instance of the anti-pattern. Those are gone: the evaluators resolve the deliverable through `{synthesis_path}`, which P2.8's `output_file:` field points at the report instead of at the node's sign-off. The one remaining occurrence of the convention is in `synthesis.md`, where the writing node names its own two deliverables — a flow author naming their own output, not the engine imposing a shape.

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
| P1.4a | [Read-only git evidence for an audit node](p1-4a-read-only-git-evidence.md) | **Implemented.** A `read-only` node may declare `git_evidence: true` and run the read-only git verbs, gated by the operator's `security.allow_git_evidence` (default off). Claude scopes a shell to those verbs and write-denies the clone in its sandbox; Codex needed no change | new capability | both providers + schema/validator/preflight |
| P1.5 | ✅ [Fix the research role prompts](p1-5-research-role-prompts.md) | Verifier rubric + full `sources.json` coverage + an under-claiming watch-item; drop the critic's false promises; class-sweep for producers | prompt edits | prompts (target + packaged) |
| P1.6 | ✅ [Make the cited line authoritative](p1-6-citation-checker-strictness.md) | Drop the `or` fallback (or emit `weak`); a missing snippet is `uncheckable`; publish `citation.json` on the pass path too | small | orchestrator |
| P1.7 | ✅ [Give `deep_research` its own finalize lens](p1-7-research-finalize-summary.md) | `flow.supervisor.finalize_role_file` (the document's `supervision:` key does not exist) + a no-fabrication rule + the recorded gate verdicts rendered into the finalize prompt | prompt + flow | packaged flow + orchestrator |
| P2.8 | ✅ [Let a node's real output cross the edge](p2-8-node-output-handoff.md) | A node declares `output_file` and the flow publishes that file, not the sign-off; evaluators got the node-output channel with P1.4. The footer slot (piece 3) is **not** done — its own review | medium | orchestrator |
| P2.9 | ✅ [Keep intermediates out of the PR](p2-9-deliverable-containment.md) | `architecture_design` writes nothing and hands on its whole blueprint (+ 10g); no commit allowlist — filenames stay the flow author's choice. The `read-only` downgrade is **declined** — it would delete P1.5's shell | small | prompt + flow |
| P3.10 | ✅ [Flow and config hygiene](p3-10-flow-and-config-hygiene.md) | `refinement` runs unconditionally, the inert gate and the session scope are documented in place, a `command_profile` gate before the evaluators, the `when:` facts written up. `fact_verification`'s reasoning trim is **declined** | small | flow + config |

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
| 7 | ✅ P1.7 | P0.2 | The finalize lens needs findings to render. |
| 8 | ✅ P1.4a | — | Spun out of P1.4 when its change 3 turned out to redefine what `read-only` means rather than add a flag. Accepted 2026-07-26, moved ahead of P2.8 because the missing history evidence is judged to have contributed to the two release-blocking false negatives, and **implemented** there. Independent of every other item. |
| 9 | ✅ P2.8 | P0.3 | Do not inline content through a redactor that mangles benign identifiers. Pieces 1-2 shipped without inlining anything (the channel stayed a path), so the dependency never bound; piece 3, the only inlining variant, is not scheduled. |
| 10 | ✅ P2.9 | P2.8 | If the node stops writing the blueprint, the blueprint must still reach `synthesis` some other way. It does, on the channel it already had — the node's own output — now that the prompt says the message _is_ the blueprint. |
| 11 | ✅ P3.10 | — | Independent throughout; 10d resolved itself once P0.1 shipped. |

A useful checkpoint after step 3: **re-run the same task** (`p9-09`) unchanged and diff the outcome. P0.1 + P0.2 alone should turn a silent `accept` into at least one rework round and a visible findings section, with no other change in the flow — the cheapest possible validation that the gating chain works end to end.

**Every step is implemented** (see each item's own "Implemented" section for the decisions taken, the two proposals declined, and the gaps found along the way). The checkpoint re-run has **not** been done, and it needs one manual step first: the packaged flows are what `worc install` copies, so a target repo's existing `.worc/flows/` still carries the pre-campaign graph and defaults and must be refreshed before the re-run measures anything. That refresh has accumulated behind it everything the campaign changed in the packaged flow and prompts — the list is in [follow_ups.md](follow_ups.md).

## Not in this campaign

- **Model selection.** `claude-opus-4-8` → `claude-opus-5` at identical pricing is already VF-16. Note only that for this flow the change must be made in `.worc/flows/deep_research.yaml` (seven pinned occurrences) — the node pins override `config.yaml` entirely, so editing the config alone does nothing.
- **Supervisor cadence and packet design.** Already designed in `../token-optimization/` (P0/P1/P2). This run is additional evidence for it; P1.7 and P3.10c defer to those documents rather than restating them.
- **Defects in the target repo.** The false negatives the post-mortem found in `wastech-mdlint` (the CLI `bin` no-op, `SEC-003`'s path escape, and eight more) belong in that repo's own backlog. They appear in [postmortem.md](postmortem.md) only as evidence that the audit's headline conclusion was unsafe.

## Open

Two breadth passes over the un-audited surface (the four structured MCP tools; a requirement-by-requirement sweep of the target's `requirements/**`) had not returned when the post-mortem was written, so its false-negative count is a floor. This does not change any item above — the coverage argument for P1.4 is already made by the two confirmed release-blocking misses.
