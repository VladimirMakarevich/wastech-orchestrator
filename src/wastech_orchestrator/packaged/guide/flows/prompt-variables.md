# Prompt variables reference

**You are an operator (or an agent helping one) writing a flow's role prompts for wastech-orchestrator.** A role prompt (`role_file`) is an ordinary Markdown file. The orchestrator turns it into the agent's prompt by substituting a fixed, allowlisted set of `{name}` variables — and **every variable is a path or a small piece of metadata**, never a task body, diff, check log, environment value, or secret. Those large/sensitive things stay in the artifact files the agent opens by path; the renderer only ever hands the agent a pointer.

You do **not** declare variables anywhere. The orchestrator populates the whole allowlisted set for every node; you only choose which ones to reference. A name outside the allowlist is left in the prompt **verbatim** (so literal `{...}` braces in code or JSON survive) — which means a typo like `{plna_path}` silently ships as placeholder text. `worc validate-flow` runs an anti-drift lint that **warns** (naming the file and token) about any `{name}` that would render verbatim; it is a warning, not a failure, because a verbatim render is the safe fallback.

## The variables

Each variable names **which runner populates it** and **when it may be empty**. An empty variable renders as the empty string, so wrap any optional one in a conditional block (below) rather than inlining a bare reference that can dangle.

| Variable | Populated by | May be empty when |
| --- | --- | --- |
| `{task_id}` | agent, evaluator, supervisor | never (always present) |
| `{stage}` | agent, evaluator | never — the current node's id |
| `{repo_path}` / `{repo}` | agent, evaluator, supervisor | never — the repository clone directory (`{repo}` is an alias) |
| `{task_path}` | agent, evaluator | no on-disk task file exists (rare) |
| `{plan_path}` | agent, evaluator | planning has not run yet, or the node that fills the `plan` output slot is disabled |
| `{diff_path}` | agent, evaluator | no workspace-write edit has happened yet |
| `{checks_path}` | agent, evaluator | the `checks` node has not run yet (it publishes on both outcomes — see `reference.md`) |
| `{review_path}` | agent, evaluator | the `evaluator` (review) node has not run yet |
| `{memory_path}` | agent, evaluator | memory is disabled, or nothing relevant was retrieved |
| `{subtask_order}` | agent | the task is **not** decomposed (whole-task run) |
| `{subtask_count}` | agent | the task is **not** decomposed |
| `{subtask_spec_path}` | agent | the task is **not** decomposed |
| `{predecessor_context}` | agent | not a decompose subtask, the subtask has no `depends_on` predecessor, or the node does not reference it |

`{predecessor_context}` is the path to the intra-task **subtask handoff brief** — a deterministic factual floor (each predecessor subtask's changed files, commit, acceptance criteria, spec pointer) plus, when the supervisor is available, a three-section interpretive brief (new surface area / locked decisions / open edges). It is available to any agent node running inside a decompose region for a subtask that has `depends_on` predecessors — and, like `{memory_path}`, only when that node's own role prompt references it (the packaged `implementation` flow reads it from its `implementation` node); wrap it in `{?predecessor_context}…{/predecessor_context}`.

("agent / evaluator / supervisor" is the node kind whose prompt receives the value. The supervisor is the constant oversight layer above the flow, not a node.)

**Put an optional section's heading inside its own block.** A heading that sits _outside_ the `{?name}…{/name}` block it introduces cannot know whether the section is empty, so it renders with nothing under it. Give each optional item its own heading within its own block, and fold the leading blank line in as well:

```text
{?memory_path}

## Repository Memory

A brief of repository memory for this task is at {memory_path}.{/memory_path}
```

The heading then appears exactly when its content does — never orphaned, never empty. The packaged `implementation` / `planning` / `fixing` / `review` roles follow this for their memory / subtask / predecessor items. Resist the inverse shape — one shared heading for several optional items, guarded by some separate flag — because blocks do not nest and there is no "any-of-these-variables" form, so nothing can tell that heading which of your variables the section actually holds.

## Optional variables: the `{?name}…{/name}` conditional block

The renderer supports a conditional block that keeps its body **only when the variable is present and non-empty**, and drops the whole block (markers included) otherwise. Use it to wrap any clause that mentions a may-be-empty variable, so a missing value never leaves a dangling fragment:

```text
{?plan_path}Base the work on the plan at {plan_path}.{/plan_path}
```

- Present → `Base the work on the plan at .worc-io/<task-id>/plan.md.`
- Empty → the whole sentence disappears (no `Base the work on the plan at .`).

Wrap the **entire clause**, not just the token. This is the sanctioned pattern for every optional variable — `{?memory_path}`, `{?subtask_spec_path}`, and the like. An always-present variable (`{task_id}`, `{repo}`, `{stage}`) does not need a block.

A block whose name is not an allowlisted variable is left verbatim like any unknown token, and the lint warns. An unbalanced `{?a}…{/b}` is left verbatim too, but the lint stays **silent** about it — neither marker matches the token shape it scans — so a mismatched closing tag ships to the agent unannounced.

## Node outputs: `{<node_id>_path}`

Every **agent** node's output is automatically persisted (as `<node_id>.out.md`), and every **`tool`** node's redacted stdout too, under that run's `.worc/logs/<task-id>/stages/<node_id>/run-<NNNNNN>/` directory. Each is exposed to later nodes as `{<node_id>_path}` — a **path** to the redacted copy published in the agent-facing exchange (`.worc-io/<task-id>/stages/<node_id>/run-<NNNNNN>/`), never the inlined content; the `.worc/` original stays the private audit record. This is how you chain your own nodes: a node reads what an upstream node produced by naming it. No declaration, no config — the channel is derived from the node id. When a node re-runs (a fix loop), every pass is kept and `{<node_id>_path}` resolves to the **latest** run.

For a node whose real product is a **file it writes**, what the channel carries is its own choice: by default the node's closing message, or — when the node declares `output_file:` in the flow — a redacted copy of that file. A node that writes a document and then describes it in one paragraph otherwise hands the next node the paragraph, which is the smaller half. Either way it is still a **path**, and the downstream prompt is unchanged (`{synthesis_path}` is `{synthesis_path}`).

Both `agent` and `evaluator` role prompts resolve these names, so an evaluator can judge an upstream node's **work** and not only the file some later node wrote from it — that is how a coverage gate grades the analysis passes it sits behind. Only `agent` and `tool` nodes _produce_ the channel (below).

```text
{?analyze_path}Base the implementation on the analysis at {analyze_path}.{/analyze_path}
```

- **Fan-in is free.** A node that runs after `scan` and `analyze` references both `{scan_path}` and `{analyze_path}`. A diamond (`build ← [analyze, scan]`) reads `{analyze_path}` and `{scan_path}`.
- **Empty until produced.** `{<id>_path}` is empty until that node has run, so a cross-branch or forward reference must be wrapped in `{?name}…{/name}` (as above) — otherwise it renders as an empty string.

**Allowed:**

- Reference any agent **or `tool`** node in the flow by its id: `{scan_path}`, `{static-scan_path}`, `{md-check_path}` (ids are lowercase only — `a-z`, digits, `-`, `_` — and start with a letter or digit).
- Need several outputs? **Split into several nodes** — one node, one output.

**Not allowed:**

- A node id that collides with a reserved core-variable prefix (`task`, `plan`, `diff`, `checks`, `review`, `repo`, `memory`, `stage`, or anything starting with `subtask`) — a fatal flow-load error, because `{plan_path}` etc. already mean the core variable.
- `{X_path}` where `X` names no node in the flow — it renders verbatim (and the lint warns).
- Expecting **two** named outputs from one node — a node exposes exactly one `{<id>_path}`. A node that fills a fixed slot with `output_artifact` (`enriched_spec` / `plan` / `summary` / `report`) writes **no** `{<id>_path}` at all: that slot is its channel. Only `plan` has a prompt variable (`{plan_path}`); the other three are orchestrator/audit outputs no downstream prompt can name.
- `{<id>_path}` **of** an `evaluator` / `checks` / `hitl` / `publish` node — only agent and `tool` nodes _expose_ one; an evaluator and a checks node publish through their dedicated `{review_path}` / `{checks_path}` instead. (An evaluator prompt may freely _read_ an agent's or tool's `{<id>_path}`.)

## What the renderer will never do

- It never substitutes a task body, a diff, a check log, an environment value, or a secret — only the paths/metadata above. Read large or sensitive content from the artifact file the path points to.
- It never lets a prompt weaken the sandbox, the argv, the environment allowlist, denied commands/reads, or the fallback policy. A prompt is prompt text only.

See also: [README.md](README.md) in this folder for authoring a whole flow, and [roles.md](roles.md) for the prompts these variables are substituted into.
