# Reliable rate-limit / session-limit handling — recognize it, fall back, park, and recover

Status: **proposed** (2026-07-10) Date: 2026-07-10 Owner: Vladimir Makarevich

A subscription/session rate-limit is a **transient infra event**, but the orchestrator currently mislabels it as a quality failure of the agent — so it neither falls back to the other provider nor parks the task, and instead keeps working as if the agent gave up on the task. This ADR consolidates two independent reproductions of that single root cause into one buildable decision: classify the limit as `ErrorClass.RATE_LIMITED` and **raise** it (so the Router's existing fallback fires), route all-providers-limited exhaustion to a resumable park until reset, pause the auto_mode queue on a rate-limit terminal, and — as defense-in-depth — stop a fix loop from counting an infra no-op fixing attempt as a productive cycle and stop the stuck-report from hiding the real cause. Scope is the **P0 self-throttle/recover chain plus its two amplifiers**; the related P1/P2 items are named as linked follow-ups, not specced here.

## The problem

The same root cause has now been reproduced twice, in opposite run modes:

- **content-rework batch, 2026-07-10 (auto_mode, 13 tasks).** The Claude subscription hit its 5-hour window at ~02:57 UTC; from that point every task terminated `manual_action_required` — 13 recoverable tasks burned in a row because the queue kept picking up the next one every poll interval. Full post-mortem: [AUDIT-content-rework-run-2026-07-10.md](../../AUDIT-content-rework-run-2026-07-10.md) (findings F1–F9).
- **p6-04-config-writer-schema, 2026-07-09 (single foreground `worc run`).** The limit hit on the 3rd fix cycle (`fixing#72`, ~21:54 UTC); the `fixing` node became a ~2-3s zero-token no-op, `review` (codex) re-found the same blocking issues each round, and **12 dead fix cycles** exhausted `max_fix_cycles=15` → `manual_action_required`. Full post-mortem: [docs/analysis/p6-04-config-writer-schema-run-analysis.md](../analysis/p6-04-config-writer-schema-run-analysis.md) (findings F48/F49/F50).

The mechanism, with the exact seams:

- The limit arrives on **stdout**, not stderr. The Claude CLI emits a terminal `result` event with `subtype:"success"`, `is_error:true`, `api_error_status:429` (plus a `rate_limit_event` with `rateLimitType:"five_hour"`, `overageDisabledReason:"out_of_credits"`), and `final_message="You've hit your session limit · resets …"`. `stderr.log` is **0 bytes**.
- [`parse_stream_json` (claude.py:364-390)](../../src/wastech_orchestrator/providers/claude.py#L364-L390) computes `succeeded = (not is_error) and subtype=="success"` → `False`, `failure_subtype="success"`, and **ignores `api_error_status`/`rate_limit_event`**. Then [`_adapter_base.py:411-421`](../../src/wastech_orchestrator/providers/_adapter_base.py#L411-L421) turns any parsed-but-unsuccessful result into a returned `NormalizedError(ErrorClass.TASK_FAILURE)` **without inspecting `final_message`**.
- The stderr `RATE_LIMITED` signature ([claude.py:102-103](../../src/wastech_orchestrator/providers/claude.py#L102-L103): `rate limit|429|too many requests|quota exceeded|overloaded`) cannot help — it is applied only to stderr, and it lacks `session limit`/`resets` patterns anyway.
- Fallback never fires: [router.py:17](../../src/wastech_orchestrator/routing/router.py#L17) documents that a quality `AgentRunResult(status=failed)` never triggers fallback — only a **raised** `ProviderError` does. `RATE_LIMITED` **is** in [`FALLBACK_ELIGIBLE` (base.py:66-79)](../../src/wastech_orchestrator/providers/base.py#L66-L79), but a _returned_ `TASK_FAILURE` never reaches that path. (`provider_used` in the batch: claude 140×, codex 0×.)
- The fix loop flows on instead of parking: the agent node raises `NodeInfraError` (→ park) **only** when `outcome.result is None` ([agent.py:333-337](../../src/wastech_orchestrator/core/flow/nodes/agent.py#L333-L337)); a session-limit returns a non-`None` result, so [`_agent_outcome` (agent.py:694-703)](../../src/wastech_orchestrator/core/flow/nodes/agent.py#L694-L703) maps it to `NodeOutcome("done")` and [`_charge_rework` (engine.py:282-299)](../../src/wastech_orchestrator/core/flow/engine.py#L282-L299) burns a cycle each round.
- Even correct classification has nowhere to defer: `_park` fires only for `TRANSIENT_RETRYABLE` or `CANCELLED` ([orchestrator.py:~1770](../../src/wastech_orchestrator/core/orchestrator.py#L1770)); `RATE_LIMITED` is deliberately excluded from `TRANSIENT_RETRYABLE` ([base.py:80-88](../../src/wastech_orchestrator/providers/base.py#L80-L88)) "in expectation of a long defer" — **but the defer path does not exist**, so exhaustion on both providers currently terminates.
- The auto_mode queue has no circuit-breaker: a rate-limit terminal on task N does not affect the pick-up of task N+1.
- The stuck report hides the cause: [`FlowRecorder.write_failure_report` (recorder.py:48-70)](../../src/wastech_orchestrator/core/flow/recorder.py#L48-L70) hardcodes `last_review_findings=None, final_diff=""`, though [`ledger.write_failure_report` (ledger.py:147-215)](../../src/wastech_orchestrator/ledger.py#L147-L215) accepts both — so `stuck.md` reads "findings: (none), diff: (empty)" while the real run had 3 blocking findings and a 929-line diff.

## Constraints

The **provider abstraction** dictates where each piece lands: rate-limit _detection_ is provider-specific and must stay in `providers/claude.py` and `providers/codex.py`; the core only ever sees `ErrorClass.RATE_LIMITED`. **Fallback is the Router's job, never the provider's** — so the fallback fix is "make classification correct and _raise_," not "add fallback in the adapter." The **park/defer and the queue circuit-breaker are orchestrator-level** (state machine + auto_mode loop), consistent with the reliable-stop park path already in the code. **Cross-platform**: the park uses the existing resumable mechanism — no new signal/`os.kill` assumptions. **No secrets in artifacts**: inspecting `final_message` for the limit banner is safe (it carries no secret). **Greenfield MVP**: no back-compat shim. This also completes an existing invariant — "no silent `failed` without a diagnosable `failure_report.json`/`stuck.md`" — which F50 currently satisfies only vacuously. The distinction that must be preserved: a **genuine** quality `task_failure` (agent gave up, `max_turns`) must still flow on as a quality outcome — only the rate-limit terminal is being reclassified as infra.

## Alternatives considered

| Option | Why rejected |
| --- | --- |
| Do nothing | Both failure modes recur on every run that crosses the 5-hour window — a batch burns its queue, a single task burns its fix budget, and the cause stays masked. |
| Widen the stderr `RATE_LIMITED` regex only | Insufficient: the limit banner arrives on **stdout** with empty stderr, so a stderr-only signature never sees it. |
| Detect the limit but keep **returning** `TASK_FAILURE` (don't raise) | Fallback still never fires — [router.py:17](../../src/wastech_orchestrator/routing/router.py#L17) reacts only to a _raised_ `ProviderError`. The raise-vs-return distinction is the whole point. |
| Treat every non-success terminal result as infra | Wrong — it would misclassify genuine quality failures (agent gave up, `max_turns`) as infra and mask real task problems. |
| **Chosen: classify the rate-limit terminal as `RATE_LIMITED` and raise it at the provider parse/finalize seam; let the existing Router fallback → orchestrator park → queue-pause follow; add the no-op-fixing guard and the reporting fix independently** | One correct classification unlocks machinery that already exists (`FALLBACK_ELIGIBLE`), and the two amplifier fixes make the system robust even in edge cases the classification alone doesn't cover. |

## Decision

Recognize a subscription/rate-limit terminal — HTTP 429 / `session limit` / `five_hour` / `out_of_credits`, wherever it arrives (a stdout `result`/`rate_limit_event` event or stderr) — as `ErrorClass.RATE_LIMITED` and **raise** it as a `ProviderError`, symmetrically in `claude.py` and `codex.py`, so the Router's existing `FALLBACK_ELIGIBLE` path falls over to the other provider (which draws on a separate quota and would very likely finish the work). When **every** provider is rate-limited, route the exhaustion into a **resumable `_park`** bounded by `agents.retry.max_blocked_s` (ideally to the reset time parsed from the banner) rather than a terminal fail. In **auto_mode**, add a **queue-level circuit-breaker** that suspends picking up new tasks on a `RATE_LIMITED` terminal until cooldown/reset. Independently — defense-in-depth even when classification is correct — a **fix loop must not count an infra no-op fixing attempt** (an infra error, or N consecutive zero-progress cycles) as a productive cycle: it aborts the loop as infra instead of burning the budget. And the **stuck artifacts must carry the real last findings and diff**.

We do this because a single misclassification is what turns one transient limit into a cascade — a burned queue or a burned fix budget — with the cause masked as "the agent failed the task"; the cost is a small amount of provider-specific detection logic plus a park/queue-pause path that must respect the reset window. The rejected half-measures (do-nothing, regex-only, detect-but-return) all leave fallback dead and the cascade intact.

## Open questions

Reset-time handling: parse the "resets HH:MM" / `resetsAt` epoch from the banner to set the park deadline, or fall back to a fixed cooldown? Note `agents.retry.max_blocked_s` defaults to 3600s, which can be **shorter** than the wait-to-reset (~1.5h observed) — so either bump the default or park-to-parsed-reset.

Circuit-breaker granularity: pause the whole auto_mode queue, or only tasks whose route needs the rate-limited provider (letting codex-only work proceed)?

codex symmetry: does the codex CLI surface an analogous **structured stdout** limit event, or only stderr? Needs a probe before writing the codex-side detection (use the `/fake-cli` skill to pin a fixture).

F49 guard threshold: abort on the **first** infra fixing attempt, or after **N consecutive** zero-progress cycles? Once F48 raises `RATE_LIMITED` the first-attempt path is the clean one; the N-consecutive guard is the provider-agnostic backstop (this is content-rework **F4**, which stays a linked follow-up).

Relationship to the two audit docs: this ADR supersedes the **P0** items of both (content-rework F1/F2/F5/F6 + p6-04 F48/F49, and the reporting F50). The P1/P2 residue stays tracked where it lives, linked not duplicated: **F3** (evaluator diagnoses an infra agent failure as "schema not honored", [evaluator.py:135-152](../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L135-L152)), **F4** (generic zero-progress fix-loop guard), **F7** (`manual_action_required` with an empty diff is a mislabel of an infra abort), and **F8/F9** (operational: batch sizing vs. the subscription window; task-file placement of runtime-manual tasks).

## Implementation notes

Detection + raise — [`parse_stream_json` (claude.py:364-390)](../../src/wastech_orchestrator/providers/claude.py#L364-L390) and the finalize step in [`_adapter_base.py:411-421`](../../src/wastech_orchestrator/providers/_adapter_base.py#L411-L421): when a terminal `result` event carries `is_error` with `api_error_status==429` (or a `rate_limit_event` with `status=="rejected"`, or a `final_message` matching a session/usage-limit pattern), raise `ProviderError(ErrorClass.RATE_LIMITED)` instead of returning a `TASK_FAILURE`. Extend the [claude.py:102-103](../../src/wastech_orchestrator/providers/claude.py#L102-L103) / [codex.py:79](../../src/wastech_orchestrator/providers/codex.py#L79) signatures with `session limit|usage limit|hit your (session|usage) limit|limit .* resets` as a secondary net.

Fallback — [routing/router.py](../../src/wastech_orchestrator/routing/router.py): no new logic; just confirm the raised `RATE_LIMITED` reaches the `FALLBACK_ELIGIBLE` branch (the router.py:17 contract) and the second provider is attempted.

No-op fixing guard — [core/flow/nodes/agent.py:333-337 and 694-703](../../src/wastech_orchestrator/core/flow/nodes/agent.py#L333-L337): a terminal `RATE_LIMITED` (and, per the F4 backstop, a repeated zero-progress infra failure) on a fixing node yields `NodeInfraError`, not `NodeOutcome("done")` — so the loop parks/aborts instead of charging a cycle at [engine.py:282-299](../../src/wastech_orchestrator/core/flow/engine.py#L282-L299).

Park path — [core/orchestrator.py:~1770](../../src/wastech_orchestrator/core/orchestrator.py#L1770) with [base.py:80-88](../../src/wastech_orchestrator/providers/base.py#L80-L88): route `RATE_LIMITED` exhaustion into the resumable `_park` bounded by `agents.retry.max_blocked_s` (ideally the parsed reset time) — the defer path that F6 says is missing today.

Queue circuit-breaker — the auto_mode / watch dispatch loop: on a `RATE_LIMITED` terminal, stop dispatching new tasks until cooldown/reset (content-rework F5, the main lever for unattended runs).

Reporting — [core/flow/recorder.py:48-70](../../src/wastech_orchestrator/core/flow/recorder.py#L48-L70): pass the last evaluator verdict (from `state.db evaluations.findings_json`) and the working-tree diff into `ledger.write_failure_report` instead of `None`/`""`.

Tests — `/fake-cli` fixtures: a session-limit stdout event → assert `RATE_LIMITED` is **raised** and fallback is attempted; a both-providers-limited case → assert resumable `_park`, not a terminal; a recorder test asserting non-empty findings/diff in `stuck.md`/`failure_report.json`.
