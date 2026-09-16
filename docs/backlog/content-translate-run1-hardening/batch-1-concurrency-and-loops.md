# Batch 1 — concurrency and loop guards

Covers **P0.1, P0.2, P0.3, P0.4**. One pull request against `dev`; three phases, three commits.

This batch is where the run's real cost sits: 10 h 29 min parked on a fixable failure, 8.87 M tokens on a loop that could not converge, and an audit trail two processes wrote over each other. Phase 1 is first because the corruption it removes is what makes every later investigation unreliable.

Back to the [campaign index](README.md); the evidence is in [inventory.md](inventory.md).

---

## Phase 1 — make the executor claim atomic

**Item:** P0.1. **Touches:** [`process_control.py`](../../../src/wastech_orchestrator/process_control.py), [`cli.py`](../../../src/wastech_orchestrator/cli.py), [`test_cli_run_liveness.py`](../../../tests/test_cli_run_liveness.py).

### What is wrong

Not a missing check — `_executor_owner` has guarded `cmd_run` since 2026-09-03. The check is **not atomic**: `build_orchestrator`, the layout resolution and the dependency scan run between it and `write_pid_file`, and the write overwrites whatever is there. `cmd_watch` has the same shape with a wider window — checks at `cli.py:3851`/`:3860`, write at `:3902`, after the console print and inside the `with controller` block. Two processes that clear the check in that window both proceed, and the loser's marker is silently replaced, after which `stop` targets the wrong process.

### What to build

1. `write_pid_file` grows an exclusive mode: create the marker with `O_EXCL`; on collision, read the existing record and re-probe it. A **live** holder is refused by raising a typed error carrying its PID; a **stale** one (dead, or alive but recycled per the start-time token) is reclaimed by replacing the file. Keep the atomic temp-file-plus-`os.replace` write for the reclaim path so a concurrent `stop` never sees a half-write.
2. Windows keeps its documented degradation: liveness cannot be probed, so marker **presence** is the signal and `O_EXCL` is the whole guard there. Branch explicitly, do not assume `os.kill`.
3. `cmd_run` and `cmd_watch` both claim through the exclusive path and turn the typed refusal into the message they already print — `_executor_owner`'s wording is good and stays. The pre-check stays too: it gives the better message in the common case, and the exclusive claim is the backstop for the race.

### Definition of done

- Two `run` invocations over one `worc_home`: the second returns non-zero, prints the live PID, and the first's marker is byte-identical afterwards.
- The same for two `watch` invocations.
- A stale marker (dead PID, or a live PID whose start-time token differs) is still reclaimed — a crash must not refuse every later command forever.
- The `run`-versus-`run` case is genuinely new coverage: today [`test_cli_run_liveness.py`](../../../tests/test_cli_run_liveness.py) asserts only that `rerun`, `watch`, `finalize` and `prs` refuse under a live `run`.
- Windows and POSIX paths are both exercised, per the cross-platform rule in [coding-style.md](../../../.agents/rules/coding-style.md).

---

## Phase 2 — the tool detector defers to a declared budget

**Item:** P0.2. **Touches:** [`nodes/tool.py`](../../../src/wastech_orchestrator/core/flow/nodes/tool.py), the flow validator, the packaged tool-node guidance under `src/wastech_orchestrator/packaged/`.

### What is wrong

`_findings_from` reads only a top-level `findings` array, so a tool that reports through `data` — a shape `parse_tool_output` explicitly supports — looks finding-less on every failure it will ever produce. `_is_repeated_no_finding_failure` then parks the task on the second identical failure, and the flow's `budgets: {form_fix: 6}` is voided with no diagnostic at authoring time, at validation time or at runtime. The operator is told the tool failed "without findings", which reads as "the tool said nothing" and sends them hunting a broken tool.

### What to build

1. **Three-tier threshold**, as decided:
   - top-level `findings` present → detector off (unchanged);
   - `findings` absent, `data` non-empty → the failure is actionable; the detector fires at the node's **declared loop budget**, not on the second repeat;
   - neither present → park on the second repeat (unchanged).

   A node with no declared budget keeps the threshold of 2. Name the threshold constant properly — the inventory's `_STALL_REPEAT_LIMIT` never existed.

2. **Reword the exception.** Name the missing structured channel rather than the tool's silence, and quote a bounded head of the repeated stdout so the operator sees what actually repeated. Redact before quoting.

3. **Warn at authoring time.** A validator or `preflight` warning where the budget is written: a `kind: tool` node whose tool returns `fail` without a top-level `findings` array makes the detector authoritative, so the declared loop budget is the ceiling rather than the plan. Say it once, at the place the number is.

4. **Document the `findings` contract** in the operator-facing tool guidance next to `outcome`. It is currently discoverable only from the source, which is how a supported shape came to look like a malfunction.

### Definition of done

- A tool node returning `fail` with non-empty `data` and no `findings`, under `budgets: {x: 6}`, runs six fix rounds, not two.
- The same node with no declared budget still parks on the second repeat.
- A tool returning `fail` with neither `data` nor `findings` parks on the second repeat regardless of budget.
- The parked-task message names the missing channel and carries a redacted stdout head.
- The authoring-time warning fires for such a node and does not fire for one that emits `findings`.
- `_last_no_finding_failure` stays in memory: it is a within-process repeat detector and a reset on resume remains correct for it. The persistent guard is phase 3's job and a different signal.

---

## Phase 3 — a restart-proof loop guard, and a clean terminal after it

**Items:** P0.3, P0.4. **Touches:** [`flow/engine.py`](../../../src/wastech_orchestrator/core/flow/engine.py), [`flow/recorder.py`](../../../src/wastech_orchestrator/core/flow/recorder.py), [`ledger.py`](../../../src/wastech_orchestrator/ledger.py), [`core/orchestrator.py`](../../../src/wastech_orchestrator/core/orchestrator.py).

The two items are one commit because P0.3 writes the failure report that P0.4 must stop advertising.

### What is wrong

`_check_stall`'s state is two plain dicts on the engine instance, documented as transient on purpose. That was defensible for the failure it was written against — an agent emitting tokens without editing, within one process — but the loop it guards outlives the process: four restarts zeroed the streak while the loop counters beside it, persisted in `tasks.flow_run_counters`, kept counting. Eleven `fidelity_critic` passes returned a near-verbatim, unfixable finding; `evaluations` rows 7–13 are the same paragraph seven times.

Then, at the terminal, the report that guard wrote is never removed: `_finish` writes a failure report when one is **missing** and has no path that clears one that is present. For a non-blocking evaluator that is the ordinary path, so every `content_translate` run with a non-converging lens ships `final_status: done` beside `failure_report: …/failure_report.json`.

### What to build

1. **A second, persistent signal, derived — not stored.** The engine takes a callable beside `_diff_fingerprint` that answers "how many consecutive times has this node returned this same finding set?". Its implementation reads the last rows of `evaluations` for `(task_id, node_id)` and compares a hash of `findings_json`. Zero new columns, zero migration, and it survives a restart because the rows do. No callable (the merge flow, a test that omits it) leaves the guard inert, matching the existing one.
2. **Threshold 3** consecutive identical finding sets. The existing in-memory tree-fingerprint guard stays exactly as it is — the two catch different shapes and neither replaces the other.
3. **Record which guard fired** in `failure_report.json`: `limit_exhausted: repeated_findings` versus `no_file_change`. The operator needs to know whether the agent did nothing or the critic asked for the impossible.
4. **At the transition to `DONE`**, move the failure artifacts to `recovered-*` names and clear `failure_report_path`. Keep the information — it is worth having — but stop the ledger pointing at it as a live failure.
5. **Add a ledger field**, `recovered_from_stuck: true` plus the loop name, so `worc list` can tell "clean" from "recovered" without the reader interpreting a stale path.

### Definition of done

- An evaluator returning identical findings across a **simulated restart boundary** still terminates, at the third pass.
- A run where the tree changes between passes is not terminated by the new guard.
- `failure_report.json` names which guard fired, and the two names are distinguishable by a reader.
- A task that trips a non-blocking loop guard and then completes ends with a `done` ledger line that advertises no failure report, and whose log directory holds `recovered-*` artifacts rather than `failure_report.json` / `stuck.md`.
- `worc list` distinguishes a clean `done` from a recovered one.
