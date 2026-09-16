# `worc merge-task` never reaches the merge flow: `task_path` breaches exchange containment

Status: **defect, reproduced** Date: 2026-09-16 Owner: Vladimir Makarevich

## Problem

`worc merge-task <id>` cannot resolve a conflict. When pulling the base branch into the task branch produces conflicts — the only situation the merge flow exists for — the run aborts before the `conflict_resolution` node makes its first provider call:

```text
NodeManualRequired: exchange containment violation:
provider request field 'task_path' is not under the current exchange: tasks/done/<id>.md
```

A clean base-merge is unaffected: it is mechanical, starts no agent, and never builds a provider request. So the feature works in exactly the case that does not need it and fails in the case that does.

Reproduced on `0.14.0a1` against a real conflicting pull request, twice, with two different sets of role prompts. The defect is present in `dev` as of this date: `_run_merge_flow` is unchanged.

## Current behavior (verified)

Three pieces, each defensible alone, that combine into a dead path.

**Containment checks `task_path`.** [`assert_orchestration_paths_contained`](../../src/wastech_orchestrator/providers/exchange.py) fails closed unless every non-empty path field of an `AgentRunRequest` resolves under the exchange root, and `task_path` heads the list of fields it walks. `working_directory` is the sole permitted non-exchange path. This is correct and should stay as it is — it is the guard that stops a private or live path leaking to a provider.

**A normal run satisfies it by re-pointing the field.** [`_publish_frozen_task_packet`](../../src/wastech_orchestrator/core/orchestrator.py) overwrites `inputs.task_path` with the redacted exchange copy of the frozen task packet. Its own docstring says so: "Re-point `task_path` at the redacted exchange copy of the frozen task packet." That is why every implementation and content run passes containment — not because the pipeline's value was ever containable.

**The merge flow skips that step on purpose.** [`_run_merge_flow`](../../src/wastech_orchestrator/core/orchestrator.py) states the intent in a comment: "Deliberate: the merge flow is ephemeral and NOT frozen; it publishes no task packet and injects no repository instructions." It then calls [`build_node_inputs`](../../src/wastech_orchestrator/core/flow/wiring.py), which sets `task_path=p.task_file` — the live repository path, `tasks/<state>/<id>.md`.

So the field keeps a repository path that containment is designed to reject, and the request dies in [`agent.py`](../../src/wastech_orchestrator/core/flow/nodes/agent.py) at the `assert_request_contained` call before any provider is launched.

Two consequences worth stating plainly, because both cost time to re-derive:

- **It is not a configuration or prompt problem.** `task_path` is a field of the request object, not a prompt token. Removing `{task_path}` from every merge role prompt changes nothing — verified by doing exactly that and getting a byte-identical failure.
- **It is not specific to a completed task.** The error text names `tasks/done/<id>.md` because `merge-task` runs after a task finishes, but `tasks/pending/<id>.md` is equally outside the exchange. Any value of `p.task_file` fails.

**What still holds.** The transactional promise is intact and was exercised twice: after the failure the orchestrator ran `git merge --abort`, left the working tree clean with no `MERGE_HEAD`, and left the pull request open and conflicting. Nothing had to be repaired by hand.

## Why it was not caught

No test drives `_run_merge_flow`. Searching `tests/` for it returns only `tests/install/test_config_writer.py`, which asserts the `git.merge_flow` config key exists — not that the flow runs. Containment has coverage (`tests/core/test_flow_threat_model.py` among others), but nothing composes the two, so the one call path where a node input is deliberately left unpublished is the one path never exercised.

## Proposed minimal fix

One line in `_run_merge_flow`, immediately after `build_node_inputs`, making the function's stated intent true of its inputs:

```python
# The merge flow publishes no task packet (see the comment above), so the pipeline's live
# tasks/ path would breach exchange containment. No merge role prompt may reference
# {task_path}; the conflict agent works from the conflicted working tree.
inputs.task_path = None
```

`{task_path}` then renders empty in merge role prompts, which is the honest result — there is no task packet to point at.

A regression test belongs with it: drive `_run_merge_flow` far enough to build the request and assert `assert_orchestration_paths_contained` passes. The cheap version needs no provider — building `NodeInputs` for the merge flow and asserting `task_path is None` pins the invariant that made this possible.

## Open questions

- **Should the merge flow publish a task packet instead?** The comment in `_run_merge_flow` already anticipates this — "A future merge agent needing richer repository conventions would wire them here" — and a merge agent that knew the task's acceptance criteria could resolve a conflict better than one working from markers alone. That is a larger change than the fix above and should not block it: clear the field now, decide about the packet separately.
- **Should containment fail loudly at wiring time rather than at launch?** The breach is decidable when `NodeInputs` is built, not only when the request is assembled. Asserting earlier would have turned this into a startup error in any flow that forgets to publish an input, instead of a runtime failure on the one path nobody tested.

## Scope / risk

Core only; no schema, config or flow-file change, and no migration. The risk of the fix is that a merge role prompt somewhere depends on `{task_path}` — nothing packaged does, and nothing could, since the path has never been reachable on this route.

## Note for anyone writing merge role prompts

Because the merge flow publishes no task packet **by design**, its roles cannot rely on `{task_path}` even after the containment bug is fixed. Write them against the conflicted working tree, which carries both sides of every hunk. Wrap `{diff_path}` and `{checks_path}` in `{?name}…{/name}` blocks: both can legitimately be empty when `conflict_resolution` starts, since no workspace-write edit and no checks run have happened yet in that flow.
