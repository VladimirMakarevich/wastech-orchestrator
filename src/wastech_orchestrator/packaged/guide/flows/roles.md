# Roles — authoring a flow's role prompts

**You are an operator (or an agent helping one) writing the role prompts for a flow.** A _role prompt_ (`role_file`) is the text that becomes a node's instructions. This page explains what a role file is, the built-in evaluator roles and their behavior, and the output contract each node kind must honor — everything you need to write correct prompts. The `{name}` variables a prompt may use live in [prompt-variables.md](prompt-variables.md); the fields that reference these files (`role_file`, `role`, `output_artifact`, `output_schema`) are in [reference.md](reference.md).

## What a role file is

- A plain Markdown file, no front matter, no schema — just prompt text.
- **Owned by the flow.** Each flow keeps its prompts in a sibling folder named after its `task_type`: `.worc/flows/<task_type>/<name>.md`. A node's `role_file` is relative to `.worc/flows/` and must stay inside that folder (`role_file: my_flow/implement.md`); a `..`/absolute path fails validation.
- **One role file per node.** To change only _what a step says_, edit its `role_file` — you do not need a new flow.
- The one shared exception is the supervisor lens at `.worc/flows/roles/supervisor.md`, used by every flow that does not override it (see below).

Prompts are short and imperative. They reference artifacts by path variable (never inlined content) and wrap any optional variable in a `{?name}…{/name}` block. Keep them focused on the node's single job.

## The node output contract (what a prompt must make the agent return)

An `evaluator` node always returns a **typed structured result**, not free text, and so does an `agent` node whose shape selects one (`hitl:`, or the node named by `decomposition.proposed_by`). The core re-validates that result, so a malformed one fails the node, and you cannot loosen it from a flow. A plain `agent` node has no built-in schema — its output is its final message. Which contract applies is selected automatically by the node's shape:

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

An audit lens that treats delivery history as evidence — "did the change that closed this milestone actually touch what it claims" — can be given the read-only git verbs with `git_evidence: true`, provided the operator turned `security.allow_git_evidence` on. It stays read-only: the verbs only report, the sandbox denies every write, and publishing is still the orchestrator's. In advanced mode (`security.strict_isolation: false`) the key is inert — the node already has an unscoped shell. See [Read-only git evidence](reference.md#read-only-git-evidence).

## Named output slots (`output_artifact`)

Besides the generic `{<id>_path}` channel, an `agent` node can fill **one** of four fixed slots with `output_artifact:`, landing its `content` in a well-known file the orchestrator writes (the node returns the content as its structured output; it does not write files itself). Only the `plan` slot is readable from a later prompt as a variable:

| `output_artifact` | Writes | Read downstream as |
| --- | --- | --- |
| `enriched_spec` | `task.enriched.md` | (audit only — no downstream variable) |
| `plan` | `plan.md` | `{plan_path}` |
| `summary` | `summary.md` | (no prompt variable — it feeds the `publish` node's pull-request body; normally the supervisor fills this, not a flow node) |
| `report` | `report.md` (into the flow's private report dir) | (private — the `private_control_workspace_report` shape, e.g. `security_audit`; read-only node, no agent write) |

The vocabulary is fixed to these four; a flow only chooses which node fills each, and one node fills at most one slot.

## When the node's product is a file it writes (`output_file`)

A node that writes a document — a report, a translated chapter, a generated spec — has two outputs: the file, and whatever it says about the file when it finishes. By default the `{<id>_path}` channel carries the **message**, which is the smaller of the two and usually a summary of the other. Name the file with `output_file:` and the channel carries the file instead:

```yaml
- id: synthesis
  kind: agent
  role_file: my_flow/synthesis.md
  permission_profile: workspace-write
  output_file: report.md # {synthesis_path} → this file, not the closing message
```

One portable filename (no `/`, no `..`), resolved inside the flow's `output_policy` report directory — the only place the node may write anyway — or the repository root for a policy without one. It is mutually exclusive with `output_artifact` (a slot node's channel is its slot). Two things to write into the prompt when you use it: the file has to **stand alone**, since no closing message travels with it, and the filename in the prompt has to match the one in the flow. If the file never appears the channel falls back to the message and the run logs a warning — never a silent empty handoff.

## Custom output schema (the one real foot-gun)

An `agent` node may set an inline `output_schema:` to return data of your own shape. If you do, **every object in the schema — top level and every nested object — must set `additionalProperties: false`.** Codex enforces `--output-schema` through OpenAI Structured Outputs and rejects a non-strict schema with a hard **400**, failing the node on every run. Claude tolerates a loose schema, but write it strict so the flow runs on both providers. Prefer the built-in contract unless you genuinely need a custom shape; keep a string `content` field if the node also fills a slot.

## The supervisor role (the constant layer)

The supervisor is not a node — it is a read-only layer above every flow that observes completed nodes and writes the final summary (the PR body). Its prompts receive **only** `{task_id}` and `{repo}`/`{repo_path}` — no node/path variables.

Two of its jobs are easy to confuse, and the split is worth holding on to when you write its prompts. **The facts of a run are recorded without it**: every executed node, its outcome, which provider it landed on, what it reported, which checks passed — all of that is written deterministically as each node finishes, with no LLM involved, and it is what the finalize turn is handed. **The supervisor's own contribution is interpretation**: an optional note per deviating step, and the final synthesis. So a lens that fails to run costs you a note, never a fact — and no prompt can make the layer the source of what happened. Its wording is:

- the global default `.worc/flows/roles/supervisor.md`, or
- a flow-local override via the flow's `supervisor:` block (`role_file` / `finalize_role_file` / `handoff_role_file`), resolved inside the flow's own folder.

Only the **wording** moves into files. The structured-output schemas (the memory delta, and the `follow_ups` array when `emit_follow_ups: true`) stay in the orchestrator — your prompt can change tone and emphasis but can never break what the orchestrator parses. `handoff_role_file` is used only by decompose flows (it writes the `{predecessor_context}` handoff brief between subtasks). Set `emit_follow_ups: true` on a code flow to have the finalize turn emit an evidence-gated technical-debt list; leave it off for research/prose flows.

**Which nodes it observes, and what the finalize turn actually reads.** Three things about its cadence are worth knowing, because all of them change what your prompts should say:

- **How often it observes is a setting, and your flow can own it.** `supervisor.observe.mode` is `events` by default (only a deviation: a rework, a failed step, a provider fallback), and a flow may narrow it in its own `supervisor:` block to `none`, or widen it to `selected` / `all` up to whatever the operator's config allows. So an observe lens must not promise to comment on every step of the flow — under the default it sees only the steps that went wrong, which is what it should be written for.
- **At `mode: none` your observe lens is never loaded at all.** Nothing observes, so `role_file` (flow-local or global) is simply unused, and `finalize_role_file` is the only wording in force. If a content flow's whole supervisor voice lives in its observe lens, moving to `none` silently drops that voice — put anything you actually need into the finalize lens. The packaged content flows (`blog_article`, `blog_article_revise`, `content_chapter`, `content_translate`) ship `none`; `implementation` ships `events`.
- Regardless of mode, `tool` and `checks` nodes are **never** observed (their result is already a recorded fact — the node's outcome and, for checks, the per-command pass/fail — so an LLM note about it bought nothing and cost a full call per run), and neither is the terminal `publish` node.
- The finalize turn runs on a **fresh** session, not as a continuation of those observations, and it happens under **every** mode including `none`. It is seeded by a small deterministic **packet** — a JSON file reachable as `packet` in its context footer, published at `.worc-io/<task-id>/supervisor/packet.json` — holding the changed paths and diff stat (with the full diff inlined only while it is small, otherwise a pointer to `current.diff`), every executed node with its outcome and what it reported, which check commands passed/failed/were skipped, a pointer to the latest evaluator findings, and whatever observations were recorded (none, at `mode: none`). The packet is built from the recorded node runs and each node's own output file, never from the observations, which is why turning them off costs you nothing in the summary. A finalize lens should therefore tell the turn to ground itself in that packet and to open the artifacts it points at — never to write from memory of the run, which it does not have — and should treat the observation section as possibly empty rather than promising to relay it.

**What the layer cost you is written next to the summary.** Every run leaves a `supervisor_usage` block in `.worc/logs/<task-id>/summary.json` (local only — it is never committed and never reaches the pull-request body): calls, input, cached input, output, cost and provider wall time, given both as a total and split by which job spent it — `observe`, `finalize`, `handoff`, `skill`. That split is the number to look at before changing a cadence: it tells you whether the per-step notes actually cost more than the one turn that writes the summary on _your_ flow, instead of you having to guess. `cost` reads `null` when the provider does not report one (Codex), and a `calls_without_usage` count appears if some call reported no figures at all, so a total is never quietly short.

**Set `finalize_role_file` whenever your deliverable is not a diff.** The built-in finalize lens summarizes "the actual committed change", which reads wrong for a document, a report, or a translation — and that summary becomes the pull-request body. Two things a good finalize lens says, both learned the hard way: the turn is a read-only observer, so it must describe what the _pipeline_ did rather than assert that it re-opened or spot-checked anything itself; and it must not state a count or a verdict it was not given ("all citations passed", "all gates passed"). It does not have to guess at the latter — the orchestrator appends every in-flow evaluator's recorded verdict and findings to that turn's prompt, so a gate that accepted **with** findings open cannot honestly be summarized as one that passed. The packaged `deep_research/summary.md` is the worked example.

## Writing and validating a role prompt

1. State the node's single job in the imperative; name the artifacts it should read by path variable.
2. For an evaluator, spell out the findings contract explicitly (severities, the `path`/`what`/`fix` fields, "empty array when clean").
3. Wrap every optional variable in `{?name}…{/name}` so a missing value never leaves a dangling fragment.
4. Run `worc validate-flow <name>` — its anti-drift lint **warns** about any `{name}` no node populates (a typo like `{plna_path}` would otherwise ship as literal text). It is a warning, not a failure, because a verbatim render is the safe fallback.
5. Set `prompt_audit: true` to inspect the exact rendered prompt per node under `logs/<task-id>/prompt-audit/`.

See [prompt-variables.md](prompt-variables.md) for the full `{name}` allowlist, which runner populates each, and the `{<node_id>_path}` chaining channel.
