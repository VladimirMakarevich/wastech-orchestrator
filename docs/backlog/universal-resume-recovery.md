# Universal resume/recovery — every terminal task must have a supported way forward

Status: **proposed** (2026-07-10) Date: 2026-07-10 Owner: Vladimir Makarevich

Every task that stops in a terminal state (`failed`, `manual_action_required`, or a parked/`blocked` state) must have at least one **supported, safe, operator-driven way to continue** — regardless of _why_ it stopped. Today resumability is real but fragmented: the two recovery paths (`rerun` fresh-from-base and `rerun --continue` from the checkpoint) each carry hard gates that, in common combinations, leave no forward path at all. This ADR defines a **broad operator-driven resume surface** — grant fresh fix budget, tolerate the task's own work-in-progress, re-enter at a chosen node, overlay a provider/model for the resumed leg, force forward past a stuck evaluator (bounded + audited), and turn hard refusals into guided reconciliation — with a phased plan. This is the **operator-driven** half of resilience; the **automatic** infra recovery (park + timed auto-resume for rate-limit / transient classes) is owned by its sibling [reliable-rate-limit-handling](reliable-rate-limit-handling.md) and assumed here. This is a stake-in-the-ground for a capability, with Phase 0 buildable immediately.

## The problem

The catch-22 that motivated this, from the p6-04 run ([post-mortem](../analysis/p6-04-config-writer-schema-run-analysis.md)): a task on the operator's `feat/p6-init` branch exhausted `max_fix_cycles` (a transient rate-limit turned `fixing` into a no-op — see the sibling ADR), and now **neither recovery path works**:

- **Fresh `rerun` is refused** because the branch is operator-owned (`branch_mode: existing`): a fresh rerun would reset a branch the orchestrator does not own ([orchestrator.py:955-963](../../src/wastech_orchestrator/core/orchestrator.py#L955-L963), guarded again in [`rerun_task`:1008-1013](../../src/wastech_orchestrator/core/orchestrator.py#L1008-L1013)). Fresh rerun is also the _only_ path that resets the loop counters ([`reset_task_for_rerun`, state_store.py:736-780](../../src/wastech_orchestrator/state_store.py#L736-L780)).
- **`rerun --continue` re-fails in one pass** because it deliberately keeps the counters to reuse prior work ([`revive_task_for_continue`, state_store.py:783-800](../../src/wastech_orchestrator/state_store.py#L783-L800)) and the resume engine hydrates them from the checkpoint ([`hydrate_run_state`, recorder.py:86-104](../../src/wastech_orchestrator/core/flow/recorder.py#L86-L104)); it re-enters at `review` with `review_fix=15 = max_fix_cycles`, so the first `review → rework` immediately re-exhausts ([`_charge_rework`, engine.py:282-299](../../src/wastech_orchestrator/core/flow/engine.py#L282-L299)) — the now-healthy `fixing` node never runs.
- **`--continue` is _also_ refused by the task's own uncommitted WIP** when it re-enters `review`/`fixing` (a dirty tree is tolerated only when the resume re-enters the `publish` region — [orchestrator.py:944-952](../../src/wastech_orchestrator/core/orchestrator.py#L944-L952)). The staged implementation (the legitimate input to review/fix) reads as "unaccounted changes" and blocks the resume.

Generalizing beyond this one case: the recovery surface has no way to **supply what progress actually needs**. There is no way to grant fresh fix budget, to re-enter at a different node (re-implement, re-review, jump to publish), to switch the provider for the resumed leg (e.g., to codex after a Claude limit), or to force forward past a review that is stuck on a debatable judgment call (the p6-04 CI-workflow `uses:`↔self-contained flip-flop is exactly this). The only escape today is `finalize` — which _records_ an out-of-band outcome but never _drives_ the task to completion — or a manual `state.db` edit, which is fragile and off-process. For a system whose whole job is unattended, long-running work that routinely crosses transient failures, "sometimes there is no supported way to continue" is the wrong default.

## Constraints

The **state machine** already sanctions a terminal → active flip as an out-of-band, operator-driven transition ([`revive_task_for_continue`](../../src/wastech_orchestrator/state_store.py#L783-L800) sets status directly, no `assert_transition`); the new controls extend that path and must keep its audit trail. **Bounded termination is non-negotiable**: granting fresh fix budget must never remove the ultimate stop — the global `max_total_fix_iterations` backstop (or an explicit, bounded per-attempt raise) still guarantees the loop ends. **"A task does not patch the flow graph"** holds, with exactly one sanctioned bounded exception — per-task `nodes.<id>.enabled: false` ([per-task stage-skip](task-node-model-override.md) / memory `per-task-stage-skip-exception`); the operator force-forward / skip-node-once control here is a _sibling_ bounded exception and must be framed the same way — explicit, per-attempt, audited, never a general graph mutation and never automatic. **Evaluator authority** is preserved by making an override an explicit recorded _human_ decision (a synthetic operator verdict in `evaluations` / the audit log), not a silent bypass an agent or a config key can trigger. The **flow fingerprint** is taken as-is on recovery (recovery does not re-resolve the flow from config — [hydrate_run_state](../../src/wastech_orchestrator/core/flow/recorder.py#L86-L104)); any control that changes the target node must not silently run a different graph than the checkpoint's fingerprint. The **provider abstraction** holds: a per-leg provider/model overlay is a route overlay (CLI/config), not the core learning CLI syntax — the same seam as the [per-node model/reasoning/provider override](task-node-model-override.md). **Only the orchestrator commits/pushes/opens PRs**, so an opt-in "commit my WIP before continue" is an orchestrator action (idempotent `commit_code`), never the agent's. **No secrets** in the new artifacts; **greenfield MVP** (no migration machinery); **cross-platform** (git via `run_process`, `pathlib`).

## Alternatives considered

| Option | Why rejected |
| --- | --- |
| Do nothing | The common "operator-branch + exhausted budget + dirty WIP" combination dead-ends; operators hand-edit `state.db` (fragile, off-process) or abandon the work. Contradicts the obligation to always offer a way forward. |
| Fix only the p6-04 counter (grant fresh fix budget, nothing else) | Solves one case; leaves the dirty-WIP refusal, the "re-enter elsewhere", the provider-switch, and the stuck-verdict cases unaddressed. The problem is a _class_, not a single gate. |
| Rely on `finalize` alone (record out-of-band) | `finalize` records a terminal the operator produced by hand; it never drives the orchestrator's own fix/review/publish machinery. It is "write it down", not "continue". |
| Make recovery fully automatic for every failure class | Over-reach: genuine quality failures, ambiguous foreign WIP, and debatable evaluator verdicts need a human decision. Automatic recovery is right _only_ for infra transients — and that is the rate-limit ADR's job, deliberately split out. |
| **Chosen: a broad operator-driven resume surface (fresh budget · WIP tolerance · re-enter-at-node · provider overlay · bounded audited force-forward · guided reconciliation), phased, with automatic infra recovery delegated to the rate-limit ADR** | Turns "sometimes no way forward" into "always a supported lever", while keeping automatic recovery scoped to the class that can be safely automated. |

## Decision

Build a **universal operator-driven resume surface** on top of the existing checkpoint machinery, so that from any terminal state the operator has a bounded, audited way to supply exactly what progress needs. The capability set:

1. **Resumability guarantee.** Every terminal carries a complete, replayable checkpoint (`current_node` + `loop_counters` + `flow_fingerprint` in `tasks`; the `node_runs` trace; `editing_lineage` sessions; `publish_operations` idempotency). Invariant to enforce and test: no terminal state exists without at least one supported forward command.
2. **Fresh fix budget on continue** (`--grant-cycles N` / `--reset-fix-budget`). Reset the _consecutive_ fix counters (or grant N more) so a `max_fix_cycles`-exhausted task can run `fixing` again — bounded still by the global `max_total_fix_iterations` backstop (or an explicit, capped per-attempt raise). Directly resolves the p6-04 catch-22.
3. **WIP tolerance on re-entry.** When `--continue` re-enters `review`/`fixing` (not only `publish`), tolerate the task's _own_ uncommitted work — it is the legitimate input to those nodes — while still refusing genuinely foreign unaccounted changes.
4. **Re-enter at a chosen node** (`--from <node>`). Re-plan, re-implement, re-review, or jump to publish instead of only the persisted checkpoint node — bounded to nodes in the checkpoint's resolved flow, audited.
5. **Per-leg provider/model overlay** (`--provider` / `--model` / `--reasoning`). Best-effort route overlay for the resumed leg (e.g., switch `fixing` to codex after a Claude limit), degrading gracefully — the same seam as the per-node override design.
6. **Bounded, audited operator force-forward** (`--accept-current`, `--skip-node <id>` once). Take the forward edge past a stuck evaluator a single time, recording a synthetic _operator verdict_ in `evaluations` / the audit log. Extends per-task `allow_review_skip`; explicit, per-attempt, never automatic — the escape for a review stuck on a debatable judgment call.
7. **Guided blocker reconciliation.** Turn today's hard refusals (dirty tree, remote branch / open PR, ambiguous source, no checkpoint) into actionable guidance naming the exact fix, plus opt-in auto-resolution where safe (`--commit-wip` to commit the task's own WIP before continue). `--dry-run` shows the resolved plan and any remaining refusals.
8. **Escape-hatch continuity.** Keep `finalize` for out-of-band recording; add "resume-then-finish" so after the operator unblocks, the orchestrator drives to a clean terminal + cleanup rather than leaving the task half-done.

We do this because the orchestrator's core promise — unattended, long-running work across transient failures — is void if a stop can be unrecoverable; the cost is a wider `rerun` surface and a few new state transitions that must each preserve the bounded-termination and evaluator-authority invariants. Not building it (or half-building it) keeps operators editing `state.db` by hand and losing work on the most common failure shapes.

### Phased plan (build order)

- **Phase 0 — unblock the common dead-end (smallest change, biggest value):** capabilities **#2** (fresh fix budget on continue) + **#3** (WIP tolerance on review/fixing re-entry). Together these make the "operator-branch + exhausted budget + dirty WIP" case resumable — the exact p6-04 shape.
- **Phase 1 — redirection:** **#4** (`--from <node>`) + **#5** (per-leg provider/model overlay). Lets an operator resume differently after addressing the real cause (re-implement, switch provider).
- **Phase 2 — bounded override:** **#6** (`--accept-current` / `--skip-node once`) with audit records. The judgment-call escape.
- **Phase 3 — DX + closure:** **#7** (guided reconciliation, `--commit-wip`, richer `--dry-run`) + **#8** (resume-then-finish).
- **Cross-cutting (not in this ADR):** automatic park + timed auto-resume for rate-limit / transient infra classes — delivered by [reliable-rate-limit-handling](reliable-rate-limit-handling.md); this surface assumes it.

## Open questions

Flow-fingerprint drift: if the flow YAML changed since the checkpoint, does `--from` / resume re-resolve the flow (risking node-id mismatch) or keep the stored fingerprint (risking an outdated graph)? Likely: keep the fingerprint unless the operator passes an explicit, bounded `--re-resolve`.

`--grant-cycles` semantics: reset the consecutive counters to 0, or add +N on top? And does it raise the global `max_total_fix_iterations` backstop for this attempt (capped), or must the grant stay under the existing global ceiling so termination is never weakened?

Where the audited override is recorded: a synthetic operator verdict row in `evaluations` (reuses existing traceability) versus a dedicated audit surface. Prefer `evaluations` for one source of truth on why a node advanced.

Interaction with decomposition: do the resume controls apply per-subtask (re-enter a specific subtask's region, grant that subtask fresh budget) or only at the whole-task level in v1?

CLI ergonomics: many discrete flags versus one `--recover <spec>`. Discrete flags are clearer for the phased MVP; revisit if the surface grows.

`--commit-wip` authorship: when the orchestrator commits the agent's uncommitted WIP to unblock a continue, what commit message / author / audit entry, and does it belong on the task branch exactly as `commit_code` would produce it?

## Implementation notes

CLI — extend the `rerun` subparser ([cli.py:427-448](../../src/wastech_orchestrator/cli.py#L427-L448)) with the new controls, all consulted only alongside `--continue`, all surfaced in `--dry-run`: `--grant-cycles N` / `--reset-fix-budget`, `--from <node>`, `--provider/--model/--reasoning`, `--accept-current`, `--skip-node <id>`, `--commit-wip`.

Recovery planning — [`plan_rerun` (orchestrator.py:886-985)](../../src/wastech_orchestrator/core/orchestrator.py#L886) computes the controls into `RerunPlan` and _downgrades_ a refusal to guidance when a control resolves it (e.g., `--commit-wip` clears the dirty-tree refusal at [944-952](../../src/wastech_orchestrator/core/orchestrator.py#L944-L952); `--grant-cycles` clears the saturated-counter dead-end).

Revive path — [`continue_task` (orchestrator.py:1022-1050)](../../src/wastech_orchestrator/core/orchestrator.py#L1022) + [`revive_task_for_continue` (state_store.py:783-800)](../../src/wastech_orchestrator/state_store.py#L783-L800): apply a _partial_ counter reset (not the full `reset_task_for_rerun`), the `--from` checkpoint-node override, and the audited override flags before `resume()`.

Run-state hydration — [`hydrate_run_state` (recorder.py:86-104)](../../src/wastech_orchestrator/core/flow/recorder.py#L86-L104): honor the per-attempt counter grant and the from-node when rebuilding `FlowRunState`; the grant interacts with [`_charge_rework` / `_reset_loops_at` (engine.py:282-299)](../../src/wastech_orchestrator/core/flow/engine.py#L282-L299) — keep the global backstop intact.

WIP tolerance — broaden the `resume_in_publish` gate (the dirty check at [orchestrator.py:944-952](../../src/wastech_orchestrator/core/orchestrator.py#L944-L952)) so re-entry at review/fixing accepts the task's own tracked changes while still refusing foreign paths (reuse `unaccounted_dirty_paths`' artifact-exclusion logic).

Provider/model overlay — reuse the per-node override overlay seam from the [per-node model/reasoning/provider override](task-node-model-override.md) design (`_apply_overrides` in the engine driver), scoped to the resumed leg.

Audit — record operator overrides in the `evaluations` table and the memory/audit log; a synthetic operator verdict keeps "why did this node advance" answerable.

Config — an optional `agents.resume` block (default grant-cycles cap; whether force-forward is allowed at all) with a schema-version bump; tests via `/fake-cli` for each control (grant-budget → fixing runs; WIP-tolerant continue at review; `--from`; force-forward records a verdict and advances; dry-run shows resolved plan).
