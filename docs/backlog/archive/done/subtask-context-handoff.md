# Sub-task context handoff (intra-task decompose)

Status: **implemented** (2026-07-02) Date: 2026-07-01 Owner: Vladimir Makarevich

Implemented on branch `feat/prompt-supervisor-handoff` (Block 4, building on Cluster B): the two-layer handoff — a deterministic factual floor (`Orchestrator._assemble_predecessor_context` + `GitManager.files_in_commit`) plus the interpretive `Supervisor.handoff` on the warm session (flow-local `handoff_role_file`) — is assembled per successor from its `depends_on` predecessors, redaction-scrubbed to `logs/<task-id>/subtasks/NN-slug.handoff.md`, and injected as `{?predecessor_context}` into the region's `implementation` node. The "soft cap on deep/wide subtask graphs" open question is deferred (recorded in [follow_ups.md](../../follow_ups.md)). See [autonomous-run-open-questions.md](autonomous-run-open-questions.md) for the implementation-time decisions.

When one task is decomposed into ordered subtasks that run back-to-back, each successor subtask starts nearly blind: its implementation agent receives only its own immutable spec, not what the predecessor subtasks built, which interfaces they locked, or what they deliberately left unfinished. This proposes a two-layer "handoff brief" — a deterministic factual floor plus an interpretive supervisor brief — propagated forward along the subtask `depends_on` graph and injected into the region edit nodes, so successors stop re-exploring, stop duplicating, and stop breaking the contracts earlier subtasks established. This refocuses an earlier draft that targeted cross-task `depends_on` chains (see Alternatives) onto the place where the blindness is most acute and most frequent.

## The problem

`decompose` splits one task into ordered subtasks. Planning runs once (in the `pre` region); the sub_flow region then runs once per subtask via `_fan_out_subtasks`, committing each subtask onto the same branch in sequence. The supervisor's `finalize` — and therefore the whole-task summary and the memory delta — runs once at whole-task close, never between subtasks. Meanwhile the implementation role prompt injects only `{?memory_path}` and `{?subtask_spec_path}`: it does not see the shared plan, the diff, or any predecessor output. So subtask N+1's agent sees its own spec and the predecessor's committed code in the working tree, but has no pointer to what changed or why. Three failure modes follow, and unlike the cross-task case they recur on every multi-subtask run: (1) the agent re-explores what the predecessor just built, wasting tokens and wall-clock; (2) it breaks an interface or contract a predecessor established, forcing rework; (3) it duplicates or contradicts work a predecessor deliberately deferred. Git shows what changed, not what is load-bearing or what was left open — the two signals that most often decide whether the successor succeeds.

## Constraints

- No secrets in any artifact — the interpretive brief is LLM-authored, so it must pass through the same redaction as memory writes before it is written (`security.md` C1).
- Cross-platform paths: `pathlib` + `Path.as_posix()` for any stored/compared/displayed path string (`coding-style.md`).
- The core must not know CLI syntax; this mechanism lives in the orchestrator and flow layer, never inside a provider.
- Advisory only — the handoff informs the successor agent; it never constrains the Core state machine, routing, or the provider (mirrors the supervisor's advisory contract).
- Best-effort — an absent or failed brief must never fail the subtask; it degrades to less context (mirrors `PacketBuilder` AC-R4 and `finalize` best-effort).
- Distinct from long-term memory (`.worc/memory/`): a handoff brief is transient and scoped to a single task's subtask sequence. It must never be written to the memory tiers. Long-term memory accumulates durable, repo-scoped lessons across independent runs; the handoff propagates one task's in-flight state across its own subtasks. Different retention horizon.

## Alternatives considered

| Option | Why rejected |
| --- | --- |
| Cross-task `depends_on` propagation (the earlier draft's target) | Lower value for V1. A dependent task only becomes eligible after its dependency is **merged**, so the predecessor's code is already on base and its `summary.md` already exists — the successor is far from blind. The acute, per-run blindness is between subtasks that run back-to-back with no `finalize` between them. Cross-task can reuse the same `{predecessor_context}` seam later. |
| Deterministic assembly only (no supervisor) | Insufficient as the sole mechanism. Git gives _what_ changed, not _what is load-bearing_ or _what was left for you_ — the interpretive signals that prevent the costliest failures (contract breakage, lost intent). Only the supervisor, which observed the subtask run, has them. Kept as the always-on factual floor, not dropped. |
| Supervisor brief only (no deterministic floor) | Fragile as the sole mechanism. When the supervisor is disabled or its turn fails, the successor falls back to fully blind — today's behavior. And an LLM brief can misname the locked surface. Ground-truth facts (changed files, commit) must always be present and anchor the interpretation. |
| Full plan + git diff injected into the prompt | Contextually complete but too noisy. The next agent drowns in implementation detail rather than seeing decisions; high token cost, low signal. A focused 3-section brief is the point. |
| Piggyback the handoff on the supervisor's per-step observation turn | Saves a trivial cost (the session is already warm) but couples the per-node observation schema to subtask-boundary state. A dedicated boundary call keeps the concern self-contained. |
| Do nothing (agent reads the committed tree) | Leaves the most frequent blindness — subtask → subtask — entirely unaddressed. |

## Decision

A two-layer handoff brief, produced at each subtask boundary and injected into the successor's region edit nodes. We accept a small, focused mechanism over both a facts-only assembly and a supervisor-only brief because the two layers cover different failure modes and back each other up: the deterministic floor is ground truth and always present, the supervisor brief carries the intent the floor cannot see, and neither alone is both robust and rich.

- **Deterministic factual floor** (always, zero LLM): the orchestrator assembles, from artifacts that already exist, the predecessor subtask's changed files, commit message, and acceptance criteria (plus its spec pointer). Ground truth; it also grounds the agent so it trusts facts over any interpretive claim. When the supervisor is disabled the successor runs on the floor alone — an accepted degradation, not an error.
- **Interpretive supervisor brief** (when the supervisor is enabled): a dedicated `handoff(subtask_order)` call with its own structured schema, on the supervisor's warm durable session at the subtask boundary, emits a three-section brief — **New surface area** (what the predecessor built and the successor should use), **Locked decisions** (contracts that must not be revisited, with brief rationale), **Open edges** (what was deferred or left to the successor, and what must not be touched). Cheap because the session already observed the subtask: it resumes, and the incremental prompt is small.
- **Predecessor selection follows `subtask.depends_on`** (integer orders), not "all prior subtasks." The data already exists and is validated acyclic; honoring it is more precise, handles the intra-task diamond (subtask 3 ← [1, 2]) out of the box, and bounds token cost on wide graphs.
- **Injection** via a `{?predecessor_context}` conditional block (mirroring `{?memory_path}`) into the region's `implementation` node. Template-driven opt-in, no flow YAML change. Not `planning`, which runs once in the `pre` region.
- **Storage** at `logs/<task-id>/subtasks/NN-slug.handoff.md` — local, uncommitted, redaction-scrubbed. It is archived and deleted with the task and is never written to the memory tiers.

## Scope

**V1 covers:**

- Intra-task `decompose` subtask chains — both agent-proposed and operator-authored (`subtasks:`) — linear and intra-task DAGs (diamonds) resolved through the subtask `depends_on` orders.
- The two-layer brief: the deterministic floor always, the supervisor brief when the supervisor is enabled.

**Explicitly out of scope for V1:**

- Cross-task `depends_on` propagation between separate tasks — a separate design; the predecessor is already merged and summarised, and it reuses the same `{predecessor_context}` seam when picked up.
- Intra-task **node** handoff (`planning` → `implementation` → `review` within a single subtask's region) — different mechanism, different horizon.
- Persistence across orchestrator instances or providers — everything here is one instance, one branch, one run.
- Regeneration semantics beyond the existing skip-committed recovery when a subtask is re-run.

## Open questions

- **Soft cap on deep/wide subtask graphs**: if a subtask depends on many predecessors, or the chain is long, injecting every brief could grow costly. Needs a policy (last N, token-budget cap, or summarise-the-summaries). Deferred — left to implementation.

## Implementation notes

Sequencing: this builds on the flow-local supervisor-prompt contract in [prompt-and-supervisor-authoring-contract.md](prompt-and-supervisor-authoring-contract.md) — land it after that ADR's Cluster B so `handoff()` inherits the refactored `_base_prompt` and declares its brief prompt as a flow-local `handoff_role_file` (a third supervisor prompt: wording in a file, schema in code); `predecessor_context` is then covered by that ADR's allowlist lint.

Key seams to touch:

- **Boundary producer** — `_fan_out_subtasks` in `src/wastech_orchestrator/core/orchestrator.py`: after `_commit_subtask` (so `commit_sha` is available) and before `reset_for_next_subtask`, assemble the deterministic floor and, when enabled, call the supervisor handoff; write `logs/<task-id>/subtasks/NN-slug.handoff.md`.
- **Supervisor brief** — a new `handoff(subtask_order)` method in `src/wastech_orchestrator/core/supervisor.py` mirroring `finalize` / `_finalize_turn`, with its own structured schema in the spirit of `_FINALIZE_SCHEMA`: resume the durable `__supervisor__` lineage session and emit the three-section brief; best-effort; run the output through the memory-subsystem redaction before writing.
- **Prompt variable** — add `predecessor_context` to `ALLOWED_PROMPT_VARS` in `src/wastech_orchestrator/core/prompts.py`; it reuses the existing `{?name}…{/name}` conditional-block mechanism as-is.
- **Builder + wiring** — a `_predecessor_context()` resolver in `src/wastech_orchestrator/core/flow/nodes/agent.py` alongside `_memory_path()`: return the assembled path only when a decompose region is active, the current subtask has at least one `depends_on` predecessor, and the node template references `{predecessor_context}`; wire it in `_prompt_variables()`.
- **Role prompts** — add a `{?predecessor_context}…{/predecessor_context}` block to `implementation.md` under `packaged/flows/implementation/` (its flow-owned folder since the flow-owned-prompt-directories change).
- **Storage helper** — extend the subtask-artifact helpers (near `subtask_spec_path`) with a `handoff` path. No config change, no new DB table — the `subtasks` table already tracks order/slug/commit_sha.
