# Token optimization campaign — follow-ups

The campaign's running list of things that outlive the item that produced them: open decisions, watch items, operational consequences, and deliberate non-goals. Each item's own document stays the record of what it did and why; this file exists so the residue does not have to be rediscovered by re-reading five documents.

**Seeded by P0 (2026-08-03).** The two earlier shipped items (`normalized-usage-accounting`, `content-flow-token-hygiene`) pre-date this file and recorded their residue in their own documents.

How to use it:

- Add an entry when an item lands and leaves something behind that a later reader would otherwise have to rediscover. Name the item it came from, in parentheses, in the heading — `(P0)` — so the source is one click away.
- Keep an entry only while it can still cost something. **Delete it when it is done or has become untrue**, rather than marking it resolved — this is a live list, not a changelog.
- Per-item detail that is too long for a shared list goes in a sibling `<item-id>/follow_ups.md`, with a short pointer here.

Sections:

- [Needs a decision](#needs-a-decision) — someone has to answer before it can close
- [Watch items](#watch-items) — nothing to do unless the world changes
- [Operational consequences](#operational-consequences) — true of every target repo, not of the code
- [Carried into `main`](#carried-into-main) — the derived-docs refresh backlog
- [Deliberate non-goals](#deliberate-non-goals) — decided against; recorded so they are re-decided, not re-discovered

## Needs a decision

Nothing open. (P1 closed the one entry that lived here: the always-written deterministic step record was dropped — `node_runs` is the step record. Recorded as deviation 2 in [P1](supervisor-observation-cadence-p1.md#отклонения-от-текста-задачи-2026-08-03).)

## Watch items

This campaign's headline numbers are **measured**, so its watch items are mostly "does this number still hold".

### P0's three run-only thresholds are unmeasured (P0)

Everything checkable without a real run is covered by tests; three acceptance criteria are not, because they need one. On the first `blog_article_revise` after P0 merges, read: the finalize row's input (the real threshold — «role_file + prompt + packet ≤ 16 KB», and it must **not** grow with the rework count), the supervisor share of the run's Claude input (a control number, ~60–65% expected — its threshold is P1's, not P0's), and the average observe call (should not move: P0 only capped `final_message`). Queries are in [P0 §A/B и метрики](supervisor-finalize-packet-and-cadence.md#ab-и-метрики-решение-x1-пересмотрено-2026-08-03) — and an already-existing run on current `dev` is a free "before" (`provider_attempts` rows are never pruned). Delete this entry once the numbers are recorded.

### P1's two run-only thresholds are unmeasured (P1)

Everything checkable without a run is covered by tests; two of P1's acceptance numbers are not. On the first `blog_article_revise` after P1 merges, read: that the run made **exactly one** supervisor call (the finalize — a structural identity on a `mode: none` flow, no baseline needed) and that the supervisor share of the run's Claude input is **≤ 20%** (historically ~70%). Same queries as P0's, in [P0 §A/B и метрики](supervisor-finalize-packet-and-cadence.md#ab-и-метрики-решение-x1-пересмотрено-2026-08-03). Also worth a glance on the first `implementation` run: that `events` fired only where something actually deviated, and that `follow_ups` did not thin out now that ordinary steps go unobserved. Delete this entry once the numbers are recorded.

### Slot nodes contribute no `steps[].message` to the packet (P0)

`steps[].message` is read from each run's own `<node_id>.out.md`, which slot nodes (`output_artifact`: plan / diff / report / summary) never write — their product goes to the slot instead. Harmless today: their product reaches the packet as `changes` / `findings_path`, and the interpretive nodes that carry the run's narrative are ordinary agent nodes. Becomes worth revisiting only if a flow ever makes a slot node the substantive step of the run, or if [P1](supervisor-observation-cadence-p1.md)'s `observe.mode: none` leaves a flow whose whole narrative sits in slot nodes.

## Operational consequences

### A target repo with its own tracked `.worc/flows/` keeps its old supervisor wording (P0)

P0 changed the packaged supervisor role prompts (`roles/supervisor.md`, `implementation/{supervisor,summary}.md`): the observe lens no longer claims to watch _every_ step, and the finalize lens is told to read the run's facts from the packet rather than recall them. A repo that tracks its own copy of `.worc/flows/` (the documented setup for owning your prompts) does **not** pick these up — it keeps a finalize lens that asks the turn to summarize "what you noted across the steps" while the turn now runs in a fresh session with no such memory. The behavior is still correct (the orchestrator appends its own packet instruction to whatever lens is in force), but the operator's own wording is now working against it. Worth a line in the upgrade notes when the derived docs are refreshed.

### A target repo with its own tracked `.worc/flows/` misses the per-flow cadence defaults (P1)

Same mechanism as P0's entry above, with a bigger bill: the per-flow cadence defaults are in the packaged flow YAML (`observe.mode: none` on the content flows, `events` on `implementation`), so a repo that tracks its own copy of `.worc/flows/` does **not** pick them up. Those flows declare no cadence, so they inherit the global default — which is `events`, i.e. still the saving, just not the flow-specific choice. The operator gets the full benefit by adding `observe: {mode: none}` to their own content flows' `supervisor:` block. Worth a line in the upgrade notes.

### `observe.mode: none` globally is refused for a flow that declares `events` (P1)

A flow may only narrow the operator's cadence, so `implementation.yaml`'s declared `events` is _broader_ than a global `none` and fails flow validation before any node runs. Deliberate — the flow is asserting that its `emit_follow_ups` needs deviation notes, and a loud refusal beats quietly thinning the follow-ups — but it does mean "turn observations off everywhere" is not a single global switch for that flow. The operator narrows their own copy of the flow instead. Expect this question the first time someone tries it.

The sting is that the rejection lands _after_ the task is claimed: resolution runs before branch prep (so no provider runs, no branch, no commit), but the task still ends in terminal `failed` and has to be re-queued by hand — and a `watch` loop burns the whole queue one task at a time. `worc validate-flow --all` runs the same validator read-only without claiming anything, and its exit codes are built for `worc validate-flow --all && worc watch`; the shipped `guide/config/reference.md` now says so under `observe.mode`. Worth repeating in the upgrade notes, because there is no flow preflight inside `run` / `watch` / `ready` — `check_flows` has exactly one caller, the `validate-flow` command.

### An existing `state.db` is refused on the first run after P2 (P2)

P2 bumped `DB_SCHEMA_VERSION` to 20 for the per-function spend column. Any `.worc/state.db` written before it is refused fail-closed on open — the house greenfield rule, exactly as v16 and v19 did — so the first `worc` invocation in a target repo that already has one fails until the file is deleted. Two things make this cheap rather than alarming: the refusal names the fix, and nothing of value is lost (the ledger `logs/completed.jsonl` and the per-task artifacts under `logs/` are separate files and survive). It is worth knowing _before_ the first post-merge run rather than during it, in every repo the operator has ever pointed `worc` at — a `watch` daemon meets it on its next task, not at a convenient moment. Delete this entry once those databases have been cycled.

## Carried into `main`

The `docs/` tree is reconstructed on `main` from the merged `dev` diff as a separate task. Doc-impact notes accumulate here so that reconstruction has breadcrumbs rather than a bare diff.

### P0 — supervisor finalize and cadence (P0)

- **`worc_architecture.md`** — the supervisor layer's description: it no longer observes every executed node (`tool` / `checks` / `publish` are excluded, keyed on node _kind_), and `finalize` is no longer a warm-session continuation but a fresh turn seeded by a deterministic `SupervisorPacket` published to `.worc-io/<task-id>/supervisor/packet.json` and carried on a new `AgentRunRequest.supervisor_packet_path`. The normal and revive paths are now one path. Also: the exchange gained a `supervisor/` sub-tree, so anything describing the exchange layout or the terminal seal's contents needs the packet added.
- **`configuration.md`** — no schema change (P0 deliberately added no config key), but the `supervisor` section's prose is stale in two ways: it says the layer observes "each step", and it does not mention that the finalize turn's input is now bounded independently of run length. The absence of a toggle is itself a decision — see the non-goal below.
- **`glossary.md`** — `SupervisorPacket` is a new term worth an entry.

### P1 — supervisor observation cadence (P1)

- **`configuration.md`** — the `supervisor` block changed shape, which is the largest doc delta of the campaign. Flat `supervisor.model` / `supervisor.reasoning` are **gone** (schema v33); model and effort are per phase under `observe` / `finalize` / `handoff`, `role_file` and `provider` stay top-level, and `observe` also carries the cadence (`mode: all|selected|events|none`, default `events`, plus `triggers` and `include_nodes`). Needs: the new key table, the mode table with what each mode costs, the narrowing rule (a flow may only narrow; rank `none < events < selected < all`, and `selected` counts as broader than `events`), the note that no budget keys exist, and the migration line — the loader rejects a flat key by name and `worc upgrade-config` strips it without carrying the value over. The shipped `packaged/guide/config/reference.md` is already rewritten and is the best source to reconstruct from.
- **`worc_architecture.md`** — the supervisor layer no longer observes every executed node: the kind gate (`tool` / `checks` / `publish`) is now joined by a cadence gate, and the default is deviations-only. Three triggers, closed set: `rework` (incl. an evaluator's give-up accept), `failure`, `fallback` — the last two read from the step's own `node_runs` row, so the post-node hook's contract is unchanged. New pure module `core/observe_cadence.py` holds the rank table, the resolution, and the trigger detection; the flow-vs-config narrowing check lives in the flow validator's config-aware layer next to `permission_ceiling`. Also worth stating the invariant explicitly: per-flow defaults are data in the flow YAML, never a flow-name branch in the engine.
- **`glossary.md`** — `observe.mode` / observation cadence, and "trigger" in the `events` sense, are new terms.
- **Upgrade notes** — both operational consequences above (a tracked `.worc/flows/` keeps the global cadence; global `none` is refused for a flow declaring `events`).

### P2 — responsibility split and per-function telemetry (P2)

- **`worc_architecture.md`** — the supervisor layer's description gains the split it now actually has: the _facts_ of a run (each executed node, its outcome, the provider it landed on, whether it fell back, what it reported, which checks ran) are stated by the flow recorder in `core/flow/recorder.py` as `StepFacts`, with no LLM involved; the layer contributes _interpretation_ only (the optional per-deviation note and the final synthesis). Two consumers read that one derivation — the finalize packet (`core/supervisor_packet.py` is now pure rendering: `PacketFacts` carries `steps`, not raw `node_runs` + a message map) and the observation cadence gate (`observe_cadence.triggers_for` takes a decided `fell_back` boolean instead of re-deriving it from the route columns). Worth stating as the layering rule it encodes: the flow layer owns facts about a node run, the supervisor layer owns its own vocabulary — which is also why `SupervisorFunction` lives in `core/supervisor_usage.py` and the store only ever sees a plain string. A new import contract (`step-record-below-supervisor`) machine-enforces the direction; it is deliberately scoped to the one module, because a blanket `core.flow` source trips the pre-existing `TYPE_CHECKING` edge from `core.flow.wiring` to the orchestrator.
- **`configuration.md`** — no schema change (`CONFIG_SCHEMA_VERSION` stays 33), but two operator-visible facts are new: `provider_attempts` gained `supervisor_function` at **DB v20** (`observe` / `finalize` / `handoff` / `skill`, NULL for a graph node — so "what did the observations cost against the summary" is one `GROUP BY`, and an older `state.db` is refused fail-closed and recreated as with v16/v19), and every run writes a `supervisor_usage` block into the local-only `summary.json`. The shipped `guide/config/reference.md` (under `observe.mode`) and `guide/flows/roles.md` are already rewritten and are the best source to reconstruct from.
- **`glossary.md`** — `StepFacts` / "step record" and the per-function spend label are new terms.
- **Upgrade notes** — the DB version bump: a `state.db` from before this change is refused on open and has to be deleted (greenfield rule, same as v16 and v19).

## Deliberate non-goals

Decided against, not overlooked.

### No config toggle for fresh finalize (P0-D4)

The warm-resume branch was deleted outright rather than put behind a key. A toggle would have forced a config-schema bump inside a phase that changed no schema, and a second code path in the tests forever — for a greenfield project that owes no backward compatibility. A warm auto-fallback "if the packet fails to build" was refused for a sharper reason: it would restore non-determinism in the one path P0 makes reproducible, and mask the build failure. The rollback is `git revert`; the only fallback is the orchestrator's deterministic minimal summary, and a failed build is recorded as `packet_built: false` on the `supervisor_final` row. [P1](supervisor-observation-cadence-p1.md) adds no `finalize.session` key either (P1-D8) — a single-valued key is dead config.

### No `finalize.enabled` / `handoff.enabled` keys (P1)

The P1 document's illustrative YAML showed both, but no decision (P1-D1…D8) and no acceptance criterion asked for them, so they were left out. `finalize.enabled: false` would create a run that produces no summary — and therefore no pull-request body — which nobody requested and which the same phase deliberately made _more_ reliable; `handoff.enabled` duplicates what a flow's `handoff_role_file` and its decomposition block already decide. Same reasoning as P0-D4's refusal of a fresh-finalize toggle: a key whose only interesting value degrades the run is not a feature. If a real need appears, it arrives with the case that motivates it.

### No flow-local `observe` keys beyond `mode` (P1)

The flow-local `supervisor.observe` block accepts `mode` and nothing else — `include_nodes`, `triggers`, `model` and `reasoning` are operator-only, and a flow naming them fails closed. Cost belongs to the operator, not to authored content: a flow saying "watch me with the expensive model" would be spending someone else's budget. `mode` is the exception because narrowing it only ever spends _less_. The block is nested (rather than a flat `observe_mode`) precisely so `include_nodes` could join it later without a second rename.

### No `flow.final_status` field in the packet (P0)

The item's field list named it, but `finalize` runs from exactly one call site — the publish hook on the success path — so the value would always be `done`: a constant a reader of the packet would mistake for a live signal. The packet carries `flow {name}` only.

### No CLI read surface for supervisor usage (P0, narrowed by P2)

`worc usage` / a block in `worc status` stays out. [P2](supervisor-responsibility-split-p2.md) delivered the read surface the operator actually needs without a command: a `supervisor_usage` block in the local-only `.worc/logs/<task-id>/summary.json`, per job, on every run. `summary.md` deliberately gets none of it — that file is the pull-request body, and the spend belongs to whoever pays it, not to the reviewer. The campaign's own thresholds are still read by SQL over `provider_attempts` (now one `GROUP BY` on the per-function label rather than "the last supervisor row"), which is why a command would only re-render data two surfaces already carry.

One known hole, decided not worth closing: on a **degraded** run (finalize produced nothing) the orchestrator's deterministic minimal summary rewrites `summary.json` with its own four-key contract, so the spend report is absent exactly where the layer still spent something. The authoritative rows are in `provider_attempts` regardless; teaching the fallback a key it does not own would break the contract test for a degenerate case.
