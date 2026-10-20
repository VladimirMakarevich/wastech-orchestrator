# Requirements — Agent diff hand-off

Derived from [problem.md](problem.md).

Vocabulary used below. **The task's change** is exactly what `current.diff` describes today: the working tree against the task's diff base, untracked files included, the runtime home excluded, secrets redacted. **The hand-off** is everything a node is given about that change: today, one path. **A node with a shell** is an attempt whose route grants command execution, as the router already decides it (`route_grants_shell`); a node without one can only read files.

## Functional requirements

- **FR-1 (Overview before content)** — Every node that today receives the diff path also receives a bounded **overview of the task's change**: each changed path with its kind (added / modified / deleted / renamed) and its added/removed line counts, plus the totals and the size of the full diff. The overview is small whatever the size of the change, so a node can decide what to open before it opens anything.
- **FR-2 (The base is stated)** — Every such node is told the exact commit the task's change is measured from (a full SHA, not a branch name), so that inspecting the change by any means covers the same scope the core uses.
- **FR-3 (The full diff stays reachable)** — The full redacted diff remains available to every node that receives the overview, by path, exactly as today. Nothing that reads it now — a role prompt naming `{diff_path}`, a `tool` gate reading `paths.diff_path` — loses it.
- **FR-4 (Capability decides the guidance, not the content)** — A node with a shell is guided to start from the overview and inspect the files it needs on its own, against the stated base; a node without a shell is guided to the full diff as its view of the change. Both receive the same facts (FR-1, FR-2, FR-3); only the instruction about which to start from differs. When the router cannot tell whether a node will have a shell, the node is treated as having **none**, so it is never told to run a command it may not be able to run.
- **FR-5 (One definition of "the change")** — The overview, the stated base and the full diff describe the same change: the same base, the same set of paths (untracked files included, the runtime home excluded), taken from the same moment. An agent that follows the guidance cannot arrive at a different scope than the dangerous-diff guard, the PR body and the failure report.
- **FR-6 (Fresh at the start of the node)** — The overview a node receives describes the working tree as it is when that node starts, not as it was after the last editing node, if anything between them (a `checks` command, a `tool` node) changed the tree.
- **FR-7 (Decomposed runs say which scope)** — In a decomposed run the hand-off makes explicit which scope it describes — the whole task since its base, the current subtask, or both — so a subtask reviewer and a whole-task documentation pass are not handed the same undifferentiated change. Which scope each packaged node uses is a decision for [questions.md](questions.md).
- **FR-8 (Packaged role prompts use it)** — The packaged `implementation` flow's `review`, `fixing` and `documentation` prompts are rewritten to work from the overview and the base, and every packaged flow's prompt that names `{diff_path}` is checked: one that should keep reading the whole change (a content critic judging the revision as a whole) says so explicitly.
- **FR-9 (Flow authors can reference it)** — A role prompt can name the overview and the base the same way it names `{diff_path}` today: as allowlisted path or metadata variables, usable in `{?name}…{/name}` blocks.

## Non-functional requirements

- **NFR-1 (Cross-platform)** — paths in the overview are repository-relative POSIX strings (`Path.as_posix()`); the overview file is written with a fixed newline; behavior is identical on Windows, Linux and macOS, including a native-Windows host where a Claude node gets no shell.
- **NFR-2 (Security envelope)** — the overview is redacted with the same secrets the full diff is redacted with, and lives in the same exchange the agent may already read. Guidance never points a node at content the current hand-off would have redacted: a path in `security.denied_read_paths` appears in the overview by name and counts only, and the guidance tells the node not to inspect it. No new capability is granted to any node — a node without a shell stays without one; no change to the flag ceiling, sandbox or tool lists, at any value of `security.strict_isolation`.
- **NFR-3 (Fail-closed determinism)** — the overview is a pure function of durable state (the same inputs the full diff is computed from), so a resumed task hands a node the same overview a fresh run would have; a failure to compute it fails the node the same way a failure to write `current.diff` does today, rather than silently handing off nothing.
- **NFR-4 (Cost / tokens)** — the overview is bounded: at most a fixed number of paths and a fixed byte size, with an explicit "N more paths not listed" line beyond that, independent of the size of the change. The footer line count per node grows by a constant. Success signal, measured with the same method as [problem.md](problem.md): on shell-enabled Claude `review` runs, the share of runs that read the full diff **and** run git drops well below today's 32/32, without a rise in reviews that miss a changed file.
- **NFR-5 (Observability)** — each node's `request.json` records the overview path and the stated base among its `context_paths`, and the prompt audit shows which guidance (shell / no shell) the node received, so a post-mortem can tell what each node was handed.
- **NFR-6 (Provider neutrality)** — whether a node has a shell is taken from the router, which asks the adapters; the core learns no provider flag or tool name. The commands the guidance names are `git` commands — which the core already owns through the Git Manager — never a provider CLI's syntax.

## Versioned surfaces touched

- **Prompt-variable allowlist** (`core/prompts.py`, the flow-derived set in `valid_prompt_vars`) — new variable(s) for the overview and the base.
- **The exchange layout** `.worc-io/<task-id>/` — one new published file beside `current.diff`.
- **The provider request** (`AgentRunRequest`, `build_context_footer`, `request.json`'s `context_paths`) — new context entries.
- **Packaged flows' role prompts** under `src/wastech_orchestrator/packaged/flows/` and the operator guide's `flows/prompt-variables.md`, `flows/roles.md`.
- **None:** the `config.yaml` schema, `state.db`, the flow YAML schema, task-file fields.

## Dependencies & assumptions

- `GitManager._diff_base` already yields the base the full diff uses; resolving it to a full SHA at hand-off time is assumed cheap and exact — to verify in `design.md`.
- `AgentRouter.route_grants_shell` already answers "does this attempt get a shell" provider-neutrally. It is **fail-closed toward "has a shell"** because it guards detection; FR-4 needs the opposite default, so the hand-off cannot reuse its answer unchanged — to settle in `design.md`.
- A route may fall back to another provider mid-node (infrastructure errors only); the hand-off is built once per request, so a fallback attempt on a provider with different shell capability is assumed to be rebuilt by the router the way `diff_path` is today for a partial diff — to verify.
- The measurement in [problem.md](problem.md) is assumed representative enough to justify the change; the success signal in NFR-4 is how that assumption gets checked after the fact.

## Priority

| Requirement | Priority |
| --- | --- |
| FR-1, FR-2, FR-3, FR-4, FR-5, FR-8 | Must |
| NFR-1, NFR-2, NFR-3, NFR-6 | Must |
| FR-7, FR-9, NFR-4, NFR-5 | Should |
| FR-6 | Could — only if something between nodes is shown to change the tree; otherwise the overview written after each editing node is already fresh |
