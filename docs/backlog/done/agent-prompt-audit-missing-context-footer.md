# Agent prompt audit trail is missing the context-files footer

Status: **implemented** (footer bug + supervisor audit gap fixed 2026-07-13; two further gaps found by the sweep are deferred, see below) Date: 2026-07-12 Owner: Vladimir Makarevich

## The problem

The per-attempt audit artifacts (`rendered-prompt.md` and the `prompt` field in `request.json`, both under `.worc/logs/<task>/stages/<node>/run-.../`) do **not** contain the full text an agent actually receives. They capture only the Core-rendered role-prompt template (`AgentRunRequest.prompt`); a second, separate step appends a "context files" footer right before the prompt is sent on stdin — and that step runs **after** the artifacts are written, so the footer is never persisted anywhere.

Mechanism (`src/wastech_orchestrator/providers/_adapter_base.py`):

- `_write_request` → `_request_representation` writes `request.prompt` verbatim into `request.json` (and `rendered-prompt.md` mirrors the same field). This happens first.
- Separately, `build_context_footer(request)` renders the non-`None` context paths (`task_path`, `plan_path`, `diff_path`, `check_artifacts_path`, `review_artifacts_path`, `human_input_path`, `skill_reference_paths`) as a deterministic block:
  ```
  Context files (read them as needed; do not assume their contents):
  - task: <path>
  - plan: <path>
  - diff: <path>
  - review: <path>
  ```
- `build_effective_prompt(request)` = `request.prompt` + `"\n\n"` + that footer (or just `request.prompt` if there are no context paths).
- Only `build_effective_prompt(request)` is what actually gets piped to the CLI's stdin (`stdin_text=build_effective_prompt(request)` in `run()`). The artifact-writing path never calls it.

Net effect: an operator reading `rendered-prompt.md`/`request.json` cannot see which context file paths the agent was actually told about for that turn — the on-disk audit trail is incomplete relative to what was really sent.

## Concrete example

Real run: `p6-04-config-writer-schema`, `stages/fixing/run-000124` (in `wastech-mdlint`).

`request.json` declares 4 context paths:

```json
"context_paths": {
  "task_path": ".../tasks/failed/p6-04-config-writer-schema.md",
  "plan_path": ".../.worc/logs/p6-04-config-writer-schema/plan.md",
  "diff_path": ".../.worc/logs/p6-04-config-writer-schema/current.diff",
  "review_artifacts_path": ".../.worc/logs/p6-04-config-writer-schema/stages/review/run-000123/findings.json"
}
```

But `rendered-prompt.md` (and `request.json`'s own `"prompt"` field) ends at:

```
## Additional Project Context

A brief of repository memory relevant to this task ... is at .../memory/fixing.md.
```

— no mention of the 4 paths above. The text Claude actually received on stdin (never written to disk anywhere) would have continued with:

```
Context files (read them as needed; do not assume their contents):
- task: .../tasks/failed/p6-04-config-writer-schema.md
- plan: .../.worc/logs/p6-04-config-writer-schema/plan.md
- diff: .../.worc/logs/p6-04-config-writer-schema/current.diff
- review: .../.worc/logs/p6-04-config-writer-schema/stages/review/run-000123/findings.json
```

This is why the `fixing.md` role-prompt template (which only ever references `{memory_path}` / `{subtask_spec_path}`, never `{task_path}`/`{plan_path}`/`{diff_path}`/`{review_path}`) still results in the agent knowing where to look: the footer, not the template, carries those paths — and the footer is the part missing from the audit trail.

## Sweep findings (2026-07-13)

A dedicated sweep (3 parallel investigations covering the provider/adapter layer, every flow node kind, and the supervisor/packet/memory layer) answered the "Follow-up needed" section below. It found the footer bug was broader than first scoped, plus three further, independent gaps:

1. The footer bug actually hits **three** audit surfaces, not one: `request.json`, `rendered-prompt.md`, and the prompt-audit JSON/timeline all persisted the bare pre-footer prompt for `agent`/`evaluator` flow nodes — `write_rendered_prompt`/`write_prompt_audit` were called with `request.prompt`, never `build_effective_prompt(request)`, because that function lived in the private `providers/_adapter_base.py` module the node layer never imported. A related one-liner was found alongside it: `skill_reference_paths` was entirely missing from `request.json`'s `context_paths` dict.
2. `core/supervisor.py`'s LLM turns (observe/finalize/handoff/propose_skill_map) never called any observability recorder at all — `rendered-prompt.md`/prompt-audit were never written for a supervisor turn, and `request.json` is pruned by default (`logging.artifacts: standard` doesn't keep it). A bigger gap than #1: the supervisor's input was 100% unrecoverable under the shipped default config, not just missing a suffix.
3. `core/flow/nodes/tool.py` bypasses the whole provider/observability spine (a raw subprocess call) — there is no audit artifact at all for what's sent to a tool executable's stdin, only its redacted stdout/stderr survive.
4. The per-node memory-packet file (`memory/packet.py`) is keyed only by `node_id` and silently overwritten on repeated runs of the same node (e.g. a second `fixing` pass), and is never registered as a tracked artifact — so a `rendered-prompt.md`'s `{memory_path}` reference can end up pointing at stale/later content by the time anyone reads it.

## Resolution

**Fixed (2026-07-13): #1 and #2.** `build_context_footer`/`build_effective_prompt` moved from `providers/_adapter_base.py` to the public `providers/base.py` (alongside `AgentRunRequest`, which both `_adapter_base.py` and the node runners already depend on); `_request_representation` now persists `build_effective_prompt(request)` and includes `skill_reference_paths` in `context_paths`; `core/flow/nodes/agent.py`/`evaluator.py` now pass `build_effective_prompt(request)` (not the bare template) into `record_run_observability`. `core/supervisor.py` now writes `rendered-prompt.md` and (when the `prompt_audit` gate is on) the prompt-audit JSON/timeline for every turn, via the standalone `write_rendered_prompt`/`write_prompt_audit` functions directly — deliberately not `record_run_observability`/`record_provider_attempts`, since the supervisor's `node_run_id` is a synthetic per-call-site namespacing sentinel, not a `node_runs` foreign key. The write happens only on the turn's success path and is wrapped in its own try/except, preserving the supervisor's "advisory, never breaks the task" contract.

**Deferred: #3 and #4.** Both are real but independent gaps, confirmed via AskUserQuestion with the operator to be out of scope for this change:

- `tool.py`'s missing stdin audit needs a new artifact type/writer (nothing to wire up — the audit surface doesn't exist yet for this node kind).
- The memory-packet staleness/registration gap is a different failure mode (a stale reference, not a missing one) with its own fix shape (run-scoping the packet path + registering it).

Revisit either as its own change when an operator need or incident makes it worth prioritizing.

## Follow-up needed: sweep for other similar audit gaps (resolved by the sweep above)

This context-files footer was originally only the **one instance found so far** while answering an operator question about a specific run — it was not the product of a deliberate audit sweep. The same class of bug (something real is appended/prepended to what the agent actually receives, after or outside the step that writes the audit artifact) was suspected to exist elsewhere. The dedicated pass above confirmed it did, covering:

- Both providers (`providers/claude.py`, `providers/codex.py`), not just the shared `_adapter_base.py` path — checked for any provider-specific pre/post-processing of the prompt, system prompt, or argv that happens after `_write_request`. Neither provider does; the footer computation is the sole content divergence at that layer, and `--resume`'d sessions were confirmed to re-send the full context-path footer on every turn (no turn skips it assuming an earlier turn already sent it), so there is no additional resume-specific audit gap beyond the footer itself.
- Every node kind that calls into a provider (`core/flow/nodes/agent.py`, `evaluator.py`, `tool.py` and the others — `checks.py`, `hitl.py`, `publish.py`) — see finding #3 above for `tool.py`; `checks`/`hitl`/`publish` have no prompt concept and are not affected.
- Anything injected via the supervisor/packet/memory layer (`core/supervisor.py`, `packet.py`, `memory/*`) — see findings #2 and #4 above.
