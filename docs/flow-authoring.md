# Flow authoring

A **flow** is the pipeline a task runs through, expressed as data: a validated graph of typed nodes (`agent`, `evaluator`, `checks`, `tool`, `publish`) connected by outcome-labelled edges. A task's `task_type` selects its flow. The orchestrator ships three built-in flows — `implementation` (the default), `deep_research`, and `security_audit` — and an operator can add or override flows without touching Python. This guide shows how to author, register, validate, and debug a custom flow from scratch.

If you only want to change _what a step says_ (not the graph), you do not need a new flow — edit the node's `role_file` prompt in the delivered copy (see [Customize a node's prompt](cookbook.md#7a-customize-a-nodes-prompt)). Author a new flow only when you need a different set of steps, a different output kind, or a different route.

## Where flows live

Flows resolve from **one place**: the operator's `<repo>/.worc/flows/<task_type>.yaml`. Drop a YAML file here to add a new `task_type` or to replace a built-in of the same name. Each flow **owns its prompts** in a sibling folder named after the `task_type`: `.worc/flows/<task_type>/*.md`. So `role_file` paths in the YAML are written relative to `.worc/flows/` and point into that folder (e.g. `role_file: my_flow/implement.md`).

The built-ins ship inside the package under `packaged/flows/`, but that tree is **delivery-only**: `install` copies it into `.worc/flows/` as editable, active copies, and the orchestrator never reads the packaged tree at run time. So the seeded copies of `implementation`/`deep_research`/`security_audit` are already your operator flows to edit — and a `task_type` with no file in `.worc/flows/` is a hard "flow not found" (run `install` to deliver the built-ins), not a silent fall-back to a bundled copy. What you see in `.worc/flows/` is exactly what runs.

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
- **`evaluator`** — a read-only judge that returns `accept` / `rework`. Fields: `role` (e.g. `review`), `role_file`, `blocking` (a failing verdict blocks vs is advisory), `max_rework_per_stage`. Evaluators are forced `read-only`. An evaluator returns a structured **findings** verdict and is **fail-closed** — see [What a node returns](#what-a-node-returns-output-contracts-schemas-and-slots).
- **`checks`** — runs deterministic repository commands, no agent. `checker: command_profile` runs the configured check command sets; other checkers exist (`citation`, `dependency_scan`). Outcomes: `pass` / `fail`.
- **`tool`** — runs **your own** executable from `.worc/tools/` out-of-process (any language), under the same launch ceiling as an agent. Fields: `tool` (the registered executable name), optional flat-scalar `args`, optional `timeout_seconds`, `when`. Outcomes: `pass` / `fail` / `route:*` (by exit code or an optional JSON object on stdout). Use it for deterministic logic that is neither an LLM step (`agent`) nor a built-in gate (`checks`). See [Custom tool nodes](#custom-tool-nodes).
- **`publish`** — the terminal. `policy: pull_request` / `documentation_pull_request` opens a PR; `policy: none` is a graph terminal that performs no Git action (the orchestrator still owns any real commit/push/PR).

Nodes never pick the next node or commit anything — the engine routes on edge outcomes, and only the orchestrator does Git.

## Edges, outcomes, and loops

Each edge is `{ from, to, outcome? }`. A `checks` or `tool` node emits `pass`/`fail` (a `tool` may also emit `route:*`); an `evaluator` emits `accept`/`rework`; a plain `agent` edge needs no `outcome`. Any `fail`/`rework` edge that loops back must carry a `loop:` name (or a `budget:`) and be bounded in `budgets:`. Exactly **one** entry node (no incoming edges) is allowed, and every node must be able to reach a terminal — the validator enforces both.

## Role files (prompts)

A node's prompt is the content of its `role_file`. Role files render only an allowlisted set of path/metadata variables — `{task_path}`, `{repo_path}`, `{plan_path}`, `{diff_path}`, `{review_path}`, `{skills_path}`, `{memory_path}`, `{subtask_order}`/`{subtask_count}`/`{subtask_spec_path}`, and a few more — never task bodies, diffs, env, or secrets. A variable that is empty for a given node renders as the empty string; wrap optional references in a conditional block `{?name}…{/name}` so they drop cleanly when empty. For the full variable contract and which runner populates each, see [configuration.md → Prompt templates](configuration.md#prompt-templates-no-longer-a-config-block) and the functional block [B15](functional/blocks/B15-prompt-templates.md).

`role_file` paths are contained to the flow directory: a path with `..` or an absolute path is rejected at load. Keep prompts inside your `<task_type>/` folder.

## Per-node overrides

Every `agent`/`evaluator` node may pin its own `provider` (`codex` | `claude`), `model`, and `reasoning`; omit any and the node inherits the `config.yaml` provider defaults (`provider` ⇒ the global primary). A node may also set `network_access: true|false` to override the flow-wide network default for that node alone. Spend more reasoning where rework is decided (review), less on mechanical steps. See [configuration.md → Per-node overrides in flows](configuration.md#per-node-overrides-in-flows).

## Editing sessions and lineages

`session_scope` decides how a node reuses the provider's LLM session (its accumulated conversation context):

- **`fresh_disposable`** — a cold session every visit; nothing is carried forward. The default, and the only sound choice for a read-only evaluator (it must never inherit an author's context).
- **`editing_lineage`** — a durable session shared across a group of editing nodes so they keep continuous context. This is what an edit → fix loop uses.
- **`resume_own_lineage`** — a node's **private** durable session across its own rework rounds (e.g. a critic that must remember what it already flagged); not shared with any other node.

A flow can carry **more than one** durable editing session per execution unit — one per **lineage**. The lineage key is derived from the graph, `lineage_affinity or <node id>`:

- An `editing_lineage` node with **no** `lineage_affinity` **owns** a lineage named after itself.
- A node with `lineage_affinity: X` **joins** the lineage owned by `X`, resuming and updating that same session.

So in the example above, `fixing` (`lineage_affinity: implement`) continues the session `implement` established. To run two isolated tracks in one flow — say a code track and a separate spec track — give each track its own affinity-less `editing_lineage` owner node and point that track's fix node at it; the two sessions never leak context into each other. (Both tracks still edit the same working tree and join the same committed diff — a lineage is about session context, never filesystem isolation.)

Two rules the validator enforces: a session cannot resume **across providers** (an `editing_lineage` node and its affinity target must not pin conflicting providers), and **affinity chains are forbidden** — a `lineage_affinity` target must itself be a lineage owner (a node with no affinity of its own), so affinity is one hop only.

> Note: two `editing_lineage` nodes that both omit `lineage_affinity` are now **two separate lineages** (each keyed by its own id), not one shared session — read operator flows with the `lineage_affinity or <node id>` rule in mind.

## What a node returns (output contracts, schemas, and slots)

Every `agent` and `evaluator` node returns a **typed structured result**, not free text — but you almost never write the shape yourself. The node's kind and role select a built-in output contract automatically:

| Node | Built-in contract | The agent is required to return |
| --- | --- | --- |
| `agent`, plain author | none | its final message (no schema enforced) |
| `agent` with `hitl:` declared | `human_input` | `content` + an optional question/approval object |
| `agent` named by `decomposition.proposed_by` | `planning` | `content` + optional `human_input` + `decompose` + `subtasks` |
| `evaluator` | findings | `{ findings: [ { severity, path, what, fix } ] }` |

Two properties of these contracts change how you write the role prompts:

- **The evaluator contract is fail-closed.** An empty `findings` array is a real clean pass, but a _missing or malformed_ one means the agent ignored the schema — the orchestrator refuses to guess and sends the task to `manual_action_required` instead of a silent `accept`. So an `evaluator` role prompt must actually produce the findings result; a prose-only "looks good to me" review will hard-stop the task.
- **The core re-validates every typed result itself**, independently of the provider. A malformed `planning` / `human_input` / findings result fails the node rather than being trusted — you cannot loosen this from a flow.

### Where a node's output goes

A node's output is written to a file and passed to later nodes **as a path variable** — never inlined as text (the same rule as all [prompt variables](#role-files-prompts)). Two mechanisms:

- **Named slots** — set `output_artifact: <slot>` on an agent node to land its `content` in a well-known file that downstream nodes read by variable:

  | `output_artifact` | File | Downstream variable | Typical filler |
  | --- | --- | --- | --- |
  | `enriched_spec` | `task.enriched.md` | — (audit only) | a refinement node |
  | `plan` | `plan.md` | `{plan_path}` | a planning node |
  | `summary` | `summary.md` | `{summary_body_path}` | usually the supervisor layer, not a flow node |

  The slot vocabulary is fixed to these three; a flow only chooses _which_ node fills each, and one node fills at most one slot.

- **Generic channel** — every other agent node's output is written to `<node_id>.out.md`, and every `tool` node's redacted stdout to `tools/<node_id>/stdout.txt`, each exposed automatically as `{<node_id>_path}`, so a later node can consume an earlier node's output by naming that variable in its prompt.

### Overriding a node's `output_schema` (the one real foot-gun)

An `agent` node may set an inline `output_schema:` (a JSON Schema) to override the built-in contract when a custom node must return data of your own shape. One rule dominates:

> **Every object in the schema — the top level and every nested object — must set `additionalProperties: false`.**

Codex enforces `--output-schema` through OpenAI Structured Outputs, which rejects any non-strict schema with a hard **400**, failing the node on _every_ run (this exact mistake once broke the built-in review node). Claude tolerates a loose schema, but write it strict so the same flow runs on both providers. Also:

- Keep the schema flat and fully typed. Deeply nested or loosely typed schemas make structured output fragile and slower to produce.
- If your schema omits the `content` key, the named slots have nothing to persist — keep a string `content` field when the node also fills `plan` / `summary` / `enriched_spec`.
- No extra flow or config is needed for Codex to hand the JSON back — the adapter reads it from the run's last-message file for you. Your only job is a strict schema.

The **supervisor** summary/follow-ups and the **memory** delta are produced by the constant orchestrator layer _above_ the flow, not by nodes you author — you never define their schemas (you only toggle them via `supervisor.emit_follow_ups` and the `memory` config block).

## Custom tool nodes

A `tool` node runs **your own** program instead of an LLM — any language, by contract, not by interpreter. Use it for deterministic logic that is neither "smart" work (`agent`) nor a built-in gate (`checks`): a bespoke `.md` linter, a data producer for the next node, a router.

**Where a tool lives.** Put the executable at `.worc/tools/<name>` and reference it from a flow by that **one name**, never a path. Resolution is cross-platform from the single name: on POSIX the bare `<name>` must be `chmod +x`; on Windows the resolver also tries launcher suffixes, so the same flow name finds `<name>.cmd`/`.exe` (and a `.cmd`/`.bat` is launched through the command interpreter, since Windows cannot start a batch file directly). The registry resolves the name to a contained, executable file and rejects anything else (a missing tool, a traversal, a symlink out of `.worc/tools/`) **fatally at preflight**, before any task starts.

**Built-in tools ship with the orchestrator.** `worc install` delivers packaged tools into `.worc/tools/` (per machine, so the launcher always matches the OS), exactly as it delivers the built-in flows — a plain re-run fills in missing files, `--reconfigure` snapshots the existing dir first. The content flows' `check_journey` prose gate is one such tool (see [`check_journey`](#the-check_journey-prose-gate) below). Because a packaged `tool` node is validated in every repo at preflight, its executable must be delivered everywhere — which the installer guarantees.

**The contract (like a Claude Code hook).** The orchestrator runs the tool through the same launch ceiling as an agent — an argv list (never a shell string), a mandatory timeout, and exactly the allowlisted `security.allowed_environment` (the parent environment is never inherited). It feeds a small JSON **context on stdin** — only allowlisted paths + your `args`, never secrets, the full environment, or a session id:

```json
{
  "task_id": "…",
  "node_id": "…",
  "subtask_order": null,
  "paths": {
    "repo": "…",
    "task_path": "…",
    "plan_path": "…",
    "diff_path": "…",
    "checks_path": "…",
    "review_path": "…"
  },
  "args": { "…": "…" }
}
```

The tool reports back through its **exit code** and an **optional** JSON object on stdout:

- **Linter style** — just `exit 0` (→ `pass`) or non-zero (→ `fail`); stdout is ignored as the outcome but saved as an artifact. Minimum effort.
- **Rich style** — print `{"outcome": "pass" | "fail" | "route:<label>", "findings": [...], "data": {...}}`. A JSON `outcome` is authoritative (an invalid value fails closed to `manual_action_required`); `route:*` drives an explicit edge. `findings` and `data` are **recorded** (shown to the human / supervisor) but **never auto-applied** — the core never turns a returned value into a Git or state write.

**Composition.** A tool's redacted stdout is exposed downstream as `{<node_id>_path}`, exactly like an agent's output. That is how a tool-as-check hands its report to a fixer agent — the fixer's role prompt reads `{md-check_path}` — and how a tool-as-producer feeds the next node. No magic: findings reach the agent as a path variable, not through the engine.

**Args** are a flat scalar mapping (`str`/`int`/`float`/`bool`) declared in the flow — allowlisted static config, not secrets. **Timeout** resolves `node.timeout_seconds` → `config.tools.default_timeout_seconds` → the built-in `3600`s; a timeout is an infrastructure failure (→ `manual_action_required`, not a quality `fail`, and it never spends a fix iteration), as is a launch error.

**The honest v1 boundary (do not over-trust).** A `tool` is **not** OS-sandboxed the way `codex`/`claude` sandbox themselves — its real ceiling is **file trust** (you own `.worc/tools/`, exactly as you own your flows and `config.yaml`) plus the env-allowlist (no secrets reach it), artifact redaction, the mandatory timeout, and the absence of any core path that applies its output to Git/state. Consequently `network_policy` is **not** forced on a `tool` in v1 (an arbitrary binary can open a socket); if you need hard network/filesystem isolation, that is an OS/container concern (a deferred follow-up). Treat a tool with the same trust you treat a role prompt you author.

### Example — a tool check that routes back to a fixer

```yaml
nodes:
  - id: md-check
    kind: tool
    tool: md-check # → .worc/tools/md-check
    args: { min_chars: 500, max_chars: 4000 }
  - id: fix
    kind: agent
    role_file: roles/fix.md # its prompt references {md-check_path}
edges:
  - { from: md-check, to: publish, outcome: pass }
  - { from: md-check, to: fix, outcome: fail }
  - { from: fix, to: md-check } # unconditional — back for a re-check
```

`roles/fix.md` shows the agent exactly the findings, via one path line: `Report: {md-check_path}`.

### The check_journey prose gate

`check_journey` is the built-in `tool` shipped for the content-authoring flows — one parametrized validator selected by `args.mode`. It is a self-contained script (no third-party dependencies), delivered on install as an extensionless `+x` file plus a `check_journey.cmd` Windows launcher.

It reports through the standard tool contract above (JSON `{"outcome", "data"}` on stdout, exit `0`/non-zero). **Scope is only the changed chapters**: it reads the changed `.md` paths from the run's `diff_path`, falls back to `.md` files named in `task_path`, and if neither yields a chapter it is a vacuous `pass` — so the gate never fails on pre-existing issues in untouched chapters. Rules per mode:

| `mode` | Enforces |
| --- | --- |
| `ru` | ≤1 title per page; `## → ### → ####` hierarchy (no skipped level); `Purpose` + `Emotional point` present; the `не …, а …` AI-antithesis pattern (+ a small cliché list); no service-label headings (`Что это`, `Философия`, …). **No character limit.** |
| `en` | all `ru` rules **plus** per-page length 500–800 chars (hard max 800) and ≤3 paragraphs. |
| `book` | the length-bearing `en` ruleset applied across every assembled page. |

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

- **Graph integrity** — edges resolve, outcomes are valid per node kind, every `fail`/`rework` edge is bounded, exactly one entry node, every node reaches a terminal, and every `lineage_affinity` target is an `editing_lineage` owner with no affinity of its own (no chains).
- **Security ceiling** — no node's `permission_profile` exceeds the flow `permission_ceiling`; evaluators are forced read-only; `role_file` paths contain no traversal; unknown fields fail closed.
- **Config consistency** — a pinned `provider` is in `agents.allowed`, its `reasoning` is supported by that provider, and (under `security.strict_isolation`) no `extra_args` selects a full-access sandbox mode.

Run it explicitly with:

```bash
worc --config ./.worc/config.yaml preflight
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
- **Strict `output_schema` or none at all.** If you override a node's `output_schema`, put `additionalProperties: false` on every object in it — Codex rejects a non-strict schema with a 400 and the node fails every run. Prefer the built-in contract unless you genuinely need a custom shape ([What a node returns](#what-a-node-returns-output-contracts-schemas-and-slots)).
- **Evaluators must emit findings, not prose.** A `review`/`verifier`/`critic` role prompt has to return the structured findings result; a prose-only verdict is treated as "schema not honored" and fail-closes the task to manual review.
- **Validate before you rely on it.** Run `preflight` after every flow edit; it fails closed with a one-line reason.

## See also

- [Configuration → Flows](configuration.md#flows-task_type-dispatch-and-operator-flows) — the flow/config split and the full validation contract.
- [Cookbook → Customize a node's prompt](cookbook.md#7a-customize-a-nodes-prompt) — editing a prompt without a new flow.
- [Task authoring](task-authoring.md) — how a task selects a flow via `task_type`.
- Functional map: [B29 flow definition & validation](functional/blocks/B29-flow-definition-and-validation.md), [B30 flow node runners](functional/blocks/B30-flow-node-runners.md).
