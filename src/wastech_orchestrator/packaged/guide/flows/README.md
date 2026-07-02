# Authoring custom flows for wastech-orchestrator

**You are an operator (or an agent helping one) authoring a flow for wastech-orchestrator.** A _flow_ is the pipeline a task runs through, written as data: a validated graph of typed nodes (`agent`, `evaluator`, `checks`, `publish`) joined by outcome-labelled edges. A task's `task_type` selects its flow. This folder is a self-contained quickstart; the full reference is `docs/flow-authoring.md` and `docs/configuration.md` in the orchestrator's repository.

If you only want to change _what a step says_, you do not need a new flow — edit that node's `role_file` prompt in the delivered copy under `.worc/flows/`. Author a flow only when you need different steps, a different output kind, or a different route.

## Where flows live

- **Dispatch file:** `<repo>/.worc/flows/<task_type>.yaml`. The file stem is the `task_type`. A file here adds a new `task_type` or overrides a packaged built-in of the same name.
- **Prompts:** each flow **owns its prompts** in a sibling folder named after the `task_type` — `.worc/flows/<task_type>/*.md`. `role_file` values in the YAML are relative to `.worc/flows/` and point into that folder (e.g. `role_file: my_flow/implement.md`).
- **Shared supervisor prompt:** `.worc/flows/roles/supervisor.md` stays shared across every flow (the supervisor is a constant layer above all flows, not a node of one).

`install` seeds editable, active copies of the three built-ins (`implementation`, `deep_research`, `security_audit`); the operator layer shadows the packaged one, so those copies are already yours to edit.

## Minimal custom flow

Save as `.worc/flows/my_flow.yaml`, with prompts under `.worc/flows/my_flow/`:

```yaml
flow:
  name: my_flow
  task_type: my_flow # must equal the file stem
  permission_ceiling: workspace-write # hard cap; no node may exceed it, no task may widen it
  output_policy: code_change # code_change | repository_document | private_control_workspace_report
  publishing: pull_request # pull_request | documentation_pull_request | none

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
      lineage_affinity: implement
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
    test_fix: 5 # every fail/rework loop must be bounded
```

`my_flow/implement.md` is an ordinary Markdown prompt; it may use only allowlisted path variables like `{task_path}`, `{repo_path}`, `{plan_path}`, `{diff_path}` (never task bodies, diffs, env, or secrets). See `prompt-variables.md` in this folder for the full list, which runner populates each, and the `{?name}…{/name}` optional-variable syntax.

## Chaining node outputs (`{<node_id>_path}`)

Every **agent** node's output is persisted and exposed to later nodes as `{<node_id>_path}` — a path to that node's `<id>.out.md`, never the inlined content. That is how a multi-step flow hands one node's result to the next by name, with no extra config:

```yaml
nodes:
  - id: scan
    kind: agent
    role_file: my_flow/scan.md # writes scan.out.md → {scan_path}
  - id: analyze
    kind: agent
    role_file: my_flow/analyze.md # references {?scan_path}…{/scan_path}
  - id: build
    kind: agent
    role_file: my_flow/build.md # references {scan_path} and {analyze_path} (fan-in)
    session_scope: editing_lineage
    permission_profile: workspace-write
edges:
  - { from: scan, to: analyze }
  - { from: analyze, to: build }
```

In `my_flow/build.md`:

```text
Implement the change.{?analyze_path} Follow the analysis at {analyze_path}.{/analyze_path}{?scan_path} The raw scan is at {scan_path}.{/scan_path}
```

One node exposes exactly one output — to publish several results, split into several nodes. A node id may not collide with a reserved core-variable prefix (`task`, `plan`, `diff`, `checks`, `review`, `repo`, `skills`, `memory`, `stage`, `subtask*`); that is a fatal load error.

## Register, run, validate

- **Register:** the file _is_ the registration. A task selects it with front matter `task_type: my_flow`. An unknown `task_type` fails the task before any branch is created.
- **Validate:** run `wastech-orchestrator --config ./.worc/config.yaml preflight`. Every flow is loaded and validated (graph integrity, security ceiling, config consistency); a failure reports `NOT ready` with a one-line reason and blocks the run.
- **Debug:** set `prompt_audit: true` to record the exact rendered prompt per node under `logs/<task-id>/prompt-audit/`; per-run artifacts live under `.worc/logs/<task-id>/`.

## Foot-guns

- Keep `permission_ceiling` as low as the flow needs; grant `workspace-write` only to nodes that edit. A task can never widen it.
- Put every `role_file` inside the flow's own `<task_type>/` folder; relative paths only (no `..`). Do not point at `roles/` — that is the shared supervisor layer.
- Bound every `fail`/`rework` loop with a `budget`; exactly one entry node (no incoming edges); every node must reach a terminal.
- Network is off by default; declare `network_policy` for a flow-wide grant or `network_access: true` on one node. A Codex `workspace-write` node with network is rejected — split external fetches into a `read-only` node.

For the complete contract (node fields, per-node provider/model/reasoning overrides, the prompt-variable allowlist, and the validation layers), see `docs/flow-authoring.md` and `docs/configuration.md`.
