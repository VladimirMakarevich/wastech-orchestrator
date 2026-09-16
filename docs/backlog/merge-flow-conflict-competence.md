# The merge flow resolves conflicts blind: what is still missing

Status: **open** Date: 2026-09-16 Owner: Vladimir Makarevich

## Why this exists

`worc merge-task` was built on a specific promise: the orchestrator wrote these four pull requests, so when they conflict with each other it is the one thing in the room that knows what each change was for — it should be able to resolve any conflict, of any kind, and drive the conflicting pull request to a close.

Two defects found while testing that promise against a real repository are fixed (see [merge-task-exchange-containment.md](merge-task-exchange-containment.md)): the flow could not start at all, and a conflict with no markers was being committed silently by nobody. What is fixed is the floor — the flow runs, and the orchestrator refuses to commit a decision that was never made. The promise itself is not met, and this document says exactly where the gap is.

The gap in one sentence: **the merge agent knows what conflicted, and nothing else.** It is not told what the task was trying to do, it cannot look at either side's history, it does not inherit the session that wrote the code, and it has no way to tell the orchestrator "I read both sides and chose to keep ours".

## Items, in the order they matter

### 1. The merge agent gets no task context

`_run_merge_flow` publishes no task packet by design, so after the containment fix `{task_path}` renders empty. The agent sees the conflicted working tree and the conflict inventory; it does not see the task's description, its acceptance criteria, its plan, its diff, or the pull request body. A semantic conflict — two changes that merge cleanly at the text level and contradict each other in behavior — is exactly what that missing context would decide, and is exactly what the agent cannot see.

Constraint: the packet must reach the provider the way every other one does — frozen, redacted, published into the exchange — not as a live `tasks/` path. `_freeze_task_and_repo_instructions` already skips gracefully when there is no source file, so the mechanism exists; what needs deciding is whether an ephemeral, unfrozen flow should carry a frozen bundle at all, and what its digest binds to when there is no run to bind it to.

### 2. No channel for a declared resolution

The gate accepts a decision only when it can see one in the working tree: different bytes, or the file gone. For a conflict with no markers that leaves one honest answer unexpressible — "I read both sides and our version, unchanged, is correct" produces the same tree as "nobody looked". The gate refuses both, deliberately (a refused correct resolution costs one manual merge; an accepted undecided one ships a merge nobody made), but it is a real false refusal on a real case.

The fix is a declaration channel: the `conflict_resolution` node returns a per-path verdict and the orchestrator reads it as evidence. Nothing supports that today — `FlowRunResult` carries no node outcomes, `node_runs` has no output column, `_run_merge_flow` passes no `post_node` hook, and the typed JSON survives only in the per-attempt `result.json`. So this is a small contract plus a small piece of plumbing, and it should be designed together with item 1 (both are "what does the merge agent say and hear").

### 3. The merge role forbids reading Git, not just writing it

`conflict_resolution` is a `workspace-write` node, so it already has an unrestricted shell; the default `denied_commands` blocks `git commit` / `git push` / `gh pr create` / `gh pr merge`. The role prompt nevertheless says "do not run `git`" with no distinction, which removes the read verbs too — `git log`, `git show`, `git diff`, and above all `git show :1:<path>` / `:2:` / `:3:`, the three sides of the conflict as Git holds them. Those are what a human uses on a hard conflict. The inventory currently substitutes a description of the sides for the sides themselves.

Constraint: the mutation ban is not negotiable (only the orchestrator commits, pushes, opens and merges pull requests) and the read grant must not read as a loophole in it. `git_evidence` is not the mechanism here — the flow validator refuses it on a `workspace-write` node precisely because such a node already has a shell — so this is prompt wording plus a note in the guide, not a capability change.

### 4. The implementation session is not reused

`editing_lineage` rows survive a task's completion (only `rerun` clears them), so the session that wrote the code is still addressable when `merge-task` runs. The flow validator requires `lineage_affinity` to name a node **in the same flow**, so a merge node cannot join the implementation flow's lineage. Lifting that would give the merge agent the whole conversation that produced the change — the strongest form of item 1 — at the cost of a cross-flow trust edge, a provider-match requirement, and sessions old enough that the provider may refuse to resume them.

### 5. The merge commit is not scoped to the conflict

`commit_merge_resolution` stages with `git add -A` (it must: a base merge brings in the base's own changes). Combined with `output_policy: code_change` and the default `trust_level: auto`, nothing reports an edit the merge agent made outside the conflicted set — it rides into the merge commit unremarked. The conflict inventory now gives the orchestrator the exact path set, so a report (or a refusal) on out-of-scope edits is finally cheap to build.

### 6. `trust_level: strict` asks the operator about the base's own deletions

The dangerous-diff gate measures from the last commit the orchestrator made for the task, so during a base merge every file the base deleted reads as a deletion in this task's diff. Under `strict` that suspends the merge flow for a human approval of changes the task did not make. Worth deciding whether the gate should measure differently inside a merge, or be inert there.

### 7. The merge run leaves an unsealed exchange

`merge_task` publishes into `.worc-io/<task-id>/` (the conflict inventory, the diff, check logs) and never seals it: there is no quiescence-gated seal into the private audit tree the way a task run has. The directory is cleared by the next task's foreign-entry sweep, so nothing breaks — but the merge run's provider-readable surface leaves no audit snapshot behind.

### 8. Containment is checked at launch, not at wiring

The breach that produced [merge-task-exchange-containment.md](merge-task-exchange-containment.md) was decidable the moment `NodeInputs` was built, and was instead found when the provider request was assembled — on the one route nobody had exercised. Asserting at wiring time would turn "a flow forgot to publish an input" into a startup error in every flow, rather than a runtime failure in one.

## What is deliberately not here

Making the gate smarter about _who_ moved the bytes. A check command that rewrites files (a formatter, a code generator) satisfies the gate on a path the agent never considered; the gate's claim is only that the bytes moved. Tightening that means tracking authorship of working-tree writes, which is a much larger mechanism than the problem justifies — the honest fix is item 2, where the agent states its decisions and the tree stops being the only evidence.
