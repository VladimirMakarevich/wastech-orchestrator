Address the failing checks and/or the blocking review findings in the context files. Make the minimal change needed to resolve them. If a human_input context file records a denied dangerous change, remove or safely rework that change.

Do NOT run `git` and do NOT commit, stage, push, or merge anything — the orchestrator publishes.

{?conflicts_path}This is a merge: the conflicted paths are listed at {conflicts_path}. A check failing on one of them is usually a resolution that took the wrong side, not a defect to patch around — fix the resolution. Never revert a conflict resolution, and never delete or empty a conflicted file, just to make a check pass; if the two sides are genuinely incompatible, say so plainly in your final message and stop.{/conflicts_path}

Stay strictly within scope. Fix only what _your_ change broke and what the task asked for; do not edit files outside the task's scope to chase an unrelated failure. Do **not** work around a failure caused by a missing or incompatible host toolchain (for example, an SDK or runtime version the environment does not provide) or by a check that was already failing before your change — changing project target frameworks, pinning toolchain versions, or disabling such a check is out of bounds. Leave those failures as they are, revert any experiment you made toward them, and describe them plainly in your final message so a human can act: a check that cannot pass in this environment is not yours to "fix".

{?memory_path}A brief of repository memory relevant to this task — failure signatures with their canonical remedy, known-fragile areas, and entity notes for the files you are touching — is at {memory_path}. Check it for a known fix before improvising; treat it as advisory and verify each point against the current code (it can be stale).{/memory_path}

{?subtask_spec_path}You are fixing subtask {subtask_order} of {subtask_count}; keep your change scoped to that subtask's spec: {subtask_spec_path}{/subtask_spec_path}
