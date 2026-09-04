# Happy path — how a task runs with `resume_role_file`

The execution walk-through for [the ADR](README.md). It states, step by step, which prompt variant each node run receives and why, then the branch where the session is lost.

## Setup

The packaged `implementation` flow. `fixing` and `implementation` each carry a `resume_role_file:`; every other node is untouched. One provider (`codex`), no fallback, no decomposition, `security.strict_isolation` on. The task takes one test-fix round, then one review-fix round.

Three inputs decide every row below, all resolved before the provider is launched:

| Input | Source |
| --- | --- |
| `session_id` — is a session being resumed? | `_resolve_resume` → `_resume_lineage`: the `editing_lineage` row for `(task, lineage_key, subtask)` whose `provider` matches the resolved route |
| has this node spoken on it? | a prior `node_runs` row for `(task, node_id, subtask)` with `provider_used` = that provider. Agent nodes only: an evaluator's `node_lineage` row is written by nobody but itself, so a live session already answers this |
| does the node offer a second text? | `resume_role_file` on the node |

## The sequence

| # | Node | Session on entry | Spoken here before | Prompt sent | Why |
| --- | --- | --- | --- | --- | --- |
| 1 | `planning` (`fresh_disposable`) | none | — | `planning.md` | Nothing to resume; the field is forbidden on this scope. |
| 2 | `implementation` (`editing_lineage`) | none — lineage empty | no | **full** `implementation.md` | First work turn of the task. On success `_persist_session` writes the lineage row: provider `codex`, session `S1`. |
| 3 | `testing` (`checks`) | — | — | no prompt | A checks node runs commands, not an agent. Fails → `test_fix` edge. |
| 4 | `fixing` round 1 (`lineage_affinity: implementation`) | `S1` | **no** | **full** `fixing.md` | The session has history, but _this role_ has not spoken in it: "Scope Discipline" and "Fix The Finding, Then Its Class" would otherwise be missing on the one turn that needs them. This is the row a `session_id`-only predicate would get wrong. |
| 5 | `testing` | — | — | no prompt | Fails again → `test_fix` round 2. |
| 6 | `fixing` round 2 | `S1` | **yes** (step 4) | **continuation** | Rules already in the conversation; the turn carries what changed — the new `checks` artifact — and nothing else. |
| 7 | `testing` → `review` (`fresh_disposable` evaluator) | none | — | `review.md` | Evaluators never join an author lineage; a review pass is fresh by construction. Verdict `rework`. |
| 8 | `fixing` round 3 (review-driven) | `S1` | yes | **continuation** | Which loop sent it here is irrelevant: the predicate asks who has spoken, not which edge was taken. The changed input is `{review_path}`. |
| 9 | `review` → accept → `documentation` (`lineage_affinity: implementation`) | `S1` | **no** | **full** `documentation.md` | Same trap as step 4, one node later: a resumed session, a role nobody has stated. |
| 10 | `publish` | — | — | no prompt | Orchestrator-owned. |

Two properties fall out of the table: the full text is sent exactly once per role per lineage, and every node whose role is new to the session gets it in full even though the session is warm.

## What the two texts hold

The main file is unchanged. The continuation file is a delta, not a summary of the main one — if it restates the rules, the change bought nothing. Illustrative shape for `fixing`:

```markdown
Another round on the same change. New results are in the context files above — read those, not the whole task again.

- Address every finding recorded this round, and its class elsewhere in the same artifact.
- The rules from earlier in this conversation still stand; do not re-derive the task or re-read files you already reviewed unless a finding points at them.
- If the same failure is recurring and the cause is outside this task's scope, stop and say so plainly instead of trying again.
```

Kept deliberately: one line of output contract where semantics (not shape) matter — an evaluator's continuation text still says "grade honestly, the flow decides the gate", because the CLI flag enforces the schema but not the calibration.

## In-node round trips

The same predicate covers the re-invocations that never leave the node, because each `_invoke` records its own `node_runs` row and the previous one has already completed with `provider_used` set:

| Trigger | Path | Result |
| --- | --- | --- |
| Max-turns grant renewed | `_invoke_with_turn_gate` loops with `resume_session_id` from the finished outcome | continuation — the agent ran out of turns mid-work and is told to carry on, not to start the task |
| HITL answer delivered | `_resume_interaction` → `_invoke` with the first run's session and the answer file | continuation + the answer at `{human_input}` in the footer |
| Dangerous diff denied | `_reconsider` → `_invoke` with no explicit session, so it resumes the lineage the finished run just wrote | continuation + the denial context |

## When the session is lost

The same task, one row different. The router clears `session_id` _after_ Core built the request and without touching the prompt text — `session_unavailable` (same-provider fresh retry), the transient-retry degrade, and the cross-provider fallback. Because the variant is chosen at the neutral seam from `request.session_id`, that attempt gets the **full** text automatically:

| Attempt | `session_id` on the attempt | Prompt sent |
| --- | --- | --- |
| `fixing` round 2, attempt 1 | `S1` | continuation |
| … `session_unavailable` → attempt 2, same provider, fresh | `None` | **full** `fixing.md` |
| … or primary exhausted → fallback attempt on the other provider | `None` (cleared with `model` / `reasoning` / `extra_args`) | **full** `fixing.md` |

The failure this prevents is the one worth stating outright: a "continue where you left off" turn arriving in a brand-new session with no rules, no remit and no history — a run that reports success and quietly produces worse work.

## The rule in one place

```python
# core/flow/nodes/agent.py — _build_request
continuation = (
    node.resume_role_file is not None
    and session_id is not None
    and self._s.store.has_prior_provider_run(
        ctx.task_id, node.id, ctx.subtask_order, provider, exclude_run_id=run_id
    )
)
request = AgentRunRequest(
    prompt=render_role_prompt(flow_dir, node.role_file, variables, allowed=allowed),
    continuation_prompt=(
        render_role_prompt(flow_dir, node.resume_role_file, variables, allowed=allowed)
        if continuation
        else None
    ),
    session_id=session_id,
    ...
)

# providers/base.py — build_effective_prompt (the neutral seam, per attempt)
body = request.continuation_prompt if (request.session_id and request.continuation_prompt) else request.prompt
```

On an evaluator the third clause drops: its session comes from `node_lineage`, which only that node writes, so `session_id is not None` already carries "has spoken here".

Core decides what _may_ be a continuation; the seam decides whether this attempt actually is one. Rendering the second template costs a file read and a regex pass, and only for a node that declares one.

## Verifying it on a real run

With the top-level `prompt_audit: true` (or the same key on the task) the run writes `prompt-audit/timeline.jsonl` plus a per-node-run prompt file. The record carries both texts once and names the variant **per attempt** — it has to, because the node-level record is built from the request Core assembled, and an attempt the router degraded never received it:

```json
"prompt": "<full fixing.md>",
"continuation_prompt": "<continuation text>",
"agents": [
  { "provider": "codex", "attempt": 1, "resumed": true,  "prompt_variant": "continuation", "error_class": "session_unavailable" },
  { "provider": "codex", "attempt": 2, "resumed": false, "prompt_variant": "full" }
]
```

On the walk-through above the signature is: `fixing` run 1 with no `continuation_prompt` at all, runs 2 and 3 carrying one with every attempt at `continuation`, `documentation` back to none, and any run that fell back or retried fresh showing `prompt_variant: full` on exactly that attempt. Anything else means the predicate is wrong, not the wording.
