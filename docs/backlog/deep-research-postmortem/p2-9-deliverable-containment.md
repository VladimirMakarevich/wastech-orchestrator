# P2.9 — keep intermediates out of the documentation PR

Priority: **P2** Status: **implemented (option 1 + 10g; option 2 withdrawn; the profile downgrade declined)** Date: 2026-07-25 Source: [postmortem.md](postmortem.md) DR-11

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

## Implemented

2026-07-27, option 1 as a prompt change, plus 10g from [P3.10](p3-10-flow-and-config-hygiene.md). One part of option 1 was declined.

**The instruction is gone.** `architecture_design.md` no longer tells the node that the deliverable directory is its writable path; it tells it to write nothing at all, and says why in the terms that matter to a model: everything in that directory is committed and opened as a pull request, so a working file ships as if it were part of the answer, and two documents that disagree are worse than one. `synthesis.md`'s adjacent phrasing ("write nothing anywhere else", which still permitted a third file _inside_ the directory) is now "exactly these two files, and no third file — not there, not anywhere else".

**Where the blueprint goes instead.** The node's channel is its closing message, so the prompt now says that in one line and at length: the message _is_ the blueprint, at full size, and nothing left out of it survives the step. This is the part option 1 understated — it says "return its structure through the output schema only", but a plain author node has **no** output schema (its typed contract is `none`), so there was never a structured field to return it in. `synthesis` reads it through `{architecture_design_path}`, which it already did. Two prompts also stopped asserting "the typed structured result required by the output schema", which neither node has — that is [P3.10](p3-10-flow-and-config-hygiene.md)'s "no role prompt asserts a mechanism the node does not have", found while editing these two files.

**Declined: the `read-only` downgrade.** Option 1's premise is that the write grant is "an unused write grant". It is not, any more: [P1.5](p1-5-research-role-prompts.md) (accepted and implemented the day _after_ this item was written) gave this node an empirical-confirmation remit — "unlike the analysis passes upstream you have a shell, so a claim that a command exits non-zero can be settled in one command instead of argued from a citation" — and on Claude the `read-only` profile's tool set is `Read`/`Glob`/`Grep`, i.e. no shell. Downgrading would silently delete the capability the later item deliberately added, which `.agents/rules/security.md` forbids as a first-class rule: restrictions only where a real risk requires them, and then the least restrictive one. The risk here is already closed by the prompt edit plus the containment guard, which confined every write correctly in the run that produced this item. The node keeps `workspace-write`, and the flow now carries a comment saying it is held for the shell and not for writes. `git_evidence` is not an alternative: it is rejected on a `workspace-write` node and grants only the git verbs.

**The operator confirmed the decline on 2026-07-27**, so this is settled and the node keeps `workspace-write`. Two facts, checked rather than assumed, decided it: Claude is `primary` in the packaged config, so the node really does run where `read-only` means no shell at all; and `commit_code` stages `changed_code_paths()` with no filename filter, so a stray file really would ship — the risk is real, it is just the hypothetical half of the trade. What settled it is which failure is visible: a stray file is legible in the pull request and now passes the target's own gate (10g) before it lands, while a claim left unverified because the shell was removed looks exactly like a verified one. `.agents/rules/security.md` points the same way — a real risk gets the least restrictive fix, not a capability removed for a lapse that the prompt now forbids in terms.

Recorded for a Codex-based instance, since it is not obvious: on **Codex** the `read-only` profile keeps the shell (workspace mounted `read`, network off), so there the downgrade would give both properties at once. It is not right for a _packaged_ flow — pinning a node to a provider that may not be in `agents.allowed` — but an operator running Codex can take it in their own `.worc/flows/deep_research.yaml`.

**Acceptance, honestly.** Criterion 1's `read-only` clause is **retired**, not outstanding: the node writes nothing (the part that mattered) and keeps the profile by operator decision. Criteria 2 and 3 are met — 10g's gate is in the graph, and no engine path filters a commit by name (nothing on the publish path was touched). Criterion 4 (the citation checker's declared manifest field) was already closed by [P1.6](p1-6-citation-checker-strictness.md). The end-to-end "commits its report and no intermediate" cannot be observed from a unit suite: it is the campaign's pending checkpoint re-run. The `## Test` line above still names a `read-only` fixture; that half of it is retired with the clause.
