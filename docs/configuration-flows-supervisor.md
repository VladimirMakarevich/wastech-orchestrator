# Configuration: Flows, Prompts, and Supervisor

Part of the [configuration reference](configuration.md): what `config.yaml` still owns around the flow graph — prompt templates, flow dispatch and per-node overrides, `supervisor`, `tools`, and `prompt_audit`.

## Prompt templates (no longer a config block)

There is no `prompts` config block. A flow node's prompt template is the content of its **`role_file`**. `install` delivers the built-in flows + their role files as editable copies under `.worc/flows/` (each flow's prompts in its own `<task_type>/` subdir) — the only copy the orchestrator reads at run time — so to customize a node's prompt you edit the delivered role file (a custom operator flow likewise keeps its role files under its own `.worc/flows/<task_type>/` subdir). (Removed in `config.yaml` `schema_version` **9**; an older config that still carries a `prompts` block loads fail-open — the key is ignored — and `upgrade-config` strips it.)

**Template variables.** A role file may reference an allowlisted set of `{name}` tokens; everything else (an unknown name, or literal braces in code/JSON) is left verbatim, so a template never breaks on stray braces. The variables are **metadata and artifact paths only** — never task bodies, diffs, check logs, environment values, or secrets (those stay in the artifact files the agent reads by path):

`{task_id}` `{stage}` `{repo_path}` `{repo}` `{task_path}` `{plan_path}` `{diff_path}` `{checks_path}` `{review_path}` `{subtask_order}` `{subtask_count}` `{subtask_spec_path}` `{predecessor_context}` `{memory_path}`

A variable with no value for the current node (e.g. `{plan_path}` before planning) renders as the empty string. `{memory_path}` is the path to this node's [memory](configuration-runtime.md#memory) retrieval packet — present only when the memory subsystem is enabled and this node has relevant memory; otherwise empty (wrap it in a `{?memory_path}…{/memory_path}` conditional block so the section drops cleanly when empty). `{predecessor_context}` is the intra-task **subtask handoff brief** — a deterministic factual floor (each `depends_on` predecessor's changed files, commit, acceptance criteria, spec pointer) plus, when the supervisor layer is available, a three-section interpretive brief (`new_surface_area` / `locked_decisions` / `open_edges`); it is injected into a decompose region's `implementation` node and is empty otherwise. The three decomposition variables — `{subtask_order}`, `{subtask_count}`, `{subtask_spec_path}` — are published to the **evaluator** runner as well as the agent one, so a `review` node sitting in `decomposition.sub_flow` judges each subtask's diff against that subtask's own spec rather than against the root task file alone. `{predecessor_context}` is deliberately the one channel that stays agent-only: it is the author's handoff brief, for the node that writes the subtask. The canonical author-facing reference — each variable, which runner populates it (agent / evaluator / supervisor), and when it may be empty — ships in the delivered operator guide at `.worc/guide/flows/prompt-variables.md`.

**Node outputs: `{<node_id>_path}`.** Beyond the fixed set above, every **agent** node's output is persisted as `<node_id>.out.md` and every **`tool`** node's redacted stdout as `tools/<node_id>/stdout.txt`, each exposed to later nodes as `{<node_id>_path}` — a **path**, never inlined content. The channel is derived from the node id: no declaration, no config. Both `agent` and `evaluator` prompts resolve these names, so an evaluator can grade an upstream node's own **work** rather than only the file some later node wrote from it (that is how a coverage gate grades the analysis passes behind it). A node id that collides with a reserved core-variable name (`task`, `plan`, `diff`, `checks`, `review`, `repo`, `memory`, `stage`, or anything starting with `subtask`) is a fatal flow-load error.

For a node whose real product is a **file it writes**, what the channel carries is the node's own choice: by default its closing message, or — when the node declares [`output_file:`](#per-node-overrides-in-flows) — a redacted copy of that file. A node that writes a document and then describes it in one paragraph otherwise hands the next node the paragraph, which is the smaller half. Either way it stays a path, and the downstream prompt is unchanged.

**Anti-drift lint.** `preflight` scans every flow's role files and **warns** (never fails) when a role file references a `{name}` outside that flow's valid prompt-variable set, naming the file and token. A verbatim render is the safe fallback (literal code/JSON braces must pass through), so this can never be fatal — it only surfaces the one real leak: a typo like `{plna_path}` that silently ships to the agent as placeholder text. The valid-set is **flow-derived**, computed from the flow graph rather than a static list, so it stays correct as a flow gains nodes.

**Conditional blocks.** To keep optional prose from leaving dangling empty placeholders, a role file may wrap a region in `{?name}…{/name}`: the body is kept only when `name` is an allowlisted variable with a present, non-empty value, and dropped entirely otherwise (a non-allowlisted name or an unclosed block is left verbatim). The packaged `implementation`/`fixing` roles use this for the decomposition clause — `{?subtask_spec_path}…subtask {subtask_order} of {subtask_count}…{/subtask_spec_path}` — so a non-decomposed task renders no "subtask of …" sentence at all, while a subtask unit still gets the full "subtask N of M" text.

**Put an optional section's heading _inside_ its block.** Blocks do not nest and there is no "any-of-these-variables" form, so a heading left outside the block it introduces cannot track whether the section is empty — it renders with nothing under it. Give each optional item its own heading within its own block (`{?memory_path}\n\n## Repository Memory\n\n…{/memory_path}`) and fold the leading blank line in too; the heading then appears exactly when its content does. The packaged `planning`/`implementation`/`fixing`/`review` roles follow this for their memory / subtask / predecessor items. Avoid the inverse temptation — one shared heading guarded by a separate flag — because nothing can tell it which of _your_ variables the section actually holds.

**Safety.** Role files are prompt **text** only — delivered to the CLI on stdin, never as a command argument. A role file cannot change the provider, `extra_args`, sandbox/approval mode, denied commands, denied reads, the environment allowlist, or fallback policy; it cannot enable `git commit`/`git push`/`gh pr create`. Each rendered prompt is written, redacted, to `logs/<task-id>/stages/<node-id>/run-<node-run-id:06d>/rendered-prompt.md` for audit (one per node run — a re-running node keeps every pass, since the run id is part of the path). `rendered-prompt.md` keeps its meaning under a node that declares a [`resume_role_file`](flow-authoring.md#continuation-prompts-resume_role_file): it is the text the run was launched with. Which of the two texts each attempt actually received is recorded per attempt in the [prompt audit](#prompt_audit). See [operations.md](operations.md) for troubleshooting.

## Flows (`task_type` dispatch and operator flows)

The pipeline a task runs is a **flow** — a declarative YAML graph of nodes (`agent` / `evaluator` / `checks` / `tool` / `hitl` / `publish`) and edges. A task's `task_type` front-matter field selects which flow runs (a clean task carries only identity/dispatch/operational inputs — it never patches the graph). `task_type` is omitted ⇒ `implementation`; an unknown `task_type` fails before any branch.

There is no flow block in `config.yaml`. Flows live as files in the operator's `.worc/flows/`, the sole resolution source (see the [Flow authoring guide](flow-authoring.md) for a from-scratch walkthrough):

- **Operator flows** — `<repo>/.worc/flows/<task_type>.yaml`. Drop a YAML file here to add a new `task_type` or to replace a built-in of the same name. Role files live in each flow's own subdir under `.worc/flows/<task_type>/*.md`.
- **Built-ins are delivered, not resolved from the package.** The built-ins — `implementation`, `deep_research`, `security_audit`, `merge`, `content_chapter`, `content_translate`, `blog_article`, `blog_article_revise` — ship inside the package under `packaged/flows/`, but that tree is **delivery-only**: **`install` seeds `.worc/flows/`** with editable copies of each (`<task_type>.yaml` plus its `<task_type>/` prompt dir, and the shared `roles/`), and the orchestrator never reads the packaged tree at run time. `install` likewise delivers the `tool` executables the packaged flows reference into `.worc/tools/`. So out of the box every built-in is already an operator flow you can edit; a package upgrade does not refresh the copies, so `install --reconfigure` refreshes them to the packaged version (snapshotting the existing dir to `flows.bak-<UTC>` first — and `.worc/tools/` to `tools.bak-<UTC>` alongside it — keeping only the newest three snapshots of each kind, matched by the fixed-width UTC stamp so a backup you named by hand is never pruned). A `task_type` with no file in `.worc/flows/` is a hard "flow not found", not a silent fall-back to a bundled copy. `merge` is the one built-in that is never task-dispatched: it is selected by [`git.merge_flow`](configuration-checks-git.md#git) and runs only when `worc merge-task` hits a base-merge conflict.

`config.yaml` is **infrastructure + provider defaults** that the flow's nodes fall back to (`model`/`reasoning`/`permission_profile`/`timeout`), plus the non-weakenable safety caps. The flow owns the graph; the config owns the environment. Trust is file-level: an operator flow is trusted to the same degree as `config.yaml` (same owner, same directory), and the fatal validator below guarantees it can never escalate beyond the ceiling.

**Fatal validation (at flow dispatch, and on demand via `worc validate-flow`).** A flow is loaded and validated when a task resolves it (`FlowRegistry.resolve`), so a broken or unsafe flow fails that task (→ `failed`/quarantine) rather than a global gate; `worc validate-flow [NAME|--all]` runs the same validation on demand over your `.worc/flows/`. `preflight` no longer validates flows. Three layers run:

- **Graph integrity** — edges resolve, outcomes are in the allowed set per node kind, every `rework`/`fail` edge is bounded by a budget or named loop, exactly one entry node, every node can reach a terminal.
- **Security ceiling** — a node's `permission_profile` may not exceed the flow `permission_ceiling`; evaluators are forced `read-only`; `extra_args` pass the forbidden-args screen; `role_file` paths contain no traversal; unknown fields anywhere fail closed.
- **Config consistency** — a node's pinned `provider` is in `agents.allowed`, its `reasoning` is supported by the provider that will run it, Codex is not routed to a `workspace-write` node that resolves `network_access: true`, the flow `permission_ceiling` is reachable by at least one configured provider's `permission_profile`, and — under `security.strict_isolation` — no node's `extra_args` selects a provider full-access mode (Codex `--sandbox danger-full-access` / Claude `--permission-mode bypassPermissions`; the flow-side half of the isolation gate). (On resume the live flow is re-validated against the live config, so a config change can only ever _narrow_ what a task may do.)

Two things are deliberately **not** fatal here because the orchestrator degrades them gracefully: a flow `budget` above `agents.max_*` is clamped to the cap at runtime (the cap always wins), and a PR-publishing flow under `git.create_pull_request: false` runs in local-commit mode (no PR). Neither is an escalation, so neither blocks the flow.

**Network access (flow-wide default + per-node override).** Network is **off by default**. A flow grants it flow-wide by declaring `network_policy` (`advisories`/`research`); absent that key, every node is offline (only the packaged `deep_research` / `security_audit` flows declare it). On top of that flow-wide default, an `agent` or `evaluator` node may carry an optional tri-state `network_access` field: omitting it (the default) inherits the flow default; `network_access: true` grants the node network **even in a flow that declares no `network_policy`** (so you can let only the `implementation` node fetch packages while `refinement`/`planning`/`review` stay offline); `network_access: false` is an explicit opt-out that forces the node offline even when the flow's `network_policy` would otherwise grant it. The operator owns this grant — they author and run the flow file, and it is preflight-validated like every other field. Like `network_policy`, the resolved grant toggles **only** the network dimension — it never relaxes the filesystem permission profile / sandbox, so a `read-only` node granted network stays read-only on disk. **Codex hardening:** the flow validator rejects any Codex-routed `workspace-write` agent node that also resolves `network_access: true`; split external research into a `read-only` node or set `network_access: false` on the write node. Codex read-only network remains constrained by Codex CLI sandbox support.

**Enforcement asymmetry — the guarantee is hard only under Codex.** `network_access: false` means different things per provider, and the difference matters for a node like `documentation` whose job must not install dependencies or build. Under **Codex** it maps to the OS sandbox (`workspace-write` without network), so the agent's own `Bash` — including `npm install`, `pip install`, `curl` — is physically blocked from the network: a **hard** guarantee. Under **Claude** it only disables the `WebFetch`/`WebSearch` tools; it does **not** sandbox `Bash`, so a command the agent runs can still reach the network through the OS. On a Claude-primary setup, therefore, `network_access: false` is **defense-in-depth, not a hard guarantee** — the packaged `documentation` role prompt also forbids installs/builds/network, but adherence is not enforced. When you need the hard "no dependency installs" guarantee on a scoped node, pin it to Codex (`provider: codex`) so the OS sandbox enforces the offline stance. The packaged `implementation` flow pins `network_access: false` on `documentation` for exactly this reason; the residual risk under Claude-primary is the same class as out-of-repo `Bash` noted for `trust_level` (see [§ `trust_level` (approval policy)](configuration-agents.md#trust_level-approval-policy)).

```yaml
nodes:
  - id: implementation
    kind: agent
    role_file: implementation/implementation.md
    permission_profile: workspace-write
    network_access: true # only this node may reach the network; siblings stay offline
```

### Per-node overrides in flows

Every `agent` and `evaluator` node may **override** provider/model/reasoning for that node alone. All three are optional; omit one and the node inherits the `config.yaml` provider default — `provider` ⇒ the global primary, `model` ⇒ `agents.providers.<provider>.model`, `reasoning` ⇒ `agents.providers.<provider>.reasoning`. This is how you spend more on the nodes that need it and less on the cheap ones — e.g. a low-reasoning model for a mechanical step, a stronger model + higher reasoning for review — without touching the global defaults. The packaged flows ship every agent/evaluator node with these slots present but commented out, so the configuration surface is visible at a glance; uncomment to pin a node.

```yaml
nodes:
  - id: planning
    kind: agent
    role_file: implementation/planning.md
    provider: claude # ∈ agents.allowed; default = global primary
    model: claude-opus-5 # default = agents.providers.<provider>.model
    reasoning: high # provider-specific; default = provider reasoning
  - id: review
    kind: evaluator
    role: review
    role_file: implementation/review.md
    model: claude-opus-5
    reasoning: xhigh # spend the most reasoning where rework is decided
```

The per-node fields that override something `config.yaml` sets, plus the evaluator's own gates (all default to the value shown). The fields that shape the graph rather than override the config — `session_scope`, `lineage_affinity`, `resume_role_file`, `when`, `hitl`, `output_schema`, `output_artifact` — belong to the flow author and are documented in the [Flow authoring guide](flow-authoring.md):

| Field | Node kinds | Default | Meaning |
| --- | --- | --- | --- |
| `provider` | agent, evaluator | global primary | Which agent runs the node; must be in `agents.allowed`. |
| `model` | agent, evaluator | provider's `model` | Model id for this node (not allowlisted — config carries one model per provider). Confirm ids against your installed CLIs. |
| `reasoning` | agent, evaluator | provider's `reasoning` | `low`/`medium`/`high`/`xhigh`/`max` (Codex clamps `max`→`xhigh`). |
| `network_access` | agent, evaluator | flow `network_policy` | Tri-state per-node network grant/deny (see above). |
| `git_evidence` | agent, evaluator | unset (no grant) | `true` declares that this node's job needs read-only delivery history. **Inert** unless the operator sets [`security.allow_git_evidence`](configuration-agents.md#allow_git_evidence-the-read-only-git-evidence-grant); with both in place the node may run the read-only git verbs while staying `read-only` on disk. A flow can state the need but cannot grant the capability. |
| `skills` | agent | `[]` | **Requires `security.strict_isolation: false`** (fatal otherwise). Names of the target repository's own Claude Code skills this node must invoke; each must resolve to `<repo>/.claude/skills/<name>/SKILL.md` before anything runs. One portable directory name each (no separators, no `..`). An empty list is rejected — omitting the key already means "off" — and it cannot be combined with `allow_skills: false`. See [Node-declared skills](configuration-agents.md#your-repositorys-own-skills-and-what-a-flow-node-decides). |
| `allow_skills` | agent | `null` (tri-state) → on when `skills` names one, otherwise **off** | Whether this node may invoke skills at all, carried by a real per-attempt CLI switch rather than prompt text. An explicit `true` requires `security.strict_isolation: false` (fatal otherwise); an omitted key is never an error; `false` narrows and is legal at every value of that switch. |
| `output_file` | agent | unset | The repo-relative file this node produces **is** its output: `{<node_id>_path}` then resolves to a redacted copy of that file instead of the node's closing message. Use it when the deliverable is the document and the closing paragraph is the smaller half of what downstream nodes should grade. Keep the name in step with what the role prompt tells the node to write. |
| `timeout_seconds` | agent | provider `timeout_seconds` | Per-node wall-clock cap. |
| `extra_args` | agent | `()` | Extra CLI flags for this node, concatenated after the provider list (same forbidden-args screen). |
| `permission_profile` | agent | flow `permission_ceiling` | May only be **≤** the ceiling; evaluators are forced `read-only`. |
| `best_effort` | agent | `false` | Tolerate an infra failure (engine continues) instead of failing the task. |
| `blocking` | evaluator | `true` | A failing verdict blocks (`true`) vs is advisory (`false`). |
| `max_rework_per_stage` | evaluator | `1` | Rework loops a **non-blocking** evaluator (e.g. `test_quality`) may trigger before it accepts. When the budget is spent with a finding still open it accepts and continues (never `manual`); the orchestrator emits a console warning + a ⚠️ Telegram trace (`accept (rework budget exhausted)`) so an operator knows the stage moved on and may need follow-up. **Ignored for a blocking evaluator** (the default): a blocking loop is bounded by the flow's named-loop budget (e.g. `budgets.review_fix`), then parks to `manual`. |
| `gate_severity` | evaluator | `high` | Minimum finding severity that gates (`blocking`/`critical`/`high`/`medium`/`low`): a finding at least this severe drives `rework`, less-severe ones are advisory. Default `high` blocks high/critical/blocking. Lower it (e.g. `low`) so a content critic blocks on any finding — pair with a larger fix budget for the extra rework rounds. Orthogonal to `blocking`. |

Note: **disabling** a node is not a flow field — it is a per-task override (`nodes.<id>.enabled: false` in the task file; see [operations.md](operations.md#disabling-flow-nodes-per-task)).

## `supervisor`

Configures the **supervisor layer** — a per-task oversight layer that lives _above_ any flow. It is **not a node and not a stage**: there is no `summary` node in any packaged flow because the supervisor owns the summary. The block is optional; when omitted it takes the defaults below. `worc install` **writes the block with concrete values** — `enabled: true`, `provider` pinned to the global primary, `observe.mode: events`, and each phase's model resolved to the primary provider's model — so the oversight layer's provider/cost/effort is visible rather than an implicit inherit-from-primary.

Three keys are one-per-layer and stay at the top; **model and reasoning are per phase**, under `observe` / `finalize` / `handoff`.

```yaml
supervisor:
  enabled: true # false removes the layer entirely (see below)
  role_file: "roles/supervisor.md"
  provider: claude # install pins the global primary; null → inherit the global primary
  observe:
    mode: events # all | selected | events | none
    triggers: [rework, failure, fallback] # narrows what counts under `events`
    include_nodes: [] # the nodes observed under `mode: selected`
    model: "claude-sonnet-5" # the cheap one — this phase can fire per step
    reasoning: low
  finalize:
    model: "claude-sonnet-5" # writes summary.md, i.e. the PR body — worth more
    reasoning: high
  handoff:
    model: "claude-sonnet-5" # the subtask brief between decompose regions
    reasoning: high
```

| Field | Type | Default (dataclass / install) | Meaning |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` / install: `true` | `false` removes the layer — the object is never built. See [Running without a supervisor](#running-without-a-supervisor) below. |
| `role_file` | string | `"roles/supervisor.md"` | Role file (resolved inside the active flow's directory) rendering the observe-lens prompt; a missing/unreadable file falls back to a minimal built-in instruction. Never loaded when the cadence resolves to `none`. |
| `provider` | `codex` \| `claude` or null | `null` / install: the primary | Which provider runs all three phases; null inherits the global primary. Validated ∈ `agents.allowed`, and each phase's `reasoning` is checked against this **resolved** provider. Model itself is passed through unverified — but a model that plainly looks like the other vendor's (a `claude-*` model under a `codex` primary) emits a **warning** (not fatal; the run degrades via fallback), so the mismatch is not silent. |
| `observe.mode` | `all` \| `selected` \| `events` \| `none` | `events` / install: `events` | How often a completed step earns an LLM note. A flow may only **narrow** it. See the table below. |
| `observe.triggers` | list of `rework` \| `failure` \| `fallback` | all three | Narrows which deviations count under `events` — e.g. `[failure]` for failures only. Closed set; an unknown name is rejected. |
| `observe.include_nodes` | list of node ids | `[]` | The nodes observed under `mode: selected`; ignored in every other mode. |
| `observe.model` / `observe.reasoning` | string or null | `null` / `low` | The **cheap** phase: advisory, and able to fire on every step of a deep fix loop. Reasoning is capped to `high` in code even if you set a max tier. `install` no longer writes either key — `low` is the default, so an absent key already says it. |
| `finalize.model` / `finalize.reasoning` | string or null | `null` / install: primary model + `high` | The turn that writes `summary.md` — the PR body, and the only part of a long run most readers see. A max tier (`xhigh`/`max`) is capped to `high` when the turn is structured. |
| `handoff.model` / `handoff.reasoning` | string or null | `null` / install: primary model + `high` | The subtask brief between regions of a decomposed task. Unused by a flow that never decomposes. |

Keep every phase **at or below** the producer nodes' tier. This layer is advisory — it never routes, reworks, or blocks — so a model stronger than `agents.providers` inverts the budget: the reasoning that decides the deliverable gets the weaker one.

### What `observe.mode` costs

Ranked by how many calls the mode can produce — which is also the order a flow may narrow along.

| Mode | Observes | Use it when |
| --- | --- | --- |
| `none` | nothing | The flow's quality is already held by a blocking gate. `finalize` and the summary still happen. |
| `events` (default) | only a deviation: an evaluator sending work back (or accepting after exhausting its rework budget), a step whose run failed, a step that fell back to the non-primary provider | Almost always. Cost tracks what went wrong, not how long the run was. |
| `selected` | exactly `include_nodes` | You want notes on two named steps and nothing else. |
| `all` | every executed step | Debugging the run itself. This is what a long run pays for. |

`tool`, `checks`, and the terminal `publish` node are never observed under **any** mode — their result is already a durable fact the finalize packet carries verbatim, so an advisory note about a pass/fail bought nothing and cost a full call per run.

**What a mode actually cost you is measured, not guessed.** Each run writes a `supervisor_usage` block into `.worc/logs/<task-id>/summary.json` (local only, never committed): calls, input, cached input, output, cost, and provider wall time, as a total and split by job — `observe`, `finalize`, `handoff`. Read the `observe` versus `finalize` split on your own flow before tuning.

A flow may **narrow** the cadence in its own `supervisor.observe.mode` but never widen it: a flow declaring a broader mode than yours fails validation before any node runs, naming both modes (a flow is authored content and must not be able to spend more than you allowed). The packaged content flows ship `none`; `implementation` ships `events`. One consequence worth knowing: because a flow that _states_ `events` is asserting it needs deviation notes, setting your global mode to `none` is **rejected** for that flow rather than silently degrading it — and the rejection lands during flow resolution, **after** the task has been claimed, so it ends in terminal `failed` that you re-queue by hand. Run `worc validate-flow --all` after editing `observe.mode` on either side:

```bash
worc validate-flow --all && worc watch
```

There is no cap on what the layer may spend beyond the mode itself: no call budget, no token ceiling. The digest the finalize turn reads is bounded deterministically in code (8 000 characters), the mode bounds the frequency, and `all` is a deliberate operator choice.

### Running without a supervisor

`supervisor.enabled: false` removes the layer wholesale — all three phases go away: the per-step observation, the whole-task finalize, and the subtask handoff brief. Two couplings resolve at config load:

- **The rest of the `supervisor` block becomes inert and is no longer validated.** One warning names it, so a `provider`/`reasoning` combination that would otherwise be rejected simply stops mattering.
- **`memory.enabled: true` is forced to `false` for the run**, with a warning naming both keys. That layer's closing turn is the only path that writes anything memory can later read back, so with the layer off memory would keep adding a packet to every prompt while never learning. Set `memory.enabled: false` yourself to make the file say what actually runs.

The **flow-cadence narrowing rule is skipped** when the layer is off — there is no cadence to widen, so a flow declaring `events` runs unchanged. That asymmetry is exactly why removing the layer is its own key rather than a global `observe.mode: none`: the global setting is _refused_ for such a flow, and refused after the task is claimed.

Switching observations off and removing the layer are therefore two different levels. `observe.mode: none` silences the per-step notes and keeps the synthesis; `enabled: false` removes the synthesis too, and the PR body is then rendered from the same recorded facts the packet is built from — the same sections, without the interpretation.

### What the layer does

- It exists for **every** task under any flow shape while `enabled` — even a single agent node with no checks/review.
- At the configured cadence it runs **one read-only** observation on its own continuing session and records an immutable advisory `supervisor_step` row. Observation is **best-effort**: a failure is logged and swallowed.
- At whole-task **close** (before `publish` in the `implementation` flow) it synthesizes the plain-language `summary.md` (the PR body) plus advisory caveats and records `supervisor_final`. The finalize turn runs on a **fresh** session seeded by a deterministic packet of the run's facts (`.worc-io/<task-id>/supervisor/packet.json`) built from the recorded node runs and each node's own output — never from the observations — so its input is a few kilobytes regardless of how long the run was, a resumed task's summary is as complete as a first run's, and `mode: none` still produces a full PR body. It is also handed every in-flow evaluator's recorded verdict and findings, so a gate that accepted **with** findings cannot be summarized as one that simply passed.
- `summary.md` carries **only human prose**: it is prefixed with a deterministic `# {task_title}` H1 (so the PR body is never a headless slab) and the evidence-gated follow-ups render as their own section; a leaked structured dump (a model that emits `<summary>…</summary><follow_ups>…<memory_delta>…` text instead of a clean tool call) is sanitized — cut at the first machine tag — so raw `follow_ups`/`memory_delta`/`lessons` never reach the PR body. A finalize turn that fails on a rerun does not overwrite an existing non-empty `summary.json` with a blank one.

The layer is **advisory by construction**: it never reworks, reopens, or routes — blocking is the job of the in-flow `review`/evaluator nodes. Its `permission_profile` is **forced `read-only`** in code (it can never edit), validated under the same ceiling as flow nodes (`provider` ∈ `agents.allowed`, each phase's `reasoning` in the allowlist against the resolved provider, `role_file` path-contained).

### The summary artifacts, and when the deterministic report takes over

The whole-task handoff is two files under `logs/<task-id>/`: the human `summary.md` (the PR body, committed beside the task file) and the local-only machine companion `summary.json`.

`summary.json` has **one key set everywhere** — every flow, every terminal, both writers:

```json
{
  "what": "<task title>",
  "summary": "<prose, or empty>",
  "follow_ups": [],
  "supervisor_usage": {},
  "degraded": true
}
```

`follow_ups`, `supervisor_usage` and `degraded` are present only when they apply. (The former four-field `What / How / Integration / Why` stub is gone, and with it the `how` / `integration` / `why` triad.)

**One renderer writes the PR body whenever no provider-authored synthesis reaches disk.** That is four situations, not one:

1. `supervisor.enabled: false` — there was never a synthesis turn.
2. The terminal has no prose by design (`failed` / `manual_action_required`).
3. The synthesis was expected but the call could not run.
4. **The synthesis came back collapsed** — prose below a 120-character floor (a one-liner or a bare probe). Deliberately not "a real synthesis" length: discarding honest short prose from a genuine prose flow would itself be a regression. A collapse yields **no** `summary.md`, flags the run `degraded`, and carries the discarded text in a WARNING; the `supervisor_final` audit row records what reached disk rather than what the turn returned.

The report's sections are **Changes / Steps / Checks / Gates / Technical debt / follow-ups / Pipeline nodes skipped**. It is a pure function of `state.db` plus the task's artifacts — two renders of one run are byte-identical — and it **never inlines the diff**: a pull request already _is_ its diff, so the report names the changed paths and points at `logs/<task-id>/current.diff`. A `failed` or `manual_action_required` run therefore gets a real report rather than a stub.

**How the follow-ups section is composed.** Every evaluator finding is persisted with a `gating` flag, so only the findings a gate actually **let past** become PR follow-ups — plus a gating finding still open because a _non-blocking_ evaluator spent its `max_rework_per_stage` budget, worded as still open. Each mechanically derived record carries the evaluator's own `fix` as its `action_hint` and a `title` that is the finding's first sentence, with the remainder in `rationale` (rather than a mid-word truncation). The persisted finding shape is `{severity, reason, paths, gating, fix}`, and `failure_report.json` findings gain the same additive keys. The finalize turn is told not to restate the accepted findings merged in for it. This composition runs regardless of `supervisor.emit_follow_ups`, which only governs the _supervisor-authored_ half — and which is a **flow** key, declared in a flow's own `supervisor:` block (the packaged `implementation` flow sets it), not a `config.yaml` one: the config `supervisor` block accepts only `enabled`, `role_file`, `provider`, `observe`, `finalize`, and `handoff`, and anything else there is a fail-closed unknown key.

## `tools`

Optional block configuring [custom `tool` nodes](flow-authoring.md#custom-tool-nodes) — operator executables a flow runs out-of-process from `.worc/tools/`. Omit the block for the same default.

```yaml
tools:
  default_timeout_seconds: 3600 # 1h; per-node `timeout_seconds` overrides it
```

| Field | Type | Default | Meaning |
| --- | --- | --: | --- |
| `default_timeout_seconds` | int | `3600` | Flow-wide default wall-clock timeout for a `tool` node whose own `timeout_seconds` is unset. |

A tool node's effective timeout resolves `node.timeout_seconds` → `tools.default_timeout_seconds` → the built-in `3600`s. A timeout (like a launch error) is an **infrastructure** failure: the task parks at `manual_action_required` — it is never a quality `fail` and never spends a fix iteration. The tool itself is registered by dropping an executable at `.worc/tools/<name>` (file-trusted, like your flows and `config.yaml`); the flow references it by name and the registry rejects a missing / uncontained / non-executable tool fatally at flow validation. There is **no** global on/off switch — a tool is enabled by adding a `kind: tool` node to a flow. See [flow-authoring.md → Custom tool nodes](flow-authoring.md#custom-tool-nodes) for the stdin/exit-code/JSON contract, `{<node_id>_path}` composition, and the honest v1 security boundary (file trust + env-allowlist + redaction; **not** OS-sandboxed, so `network_policy` is not forced on a tool).

> **Write your tool's stdout as UTF-8 explicitly.** The runner decodes a tool's stdout as **UTF-8 on every OS**, so a tool that falls back to the host's locale encoding is broken on Windows only — and silently until the first non-ASCII byte. A piped child on a Windows host gets `cp1252`, which cannot encode a `≤` or a `→`, so a violation message carrying one kills the script inside its own `print`: the node then reports a _crashed checker_ instead of the `fail` it had already computed. Both shipped tools (`check_chapter`, `check_length`) pin stdin/stdout to UTF-8 for exactly this reason — do the same in yours (in Python, `sys.stdout.reconfigure(encoding="utf-8")`, or write bytes).

## `prompt_audit`

Optional top-level boolean (default `false`). Added in `config.yaml` `schema_version` **8**. When enabled, every agent-routed stage run records **who** received **what prompt** — a self-contained, redacted record per stage execution — under `logs/<task-id>/prompt-audit/`, in chronological order, plus a combined `timeline.jsonl`.

```yaml
prompt_audit: false # record each step's prompt + who (provider/model/attempt/fallback/status)
```

The directory carries the same content twice, one half per reader.

- **The per-step file** is `<node_run_id:06d>-<node-id>[-sub<NN>].md` — Markdown, not JSON (the zero-padded `node_run_id` makes a lexical sort chronological). A fenced `json` metadata header holds the who: route, provider, the **effective** model/reasoning the settled attempt ran on plus the `configured_*` pair the flow node overrode (`null` when it overrode nothing), the node's declared skill posture (`skills_allowed` / `skills_required`), and one row per agent that ran the prompt — primary plus any fallback — with its attempt number, status, error class and `resumed` flag. The prompt then follows **verbatim as the document body**, under its own heading, and a node with a `resume_role_file` has a second `## Continuation prompt` section beside it. It is written last and unescaped, so a prompt containing its own fences or headings cannot break the metadata above it. This is the half an operator opens: inside a JSON string every newline is an escaped `\n`, and a multi-page prompt read as one flat line.
- **`timeline.jsonl`** is the machine-readable half and is unchanged — one whole record per line, prompt included. Parse this one.

The prompt is identical across a stage's attempts, so it is stored once, and it is redacted with the same policy as `rendered-prompt.md` (no secrets in artifacts). The directory is archived into `attempt-<N>/` on `rerun` like the rest of the task's logs.

A per-task `prompt_audit: true|false` in the [task front matter](task-authoring.md) **always overrides** this global value (task wins, in both directions — there is no operator gate). So a global `true` audits every task, while a global `false` plus a per-task `true` audits only that task.
