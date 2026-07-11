# End-to-End Scenarios

Brief end-to-end flows that span multiple blocks. Details for each block are in its own document (see [block-registry.md](./block-registry.md)); the per-flow node graphs are in [flows/](./flows/index.md). This document covers only the order and connections. The pipeline body is driven by the [FlowEngine](./blocks/B28-flow-engine.md) — there is no hardcoded stage loop.

## Single-task processing (`run`, happy path)

1. [B01 CLI](./blocks/B01-cli-and-operator-commands.md) parses `run`, resolves and loads configuration ([B04](./blocks/B04-install-registry-and-config-discovery.md)/[B05](./blocks/B05-configuration.md)), builds the orchestrator, calls `run_task`.
2. [B16 Gate](./blocks/B16-task-parsing-and-validation-gate.md) parses and validates the task (§19); on reject — quarantine + [B08 Ledger](./blocks/B08-ledger-and-failure-reports.md), no branch.
3. [B06 Orchestrator](./blocks/B06-orchestrator-pipeline.md) acquires the single slot, registers the task in [B07 State Store](./blocks/B07-state-machine-and-store.md), resolves the flow for the task's `task_type` via [B29 Registry](./blocks/B29-flow-definition-and-validation.md) (validated, fail-closed), runs the isolation ([B25](./blocks/B25-security-policy.md)) and check ([B23](./blocks/B23-check-discovery.md)) preflights — both **before** the branch.
4. [B22 Git Manager](./blocks/B22-git-manager.md) prepares the task branch (`worc/<id>-<slug>` by default, or the task's validated `branch_name`); the orchestrator builds the node services/inputs + the [B31 Supervisor](./blocks/B31-supervisor.md) and hands the graph to [B28 FlowEngine](./blocks/B28-flow-engine.md).
5. The engine traverses the default `implementation` graph ([flows/implementation.md](./flows/implementation.md)): `refinement → planning → implementation → testing → review → documentation → publish`. Each agent node runs via [B17 Router](./blocks/B17-agent-router-and-fallback.md) → [B18 Providers](./blocks/B18-agent-providers.md) ([B30 runners](./blocks/B30-flow-node-runners.md)); prompts are assembled by [B15](./blocks/B15-prompt-templates.md), skills by [B13](./blocks/B13-skill-selection.md), typed output by [B12](./blocks/B12-hitl-and-typed-output.md). After each step the supervisor observes read-only.
6. `testing` selects the operator's command sets matching the diff ([B23](./blocks/B23-check-discovery.md) `select_check_sets`) and runs them all through the Check Runner ([B24](./blocks/B24-check-execution.md)) — a quality failure routes to `fixing`, while an incomplete gate (a required toolchain absent, or every selected check skipped) goes to manual; `review` is a read-only evaluator. `implementation`/`fixing` edits are guarded by the dangerous-diff classifier ([B14](./blocks/B14-dangerous-diff-guardrail.md)).
7. `publish`: the orchestrator's finalize hook writes the supervisor summary + moves the task file; [B22](./blocks/B22-git-manager.md) commits (code + audit), pushes, opens the PR — idempotently.
8. [B06](./blocks/B06-orchestrator-pipeline.md) performs terminal cleanup, writes one [B08 Ledger](./blocks/B08-ledger-and-failure-reports.md) record, sends a notification ([B26](./blocks/B26-notifications-telegram.md)). [B01](./blocks/B01-cli-and-operator-commands.md) maps the status to an exit code.

## Quality fix loop

A `testing` `fail` takes the `test_fix` loop edge to `fixing`; a `review` `rework` takes the `review_fix` loop edge to `fixing`; `fixing → testing` re-runs the gate. The engine charges each rework against its named loop and the single global counter, capped at `min(flow budget, agents.max_*)` ([B28](./blocks/B28-flow-engine.md)/[B09](./blocks/B09-fix-loop-control.md)). Exhausting any limit ends the run at `manual_action_required` with a failure report ([B08](./blocks/B08-ledger-and-failure-reports.md)). An **evaluator that cannot run at all** (no provider — infra/misconfig) likewise degrades to `manual_action_required` (the green diff is preserved on the branch for the operator, not discarded as `failed`); every infra terminal — `failed` or `manual_action_required` — now writes a failure report ([B30](./blocks/B30-flow-node-runners.md)/[B06](./blocks/B06-orchestrator-pipeline.md)). `fixing` resumes `implementation`'s editing session (`lineage_affinity`, durable in `editing_lineage`).

## Decomposition

If planning proposes a split that passes the deterministic gate ([B11](./blocks/B11-task-decomposition.md)), the orchestrator partitions the flow into pre / region / post ([B28](./blocks/B28-flow-engine.md) `partition_decomposition`) and runs the `sub_flow` region (`implementation → testing → review → fixing`) once per subtask, committing each ([B22](./blocks/B22-git-manager.md)) and resetting per-loop budgets between subtasks while the global counter accumulates. A subtask with a verified commit is never re-run.

## HITL (planning approval, dangerous diff)

- **Planning approval / question** — a typed `human_input` on a HITL-capable agent node triggers one durable round-trip via [B30](./blocks/B30-flow-node-runners.md) `HumanGate` over [B26](./blocks/B26-notifications-telegram.md); the interaction artifact ([B12](./blocks/B12-hitl-and-typed-output.md)) is persisted before asking, so a restart resumes it. Fail-closed on timeout/denial.
- **Dangerous diff** — a deletion/dependency change after a `workspace-write` edit ([B14](./blocks/B14-dangerous-diff-guardrail.md)) requires a durable approval (a matching planning pre-approval counts); on denial the node reconsiders once, then fails closed to manual.

## `watch` daemon

Between ticks ([B02](./blocks/B02-watch-daemon-and-scheduling.md)): `refresh_repo` fetch/ff-pulls the base branch (slot free), then `watch_once` resumes any in-flight task first, then processes pending tasks one at a time — back-to-back only with `auto_mode`; `manual_action_required` always blocks. A stop is honored between ticks: a `SIGTERM` (POSIX) and/or an `orchestrator.stop` sentinel file the loop polls (the cross-platform channel — `SIGTERM` is undeliverable cross-process on Windows).

## Resume (`resume`)

On startup [B10](./blocks/B10-recovery-and-resume.md) reconciles: >1 active → `manual_action_required`; one active → resume from the flow checkpoint (`current_node` + counters + fingerprint, hydrated from `node_runs`); a fingerprint mismatch restarts from the entry node; an incomplete terminal cleanup → finish it. `publish_operations` idempotency ([B07](./blocks/B07-state-machine-and-store.md)) means a resumed run never repeats a commit/push/PR; the flow is re-validated against the live config ([B29](./blocks/B29-flow-definition-and-validation.md)).

## `rerun` / `rerun --continue`

[B06](./blocks/B06-orchestrator-pipeline.md): `rerun` archives prior artifacts, resets the branch to base, clears per-attempt state ([B07](./blocks/B07-state-machine-and-store.md)), and re-runs from scratch; `rerun --continue` revives the terminal task and resumes at the persisted flow checkpoint node, reusing the branch and prior work. `--continue` tolerates the task's own uncommitted working tree once it has reached a code-operating stage, and takes two recovery controls: `--reset-fix-budget` (grant a fresh fix budget for an exhausted `max_fix_cycles`, keeping the global backstop) and `--from <node>` (re-enter at a chosen node of the checkpoint's flow).

## `finalize`

[B06](./blocks/B06-orchestrator-pipeline.md) records a task the operator handled out-of-band: terminal cleanup (fail-closed if unsafe), set the declared status (operator override), relocate the file, append a `manual` ledger record — no pipeline, no commit/push/PR.

## Research and audit flows

`task_type: deep_research` and `security_audit` resolve to their own packaged graphs ([flows/deep-research.md](./flows/deep-research.md), [flows/security-audit.md](./flows/security-audit.md)): the same engine, supervisor, and HITL machinery, but with a `citation` / `dependency_scan` checker ([B32](./blocks/B32-flow-checkers.md)), `network_policy`-granted research, and a documentation-PR or private-report publishing policy ([B30](./blocks/B30-flow-node-runners.md)).
