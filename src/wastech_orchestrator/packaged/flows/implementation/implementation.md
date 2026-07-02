Implement the task in the working tree, following the plan. Make a minimal focused change. If a human_input context file records a denied dangerous change, remove or safely rework that change.

{?memory_path}A brief of repository memory relevant to this task — distilled lessons, conventions, known-fragile areas, and entity cards for the files you will touch — is at {memory_path}. Read it before editing and let it guide the change; treat it as advisory and verify each point against the current code (it can be stale).{/memory_path}

{?subtask_spec_path}The task is decomposed and you must implement ONLY this subtask — subtask {subtask_order} of {subtask_count} — per its immutable spec: {subtask_spec_path}{/subtask_spec_path}

{?predecessor_context}A handoff brief covering the subtask(s) this one depends on — their changed files, locked decisions, and open edges — is at {predecessor_context}. Read it first: build on what they established, do not re-explore or duplicate it, and do not break the contracts it marks as locked. It is ground truth for facts (files, commits) and advisory for interpretation — verify interpretive claims against the current code.{/predecessor_context}
