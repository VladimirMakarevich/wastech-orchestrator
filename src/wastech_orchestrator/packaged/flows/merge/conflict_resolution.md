The task branch has been merged with the latest base branch and the merge stopped on conflicts: one or more files in the working tree contain Git conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`). Resolve every conflict.

For each conflicted file, produce a single coherent version that preserves BOTH the intent of the task's change AND the changes that have since landed on the base branch. Remove every conflict marker. Make only the edits the merge requires — do not refactor, do not touch files the merge did not conflict on, and do not discard either side's work.

Do NOT run `git` and do NOT commit, stage, push, or merge anything — the orchestrator stages and commits the merge once the tree is clean and the checks pass. Just leave the resolved files in the working tree.

If a conflict genuinely cannot be resolved safely (the two sides are incompatible and merging them would change behavior in a way you cannot determine), do not guess: leave a clear, specific description of the conflict in your final message so a human can take over.
