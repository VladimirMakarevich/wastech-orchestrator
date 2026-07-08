# Multiple named editing lineages in one flow

Status: **accepted (implemented 2026-07-08)** Date: 2026-07-08 Owner: Vladimir Makarevich

Implemented on branch `feat/multiple-editing-lineages`: `editing_lineage` is keyed `(task_id, subtask_order, lineage_key)` with `lineage_key = node.lineage_affinity or node.id` (state.db v15); `_resume_session_id`/`_persist_session` resolve by that key; validator rule 7 forbids affinity chains (a target must be a lineage owner). Open questions resolved as proposed: chains forbidden (one hop), the affinity-less semantic shift is a docs callout, and per-lineage provider consistency rides the existing per-row `provider != primary → fresh` guard (no validator change beyond keying).

Let a single flow carry more than one durable editing session per execution unit, each shared across a distinct subset of `editing_lineage` agent nodes, so an operator can run two (or more) isolated editing tracks in the same task — for example a code track and a separate spec/contract track — without the tracks leaking session context into each other. The lineage key is derived from the graph, not a new config field: an `editing_lineage` node with no `lineage_affinity` owns a lineage keyed by its own id; a node with `lineage_affinity: X` joins the lineage owned by `X`. This is a small, backward-compatible change that finally makes `lineage_affinity` route sessions at runtime (today it is only validated, never honored).

## The problem

Today there is exactly one durable editing session per execution unit. The `editing_lineage` table is keyed `(task_id, subtask_order)` ([state_store.py](../../../../src/wastech_orchestrator/state_store.py) — `CREATE TABLE editing_lineage`), and `_resume_session_id`/`_persist_session` in [core/flow/nodes/agent.py](../../../../src/wastech_orchestrator/core/flow/nodes/agent.py) load and upsert that single row, ignoring the node id and `lineage_affinity` entirely. So every `editing_lineage` node on the unit resumes the same session. `lineage_affinity` is currently pure declaration: the validator ([core/flow/validator.py](../../../../src/wastech_orchestrator/core/flow/validator.py), rule 7) checks the target exists, is an `editing_lineage` agent, and does not conflict on provider, but the target id is never used at runtime.

An operator who wants two independent editing tracks in one flow (each track a chain of edit → fix that must keep its own continuous context and must not inherit the other track's session) has no clean option: putting both on `editing_lineage` collapses them into one shared session (the second track inherits the first's context), and dropping the second track to `fresh_disposable` throws away continuity (every visit starts cold and re-reads everything, defeating durable sessions).

## Constraints

- **Core does not know CLI syntax** — this change stays entirely in the flow engine, state store, and validator; providers are untouched.
- **No secrets outside `state.db`** — the raw session id already lives only in `editing_lineage`/`node_lineage` and is redacted everywhere else ([providers/redaction.py](../../../../src/wastech_orchestrator/providers/redaction.py)); adding a lineage-key column keeps that property.
- **Cross-provider sessions cannot be resumed** — a session created on Codex cannot resume on Claude. The existing runtime guard (`row.provider != route.primary → fresh`) and validator rule 7 (affinity/target provider must agree) must continue to hold, now independently per lineage.
- **Greenfield, no migration machinery** — the orchestrator is not deployed anywhere, so the `state.db` schema change is a plain `CREATE TABLE`/version bump, not a data migration (see the greenfield-MVP note in the backlog).
- **One diff per unit is unchanged** — both tracks edit the same working tree and their edits join the same committed diff; lineage is only about LLM session context, never about filesystem isolation. The commit-candidate mutation guard on `checks` is unaffected.

## Alternatives considered

| Option | Why rejected |
| --- | --- |
| Do nothing (single shared lineage) | The motivating scenario — two isolated editing tracks in one flow — is impossible; the second track either inherits the first's context or loses continuity. |
| Force the second track onto `fresh_disposable` | Loses durable continuity: every re-entry into the track starts a cold session and re-reads context, which is exactly what durable sessions exist to avoid. |
| Use `resume_own_lineage` per node (the `node_lineage` table, keyed by `node_id`) | That is a node's **private** session across its own rework rounds, not a session **shared** across a group of nodes, so `fix` could not continue the track that `edit` established. |
| Split the second track into a separate subtask or a separate flow | Heavyweight: changes decomposition semantics, and the tracks become separate execution units / commits rather than two tracks in one unit. |
| New explicit `lineage: <name>` node field (variant B) | Adds a second, overlapping way to express what `lineage_affinity` already expresses, forcing a precedence rule between the two; more config surface for no extra capability the node-id key does not already give. |

## Decision

Adopt the graph-derived lineage key (variant A): the lineage key is `node.lineage_affinity or node.id`. An `editing_lineage` node with no affinity owns a lineage named after itself; a node with `lineage_affinity: X` joins lineage `X`. The `editing_lineage` store becomes keyed `(task_id, subtask_order, lineage_key)`, and `_resume_session_id`/`_persist_session` resolve by that key. We do this because it delivers multiple isolated tracks with **zero new config surface** and full backward compatibility — every packaged flow has at most one affinity-less `editing_lineage` node (`implementation` → `implementation`; `merge.yaml` → `conflict_resolution`), so the rule reproduces today's single-lineage behavior 1:1 — while finally making `lineage_affinity` mean something at runtime. The cost is that the free-form naming of variant B is not available (a track is always named after the node that starts it), which is an acceptable limitation given the node-id key covers the concrete scenario.

## Open questions

- **Affinity chains.** Under the new key, an affinity target that itself declares `lineage_affinity` (A → B → C) is ambiguous. Proposed resolution: the validator requires an `lineage_affinity` target to be a lineage **owner** (i.e. itself affinity-less) — one level only, no chains. Confirm this is the rule we want rather than transitively resolving to the chain root.
- **Semantic shift for affinity-less nodes.** Today two `editing_lineage` nodes that both omit `lineage_affinity` share the one session; under the new rule they become two separate lineages. No packaged flow hits this, and the orchestrator is greenfield, but the behavior change should be called out in the docs so operator flows are read with the new rule in mind.
- **Per-lineage provider consistency.** The existing `row.provider != route.primary → fresh` guard already degrades gracefully per row, so a joiner that resolves to a different provider than its lineage owner just starts fresh for that node. Confirm this per-lineage behavior is the intended contract (no cross-lineage interference) and that validator rule 7 needs no change beyond keying by lineage.

## Implementation notes

- [state_store.py](../../../../src/wastech_orchestrator/state_store.py): add a `lineage_key` column to the `editing_lineage` table and its primary key `(task_id, subtask_order, lineage_key)`; thread the key through `get_editing_lineage`/`upsert_editing_lineage`; `clear_editing_lineage` stays keyed by `task_id` (clears all lineages for the task); bump the `state.db` schema version.
- [core/flow/nodes/agent.py](../../../../src/wastech_orchestrator/core/flow/nodes/agent.py): in `_resume_session_id` and `_persist_session` compute `lineage_key = node.lineage_affinity or node.id` and pass it to the store lookups/upsert.
- [core/flow/validator.py](../../../../src/wastech_orchestrator/core/flow/validator.py): keep rule 7's existence/kind/provider checks; add the "affinity target must be a lineage owner (no affinity of its own)" rule to forbid chains.
- [core/flow/schema.py](../../../../src/wastech_orchestrator/core/flow/schema.py) / [core/flow/snapshot.py](../../../../src/wastech_orchestrator/core/flow/snapshot.py): no new field — the existing `lineage_affinity` is reused as the key source.
- Docs to sync in the same change: [flow-authoring.md](../../../flow-authoring.md), the functional blocks [B29](../../../functional/blocks/B29-flow-definition-and-validation.md)/[B30](../../../functional/blocks/B30-flow-node-runners.md), [worc_architecture.md](../../../worc_architecture.md), and [system-flows.md](../../../functional/system-flows.md).
