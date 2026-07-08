# Prompt variables reference

**You are an operator (or an agent helping one) writing a flow's role prompts for wastech-orchestrator.** A role prompt (`role_file`) is an ordinary Markdown file. The orchestrator turns it into the agent's prompt by substituting a fixed, allowlisted set of `{name}` variables — and **every variable is a path or a small piece of metadata**, never a task body, diff, check log, environment value, or secret. Those large/sensitive things stay in the artifact files the agent opens by path; the renderer only ever hands the agent a pointer.

You do **not** declare variables anywhere. The orchestrator populates the whole allowlisted set for every node; you only choose which ones to reference. A name outside the allowlist is left in the prompt **verbatim** (so literal `{...}` braces in code or JSON survive) — which means a typo like `{plna_path}` silently ships as placeholder text. `preflight` runs an anti-drift lint that **warns** (naming the file and token) about any `{name}` that would render verbatim; it is a warning, not a failure, because a verbatim render is the safe fallback.

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
| `{checks_path}` | agent, evaluator | the `checks` node has not run yet |
| `{review_path}` | agent, evaluator | the `evaluator` (review) node has not run yet |
| `{skills_path}` | agent | the node has no resolved skills |
| `{memory_path}` | agent, evaluator | memory is disabled, or nothing relevant was retrieved |
| `{subtask_order}` | agent | the task is **not** decomposed (whole-task run) |
| `{subtask_count}` | agent | the task is **not** decomposed |
| `{subtask_spec_path}` | agent | the task is **not** decomposed |
| `{predecessor_context}` | agent | not a decompose subtask, the subtask has no `depends_on` predecessor, or the node does not reference it |

`{predecessor_context}` is the path to the intra-task **subtask handoff brief** — a deterministic factual floor (each predecessor subtask's changed files, commit, acceptance criteria, spec pointer) plus, when the supervisor is available, a three-section interpretive brief (new surface area / locked decisions / open edges). It is injected into a decompose region's `implementation` node for a subtask that has `depends_on` predecessors; wrap it in `{?predecessor_context}…{/predecessor_context}`.

("agent / evaluator / supervisor" is the node kind whose prompt receives the value. The supervisor is the constant oversight layer above the flow, not a node.)

## Optional variables: the `{?name}…{/name}` conditional block

The renderer supports a conditional block that keeps its body **only when the variable is present and non-empty**, and drops the whole block (markers included) otherwise. Use it to wrap any clause that mentions a may-be-empty variable, so a missing value never leaves a dangling fragment:

```text
{?plan_path}Base the work on the plan at {plan_path}.{/plan_path}
```

- Present → `Base the work on the plan at .worc/logs/<id>/plan.md.`
- Empty → the whole sentence disappears (no `Base the work on the plan at .`).

Wrap the **entire clause**, not just the token. This is the sanctioned pattern for every optional variable — `{?memory_path}`, `{?subtask_spec_path}`, and the like. An always-present variable (`{task_id}`, `{repo}`, `{stage}`) does not need a block.

A block whose name is not an allowlisted variable, or an unbalanced `{?a}…{/b}`, is left verbatim like any unknown token (and the lint warns).

## Node outputs: `{<node_id>_path}`

Every **agent** node's output is automatically persisted to `<node_id>.out.md`, and every **`tool`** node's redacted stdout to `tools/<node_id>/stdout.txt`, each exposed to later nodes as `{<node_id>_path}` — a **path** to that file (never the inlined content). This is how you chain your own nodes: a node reads what an upstream node produced by naming it. No declaration, no config — the channel is derived from the node id.

```text
{?analyze_path}Base the implementation on the analysis at {analyze_path}.{/analyze_path}
```

- **Fan-in is free.** A node that runs after `scan` and `analyze` references both `{scan_path}` and `{analyze_path}`. A diamond (`build ← [analyze, scan]`) reads `{analyze_path}` and `{scan_path}`.
- **Empty until produced.** `{<id>_path}` is empty until that node has run, so a cross-branch or forward reference must be wrapped in `{?name}…{/name}` (as above) — otherwise it renders as an empty string.

**Allowed:**

- Reference any agent **or `tool`** node in the flow by its id: `{scan_path}`, `{static-scan_path}`, `{md-check_path}` (ids may contain letters, digits, `-`, `_`).
- Need several outputs? **Split into several nodes** — one node, one output.

**Not allowed:**

- A node id that collides with a reserved core-variable prefix (`task`, `plan`, `diff`, `checks`, `review`, `repo`, `skills`, `memory`, `stage`, or anything starting with `subtask`) — a fatal flow-load error, because `{plan_path}` etc. already mean the core variable.
- `{X_path}` where `X` names no node in the flow — it renders verbatim (and the lint warns).
- Expecting **two** named outputs from one node — a node exposes exactly one `{<id>_path}`. A node that fills a special slot (`plan` / `summary` / `enriched_spec` via `output_artifact`) uses that slot's variable (`{plan_path}` …) as its channel and writes **no** `{<id>_path}`.
- `{<id>_path}` from an **evaluator / checks / human** node — only agent and `tool` nodes expose it; those kinds keep their dedicated `{review_path}` / `{checks_path}`.

## What the renderer will never do

- It never substitutes a task body, a diff, a check log, an environment value, or a secret — only the paths/metadata above. Read large or sensitive content from the artifact file the path points to.
- It never lets a prompt weaken the sandbox, the argv, the environment allowlist, denied commands/reads, or the fallback policy. A prompt is prompt text only.

See also: the orchestrator repo's `docs/configuration.md` and `docs/functional/blocks/B15-prompt-templates.md` (the contributor-facing view of this same allowlist), and `README.md` in this folder for authoring a whole flow.
