# HITL session resume & planning autonomy

Status: **P0 done (in-process resume) · P2 documented (no code knobs)** Date: 2026-06-26 (resolved 2026-06-27) Owner: Vladimir Makarevich

## Resolution (2026-06-27)

- **#1 session resume (P0) — done, in-process.** The HITL post-answer re-invoke now threads the first run's `session_id` so the agent resumes the same conversation instead of starting fresh. `_hitl_resume_session_id` gates resume to the same provider (`outcome.provider_used == route.primary`); `_invoke`/`_build_request` take a `resume_session_id` that wins over the editing-lineage lookup (`core/flow/nodes/agent.py`). `_reconsider` needed no change — the dangerous-diff denial re-run targets `editing_lineage` author nodes, which already resume via `_resume_session_id`. **Scope cut (confirmed):** resume is **in-process only**. Across an orchestrator restart the first-run session id is gone (a `fresh_disposable` node has no durable session slot, by design), so the re-entered round-trip falls back honestly to a fresh session + the persisted answer context file — no new persistence, no `state.db` bump. Regression tests: `test_agent_hitl_round_trip_resumes_first_run_session` (acceptance), `test_agent_hitl_round_trip_no_resume_when_first_run_used_fallback` (provider gate), `test_dangerous_diff_reconsider_resumes_editing_lineage` (reconsider resumes for free).
- **#2 autonomy (P2) — documented, no code.** The "escalation policy explicitly documented" acceptance branch: [task-authoring.md → Planning escalation and unattended runs](../task-authoring.md#planning-escalation-and-unattended-runs) and [operations.md → Running](../operations.md#4-running) (auto_mode governs sequencing, not HITL). No code knobs this round — the per-node agent-HITL timeout and a bounded-wait "auto-proceed on default" are deferred (see [follow_ups.md](follow_ups.md)).

---

Carved out of the `td-be-003-conform-m1-m2-contract-shapes` run post-mortem (first real Windows run on `argudebate`): [docs/analysis/td-be-003-conform-m1-m2-contract-shapes-run-analysis.md](../analysis/td-be-003-conform-m1-m2-contract-shapes-run-analysis.md), findings **0** (critical) and **4**. Both concern how a HITL round-trip behaves at the `planning` node: the session is thrown away across the human round-trip, and the escalation makes an otherwise-autonomous run depend on a human tap.

## The problem

### 1. HITL recreates the agent session instead of resuming it (🔴 CRITICAL — operator directive)

When an `agent` node raises a `human_input` signal (question/approval), the orchestrator does one durable round-trip and then **re-runs the node in a brand-new session**. The operator's answer is delivered as a context **file**, not as a continuation of the same conversation. The agent loses everything it had read/decided in the first session and re-does the work from scratch.

**Operator directive:** HITL must **never** recreate the agent session — it must **resume** it. Regardless of which node raised the signal and when, after the operator replies the agent MUST continue the **same** session from exactly the point the Telegram request was made.

**Evidence (td-be-003).** The `planning` node ran twice with **different** session ids — `result.session_id` run-000002 = `session:c8162f4f92ff`, run-000003 = `session:c5d799a3aef0`. No `--resume`/`--continue`/`--session` appears in either `request.json.argv`. The only input difference for the second run was an added `context_paths: [task_path, human_input_path]`. Cost of the recreate: a full re-plan (~34k output tokens, ~532 s wall) and loss of working context.

### 2. The HITL approval makes the run non-autonomous (finding 4)

`planning` has `hitl: {allow_question: true, allow_approval: true}`, and on this task the agent (correctly) escalated a genuinely material scope-boundary decision (does honest canon conformance pull in an EF migration, jsonb storage, and a `deviceId`/security-scope reduction?). The run then blocked until a human approved via Telegram (`telegram.ask_timeout_s = 28800` = 8 h). For an unattended `watch`/autonomous run this is a stall, even though the agent already had a sensible default ("if you just say 'proceed': …").

These two are related: #1 is the mechanism (session lifecycle across the round-trip), #2 is the policy (when to stop for a human at all).

## Root cause (exact levers)

All in `src/wastech_orchestrator/core/flow/nodes/agent.py`:

- `_run_with_hitl` re-invokes after the answer at **`agent.py:132`** — `self._invoke(node, ctx, route, human_input_path=str(path))` — without threading the first run's `session_id`.
- `_build_request` sets `session_id=self._resume_session_id(node, ctx, route)` at **`agent.py:441`**.
- `_resume_session_id` (**`agent.py:491-508`**) returns `None` for everything except `SessionScope.EDITING_LINEAGE` (`agent.py:503`) — so `planning`/`refinement` (`fresh_disposable`) always start a fresh session on the HITL re-run.
- Same gap in `_reconsider` (**`agent.py:373`**, the dangerous-diff-denied re-run) and in the after-restart `_resume_interaction` path (`agent.py:138-149`).

The resume machinery already exists and works: `implementation` → `fixing` continue one session via `AgentRunRequest.session_id` → `--resume` (`providers/claude.py`, persisted through `_persist_session`/`get_editing_lineage`). HITL re-entry simply does not use it for non-`editing_lineage` nodes.

## Proposed approach

1. **Session resume on HITL re-entry (P0 critical).** Capture the first run's `session_id` (`outcome.result.session_id`) in `_run_with_hitl`/`_reconsider` and thread it into the second `_invoke` → `_build_request` → `AgentRunRequest.session_id`, **bypassing** the `_resume_session_id` "editing_lineage-only" restriction for the HITL re-invoke case. Resume must work for **any** `session_scope` and **any** node. Keep the provider gate (you cannot resume a Claude session on Codex — mirror the `route.primary` check at `agent.py:506`). For the across-restart case (the CLI session may be gone), attempt resume and fall back honestly if it cannot be resumed — but that does not relax the in-process requirement.
2. **Autonomy policy (P2).** Reduce avoidable stops on well-specified tasks: either pre-decide the scope boundary in the task spec (task-authoring guidance), or let the planning node proceed on its stated default after a bounded wait (downgrade `allow_approval` to a question-with-default, or a shorter HITL timeout in `watch`). The decision to escalate stays data-driven (the node's `hitl` flags), never the stage name.

## Constraints / invariants

- Provider abstraction holds: session ids, model ids, and CLI args are provider-specific; never resume across providers. The orchestrator owns the round-trip; providers do not.
- No change to the state machine or to "only the orchestrator commits/pushes/PRs".
- Data-driven HITL: keep the `hitl` capability flags as the decision surface (no stage-name special-casing).

## Acceptance

- A task that triggers a HITL round-trip produces a second node run whose `result.session_id` **equals** the first run's session id (regression test).
- The agent's second turn continues the prior conversation (it does not re-read/re-derive from scratch); a `--resume`-style session id is present in the re-invoke `request.json`.
- (Autonomy) A well-specified task with a sensible planning default can complete unattended within a bounded wait, or the escalation policy is explicitly documented.

## Scope

Orchestrator default (every repo, every HITL-capable node) for #1. Target/task-authoring + flow for #2.

## References

- Analysis: [td-be-003-conform-m1-m2-contract-shapes-run-analysis.md](../analysis/td-be-003-conform-m1-m2-contract-shapes-run-analysis.md) findings 0 and 4.
- Related deferred item: HITL pause invisibility in the operator log — [hitl-wait-observability-and-prompt-cleanup.md](hitl-wait-observability-and-prompt-cleanup.md) (finding 5) and the 2026-06-24 watch-autonomy row (c) in [follow_ups.md](follow_ups.md).
- Code: `core/flow/nodes/agent.py` (`_run_with_hitl`, `_reconsider`, `_resume_session_id`, `_build_request`); `providers/{claude,_adapter_base}.py` (session resume); `core/hitl.py` (round-trip).
