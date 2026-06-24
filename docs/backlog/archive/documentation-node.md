# Documentation step for the `implementation` flow

Status: **shipped 2026-06-23.** Owner: Vladimir Makarevich

Built as designed. Two notes vs. the plan below: (1) the co-design reference graph touchpoint is moot — that tree now lives under `docs/backlog/archive/outdated/` and is no longer a synced source of truth (the [Functional Map](../functional/index.md) is). (2) Adding a second workspace-write node after `review` surfaced a latent gap in the core dangerous-diff guard: it re-classifies the whole uncommitted working-tree diff after every editing node, so `documentation` re-saw (and would re-prompt for) a deletion/dependency change `implementation` already got approved. Fixed by generalizing the guard to honor **any** prior in-task approval of the identical dangerous diff (same risk + exact path set), not just the planning pre-approval (`_already_approved_in_task` + `iter_task_interactions`); this also closes the same latent double-prompt for `fixing`. A new or expanded dangerous set still prompts — the guard never weakens.

## Goal

Add a new step to the packaged `implementation` flow ([../../src/wastech_orchestrator/core/flow/packaged/implementation.yaml](../../src/wastech_orchestrator/core/flow/packaged/implementation.yaml)): after the code change is accepted, an agent updates the **target project's** documentation so it reflects what was just implemented. The step is an ordinary agent node — it uses the same machinery as every other agent node in the flow (a `role_file` prompt, a durable editing lineage, the flow's permission ceiling, the core-owned commit/push/PR), so it adds no new node kind and no provider-specific logic. This bakes the repo's own discipline ("change behavior → update the affected docs in the same change") into the executable default flow for the projects the orchestrator works on.

## Design

A single new `agent` node, `documentation`, mirroring `fixing` (durable editing lineage, workspace-write), plus its role prompt and one rewired edge. No schema change, no new node kind, no config flag.

### New node

```yaml
- id: documentation
  kind: agent
  role_file: roles/documentation.md
  session_scope: editing_lineage
  lineage_affinity: implementation
  permission_profile: workspace-write
```

- `kind: agent` — same node kind as `implementation`/`fixing`; the engine dispatches it through the existing `agent` runner ([core/flow/nodes/agent.py](../../src/wastech_orchestrator/core/flow/nodes/agent.py)).
- `session_scope: editing_lineage` + `lineage_affinity: implementation` — resumes the implementation lineage (the same durable-session pattern `fixing` uses), so the documentation agent has the full context of what was built (including any fix-loop changes that advanced that lineage). No new lineage is introduced.
- `permission_profile: workspace-write` — it edits files in the working tree, exactly like `implementation`/`fixing`. The edits become part of the same diff the orchestrator commits and publishes; the node never commits/pushes itself (the "git only the orchestrator" invariant is untouched).
- No `output_artifact` — like `implementation`/`fixing`, its output is the working-tree change, not a persisted artifact slot.
- No `hitl`, no `when` — consistent with the editing nodes. Per-task disabling is already available for free via `nodes.documentation.enabled: false` (PRE.3, the bounded per-task exception); a disabled node is skipped and its forward edge is taken.

### New role prompt: `roles/documentation.md`

A short prompt in the same style as the other role files (advisory; the renderer only substitutes allowlisted path tokens — see `ALLOWED_PROMPT_VARS` in [core/prompts.py](../../src/wastech_orchestrator/core/prompts.py), e.g. `{plan_path}`, `{diff_path}`, `{task_path}`, `{skills_path}`). Draft intent:

> Update the project's documentation so it reflects the change just implemented. Read the plan and the diff (`{plan_path}`, `{diff_path}`) to see what changed, then bring the affected docs in line — READMEs, the `docs/` tree, configuration/usage references, changelog, public API docs, and any in-repo guide the change touches. Follow the project's existing documentation conventions and formatting; if the project ships a docs formatter or linter, run it. Make a focused, minimal change — do not edit code, tests, or behavior, and do not restructure docs beyond what this change requires. If nothing in the documentation needs updating, make no edits. If skill-reference paths are listed (`{skills_path}`), they are advisory read-only references.

The role file ships in the packaged flow tree under `packaged/roles/`, so `install` seeds it into `.worc/flows/roles/` as an editable active copy with no install-code change (it is a tree copy — see `_copy_packaged_flows` in [cli.py](../../src/wastech_orchestrator/cli.py)).

### Edge change (placement)

Insert `documentation` on the **accept exit of `review`, before `publish`**:

```yaml
# was: { from: review, to: publish, outcome: accept }
- { from: review, to: documentation, outcome: accept }
- { from: documentation, to: publish }
```

All other edges, budgets, and the two fix loops are unchanged. `documentation` is an unconditional agent with a single forward edge, so it emits the `done` pass-through outcome and takes that edge.

### Why after `review`, not between `implementation` and `testing`

This placement is dictated by the decomposition contract, not just preference. The decomposition `sub_flow` is `[implementation, testing, review, fixing]` — the per-subtask region. The engine ends a subtask when a **forward** edge leaves that region (see `_region` in [core/flow/engine.py](../../src/wastech_orchestrator/core/flow/engine.py)); rework/fail edges point back into the region. Documentation is a **whole-task** concern that must run **once**, after every subtask's code is accepted — not once per subtask. Keeping `documentation` out of `sub_flow` and placing it on `review --accept-->` means:

- For a plain task: `… → review --accept--> documentation → publish`.
- For a decomposed task: each subtask runs the region, the region's forward exit (`review --accept-->`) ends the subtask, and after the last subtask the post-region phase runs `documentation → publish` exactly once.

It also means the documentation agent sees the **final** accepted code (post fix-loops), so the docs can't drift from what shipped. This mirrors where the constant supervisor layer writes its whole-task summary (at whole-task close, before publish).

The rejected alternative — `implementation → documentation → testing` — would put docs in the same diff that `review` inspects (a plus), but it is incompatible with the existing `sub_flow` region (a forward edge would leave `implementation`'s region and break the per-subtask test/review loop), and adding `documentation` into `sub_flow` would run it redundantly per subtask and risk drift when later subtasks change the code.

Trade-off accepted: because `documentation` runs after `review`, the documentation edits are **not** themselves reviewed or run through `testing`. The role prompt asks the agent to follow the project's doc conventions and run its docs formatter/linter; a dedicated post-documentation `checks` node (e.g. a docs/markdown gate) is a possible future refinement, deliberately out of scope here to avoid adding nodes for v1.

## Touchpoints

- [src/wastech_orchestrator/core/flow/packaged/implementation.yaml](../../src/wastech_orchestrator/core/flow/packaged/implementation.yaml) — add the `documentation` node; rewire the `review --accept-->` edge; add `documentation → publish`; update the header comment that lists the pipeline; leave `decomposition.sub_flow` unchanged.
- **New** `src/wastech_orchestrator/core/flow/packaged/roles/documentation.md` — the role prompt (auto-seeded by `install`; no `cli.py` change).
- [docs/backlog/flows/co-design/implementation.yaml](flows/co-design/implementation.yaml) — keep the reference graph in sync with the canonical flow.
- [core/flow/validator.py](../../src/wastech_orchestrator/core/flow/validator.py) — **verify only**: routing-soundness should pass unchanged (new reachable node, valid `done` forward edge, `publish` still reachable). No validator code change expected; confirm with a test.
- Docs: update the `implementation`-flow description in the [Functional Map](functional/index.md) / system-flows so the documented pipeline matches (`… → review → documentation → publish`), per the docs-sync rule. Run `/sync-docs`.

No config schema bump, no `state.db` version bump, no new status — this is a pure graph-shape change plus one role file.

## Tests

- `test_implementation_flow_loads_with_documentation_node` — snapshot/validator accept the new node and edges.
- `test_documentation_routing_soundness` — `review --accept--> documentation --> publish`; `documentation` reachable; `publish` still reachable.
- `test_documentation_runs_once_after_decomposition` — for a decomposed task, `documentation` runs exactly once in the post-region phase, not per subtask.
- `test_documentation_disabled_per_task_skips` — `nodes.documentation.enabled: false` skips the node and its `done` outcome routes straight to `publish`.
- `test_documentation_resumes_implementation_lineage` — the node resumes the implementation editing lineage (like `fixing`).
- Integration (fake CLI, see the `fake-cli` skill): the documentation node edits a doc file in the working tree and the edit is included in the committed diff / opened PR.

## Out of scope (v1)

- A deterministic `when`-gate for "this change needs docs" — there is no clean derived fact; the agent no-ops when there is nothing to update, and an operator can disable the node per task.
- A post-documentation docs/markdown `checks` gate.
- Authoring/managing target-repo agent-instruction stubs (`AGENTS.md`/`CLAUDE.md`) — tracked separately in the README inventory and [follow_ups.md](follow_ups.md).

## Upgrade note

The change reaches **new** installs automatically (packaged flow + seeded role prompt). **Existing** installs keep their already-seeded `.worc/flows/implementation.yaml` (a plain `install` re-run adds the missing `roles/documentation.md` but skips the existing flow file); they pick up the new node via `install --reconfigure` (backs up and overwrites) or a manual edit. This is the same gap as the deferred flow-resync follow-up — no special migration is built here.

## Exit criteria

The packaged `implementation` flow runs `implementation → testing → review → documentation → publish`; an agent updates the target project's docs under the flow's workspace-write ceiling using the same prompt/lineage/commit machinery as the other nodes; it runs once per task, including decomposed tasks; the docs match the code; `ruff`/`mypy`/`pytest` green.
