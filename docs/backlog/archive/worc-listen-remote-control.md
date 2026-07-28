# Remote operator agent over Telegram (`worc listen`) — future vision

Status: **proposed** (exploratory — future vision) Date: 2026-06-26 Owner: Vladimir Makarevich

A stake-in-the-ground for a remote, conversational way to run the orchestrator. `worc listen` starts a long-running process that lets the operator drive and interrogate the orchestrator from a Telegram chat — by tagging the bot — instead of being limited to a terminal next to the machine. The **centerpiece is a small "navigator" agent**: the operator talks to it in natural language ("what's queued right now?", "rerun the ion-list task", "why did review fail?"), and it answers from the orchestrator's own redacted state and minimally drives it. The natural-language agent is the front door; underneath it sits a deterministic, bounded command surface that is also the agent's entire tool surface — so the LLM-in-the-loop can never do more than a safe command parser could. This is exploratory: it records the direction and its constraints, not a build-ready spec.

## The vision

Today the orchestrator is something you sit next to. `worc watch` is a headless daemon; the only attended ergonomics are local (interactive operator console, `worc shell`/`worc top`). The vision is to make the orchestrator something you can **carry in your pocket**: open Telegram, tag the bot, and ask it what it's doing or tell it what to do next — the same way you would lean over to the terminal, but from anywhere.

Concretely, the operator should be able to, from chat:

- **Ask what is going on** — "what tasks are pending / running / recently finished?", "status of `<id>`?", "show me why `<id>` failed" — answered from the same read-only state `worc list` / `worc status` already expose.
- **Get oriented in the project** — light navigation questions about the current work and recent history (the orchestrator's memory of what it has been doing), not a free read of the repository.
- **Drive the next step** — start a specific **already-queued** task, rerun a terminal one, stop/restart the daemon — over the bounded verb set, with consequential actions confirmed in-chat.

The "navigator agent" is what ties this together: rather than memorizing a command grammar, the operator converses, and the agent translates intent into the bounded verbs, proposing consequential actions for confirmation. It is an **orchestrator-level assistant** (a sibling of the supervisor layer), not a task-pipeline agent — its job is to help the operator see and steer, not to write code.

## The problem

Between "task started" and "task finished" a remote operator is both blind and mute. The only outbound signal is `send_notification` on a terminal status; the only inbound channel is a **correlated** `ask_human` gate (approve/deny a specific question the orchestrator chose to ask — the §14 dangerous-diff gate, and the proposed confirmation gates). There is no way for the operator to **initiate** an interaction: to ask "what's queued?", to redirect "run that one next", or to get oriented — without being at the machine. The local console solves exactly this, but only when you are attended at the terminal. Nothing solves it remotely, even though the Telegram transport for both directions already exists.

## What this is — and what it is not

In scope:

- A **conversational navigator agent** over Telegram as the primary interface, backed by a **deterministic command grammar** as both a fast path and the agent's tool surface.
- **Read-only navigation** of orchestrator state (queue, active task + node, recent terminal tasks, redacted logs/summary) — reusing the `worc list` / `worc status` read surfaces.
- **Bounded control over tasks that already exist as files**: enqueue an existing `tasks/pending` file, `rerun` a terminal task, `stop`/`restart` the daemon — i.e. a dispatcher over the existing `cmd_*` verbs.

Explicitly **not** in scope (the trust boundary):

- **Authoring task content from chat.** A chat message never becomes the body a provider executes. Task content reaches providers only as validated file paths through the §19 ingestion gate (security rules #8/#19); the bot dispatches over already-vetted artifacts, exactly as `worc shell` dispatches over `cmd_*`. (Operator-dictated **creation** of new tasks from chat — composing and running them — is the next rung beyond this vision; recorded in [§ Further horizon](#further-horizon-full-task-dictation-beyond-this-vision).)
- **New git / network / filesystem capability.** Only the orchestrator commits/pushes/PRs; the bot introduces none of that. The navigator agent gets **no raw shell, git, or filesystem tools** — its only tools are the bounded verbs.
- **A second engine host.** Like the local console, `listen` is a **client over the daemon**, never a place that runs `run_task`. The single-slot invariant is untouched.
- **Mid-task control beyond the existing stop ladder.** No per-stage cancel beyond what cli-upgrade.md already scopes.
- **A free read of the repository by the agent.** Navigation answers come from redacted orchestrator state, not from opening source files (that would reopen the secret-leak surface the redaction net exists to close).

## Constraints

From [.agents/rules/security.md](../../../.agents/rules/security.md), [architecture.md](../../../.agents/rules/architecture.md), and the code as it stands:

- **One bot token can be long-polled by only one process — the load-bearing technical fact.** Telegram `getUpdates` is exclusive: `check_telegram_preflight` already fails on a `409 Conflict` when a second poller appears (`notify/telegram.py`). Today `ask_human` polls `get_updates` **on demand**, only while waiting for an answer. A continuous command listener is a **continuous** poller, so it would collide with every on-demand `ask_human` gate. This forces a **single shared poll loop** owned by the daemon (the broker in [§ Architecture](#architecture-sketch)) that all inbound consumers subscribe to. It is not optional, and it retroactively binds the confirmation gates and step-trace items: once continuous listening exists, those cannot poll independently either.
- **Single configured chat; env-only credentials (rule #15).** The bot only ever talks to the one configured chat; bot token and chat id are environment-only and never enter SQLite, logs, argv, or artifacts. The trust boundary for accepting commands is therefore "messages from the configured chat" (optionally narrowed to a configured operator user id within it) — reusing the existing single-chat trust model rather than inventing auth.
- **No secrets in chat (hard invariant).** Every outbound line passes the existing redaction net; the navigator answers only from already-redacted state. An LLM in the loop must not be able to widen this — it sees redacted state, never raw env, secrets, or workspace files.
- **The provider layer stays dumb.** Command dispatch and the navigator live at the CLI/orchestrator level; `providers/` learns nothing about Telegram or remote control. If the navigator itself uses an LLM, it does so through the existing provider abstraction (as the supervisor does), but its **tool surface is the bounded verbs**, not provider-native filesystem/shell access.
- **Proposer proposes, Core decides.** The agent **proposes** actions; deterministic, audited code executes them; consequential ones (anything mutating: enqueue, rerun, stop) route through the existing `ask_human` approval before running. This mirrors the supervisor and skills-selection-rework pattern.
- **Fail-closed.** If `listen` is enabled while `telegram.enabled` is false, preflight refuses to start (an enabled remote-control surface with no transport is a misconfiguration, not a silent no-op) — the same posture the confirmation-gates item takes.

## How it fits what already exists

This is not a new transport — it is the **bidirectional, operator-initiated** layer over the Telegram channel the other items use one direction of:

| Existing / proposed item | Relationship |
| --- | --- |
| Interactive operator console (`worc shell` / `worc top`) | **Local twin.** Identical shape — a client/dispatcher over the daemon, never a second engine host — with Telegram replacing `prompt_toolkit` and the operator remote instead of attended. The verbs and read views are the same; only the transport and the conversational layer differ. |
| Telegram step-trace | **The outbound half.** Node→outcome feed. A consumer of the same single poll loop's sibling send path; the navigator's "what just happened?" answers overlap with it. |
| Operator confirmation gates | **The correlated-question half.** Approve/deny at decision points via `ask_human`. `listen` generalizes inbound from "answer the question the orchestrator asked" to "issue the command the operator chose"; both must share the broker. |
| `worc list` / `worc status` (shipped) | **The read surface the navigator queries.** "What tasks now?" reuses the `worc list` read helpers and `StateStore.open_readonly`; no new read machinery. |
| Supervisor layer + orchestrator memory | **The navigator's knowledge source and natural voice.** "Help me navigate current project affairs" is exactly a memory-backed assistant; the navigator is the conversational front-end to orchestrator memory. Connection, not dependency. |

## Architecture sketch

Three layers, bottom-up. Only L0 is a hard prerequisite for anything; L1 is independently useful without L2; L2 is the vision.

```text
┌─ Telegram chat (the one configured chat) ─────────────────────────┐
│  operator: "@worcbot what's queued?"   "@worcbot rerun ion-list"  │
└───────────────────────────────┬───────────────────────────────────┘
                                 │  one getUpdates long-poll (exclusive)
        ┌────────────────────────▼─────────────────────────┐
        │ L0  Telegram I/O broker (owned by the daemon)     │
        │     one poll loop → fans updates out to:          │
        │       • HITL correlation (ask_human gates)        │
        │       • step-trace / notifications (outbound)     │
        │       • command dispatch (new)                    │
        └───────────────┬───────────────────────┬───────────┘
                        │                       │
        ┌───────────────▼────────────┐  ┌───────▼────────────────────┐
        │ L1  Deterministic verbs     │  │ L2  Navigator agent (LLM)  │
        │  list / status / logs       │  │  NL → intent;              │
        │  run-existing / rerun        │  │  reads redacted state;     │
        │  stop / restart             │  │  PROPOSES L1 verbs;        │
        │  (configured-chat only)      │  │  tool surface = L1 ONLY    │
        └───────────────┬─────────────┘  └───────┬────────────────────┘
                        │  reuses cmd_* / read helpers │ consequential → ask_human
                        ▼                              ▼
        worc watch daemon (the ONLY engine host) · state.db (read-only) · tasks/pending
```

- **L0 — the shared poll broker (prerequisite).** Extract the one-shot `get_updates` polling that lives inside `ask_human`/`poll_reply` today into a single long-poll owned by the daemon, dispatching each update to the right consumer: a `callback_query`/reply that matches an open HITL handle → the waiter; an addressed-to-bot command → the dispatcher. This is the change that lets `listen` + gates + step-trace coexist on one token at all.
- **L1 — deterministic command grammar.** Addressed-to-bot, filter-prefixed verbs (e.g. `@worcbot list`, `@worcbot status <id>`, `@worcbot rerun <id>`) mapping straight onto existing `cmd_*` and the `worc list` read helpers. Cheap, auditable, no model in the loop. Mutating verbs are slot-guarded and gated by `ask_human`. This alone is the safe, high-value first slice — the remote `worc top` + a few control verbs.
- **L2 — the navigator agent (the vision).** When a message is not a plain L1 command, fall through to a small LLM agent: it parses intent, answers navigation questions from redacted state, and **proposes** L1 verbs for the operator to confirm. Its tool surface is exactly L1, so its blast radius equals a safe parser's — the LLM is a natural-language front-end, not an autonomous actor. The deterministic-first / agent-on-fallthrough split also keeps cost down: `@worcbot list` never spends a token; only "почему упала ion-list?" invokes the model.

**Launch model (reconciling the operator's two framings).** The 409 constraint pushes the **combined** mode to the fore: one daemon owning one poll loop.

- `worc watch --listen` (or `orchestrator.listen.enabled: true`) — the watch daemon also listens. This is the operator's `worc --watch & listen` intuition, made one process: the orchestrator works autonomously **and** the operator has remote control, with no token conflict.
- `worc listen` standalone — a listen-only daemon (no autonomous claiming). To also process tasks it would spawn-or-attach a `watch` child, exactly as the console does, so the poll loop stays single-owner.

## Alternatives considered

| Option | Why rejected / deferred |
| --- | --- |
| Do nothing — terminal notifications + the local `worc shell` | No remote, operator-initiated control; the stated goal. The local console is attended-only. |
| Two processes (`watch` polling `ask_human` + a separate `listen` poller) | **Impossible as-is** — both long-poll one bot token and 409 against each other. Forces the L0 broker regardless. |
| Webhook instead of long-polling | Preflight deliberately requires polling and **fails if a webhook is set**; webhooks need a public HTTPS endpoint. Out of scope. |
| Deterministic commands only, no LLM (ship L0+L1, stop there) | **Not rejected — it is the foundation.** L1 is the safe first slice and the agent's substrate; the navigator (L2) is the vision layered on top, not a replacement. |
| Let the navigator hold raw shell/git/filesystem tools | Violates the dumb-provider and only-orchestrator-commits invariants and reopens the secret surface. The bounded-verb tool surface is the safety crux. |
| Author new task content from chat | Bigger trust surface; would still have to land as a validated file through §19. **Deferred to the [further horizon](#further-horizon-full-task-dictation-beyond-this-vision)** — this vision's framing ("run a specific task", "rerun") is dispatch-over-existing; full dictation is the rung beyond it. |
| Accept commands from any chat / open the bot | Breaks rule #15 (single configured chat) and turns the bot into a remote-execution surface for strangers. The configured chat is the trust boundary. |

## Decision

Build, eventually, a **remote operator interface over Telegram whose centerpiece is a conversational navigator agent**, resting on two foundations: (L0) a single daemon-owned Telegram poll broker that all inbound/outbound consumers share, and (L1) a deterministic, configured-chat-only command grammar that maps onto existing `cmd_*`/read helpers and serves as the navigator's entire tool surface. The navigator (L2) translates natural language into proposed L1 actions, answers navigation questions from redacted state, and routes every mutating action through the existing `ask_human` approval. The bot is a **client/dispatcher over the daemon**, bounded to **already-vetted artifacts** — it never authors task content, never gains new git/network/filesystem power, and never becomes a second engine host.

We do this because it makes the orchestrator steerable from anywhere while changing none of the safety invariants — the LLM gets reach no wider than a safe command parser. The cost of the alternatives: two-process designs are physically impossible on one bot token (409); a no-LLM design is a strictly smaller subset (and is in fact our own foundation); a raw-tool agent or an open bot would trade the whole security model for convenience.

## Further horizon: full task dictation (beyond this vision)

The furthest rung — beyond this document's scope, recorded here as a deliberate stake-in-the-ground. Once the navigator (L2) is trusted, the natural escalation is a **full authoring agent**: the operator dictates a task in natural language ("add a `worc logs clean` command with `--keep N`, wire it into the CLI, add tests"), and the agent **composes, creates, and runs** it end-to-end — not just dispatching work that already exists, but originating it. This is the part of the original idea ("ask some details about the project", a "simple agent that can manage the orchestrator remotely") taken to its conclusion: dictate, and it runs.

Crucially, this does **not** bypass the ingestion model. A dictated task still **materializes as a validated task file** and enters through the §19 gate exactly like a hand-written one; the provider still only ever sees a validated file path (rule #8 holds). The chat becomes a new **authoring front-end** for task files, never a new execution path. What it adds is a **draft → confirm → enqueue** loop: the agent drafts the task file from the dictation (asking clarifying questions as needed, the way [`/clarify-task`](../../../.claude/skills/clarify-task) would), **shows the concrete draft back in chat for approval**, and only an approved draft is written into `tasks/pending`. Proposer-proposes / Core-decides still holds — the operator confirms the actual artifact before it becomes work.

What genuinely widens, and why this is the furthest rung: the chat can now **introduce new work**, not just trigger vetted work — a materially larger trust surface than the rest of this vision. So it needs the strongest intent-authentication (configured chat **and** operator user id, at least), an explicit per-task confirmation of the drafted file, and the same secret-free posture (the draft is plain text the operator reads in full before it runs). It also leans hardest on the agent's quality: a misunderstood dictation becomes a real run. That is exactly why it sits behind the read-only and dispatch-over-existing rungs — those prove the interaction and the trust model first; full dictation is the reward for getting them right.

## Open questions

- **Broker feasibility.** Extracting one shared `get_updates` loop without regressing the existing HITL correlation/timeout semantics is the crux. Confirm a single poller can cleanly fan out to HITL waiters **and** the dispatcher, and that the confirmation-gates / step-trace items refactor onto it rather than fighting it.
- **Which model runs the navigator, and at what cost/bound.** A per-message LLM call adds tokens and latency. The deterministic-first split limits it to genuinely conversational turns, but the model choice, turn budget, and a per-window rate/cost cap are open. Likely a small/cheap model with a tight cap.
- **Intent authentication.** Is "from the configured chat" enough, or is a configured operator **user id** within the chat warranted (group chats, forwarded messages)? Lean toward an optional user-id narrowing.
- **LLM secret-leak surface.** Even reading only redacted state, does letting a model compose free-text answers create any new exfiltration path the line-based redaction net does not cover? Needs a deliberate look before L2.
- **Sequencing vs. siblings.** L0 is a shared prerequisite for this, gates, and step-trace — build it once, first? And how much of L1 is literally the console's verb set re-skinned for Telegram (shared dispatcher) vs. duplicated?
- **Navigator memory.** Does the navigator get its own short-term context, or is it purely the read-only voice of orchestrator memory? The latter keeps it bounded; the former is richer but unbounded.

## Implementation notes

Pointers, not a spec — this is exploratory.

- **CLI**: a `listen` subparser + `cmd_listen` in `cli.py` (same pattern as `cmd_watch`: load config, build orchestrator, `StopController` + PID file), plus a `--listen` flag / `orchestrator.listen.enabled` to fold listening into `cmd_watch`.
- **L0 broker**: refactor the on-demand polling in `notify/telegram.py` (`poll_reply` / `wait_for_answer`, `_ALLOWED_UPDATES`) into a single owned long-poll that routes updates to HITL handles vs. the command dispatcher. This is the bulk of the new code and the thing the other two Telegram items also need.
- **L1 dispatch**: reuse the `worc list` read helpers + `StateStore.open_readonly` for queries; reuse `cmd_status`/`cmd_rerun`/`cmd_stop`/enqueue-into-`tasks/pending` for verbs; gate mutating verbs with `find_active_tasks()` (slot guard) and `ask_human` (approval). Accept only the configured chat id.
- **L2 navigator**: an orchestrator-level agent (sibling of the supervisor) invoked through the provider abstraction, with a tool schema restricted to the L1 verbs; never raw shell/git/fs. Fall through to it only when L1 does not parse.
- **Config**: new keys under `telegram`/`orchestrator` (e.g. `listen.enabled`, optional `listen.operator_user_id`, navigator model/cap). Additive optional fields — confirm whether `CONFIG_SCHEMA_VERSION` needs a bump or loader tolerance suffices. Add the fail-closed preflight rule (listen-on requires `telegram.enabled`).
- **Tests**: broker fan-out (an HITL reply and a command in the same update batch route correctly); L1 verb dispatch (deterministic parse → `cmd_*`, slot guard refuses while busy, non-configured chat ignored); navigator proposes a verb and a mutating proposal still hits the approval gate; fail-closed preflight when listen-on without Telegram. Reuse the `/fake-cli` fixtures for any spawned `watch` child.
