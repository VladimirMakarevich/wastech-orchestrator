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
| `supervisor` | no | `{role_file?, finalize_role_file?, handoff_role_file?, emit_follow_ups?, observe?}` | none | Prompt paths flow-dir-contained (no `..`); `observe.mode` may only narrow the config's. | Flow-local supervisor wording, the follow-ups opt-in, and this flow's observation cadence — `observe: {mode: all\|selected\|events\|none}` (see [roles.md](roles.md)). |

### `output_policy` — what each variant means

| Value | What writing nodes may write | Must produce | Enters git? | When to use |
| --- | --- | --- | --- | --- |
| `code_change` | Anywhere in the repo (the deliverable _is_ the diff, guarded by the dangerous-diff gate). | — | Yes (normal diff). | Code/implementation flows. Pairs with `publishing: pull_request`. |
| `repository_document` | **Only** `docs/research/<task_id>/`. | `report.md` + `sources.json` | Yes (committable document). | Research/analysis flows that ship a document into the repo. Pairs with `publishing: documentation_pull_request`. |
| `private_control_workspace_report` | Orchestrator-written into `.worc/security-reports/<task_id>/` from the report node's structured output (`output_artifact: report`); the node is read-only. | `report.md` | **No — private, fail-closed.** Any attempt to stage/commit/PR it is refused. | Sensitive reports (e.g. security audits) that must never enter git. Pairs with `publishing: none` (or `private_control_workspace_report`). |

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

### Read-only git evidence

An audit node that treats delivery history as prime evidence needs to be able to read it. `git_evidence: true` on an agent or evaluator node asks for that; the operator's `security.allow_git_evidence` decides whether the request is honored. Both halves are required — a flow can express the need but cannot grant itself the capability, and turning the switch on hands the verbs only to the nodes that asked, never to every read-only node in the run. With the switch off (the default) a declaring flow still loads and validates; it simply runs as it does today.

**What you get is stated as an observable contract, not as a list of flags: history is readable, the repository cannot be changed, and nothing is published.** The two providers reach it by different means, and it is worth knowing which, because the guarantees have different shapes:

- **Claude** has no shell at all under `read-only`, so the grant adds one and scopes it to the read-only verbs (`log`, `show`, `diff`, `blame`, `status`, `rev-list`, `rev-parse`, `ls-files`, `shortlog`, `describe`, `cat-file`, `for-each-ref`). A command matching none of them is refused by the CLI before it runs. Underneath that allowlist the OS sandbox write-denies the whole clone, so the allowlist is the convenience and the sandbox is the guarantee. **If you pin a new `claude` major, re-verify that refusal.** That the CLI *denies* a `Bash` call matching no `--allowedTools` pattern (rather than approving it) is behavior no offline test can assert and no `--help` grep can answer — preflight checks that the flag exists, not what it means. It was verified by hand against `claude` 2.1.217: under the argv this adapter builds, `--allowedTools "…,Bash(git log:*)"` plus a request to run `echo x > f.txt` must come back an error tagged `"non_execution_kind": "permission-rule"` with no file created, while `git log --oneline -1` must run clean. If a future CLI approves the first one instead, the verb allowlist is decorative and only the sandbox confines the shell — history stays readable and the repository still cannot be changed, but the node is no longer held to *reading git*. The documented fallback in that case is Codex's shape: drop the allowlist and rely on the sandbox alone. On a host where a shell cannot be sandboxed the adapter refuses the attempt rather than running it unsandboxed — on Linux/WSL2 missing `bubblewrap`+`socat` that is a `CAPABILITY_UNAVAILABLE` refusal raised **when the attempt starts**, not a preflight failure, so the node may still be covered by a fallback provider that can isolate on this host; on native Windows (no supported Bash sandbox) the shell is simply dropped and the node runs as an ordinary read-only node. Either way `worc preflight` and the run log name the host in a loud line beforehand, but neither blocks the run.
- **Codex** needs nothing added: its `read-only` sandbox already permits commands, so `git log` works there today. What forbids mutation is not a verb list but the sandbox — the workspace is mounted `read` and the network is off, so `git commit` has nothing to write to and `git push` has nowhere to go. That is the stronger guarantee of the two, and no prompt, task or flow can argue with it.

So do not expect a symmetric verb allowlist across providers; expect the same observable contract. `security.denied_commands` (`git commit`, `git push`, `gh pr create`, `gh pr merge`) stays the floor beneath both — a deny always beats an allow — and publishing remains the orchestrator's alone.

If a write from such a node ever does land, the run does **not** park: the orchestrator emits a console warning plus a ⚠️ Telegram trace (`done (read-only node wrote to the workspace)`) and continues, and the change is not published or handed to any downstream node. The reasoning is the same as for a non-blocking evaluator's exhausted budget — the capability is real and worth keeping, so a stray file is reported rather than traded for it.

The same holds if such a node changes **Git control state** — a hook, `.git/config`, the index: warning plus a ⚠️ `done (read-only node changed git control state)` trace, naming the drifted aspect, and the run continues. **Treat that one as a stop-the-run signal, not a note.** The two events differ in what they cost you: a stray file is inert (nothing stages it, nothing downstream reads it), while a planted `.git/hooks/post-commit` is executed by the next git command in that clone — and the next one is the orchestrator's own commit or push. Do not let the run finish; kill it, discard the clone, and look at what was planted. A `workspace-write` node doing the same still parks the task in `manual_action_required` — this never-park rule covers the read-only class alone.

### `defaults.evaluator`

Applied to any evaluator node that omits the field. Keys: `session_scope` (default `fresh_disposable`), `permission_profile` (default `read-only`), `max_rework_per_stage` (default `1`), `gate_severity` (built-in default `high`). Use it to avoid repeating the same evaluator settings across many evaluator nodes — `deep_research` sets `gate_severity: medium` here, which covers all three of its evaluators (`coverage_gate`, `fact_verification`, `critical_review`) at once.

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
| `session_scope` | `fresh_disposable` \| `editing_lineage` \| `resume_own_lineage` | `fresh_disposable` | — | Session intent: fresh each pass, a durable editing session, or its own resumable session. Resuming re-sends the session's full history each turn (input tokens grow), so prefer `fresh_disposable` between unrelated stages. |
| `lineage_affinity` | string \| null | `null` | Target must be an `editing_lineage` owner with no affinity of its own (one hop; no cross-provider). | Join another editing node's session (e.g. `fixing` joins `implementation`). |
| `permission_profile` | `read-only` \| `workspace-write` \| null | `null` → resolved from the flow ceiling | Must be `<= permission_ceiling`. | This node's filesystem access. Grant `workspace-write` only to nodes that edit. |
| `network_access` | bool \| null (tri-state) | `null` (inherit `network_policy`) | Codex `workspace-write` + network is rejected. | Per-node network override. |
| `git_evidence` | bool \| null (tri-state) | `null` (does not ask) | Rejected on a `workspace-write` node (it already has an unrestricted shell). Honored only while the operator's `security.allow_git_evidence` is on. | Ask for the **read-only git verbs** so this node can inspect delivery history — see [Read-only git evidence](#read-only-git-evidence) below. |
| `provider` | `codex` \| `claude` \| null | `null` → global primary | Must be in `agents.allowed`. | Which provider runs this node. |
| `model` | string \| null | `null` | Passed through unverified. | Override the provider's default model. |
| `reasoning` | string \| null | `null` | Must be valid for the resolved provider (Claude vs Codex sets differ). | Override reasoning effort. |
| `timeout_seconds` | int \| null | `null` | — | Per-attempt CLI wall-clock ceiling. |
| `output_artifact` | `enriched_spec` \| `plan` \| `summary` \| `report` \| null | `null` | Vocabulary is fixed to these four. | Persist the node's output into a well-known slot (see [roles.md](roles.md)). `report` (read-only node) has the orchestrator capture the node's structured output into the private report dir. |
| `output_file` | string \| null | `null` | One portable filename — no path separators, no `..` (fatal at load). Mutually exclusive with `output_artifact` (fatal). | The file this node **produces** is what `{<node_id>_path}` carries downstream, instead of the node's closing message. Resolved inside the flow's `output_policy` report dir (the repository root for a policy without one). See [roles.md](roles.md#when-the-nodes-product-is-a-file-it-writes-output_file). |
| `output_schema` | JSON-encoded string \| null | `null` | **Every object must set `additionalProperties: false`** (Codex 400s otherwise). | Custom structured-output shape. Prefer the built-in contract. |
| `best_effort` | bool | `false` | — | Tolerate an infrastructure failure and continue the task (e.g. the summary node). |
| `hitl` | `{allow_question, allow_approval}` \| null | `null` | — | Allow the agent to ask a question / request approval mid-node. |
| `extra_args` | list[str] | `[]` | Forbidden-args scan, which includes the provider full-access selectors — rejected at any value of `strict_isolation`. | Raw CLI flags for this node. |
| `skills` | list[str] | `[]` | Existence checked at task start (name or repo-relative `SKILL.md` path). | Operator-pinned repo skills for this node. |
| `when` | `{fact, equals?}` \| null | `null` | `fact` must be namespaced `derived.*` or `config.*`. | Conditionally run the node — see [Conditional nodes](#conditional-nodes-when) below. |

#### Conditional nodes (`when:`)

The fact resolver is core code, not an operator surface: the namespace prefix is validated at load, but the **names** are a closed set of two, and an unrecognized name resolves `false` silently — so a typo is indistinguishable from a gate that works. Read them literally before gating anything on them:

| `fact` | Actually means | Watch out |
| --- | --- | --- |
| `derived.needs_refinement` | The task file is **not** well-formed — a non-empty description plus an `## Acceptance criteria` section already counts as complete. | This is a *formedness* check, not a "does this task need scoping" check. Every properly written task skips the node. |
| `config.external_research` | This **flow** declares a `network_policy`. Despite the namespace it is neither a config key nor a task field. | Always `true` in a flow that sets `network_policy`, always `false` in one that does not — it can never gate at run time. |

Neither is a relevance test. To make a node optional per task, disable it in the task file (`nodes: { <id>: { enabled: false } }`); that is the operator-facing switch, and it is checked before the predicate. If you keep a `when:` that cannot change the outcome, say so in a comment next to it — an inert predicate reads like a working gate.

### `evaluator` node

| Field | Type / values | Default | Constraint | Meaning |
| --- | --- | --- | --- | --- |
| `role` | string | required | Built-ins `review` / `critic` / `verifier` / `test_quality`; other strings get default evaluator behavior. | The evaluator's lens (see [roles.md](roles.md)). |
| `role_file` | string | required | Flow-dir-relative; no `..`. | The evaluator's prompt. |
| `session_scope` | `fresh_disposable` \| `resume_own_lineage` | `fresh_disposable` (or `defaults.evaluator`) | **Never `editing_lineage`** (fatal). | An evaluator never joins an author's editing session. |
| `permission_profile` | `read-only` | `read-only` | **Forced read-only** (fatal otherwise). | Evaluators never write. |
| `network_access` | bool \| null | `null` (inherit) | — | Per-node network override. |
| `git_evidence` | bool \| null | `null` (does not ask) | Honored only while `security.allow_git_evidence` is on. | Ask for the read-only git verbs (see [Read-only git evidence](#read-only-git-evidence)). The evaluator stays read-only either way. |
| `blocking` | bool | `true` | — | `true` = a `rework` verdict loops until the named-loop budget is spent, then parks to manual. `false` = advisory. |
| `max_rework_per_stage` | int | `1` | **Only used when `blocking: false`.** | A non-blocking evaluator accepts after this many rework verdicts instead of looping. When the budget is spent with a finding still open it accepts and continues (never `manual`) — and the orchestrator emits a **console warning + a ⚠️ Telegram trace** (`accept (rework budget exhausted)`) so you know the stage moved on and may need follow-up. The still-open finding also reaches the "Technical debt / follow-ups" section of the PR body, worded as still open rather than as an accepted sub-threshold note. |
| `gate_severity` | `blocking` \| `critical` \| `high` \| `medium` \| `low` | `high` | Must be one of the five severities. | Minimum finding severity that gates: a finding at least this severe drives `rework`, less-severe ones are advisory (still reported — see below). The built-in default `high` blocks high/critical/blocking; **every packaged flow whose evaluator is a quality lens sets `medium`**, because "is this good enough" has no natural way to emit `high`. Lower it to `low` to block on any finding. Orthogonal to `blocking` (that decides whether the node gates at all; this decides which severities count) — but read them together: on a **`blocking: true`** node a gating finding loops the named-loop budget and then parks the task in `manual_action_required`, so lowering the gate there means raising that budget too. On a non-blocking node the same change costs at most `max_rework_per_stage` extra rounds and lands on accept + a ⚠️ warning. |
| `provider` / `model` / `reasoning` | as agent | `null` | as agent | Per-node provider overrides. |
| `when` | predicate | `null` | as agent | Conditional run. |

### `checks` node

| Field | Type / values | Default | Meaning |
| --- | --- | --- | --- |
| `checker` | `command_profile` \| `citation` \| `dependency_scan` | required | The built-in checker to run. `command_profile` runs the repo's configured `checks.command_sets`; `citation` verifies a research report's citation manifest; `dependency_scan` runs a dependency vulnerability scan. |
| `manifest` | string | `sources.json` | `citation` only: the manifest filename inside the flow's report dir. One portable filename — no path separators, no `..` (fatal at load). Set it when your writing node names its manifest something else; the wrong name yields `uncheckable: <name> missing` and a gate that does nothing. Note `repository_document`'s required-files list is fixed at `report.md` + `sources.json`, so a renamed manifest is still checked but is not registered as a `report` artifact. |
| `when` | predicate | `null` | Conditional run. |

The `citation` checker classifies each entry as `verified` (the snippet is present **at the cited line**), `weak` (the snippet is in the file but not at the cited line — a real quote, a mis-attributed location), `broken` (a path/line/snippet that does not resolve), or `uncheckable` (an external `url`, an entry with no snippet to check, or a malformed entry). **Only `broken` gates.** It never judges the `claim` field: a real snippet at a real line can still carry a fabricated assertion, which is an evaluator's job — so read a pass as "every location resolves", never as "every claim holds". The per-entry verdicts are published on both outcomes and reach the next node as `{checks_path}`; that file also carries `manifest_path` (repo-relative, `null` for a flow with no report dir), so the evaluator can open the manifest for each entry's claim without its prompt hardcoding where your deliverable lives.

### `tool` node

| Field | Type / values | Default | Constraint | Meaning |
| --- | --- | --- | --- | --- |
| `tool` | string | required | Resolved against `.worc/tools/<tool>` by the `ToolRegistry` (fail-closed; not a path). A frozen task copies the whole existing same-name launch set (`name` plus Windows launcher suffix siblings); no arbitrary helper/data files. | Your executable's registered name. |
| `args` | flat scalar mapping (str/int/float/bool) | `{}` | Nested/non-scalar values are a fatal load error; no secrets. | Args passed to the tool on stdin. |
| `timeout_seconds` | int \| null | `null` | — | Wall-clock timeout; resolves node → `config.tools.default_timeout_seconds` → 3600s. A timeout parks the task at `manual_action_required`. |
| `when` | predicate | `null` | — | Conditional run. |

The tool runs under the same ceiling as an agent (argv-no-shell, mandatory timeout, allowlisted env), gates the graph by exit code (`0`→`pass`, non-zero→`fail`) or a printed JSON `{outcome, findings, data}`, and exposes its stdout as `{<id>_path}`. A non-zero exit with empty stdout and non-empty stderr is a crashed checker, not a quality fail: it parks at `manual_action_required` with a bounded redacted stderr diagnostic and does not charge a fix iteration. Two identical `fail` results without findings from the same node also park on the second result before another fix iteration is charged; changed output or findings reset that guard.

### `hitl` node

| Field | Type / values | Default | Meaning |
| --- | --- | --- | --- |
| `signal` | `question` \| `approval` | required | The kind of human interaction. Requires `telegram.enabled`. `question` proceeds unconditionally (`done`) once any non-empty answer arrives; `approval` **branches** — declare `route:approve` / `route:deny` edges for it. |
| `timeout_s` | int \| null | `null` → `telegram.ask_timeout_s` | Blocking timeout, fail-closed: a timeout, a transport error, a missing notifier, or an invalid answer parks the task at `manual_action_required`. |
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

Allowed `outcome` values by source kind: **evaluator** `{accept, rework}`; **checks** `{pass, fail}`; **tool** `{pass, fail}`; **all other kinds** unconditional (`outcome` omitted). A `route:<name>` outcome is accepted on **any** source kind — and a `hitl` node with `signal: approval` emits one, so it needs `route:approve` / `route:deny` edges rather than an unconditional one. The outcomes that charge a loop budget are `{rework, fail}` — so **every `rework`/`fail` edge must carry a `budget` or a `loop`** (unbounded loops fail validation).

## Validation (what `validate-flow` checks — all fatal, all collected)

1. **Graph integrity** — edges resolve; outcomes are legal for the source kind; every `rework`/`fail` edge is bounded; named loops are declared in `budgets`; exactly one entry node (no incoming edges); full forward reachability; at least one terminal and every node reaches one; `lineage_affinity` is valid; decomposition references resolve and its `sub_flow` region is connected (an edge from `proposed_by` into it, and a forward exit edge out of it).
2. **Security ceiling** — evaluators forced `read-only` and never `editing_lineage`; every agent `permission_profile <= permission_ceiling`; `git_evidence` rejected on a `workspace-write` agent node; `extra_args` pass the forbidden-args scan; all `role_file` / supervisor prompt paths are flow-dir-contained.
3. **Config-aware** (when a config is loaded) — every `provider` is in `agents.allowed`; `reasoning` is valid for the resolved provider; a Codex `workspace-write` node never also has network; the ceiling is satisfiable by some allowed provider; `supervisor.observe.mode` is no broader than the config's (rank `none < events < selected < all`; skipped entirely when `supervisor.enabled` is false — there is then no cadence to widen); every `tool` name resolves in `.worc/tools/`.

Non-fatal: a `budgets` value above a config cap (the engine clamps to the min), a PR-publishing flow with no git configured (runs local-commit mode), and the prompt-variable anti-drift lint (warns on a `{name}` no node populates).

## Reserved node ids

An `agent` or `tool` node id (both expose `{<id>_path}`) may **not** equal `task`, `plan`, `diff`, `checks`, `review`, `repo`, `skills`, `memory`, or `stage`, and may not start with `subtask` — a fatal load error, because those `{<name>_path}` variables already mean a core variable.
