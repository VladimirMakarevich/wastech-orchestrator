# Problem — Agent diff hand-off

## The problem, as stated

After every role that edits the working tree, the orchestrator rebuilds the task's whole change into one file, and every later role is pointed at it. The operator's suspicion: in advanced mode (`security.strict_isolation: false`), where every node has a shell, this mechanism makes things worse rather than better — it loads the agent's context with a large file the agent then supplements, or duplicates, by inspecting the repository with `git` itself. The question behind it: for an agent working on a codebase with a concrete task, is it better to find the changes itself through the shell, or to read and track this file?

## Whose problem it is

- **An agent executing a node** — it pays for the file in context, and it is given a path but not the facts it would need to inspect the change itself (where the change starts).
- **The operator running `worc`** — pays for those tokens on every review, fix and documentation pass, across every rework round.
- **A maintainer of this repository** — owns the role prompts and the hand-off contract, which today give one answer to every node regardless of what the node can do.

## Current situation (the pain)

What happens today, from the code:

1. After a `workspace-write` agent node, `GitManager.write_current_diff` (`src/wastech_orchestrator/git_manager.py:3597`) writes `git diff --text <base> -- :/ <runtime excludes>` against the working tree — untracked files included through a transient intent-to-add, secrets redacted — to the private `.worc/logs/<task-id>/current.diff`.
2. `_apply_post_edit_guard` (`src/wastech_orchestrator/core/flow/nodes/agent.py:649`) publishes a redacted copy to `.worc-io/<task-id>/current.diff` and stores its path in the shared `NodeInputs.diff_path`. The same diff feeds the dangerous-diff guard.
3. From then on every agent and evaluator request carries that path: `build_context_footer` (`src/wastech_orchestrator/providers/base.py:303`) appends `- diff: <path>` under "Context files (read them as needed; do not assume their contents)", and a role prompt may also name it as `{diff_path}`.

What the agent does **not** receive:

- **The base.** `_diff_base` (`src/wastech_orchestrator/git_manager.py:1513`) — `base_branch` for a `new` branch, the branch tip at task start for an `existing`/`current` one — is internal; no prompt variable carries it. An agent that wants to inspect the change itself has to guess, and the guesses observed are `git diff HEAD`, `git diff --stat`, `git status`, `git log --oneline -N`.
- **Any summary.** The file is the whole cumulative change since the base — every subtask of a decomposed run, every file, every hunk. There is no stat, no file list, no indication of size before the agent opens it.

Why it hurts:

- A node with a shell reads the file **and** runs git on top of it: the same change enters the context twice, in two shapes.
- The file is often too large to read in one call, so it is read in chunks — more turns, more tokens.
- The hand-off is identical for every node: a fixer that resumes the implementation session (and remembers what it changed), a fresh-session reviewer, a documentation pass and a content critic all get the same path to the same monolithic file.

## Why it matters now

Review, fixing and documentation run once per rework round, and the `implementation` flow's budgets allow up to 15 `review_fix` and 15 `test_fix` rounds. Whatever a pass spends on re-reading the change is multiplied by the number of rounds. Advanced mode is what a fresh `worc install` writes, so it is the default posture, and the one in which every node has the shell that makes the duplication possible.

## Signals & evidence

Measured on 2026-10-19 over every recorded node run whose `request.json` carries `diff_path`, in four target repositories with orchestrator history: `wastech-mdlint`, `wastechlab-mobile-template`, `wastime-app-content`, `wastime-journey` (`.worc/logs/<task>/stages/<node>/run-*/<attempt>-<provider>/`). "Shell" means the Claude argv's `--tools` includes `Bash`; every Codex run can execute commands. "Read the diff" means a `Read`/`Grep` of `current.diff` or a command naming it; "git" means `git diff|status|show|log`.

**Size of `current.diff`** (one per task, the last written):

| Repository                   | Tasks | Median | p90    | Max     |
| ---------------------------- | ----- | ------ | ------ | ------- |
| `wastech-mdlint`             | 44    | 57 KB  | 684 KB | 1156 KB |
| `wastechlab-mobile-template` | 9     | 20 KB  | 63 KB  | 63 KB   |
| `wastime-app-content`        | 30    | 97 KB  | 179 KB | 195 KB  |
| `wastime-journey`            | 8     | 14 KB  | 19 KB  | 19 KB   |

**Nodes of the `implementation` flow, with a shell:**

| Node (provider) | Runs | Read the diff | Ran git | Both | Chunked reads |
| --- | --- | --- | --- | --- | --- |
| `review` (Claude) | 32 | 32 | 32 | 32 | 7 |
| `review` (Codex) | 16 | 16 | 2 | 2 | 0 |
| `documentation` (Claude) | 50 | 50 | 30 | 30 | 4 |
| `fixing` (Claude) | 50 | 33 | 43 | 29 | 1 |

**Content flows' critics and fixers, with a shell (Claude):** critics lean on the file and rarely go to git — `page_critic` 112/113 read it, 12 ran git; `chapter_reader` 28/28 and 3; `regression_critic` 18/18 and 7. Fixers mostly ignore it — `bookkeeping_fixing` 0/26 read it, `page_revise` 2/62, `page_fixing` 2/44.

**Nodes without a shell** (read-only Claude with `--tools Read,Glob,Grep`): `review` 30/30 read the diff, the content critics 34–37 of 35–40. For them the file is the only view of the change there is.

What the evidence says, without yet saying what to do about it:

- The duplication is real and concentrated: every shell-enabled Claude reviewer read the file and then went to git anyway.
- The file is not useless: critics and no-shell nodes depend on it, and Codex reviewers mostly read it and stop there.
- A node that resumes its own editing session reads it often (33/50 fixing runs), though it already knows what it changed.

Limits of the evidence: four repositories, one operator; `wastech-mdlint` dominates the `implementation`-flow counts; the run-time `strict_isolation` value is not recorded per run, so the split is by actual tool availability rather than by mode; token cost is inferred from file size, not taken from provider usage.

## Constraints given up front

- **`current.diff` stays the core's artifact.** It is read by the dangerous-diff guard, the supervisor packet and PR body, the packaged `check_length` / `check_chapter` tools (through `paths.diff_path`), the failure report, and recovery (`_restore_engine_inputs`, `src/wastech_orchestrator/core/orchestrator.py:2493`). Whatever changes is what the **agent** is handed, not whether the core writes the file.
- **A node without a shell has no other channel.** Under `strict_isolation: true` a read-only Claude node has no shell unless it is granted `git_evidence`; it can only read files.
- **Task content reaches a provider only as a file path**, never inlined; prompt variables are paths or small metadata.
- **The core knows no CLI syntax**: what a node may run is decided in `providers/`, not in the hand-off.
- **No secrets** in what an agent is shown: the published copy is redacted today, and a raw `git diff` the agent runs itself is not.
- **Greenfield MVP** — no deployed installs, no migration or compatibility path for the old hand-off.
- **Cross-platform** — Windows / Linux / macOS for anything stored, compared or displayed.
