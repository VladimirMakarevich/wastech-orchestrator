# Out of scope — Agent diff hand-off

Fences [requirements.md](requirements.md).

## Not in this task

- **How the core computes `current.diff`** — its base, its pathspec, the intent-to-add bracket, `--text`, redaction and the moment it is written stay exactly as they are. This task changes what an agent is _handed_, not the artifact the core relies on.
- **The core's other readers of the diff** — the dangerous-diff guard, the supervisor packet and PR body, the `check_length` / `check_chapter` tools, the failure report and recovery keep reading `current.diff` unchanged.
- **Removing the full diff from any node's hand-off** — FR-3 keeps it reachable for every node; a node with a shell is _guided_ away from reading it first, not denied it.
- **Granting a shell to a node that has none** — for example giving every read-only reviewer `git_evidence` under `strict_isolation: true` so it can follow the shell guidance. That is a security-posture decision with its own owner (`security.allow_git_evidence`), not a hand-off change.
- **Inlining the change into the prompt** — the overview is a file reached by path; only the base SHA, a small piece of metadata, is substituted inline. Task content still never reaches a provider as prompt text.
- **The other context artifacts** — `plan`, `checks`, `review`, `prior_fix`, `human_input` and the supervisor `packet` keep their current hand-off, even where the same "overview first" idea might apply.
- **Operator-installed flows** — only the packaged copies under `src/wastech_orchestrator/packaged/flows/` are rewritten. Getting them into an already-installed target's `.worc/flows/` is the `upgrade-flows` backlog item's job.

## Deferred (maybe later, not now)

- **A delta since the previous pass** — on a rework round a reviewer mostly needs what changed since it last looked, not the whole change again. Deferred because it needs a durable per-node "last seen" point and its own semantics for decomposed runs; revisit once the overview has landed and the NFR-4 measurement shows how much a re-review still spends.
- **A model-written summary of the change** — the overview is deterministic: paths, kinds and counts computed by code. A narrative summary would be another model turn and another thing to trust. Revisit only if the deterministic overview is shown to be insufficient.
- **Refreshing the overview at the start of every node** — FR-6 stays a Could until Q-3 shows something between nodes changes the tree; until then the overview written after each editing node is the fresh one.
- **Measuring cost from provider usage** — the evidence and the success signal use file size and tool calls; attributing actual tokens to diff reads belongs to the post-mortem tooling (`analyze-task-run`), not to this change.

## Explicit non-goals

- We will **not** key the behavior on the value of `security.strict_isolation`. The pain was seen in advanced mode because that is where nodes have a shell, but what decides the guidance is whether _this attempt_ gets one — the same advanced-mode node on native Windows has none.
- We will **not** add a config key or a flow-node knob to choose between the old and the new hand-off. One behavior; a role prompt that wants the whole diff already names `{diff_path}`.
- We will **not** keep the old hand-off around for compatibility — greenfield, no deployed installs; the packaged prompts and the footer change together.
- We will **not** make the overview authoritative over the working tree. It is a map for deciding what to open; the files in the clone remain what a node judges.
