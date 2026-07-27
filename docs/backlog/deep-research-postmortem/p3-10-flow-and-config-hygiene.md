# P3.10 — flow and config hygiene: unreachable nodes, inert gates, dead session scope, cost trim

Priority: **P3** Status: **implemented (10c dropped)** Date: 2026-07-25 Source: [postmortem.md](postmortem.md) DR-8, DR-9, DR-12

A collection of small, independent items. None is individually worth a task; together they remove three pieces of configuration that cannot do anything and trim ~$0.9 per run.

**10c is dropped from this campaign** (operator decision, 2026-07-25): the supervisor's cost is not addressed here. The structural answer stays where it was already designed — [supervisor-observation-cadence-p1](../token-optimization/supervisor-observation-cadence-p1.md). With 10c out, the supervisor row of the 10e reasoning table goes with it; the remaining trim is ≈ −$0.7 per run.

## 10a — `refinement` is structurally unreachable

[`core/orchestrator.py:3120`](../../../src/wastech_orchestrator/core/orchestrator.py) resolves `derived.needs_refinement = completeness is not Completeness.COMPLETE`. `Completeness.COMPLETE` requires only a non-empty description plus an `## Acceptance criteria` section, so **any well-formed task file skips refinement**. On `p9-09` the ledger recorded `skip_reason = "disabled by task"` only because `_should_skip` checks the disabled set before the `when:` predicate — the node would have been skipped anyway.

What is lost: `.worc/flows/deep_research/refinement.md` is the strongest-written prompt in the set. It instructs decomposition along the roadmap, per-phase sub-questions, and anchoring each sub-question to where its evidence lives — and `repository_analysis.md` consumes it via `{?refinement_path}… cover every sub-question it lists{/refinement_path}`. A per-subsystem sub-question brief is a plausible partial fix for [P1.4](p1-4-audit-coverage-gate.md)'s coverage problem.

**Change:** drop `when: { fact: derived.needs_refinement }` from the `refinement` node so it runs unconditionally on this flow, and stop setting `nodes.refinement.enabled: false` per task. A "complete" task file and a _scoped_ task are different things, and for an audit-shaped question the scoping pass is where the value is. Cost: +$0.3–0.8.

## 10b — `external_research`'s gate can never fire

Same resolver, [`core/orchestrator.py:3124`](../../../src/wastech_orchestrator/core/orchestrator.py): `config.external_research = snapshot.doc.network_policy is not None`. `deep_research.yaml` sets `network_policy: research`, so `when: { fact: config.external_research }` is **always True**. Despite the `config.` namespace it is neither a config key nor a task field, and unknown facts silently resolve `False`, so an author cannot tell a typo from a working gate.

**Change:** none to the engine — the fact resolver is core code, not operator-authorable. At 3.2% of spend the node is cheap insurance, and per-task opt-out via `nodes: { external_research: { enabled: false } }` already works. Worth doing: document that `config.external_research` means "this flow has a network policy", not "this question needs external grounding", so nobody mistakes it for a relevance gate.

Related and worth recording: the node made **0** `WebSearch` calls despite the tool being granted, 2 real `WebFetch` calls to MDN (both quoted in the deliverable, both surviving into `sources.json`), and touched none of the nine authoritative-source families its role prompt names. The cause is structural — it sits downstream of `repository_analysis` and validates only what that node found, so six upstream findings gave it exactly one external dependency to check. If [P1.4](p1-4-audit-coverage-gate.md)'s node split lands, revisit whether `external_research` should carry its own standing brief ("validate the stack's core contracts regardless of upstream findings") rather than being purely reactive.

## 10c — the per-step supervisor is unread overhead — **DROPPED**

Kept below as evidence only; no change is made under this campaign.

$0.72 (5.6% of task cost) and ~76 s of serialized dead time, for 7 step notes plus the finalize turn. Ten tool calls across all eight runs; runs 000077, 000080, 000081 and 000082 made **zero**. Six of seven notes contain an explicit "no corrections needed".

The step notes have exactly one consumer in the codebase — `_recover_from_digest` ([`core/supervisor.py:661-687`](../../../src/wastech_orchestrator/core/supervisor.py)), used only when the session dies. Here `recovered_from_digest: false`, so **nothing ever read them**. Its rubric (`roles/supervisor.md`) names two detection targets — repeated fix-cycle failure and out-of-scope file drift — and with `fix_iterations = 0` on a read-only flow, neither could fire.

**Change:** `SupervisorConfig` has no `enabled` flag, so the layer can only be cheapened. Set `supervisor.reasoning: medium → low` in the target config. The structural answer — a flow-local `supervision.role_file` so a research flow can state a rubric that can actually fire, and an observation cadence — is already designed in [supervisor-observation-cadence-p1](../token-optimization/supervisor-observation-cadence-p1.md); this run is additional evidence for it, not a new proposal.

## 10d — `resume_own_lineage` is dead configuration, and the prompt says otherwise

`critical_review` is the only node using `session_scope: resume_own_lineage`. Its `request.json` argv contains no `--resume` and no `--session-id` (the supervisor's argv does), and the `node_lineage` row's `updated_at` equals the node's finish time — written at the end, never read. `_resume_node_lineage()` returns `None` when no prior row exists, which on round 1 is always.

Meanwhile `critic.md:14-15` asserts _"**You keep your own session across rounds**, so do not repeat a point you already raised."_

**Change:** no engine change. The field becomes functional the moment [P0.1](p0-1-evaluator-gate-severity.md) makes a second round possible. The prompt sentence is handled in [P1.5](p1-5-research-role-prompts.md) item 7.

## 10e — reasoning trim

Per-node fit from the run, holding [VF-16](../issues/runtime-validation-findings.md)'s model discussion separate:

| Node | Now | Proposed | Why |
| --- | --- | --- | --- |
| `architecture_design` | xhigh | high | An organizing pass over already-gathered evidence; 13/13 of its repository reads were re-reads, 0 new findings. |
| `fact_verification` | high | medium | Returned `accept` with zero findings after the deterministic checker had already resolved all 41 citations. |

(The supervisor row — `medium → low` — is dropped with 10c.)

≈ −$0.7 per run of this shape. Do **not** lower `repository_analysis` — its problem is scope, not depth, and lowering effort would make it worse.

## 10f — target config re-sync

Not orchestrator defects; hygiene on the validation target:

- `.worc/config.example.yaml` is at `schema_version: 24` against a packaged `31` — seven versions of missing guidance (`repo.branch_mode`, `tasks/preparing/`, `security.disable_read_isolation`, `providers.claude.allow_native_memory`, the codex `sandbox:` → `permission_profile:` move), while `config.yaml`'s own header directs the operator to read it. Re-copy from `packaged/config.example.yaml`.
- `agents.retry.max_blocked_s: 3600.0` against a current default of `21600.0` — a mid-run rate limit would fail the task ~5 h early. Did not fire here.
- ~~`agents.providers.codex.model: gpt-5.4` against packaged `gpt-5.5`.~~ **Withdrawn 2026-07-27 — the discrepancy does not exist.** The packaged value is `gpt-5.4` in both places that carry it (`install/config_writer.py`'s `_PROVIDER_DEFAULTS` and `packaged/config.example.yaml`), and no `gpt-5.5` appears anywhere in the repository. It was `gpt-5.5` once and was deliberately changed to `gpt-5.4` in commit `5b36af0` on **2026-07-11**, a fortnight before this campaign — so the claim was already untrue when written, presumably checked against a remembered value rather than the file. The target's config is correct on this key; nothing to re-sync.
- The packaged `config.example.yaml:266-270` suggests Opus for the _supervisor_ and Sonnet as the _primary provider_ — the inverse of what this operator configured, and the operator's arrangement is the better one. Correct the packaged example.

## 10g — `deep_research` runs no repository command before committing

Source: [postmortem.md](postmortem.md) DR-13. Related: [VF-11](../issues/runtime-validation-findings.md).

The flow's only `checks` node is `checker: citation`. There is no `command_profile` node anywhere in the graph, unlike `implementation.yaml:72-73`. So the flow writes Markdown into the target repository, commits it, and opens a PR without running anything the repository defines.

On `p9-09` that turned the target's CI red: `npm run format` now fails on 4 files, two of them (`report.md`, `report-structure.md`) committed by the `p9-09` deliverable itself. Everything else on the branch is green — typecheck, lint, build, and 614/614 tests.

**Change:** add a `command_profile` checks node after `synthesis` (or after `citation_check`), with a fail edge back to `synthesis`. A research flow does not need `typecheck`/`test`, but it does need whatever validates the files it is about to commit — here `npm run format`. Reference a **named** command set (e.g. `docs`) rather than hardcoding commands, and skip cleanly when the target defines no such set. Check first whether `command_profile` already supports a named-set selector before proposing schema changes.

The other half — the target's `checks.command_sets.default` listing `typecheck`/`lint`/`test`/`build` and omitting `format`, which is why two `implementation`-flow tasks also slipped through — is already VF-11 and is fixed in the target's `config.yaml`, not here.

## Acceptance

- No `deep_research` node carries a `when:` predicate that cannot change the outcome, or an unreachable-by-construction gate, without a comment saying so.
- No role prompt asserts session continuity, network use, or a mechanism the node does not have.
- A `deep_research` run cannot commit files that fail the target's own documentation gate.
- The target's `config.example.yaml` matches the packaged schema version.

## Depends on

10d resolves itself once [P0.1](p0-1-evaluator-gate-severity.md) ships. 10b's second half is worth revisiting after [P1.4](p1-4-audit-coverage-gate.md). Everything else is independent.

## Implemented

2026-07-27, on the packaged flow and the shipped operator docs. Per sub-item:

**10a — `refinement` runs unconditionally.** The `when:` is gone from the packaged node and the node carries a comment saying why (a formedness check is not a scoping check). The per-task escape is the node-disable switch, which is checked before any predicate anyway, so nothing was lost. The second half of the change — "stop setting `nodes.refinement.enabled: false` per task" — is a **target-repo task file**, not on this branch; it is in the follow-ups.

**10b — documented, no engine change.** The `external_research` node now carries a comment stating that `config.external_research` means "this flow declares a `network_policy`", and the shipped flow reference gained a `Conditional nodes (when:)` subsection with both facts spelled out, the silent-`false`-on-unknown behavior, and the instruction to comment any predicate that cannot change the outcome. That table is the item's real deliverable: neither fact is a relevance test, and nothing in the guide said so before.

**10d — closed by P0.1, plus a comment.** No engine or prompt change was needed: the field became functional the moment a `medium` finding could force a second round, and the prompt sentence had already been hedged by [P1.5](p1-5-research-role-prompts.md) item 7 ("if you can see your own earlier round(s)"). The node now carries a comment recording that round 1 always starts fresh, so the next reader does not re-derive it from the absence of `--resume` in an `argv`.

**10e — half declined, and the other half is a comment.** The packaged flow pins no `reasoning` at all: every value in the item's table is a **target-repo** pin, and against the packaged default (`high`) `architecture_design` is already at the proposed value. What was changed is the commented example the operator copies (`xhigh` → `high`, with the measured reason). `fact_verification`'s `high` → `medium` was **not** made: the evidence for it ("returned `accept` with zero findings") predates [P1.5](p1-5-research-role-prompts.md) and [P1.6](p1-6-citation-checker-strictness.md), which between them made that node fetch every external source, resolve the `weak`/`uncheckable` verdicts, and run the under-claiming sweep that is the campaign's answer to the headline false negative. Cutting its effort right after widening its remit measures a node that no longer exists. So the ≈ −$0.7 estimate does not apply to this branch; the live trim is a target-config change and is in the follow-ups.

**10f — already satisfied on the packaged side.** The one bullet that lives in this repository (the example suggesting Opus for the supervisor and Sonnet as primary) was corrected by [P1.7](p1-7-research-finalize-summary.md): the packaged `supervisor.model` is Sonnet and the block now carries the "spend on the PRODUCER nodes, not here" reasoning. The other three bullets — `schema_version` drift, `max_blocked_s`, the stale Codex model — are all edits to the target's own `.worc/`, tracked in the follow-ups. No file was churned to re-do what is already right.

**10g — a `document_checks` node, no schema change.** The item asked to check first whether `command_profile` supports a named-set selector before proposing one. It does not — and it does not need one: selection is by **diff glob** (`select_check_sets` matches each set's `paths` against the changed paths), so a set matching the committed documents runs and a repository with no matching set selects nothing and passes vacuously. That is exactly the "reference a named set, skip cleanly when absent" behavior the item wanted, reachable from config alone. The node sits on the `citation_check → pass` edge, before the two expensive evaluators, with `fail → synthesis` (budget 1). One operator-facing caveat is now in both the flow comment and `config.example.yaml`: name a _checking_ command, because a command that rewrites files trips the core's green-but-dirtying guard and parks the task.

**Not done.** Nothing in 10c (dropped by operator decision), and nothing in a target repository — every 10f bullet except the packaged example, 10a's per-task `enabled: false`, and 10e's live reasoning pins are edits to a `.worc/` tree that is not on this branch.
