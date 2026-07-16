# Flows — complete field reference

**You are an operator (or an agent helping one) authoring a flow for wastech-orchestrator.** This is the complete, self-contained reference for every flow-level and node-level field — allowed values, defaults, constraints, and when to use each. You do not need the internet or the repo's own `docs/` to author a valid flow. For the _how-to_ quickstart (where flows live, a minimal example, chaining, foot-guns) see [README.md](README.md); for writing the node prompts see [roles.md](roles.md) and the `{name}` variable list in [prompt-variables.md](prompt-variables.md).

A flow is a validated graph of typed nodes joined by outcome-labelled edges, stored as `<repo>/.worc/flows/<task_type>.yaml`. Every mapping is a **fail-closed allowlist**: an unknown key anywhere is a fatal load error. Validate with `worc validate-flow <name>` (or `--all`).

## Flow-level fields (the `flow:` block)

| Field | Required | Type / values | Default | Constraint | Meaning |
| --- | --- | --- | --- | --- | --- |
| `name` | **yes** | string | — | — | The flow's name. |
| `task_type` | **yes** | string | — | Must equal the file stem (`<task_type>.yaml`). | The selector a task sets in front matter to run this flow. |
| `permission_ceiling` | **yes** | `read-only` \| `workspace-write` | — | A hard cap: no node's `permission_profile` may exceed it, and no task may widen it. | The maximum filesystem access any node in this flow can have. Keep it as low as the flow needs. |
| `output_policy` | **yes** | `code_change` \| `repository_document` \| `private_control_workspace_report` | — | Governs _where writing nodes may write_ and _what the flow must produce_ (see below). | The kind of deliverable this flow produces. |
| `publishing` | **yes** | `pull_request` \| `documentation_pull_request` \| `local_artifact` \| `private_control_workspace_report` \| `none` | — | The publish node's `policy` must match the flow's intent (see below). | Where the flow's output ends up (its terminal publishing behavior). |
| `network_policy` | no | `advisories` \| `research` | absent ⇒ no network | Absence = no network for any node unless a node sets `network_access: true`. | The flow-wide network grant (see below). |
| `nodes` | **yes** | list | — | Non-empty; exactly one entry node; every node reaches a terminal. | The graph's nodes. |
| `edges` | no | list | `[]` | (see Edges) | The transitions. |
| `budgets` | if loops | mapping name→int | `{}` | Every named loop referenced by an edge must be declared here. | Loop iteration caps (the engine clamps to `min(flow, config cap)`). |
| `defaults` | no | `evaluator: {...}` | none | (see below) | Field defaults applied to nodes that omit them. |
| `decomposition` | no | `{proposed_by, sub_flow, shared_budget?}` | none | (see below) | Enables one-task-many-subtasks planning. |
| `supervisor` | no | `{role_file?, finalize_role_file?, handoff_role_file?, emit_follow_ups?}` | none | Prompt paths flow-dir-contained (no `..`). | Flow-local supervisor wording + the follow-ups opt-in (see [roles.md](roles.md)). |

### `output_policy` — what each variant means

| Value | What writing nodes may write | Must produce | Enters git? | When to use |
| --- | --- | --- | --- | --- |
| `code_change` | Anywhere in the repo (the deliverable _is_ the diff, guarded by the dangerous-diff gate). | — | Yes (normal diff). | Code/implementation flows. Pairs with `publishing: pull_request`. |
| `repository_document` | **Only** `docs/research/<task_id>/`. | `report.md` + `sources.json` | Yes (committable document). | Research/analysis flows that ship a document into the repo. Pairs with `publishing: documentation_pull_request`. |
| `private_control_workspace_report` | **Only** `.worc/security-reports/<task_id>/`. | `report.md` | **No — private, fail-closed.** Any attempt to stage/commit/PR it is refused. | Sensitive reports (e.g. security audits) that must never enter git. Pairs with `publishing: none` (or `private_control_workspace_report`). |

The write confinement is enforced twice: an after-stage write guard on every workspace-write node, and again at publish. Pick `output_policy` to match what the flow actually creates — a mismatch (e.g. a research flow trying to edit `src/`) is blocked at runtime.

### `publishing` — where the output ends up

| Value | Terminal behavior | When to use |
| --- | --- | --- |
| `pull_request` | Commit → push branch → open a PR. | Code flows (`output_policy: code_change`). |
| `documentation_pull_request` | Same, for a committed document deliverable. | `repository_document` flows. |
| `local_artifact` | Commit locally only (no push, no PR). | Flows whose result stays on the local branch. |
| `private_control_workspace_report` | Keep the private report under `.worc/`; nothing enters git. | `private_control_workspace_report` flows. |
| `none` | No git/publish step at all (graph terminal only). | Advisory-only flows, or the `merge` helper flow. |

A task's `publish` front-matter field can only **downgrade** this (`min(flow_policy, task.publish)`), never escalate. The publish node's own `policy` (below) is a `PublishingPolicy` too and should agree with the flow header.

### `network_policy` — the flow-wide network grant

| Value | Meaning | When to use |
| --- | --- | --- |
| (absent) | No node has network unless it sets `network_access: true`. | The default and the safe choice. |
| `advisories` | Grants network for fetching vulnerability advisories / package metadata. | Security-audit-style flows. |
| `research` | Grants broader external-research fetches. | Deep-research flows. |

A node's `network_access` overrides this for that node alone (`true` grants even with no flow policy; `false` forces one node offline). **A Codex `workspace-write` node with network is rejected** — split external fetches into a `read-only` node.

### `defaults.evaluator`

Applied to any evaluator node that omits the field. Keys: `session_scope` (default `fresh_disposable`), `permission_profile` (default `read-only`), `max_rework_per_stage` (default `1`), `gate_severity` (default `high`). Use it to avoid repeating the same evaluator settings across many evaluator nodes (e.g. set a stricter `gate_severity` once for a content flow's critics).

### `decomposition`

| Field | Type | Constraint | Meaning |
| --- | --- | --- | --- |
| `proposed_by` | string | Must name an agent node in this flow. | The node (usually `planning`) allowed to propose a split. |
| `sub_flow` | list of node ids | Node ids that form the per-subtask region. | The sub-graph each subtask runs. |
| `shared_budget` | string \| null | A declared budget name. | A fix budget shared across subtasks (e.g. `global_fix_iterations`). |

Only meaningful together with the config gate (`agents.decomposition.enabled` / a task's `decomposition` field) — the flow declares the _capability_; the gate decides whether it fires.

## Node kinds

Every node has an `id` (unique; see reserved ids below) and a `kind`. The six kinds:

| Kind | Runs | Gates the graph by |
| --- | --- | --- |
| `agent` | An LLM coding agent (a `role_file` prompt). | Unconditional edge (produces work). |
| `evaluator` | An LLM reviewer (read-only, fail-closed findings). | `accept` / `rework`. |
| `checks` | A built-in checker. | `pass` / `fail`. |
| `tool` | Your own executable from `.worc/tools/`. | `pass` / `fail` / `route:*` (exit code or JSON). |
| `hitl` | A human-in-the-loop pause. | The human's `question`/`approval` reply. |
| `publish` | The orchestrator's commit/push/PR step. | Unconditional (terminal). |

### `agent` node

| Field | Type / values | Default | Constraint | Meaning |
| --- | --- | --- | --- | --- |
| `role_file` | string | required | Flow-dir-relative; no `..`/absolute. | The node's prompt file, under this flow's own folder. |
| `session_scope` | `fresh_disposable` \| `editing_lineage` \| `resume_own_lineage` | `fresh_disposable` | — | Session intent: fresh each pass, a durable editing session, or its own resumable session. |
| `lineage_affinity` | string \| null | `null` | Target must be an `editing_lineage` owner with no affinity of its own (one hop; no cross-provider). | Join another editing node's session (e.g. `fixing` joins `implementation`). |
| `permission_profile` | `read-only` \| `workspace-write` \| null | `null` → resolved from the flow ceiling | Must be `<= permission_ceiling`. | This node's filesystem access. Grant `workspace-write` only to nodes that edit. |
| `network_access` | bool \| null (tri-state) | `null` (inherit `network_policy`) | Codex `workspace-write` + network is rejected. | Per-node network override. |
| `provider` | `codex` \| `claude` \| null | `null` → global primary | Must be in `agents.allowed`. | Which provider runs this node. |
| `model` | string \| null | `null` | Passed through unverified. | Override the provider's default model. |
| `reasoning` | string \| null | `null` | Must be valid for the resolved provider (Claude vs Codex sets differ). | Override reasoning effort. |
| `timeout_seconds` | int \| null | `null` | — | Per-attempt CLI wall-clock ceiling. |
| `output_artifact` | `enriched_spec` \| `plan` \| `summary` \| null | `null` | Vocabulary is fixed to these three. | Persist the node's output into a well-known slot (see [roles.md](roles.md)). |
| `output_schema` | JSON-encoded string \| null | `null` | **Every object must set `additionalProperties: false`** (Codex 400s otherwise). | Custom structured-output shape. Prefer the built-in contract. |
| `best_effort` | bool | `false` | — | Tolerate an infrastructure failure and continue the task (e.g. the summary node). |
| `hitl` | `{allow_question, allow_approval}` \| null | `null` | — | Allow the agent to ask a question / request approval mid-node. |
| `extra_args` | list[str] | `[]` | Forbidden-args scan; full-access mode is rejected under `strict_isolation`. | Raw CLI flags for this node. |
| `skills` | list[str] | `[]` | Existence checked at task start (name or repo-relative `SKILL.md` path). | Operator-pinned repo skills for this node. |
| `when` | `{fact, equals?}` \| null | `null` | `fact` must be namespaced `derived.*` or `config.*`. | Conditionally run the node (e.g. `derived.needs_refinement`). |

### `evaluator` node

| Field | Type / values | Default | Constraint | Meaning |
| --- | --- | --- | --- | --- |
| `role` | string | required | Built-ins `review` / `critic` / `verifier` / `test_quality`; other strings get default evaluator behavior. | The evaluator's lens (see [roles.md](roles.md)). |
| `role_file` | string | required | Flow-dir-relative; no `..`. | The evaluator's prompt. |
| `session_scope` | `fresh_disposable` \| `resume_own_lineage` | `fresh_disposable` (or `defaults.evaluator`) | **Never `editing_lineage`** (fatal). | An evaluator never joins an author's editing session. |
| `permission_profile` | `read-only` | `read-only` | **Forced read-only** (fatal otherwise). | Evaluators never write. |
| `network_access` | bool \| null | `null` (inherit) | — | Per-node network override. |
| `blocking` | bool | `true` | — | `true` = a `rework` verdict loops until the named-loop budget is spent, then parks to manual. `false` = advisory. |
| `max_rework_per_stage` | int | `1` | **Only used when `blocking: false`.** | A non-blocking evaluator accepts after this many rework verdicts instead of looping. |
| `gate_severity` | `blocking` \| `critical` \| `high` \| `medium` \| `low` | `high` | Must be one of the five severities. | Minimum finding severity that gates: a finding at least this severe drives `rework`, less-severe ones are advisory. Default `high` blocks high/critical/blocking. Lower it (e.g. `low`) to make a critic block on any finding — pair with a larger fix budget so the extra rework rounds have headroom. Orthogonal to `blocking` (that decides whether the node gates at all; this decides which severities count). |
| `provider` / `model` / `reasoning` | as agent | `null` | as agent | Per-node provider overrides. |
| `when` | predicate | `null` | as agent | Conditional run. |

### `checks` node

| Field | Type / values | Default | Meaning |
| --- | --- | --- | --- |
| `checker` | `command_profile` \| `citation` \| `dependency_scan` | required | The built-in checker to run. `command_profile` runs the repo's configured `checks.command_sets`; `citation` verifies a research report's `sources.json`; `dependency_scan` runs a dependency vulnerability scan. |
| `when` | predicate | `null` | Conditional run. |

### `tool` node

| Field | Type / values | Default | Constraint | Meaning |
| --- | --- | --- | --- | --- |
| `tool` | string | required | Resolved against `.worc/tools/<tool>` by the `ToolRegistry` (fail-closed; not a path). | Your executable's registered name. |
| `args` | flat scalar mapping (str/int/float/bool) | `{}` | Nested/non-scalar values are a fatal load error; no secrets. | Args passed to the tool on stdin. |
| `timeout_seconds` | int \| null | `null` | — | Wall-clock timeout; resolves node → `config.tools.default_timeout_seconds` → 3600s. A timeout parks the task at `manual_action_required`. |
| `when` | predicate | `null` | — | Conditional run. |

The tool runs under the same ceiling as an agent (argv-no-shell, mandatory timeout, allowlisted env), gates the graph by exit code (`0`→`pass`, non-zero→`fail`) or a printed JSON `{outcome, findings, data}`, and exposes its stdout as `{<id>_path}`.

### `hitl` node

| Field | Type / values | Default | Meaning |
| --- | --- | --- | --- |
| `signal` | `question` \| `approval` | required | The kind of human interaction. Requires `telegram.enabled`. |
| `timeout_s` | int \| null | `null` | Blocking timeout (fails closed). |
| `when` | predicate | `null` | Conditional run. |

### `publish` node

| Field | Type / values | Default | Meaning |
| --- | --- | --- | --- |
| `policy` | a `PublishingPolicy` (see the flow-level `publishing` table) | required | Where this node publishes. Should agree with the flow header's `publishing`. |
| `when` | predicate | `null` | Conditional run. |

## Edges

| Field | Type | Default | Constraint | Meaning |
| --- | --- | --- | --- | --- |
| `from` | node id | required | Must resolve. | Source node. |
| `to` | node id | required | Must resolve. | Target node. |
| `outcome` | string \| null | `null` (unconditional) | Allowed set depends on the source kind. | Which result routes down this edge. |
| `budget` | int | — | Required on a `rework`/`fail` edge unless it has a `loop`. | Inline iteration cap for this edge. |
| `loop` | string | — | Must be declared in `budgets`. | Names a shared loop budget (e.g. `test_fix`). |

Allowed `outcome` values by source kind: **evaluator** `{accept, rework}`; **checks** `{pass, fail}`; **tool** `{pass, fail}` (plus any `route:*`); **all other kinds** unconditional (`outcome` omitted). The outcomes that charge a loop budget are `{rework, fail}` — so **every `rework`/`fail` edge must carry a `budget` or a `loop`** (unbounded loops fail validation).

## Validation (what `validate-flow` checks — all fatal, all collected)

1. **Graph integrity** — edges resolve; outcomes are legal for the source kind; every `rework`/`fail` edge is bounded; named loops are declared in `budgets`; exactly one entry node (no incoming edges); full forward reachability; at least one terminal and every node reaches one; `lineage_affinity` is valid; decomposition references resolve.
2. **Security ceiling** — evaluators forced `read-only` and never `editing_lineage`; every agent `permission_profile <= permission_ceiling`; `extra_args` pass the forbidden-args scan; all `role_file` / supervisor prompt paths are flow-dir-contained.
3. **Config-aware** (when a config is loaded) — every `provider` is in `agents.allowed`; `reasoning` is valid for the resolved provider; a Codex `workspace-write` node never also has network; the ceiling is satisfiable by some allowed provider; under `strict_isolation`, no `extra_args` full-access mode; every `tool` name resolves in `.worc/tools/`.

Non-fatal: a `budgets` value above a config cap (the engine clamps to the min), a PR-publishing flow with no git configured (runs local-commit mode), and the prompt-variable anti-drift lint (warns on a `{name}` no node populates).

## Reserved node ids

An `agent` or `tool` node id (both expose `{<id>_path}`) may **not** equal `task`, `plan`, `diff`, `checks`, `review`, `repo`, `skills`, `memory`, or `stage`, and may not start with `subtask` — a fatal load error, because those `{<name>_path}` variables already mean a core variable.
