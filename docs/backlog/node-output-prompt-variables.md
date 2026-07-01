# Generic node-output prompt variables (`{<node_id>_path}`)

Status: **proposed** (2026-07-01) Date: 2026-07-01 Owner: Vladimir Makarevich

Today a custom flow cannot hand one node's output to a later node under a name of its choosing: the prompt-variable allowlist (`ALLOWED_PROMPT_VARS`) and the output-slot table (`_OUTPUT_ARTIFACT_SLOTS = {enriched_spec, plan, summary}`) are both closed, Core-fixed sets, and a node id plays no part in them. This proposes a generic, zero-config channel: every agent node's output is persisted to `<artifacts>/<node_id>.out.md` and exposed downstream as the path variable `{<node_id>_path}`, so an author chains arbitrary nodes by referencing them by id — `{scan_path}`, `{analyze_path}` — without declaring anything or editing the allowlist. It relaxes the _static_ nature of the allowlist while keeping the hard invariant intact: the variable resolves to a Core-written artifact _path_, never inlined content.

## The problem

The only ways one node's result reaches another today are: the three fixed output slots (`plan` / `summary` / `enriched_spec`, each with a Core-fixed downstream consumer), the side-effect path variables (`{diff_path}`, `{review_path}`, `{checks_path}`), the durable provider session within an editing lineage, and the committed working tree. A custom flow with its own nodes — say `scan` → `analyze` → `build` — has no way to say "`build`, read what `analyze` produced" by a meaningful name. `output_artifact: analysis` is a fatal load error (not one of the three slots); `{analyze_path}` in a role file renders verbatim (not allowlisted) and is never populated. So authors are forced to misuse the single generic `plan` slot or fall back to reading the working tree, and multi-output custom flows are effectively unsupported. Node ids are unique and meaningful, yet carry no data-flow role.

## Constraints

- The renderer's hard invariant is unchanged: it substitutes only _path / metadata_ values, never task bodies, diffs, logs, env, or secrets. This proposal widens _which names_ are allowed (adding node-derived ones) but every value stays a path to a Core-written artifact.
- No secrets in artifacts (`security.md`): a node's raw output can echo secrets, so `<node_id>.out.md` must pass the same redaction the memory/handoff writes use before it is written, and it is local/uncommitted like the diff/review artifacts.
- Cross-platform paths: `Path.as_posix()` for the stored/displayed path string.
- The Core must not learn CLI syntax; this lives in the flow/orchestrator layer, never a provider.
- Node ids used as variables must be render-compatible: `_VAR_RE` currently matches only `[a-z_]+`, so either node ids intended for `{id_path}` are constrained to that charset or the regex is widened (see open questions).

## Alternatives considered

| Option | Why not chosen |
| --- | --- |
| Author-declared per-node variable names (`output_var: analysis`) | Works, but adds a declaration to every producing node and a second name to keep in sync with the node id. The node id is already a unique, meaningful name, so the extra field is redundant config. |
| One unified `{upstream_path}` | Simplest, but collapses all upstream outputs into one and cannot address a specific predecessor or a diamond (`build ← [analyze, scan]`). Lossy for any non-linear flow. |
| Keep the closed model (do nothing) | Leaves multi-output custom flows unsupported; forces misuse of the `plan` slot or the working tree. |
| Open the three slots to arbitrary names | Conflates two concerns: the slots have _special consumers_ (`summary` → PR body, `enriched_spec` → spec replacement), not just "node output." Widening them muddies those semantics. Keep them; add the generic channel beside them. |

## Decision

Add a generic, node-id-derived output channel, additive to the existing mechanisms:

- **Persist each agent node's output** to `<artifacts>/<node_id>.out.md` (content = `structured_output["content"]` or `final_message`, reusing `_slot_content`), redaction-scrubbed, local/uncommitted. Register it as an artifact so it is also an audit/debug record (symmetric with the per-node rendered-prompt audit).
- **Expose it as `{<node_id>_path}`.** The effective allowlist becomes `ALLOWED_PROMPT_VARS ∪ {"<id>_path" for each node id in the active flow}`. `render_prompt` stays the fixed security core — it substitutes only names in the allowlist it is _given_; the caller computes the effective set from the flow graph and only ever places path values in the dict. So the renderer never invents content, and the invariant holds unchanged.
- **Keep the three special slots** (`plan` / `summary` / `enriched_spec`) and the existing side-effect variables — they have dedicated consumers and stay as-is. The generic channel lives beside them, not instead of them.
- **Fan-in works for free:** a node consumes any number of upstream outputs by naming each — `D` after `A` / `B` / `C` references `{A_path}`, `{B_path}`, `{C_path}`; a diamond `build ← [analyze, scan]` references `{analyze_path}` and `{scan_path}`. (Consuming many inputs is supported; producing many named outputs from one node is not — split the node.) A referenced node's variable is empty until that node has run, so cross-branch references wrap in `{?name}...{/name}`.

Cost of the choice: the allowlist stops being a single static frozenset and becomes flow-derived, so the renderer signature and the validate-time lint must accept a computed valid-set. We accept that in exchange for zero per-node config and self-documenting, collision-free names.

**Resolved parameters (refinement 2026-07-01):**

- The output is persisted for **every agent node, always** — uniform, and it doubles as a per-node audit record.
- **Only agent nodes** get `{<node_id>_path}`. Evaluator / checks / human nodes keep their dedicated variables (`review_path`, `checks_path`); the generic channel does not extend to them.
- **`_VAR_RE` is widened** (and `{?name}` block matching with it) to accept `-` and digits, so ids like `static-scan` / `pass2` resolve — kept to an all-lowercase token shape so camelCase braces in code/JSON still pass through untouched.
- **Node ids are validated at load** not to collide with the reserved core-variable prefixes (`task`, `plan`, `diff`, `checks`, `review`, `repo`, `skills`, `memory`, `stage`, `subtask*`); a colliding id is a fatal flow error.
- **One node = one output.** A node exposes exactly one generic `{<node_id>_path}`; to publish several distinct results, split into several nodes (each with its own path). Arbitrary multiple named outputs per node is intentionally unsupported (that is the rejected author-declared-names model). A node may additionally fill **one** of the three special slots via `output_artifact` — that slot then _is_ its output channel (`{plan_path}` etc.) and no duplicate generic `.out.md` is written for it.

## Open questions

The parameters raised during refinement (2026-07-01) are resolved and folded into the Decision above: `_VAR_RE` is widened; output is written for every agent node, always; only agent nodes get `{<node_id>_path}`; node ids are validated against the reserved prefixes; and one node = one generic output. One coordination note remains:

- **Sequencing with the Cluster A lint.** The prompt-var lint in [prompt-and-supervisor-authoring-contract.md](prompt-and-supervisor-authoring-contract.md) must check against the flow-derived valid-set, not the static frozenset. Build that lint flow-aware from the start (it already runs at validate time with the graph in hand), so this change extends the valid-set without reworking the lint.

## Implementation notes

- `src/wastech_orchestrator/core/prompts.py` — parameterize `render_prompt` with the effective allowed-name set (default `ALLOWED_PROMPT_VARS` for back-compat); widen `_VAR_RE` and the `_BLOCK_RE` name pattern consistently to accept `-` / `0-9` (all-lowercase token shape preserved).
- `src/wastech_orchestrator/core/flow/nodes/agent.py` — after a node runs, persist `<node_id>.out.md` (redaction-scrubbed); in `_prompt_variables`, add `{<id>_path}` for the flow's node ids and pass the effective allowlist to the renderer.
- `src/wastech_orchestrator/core/flow/postprocess.py` — the node-output writer can live beside `apply_output_artifact` / `_slot_content` (reuse the content-extraction logic).
- `src/wastech_orchestrator/core/flow/snapshot.py` — validate node ids against the reserved core-variable prefixes; keep `_OUTPUT_ARTIFACT_SLOTS` as-is.
- `src/wastech_orchestrator/core/flow/validator.py` — build the prompt-var lint flow-aware (valid-set = core ∪ node-derived), so it also flags a `{X_path}` that names no node.
- Docs: the delivered prompt-variable reference (from the paired ADR) documents the `{<node_id>_path}` convention **with clear allowed / not-allowed examples** (allowed: reference any node by its id, split a step into several nodes for several outputs; not allowed: a node id colliding with a reserved prefix, `{X_path}` naming no node, expecting multiple named outputs from one node); the custom-flow guide shows a worked multi-node example.
- Related: orthogonal to [subtask-context-handoff.md](subtask-context-handoff.md) (cross-subtask `{predecessor_context}`, a different axis) and paired with [prompt-and-supervisor-authoring-contract.md](prompt-and-supervisor-authoring-contract.md) (its lint + variable reference must be flow-aware).
