# Authoring custom flows for wastech-orchestrator

**You are an operator (or an agent helping one) authoring a flow for wastech-orchestrator.** A _flow_ is the pipeline a task runs through, written as data: a validated graph of typed nodes (`agent`, `evaluator`, `checks`, `tool`, `hitl`, `publish`) joined by outcome-labelled edges. A task's `task_type` selects its flow. This file is the quickstart; the complete, self-contained references are alongside it:

- **[reference.md](reference.md)** — every flow-level and node-level field, with allowed values, defaults, and constraints (including what each `output_policy` / `publishing` / `permission_ceiling` / `network_policy` variant means and when to pick it).
- **[roles.md](roles.md)** — how to write the node prompts: the built-in evaluator roles, the per-node output contract, output slots, and the supervisor layer.
- **[prompt-variables.md](prompt-variables.md)** — the `{name}` variables a role prompt may reference.

Between them these three cover every field and every prompt variable, so you should not need anything outside this guide to author a flow. The orchestrator's own repository carries the same material with extra contributor-facing detail, for work on the orchestrator itself.

If you only want to change _what a step says_, you do not need a new flow — edit that node's `role_file` prompt in the delivered copy under `.worc/flows/` (see [roles.md](roles.md)). Author a flow only when you need different steps, a different output kind, or a different route.

## Where flows live

- **Dispatch file:** `<repo>/.worc/flows/<task_type>.yaml`. The file stem is the `task_type`. A file here adds a new `task_type` or replaces a built-in of the same name. `.worc/flows/` is the only place the orchestrator resolves flows from — a `task_type` with no file here is a hard "flow not found", never a silent fall-back to a bundled copy.
- **Prompts:** each flow **owns its prompts** in a sibling folder named after the `task_type` — `.worc/flows/<task_type>/*.md`. `role_file` values in the YAML are relative to `.worc/flows/` and point into that folder (e.g. `role_file: my_flow/implement.md`).
- **Supervisor prompts:** the supervisor is a constant layer above all flows (not a node). Its global default observe lens is `.worc/flows/roles/supervisor.md`, but a flow may own its supervisor wording — and its observation cadence — with a `supervisor:` block (see "Flow-local supervisor prompts" below); a flow with no such block uses the shared `roles/supervisor.md` and the operator's global cadence.

`install` seeds editable, active copies of the built-in flows — `implementation`, `deep_research`, `security_audit`, `merge`, and the content-authoring flows `content_chapter` / `content_translate` / `blog_article` / `blog_article_revise` — plus the executables their `tool` nodes resolve against (e.g. the `check_chapter` prose gate, the `check_length` minimum-size floor) under `.worc/tools/`. The packaged copies inside the wheel are delivery-only (never read at run time), so those seeded copies under `.worc/flows/` are already yours to edit.

**Tracking flows in git:** `install` gitignores the whole `.worc/` runtime home as one unit, so `.worc/flows/` has no git history by default. To track it (review changes via PR, share flows with teammates), replace the blanket `.worc/` line `install` wrote in the repo's tracked `.gitignore` with:

```gitignore
# Ignore the runtime home's contents (not the dir itself) so flows/ can be re-included:
# Git won't descend into a fully-excluded dir, so `.worc/` would make any !.worc/flows a no-op.
.worc/*
!.worc/flows/
```

`state.db`, `logs/`, `workspace/`, `config.yaml`, and everything else under `.worc/` stay ignored — only `flows/` (and everything nested under it: per-flow prompt folders, `roles/supervisor.md`) is carved back out. Add `!.worc/tools/` too if you also want the packaged tool executables tracked.

## Minimal custom flow

Save as `.worc/flows/my_flow.yaml`, with prompts under `.worc/flows/my_flow/`:

```yaml
flow:
  name: my_flow
  task_type: my_flow # must equal the file stem
  permission_ceiling: workspace-write # hard cap; no node may exceed it, no task may widen it
  output_policy: code_change # code_change | repository_document | private_control_workspace_report (reference.md explains each)
  publishing: pull_request # pull_request | documentation_pull_request | local_artifact | private_control_workspace_report | none (reference.md explains each)

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

## Output policy

`output_policy` is a **closed set of three** — you choose which _shape_ of deliverable the flow produces, and the engine resolves that name to a fixed write area and required files. You cannot specify anything else or point a flow at an arbitrary directory, and the name is a **contract, not a description**.

- **`code_change`** — the diff (anywhere in the repo) **is** the deliverable; no required files. Use for code **and** for a brand-new prose/Markdown file committed to the repo — a blog post, a chapter, a translation (the packaged `content_chapter` / `content_translate` flows all use this). Pair with `pull_request` / `documentation_pull_request`.
- **`repository_document`** — writes are confined to `docs/research/<task_id>/` and must include `report.md` + `sources.json` (the `deep_research` shape; a `citation` node checks the manifest). Pair with `documentation_pull_request`.
- **`private_control_workspace_report`** — the report node returns its report as structured output and the orchestrator writes `report.md` into the private `.worc/security-reports/<task_id>/` (via `output_artifact: report`); it **never enters git** (the `security_audit` shape). The node stays `read-only` — no agent write. Pair with `none`.

**Common trap:** a brand-new document that is not a `docs/research/*` sources bundle — e.g. a blog post under `blog/` — is a `code_change`, not a `repository_document`. Choosing `repository_document` because "it's a document" confines every write to `docs/research/<task_id>/`, so the real file lands outside it and the flow hard-stops at `manual_action_required` on the first successful write. See [reference.md → `output_policy`](reference.md#output_policy--what-each-variant-means) for what each variant permits.

## Chaining node outputs (`{<node_id>_path}`)

Every **agent** node's output is persisted and exposed to later nodes as `{<node_id>_path}` — a path to that node's `<id>.out.md`, never the inlined content. A **`tool`** node exposes the same variable (its redacted stdout). The variable resolves to the redacted copy in the agent-facing exchange (`.worc-io/<task-id>/stages/<id>/run-<NNNNNN>/`), while the original stays the private audit record under `.worc/logs/<task-id>/stages/<id>/run-<NNNNNN>/`; a node that re-runs in a loop keeps every pass and `{<node_id>_path}` resolves to the latest. That is how a multi-step flow hands one node's result to the next by name, with no extra config:

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

One node exposes exactly one output — to publish several results, split into several nodes. A node id (agent or tool — both expose `{<id>_path}`) may not collide with a reserved core-variable prefix (`task`, `plan`, `diff`, `checks`, `review`, `repo`, `skills`, `memory`, `stage`, `subtask*`); that is a fatal load error.

If the node's real product is a **file it writes** rather than what it says at the end, name that file with `output_file:` and the channel carries the file instead of the message — see [roles.md](roles.md#when-the-nodes-product-is-a-file-it-writes-output_file).

An **`evaluator`** prompt reads the same names, so a gate can judge what an upstream node actually reported rather than only the file a later node wrote from it. The packaged `deep_research` flow uses this for its `coverage_gate`: three analysis passes each publish a report, and the gate opens all three (`{?analysis_core_path}` …) and compares them against the repository before the flow writes a conclusion on top of them.

## Custom tool nodes (`kind: tool`)

A `tool` node runs **your own** executable (any language) from `.worc/tools/` instead of an LLM — for deterministic logic that is neither "smart" work (`agent`) nor a built-in gate (`checks`). Drop the program at `.worc/tools/<name>` (on POSIX, `chmod +x`; on Windows add a `.cmd`/`.exe` — the resolver finds `<name>.cmd` from the same flow name), or use a built-in tool `install` already delivered (e.g. `check_chapter`, `check_length`), then reference it by name. A Windows wrapper may delegate to the extensionless same-name payload beside it (`<name>.cmd` → `<name>`): the frozen per-task control bundle preserves every existing same-name launch candidate so the pair stays together. This same-name launcher/payload set is the only supported multi-file shape; arbitrary helper modules and data files are not inferred or copied, so package those into the executable or expose a separate self-contained tool.

```yaml
nodes:
  - id: md-check
    kind: tool
    tool: md-check # → .worc/tools/md-check
    args: { min_chars: 500 } # flat scalars only, no secrets
edges:
  - { from: md-check, to: publish, outcome: pass }
  - { from: md-check, to: fix, outcome: fail } # fail → a fixer agent that reads {md-check_path}
```

The orchestrator runs it under the same ceiling as an agent (argv-no-shell, mandatory timeout, allowlisted env — never secrets or the full environment) and feeds a small JSON context on **stdin** (`task_id`, `node_id`, `subtask_order`, allowlisted `paths`, your `args`). The tool gates the graph by its **exit code** (`0` → `pass`, non-zero → `fail`) or, for `route:*` / structured findings, by printing `{"outcome": "...", "findings": [...], "data": {...}}` on stdout (a JSON `outcome` wins; an invalid one fails closed to manual). `findings`/`data` are recorded, never auto-applied. Its redacted stdout is exposed downstream as `{<id>_path}`. Timeout resolves node → `config.tools.default_timeout_seconds` → `3600`s; a timeout/launch error parks the task at `manual_action_required` (not a quality fail). **Write UTF-8 on stdout, explicitly.** The orchestrator decodes your tool's stdout as UTF-8 on every OS, so a tool must not fall back to the host's locale encoding: on Windows a piped child gets `cp1252`, and a single `≤`, `→` or `—` in a message then kills the tool on its own `print` — the node reports a crash instead of the verdict it computed. In Python that is one line at the top of `main` (`sys.stdout.reconfigure(encoding="utf-8")`); both shipped tools do it. A non-zero exit with empty stdout and non-empty stderr is also treated as a crashed checker rather than a quality verdict: the task parks before a fix iteration is charged, and the operator sees a bounded redacted stderr diagnostic. If the same node instead returns an identical `fail` without findings twice, the second result parks before another fix iteration can be charged; a changed report or structured findings resets that guard. A `tool` is **file-trusted** (you own `.worc/tools/`, as you own your flows) and is **not** OS-sandboxed: the ceiling above bounds what it is *handed* (argv, timeout, env allowlist, stdin context), not what it may *do* once running, so treat a tool script as code you have reviewed yourself. [reference.md → `tool` node](reference.md#tool-node) lists its fields.

## What a node returns (contracts, slots, and custom schemas)

An `evaluator` node always returns a **typed structured result**, not free text, and so does an `agent` node whose shape selects one — and for the built-in node kinds you do not write the schema; it is selected automatically:

- **plain `agent`** — no schema; the output is its final message (still exposed as `{<id>_path}` above).
- **`agent` with `hitl:`** — must return `content` plus an optional question/approval object.
- **`agent` named by `decomposition.proposed_by`** (the planning node) — `content` + optional `human_input` + `decompose` + `subtasks`.
- **`evaluator`** — a findings verdict `{ findings: [ { severity, path, what, fix } ] }`, and it is **fail-closed**: a missing or malformed findings result routes the task to `manual_action_required` instead of a silent `accept`. So an evaluator `role_file` must actually emit the findings result — a prose-only "looks good" review hard-stops the task.

The core re-validates every typed result itself, so a malformed result fails the node; you cannot loosen this from a flow.

### Named output slots (`output_artifact`)

Besides the generic `{<id>_path}` channel above, an agent node can fill one of four fixed slots with `output_artifact:`, landing its `content` in a well-known file that the orchestrator writes (the node returns the content as its structured output; it does not write files itself):

- `output_artifact: enriched_spec` → writes `task.enriched.md` (audit only; no downstream variable).
- `output_artifact: plan` → writes `plan.md`, read downstream as `{plan_path}`.
- `output_artifact: summary` → writes `summary.md`, which feeds the `publish` node's pull-request body — not a prompt variable (normally the supervisor layer fills this, not a flow node).
- `output_artifact: report` → the orchestrator writes `report.md` into the flow's private report directory (`private_control_workspace_report` output policy — the `security_audit` shape). The node is `read-only`; no agent write is needed.

The slot vocabulary is fixed to these four; a flow only chooses which node fills each, and one node fills at most one slot.

### Overriding the schema (the one real foot-gun)

An `agent` node may set an inline `output_schema:` to return data of your own shape. If you do, **every object in the schema — the top level and every nested object — must set `additionalProperties: false`.** Codex enforces `--output-schema` through OpenAI Structured Outputs, which rejects a non-strict schema with a hard **400** and fails the node on every run (this exact mistake once broke the built-in review node). Claude tolerates a loose schema, but write it strict so the same flow runs on both providers. Keep a string `content` field if the node also fills a slot, and prefer the built-in contract unless you genuinely need a custom shape.

## Flow-local supervisor prompts

The supervisor is a constant read-only layer above the flow — it observes completed nodes (never the deterministic `tool` / `checks` ones or the terminal `publish`, and otherwise as often as the cadence says) and writes the final summary (the PR body). A flow can reshape **its wording**, and choose its own observation cadence, with an optional `supervisor:` block:

```yaml
supervisor:
  role_file: my_flow/supervisor.md # observe lens; fallback: flow -> config.supervisor.role_file -> built-in
  finalize_role_file: my_flow/summary.md # finalize lens; fallback: flow -> built-in (no global one)
  handoff_role_file: my_flow/handoff.md # subtask handoff-brief lens (decompose flows); fallback: flow -> built-in
  emit_follow_ups: true # opt this flow's finalize into the technical-debt / follow-ups signal
  observe:
    mode: none # this flow's observation cadence: all | selected | events | none
```

- All three prompt files are resolved inside the flow dir (relative paths, no `..`); a traversing path fails validation.
- **`observe.mode`** picks how often a completed step is worth an LLM note: `none` (never — `finalize` and the summary still happen), `events` (only a deviation: a rework, a failed step, a provider fallback), `selected` (the operator's `include_nodes`), `all` (every step). Omit the block to inherit `config.supervisor.observe.mode`, whose default is `events`. A flow may only **narrow** the operator's mode — a broader one fails validation, naming both (rank `none < events < selected < all`, and `selected` counts as broader than `events`). Two consequences: at `none` the `role_file` observe lens above is never loaded at all, so put anything you need into `finalize_role_file`; and a flow that *states* `events` cannot run under a global `none`, which is the point — it is asserting that its follow-ups need deviation notes rather than degrading quietly.
- Supervisor prompts receive only `{task_id}`, `{repo}` / `{repo_path}` — no node/path variables. The `validate-flow` anti-drift lint scans them too, so a mistaken `{plan_path}` (or any other `{name}` the supervisor never fills) is flagged as rendering verbatim.
- **`handoff_role_file`** is only used by decompose flows: at each subtask boundary the supervisor writes an interpretive handoff brief for the next subtask, exposed as `{predecessor_context}` to any agent node in the region whose role prompt references it — the packaged `implementation` flow reads it from its `implementation` node (see `prompt-variables.md`). A deterministic factual floor (changed files, commit, acceptance criteria, spec pointer) is always present; the interpretive brief is one best-effort turn on the supervisor's own session, and it runs under every `observe.mode` — including `none`, where it is simply the first turn on that session rather than a continuation of any observations.
- **`finalize_role_file`** is the one to set whenever your deliverable is not a diff: the built-in lens summarizes "the actual committed change", which reads wrong for a document or a report — and this summary _is_ the PR body. Word it so the turn describes what the **pipeline** did (it is a read-only observer; it must never claim it re-opened or spot-checked a file itself) and so it states no count or verdict it was not handed. It is not guessing at the latter: the orchestrator appends every in-flow evaluator's recorded verdict and findings to that turn's prompt, and hands it a deterministic **packet** of the run's facts (`packet` in its context footer — changed paths and diff stat, each executed node with its outcome and what it reported, the check commands and their results, and whatever observations were recorded — which is nothing at all under `observe.mode: none`, so word the lens to treat that section as possibly empty). So a gate that accepted **with** findings open cannot be summarized as one that passed. Note that this turn runs on a **fresh** session, not as a continuation of the observations: word the lens to read the packet and open what it points at, never to recall the run. See the packaged `deep_research/summary.md`. One floor your wording cannot lower: a one-line or placeholder summary is discarded as a collapsed generation — the run logs a warning and the deterministic report becomes the PR body, flagged as a fallback — so never word a lens to permit one. The floor is deliberately low (it catches a probe, not brevity), because a mechanical report is a worse PR body than a short honest paragraph; the packaged prose lenses ask for concision and still clear it comfortably.
- **`emit_follow_ups`** (default `false`) is a **code-oriented** capability: when on, the supervisor's existing finalize turn (no extra LLM call) also emits an **evidence-gated** `follow_ups` array — technical debt / refactor candidates it saw, each with `title` / `rationale` / `paths` / `evidence` / `severity` / `action_hint` — written into `summary.json` and a "Technical debt / follow-ups" section of `summary.md`. Set it on a code flow (the packaged `implementation` flow does); leave it off for research / prose flows (never ask them to invent "refactor candidates").
- That same section carries a second, **deterministic** source you do not opt into: the evaluator findings a gate **let past** — each evaluator node's final verdict, minus the findings that actually gated (those already went through the fix loop, so repeating them would describe work that was done). The one exception is a finding still open above the gate because a non-blocking evaluator spent its `max_rework_per_stage` budget: that one is kept, with an evidence line saying so rather than "accepted with findings". Each derived record carries the reviewer's own `fix` as its `action_hint`, so a mechanical follow-up arrives with the proposed remedy, and its `title` is the finding's first sentence (or a word-boundary cut) with the remainder in `rationale` — never a truncated copy of the text beside it.
- Both sources are merged and deduplicated on exact text. Because a paraphrase cannot exact-match, the finalize turn is told that the accepted findings are merged in for it and that it must report only debt that is **not** already in the gate-verdict list — keep that expectation if you reword the lens, or the same issue lands twice at two severities.
- The merged list has a **third** destination, which needs no flow field: as the task finishes it is appended to `.worc/follow-ups.md`, the repository's accumulating list of what the orchestrator noticed and did not fix. The PR body is per-change and `summary.json` is deleted by `worc logs clean`; that file is neither. It is append-only and there is no command for it — see [footprint.md](../footprint.md).
- Only the **wording** moves into files. The structured-output schemas (`follow_ups`, and the memory delta) stay in the orchestrator, so your prompt can change tone and emphasis but can never break what the orchestrator parses.

## Register, run, validate

- **Register:** the file _is_ the registration. A task selects it with front matter `task_type: my_flow`. An unknown `task_type` fails the task before any branch is created.
- **Validate:** run `wastech-orchestrator --config ./.worc/config.yaml validate-flow <name>` (or `--all`). The flow is loaded and validated config-aware (graph integrity, security ceiling, config consistency, `.worc/tools/` tool names) — exactly what the engine checks at dispatch; a failure prints `flow <name>: FAIL — <reason>` and exits non-zero. Preflight does **not** validate flows. (A broken flow that a task requests also fails safely at dispatch, before any branch is created.)
- **Debug:** set `prompt_audit: true` to record the exact rendered prompt per node under `logs/<task-id>/prompt-audit/`; per-run artifacts live under `.worc/logs/<task-id>/`.

## Foot-guns

- Keep `permission_ceiling` as low as the flow needs; grant `workspace-write` only to nodes that edit. A task can never widen it.
- Put every node `role_file` inside the flow's own `<task_type>/` folder; relative paths only (no `..`). `roles/supervisor.md` is the shared **global** supervisor default; to give this flow its own supervisor wording, use the `supervisor:` block (above) pointing into your own folder, not a node `role_file`.
- Bound every `fail`/`rework` loop with a `budget`; exactly one entry node (no incoming edges); every node must reach a terminal.
- **Editing sessions are per lineage.** An `editing_lineage` node keeps a durable LLM session keyed `lineage_affinity or <node id>`: a node with no `lineage_affinity` owns its own lineage, and a node with `lineage_affinity: X` joins X's session (so `fixing` continues what `implement` established). Two affinity-less editing nodes are therefore two **separate** tracks that never share context — use `lineage_affinity` to make one join another. Affinity is one hop only: the target must itself be affinity-less (a chain like `a → b → c` fails validation), and a lineage cannot resume across providers. **Resuming has a token cost:** the session's full history is re-sent on every following turn, so input tokens grow — share a session only where stages genuinely build on each other, and prefer `fresh_disposable` between unrelated stages.
- Network is off by default; declare `network_policy` for a flow-wide grant or `network_access: true` on one node. A Codex `workspace-write` node with network is rejected — split external fetches into a `read-only` node.
- A `read-only` node that must read delivery history (an audit lens citing the commit that closed a milestone) declares `git_evidence: true` — do **not** raise it to `workspace-write` to get a shell. It stays read-only: the verbs only report and the sandbox denies every write. Inert until the operator sets `security.allow_git_evidence`, and rejected on a `workspace-write` node (which already has an unrestricted shell).
- If you set a custom `output_schema`, make every object in it `additionalProperties: false` — Codex rejects a non-strict schema with a 400 and the node fails every run. Prefer the built-in contract; an `evaluator` prompt must emit the findings result or the task fail-closes to manual.

For the complete contract (every node field, per-node provider/model/reasoning overrides, edges, and the validation layers), see [reference.md](reference.md); for the node prompts see [roles.md](roles.md) and [prompt-variables.md](prompt-variables.md). The orchestrator's own repository adds contributor-facing internals on top of the same material.
