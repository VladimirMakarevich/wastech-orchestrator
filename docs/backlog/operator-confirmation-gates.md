# Operator confirmation gates in autonomous mode

Status: **proposed** Date: 2026-06-26 Owner: Vladimir Makarevich

Two operator-confirmation checkpoints for full-auto `watch` mode, recorded together because they are one mechanism — a durable Telegram approval gate that pauses an autonomous run at a decision point — applied at two points: before claiming the next task (idea 27) and when an agent exhausts its turn budget (idea 29). The max-turns "continue" path rests on one unverified feasibility assumption (does provider `--resume` grant a fresh turn budget?), so this item stays exploratory until that is confirmed — see [Open questions](#open-questions).

## The problem

In full-auto mode (`orchestrator.auto_mode.enabled: true`) the `watch` loop chains tasks back-to-back and lets each agent burn its turn budget with no operator checkpoint. An operator watching remotely has no way to say "wait, not that task next" before a run starts, and no way to extend a run that stopped only because it hit the turn cap. The current escape hatches are coarse: stop the daemon, or let `error_max_turns` fail the task and re-queue it manually.

The turn cap is the sharper pain. Because hitting it is a terminal `TASK_FAILURE` today, the default `max_turns` is set defensively high (400) so legitimate long runs are not killed. A live "continue or stop?" gate would make a low cap (~50–100) safe again: short by default, extendable on demand.

## Constraints

- **The provider layer must stay dumb** (hard invariant — providers do not know about Telegram, notifications, or the state machine). The Claude adapter already surfaces `error_max_turns` as a parsed terminal outcome (`failure_subtype`); the gate for idea 29 therefore lives at the flow/orchestrator level, never in `providers/`.
- Both gates reuse the **existing** durable HITL machinery (`notify/telegram.py` `ask_human`, the durable "waiting" artifact, `telegram.ask_timeout_s`) and the orchestrator's durable-resume — they do not introduce a new interaction transport.
- **No secrets in gate messages** — prompts carry only task id/title and node id, never diff or prompt content.
- `max_turns` is **Claude-only** (Codex has no turn cap); idea 29 is a Claude-provider concern.
- Only the orchestrator drives resume/continue; the provider does not retry or change state on its own.

## Alternatives considered

| Option | Why rejected |
| --- | --- |
| Do nothing — keep coarse stop-the-daemon control | No remote, per-decision control; the stated goal of idea 27/29 |
| Local terminal prompt (`input()`) for the next-task gate | `watch` runs as a daemon with no attended TTY; Telegram is the only viable remote channel |
| Put the max-turns gate inside the Claude provider | Violates the dumb-provider invariant; the provider would need to know about notifications and resume |
| Idea 29 "continue" = hand partial result to the fix loop | "Continue" must mean "finish the same work", not "patch whatever got done"; loses the point of extending the budget |
| Idea 29 "continue" = restart the node from scratch | Discards all session progress; wasteful and slower than resuming |
| Gate degrades to no-op when Telegram is off | An enabled-but-silent safety gate is worse than none; operator would believe they have control they don't |
| Per-task / per-tick batch approval for idea 27 | Less control than per-task; rejected in favor of one prompt per task (the explicit ask) |

## Decision

Two gates, both built on `ask_human`, both **fail-closed**.

### 1. Next-task gate (idea 27)

In the `watch` loop, when `auto_mode.enabled` is true and a new pending task is about to be claimed, send a Telegram approve/deny prompt showing the task id and title. Approve → claim and run it. Deny → leave it pending and stop chaining for this cycle (operator decides later). This gates **new task claims only** — resuming an already in-flight task on daemon restart is not gated. Granularity is **per task**: one prompt per next task, maximum control.

Config: `orchestrator.auto_mode.confirm_next_task: bool` (default `false`).

### 2. Max-turns gate (idea 29)

When a Claude attempt returns the `error_max_turns` outcome, the flow/node layer (not the provider) intercepts before terminal classification and sends a "turn limit reached — continue or stop?" prompt. Continue → the orchestrator **resumes the same agent session with a fresh turn grant** (reusing the durable-resume path). Stop → the run terminates as it does today. With this gate on, the recommended default `max_turns` drops from 400 to ~50–100.

Config: `agents.providers.claude.max_turns_gate: bool` (default `false`). The per-continue grant reuses the configured `max_turns` value (no separate grant key in v1).

### Fail-safe posture (both gates)

- **Preflight**: if either gate is enabled while `telegram.enabled` is `false`, fail validation and refuse to start — an enabled gate with no transport is a misconfiguration, not a silent no-op.
- **Timeout**: a gate that times out (`telegram.ask_timeout_s`) resolves to the safe default — **STOP**: do not claim the next task (27); do not continue burning turns, terminate the run (29). The operator's silence never advances an autonomous action.

## Open questions

- **Feasibility crux for idea 29**: does provider `--resume` actually grant a _fresh_ turn budget, or does the resumed session inherit the exhausted counter? If resume does not reset the cap, "continue" cannot mean "resume +N" and the decision for 29 must fall back to restart-the-node. Verify against the real Claude CLI before moving this item to "accepted".
- **Runaway continue**: per-continue grants are operator-gated (and timeout→STOP bounds unattended loops), but should there also be a hard ceiling on total resumes per node to bound a pathological approve-loop? Probably yes — a small cap (e.g. 3) with a final forced stop.
- **Config placement for the max-turns gate**: `agents.providers.claude.max_turns_gate` sits next to `max_turns` (natural), but the behavior is orchestrator-level. Confirm placement vs an `orchestrator`-level key during implementation.
- **Schema version**: both keys are additive optional fields; confirm whether a `CONFIG_SCHEMA_VERSION` bump is required (loader tolerance for unknown keys may make it unnecessary).

## Implementation notes

- **Next-task gate**: `src/wastech_orchestrator/cli.py` — `watch_once` / `watch_loop`, at the point a pending task is selected (`select_pending`) and before it is claimed. Call the existing notifier `ask_human`; on deny/timeout skip the claim. Honors the existing "one task per tick" / dependency-eligibility flow.
- **Max-turns gate**: the Claude adapter already classifies `error_max_turns` (see `providers/claude.py` `parse_stream_json` and `providers/_adapter_base.py`), but the subtype is currently embedded in the `NormalizedError` message string. Surface it as a **structured field** so the node layer can detect it cleanly rather than substring-matching. Detect in the agent node runner (`core/flow/nodes/agent.py`), invoke `ask_human`, and on "continue" route into the orchestrator's durable-resume with a fresh grant.
- **Config**: add `confirm_next_task` to the auto-mode config and `max_turns_gate` to `ProviderConfig` in `config/schema.py`; parse in `config/loader.py`; add the preflight rule (gate-on requires `telegram.enabled`) to `config/validation.py`.
- **Reuse**: timeout, durable "waiting" artifact, and resume-across-restart all come from the existing HITL path — no new persistence or transport.
- Tests: extend the fake-CLI fixtures (`/fake-cli`) to emit an `error_max_turns` terminal stream; assert the gate is invoked, that continue resumes and deny/timeout stops, and that preflight rejects gate-on-without-telegram.
