# Agent prompt audit trail is missing the context-files footer

Status: **candidate** (finding only, no decision yet) Date: 2026-07-12 Owner: Vladimir Makarevich

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

## Possible next step (not decided)

Persist `build_effective_prompt(request)` (or at least the footer) into the audit artifacts instead of the bare `request.prompt` — likely in `_write_request`/`_request_representation` in `_adapter_base.py`, and wherever `rendered-prompt.md` is written. Not scoped or planned yet; recording the finding only.

## Follow-up needed: sweep for other similar audit gaps

This context-files footer is only the **one instance found so far** while answering an operator question about a specific run — it was not the product of a deliberate audit sweep. The same class of bug (something real is appended/prepended to what the agent actually receives, after or outside the step that writes the audit artifact) could exist elsewhere and has not been checked yet. Needs a dedicated pass, not assumed to be limited to this one footer:

- Both providers (`providers/claude.py`, `providers/codex.py`), not just the shared `_adapter_base.py` path — check for any provider-specific pre/post-processing of the prompt, system prompt, or argv that happens after `_write_request`.
- Every node kind that calls into a provider (`core/flow/nodes/agent.py`, `evaluator.py`, `tool.py` and any others), not just `fixing` — each may assemble/append its own extra context differently.
- Anything injected via the supervisor/packet/memory layer (`core/supervisor.py`, `packet.py`, `memory/*`) — per the follow_ups.md history (F43/F48 and others), this layer already has known packet-content issues; check whether any of what it injects is likewise absent from the audit artifacts.
- `--resume`'d sessions specifically: confirm whether anything about the resumed session (e.g. what established the task/plan context on the _first_ turn) is itself unaudited, since later turns may rely on it without repeating it in their own prompt.

Goal: produce a list of every point where agent input diverges from what `rendered-prompt.md`/`request.json` record, then decide case by case whether to fix (persist the real effective input) or accept as a known, documented gap.
