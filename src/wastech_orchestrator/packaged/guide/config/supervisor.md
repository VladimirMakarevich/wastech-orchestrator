# `supervisor` — the oversight layer

**You are an operator (or an agent helping one) configuring wastech-orchestrator.** This page documents the `supervisor` block: the per-task oversight layer above any flow, its observation cadence, its model/reasoning settings, and what each `observe.mode` costs.

For the fields not on this page see [reference.md](reference.md), which also carries the cross-field rules that apply across blocks; for the how-to walkthrough see [README.md](README.md) and for safe defaults [best-practices.md](best-practices.md).

## `supervisor` — the oversight layer

A read-only layer above every flow that observes completed nodes and writes the final summary. Its `permission_profile` is forced `read-only` in code. It is on by default and can be removed entirely with `enabled: false`, in which case the pull-request body is rendered deterministically from the run's recorded facts instead.

Three keys are one-per-layer and stay at the top; model and effort are **per phase**, under `observe` / `finalize` / `handoff`.

| Field | Type / values | Default (dataclass / install) | Constraint | When to use |
| --- | --- | --- | --- | --- |
| `supervisor.enabled` | bool | `true` / install: `true` | — | `false` removes the layer: no per-step notes, no summary turn, no subtask handoff brief, no `skills.dynamic` proposal, and every key below inert (one warning says so). The PR body is then written deterministically. Also forces `memory.enabled` to `false` for the run — see [`memory`](runtime.md#memory--persistent-repo-scoped-memory). |
| `supervisor.role_file` | string | `"roles/supervisor.md"` | No path traversal (`..`/absolute). | The observe-lens prompt. Never loaded when the cadence resolves to `none`. |
| `supervisor.provider` | `codex` \| `claude` \| null | `null` / install: pinned to the primary | Must be in `agents.allowed` when set. | `null` inherits the global primary; pin it so the phase models reach a provider that accepts them. |
| `supervisor.observe.mode` | `all` \| `selected` \| `events` \| `none` | `events` / install: `events` | A flow may only narrow it (see below). | How often a completed step is worth an LLM note. See the table under it. |
| `supervisor.observe.triggers` | list of `rework` \| `failure` \| `fallback` | all three | Closed set; an unknown name is rejected. | Narrows which deviations count under `events` — e.g. `[failure]` to be notified of failures only. |
| `supervisor.observe.include_nodes` | list of node ids | `[]` | — | The nodes observed under `mode: selected`; ignored in every other mode. |
| `supervisor.observe.model` | string \| null | `null` / install: the primary's model | Passed through unverified; a vendor/primary mismatch warns. | `null` = the resolved provider's default. The **cheap** one: this phase is advisory and can fire on every step of a deep fix loop. Also governs the once-per-task skill proposal. |
| `supervisor.observe.reasoning` | string \| null | `null` / install: `low` | Per-provider set (as providers, above). | `null` = the resolved provider's default. Capped to `high` in code even if you set a max tier. |
| `supervisor.finalize.model` | string \| null | `null` / install: the primary's model | As above. | The turn that writes `summary.md` — the pull-request body, and the only part of a long run most readers see. Worth more than `observe`. |
| `supervisor.finalize.reasoning` | string \| null | `null` / install: `high` | Per-provider set. | `null` = the resolved provider's default. A max tier (`xhigh`/`max`) is capped to `high` when the turn is structured. |
| `supervisor.handoff.model` | string \| null | `null` / install: the primary's model | As above. | The subtask brief between regions of a decomposed task. Unused by a flow that never decomposes. |
| `supervisor.handoff.reasoning` | string \| null | `null` / install: `high` | Per-provider set. | `null` = the resolved provider's default. |

Keep every phase **at or below** the producer nodes' tier. This layer is advisory — it never routes, reworks, or blocks — so a model stronger than `agents.providers` inverts the budget: the reasoning that decides the deliverable gets the weaker one.

### What `observe.mode` costs

Ranked by how many calls the mode can produce — which is also the order a flow may narrow along.

| Mode | Observes | Use it when |
| --- | --- | --- |
| `none` | nothing | The flow's quality is already held by a blocking gate. `finalize` and the summary still happen. |
| `events` (default) | only a deviation: an evaluator sending work back (or accepting after exhausting its rework budget), a step whose run failed, a step that fell back to the non-primary provider | Almost always. Cost tracks what went wrong, not how long the run was. |
| `selected` | exactly `include_nodes` | You want notes on two named steps and nothing else. |
| `all` | every executed step | Debugging the run itself. This is what a long run pays for. |

`tool`, `checks` and the terminal `publish` node are never observed under **any** mode — their result is already a durable fact the finalize packet carries verbatim, so an advisory note about a pass/fail bought nothing and cost a full call per run.

**What a mode actually cost you is measured, not guessed.** Each run writes a `supervisor_usage` block into `.worc/logs/<task-id>/summary.json` (local only, never committed): calls, input, cached input, output, cost and provider wall time, as a total and split by job — `observe`, `finalize`, `handoff`, `skill`. Read the `observe` versus `finalize` split on your own flow before tuning this setting; the table above ranks the modes by how many calls they *can* produce, and that block tells you what they did produce.

Switching observations off and removing the layer are two different levels. `observe.mode: none` silences the per-step notes and keeps the synthesis; `enabled: false` removes the layer including that synthesis, and the pull-request body is then rendered from the same recorded facts the packet is built from — the same sections, without the interpretation.

Switching observations off does not cost you the summary. The finalize turn runs on a **fresh** session seeded by a deterministic packet of the run's facts (`.worc-io/<task-id>/supervisor/packet.json`) built from the recorded node runs and each node's own output — never from the observations — so its input is a few kilobytes regardless of how long the run was, a resumed task's summary is as complete as a first run's, and `mode: none` still produces a full PR body. Nor does a task that **failed** cost you one: every terminal other than `done` now leaves a summary too, falling back to the deterministic render of the same facts when the run stopped before the finalize turn could happen — alongside the `failure_report.json` / `stuck.md` pair and the stop reason in the log. The finalize turn is also handed every in-flow evaluator's recorded verdict and findings, so a gate that accepted **with** findings cannot be summarized as one that simply passed.

A flow may **narrow** the cadence in its own `supervisor.observe.mode` but never widen it: a flow declaring a broader mode than yours fails validation before any node runs, naming both modes (a flow is authored content and must not be able to spend more than you allowed). The packaged content flows ship `none`; `implementation` ships `events`. **When a flow narrows yours, the run says so once at the start**, naming both modes and — when the flow lands on `none` — the `triggers` that stop applying. That line exists because the loss used to be silent: an operator who set `events` with `triggers: [rework, failure, fallback]` and then ran a packaged content flow got run after run with a real provider fallback in them and not one observation, with nothing anywhere explaining it. The triggers are configured globally and discarded per flow, which is the part nobody expects. One consequence worth knowing in the other direction: because a flow that *states* `events` is asserting it needs deviation notes, setting your global mode to `none` is rejected for that flow rather than silently degrading it — narrow the flow's own copy if that is what you want. The rule does not apply at `enabled: false` — there is no cadence to widen, so a flow that declares `events` runs unchanged. That is why removing the layer is its own key rather than a global `mode: none`.

**Check the change before you queue work against it.** The rejection is fatal but cheap — it happens during flow resolution, before branch prep, so no provider runs and nothing is committed — yet the task has already been claimed by then and ends in terminal `failed`, which you have to re-queue by hand. `worc validate-flow` runs exactly the validator the engine runs at dispatch, read-only and without claiming anything, so run it after editing `observe.mode` on either side:

```bash
worc validate-flow --all && worc watch
```

Exit `0` = every checked flow is valid, `1` = at least one is not, `2` = flow name not found or the config would not load — so `&&` gates the run correctly. Two practical notes: the command needs a flow NAME or `--all` (a bare `worc validate-flow` is a usage error and exits `2`, which would block the chain for the wrong reason), and `--all` checks **every** file in `.worc/flows/`, so one unrelated broken flow there fails the gate. Name the flow you are about to run — `worc validate-flow implementation && worc watch` — when that is a problem.

There is no cap on what the layer may spend beyond the mode itself: no call budget, no token ceiling. The digest the finalize turn reads is bounded deterministically in code (8 000 characters), the mode bounds the frequency, and `all` is a deliberate operator choice.

