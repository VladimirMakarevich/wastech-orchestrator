# Memory: durable concepts, not an episodic task ledger

Status: **proposed** (2026-07-11) Date: 2026-07-11 Owner: Vladimir Makarevich

A V2 refinement of the shipped [orchestrator memory](memory/index.md) subsystem (V1 = [ADR-0001](memory/adr.md), phases 01–05, PR #14). It sharpens what memory injects into an agent's context: carry distilled, durable project knowledge (principles, conventions, entity cards, failure remedies) and stop carrying an episodic ledger of orchestrator task runs. The episodic tier stays as internal bookkeeping but is never rendered into a packet, and the durable tier is re-anchored on concepts (names/principles) rather than churn-prone file paths, with a more flexible capture rule so high-trust knowledge lands without waiting for orchestrator recurrence.

## The problem

The memory packet that gets injected into agent context (`memory/packet.py`) carries three kinds of low-value or misleading content today.

Ephemeral, rotting pointers reach the agent. The "Recent episodes" section renders raw task ids verbatim — `- task <task_id> — task=done (touched: ...)` (`packet.py:340-352`) — and an episode's only artifact pointer is the repo-relative `.worc/logs/<task-id>` directory (`orchestrator.py:2180-2184`). Tasks and their logs are short-lived and get cleaned up quickly, so these become dead references. The same rot affects `Evidence.ref` values that point at a task id or a commit SHA (in a greenfield repo, task branches are deleted and history is squashed/rebased after merge, so commit-anchored evidence rots too), and the durable `seen_task_ids` / `last_seen_task_ids` provenance lists (`records.py:132-136`, `records.py:154`).

The episodic ledger is a misleading, partial view. The "Recent episodes" section reads like "what has been done in this repo", but the orchestrator only ever sees the tasks it ran itself. The operator frequently does work outside the orchestrator, so this ledger is a strict subset presented as the whole — it confuses more than it helps. And it carries almost no information anyway: `stage_outcomes` is a single terminal token, `{"task": "done"}`, and `task_type` is never populated (`orchestrator.py:2171-2185`) even though the packet ranks episodes by it (`packet.py:171`).

The durable tier — the part the operator actually wants — is fragile and under-fed. Entity cards and lesson scopes are anchored on file paths (`service.py:341`; `Scope.paths`), so an ordinary refactor/rename quarantines the whole card or lesson (`cleanup.py:126-241`, mitigated only by single-candidate basename remap). And promotion out of short-term holding is gated on recurrence across tasks (`promote_min_tasks: 2`, `service.py:303-320`); because the operator works around the orchestrator, the same lesson rarely recurs through it, so genuinely durable knowledge stays stuck in `pending.jsonl`.

## Constraints

Preserve the subsystem's hard invariants from the V1 design ([memory/index.md](memory/index.md)):

- **Model-free except the supervisor proposal.** Nothing in `memory/` calls an LLM (`memory/__init__.py:9`); the only LLM touch is the supervisor _proposing_ a candidate delta. Any new capture/promotion rule must stay deterministic on the write path.
- **Advisory only.** Memory never enforces; Core decides. The packet header already says "advisory — verify against the code".
- **No secrets, no raw diffs, no unbounded context** in the store or the packet.
- **Reversibility.** Every write batch snapshots touched tiers first; any re-keying of existing records must go through the same snapshot/restore path.
- **Cross-platform paths.** Any path attribute stays POSIX repo-relative (`Path.as_posix()`).
- **Greenfield, no migration machinery.** There is no deployed store to migrate; a store-format change may be handled by `worc memory clear` on upgrade rather than a migration.

## Alternatives considered

| Option | Why not chosen |
| --- | --- |
| **Do nothing** | Leaves the three problems: dead task/log/commit pointers in-context, a misleading partial "what was done" ledger, and a path-fragile, recurrence-starved durable tier. |
| **Episodes: anonymize instead of hide** — keep an episodic section but strip task id + log pointer and render anonymous "hot path" areas | Still injects a partial, orchestrator-only activity view (the core of complaint #2) and re-introduces a rendered section whose only honest signal (changed-path frequency) is better derived live from git than from an incomplete memory ledger. |
| **Episodes: delete the short-term tier entirely** | Investigation (open question 1, resolved) confirmed the tier becomes write-only for retrieval after de-injection — recurrence counting is backed by `seen_task_ids` on the records, not by episodes. We still keep the tier shell deliberately (see the decision) as raw material for a future V2/V3 (embeddings/graph) consumer; deleting it now would only trade a cheap write for having to re-introduce the tier later. |
| **Durable tier: flexible capture only** (loosen promotion, keep path anchoring) | Addresses the recurrence-starvation half but leaves cards/lessons quarantining on ordinary refactors — the "concepts vs paths" complaint (#3) is untouched. |
| **Durable tier: anti-rot only** (fix pointers + drop the ledger, don't touch anchoring/promotion) | Fixes the leakage but leaves the durable tier fragile and under-fed, so the packet stays thin on the project concepts the operator wants. |

## Decision

We split memory's mandate cleanly: **the injected packet carries only durable, distilled project knowledge; the episodic record of orchestrator runs becomes internal-only and is never rendered.** We do this because all three complaints share one root — the episodic/id/pointer layer — while the concept layer the operator wants already exists and only needs to be un-starved and un-path-anchored. The cost is losing the in-context "what recently changed" signal (accepted: it was a misleading partial view) and trusting the deterministic trust-classifier more at capture time (accepted: promotion-on-first-sight is restricted to durable-trust records).

Concretely, three moves:

1. **Episodic tier → write-only shell, not injected.** Drop the "Recent episodes" section from the packet entirely. Task ids, `.worc/logs/<task-id>` pointers, and commit SHAs never appear in anything an agent reads. Episodes are still written (cheap, deterministic) but the tier is now **write-only for retrieval** — nothing reads an episode's content once the section is gone (recurrence counting is backed by `seen_task_ids` on the long-term/pending records, not by the episode store). We keep the tier shell deliberately as raw material for a future V2/V3 consumer (embeddings/graph), not because anything reads it today; the written rows still drop rotting pointers (see implementation notes) so a future consumer never ingests dead refs.

2. **No ephemeral pointers in the rendered packet.** Whatever is injected cites only durable, resolvable references — repo files, docs, named checks. Task-id / commit-SHA / log-dir refs may persist internally as provenance but are filtered out of `render()`, never shown as a "see …" pointer.

3. **Durable tier → concepts, captured flexibly.** Anchor entity cards on `(canonical_name, entity_type)` with `paths` as a mutable attribute (a moved file updates the attribute; it does not quarantine the card). Treat path-less "principle" lessons (architecture invariants, conventions) as first-class. Promote high-trust knowledge — `HUMAN_CURATED` / `REVIEW_VERIFIED` — on first sight, keeping the recurrence gate (`promote_min_tasks`) only for `AGENT_INFERRED` proposals. Reframe the supervisor's delta prompt (`supervisor.py:883-897`) to elicit principles/conventions/entity knowledge and to stop asking for task narration or commit-anchored evidence.

## Open questions

1. **~~Does any internal consumer still read the episode store once it is not injected?~~ Resolved (2026-07-11).** Investigated: after de-injection the only remaining reads are TTL expiry (`cleanup._expire_episodes`, timestamps + id) and two `len()` count displays (`worc memory show`; the `clear`/`tier_counts` confirmation) — none read episode content. Recurrence/promotion is backed entirely by `seen_task_ids` on the long-term/pending records (`service.py:248-311`, `lifecycle.py:88-111`), not by the episode store. **Decision: keep the tier shell** as deliberate raw material for a future V2/V3 consumer (embeddings/graph); it is write-only for retrieval in V2 and that is accepted (over "delete the tier").
2. **Commit-SHA evidence** (`Evidence` with a commit `ref`): keep it as internal, non-rendered provenance (alongside `first_seen_commit`), or forbid it at capture time so the supervisor never anchors on a rotting ref? Leaning "keep internal, never render".
3. **Existing path-keyed entity store**: on the re-key to `(name, type)`, rely on `worc memory clear` at upgrade (greenfield, disposable), or let the existing duplicate-merge in `CleanupJob` collapse old path-keyed rows into the new name-keyed ones?
4. **Does re-keying entities by name lose the path-scoped retrieval signal** the packet uses for ranking (path overlap)? Paths stay on the card as an attribute, so overlap ranking should still work — confirm the selection/ranking code reads the attribute, not the key.

## Implementation notes

Levers, roughly in order of leverage:

- **Packet render** (`memory/packet.py`): remove the episode section from `render()`/`build()` (`_select_episodes`, `packet.py:163-172`, and the episode branch of `_format`); add a render-time filter that drops non-durable evidence refs (task-id/commit/log-dir) from lesson/entity output.
- **Supervisor prompt** (`core/supervisor.py:883-897`, the `with_delta` branch): reword to ask for durable principles/conventions/entity cards with doc/architecture/file evidence; explicitly say not to record which task did what and not to anchor evidence on commit SHAs.
- **Promotion gate** (`memory/service.py:303-320`, `_ingest_long_term`): make `should_promote()` bypass the recurrence threshold when trust is in `DURABLE_TRUST_LEVELS` minus `AGENT_INFERRED` (`trust.py:30-37`); keep the recurrence gate for agent-inferred.
- **Entity identity** (`memory/service.py:326-379`, `_ingest_entity`; `_derive_id`): key on `(canonical_name, entity_type)`; treat `paths` as a mutable attribute; in `CleanupJob` (`memory/cleanup.py:126-241`), a vanished path updates/clears the attribute instead of quarantining the card, and a path-less lesson stays intact (already the case).
- **Episode write** (`core/orchestrator.py:2146-2193`): the tier stays (write-only shell, see decision), so drop the `.worc/logs/<task-id>` `artifact_paths` entry (dead pointer) and the always-`done` `stage_outcomes` from the written episode — even a write-only row should carry no rotting ref a future consumer would ingest.
- **Config** (`config/schema.py:478-514`, `packaged/config.example.yaml:250-268`): no new knobs strictly required; if promotion-on-first-sight wants a guard, it is a one-line policy, not a category toggle. `packet_max_episodic` becomes inert once the section is gone.
- **Docs** to sync in the same change: the memory hub ([memory/index.md](memory/index.md)) and the packaged operator docs (`config.example.yaml`, any role-prompt references to recent-episode memory).
