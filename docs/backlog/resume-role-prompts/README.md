# Continuation prompts for resumable nodes (`resume_role_file`)

Status: **proposed** Date: 2026-09-02 Owner: Vladimir Makarevich

A node that resumes a provider session is handed its full role prompt again on every re-entry — the same rules, the same remit, the same output contract, into a conversation that already contains them. This item gives such a node an optional second role file, used only for the turns that continue an existing session, and makes the choice between the two a deterministic orchestrator decision rather than something the model is asked to infer about its own context.

The step-by-step execution, including the branch where the session is lost, is in [happy-path.md](happy-path.md).

## Problem

On every re-entry the runner rebuilds the whole prompt and sends it into the same session: `security_preamble` (1 199 chars) + the node's rendered `role_file` (`implementation/fixing.md` = 3 309 bytes) + the context footer ≈ 4.8 KB ≈ ~1 200 tokens of repeated instructions. The default flow's budgets are `test_fix: 15` and `review_fix: 15` ([`implementation.yaml`](../../../src/wastech_orchestrator/packaged/flows/implementation.yaml)), and every repetition then stays in the history that is re-sent on each later turn — the property [`supervisor_packet.py`](../../../src/wastech_orchestrator/core/supervisor_packet.py) already names ("the whole editing lineage is re-sent on each turn"). Token cost is the smaller half of the damage; the larger half is that three different situations are addressed with one start-of-work text:

| Situation | Where | What the agent receives today |
| --- | --- | --- |
| Loop re-entry | `fixing` round 2+ | the whole of `fixing.md` again |
| Turn grant renewed | `_invoke_with_turn_gate` ([`agent.py:370`](../../../src/wastech_orchestrator/core/flow/nodes/agent.py)) | the whole of `implementation.md` — "Implement the assigned task…" to an agent that merely ran out of turns mid-work |
| Human answer / denial delivered | [`agent.py:187`](../../../src/wastech_orchestrator/core/flow/nodes/agent.py), `_resume_guardrail` | the whole role prompt plus the answer file |

The second row is the sharpest: a start-of-work instruction, replayed as a reply inside a running conversation, reads as "start over".

## Evidence

The fact needed to fix this is already known to the Core and is currently delegated to the model's introspection. [`deep_research/critic.md`](../../../src/wastech_orchestrator/packaged/flows/deep_research/critic.md):

> This node runs on its own resumed session, so **if you can see your own earlier round(s) in this conversation**, treat this as a re-review … If you see no earlier round — the first pass, or a session that could not be resumed — review the report whole.

The flow YAML records why it is written that way (`deep_research.yaml:248-251`): "Round 1 has no prior lineage and always starts fresh — which is why the role prompt makes re-review conditional on actually seeing an earlier round rather than asserting continuity." The author could not know at authoring time whether the session resumed. The runner can: `session_id` is resolved by `_resolve_resume` ([`agent.py:920`](../../../src/wastech_orchestrator/core/flow/nodes/agent.py)) before `_build_request` ([`agent.py:750`](../../../src/wastech_orchestrator/core/flow/nodes/agent.py)) is called, and the evaluator runner has it in hand at the same point ([`evaluator.py:421`](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py)).

## Why prompt authoring alone cannot fix it

Rewriting `fixing.md` to be shorter is not an option, because the same file must also serve a **fresh** session: the lineage row can be absent, belong to another provider, or be dropped by the router mid-run, and on that path the node has no context beyond the artifacts and needs every rule. One text has to serve two mutually exclusive contexts, and only the orchestrator knows which one applies. That is what makes this a mechanism change rather than a wording change.

## Decision

Three parts, each small, and the third is what makes the first two safe.

1. An optional `resume_role_file:` on `agent` and `evaluator` nodes — a second, short role file used for continuation turns. Absent, behavior is byte-for-byte today's.
2. A deterministic continuation predicate in the runners: **the session is live _and_ this node has already spoken on it**.
3. Both texts ride the request; the variant is selected at the neutral seam from the same field that decides the `resume` argv.

A second file rather than a `{?resumed}` block inside one file: the repository already models prompt variants as separate files (`supervisor.role_file` / `finalize_role_file` / `handoff_role_file`), a continuation file reviews and diffs on its own, and the block form would require wrapping the entire main body in `{?fresh}…{/fresh}`, where one lost closing marker renders the whole prompt as literal text.

### 1. `resume_role_file` — a second role file on the node

Same contract as `role_file`: relative to `.worc/flows/`, resolved inside the flow directory, read through `read_role_file`, rendered by the same `render_prompt` with the same flow-derived variable set. It is not a new kind of thing — it is a role file that happens to be selected on a different turn.

Validation: reject `resume_role_file` on a node whose `session_scope` is `fresh_disposable`, where it could never fire. A field that silently does nothing reads as protection — the same reasoning the validator already applies to `git_evidence` on a workspace-write node ([`validator.py:442`](../../../src/wastech_orchestrator/core/flow/validator.py)).

### 2. The continuation predicate

`session_id is not None` is **not** the predicate. It conflates two different facts:

- **the session has history** — true for `fixing` round 1, which joins `implementation`'s lineage via `lineage_affinity`;
- **this node's own role prompt was already delivered on that session** — false for `fixing` round 1, whose rules ("Scope Discipline", "Fix The Finding, Then Its Class") nobody has yet stated.

Selecting on the first fact would silently strip the rules from the first run of every affinity node — `fixing` round 1, `documentation`, `polish`/`revise` in the content flows. The predicate is the second fact:

```
continuation = session_id is not None
    and ∃ node_runs row (task_id, node_id, subtask_order)
        with provider_used == <provider of the resumed lineage row>
```

No new table and no new column: `provider_used` is already on `NodeRunRow` ([`state_store.py:456`](../../../src/wastech_orchestrator/state_store.py)), and `_resume_lineage` ([`agent.py:942`](../../../src/wastech_orchestrator/core/flow/nodes/agent.py)) already returns a row only when its provider matches the resolved route, so matching on it also closes the fallback-churn skew (a prior run on the other provider does not count as having spoken on this session).

Two traps in the implementation:

- **The current run's own row already exists.** `_invoke` calls `record_node_run` _before_ `_resolve_resume`, so a naive existence check is true on the very first run and inverts the predicate. `provider_used` is `NULL` until `_record_completion`, which excludes it; assert that explicitly (and exclude the current `run_id`) rather than relying on it.
- **`ctx.run_state.completed_nodes` is the wrong source.** It looks like a free answer, but `hydrate_run_state` ([`recorder.py:241`](../../../src/wastech_orchestrator/core/flow/recorder.py)) rebuilds it from _every_ `node_runs` row of the task with no `subtask_order` filter, and the engine carries one `FlowRunState` across a decompose region — so in a decomposed task it reports another subtask's runs as this unit's.

A narrow store read (`has_prior_provider_run(task_id, node_id, subtask_order, provider)`) is preferred over `get_node_runs(task_id)` plus in-memory filtering: the whole-task scan grows with the run and is executed once per node run.

### 3. Selection at the neutral seam

Core cannot make the final choice, because the router drops the session **after** the prompt is built and without touching the prompt text: `SESSION_UNAVAILABLE` → same-provider fresh retry ([`router.py:424`](../../../src/wastech_orchestrator/routing/router.py)), the transient-retry degrade ([`router.py:642`](../../../src/wastech_orchestrator/routing/router.py)), and the cross-provider fallback, which clears `session_id` with `model`/`reasoning`/`extra_args` ([`router.py:700`](../../../src/wastech_orchestrator/routing/router.py)). A Core-only decision would send "continue where you left off" into a brand-new session — a silent quality loss on a run that reports success.

So both texts ride the request and the seam chooses:

```python
# providers/base.py
prompt: str  # the full text — unchanged meaning, still the default
continuation_prompt: str | None = None  # used ONLY while session_id is live


def build_effective_prompt(request: AgentRunRequest) -> str:
    body = (
        request.continuation_prompt
        if request.session_id and request.continuation_prompt
        else request.prompt
    )
    ...
```

This is text assembly, not CLI syntax, so it stays inside what [`build_effective_prompt`](../../../src/wastech_orchestrator/providers/base.py) already owns, and it makes the invariant airtight: **the prompt variant and the `resume` argv are derived from one field of one request object at one moment** (`codex … resume <id>` at `codex.py:532`, `claude --resume <id>` at `claude.py:1038`). All three router degradations are then correct for free, with no router change. `prompt` keeps its current meaning, so any consumer that ignores the new field degrades to "correct but verbose". The pattern is established: `resume_baseline_output_tokens` is likewise honored only while `session_id` is set ([`_adapter_base.py:199`](../../../src/wastech_orchestrator/providers/_adapter_base.py)).

## Invariants this must not touch

- **The frozen control plane.** `resume_role_file` is frozen at task start like any other role file — one line in `_add_role` ([`control_bundle.py:116`](../../../src/wastech_orchestrator/core/flow/control_bundle.py)), which already takes `str | None`. Its bytes therefore enter the manifest digest that continue/resume verifies, so a resumed session cannot be handed a continuation prompt the task was never validated against.
- **The renderer stays the fixed security core.** Both variants go through `render_prompt` with allowlisted **path** substitution only; no new variable, no inlined content.
- **The security preamble is unchanged in this item.** It is the stand-in for enforcement, and a long session is exactly where compliance drifts. A shortened continuation form is a separate decision for the security owner, not a side effect of this change.
- **The context footer stays.** The artifact paths differ every round (new checks, new review findings), so the footer is not repetition.
- **No node gains a publication mandate.** Nothing here touches permissions, argv, sandbox, or the state machine.

## Files to change

| File | Change |
| --- | --- |
| [`core/flow/schema.py`](../../../src/wastech_orchestrator/core/flow/schema.py) | `resume_role_file: str \| None = None` on `AgentNode` and `EvaluatorNode` |
| [`core/flow/snapshot.py`](../../../src/wastech_orchestrator/core/flow/snapshot.py) | the key in `_AGENT_FIELDS` / `_EVALUATOR_FIELDS` (unknown keys are fail-closed) + parse it in both node parsers |
| [`core/flow/validator.py`](../../../src/wastech_orchestrator/core/flow/validator.py) | `_check_path` for the new field on both node kinds; the new `fresh_disposable` rule; the prompt-var lint reads the second template too (`validator.py:653`) |
| [`core/flow/control_bundle.py`](../../../src/wastech_orchestrator/core/flow/control_bundle.py) | freeze it (`_add_role(getattr(node, "resume_role_file", None))`) |
| [`providers/base.py`](../../../src/wastech_orchestrator/providers/base.py) | `continuation_prompt` field + the selection in `build_effective_prompt` |
| [`core/flow/nodes/agent.py`](../../../src/wastech_orchestrator/core/flow/nodes/agent.py) | render the second template in `_build_request` and set `continuation_prompt` when the predicate holds |
| [`core/flow/nodes/evaluator.py`](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py) | the same, keyed on `resume_own_lineage` |
| [`state_store.py`](../../../src/wastech_orchestrator/state_store.py) | `has_prior_provider_run(task_id, node_id, subtask_order, provider)` |
| `packaged/flows/**` | the continuation prompts (below) and the `resume_role_file:` lines that reference them |
| [`packaged/guide/flows/roles.md`](../../../src/wastech_orchestrator/packaged/guide/flows/roles.md), [`reference.md`](../../../src/wastech_orchestrator/packaged/guide/flows/reference.md), [`skills/worc-flow-role/SKILL.md`](../../../src/wastech_orchestrator/packaged/guide/skills/worc-flow-role/SKILL.md) | the authoring contract: what a continuation prompt is for, and what it must still restate |

## Packaged prompts to author

The minimum set is the loop-re-entered nodes: `fixing` in `implementation`, `merge`, `blog_article`, `blog_article_revise`, `content_chapter`, `content_translate`, plus `critical_review` in `deep_research`. Seven files, 5–10 lines each, well inside the role-prompt size budget in [`wastech-mdlint.config.json`](../../../wastech-mdlint.config.json) (warn 1 800 tokens).

An eighth belongs in the same slice: `implementation/implementation.md`. A head-of-lineage author is never re-entered by a loop, but it _is_ re-invoked in-node — a max-turns continue and a dangerous-diff reconsider each call `_invoke` again on a resumed session — and that is the case the Problem section calls the sharpest. The other flows' head authors (`draft`, `revise`, `adapt_en`, `conflict_resolution`) are the same pattern and can follow once the shape is proven.

Each one drops the rules and the remit, and keeps three things: what changed since the last turn (the artifact paths), what is expected of this turn, and one line of output contract — the schema shape is enforced by the CLI flag on both providers, but its semantics ("grade honestly, the flow decides the gate") live only in the prompt.

`critic.md` loses its "if you can see your own earlier round(s)" hedge in the same change: the fresh text asserts a first pass, the continuation text asserts a re-review, and neither asks the model to guess.

## Acceptance

- A node with no `resume_role_file` produces a byte-identical prompt to today, fresh and resumed.
- `fixing` round 1 (joining `implementation`'s lineage) gets the **full** prompt; round 2+ gets the continuation prompt.
- A max-turns continue and a HITL answer round-trip both get the continuation prompt.
- A run whose session the router dropped — `session_unavailable`, transient degrade, cross-provider fallback — receives the **full** prompt on that attempt.
- `resume_role_file` on a `fresh_disposable` node is a validation error.
- The continuation file's bytes are in the control-bundle manifest digest.

## Tests

- Predicate matrix: `{session live, no session} × {node has spoken, has not} × {same provider, other provider}` → which variant is built.
- The first-run trap: a single `node_runs` row in `running` state for the current run must not satisfy the predicate.
- Decomposition: `fixing` in subtask 2 gets the full prompt even though subtask 1 already ran it, both in-process and after `hydrate_run_state`.
- Seam: `build_effective_prompt` with `session_id=None` and a `continuation_prompt` set returns the full prompt (this is the router-degradation guarantee, tested at the seam and again through the router's three degrade paths with the fake CLIs — see the `/fake-cli` skill).
- Validator: traversal (`../`) on the new field is fatal; the `fresh_disposable` rule fires; the prompt-var lint reports an unknown token inside a continuation template.
- Control bundle: the continuation file appears in the manifest and drift on it is a security violation like any other control input.

## Out of scope

- **A round number in the prompt** (`{fix_round}`) — the data is at hand (`ctx.run_state.loop_counters`), and "round 4 of 15, stop if the same failure recurs" pairs naturally with a continuation prompt, but it is a new prompt variable and belongs to its own slice.
- **A Core-owned continuation reason** — one line from a closed vocabulary (loop / turn grant / human answer) prepended at the seam, so that one file need not serve three situations with compromise wording. Defer until the three-way wording actually proves insufficient.
- **Per-attempt prompt audit.** `record_run_observability` records the node-level prompt, so a fallback attempt's recorded prompt already diverges from what was sent (`model`/`reasoning` diverge the same way). Pre-existing, but two prompt variants make it more visible; fix it separately.
- **Supervisor-generated prompt text.** Rejected on three grounds and not deferred: the control bundle freezes role prompts at task start and verifies the digest before reusing a session, so generated text cannot be frozen and the "an agent cannot rewrite its own rules mid-task" invariant collapses; `render_prompt` substitutes paths and never content, whereas the supervisor's own inputs include diffs and node final messages — agent-controlled text — which would make model output the instruction channel of a workspace-write node; and the finalize packet was deliberately made a pure function of `state.db` for reproducibility. The safe form of that idea already exists — the supervisor writes a bounded artifact and a frozen prompt references it by path, as `memory_path` / `predecessor_context` / `supervisor_packet_path` do — and its real addressee is a **fresh** re-entering node with no history, which is [P0.1 `self_prior_path`](../deep-research-run2-hardening.md#p01--give-a-re-entering-node-its-own-prior-output). The two ideas serve different node classes; merging them into one mechanism is the trap.

## Delivery to installed targets

`packaged/flows/` is copied as a tree, so a fresh `worc install` ships the new files with no enumeration to update. Existing installations resolve flows only from `.worc/flows/`, so they need the [`upgrade-flows`](../upgrade-flows.md) path — and until that lands, an installed flow YAML carrying `resume_role_file:` requires a `worc` that knows the key, since unknown node keys are fail-closed. Editing a packaged flow YAML also changes its `flow_fingerprint` (a SHA-256 over the raw `flow:` dict), which is the ordinary consequence of any flow edit for a task mid-run.

## Depends on

Nothing. It is independent of [P0.1](../deep-research-run2-hardening.md#p01--give-a-re-entering-node-its-own-prior-output) and complementary to it: P0.1 gives a _fresh_ re-entering node its own prior output, this item stops re-teaching a _resumed_ one what it already knows.
