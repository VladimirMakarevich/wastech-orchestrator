---
name: analyze-task-run
description: Post-mortem a completed orchestrator task run on a TARGET repo to improve execution quality. Ingests everything the run left behind under the target's `.worc/` — the ledger, per-node logs, prompt-audit, rendered prompts, the committed diff, and the `state.db` audit tables — then cross-references the orchestrator's own source of truth (packaged flows, role prompts, config defaults, model/reasoning resolution) to find concrete improvement points. Produces a prioritized report that maps each finding to the exact lever to change — a role prompt, a flow node's provider/model/reasoning, a config key, the flow graph, or the prompt-building code. Read-only / analysis: it proposes changes, it does not apply them. Run it FROM the orchestrator repo, pointing at the target repo (or a task id). Use after a run finishes (success or failure) to tune prompts, configuration, and model selection.
---

# analyze-task-run

Analyze one finished orchestrator task run end to end and surface everything that could make the next run better: weak or ambiguous prompts, under- or over-powered model/reasoning settings, flow-graph friction, wasted fix loops, avoidable human-input stops, flaky checks, scope drift in the diff, and infra/retry noise. The deliverable is a **prioritized, evidence-backed report** where every finding names the precise lever to pull.

This is **analysis-only**. It reads artifacts and source; it does not edit prompts, config, flows, or code. When the user wants a finding applied, point them at the lever or offer to make that specific change in a follow-up.

## Where to run it (and why)

Run it **from the orchestrator repo** (`wastech-orchestrator`), passing the **target repo's path** (the repo the task executed on) as the argument.

The analysis has two halves and needs both:

- **Symptoms** live in the target repo's `.worc/` — logs, audit, artifacts, `state.db`. This is read by absolute path, so the target only needs to be reachable on disk (add it as an additional working directory if needed).
- **Levers** live in the orchestrator repo — role prompts, flow graphs, config defaults, per-node model/reasoning resolution, the prompt-building code, the provider adapters. A finding is only useful if it points at the file that fixes it, and that file is here.

Running from the target repo would give you symptoms with no levers — you'd be guessing where and how to fix. So: **orchestrator repo in, target path as argument.**

## Arguments / locating the run

The argument is a target locator. Resolve it in this order:

1. A **task id** (e.g. `task-023-…`) — combined with a target repo path, or if the orchestrator repo session already has the target as an additional working dir.
2. A **target repo path** — resolve its run home at `<target>/.worc/`.
3. A direct **`.worc/` path**.
4. **Nothing** — ask for the target path; then default to the most recent entry in `<target>/.worc/logs/completed.jsonl`.

Confirm which run you're analyzing (task id + final status + finished_at) before producing findings.

## The two sources of truth

| Symptom (in target `<target>/.worc/`) | Lever to fix (in orchestrator repo) |
| --- | --- |
| Active role prompt used by the run — `roles/*.md` | Packaged default `src/wastech_orchestrator/packaged/flows/roles/*.md` (change the default) **or** the target's own `roles/*.md` (change just this repo) |
| Active flow graph — `flows/*.yaml` (nodes, edges, per-node `provider`/`model`/`reasoning`/`when`/`enabled`) | Packaged `src/wastech_orchestrator/packaged/flows/*.yaml`; node schema `core/flow/schema.py`, parse `core/flow/snapshot.py`, validate `core/flow/validator.py` |
| Active config — `config.yaml` (providers, budgets, checks, decomposition, supervisor) | Defaults: `config/loader.py` (`_DEFAULT_*`), `install/config_writer.py`, `packaged/config.example.yaml`; schema `config/schema.py` |
| Model/reasoning that actually ran | Resolution: node value wins, else provider default — `providers/claude.py` (`request.model or config.model`, `request.reasoning or config.reasoning`), `_adapter_base.py` |
| Final rendered prompt the agent received | `rendered-prompt.md` is the literal text = role file + Core-built instruction + context footer (`_adapter_base.build_effective_prompt`). The editable part is the **role file**; the scaffolding is code |

Per-node `provider` / `model` / `reasoning` override is supported on `agent` and `evaluator` nodes (optional, default inherits the provider config). Use it when a finding is "this one node needs a stronger/cheaper model" rather than changing the global provider default.

## The artifact map (what to read, what it tells you)

Under `<target>/.worc/`:

- `logs/completed.jsonl` — the **ledger**: one JSON line per finished task (final_status, branch, pr_url, fix_iterations, auto_merged, validation_reason, decomposed, attempt, finished_at). Start here.
- `logs/<task-id>/`:
  - `task.normalized.json` — the normalized task spec the run actually used. The yardstick for scope and intent.
  - `validation_report.json` — gate verdict + completeness (was the spec weak / needing enrichment?).
  - `summary.json` / `summary.md` — the supervisor's whole-task summary. If it says "no provider-authored summary," the run produced no real output.
  - `current.diff` — the committed change. Judge it against `task.normalized.json` for scope match, over/under-delivery, unexpected files.
  - `stages/<node>/rendered-prompt.md` — the **final prompt** each node received. The primary input for prompt-quality findings.
  - `stages/<node>/run-<NNNNNN>/<seq>-<provider>/`:
    - `request.json` — what was sent (model, reasoning, permission, network, timeout, schema).
    - `result.json` — status, exit_code, error (e.g. `process_crashed`), usage (tokens/cost), session id, structured_output.
    - `events.jsonl` / `stdout.log` — the agent's turn-by-turn behavior. Read these when an agent looped, stalled, misread the task, or asked for human input.
    - `stderr.log`, `output-schema.json`.
  - `<NNNNNN>-<node>.json` — a node's structured result (e.g. the planning plan / subtasks).
  - `prompt-audit/` — present only when `config.prompt_audit: true`: `timeline.jsonl` (chronological per-node prompt + provider/model/attempt/fallback/status) and `<NNNNNN>-<node>.json`. If absent, note it as a **data gap** and recommend enabling it.
  - `publish/terminal-cleanup.json` — branch/cleanup outcome.
- `exchange-seals/<task-id>/seal-<NNNNNN>/` (WRI-007) — the sealed, checksum-verified snapshot of the agent-readable **exchange** as it was at each terminal, with a `manifest.json` (the in-repo `.worc-io/<task-id>/` is removed at terminal, so this is where a terminal task's curated agent-facing plan/diff/findings live). Read the newest `seal-*` to see exactly what the agent last saw. `exchange-quarantine/<task-id>/<NNNNNN>/` holds a tree quarantined because mutation detection flagged an agent-side change (`evidence.json` = expected vs observed) — a signal the agent tried to write the read-only surface.
- `state.db` (SQLite) — the **audit gold**. Query it directly (`sqlite3`):
  - `tasks` — full final row (status, `test_fix_cycles`, `review_fix_cycles`, `fix_iterations`, `decomposition_*`, `current_node`, `refinement_ran`).
  - `node_runs` — per node: status, outcome, **stage_attempts**, provider_used, **route_fallback**, error_class, commit_sha_before/after, skipped, skip_reason, timings.
  - `provider_attempts` — per attempt: provider, attempt, status, **error_class**, exit_code, timings (retries, fallbacks, crashes).
  - `evaluations` — evaluator verdicts: kind (review / test_quality), verdict, **findings_json** (why it reworked).
  - `check_runs` — checks: command, exit_code, **timed_out**, passed, log_path (failures, flakiness, coverage).
  - `node_lineage` / `editing_lineage` — session reuse; `publish_operations`; `subtasks`.

## How to run

1. **Locate & frame the run** — resolve the locator, read the ledger entry, state the task id / final status / attempt / finished_at.
2. **Reconstruct the timeline** — walk `node_runs` + `provider_attempts` (and `prompt-audit/timeline.jsonl` if present) to get the actual path through the flow: which nodes ran, retried, fell back, looped, were skipped.
3. **Ingest outcome & cost** — fix loops (`test_fix_cycles`, `review_fix_cycles`, `fix_iterations`), attempts, decomposition, token usage / cost from `result.json`.
4. **Read the prompts** — for each agent/evaluator node, read `rendered-prompt.md` and compare against the agent's behavior (`events.jsonl`) and output (`result.json`). Locate the editable role file behind it.
5. **Judge the diff** — `current.diff` vs `task.normalized.json`: did it do what was asked, no more, no less?
6. **Cross-reference the levers** — for every weakness, find the exact role file / flow node / config key / source location that controls it, in the target copy and/or the packaged default.
7. **Score model/reasoning fit** — per node, was it under-powered (loops, rework, timeouts, confusion) → bump model/reasoning; or over-powered (trivial deterministic node on high reasoning) → lower it / cheaper model. Recommend per-node overrides where appropriate. When you name a specific model id, verify it with the `claude-api` skill — don't guess ids or pricing.
8. **Write the report** (below), then stop.

## Analysis dimensions (what to look for)

- **Outcome & cost** — failed/looped/needed-human vs clean; wasted fix iterations; token/cost hot-spots; nodes that dominate wall-time.
- **Prompt quality** — ambiguity, missing constraints, no acceptance criteria, weak output-schema guidance, prompts that invited scope drift or repeated HITL. The lever is almost always a **role file**.
- **Model / reasoning fit** — per node, too weak (rework/loops/timeouts) or overkill (cost with no benefit). Lever: per-node `model`/`reasoning` override, or the provider default.
- **Flow-graph friction** — nodes that should be reordered, gated (`when`), disabled per-task (`enabled: false`), or added (e.g. a `testing_quality` evaluator); edges/loops that thrashed.
- **Fix loops** — high `review_fix` / `test_fix` counts point upstream: weak planning/implementation prompt, or noisy checks. Find the real cause, not the symptom node.
- **Evaluator verdicts** — `evaluations.findings_json`: recurring rework reasons are prompt or spec signals.
- **Checks / testing** — failures, `timed_out`, flakiness, command_set coverage vs the diff's paths.
- **HITL** — each human_input request: necessary, or avoidable with a clearer prompt / richer task spec?
- **Fallbacks / retries / infra** — `provider_attempts.error_class` (process_crashed, auth, transient): config/env/auth problems vs genuine agent failures. (e.g. a `process_crashed` + "Not logged in" is an env/allowlist issue, not a prompt issue.)
- **Validation & spec** — `validation_report.json` completeness: was the task under-specified going in? Lever may be the refinement prompt or task-authoring guidance, not the implementation prompt.
- **Diff vs intent** — scope creep, gold-plating, missed requirements, files touched that the task didn't imply.
- **Supervisor & summary** — did the advisory layer catch anything; is the summary substantive?

## Output

Report concisely, in the user's language. Lead with a 2–3 line verdict (did the run go well; the single biggest improvement). Then:

- **Run frame** — task id, final status, attempt, fix loops, decomposition, cost; the actual path through the flow.
- **Findings**, ranked by impact. Each one:
  - **category** (prompt / model / reasoning / config / flow / checks / HITL / infra / spec / diff),
  - **severity** and **confidence**,
  - **evidence** — the artifact path or `state.db` query it rests on (quote the telling lines),
  - **root cause** — what actually went wrong, not just where,
  - **recommended change** — the exact lever: file (target copy and/or packaged default) + what to change; for model/reasoning, the node and the new value,
  - **scope** — target-only (one repo) vs orchestrator default (every repo),
  - **expected impact** — what the next run gains.
- **Data gaps** — what you couldn't assess and why (e.g. `prompt_audit: false` → no per-prompt audit; recommend enabling it before the next run).
- **What's already good** — patterns worth keeping, so the user knows you checked.

Offer to persist the actionable items to `docs/backlog/follow_ups.md` (the repo's deferred-work tracker), and to apply any single finding on request. Then **stop**.

## What not to do

- **Don't edit** prompts, config, flows, or code — analysis only. Recommend; don't apply.
- **Don't leak secrets.** Artifacts and env are allowlisted by design, but if you spot a secret in a log, report the leak as a finding — never reproduce its value.
- **Don't guess model ids or pricing** — confirm with the `claude-api` skill before recommending a model change.
- **Don't violate the hard invariants** when proposing fixes (provider abstraction, orchestrator-only commit/push/PR, no per-model allowlist, the state machine). A recommendation that breaks `.agents/rules/` is not a valid finding.
- **Don't invent problems.** "This run was clean; the one worthwhile tweak is X" is a good result — say it plainly. Tie every finding to evidence.
- **Don't conflate infra and prompt failures** — a crash/auth/transient error is an env/config finding, not a prompt one.
