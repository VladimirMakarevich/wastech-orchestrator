# Authoring custom flows for wastech-orchestrator

**You are an operator (or an agent helping one) authoring a flow for wastech-orchestrator.** A _flow_ is the pipeline a task runs through, written as data: a validated graph of typed nodes (`agent`, `evaluator`, `checks`, `publish`) joined by outcome-labelled edges. A task's `task_type` selects its flow. This folder is a self-contained quickstart; the full reference is `docs/flow-authoring.md` and `docs/configuration.md` in the orchestrator's repository.

If you only want to change _what a step says_, you do not need a new flow — edit that node's `role_file` prompt in the delivered copy under `.worc/flows/`. Author a flow only when you need different steps, a different output kind, or a different route.

## Where flows live

- **Dispatch file:** `<repo>/.worc/flows/<task_type>.yaml`. The file stem is the `task_type`. A file here adds a new `task_type` or overrides a packaged built-in of the same name.
- **Prompts:** each flow **owns its prompts** in a sibling folder named after the `task_type` — `.worc/flows/<task_type>/*.md`. `role_file` values in the YAML are relative to `.worc/flows/` and point into that folder (e.g. `role_file: my_flow/implement.md`).
- **Supervisor prompts:** the supervisor is a constant layer above all flows (not a node). Its global default observe lens is `.worc/flows/roles/supervisor.md`, but a flow may own its supervisor wording with a `supervisor:` block (see "Flow-local supervisor prompts" below); a flow with no such block uses the shared `roles/supervisor.md`.

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

## What a node returns (contracts, slots, and custom schemas)

Every `agent` and `evaluator` node returns a **typed structured result**, not free text — and for the built-in node kinds you do not write the schema; it is selected automatically:

- **plain `agent`** — no schema; the output is its final message (still exposed as `{<id>_path}` above).
- **`agent` with `hitl:`** — must return `content` plus an optional question/approval object.
- **`agent` named by `decomposition.proposed_by`** (the planning node) — `content` + optional `human_input` + `decompose` + `subtasks`.
- **`evaluator`** — a findings verdict `{ findings: [ { severity, path, what, fix } ] }`, and it is **fail-closed**: a missing or malformed findings result routes the task to `manual_action_required` instead of a silent `accept`. So an evaluator `role_file` must actually emit the findings result — a prose-only "looks good" review hard-stops the task.

The core re-validates every typed result itself, so a malformed result fails the node; you cannot loosen this from a flow.

### Named output slots (`output_artifact`)

Besides the generic `{<id>_path}` channel above, an agent node can fill one of three fixed slots with `output_artifact:`, landing its `content` in a well-known file that later nodes read by a stable variable:

- `output_artifact: enriched_spec` → writes `task.enriched.md` (audit only; no downstream variable).
- `output_artifact: plan` → writes `plan.md`, read downstream as `{plan_path}`.
- `output_artifact: summary` → writes `summary.md` as `{summary_body_path}` (normally the supervisor layer fills this, not a flow node).

The slot vocabulary is fixed to these three; a flow only chooses which node fills each, and one node fills at most one slot.

### Overriding the schema (the one real foot-gun)

An `agent` node may set an inline `output_schema:` to return data of your own shape. If you do, **every object in the schema — the top level and every nested object — must set `additionalProperties: false`.** Codex enforces `--output-schema` through OpenAI Structured Outputs, which rejects a non-strict schema with a hard **400** and fails the node on every run (this exact mistake once broke the built-in review node). Claude tolerates a loose schema, but write it strict so the same flow runs on both providers. Keep a string `content` field if the node also fills a slot, and prefer the built-in contract unless you genuinely need a custom shape.

## Flow-local supervisor prompts

The supervisor is a constant read-only layer above the flow — it observes each step and writes the final summary (the PR body). A flow can reshape **its wording** (never the machine contract) with an optional `supervisor:` block:

```yaml
supervisor:
  role_file: my_flow/supervisor.md # observe lens; fallback: flow -> config.supervisor.role_file -> built-in
  finalize_role_file: my_flow/summary.md # finalize lens; fallback: flow -> built-in (no global one)
  handoff_role_file: my_flow/handoff.md # subtask handoff-brief lens (decompose flows); fallback: flow -> built-in
  emit_follow_ups: true # opt this flow's finalize into the technical-debt / follow-ups signal
```

- All three prompt files are resolved inside the flow dir (relative paths, no `..`); a traversing path fails validation.
- Supervisor prompts receive only `{task_id}`, `{repo}` / `{repo_path}` — no node/path variables. The `preflight` anti-drift lint scans them too, so a mistaken `{plan_path}` (or any other `{name}` the supervisor never fills) is flagged as rendering verbatim.
- **`handoff_role_file`** is only used by decompose flows: at each subtask boundary the supervisor writes an interpretive handoff brief for the next subtask, injected as `{predecessor_context}` into the region's `implementation` node (see `prompt-variables.md`). A deterministic factual floor (changed files, commit, acceptance criteria, spec pointer) is always present; the interpretive brief rides the supervisor's warm session (no extra turn budget) and is best-effort.
- **`emit_follow_ups`** (default `false`) is a **code-oriented** capability: when on, the supervisor's existing finalize turn (no extra LLM call) also emits an **evidence-gated** `follow_ups` array — technical debt / refactor candidates it saw, each with `title` / `rationale` / `paths` / `evidence` / `severity` / `action_hint` — written into `summary.json` and a "Technical debt / follow-ups" section of `summary.md`. Set it on a code flow (the packaged `implementation` flow does); leave it off for research / prose flows (never ask them to invent "refactor candidates").
- Only the **wording** moves into files. The structured-output schemas (`follow_ups`, and the memory delta) stay in the orchestrator, so your prompt can change tone and emphasis but can never break what the orchestrator parses.

## Register, run, validate

- **Register:** the file _is_ the registration. A task selects it with front matter `task_type: my_flow`. An unknown `task_type` fails the task before any branch is created.
- **Validate:** run `wastech-orchestrator --config ./.worc/config.yaml preflight`. Every flow is loaded and validated (graph integrity, security ceiling, config consistency); a failure reports `NOT ready` with a one-line reason and blocks the run.
- **Debug:** set `prompt_audit: true` to record the exact rendered prompt per node under `logs/<task-id>/prompt-audit/`; per-run artifacts live under `.worc/logs/<task-id>/`.

## Foot-guns

- Keep `permission_ceiling` as low as the flow needs; grant `workspace-write` only to nodes that edit. A task can never widen it.
- Put every node `role_file` inside the flow's own `<task_type>/` folder; relative paths only (no `..`). `roles/supervisor.md` is the shared **global** supervisor default; to give this flow its own supervisor wording, use the `supervisor:` block (above) pointing into your own folder, not a node `role_file`.
- Bound every `fail`/`rework` loop with a `budget`; exactly one entry node (no incoming edges); every node must reach a terminal.
- Network is off by default; declare `network_policy` for a flow-wide grant or `network_access: true` on one node. A Codex `workspace-write` node with network is rejected — split external fetches into a `read-only` node.
- If you set a custom `output_schema`, make every object in it `additionalProperties: false` — Codex rejects a non-strict schema with a 400 and the node fails every run. Prefer the built-in contract; an `evaluator` prompt must emit the findings result or the task fail-closes to manual.

For the complete contract (node fields, per-node provider/model/reasoning overrides, the prompt-variable allowlist, and the validation layers), see `docs/flow-authoring.md` and `docs/configuration.md`.
