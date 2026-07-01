# Flow authoring

A **flow** is the pipeline a task runs through, expressed as data: a validated graph of typed nodes (`agent`, `evaluator`, `checks`, `publish`) connected by outcome-labelled edges. A task's `task_type` selects its flow. The orchestrator ships three built-in flows — `implementation` (the default), `deep_research`, and `security_audit` — and an operator can add or override flows without touching Python. This guide shows how to author, register, validate, and debug a custom flow from scratch.

If you only want to change _what a step says_ (not the graph), you do not need a new flow — edit the node's `role_file` prompt in the delivered copy (see [Customize a node's prompt](cookbook.md#7a-customize-a-nodes-prompt)). Author a new flow only when you need a different set of steps, a different output kind, or a different route.

## Where flows live

Flows resolve in two layers, **operator-first**:

1. **Operator flows** — `<repo>/.worc/flows/<task_type>.yaml`. Drop a YAML file here to add a new `task_type` or to **override** a packaged flow of the same name. Each flow **owns its prompts** in a sibling folder named after the `task_type`: `.worc/flows/<task_type>/*.md`. So `role_file` paths in the YAML are written relative to `.worc/flows/` and point into that folder (e.g. `role_file: my_flow/implement.md`).
2. **Packaged built-ins** — shipped inside the package and copied into `.worc/flows/` by `install` as editable, active copies. The operator layer shadows the packaged layer, so the seeded copies of `implementation`/`deep_research`/`security_audit` are already operator flows you can edit.

The dispatch file stays flat (`.worc/flows/<task_type>.yaml`) so the registry finds it by name; only the prompts live in the per-flow subfolder. The shared supervisor prompt is the one exception — it stays at `.worc/flows/roles/supervisor.md` because the supervisor is a constant layer above _every_ flow, not a node of any one flow.

```text
.worc/flows/
├── my_flow.yaml            # dispatch file (task_type: my_flow)
├── my_flow/                # this flow owns its prompts
│   ├── implement.md
│   └── fixing.md
└── roles/
    └── supervisor.md       # shared supervisor layer (all flows)
```

## A minimal custom flow

This is a complete, valid coding flow: implement → test → publish, with a bounded test-fix loop. Save it as `.worc/flows/my_flow.yaml` and put its two prompts under `.worc/flows/my_flow/`.

```yaml
flow:
  name: my_flow
  task_type: my_flow # must match the file stem; a task selects it with `task_type: my_flow`
  permission_ceiling: workspace-write # the most any node here may do on disk
  output_policy: code_change # what the flow is allowed to produce
  publishing: pull_request # how the result is published

  nodes:
    - id: implement
      kind: agent
      role_file: my_flow/implement.md # relative to .worc/flows/, into this flow's own folder
      session_scope: editing_lineage
      permission_profile: workspace-write
    - id: testing
      kind: checks
      checker: command_profile # runs the repo's configured check command sets
    - id: fixing
      kind: agent
      role_file: my_flow/fixing.md
      session_scope: editing_lineage
      lineage_affinity: implement # resume the implement agent's session
      permission_profile: workspace-write
    - id: publish
      kind: publish
      policy: pull_request

  edges:
    - { from: implement, to: testing }
    - { from: testing, to: publish, outcome: pass }
    - { from: testing, to: fixing, outcome: fail, loop: test_fix }
    - { from: fixing, to: testing }

  budgets:
    test_fix: 5 # the fail loop is bounded; without a budget the flow fails validation
```

The two role files are ordinary Markdown prompts. `implement.md` might be as small as:

```markdown
Implement the task at {task_path} in the repository {repo_path}. Follow the plan at {plan_path} if present. Keep the change focused.
```

## Flow header fields

| Field | Required | Values / meaning |
| --- | --- | --- |
| `name` | yes | Flow name (match the `task_type`). |
| `task_type` | yes | The dispatch key. Must equal the file stem (`<task_type>.yaml`); a task selects the flow by setting this value. |
| `permission_ceiling` | yes | `read-only` or `workspace-write` — the hard cap; no node may exceed it, and it can never be widened by a task. |
| `output_policy` | yes | What the flow may produce: `code_change`, `repository_document`, or `private_control_workspace_report`. |
| `publishing` | yes | `pull_request`, `documentation_pull_request`, or `none` (a graph-terminal that touches no Git). |
| `network_policy` | no | `advisories` / `research` grants the flow network flow-wide; omit and every node is offline by default (nodes may still opt in with `network_access: true`). |
| `budgets` | if loops | Bounds each named loop / rework edge (e.g. `test_fix: 5`). Every `fail`/`rework` edge must be bounded or validation fails. |

## Node kinds

- **`agent`** — launches a coding-agent CLI (Codex or Claude Code) with the node's `role_file` as its prompt. Fields: `role_file` (required), `session_scope` (`fresh_disposable` | `editing_lineage` | `resume_own_lineage`), `permission_profile` (≤ the flow ceiling), optional `output_artifact`, `hitl`, `when`, `lineage_affinity`, and the per-node overrides below.
- **`evaluator`** — a read-only judge that returns `accept` / `rework`. Fields: `role` (e.g. `review`), `role_file`, `blocking` (a failing verdict blocks vs is advisory), `max_rework_per_stage`. Evaluators are forced `read-only`.
- **`checks`** — runs deterministic repository commands, no agent. `checker: command_profile` runs the configured check command sets; other checkers exist (`citation`, `dependency_scan`). Outcomes: `pass` / `fail`.
- **`publish`** — the terminal. `policy: pull_request` / `documentation_pull_request` opens a PR; `policy: none` is a graph terminal that performs no Git action (the orchestrator still owns any real commit/push/PR).

Nodes never pick the next node or commit anything — the engine routes on edge outcomes, and only the orchestrator does Git.

## Edges, outcomes, and loops

Each edge is `{ from, to, outcome? }`. A `checks` node emits `pass`/`fail`; an `evaluator` emits `accept`/`rework`; a plain `agent` edge needs no `outcome`. Any `fail`/`rework` edge that loops back must carry a `loop:` name (or a `budget:`) and be bounded in `budgets:`. Exactly **one** entry node (no incoming edges) is allowed, and every node must be able to reach a terminal — the validator enforces both.

## Role files (prompts)

A node's prompt is the content of its `role_file`. Role files render only an allowlisted set of path/metadata variables — `{task_path}`, `{repo_path}`, `{plan_path}`, `{diff_path}`, `{review_path}`, `{skills_path}`, `{memory_path}`, `{subtask_order}`/`{subtask_count}`/`{subtask_spec_path}`, and a few more — never task bodies, diffs, env, or secrets. A variable that is empty for a given node renders as the empty string; wrap optional references in a conditional block `{?name}…{/name}` so they drop cleanly when empty. For the full variable contract and which runner populates each, see [configuration.md → Prompt templates](configuration.md#prompt-templates-no-longer-a-config-block) and the functional block [B15](functional/blocks/B15-prompt-templates.md).

`role_file` paths are contained to the flow directory: a path with `..` or an absolute path is rejected at load. Keep prompts inside your `<task_type>/` folder.

## Per-node overrides

Every `agent`/`evaluator` node may pin its own `provider` (`codex` | `claude`), `model`, and `reasoning`; omit any and the node inherits the `config.yaml` provider defaults (`provider` ⇒ the global primary). A node may also set `network_access: true|false` to override the flow-wide network default for that node alone. Spend more reasoning where rework is decided (review), less on mechanical steps. See [configuration.md → Per-node overrides in flows](configuration.md#per-node-overrides-in-flows).

## Registering and running the flow

Registration is implicit: the file **is** the registration. Put `my_flow.yaml` under `.worc/flows/`, and a task selects it with front matter:

```markdown
---
id: task-123
title: "Do the thing my_flow does"
task_type: my_flow
---
```

An unknown `task_type` (no matching flow file) fails the task at flow resolution before any branch is created. A task only _names_ the flow — it can never edit the graph (the one exception is disabling a node per task with `nodes.<id>.enabled: false`).

## Validation catches flow errors before any task runs

Every flow file — packaged and operator — is loaded and validated at `install` and at `preflight`; any failure makes `preflight` report `NOT ready` and blocks the run. Three layers run:

- **Graph integrity** — edges resolve, outcomes are valid per node kind, every `fail`/`rework` edge is bounded, exactly one entry node, every node reaches a terminal.
- **Security ceiling** — no node's `permission_profile` exceeds the flow `permission_ceiling`; evaluators are forced read-only; `role_file` paths contain no traversal; unknown fields fail closed.
- **Config consistency** — a pinned `provider` is in `agents.allowed`, its `reasoning` is supported by that provider, and (under `security.strict_isolation`) no `extra_args` selects a full-access sandbox mode.

Run it explicitly with:

```bash
wastech-orchestrator --config ./.worc/config.yaml preflight
```

## Inspecting rendered prompts and artifacts

- Set `prompt_audit: true` (config-wide or per task) to record **who** received **what prompt** per node run under `logs/<task-id>/prompt-audit/`. Compare the rendered prompt against your role file when an edit "did nothing" — you may be editing a different copy than the one the node resolves.
- Per-run artifacts (plan, diff, check logs, review findings, `summary.json`) live under `.worc/logs/<task-id>/`. Node execution and status live in `state.db` (inspect with `status`).

## Best practices and foot-guns

- **Keep the ceiling as low as the flow needs.** Set `permission_ceiling: read-only` for an advisory flow; grant `workspace-write` only to the nodes that must edit. A task can never widen the ceiling — get it right in the flow.
- **`role_file` discipline.** Put every prompt inside the flow's own `<task_type>/` folder; use relative paths only. Do not point at `roles/` (that folder is the shared supervisor layer, not your flow's prompts).
- **Bound every loop.** Any `fail`/`rework` edge that loops needs a `budget`; an unbounded loop fails validation.
- **Network is off by default.** Declare `network_policy` for a flow-wide grant, or `network_access: true` on the single node that needs it. Codex `workspace-write` + network is rejected — split external fetches into a `read-only` node.
- **One entry, all reachable.** Design the graph so exactly one node has no incoming edge and every node can reach a `publish` terminal.
- **Prompt variables are paths only.** Never expect task bodies, diffs, or secrets in a prompt — only the allowlisted path/metadata variables are substituted.
- **Validate before you rely on it.** Run `preflight` after every flow edit; it fails closed with a one-line reason.

## See also

- [Configuration → Flows](configuration.md#flows-task_type-dispatch-and-operator-flows) — the flow/config split and the full validation contract.
- [Cookbook → Customize a node's prompt](cookbook.md#7a-customize-a-nodes-prompt) — editing a prompt without a new flow.
- [Task authoring](task-authoring.md) — how a task selects a flow via `task_type`.
- Functional map: [B29 flow definition & validation](functional/blocks/B29-flow-definition-and-validation.md), [B30 flow node runners](functional/blocks/B30-flow-node-runners.md).
