# Telegram step-trace (live run progress)

Status: **implemented** (2026-06-27, config v21, suite green) Date: 2026-06-26 Owner: Vladimir Makarevich

A one-way, best-effort live progress feed pushed to Telegram so a remote operator can see which flow node just ran and how it resolved, toggled by a single global config flag. Builds entirely on the existing `Notifier` abstraction and the per-node observability seam — no new transport, no new persisted state.

## The problem

Per-node progress already exists, but only as persisted data: the `node_runs` table and `core/flow/observability.py` (`record_run_observability`) record every node transition and outcome to SQLite and disk. An operator watching a `watch` run remotely has no live view of it. The only outbound Telegram messages today are `send_notification` on a **terminal** task status (DONE / FAILED / MANUAL_ACTION_REQUIRED) — so between "task started" and "task finished" the operator sees nothing, which is exactly the window where a long autonomous run is opaque.

## Constraints

- **No secrets in messages** (hard invariant). The trace carries only the node id and its outcome — never diff content, prompt text, or agent output. This is what keeps the feature safe by construction.
- **Best-effort, never blocks or raises**, matching the existing notification semantics: a trace-send failure must not affect the pipeline.
- **The provider layer stays dumb** — the trace is emitted from the flow/observability layer, not from `providers/`.
- This is distinct from the proposed `logging.*` keys (see [log-management.md](log-management.md)): `logging.level` controls local log/console verbosity (files), while this controls a Telegram push (a different channel). They should not be conflated into one key.

## Alternatives considered

| Option | Why rejected / deferred |
| --- | --- |
| Do nothing — rely on terminal notifications + reading logs | No live remote visibility; the stated goal |
| LLM-generated "what the node did" one-liner | Cost, latency, and a secret/code-leak risk into Telegram — against the no-secrets invariant |
| Static human label per node id ("implementation — writes the code") | **Deferred**, not rejected: node ids are already human-readable; a label map is a small future enrichment if needed |
| Verbosity levels (`off` / `milestones` / `verbose`) instead of a bool | **Deferred**: the natural escalation if per-node-per-task noise becomes a problem; bool ships first as requested |
| Emit on node **start** as well as finish | **Deferred**: doubles message volume; finish-with-outcome is the higher-signal event |
| Reuse `logging.level` to also gate Telegram output | Conflates two channels (files vs Telegram); separate concerns, separate keys |

## Decision

Add a single global on/off flag and push one best-effort message per **node finish**, hooked at the existing post-node observability seam.

- **Config**: `telegram.trace: bool` (default `false`). Lives in `TelegramConfig` because it is a Telegram-channel concern.
- **Message content**: node id + outcome, e.g. `✅ implementation → accept`, `🔁 fixing → rework`, `❌ testing → fail`. Node ids are already meaningful (`refinement`, `planning`, `implementation`, …), so this satisfies both "which step ran" and "a couple words about what it does" with zero maintenance and zero secret-leak surface.
- **Semantics**: best-effort, fire-and-forget; never blocks or raises into the pipeline; a `NullNotifier` (Telegram disabled) makes it a no-op automatically.

## Open questions

- **Noise at scale**: per-node-per-task across a busy `watch` queue (7+ nodes × fix iterations × many tasks) can be chatty enough that an operator turns it off — defeating the purpose. If that happens, the deferred `off/milestones/verbose` enum is the fix; ship the bool first and watch real usage.
- **Interleaving with gate prompts**: this trace and the operator-confirmation gate messages ([operator-confirmation-gates.md](operator-confirmation-gates.md)) share the same chat. Confirm the trace does not bury an awaiting approve/deny prompt; a distinct prefix/emoji for gate prompts is likely enough.
- **Task-level bookends**: should the trace also emit a "task started" line (terminal status is already covered by `send_notification`)? Probably yes for context, but it is additive and can follow.

## Implementation notes

- Add a `send_trace` (or `send_progress`) method to the `Notifier` protocol in `notify/interface.py`; implement as a no-op in `NullNotifier` and as a best-effort send in `notify/telegram.py`.
- Call it from `core/flow/observability.py` `record_run_observability` (the existing post-node hook), passing node id and outcome — the data is already in hand there. Gate the call on `telegram.trace`.
- Add the `trace` field to `TelegramConfig` in `config/schema.py` and parse it in `config/loader.py`. Additive optional field — confirm whether a `CONFIG_SCHEMA_VERSION` bump is needed.
- No new persistence and no provider changes. Tests: assert a node finish triggers `send_trace` when `telegram.trace` is on, that it is a no-op when off / with `NullNotifier`, and that a send failure never propagates.

## Implementation outcome (2026-06-27)

Shipped exactly the Decision (per-node-finish trace, single `telegram.trace` bool, default `false`, config v21). Key correction to the notes above and resolved open questions:

- **Seam: the engine post-node hook, not `record_run_observability`.** `record_run_observability` runs inside the node runners right after `router.run_stage`, where the flow **verdict is not yet known** (agent nodes carry only a provider `RunStatus`; an evaluator's accept/rework is computed later) and the orchestrator's config/notifier are not cleanly in scope. The emit lives in `Orchestrator._engine_post_node` → `post_node` (`core/orchestrator.py`), the existing once-per-executed-node hook where the supervisor already observes each step. It has `node.id`, the canonical `NodeOutcome.kind`, `self._config.telegram.trace`, and `self._notifier` all in hand. Gated on `telegram.trace` alone — when Telegram is off the notifier is a `NullNotifier`, so it is a no-op automatically.
- **Outcome vocabulary.** The message is `[task] <emoji> <node-id> → <outcome>` where outcome is the real `NodeOutcome.kind`: `done` (agent), `accept`/`rework` (evaluator), `pass`/`fail` (checks), `route:<label>` (explicit). The idealized examples above (`implementation → accept`) read as `implementation → done` in practice — agent nodes resolve `done`. Emoji: ✅ accept/done/pass, 🔁 rework, ❌ fail, ▶️ otherwise.
- **CONFIG_SCHEMA_VERSION bump: yes (20 → 21).** Additive/optional, so no migration code — `upgrade-config` merges the new template key into old configs and the loader accepts absent/lower versions; the bump is the convention that signals operators to re-sync.
- **Gate-prompt interleaving (open Q2): handled by the distinct leading emoji** — trace lines are visually separable from approve/deny prompts in the same chat.
- **Deferred** (recorded in [follow_ups.md](follow_ups.md)): task-start bookend (open Q3), the `off/milestones/verbose` verbosity enum (noise-at-scale, open Q1), and the static per-node human-label map.
