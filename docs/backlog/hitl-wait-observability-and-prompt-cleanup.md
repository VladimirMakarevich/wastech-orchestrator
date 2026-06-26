# HITL-wait observability & not-decomposed prompt cleanup

Status: **done (2026-06-27)** — both findings implemented Date: 2026-06-26 Owner: Vladimir Makarevich

Resolution: (1) `HumanGate.request`/`resume` ([core/flow/nodes/human_gate.py](../../src/wastech_orchestrator/core/flow/nodes/human_gate.py)) now bracket the blocking wait with an `awaiting human input` entry line, a `awaiting human input heartbeat` per tick (reusing `run_with_heartbeat`), and a `human input resolved` exit line carrying the resolution status — secret-free ids/kind/timeout only. The interval is the orchestrator-wide `--heartbeat-seconds`, threaded via `NodeServices.ask_heartbeat_seconds`. (2) The safe renderer ([core/prompts.py](../../src/wastech_orchestrator/core/prompts.py)) gained a backward-compatible `{?name}…{/name}` conditional block; the packaged `implementation`/`fixing` roles wrap their subtask clause in `{?subtask_spec_path}…{/subtask_spec_path}`, so a non-decomposed run renders no dangling "subtask of …" sentence.

Carved out of the `td-be-003-conform-m1-m2-contract-shapes` run post-mortem: [docs/analysis/td-be-003-conform-m1-m2-contract-shapes-run-analysis.md](../analysis/td-be-003-conform-m1-m2-contract-shapes-run-analysis.md), findings **5** and **6**. Two independent polish items grouped as one low-risk cleanup task.

## The problem

### 1. No console/log signal while blocked on a HITL approval (finding 5)

When a node blocks on `human_input`, nothing is emitted to the run logger or as a heartbeat — the prompt goes only to Telegram. An operator tailing the console sees an unexplained silent gap and cannot tell the run is waiting on a human (vs. hung).

**Evidence (td-be-003).** Grepping the run console log for `human|approval|telegram|await|question|hitl|input` returns **zero lines**. Between `planning #1 completed` (18:33:24) and the next `planning route resolved` (18:35:55) there is a ~2.5-min silence; the approval request (`message_id=28`) was visible only in Telegram. This is the same class as the 2026-06-24 watch-autonomy follow-up item (c) and the "silent tail" finding in the redesign-form-controls analysis.

### 2. Dangling subtask placeholders in the prompt when the task is not decomposed (finding 6)

The implementation/fixing role files always include a trailing "if a subtask spec path is listed here…" clause. When the task is **not** decomposed, `{subtask_order}` / `{subtask_count}` / `{subtask_spec_path}` substitute to empty strings, leaving a nonsensical hanging sentence in the rendered prompt.

**Evidence (td-be-003).** `stages/implementation/rendered-prompt.md` line 17 reads: "…you must implement ONLY that subtask — subtask of — per its immutable spec: " — empty placeholders. The same template line is in `roles/implementation_backend/fixing.md`.

## Root cause (exact levers)

1. **HITL-wait logging.** The `human_input` wait path emits no info-level log line or heartbeat, unlike provider operations (which heartbeat every N s). Lever: the orchestrator HITL/ask path — `src/wastech_orchestrator/core/hitl.py` and `core/orchestrator.py` (the ask/`HumanGate.request`/`resume` call sites), plus the observability layer that already emits provider heartbeats.
2. **Subtask clause.** The clause is unconditional in the role text; the prompt builder substitutes empty values when `ctx.subtask_order is None`. Levers: the role files `roles/implementation_backend/{implementation,fixing}.md` (make the clause conditional) **or** the prompt assembler `core/flow/nodes/agent.py` `_prompt_variables` / `core/flow/prompt.py` `render_role_prompt` (omit the clause when not decomposed). Note the packaged default roles were already de-cluttered for the _skill_ clause in the 2026-06-25 redesign-form-controls remediation; this is the analogous _subtask_ clause and applies to the target's `implementation_backend` roles.

## Proposed approach

1. **Emit a HITL-wait signal (P1).** On entering a `human_input` wait, log an info line and start a heartbeat, e.g. `awaiting human approval (node=…, interaction=…, channel=telegram, timeout=8h)`, and log resolution (`answered`/`timeout`/`failed`) on exit. No secrets in the line (the question text/paths are already allowlisted artifacts; keep ids + channel + timeout).
2. **Make the subtask clause conditional (P2).** Render the "implement ONLY subtask N of M" sentence only when the unit is actually a subtask (`subtask_order is not None`); otherwise omit it entirely. Apply to both `implementation` and `fixing` roles. Prefer fixing it once in the prompt assembler so any role with the clause benefits.

## Constraints / invariants

- No secrets in logs (security rule). Log ids/channel/timeout, not credential material.
- Keep markdown docs un-wrapped (Prettier `proseWrap: never`).
- Cosmetic prompt change must not alter behavior for decomposed runs.

## Acceptance

- A run blocked on HITL shows at least one info-level log line (and periodic heartbeat) identifying the wait, and a resolution line when it ends.
- A non-decomposed run's `rendered-prompt.md` contains no dangling "subtask of …" sentence; a decomposed run still gets the correct "subtask N of M" text.

## Scope

Orchestrator default for #1 (HITL logging). Target roles (`implementation_backend/{implementation,fixing}.md`) and/or the prompt assembler for #2; if fixed in the assembler, it benefits the packaged default roles too.

## References

- Analysis: [td-be-003-conform-m1-m2-contract-shapes-run-analysis.md](../analysis/td-be-003-conform-m1-m2-contract-shapes-run-analysis.md) findings 5 and 6.
- Related: HITL session resume & autonomy — [hitl-session-resume-and-autonomy.md](hitl-session-resume-and-autonomy.md); 2026-06-24 watch-autonomy follow-up (c) (HITL pause invisibility).
- Code: `core/hitl.py`; `core/orchestrator.py` (ask path); `core/flow/nodes/agent.py` `_prompt_variables`; `core/flow/prompt.py`; `roles/implementation_backend/{implementation,fixing}.md`.
