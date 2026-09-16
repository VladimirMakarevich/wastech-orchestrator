# `content_translate` run 1 — close the concurrency hole, make the loop guards honest, complete the audit record

Status: **inventory, evidence-backed** Date: 2026-09-16 Owner: Vladimir Makarevich

## Why this exists

Two content tasks ran against a real repository overnight and both reached `done` with an open pull request. The deliverables are correct — every acceptance criterion of both tasks verifies independently. What the run exposed is not a quality failure of the flow but sixteen defects in the **orchestrator around it**: a concurrency hole that corrupted the audit tables, two budget mechanisms that do not do what the flow file declares, a terminal state that ships a success carrying a failure report, and an audit record missing exactly the fields a post-mortem needs.

The items are ordered, because two of them compound: the concurrency hole (P0.1) is what produced the contradictory rows this document cites elsewhere, so it is fixed first or every later investigation reads noise.

Findings that belong to the **target repository's** own flow and tools — the critic role that emits unfixable findings, the project tool that never populates `findings`, the gate that hides its measurements from the fixer — are not here. They are the operator's copies, they live in that repository, and they are tracked with it. This document carries only what ships from `src/`.

## Run frame

|  | `en-adapt-01` | `en-adapt-01a` |
| --- | --- | --- |
| Flow / final status | `content_translate` / `done`, PR #1 | `content_translate` / `done`, PR #2 |
| Attempt | 1 | 7 |
| `fix_iterations` | 2 | 10 |
| Wall clock | 42.8 min | 11 h 56 min (10 h 29 min of it parked) |
| Provider attempts | 10 | 27 |
| Input / output tokens | 13.8 M / 186 k | 23.4 M / 271 k |
| Recorded cost | $15.81 | $22.40 |

Recorded cost is **Claude only**. The 13 Codex attempts across both tasks (8.4 M input tokens, 23% of the run's input) carry no cost at all — see P1.7.

Path actually taken by `en-adapt-01a`, from `node_runs`: `brief → adapt_en → form_gate ✗ → form_fixing → form_gate ✗ → form_fixing → form_gate ⏸ stalled` (parked 10.5 h) `→ form_gate ✓ → fidelity_critic ×10 ↺ fidelity_fixing ×6 → [no-progress guard breaks the loop] → voice_critic ✗ → voice_fixing → voice_critic ✓ → constraints_en ✗ → constraint_fixing → constraints_en ✓ → bookkeeping → publish`. No `route_fallback` fired on either task; all 37 attempts went to the node's primary provider.

## Priorities

| Item | What it costs today |
| --- | --- |
| [P0.1](#p01--refuse-a-second-worc-run-against-the-same-clone) | Two processes wrote one task: two lost `evaluations` rows, a `node_runs` row with contradictory fields, a duplicated `provider_attempts` key, a stolen PID file |
| [P0.2](#p02--stop-calling-an-actionable-tool-failure-without-findings-and-stop-voiding-the-declared-budget-in-silence) | 10 h 29 min parked overnight on a two-line failure the flow had budget to fix six times |
| [P0.3](#p03--give-the-no-progress-guard-a-signal-that-survives-a-restart) | 25.6 provider-minutes and 8.87 M tokens on a loop that could not converge, because four restarts reset the only guard that could see it |
| [P0.4](#p04--a-recovered-task-must-not-ship-as-done-carrying-a-failure-report) | The ledger reports `final_status: done` beside `failure_report: …/failure_report.json`; the log dir keeps `stuck.md` and a `parked-*` copy |
| [P1.5](#p15--keep-requestjson-at-the-default-artifact-level) | The one artifact carrying `argv` and the permission profile is deleted by default, while 14.2 MB of raw stdout is kept |
| [P1.6](#p16--record-the-model-that-actually-ran-in-the-audit-tables) | Cost cannot be split by model; with `prompt_audit: false` (the default) the model is recorded nowhere |
| [P1.7](#p17--give-codex-attempts-a-cost-even-an-estimated-one) | The run's headline cost is understated in proportion to how much Codex the flow uses |
| [P1.8](#p18--put-tool-output-and-evaluator-findings-in-the-supervisor-packet-and-keep-its-paths-alive) | The supervisor wrote the PR body without seeing what any gate said; every exchange path in the saved packet is dead |
| [P1.9](#p19--do-not-mint-an-empty-quarantine-bundle-and-do-not-lose-the-pointer-to-it) | An evidence directory containing no evidence, kept forever; `exchange_contaminated` back to `0` with quarantine on disk |
| [P1.10](#p110--close-the-node_runs-row-when-the-merge-flow-aborts) | A `done` task holds two `running` node rows with no `finished_at`, permanently |
| [P2.11](#p211--say-out-loud-that-the-task-packet-is-frozen) | The operator corrected the task mid-run; the run finished on the stale text and said nothing |
| [P2.12](#p212--apply-the-settled_own_file-guard-on-the-run-path-too) | `duplicate_task_id` moved the operator's own task file into the private home without printing where |
| [P2.13](#p213--let-a-repository-add-governance-paths-never-remove-them) | Half a governance edit reached the pull request unannounced |
| [P2.14](#p214--announce-a-narrowed-observe-cadence-at-warning) | `observe.mode: events` with three triggers was configured, silently discarded, and never fired |
| [P2.15](#p215--audit-hygiene-four-small-record-defects) | `skip_reason` carries an abort reason, `stuck.md` prints a Python repr, the stuck report has no provider evidence, `artifacts.path` mixes conventions |
| [P2.16](#p216--say-what-the-terminal-kept) | "Logs did not get cleaned" is the operator's correct observation of documented behavior nobody told them about |

---

## P0.1 — refuse a second `worc run` against the same clone

### Problem

Between 11:23 and 11:29 UTC two orchestrator processes were live against one `state.db` and one working copy. The audit tables record the collision directly:

- `node_runs` 21 (`11:21:50 → 11:25:29`) and 22 (`11:24:29 → 11:27:43`) are **both** `fidelity_critic` on the same task and they overlap.
- `provider_attempts` 18 and 19 share `node_run_id = 24`, `provider = codex` and `attempt = 1`. One is `aborted / cancelled`, the other `succeeded`.
- `node_runs.id = 24` ends as `status='succeeded', outcome='rework', error_class=NULL, skipped=0` while its `skip_reason` reads `node 'fidelity_critic': exchange mutated during a provider attempt (…)`. One process's reconciler closed the row as cancelled; the other process then overwrote status, outcome and error class, leaving the abort reason orphaned in the row.
- `evaluations` has no row for `node_runs` 22 or 24, although both carry `outcome='rework'`. Two verdicts were lost.
- `logs/completed.jsonl` carries a `duplicate_task_id` rejection timestamped `11:23:36`, inside the overlap window — the second process announcing itself.

### Root cause

> **Corrected 2026-09-16 against `dev`.** An earlier draft of this section said `cmd_run` never checks whether an executor is already recorded. That is not true: [`cmd_run`](../../../src/wastech_orchestrator/cli.py) calls `_executor_owner(config)` — which probes the daemon PID file **and** the runner file — and refuses with a non-zero exit before building anything. The guard landed in `f12a3ad` (2026-09-03) and ships in `v0.12.0a1` onward, so the run that produced this document had it. The defect is not a missing check; it is a **non-atomic** one. Do not implement the old step 1 — it is already there.

The check and the write are two separate operations with real work between them:

```python
owner = _executor_owner(config)          # cli.py — the check
if owner is not None:
    return 1
orchestrator = build_orchestrator(...)   # opens state.db, resolves the layout, scans dependencies
...
runner_path = process_control.runner_file_path(worc_home_for(config))
process_control.write_pid_file(runner_path)   # the write, unconditional
```

Two processes that pass the check inside that window both proceed, and `write_pid_file` **overwrites** whatever is there — after which `worc stop` targets the wrong process and `status` / `list` / `top` report the wrong executor. `cmd_watch` has the same shape and a **wider** window: its checks sit at `cli.py:3851`/`:3860`, while `write_pid_file(pid_path)` runs at `:3902` — after `build_orchestrator`, after the console print, and inside the `with controller` block.

Which of the two shapes produced this run's overlap is not recoverable: no process-level log was kept, and the per-task log directories hold no executor identity. Both are in scope, because both are the same defect.

The task-level `duplicate_task_id` gate does not help either: it governs claiming a **pending file**, and `resume()` runs before claiming, so the second process resumed the in-flight task before the gate ever spoke.

### Fix steps

1. ~~In `cmd_run`, run the same two checks `cmd_watch` runs.~~ **Already done** — see the correction above. Verify and move on.
2. Make the claim atomic: `write_pid_file` acquires the marker exclusively (`O_EXCL`) and refuses when a **live** record already holds it, so the guard cannot be lost to a race between the check and the write. A stale record (dead PID, or alive but recycled) is still reclaimed, which is what keeps a crash from refusing every later command forever.
3. Apply it on both paths — `cmd_run`'s runner file and `cmd_watch`'s daemon PID file. `cmd_watch`'s window is the wider one.
4. Regression tests, one per path: two `run` invocations over one `worc_home`, second returns non-zero and leaves the first's marker intact; the same for two `watch` invocations. The `run` case has no coverage today — [`test_cli_run_liveness.py`](../../../tests/test_cli_run_liveness.py) asserts that `rerun`, `watch`, `finalize` and `prs` refuse under a live `run`, but never that a second `run` does.

### Scope and expected impact

Orchestrator default, every repository. Removes a class of silent audit corruption that is otherwise indistinguishable from an engine bug — the contradictory row in this run cost real time to explain.

---

## P0.2 — stop calling an actionable tool failure "without findings", and stop voiding the declared budget in silence

### Problem

At 00:52 UTC the night run parked in `manual_action_required` and stood for 10 h 29 min. The `form_gate` tool node had failed three times; its stdout on runs 2 and 3 is byte-identical (the sha256 pair in the quarantine manifest confirms it) and perfectly actionable:

```json
{
  "outcome": "fail",
  "data": {
    "mode": "en",
    "violations": {
      "…_en.md": [
        "page '1A.1.2 No ready answers here' is 899 chars — exceeds the hard maximum 800"
      ]
    }
  }
}
```

The operator was told:

> tool node 'form_gate': the tool 'check_journey' repeated an identical failure **without findings**; task parked before another fix iteration could be charged

The flow had declared `budgets: {form_fix: 6}`. Two rounds were spent.

### Root cause — two defects, one mechanism

[`_is_repeated_no_finding_failure`](../../../src/wastech_orchestrator/core/flow/nodes/tool.py) parks on the second identical failure when `contract.findings` is empty, and [`_findings_from`](../../../src/wastech_orchestrator/core/flow/nodes/tool.py) reads only a **top-level `findings` array**. A tool that reports through `data` — which the contract explicitly permits, since `parse_tool_output` documents `data` as a first-class field — therefore looks finding-less on every failure it will ever produce.

1. **The message is wrong about the facts.** "Without findings" reads as "the tool said nothing", and the operator goes looking for a broken tool. The true statement is narrower: the tool produced no _structured_ findings, so the fixer had no typed handle. A tool whose entire output is `data` is a supported shape, not a malfunction.
2. **A declared budget is voided without a word.** The stall detector sits above `budgets`, knows nothing about it, and takes precedence. A flow author writes 6 and gets 2, with no diagnostic at authoring time, at validation time, or at runtime.

### Fix steps

1. Reword the exception: name the missing structured channel, not the tool's silence, and quote the head of the repeated stdout so the operator sees what actually repeated.
2. Add a validator or `preflight` warning: a `kind: tool` node whose tool returns `fail` without a top-level `findings` array makes the stall detector authoritative and its declared loop budget unreachable. Say so once, at the place the budget is written.
3. **Decided 2026-09-16 — three-tier.** `findings` present → the detector stays off, as today. `findings` absent but `data` non-empty → the failure counts as actionable and the detector becomes a **backstop**: it fires at the node's declared loop budget, not on the second repeat. Neither present → park on the second repeat, as today. A tool that reports through `data` was held to a stricter standard than one that reports through `findings`, which is the opposite of the intent. Note that the constant this step originally named, `_STALL_REPEAT_LIMIT`, does not exist: the second-occurrence threshold is hard-coded in `_is_repeated_no_finding_failure` via the in-memory `_last_no_finding_failure` map. Nodes with no declared budget keep the threshold of 2.
4. Document the `findings` contract in the operator-facing tool guidance, next to `outcome` — it is currently discoverable only from the source.

### Scope and expected impact

Orchestrator default. Directly buys back unattended overnight hours: this run lost 10.5 of them to a failure the flow was configured to fix.

---

## P0.3 — give the no-progress guard a signal that survives a restart

### Problem

`fidelity_critic` on `en-adapt-01a` ran eleven times and returned `accept` zero times. The first pass produced one real finding, which was fixed. From the second pass to the eleventh the finding was the same one, near-verbatim, and it was unfixable by construction: its own `fix` field asked for the **task file and the brief** to be changed, and those are read-only inputs to the node. `evaluations` rows 7–13 are the same paragraph seven times over.

The loop ended on the no-progress guard, not on a verdict — `failure_report.json` records `"limit_exhausted": "no_file_change"` — after 25.6 provider-minutes and 8.87 M input tokens, which is 31% of that task's provider time.

### Root cause

[`_check_stall`](../../../src/wastech_orchestrator/core/flow/engine.py) breaks a loop after `_STALL_NO_CHANGE_LIMIT` consecutive rework charges that leave the tree unchanged. Its state is two plain dicts on the engine instance:

```python
# EXPERIMENTAL(no-work-infra). No-effective-work stall guard (transient, never persisted — a
# reset on resume is desired).
self._stall_fp: dict[str, str] = {}
self._stall_streak: dict[str, int] = {}
```

The run restarted at least four times (gaps in `node_runs` at `11:29:13→11:32:28`, `11:41:47→11:43:50`, `11:53:30→11:56:55`). Each restart zeroed the streak. The loop counters beside it **are** persisted, in `tasks.flow_run_counters`. So two budget mechanisms guard the same loop with different lifetimes, and the shorter-lived one is the only one that can recognise a finding that will never close.

The docstring's claim is defensible for the failure it was written against — an agent that emits tokens without editing, within one process. It is not sufficient for a loop that outlives the process.

### Fix steps

1. Add a second, persistent signal: a hash of the evaluator's `findings_json` per `(task_id, node_id)`, with a consecutive-repeat counter stored beside the loop counters. Break the loop when the same findings text repeats N times, independently of process lifetime and of whether the tree changed.
2. Keep the existing in-memory tree-fingerprint guard as-is; the two catch different shapes and the new one does not replace it.
3. Record which guard fired in `failure_report.json` (`limit_exhausted: repeated_findings` vs `no_file_change`) — the operator needs to know whether the agent did nothing or the critic asked for the impossible.
4. Regression test: an evaluator returning identical findings across a simulated restart boundary must still terminate.

### Scope and expected impact

Orchestrator default. On this run it would have ended the loop on the third pass instead of the eleventh — roughly 20 provider-minutes and 7 M tokens saved on one task.

---

## P0.4 — a recovered task must not ship as `done` carrying a failure report

### Problem

`tasks.failure_report_path` for `en-adapt-01a` points at `logs/en-adapt-01a/failure_report.json` while `tasks.status` is `done`. The ledger copies it verbatim, so `completed.jsonl` holds a line reading `"final_status": "done"` beside `"failure_report": "…/failure_report.json"`. The log directory of a successful task keeps three artifacts of failure:

```
logs/en-adapt-01a/failure_report.json          16 KB
logs/en-adapt-01a/stuck.md                     16 KB
logs/en-adapt-01a/parked-01a_…_en.md          3.4 KB
```

### Root cause

The field is cleared only when a task row is reset ([`state_store.py`](../../../src/wastech_orchestrator/state_store.py), the `reset` path writes `failure_report_path=None`). At the terminal, [`_finish`](../../../src/wastech_orchestrator/core/orchestrator.py) reads `if row is not None and not row.failure_report_path:` and writes a report when one is **missing** — it never removes one that is present.

For a non-blocking evaluator this is the ordinary path, not an edge case: the stall guard writes the report, the flow continues because the lens does not block, and the task succeeds with the pointer still set. Every `content_translate` run with a non-converging lens will look like this.

### Fix steps

1. On the transition to `DONE`, either clear `failure_report_path` or move the artifacts to `recovered-*` names and clear the pointer.
2. Prefer the second: the information is worth keeping. Add a ledger field (`recovered_from_stuck: true` plus the loop name) so `worc list` can distinguish "clean" from "recovered" without the reader having to interpret a stale path.
3. Regression test: a task that trips a non-blocking loop guard and then completes must end with a `done` ledger line that does not advertise a failure report.

### Scope and expected impact

Orchestrator default. The ledger is what `list` / `status` and any external reader are built on; this is the one place it currently states something untrue about an outcome.

---

## P1.5 — keep `request.json` at the default artifact level

### Problem

No `request.json` exists anywhere in this run (`find logs -name request.json` is empty) although `logging.artifacts` was at its shipped default, `standard`.

### Root cause

[`providers/artifacts.py`](../../../src/wastech_orchestrator/providers/artifacts.py):

```python
_ARTIFACT_KEEP: dict[str, set[str]] = {
    "minimal":  {RESULT_FILENAME},
    "standard": {RESULT_FILENAME, STDOUT_FILENAME, STDERR_FILENAME},
}
```

`REQUEST_FILENAME` is written by `write_request_artifact` and then pruned at every level except `full`. Two places in the codebase depend on it existing: [`ledger.py`](../../../src/wastech_orchestrator/ledger.py) calls it "the only artifact carrying the permission profile and full `argv`", and [`observability.py`](../../../src/wastech_orchestrator/core/flow/observability.py) describes cross-checking the prompt-audit's effective model against it.

The retention priority is inverted. This run kept 14.2 MB of raw provider stdout — 83% of the whole `.worc/` footprint — and discarded the ~2 KB per attempt that answers "with which flags and under which permission profile was the CLI actually launched". For a product whose central invariant is the permission ceiling, that is the wrong file to drop.

### Fix steps

1. Move `REQUEST_FILENAME` into the `standard` set, and consider `minimal` too — `result.json` is not interpretable without knowing what was requested.
2. State the retention sets in the operator guide's `logging.artifacts` row, which currently lists stdout/stderr and does not mention the request at all.

### Scope and expected impact

Orchestrator default. Kilobytes per attempt; restores the only launch-time security record.

---

## P1.6 — record the model that actually ran in the audit tables

### Problem

`result.json` carries `status, provider, node_id, attempt, exit_code, started_at, finished_at, final_message, structured_output, usage, normalized_usage, session_id` — and no model. `provider_attempts` has no model column. So `state.db` cannot answer "what did this $38.21 buy, per model".

The value **is** recorded, in `prompt-audit/timeline.jsonl`, and recorded well — effective and configured side by side:

```json
{
  "node_id": "bookkeeping",
  "model": "claude-sonnet-5",
  "reasoning": "xhigh",
  "model_configured": "claude-sonnet-5",
  "reasoning_configured": "xhigh"
}
```

That file is how this review confirmed resolution was correct on every node of both tasks. But `prompt_audit` defaults to **false** ([`config/schema.py`](../../../src/wastech_orchestrator/config/schema.py): `prompt_audit: bool = False`; the packaged example ships `prompt_audit: false`). The operator here enabled it by hand. Out of the box, the model that ran a node survives only in the CLI's `init` event inside `stdout.log`, which itself disappears at `logging.artifacts: minimal`.

### Fix steps

1. Add `model` and `reasoning` (effective values) to `result.json`.
2. Add `provider_attempts.model` and `provider_attempts.reasoning`, written from the same source as the prompt-audit record so the two cannot disagree.
3. Keep the prompt-audit record as-is; it serves a different reader. The point is that the mandatory accounting table must not depend on an optional reading aid.

### Scope and expected impact

Orchestrator default; schema touch (column add, version bump). Makes per-model cost and "did my per-node override apply" answerable by query rather than by grep.

---

## P1.7 — give Codex attempts a cost, even an estimated one

### Problem

| Provider | Attempts | Input tokens | Recorded cost |
| -------- | -------- | ------------ | ------------- |
| claude   | 24       | 28.8 M       | $38.21        |
| codex    | 13       | 8.4 M        | —             |

[`providers/claude.py`](../../../src/wastech_orchestrator/providers/claude.py) sets `cost=coerce_usage_cost(total_cost_usd)`. [`providers/codex.py`](../../../src/wastech_orchestrator/providers/codex.py) sets no cost anywhere. The orchestrator therefore reports $38.21 as the price of a run in which Codex handled 23% of the input tokens — and on `en-adapt-01a` Codex was the single heaviest node (`fidelity_critic`, 6.5 M tokens).

The understatement scales with how much of a flow runs on Codex, which makes the number worse precisely where it matters most.

### Fix steps

1. If the Codex CLI reports no USD, compute cost from normalized token counts and mark the provenance: `usage_cost_source: 'estimated' | 'reported'` on `provider_attempts`.
2. Until that lands, make the summary say what it does not know: "Codex cost not accounted (13 attempts, 8.4 M input tokens)". A silent zero is worse than an honest estimate and much worse than an honest gap.
3. The price table is provider data, not core knowledge — it belongs in the adapter, behind `AgentProvider`, like every other provider-specific fact.

### Scope and expected impact

Orchestrator default. Cost reporting stops being systematically low.

---

## P1.8 — put tool output and evaluator findings in the supervisor packet, and keep its paths alive

### Problem

The saved packet for `en-adapt-01a` holds 32 steps. Agent steps carry a full `message`. Tool steps carry nothing but timing and verdict:

```json
{
  "kind": "tool",
  "node": "form_gate",
  "outcome": "fail",
  "status": "failed",
  "started_at": "…",
  "finished_at": "…"
}
```

Evaluator steps are the same shape. `findings_path` appears **once**, pointing at the last evaluator (`voice_critic/run-000039/findings.json`); the ten `fidelity_critic` finding sets are absent. `checks` is `{"failed": [], "passed": [], "skipped": []}` — that field reflects the config's `checks` section, and tool-node verdicts never reach it.

The supervisor wrote an accurate PR body only because the fixing agents happened to quote the findings in their own messages. It noticed the gap and wrote it into the pull request: _"Сохранённых stdout у проходного прогона в дереве артефактов нет, вердикт взят из пакета"_ — about a file that exists and is registered in `artifacts` (id 72). It is simply not in the packet.

Two further defects in the same artifact:

- **The publish step is `running`.** The packet is built before publish completes, so the last step reads `{"kind":"publish","status":"running","finished_at":null}`, and the finalizer dutifully reported that fact **in the pull request body** ("Шаг публикации в пакете ещё числится запущенным"). Internal state leaked into text people read.
- **Every exchange path in the saved packet is dead.** `findings_path` and `changes.diff_path` point into `.worc-io/<task>/…`, which terminal cleanup removes. The packet is preserved; its references are not.

### Fix steps

1. Include a tool step's `data` (and a bounded head of its stdout) in its packet step.
2. Include each evaluator step's findings, or a path to them that survives cleanup — one `findings_path` for the whole run is not enough for a flow with two lenses and ten rework rounds.
3. Either exclude an unfinished `publish` step from the packet or label it so the finalizer knows it is expected and does not narrate it.
4. When persisting the packet, rewrite exchange paths to their private copies under `logs/<task>/stages/…`.

### Scope and expected impact

Orchestrator default. This is the input the whole-task summary is built from, and it is the artifact a post-mortem reads first.

---

## P1.9 — do not mint an empty quarantine bundle, and do not lose the pointer to it

### Problem

```
runs/exchange-quarantine/en-adapt-01a/000001/   evidence.json + tree/
runs/exchange-quarantine/en-adapt-01a/000002/   evidence.json
```

The second bundle:

```json
{
  "expected_manifest": null,
  "format": 1,
  "observed_changes": [],
  "task_id": "en-adapt-01a"
}
```

No manifest, no observed changes, no `tree/`. Evidence containing no evidence — and [`runs_retention.py`](../../../src/wastech_orchestrator/runs_retention.py) correctly excludes the quarantine root from automatic reclamation, so it is kept forever.

Separately, `tasks.exchange_contaminated` is now `0` for both tasks while the quarantine bundles sit on disk. The database no longer points at the incident; the only link left is the directory name.

### Root cause

[`_terminal_exchange`](../../../src/wastech_orchestrator/core/orchestrator.py) enters the quarantine branch on `if contaminated or mutation is not None`. On the second terminal the stored `contaminated` flag was still set from the first, and `mutation` was `None`, so `expected=None` and `observed=()`. [`quarantine_contaminated`](../../../src/wastech_orchestrator/core/flow/exchange_seal.py) then creates the directory and writes evidence unconditionally, and only afterwards checks `os.path.lexists(task_dir)` — which is false, because the tree already moved into bundle `000001`. The result is `mkdir` plus a contentless JSON.

### Fix steps

1. Do not create a bundle when there is nothing to record: with `expected is None`, `observed_changes` empty **and** no live tree, log a `WARNING` and return.
2. Add `created_at`, `node_id` and attempt to `evidence.json`. It currently carries `task_id` and nothing else temporal, so the order of two bundles is recoverable only from directory mtime.
3. Do not reset `exchange_contaminated` without leaving a trace: either keep the flag, or write a `quarantine_refs` list on the task row so a finished task still names its own evidence.

### Scope and expected impact

Orchestrator default. Keeps the only never-reclaimed directory meaningful, and keeps the incident reachable from the database that recorded it.

---

## P1.10 — close the `node_runs` row when the merge flow aborts

### Problem

```
id 45  en-adapt-01  conflict_resolution  agent  running  started 12:39:11  finished NULL
id 46  en-adapt-01  conflict_resolution  agent  running  started 12:40:43  finished NULL
```

Two runs of `worc merge-task`, both stopped by the containment defect documented in [merge-task-exchange-containment.md](../merge-task-exchange-containment.md) — since fixed. Their node rows were never closed. `en-adapt-01` is `done` and permanently holds two open node runs with no `provider_used` and no `finished_at`.

### Root cause

`merge_task` converts the node-layer `NodeManualRequired` into `ManualActionRequired` at the merge seam and aborts the git merge cleanly. Nothing closes the `node_runs` row the node opened before raising. The transactional promise covers git; it does not cover the state store.

The general shape: any node that raises `NodeManualRequired` **before** its provider call, on a path that does not go through the task driver's terminal, leaves its row open. The merge flow is the one such path today, which is why it shows here.

### Fix steps

1. Close the open row on the way out of `_run_merge_flow` — status `aborted`, the raised reason, a `finished_at`.
2. Better, at the node layer: make the row's lifetime a context manager so no exit path can skip closing it.
3. Regression test beside the existing merge-task coverage: after an aborted merge flow, `node_runs` for that task holds no `running` row.

### Scope and expected impact

Orchestrator default. Belongs with the merge-flow work in [merge-flow-conflict-competence.md](../merge-flow-conflict-competence.md) — it is the state-store half of the same seam.

---

## P2.11 — say out loud that the task packet is frozen

### Problem

The frozen packet in the exchange is `task.md`, 5 051 bytes, sha256 `5520d3bb…`. The task file on disk at the end of the run is 5 352 bytes and contains a named ceiling exception the earlier text lacked. The operator corrected the task **while it was running**, and the run finished on the stale text — which is exactly the contradiction the critic then reported ten times (P0.3).

### Root cause

Freezing is correct and must stay: without it a run is not reproducible. What is missing is any signal. No log line, no warning, no line in the summary. The operator edits the task, observes nothing change, and concludes the agent is ignoring them.

### Fix steps

1. On every resume, compare the live task file's sha256 with the frozen packet's and, when they differ, emit a `WARNING`: the file changed after start, the run continues on the packet frozen at `<time>`, and `worc stop` plus a fresh start is what applies the edit.
2. Put the same sentence in `summary.md` when it fires, so it survives into the pull request.

### Scope and expected impact

Orchestrator default. One warning; removes a genuinely confusing silence.

---

## P2.12 — apply the `settled_own_file` guard on the `run` path too

### Problem

At 11:23:36 the second process rejected a pending file as `duplicate_task_id` and **moved the operator's own task file** to `.worc/tasks/rejected/en-adapt-01a.md` — under the private home, which the operator does not browse and agents cannot read. The destination was not printed.

### Root cause

The guard for exactly this exists — `settled_own_file`, whose call site in [`cli.py`](../../../src/wastech_orchestrator/cli.py) is commented _"re-running it would only reject it as `duplicate_task_id` and quarantine the operator's own file"_ — and it lives on the `watch` path only.

### Fix steps

1. Apply `settled_own_file` in `cmd_run` as well.
2. Print the quarantine destination on every reject, whichever path took it. The operator must learn where their file went from the command's output, not from `find`.

### Scope and expected impact

Orchestrator default. Small; removes a file-disappears-silently surprise.

---

## P2.13 — let a repository add governance paths, never remove them

### Problem

The run's diff touched `AGENTS.md` **and** `.rules/wastime-journey-rules.md`. The ledger reported `governance_changed: ["AGENTS.md"]` only.

### Root cause

[`instruction_bundle.py`](../../../src/wastech_orchestrator/core/flow/instruction_bundle.py):

```python
REPO_INSTRUCTION_NAMES = ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md")
GOVERNANCE_PATH_GLOBS  = (".agents/rules/**",)   # "A constant, never a config key"
```

The intent behind the comment is right — an operator must not be able to switch the notice off. But "not a config key" is being read as "not extensible", and a repository that keeps its rule set anywhere other than `.agents/rules/` gets a notice covering part of its governance and silently omitting the rest.

### Fix steps

1. Add a config key that **appends** to `GOVERNANCE_PATH_GLOBS` and cannot remove from it. Validation rejects any attempt to shrink the constant set.
2. Document it as additive in the config reference, with the reason stated where the constant is defined.

### Scope and expected impact

Orchestrator default. Restores the notice's completeness for repositories with their own layout, without weakening the guarantee.

---

## P2.14 — announce a narrowed observe cadence at `WARNING`

### Problem

`config.yaml` set `supervisor.observe.mode: events` with triggers `rework`, `failure`, `fallback`. The flow set `supervisor.observe.mode: none`. The flow wins, and across both tasks `provider_attempts.supervisor_function` holds only `finalize` — zero observations, against twelve `rework` events.

### Root cause

The mechanism to report this exists and is thoughtful: [`_announce_observe_cadence`](../../../src/wastech_orchestrator/core/orchestrator.py) prints the configured mode, the mode in force, and the dropped triggers, and its docstring explains precisely why the loss is worth naming. It logs at `info`. The config in force set `logging.level: warning`. Nobody saw it.

### Fix steps

1. Raise this one line to `WARNING` — it reports a configured setting being discarded, which is what that level is for.
2. Or print it on the console at task start regardless of log level, beside the flow name.

### Scope and expected impact

Orchestrator default. One level change; makes an existing good mechanism actually reach the operator.

---

## P2.15 — audit hygiene: four small record defects

**`skip_reason` carries two unrelated meanings.** `record_skipped_node_run` uses it for a deterministic `when`-false skip (`status='skipped'`, `skipped=True`). `reconcile_open_node_runs` reuses the same column for the **abort** reason of an orphaned run, with `skipped` left at 0. The result is `node_runs.id=24` from P0.1: `skipped=0`, `status='succeeded'`, and a `skip_reason` explaining an exchange mutation. Add `abort_reason` and stop overloading.

**`stuck.md` prints a Python repr.** Its "Last blocking review findings" section renders `{'severity': 'low', 'reason': "…", 'gating': True, 'fix': '…'}` — single quotes, capitalised `True`. This is a document written for a human or a handoff agent; findings belong as formatted list items with named fields.

**The stuck report has no provider evidence.** `failure_report.json` carries `"provider_attempts": []` although more than twenty attempts existed at the time, eight of them on the stuck node, and `"last_check_log": null` because that field reads `check_runs` and tool nodes do not write there. The report cannot answer "which provider call produced the repeated finding", which is the first question anyone asks it.

**`artifacts.path` mixes conventions.** 160 of 162 rows are absolute (`/Users/<user>/…`); the two `summary_md` rows are repo-relative (`tasks/done/en-adapt-01.summary.md`). One column, two conventions, and the absolute form embeds a home directory — which makes `state.db` non-portable and sits badly with the stored-path rule in [coding-style.md](../../../.agents/rules/coding-style.md).

**Ledger attempt numbers have gaps.** `completed.jsonl` for `en-adapt-01a` records attempts 1, 1, 1, 2, 5, 6, 7 — 3 and 4 never appear, and the first entry is duplicated. Whether this is P0.1's concurrency or a write path of its own, the ledger is currently not a complete record of attempts, and `list` / `status` are built on it. Determine which, then either fix it or document the ledger as "terminal transitions observed", not "every attempt".

---

## P2.16 — say what the terminal kept

`logging.clean_runs_on_success: true` worked exactly as documented: `control-bundles/`, `instruction-bundles/` and `exchange-seals/` for both tasks are empty. Per-task log directories are out of its scope by design and are reclaimed by `worc logs clean`.

The operator's report was "the logs did not get cleaned". They are right about the observation and the behavior is correct; what is missing is the sentence that connects them. `.worc/` stands at 17 MB, of which `logs/` is 16 MB and 14.2 MB is 36 raw `stdout.log` files.

Print one line at the successful terminal: runs cleaned, logs kept, the size, and the command that reclaims them. Mention `logging.artifacts: minimal` in the same breath — it would cut that 14.2 MB by roughly thirty times for an operator who does not need the raw event stream.

While there: the task branch is left behind locally after the pull request merges (`worc/1789517984-en-adapt-01a-en-01a` is still present). Terminal cleanup returns the tree to the base branch and deliberately does not delete the branch. For a repository that will take dozens of tasks, that wants either an option or a `worc branches clean`.

---

## What this run proves is working

Stated because none of it should be disturbed by the fixes above.

- **The publication contract held.** Zero attempts at `git commit`, `git push` or `gh pr create` across 37 provider calls. Every commit was the orchestrator's, with no agent-attribution trailer.
- **Read isolation held.** All seven permission denials were agents reaching for `.worc/`, refused at `security.disable_read_isolation: true`, exactly as `config.example.yaml` promises for the private set.
- **Model and permission resolution is correct and provable.** `prompt-audit/timeline.jsonl` shows `model` equal to `model_configured` on every node of both tasks — `claude-sonnet-5 / xhigh` for `bookkeeping`, `gpt-5.6-sol / high` for `fidelity_critic`, `claude-opus-5` elsewhere — and `permission_profile: workspace-write` produced `permissionMode: acceptEdits` in the CLI's own init event. No fallback fired.
- **Terminal cleanup is transactional.** `publish/terminal-cleanup.json` reads `completed: true, safe: true` on both tasks; the tree was clean and back on the base branch.
- **`publish_operations` is complete and idempotent.** Four rows per task (`code_commit`, `audit_commit`, `push`, `pr`), all `completed`, with fingerprints and `pushed_sha`; `audit_commit` correctly `noop` under `footprint.audit_on_branch: task` with no audit footprint.
- **Usage normalization is sound.** All 37 attempts carry `usage_delta_status='ok'` except the one cancelled attempt, and `usage_scope` correctly distinguishes Claude's `per_invocation` from Codex's `session_cumulative`.
- **The whole-task summary is a genuine product strength.** It named the open finding, the contested adaptation decisions, the exact `file:line` where a late edit diverged from what a lens had accepted, and its own blind spots.
