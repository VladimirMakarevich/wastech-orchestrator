# Roles — authoring a flow's role prompts

**You are an operator (or an agent helping one) writing the role prompts for a flow.** A _role prompt_ (`role_file`) is the text that becomes a node's instructions. This page explains what a role file is, the built-in evaluator roles and their behavior, and the output contract each node kind must honor — everything you need to write correct prompts. The `{name}` variables a prompt may use live in [prompt-variables.md](prompt-variables.md); the fields that reference these files (`role_file`, `role`, `output_artifact`, `output_schema`) are in [reference.md](reference.md).

## What a role file is

- A plain Markdown file, no front matter, no schema — just prompt text.
- **Owned by the flow.** Each flow keeps its prompts in a sibling folder named after its `task_type`: `.worc/flows/<task_type>/<name>.md`. A node's `role_file` is relative to `.worc/flows/` and must stay inside that folder (`role_file: my_flow/implement.md`); a `..`/absolute path fails validation.
- **One role file per node.** To change only _what a step says_, edit its `role_file` — you do not need a new flow.
- The one shared exception is the supervisor lens at `.worc/flows/roles/supervisor.md`, used by every flow that does not override it (see below).

Prompts are short and imperative. They reference artifacts by path variable (never inlined content) and wrap any optional variable in a `{?name}…{/name}` block. Keep them focused on the node's single job.

## The node output contract (what a prompt must make the agent return)

Every `agent` and `evaluator` node returns a **typed structured result**, not free text — and the core re-validates it, so a malformed result fails the node. You cannot loosen this from a flow. Which contract applies is selected automatically by the node's shape:

| Node shape | Must return | Notes |
| --- | --- | --- |
| plain `agent` | its final message | Also exposed to later nodes as `{<id>_path}`. |
| `agent` with `hitl:` | `content` + an optional question/approval object | Set the question only for a material ambiguity that repository evidence cannot resolve. |
| `agent` named by `decomposition.proposed_by` (planning) | `content` + optional `human_input` + `decompose` + `subtasks` | Emit subtasks only when decomposition is permitted. |
| `evaluator` | `{ findings: [ { severity, path, what, fix } ] }` | **Fail-closed**: a missing or malformed findings result routes the task to `manual_action_required` — never a silent `accept`. A prose-only "looks good" hard-stops the task. Return an **empty `findings` array** when clean, not prose. |

Example evaluator prompt (the built-in `review.md`): _"Report each finding with a severity, and mark anything that must change before merge as blocking. Weight the review: correctness and invariant violations block; quality and style observations are advisory unless they introduce real risk … No findings means the diff is clean — return an empty `findings` array, not prose."_ Which severities actually gate is the node's `gate_severity` (built-in default `high`; the packaged quality critics set `medium`), not the prompt's job — so a prompt should tell the model to grade honestly and say that the flow decides, never restate a severity threshold that can drift out of the YAML. See [reference.md](reference.md). A finding below the gate is not discarded: it is carried to the operator in the run summary and the pull-request body, so "file it anyway" is the right instruction.

Example planning prompt (`refinement.md`): _"Enrich the task into a complete spec… Return the typed structured result required by the output schema. Set `human_input` only when a material ambiguity cannot be resolved from repository evidence."_

## Evaluator `role` — the built-in vocabulary

An `evaluator` node's `role` field selects its lens. Four built-ins ship; any other string gets the **default evaluator behavior** (the same findings contract, no special handling):

| `role` | Lens | Typical use |
| --- | --- | --- |
| `review` | Code review of the diff against the task and plan. | The `implementation` flow's blocking review gate. |
| `critic` | Adversarial critique of a produced artifact (often multi-round, `resume_own_lineage`). | Research/quality critique. |
| `verifier` | Fact/finding verification (often `blocking: false`). | Research fact-checks, security-finding verification. |
| `test_quality` | Judges the _quality_ of tests, not just pass/fail (often non-blocking with a `max_rework_per_stage`). | Test-adequacy gates. |

A non-blocking evaluator (`blocking: false`) never parks the task: once its `max_rework_per_stage` budget is spent with a finding still open it accepts and the flow continues. That "moved on" is not silent — the orchestrator logs a console warning and (when `telegram.trace` is on) pushes a ⚠️ trace so you know that stage may still need follow-up. See the `max_rework_per_stage` row in [reference.md](reference.md).

`role` is an audit/behavior discriminator, not a permission — every evaluator is forced `read-only` and can never use `editing_lineage` (see [reference.md](reference.md)).

## Named output slots (`output_artifact`)

Besides the generic `{<id>_path}` channel, an `agent` node can fill **one** of four fixed slots with `output_artifact:`, landing its `content` in a well-known file that later nodes read by a stable variable (the node returns the content as its structured output; the orchestrator writes the file):

| `output_artifact` | Writes | Read downstream as |
| --- | --- | --- |
| `enriched_spec` | `task.enriched.md` | (audit only — no downstream variable) |
| `plan` | `plan.md` | `{plan_path}` |
| `summary` | `summary.md` | `{summary_body_path}` (normally the supervisor fills this, not a flow node) |
| `report` | `report.md` (into the flow's private report dir) | (private — the `private_control_workspace_report` shape, e.g. `security_audit`; read-only node, no agent write) |

The vocabulary is fixed to these four; a flow only chooses which node fills each, and one node fills at most one slot.

## Custom output schema (the one real foot-gun)

An `agent` node may set an inline `output_schema:` to return data of your own shape. If you do, **every object in the schema — top level and every nested object — must set `additionalProperties: false`.** Codex enforces `--output-schema` through OpenAI Structured Outputs and rejects a non-strict schema with a hard **400**, failing the node on every run. Claude tolerates a loose schema, but write it strict so the flow runs on both providers. Prefer the built-in contract unless you genuinely need a custom shape; keep a string `content` field if the node also fills a slot.

## The supervisor role (the constant layer)

The supervisor is not a node — it is a read-only layer above every flow that observes each step and writes the final summary (the PR body). Its prompts receive **only** `{task_id}` and `{repo}`/`{repo_path}` — no node/path variables. Its wording is:

- the global default `.worc/flows/roles/supervisor.md`, or
- a flow-local override via the flow's `supervisor:` block (`role_file` / `finalize_role_file` / `handoff_role_file`), resolved inside the flow's own folder.

Only the **wording** moves into files. The structured-output schemas (the memory delta, and the `follow_ups` array when `emit_follow_ups: true`) stay in the orchestrator — your prompt can change tone and emphasis but can never break what the orchestrator parses. `handoff_role_file` is used only by decompose flows (it writes the `{predecessor_context}` handoff brief between subtasks). Set `emit_follow_ups: true` on a code flow to have the finalize turn emit an evidence-gated technical-debt list; leave it off for research/prose flows.

**Set `finalize_role_file` whenever your deliverable is not a diff.** The built-in finalize lens summarizes "the actual committed change", which reads wrong for a document, a report, or a translation — and that summary becomes the pull-request body. Two things a good finalize lens says, both learned the hard way: the turn is a read-only observer, so it must describe what the _pipeline_ did rather than assert that it re-opened or spot-checked anything itself; and it must not state a count or a verdict it was not given ("all citations passed", "all gates passed"). It does not have to guess at the latter — the orchestrator appends every in-flow evaluator's recorded verdict and findings to that turn's prompt, so a gate that accepted **with** findings open cannot honestly be summarized as one that passed. The packaged `deep_research/summary.md` is the worked example.

## Writing and validating a role prompt

1. State the node's single job in the imperative; name the artifacts it should read by path variable.
2. For an evaluator, spell out the findings contract explicitly (severities, the `path`/`what`/`fix` fields, "empty array when clean").
3. Wrap every optional variable in `{?name}…{/name}` so a missing value never leaves a dangling fragment.
4. Run `worc validate-flow <name>` — its anti-drift lint **warns** about any `{name}` no node populates (a typo like `{plna_path}` would otherwise ship as literal text). It is a warning, not a failure, because a verbatim render is the safe fallback.
5. Set `prompt_audit: true` to inspect the exact rendered prompt per node under `logs/<task-id>/prompt-audit/`.

See [prompt-variables.md](prompt-variables.md) for the full `{name}` allowlist, which runner populates each, and the `{<node_id>_path}` chaining channel.
