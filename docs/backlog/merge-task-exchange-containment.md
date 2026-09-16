# `worc merge-task` never reaches the merge flow: `task_path` breaches exchange containment

Status: **fixed** Date: 2026-09-16 Owner: Vladimir Makarevich

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

This document's first answer — "no test drives `_run_merge_flow`" — was wrong, and the real answer is more useful. `tests/core/test_merge_task.py` has driven the whole routine against a real conflicting merge, a real exchange root and fake provider CLIs since the feature landed, and it passed throughout. It passed because its fixture seeded a `TaskRow` with **no `source_path`**, so `_degraded_pipeline` set `task_file` to `""` — and containment skips falsy values (`if value`), which made the one field under test invisible. A test can exercise the exact call path and still prove nothing if its fixture is cheaper than reality; the fixture now carries `tasks/done/m1.md`, which is what makes that test a regression test.

## How it was fixed

One line in `_run_merge_flow`, immediately after `build_node_inputs`, making the function's stated intent true of its inputs:

```python
# The merge flow publishes no task packet (see the comment above), so the pipeline's live
# tasks/ path would breach exchange containment. No merge role prompt may reference
# {task_path}; the conflict agent works from the conflicted working tree.
inputs.task_path = None
```

`{task_path}` renders empty in merge role prompts, which is the honest result — there is no task packet to point at. Two things landed beside it:

- `merge_task` converts a `NodeManualRequired` raised inside the merge flow into `ManualActionRequired`, and `cmd_merge_task` prints it with exit 2. The node-layer class is not `git_manager.ManualActionRequired`, so it escaped the CLI uncaught and reached the operator as a Python traceback.
- The merge flow now receives a conflict inventory as `{conflicts_path}`, and the orchestrator refuses to commit a conflicted path the flow left byte-identical to what the merge put there. Both are the neighbouring defect this one hid: a conflict with no markers was being committed silently. See [merge-flow-conflict-competence.md](merge-flow-conflict-competence.md) for what is still open.

## What stayed open

Both questions this document raised — whether the merge flow should publish a task packet, and whether containment should fail at wiring time rather than at launch — moved to [merge-flow-conflict-competence.md](merge-flow-conflict-competence.md) with the rest of the merge-flow work.

## Scope / risk

Core only; no schema, config or flow-file change, and no migration. The risk of the fix is that a merge role prompt somewhere depends on `{task_path}` — nothing packaged does, and nothing could, since the path has never been reachable on this route.

## Note for anyone writing merge role prompts

Because the merge flow publishes no task packet **by design**, its roles cannot rely on `{task_path}`. Write them against the conflicted working tree and against `{conflicts_path}`, the inventory the orchestrator publishes for exactly this purpose. Wrap `{diff_path}` and `{checks_path}` in `{?name}…{/name}` blocks: both can legitimately be empty when `conflict_resolution` starts, since no workspace-write edit and no checks run have happened yet in that flow.
