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

### Does P1 still need its "always write a deterministic step record"? (P0 → P1)

[P1](supervisor-observation-cadence-p1.md) plans (item 3 of its implementation sketch) to write a deterministic step record with `note=""` on every post-node hook call, so that "the ledger and the packet stay complete even when observations are off". P0 reached that completeness by another route: the packet's steps come from `node_runs` and each run's own `<node_id>.out.md`, neither of which depends on an observation having run. So the extra always-written `evaluations` row may now buy nothing for the packet — it would only matter if something else wants an observation-shaped row per step (P2's `StepRecorder` is the plausible claimant). Decide when P1 is implemented: keep it for P2's sake, or drop it and let `node_runs` be the step record. Do not silently do both.

## Watch items

This campaign's headline numbers are **measured**, so its watch items are mostly "does this number still hold".

### P0's three run-only thresholds are unmeasured (P0)

Everything checkable without a real run is covered by tests; three acceptance criteria are not, because they need one. On the first `blog_article_revise` after P0 merges, read: the finalize row's input (the real threshold — «role_file + prompt + packet ≤ 16 KB», and it must **not** grow with the rework count), the supervisor share of the run's Claude input (a control number, ~60–65% expected — its threshold is P1's, not P0's), and the average observe call (should not move: P0 only capped `final_message`). Queries are in [P0 §A/B и метрики](supervisor-finalize-packet-and-cadence.md#ab-и-метрики-решение-x1-пересмотрено-2026-08-03) — and an already-existing run on current `dev` is a free "before" (`provider_attempts` rows are never pruned). Delete this entry once the numbers are recorded.

### Slot nodes contribute no `steps[].message` to the packet (P0)

`steps[].message` is read from each run's own `<node_id>.out.md`, which slot nodes (`output_artifact`: plan / diff / report / summary) never write — their product goes to the slot instead. Harmless today: their product reaches the packet as `changes` / `findings_path`, and the interpretive nodes that carry the run's narrative are ordinary agent nodes. Becomes worth revisiting only if a flow ever makes a slot node the substantive step of the run, or if [P1](supervisor-observation-cadence-p1.md)'s `observe.mode: none` leaves a flow whose whole narrative sits in slot nodes.

## Operational consequences

### A target repo with its own tracked `.worc/flows/` keeps its old supervisor wording (P0)

P0 changed the packaged supervisor role prompts (`roles/supervisor.md`, `implementation/{supervisor,summary}.md`): the observe lens no longer claims to watch _every_ step, and the finalize lens is told to read the run's facts from the packet rather than recall them. A repo that tracks its own copy of `.worc/flows/` (the documented setup for owning your prompts) does **not** pick these up — it keeps a finalize lens that asks the turn to summarize "what you noted across the steps" while the turn now runs in a fresh session with no such memory. The behavior is still correct (the orchestrator appends its own packet instruction to whatever lens is in force), but the operator's own wording is now working against it. Worth a line in the upgrade notes when the derived docs are refreshed.

## Carried into `main`

The `docs/` tree is reconstructed on `main` from the merged `dev` diff as a separate task. Doc-impact notes accumulate here so that reconstruction has breadcrumbs rather than a bare diff.

### P0 — supervisor finalize and cadence (P0)

- **`worc_architecture.md`** — the supervisor layer's description: it no longer observes every executed node (`tool` / `checks` / `publish` are excluded, keyed on node _kind_), and `finalize` is no longer a warm-session continuation but a fresh turn seeded by a deterministic `SupervisorPacket` published to `.worc-io/<task-id>/supervisor/packet.json` and carried on a new `AgentRunRequest.supervisor_packet_path`. The normal and revive paths are now one path. Also: the exchange gained a `supervisor/` sub-tree, so anything describing the exchange layout or the terminal seal's contents needs the packet added.
- **`configuration.md`** — no schema change (P0 deliberately added no config key), but the `supervisor` section's prose is stale in two ways: it says the layer observes "each step", and it does not mention that the finalize turn's input is now bounded independently of run length. The absence of a toggle is itself a decision — see the non-goal below.
- **`glossary.md`** — `SupervisorPacket` is a new term worth an entry.

## Deliberate non-goals

Decided against, not overlooked.

### No config toggle for fresh finalize (P0-D4)

The warm-resume branch was deleted outright rather than put behind a key. A toggle would have forced a config-schema bump inside a phase that changed no schema, and a second code path in the tests forever — for a greenfield project that owes no backward compatibility. A warm auto-fallback "if the packet fails to build" was refused for a sharper reason: it would restore non-determinism in the one path P0 makes reproducible, and mask the build failure. The rollback is `git revert`; the only fallback is the orchestrator's deterministic minimal summary, and a failed build is recorded as `packet_built: false` on the `supervisor_final` row. [P1](supervisor-observation-cadence-p1.md) adds no `finalize.session` key either (P1-D8) — a single-valued key is dead config.

### No `flow.final_status` field in the packet (P0)

The item's field list named it, but `finalize` runs from exactly one call site — the publish hook on the success path — so the value would always be `done`: a constant a reader of the packet would mistake for a live signal. The packet carries `flow {name}` only.

### No operator read surface for supervisor usage (P0)

`worc usage` / a block in `worc status` stays out. The data is already in `provider_attempts` and the acceptance thresholds are read with two SQL queries; a rendered surface is [P2](supervisor-responsibility-split-p2.md) items 3–4, on top of the per-function label that makes the query direct instead of "the last supervisor row".
