# B24 — Check Execution

> Reconstructed from code (`src/wastech_orchestrator/check_runner.py`) and tests (`tests/check/test_check_runner.py`, `tests/security/test_no_shell_interpolation.py`). The code is the only source of truth; this document was rebuilt from the implementation, not from prose or comments. Significant claims carry a `file:line` reference.

**Status:** documented · **Source modules:** `src/wastech_orchestrator/check_runner.py`

## Responsibility

`CheckRunner` is the **command-set execution engine**: given the diff-selected `ResolvedCheckSet`s (each carrying its checks, `cwd`, timeout, and `skip_if_unavailable` flag), it launches **every** check in every set **through the safe runner** ([B19](B19-subprocess-runner.md)), writes a per-run log, and folds the per-check results into an aggregate `CheckOutcome` ([check_runner.py:102](../../../src/wastech_orchestrator/check_runner.py#L102)). It runs **all** selected checks — there is **no fail-fast** — so the human sees the full picture and `fixing` can address every quality failure in one cycle. It never transitions state, never touches git, and does not decide what to run — selection is [B23](B23-check-discovery.md)'s `select_check_sets`, called by the [checks](B30-flow-node-runners.md) node.

Its load-bearing distinctions are three, surfaced as aggregate flags on `CheckOutcome`:

- **Quality failure** (`any_quality_failed`) — an executed check exited non-zero (or timed out): the node routes it to `fixing`.
- **Launch failure** (`any_launch_failed`) — a _required_ set's binary could not be launched (infrastructure): the node hands the incomplete gate to a human, never spending a fix iteration on a problem no code change can fix.
- **Skip** (`any_skipped`) — a `skip_if_unavailable` set whose toolchain binary is absent is **skipped**, loudly, never "passed"; if every selected check was skipped (`nothing_ran`), changed code went unchecked and the node escalates to manual.

## Public surface

- `CheckRunner.run(*, clone_dir, artifacts_root, task_id, subtask=None, selected=None) -> CheckOutcome` ([check_runner.py:102](../../../src/wastech_orchestrator/check_runner.py#L102)) — run every check in the `selected` sets and aggregate; `selected=None` normalizes and runs all of `config.checks.command_sets`.
- `CheckRunner.__init__(config, *, run_process=..., which=shutil.which, ...)` ([check_runner.py:81](../../../src/wastech_orchestrator/check_runner.py#L81)) — the `which` seam (default `shutil.which`) is the toolchain-availability probe for `skip_if_unavailable`.
- `CheckRunner.run_process` (property) ([check_runner.py:96](../../../src/wastech_orchestrator/check_runner.py#L96)) — exposes the injected safe runner, shared with the flow's `dependency_scan` checker so a test's fake runner drives both.
- `CheckOutcome` ([check_runner.py:64](../../../src/wastech_orchestrator/check_runner.py#L64)) — `passed`, `runs: tuple[CheckRunResult, ...]`, and the aggregates `any_quality_failed`, `any_launch_failed`, `any_skipped`, `nothing_ran`, `first_failure_log`.
- `CheckRunResult` ([check_runner.py:46](../../../src/wastech_orchestrator/check_runner.py#L46)) — one check's `command`, `name`, `exit_code`, `timed_out`, `passed`, `log_path`, `launch_failed`, `launch_error`, and `skipped`.

## Behavior

### Resolving what to run

`run` takes the diff-selected sets ([check_runner.py:119](../../../src/wastech_orchestrator/check_runner.py#L119)):

- `selected` (a possibly-empty sequence of `ResolvedCheckSet`) → run exactly those sets. An empty sequence runs zero checks and aggregates to a **vacuous pass** (`CheckOutcome(passed=True, runs=())`, confirmed by `test_no_sets_passes_vacuously`).
- `selected=None` → fall back to `normalize_command_sets(config.checks.command_sets)` ([check_runner.py:122](../../../src/wastech_orchestrator/check_runner.py#L122)) — used in unit harnesses; production always passes the selected sets from the node.

The runner consumes the canonical `ResolvedCheckSet` / `ResolvedCheck` shapes ([B23](B23-check-discovery.md)); no string→argv translation happens here.

### The run loop

Before the loop: the global per-check timeout is `checks.timeout_seconds`, the child env is built from the **allowlist** only ([check_runner.py:124](../../../src/wastech_orchestrator/check_runner.py#L124), [B25](B25-security-policy.md)), and `<artifacts_root>/logs/<task-id>/checks/` is created ([check_runner.py:125](../../../src/wastech_orchestrator/check_runner.py#L125)). The loop iterates set-by-set; each set resolves its timeout as `cset.timeout_seconds or global_timeout` (Р3a — a per-set override) ([check_runner.py:132](../../../src/wastech_orchestrator/check_runner.py#L132)).

For each check in a set ([check_runner.py:133](../../../src/wastech_orchestrator/check_runner.py#L133)):

1. A fresh, non-overwriting log path is chosen ([check_runner.py:136](../../../src/wastech_orchestrator/check_runner.py#L136)).
2. **Availability probe (Р4):** if the set is `skip_if_unavailable` and `which(argv[0]) is None`, the check is **skipped** — a loud `check skipped: toolchain absent` log, a distinct `skipped (toolchain absent)` log file, and a `CheckRunResult(skipped=True, passed=False)` ([check_runner.py:146-150](../../../src/wastech_orchestrator/check_runner.py#L146), [check_runner.py:219-234](../../../src/wastech_orchestrator/check_runner.py#L219), confirmed by `test_skip_if_unavailable_skips_when_binary_absent`). A missing binary on a _required_ set is **not** skipped — it falls through to `run_process`, which records a launch failure (confirmed by `test_required_launch_failure_is_infra`).
3. The launch runs under a heartbeat with `cwd = clone_dir / check.cwd` (or `clone_dir` when `cwd` is empty) ([check_runner.py:151](../../../src/wastech_orchestrator/check_runner.py#L151), confirmed by `test_per_command_cwd`): `run_process(argv, cwd=..., env=<allowlisted>, timeout_seconds=<set or global>, stdout_path=<log>)`. argv is passed verbatim — no shell, no `shlex` (confirmed by `test_argv_no_shell`).
4. `_append_stderr` appends the **redacted** stderr and a status footer to the log ([check_runner.py:169](../../../src/wastech_orchestrator/check_runner.py#L169)).
5. The pass formula: `passed = exit_code == 0 and not timed_out and not launch_failed`, where `launch_failed = result.launch_error is not None` ([check_runner.py:170-171](../../../src/wastech_orchestrator/check_runner.py#L170)).
6. A `CheckRunResult` is appended ([check_runner.py:183](../../../src/wastech_orchestrator/check_runner.py#L183)). **The loop never short-circuits** — the next check always runs.

### Aggregation (run-all precedence)

After the loop, `_aggregate` folds the per-check results ([check_runner.py:198](../../../src/wastech_orchestrator/check_runner.py#L198)): `executed` = non-skipped runs; `any_skipped` = any run was skipped; `any_launch_failed` = any run had a launch error; `any_quality_failed` = any _executed_ run failed but did not launch-fail; `nothing_ran` = there were checks but every one was skipped; `first_failure_log` = the first executed quality-failure's log (for the fixing loop). The aggregate `passed = not (any_quality_failed or any_launch_failed or nothing_ran)` ([check_runner.py:208](../../../src/wastech_orchestrator/check_runner.py#L208)). The precedence between these flags (manual vs. fixing) is the **node's** decision ([B30](B30-flow-node-runners.md)), not the runner's.

```mermaid
flowchart TB
    start(["run(... selected)"]) --> norm["selected is None?<br/>yes → normalize(config.command_sets)<br/>no → list(selected)"]
    norm --> loop{"next check<br/>(set-by-set, no fail-fast)"}
    loop -->|none left| agg["_aggregate(runs)"]
    loop -->|skip_if_unavailable set<br/>&amp; which(argv0)==None| skip["record skipped (loud, never passed)"]
    skip --> loop
    loop -->|else| exec["run_process(argv, cwd=clone/cwd, allowlisted env,<br/>set-or-global timeout) — B19; redact+footer stderr"]
    exec --> loop
    agg --> out["CheckOutcome(passed, runs,<br/>any_quality_failed / any_launch_failed /<br/>any_skipped / nothing_ran / first_failure_log)"]
```

### Logs

`_next_log_path` returns `<NNN>.log` (3-digit, 1-based) — or `sub-<NN>-<NNN>.log` for a subtask — sized from the count of already-present matching logs so a fix-loop re-run **never clobbers** an earlier log ([check_runner.py:236](../../../src/wastech_orchestrator/check_runner.py#L236), confirmed by `test_logs_not_overwritten_across_runs` and `test_subtask_logs_are_prefixed`). `_append_stderr` writes a `----- stderr -----` section only when redacted stderr is non-empty and a `----- status -----` footer carrying `launch_error: …` and/or `timed_out: true` when set ([check_runner.py:242](../../../src/wastech_orchestrator/check_runner.py#L242), confirmed by `test_stderr_redacted_in_log`). A skipped check's log is the distinct one-line `skipped (toolchain absent)` file ([check_runner.py:223-225](../../../src/wastech_orchestrator/check_runner.py#L223)).

### Structured logging

Each launched check emits `check started` and `check completed` bound to `task_id` and `stage="testing"`, carrying `check_index`, `command_set`, `timeout_seconds`, and on completion `passed` / `launch_failed` / `exit_code` / `timed_out` / a rounded `duration_seconds` ([check_runner.py:153](../../../src/wastech_orchestrator/check_runner.py#L153), [check_runner.py:172](../../../src/wastech_orchestrator/check_runner.py#L172), confirmed by `test_check_logs_start_completion_and_duration`). A skipped check emits `check skipped: toolchain absent` ([check_runner.py:148](../../../src/wastech_orchestrator/check_runner.py#L148)); the heartbeat emits `check heartbeat` while a check runs long.

## Invariants & guarantees

- **Argv, never a shell.** Checks reach `run_process` as a list with `shell=False`; the runner applies no `shlex.split` ([check_runner.py:135](../../../src/wastech_orchestrator/check_runner.py#L135)).
- **Allowlisted env only.** The child gets exactly `build_child_env(security.allowed_environment)`, never the parent's full environment ([check_runner.py:124](../../../src/wastech_orchestrator/check_runner.py#L124), [B25](B25-security-policy.md)).
- **Run-all, no fail-fast.** Every selected check runs; results are aggregated, so `fixing` sees all quality failures at once (confirmed by `test_runs_all_no_fail_fast`).
- **Launch failure ≠ quality failure ≠ skip.** A timeout is a quality failure (the process launched, `test_timeout_is_quality_failure`); only `launch_error != None` sets `launch_failed` (`test_required_launch_failure_is_infra`); only an absent binary on a `skip_if_unavailable` set sets `skipped` — and a skip is **never** `passed` (confirmed by `test_skip_if_unavailable_skips_when_binary_absent`).
- **Incomplete gate is not a pass.** `nothing_ran` (every selected check skipped) makes the aggregate `passed=False` so the node can escalate ([check_runner.py:204-208](../../../src/wastech_orchestrator/check_runner.py#L204)).
- **Per-set `cwd` and timeout.** Each check launches in `clone_dir / check.cwd`; a set's `timeout_seconds` overrides the global ([check_runner.py:132](../../../src/wastech_orchestrator/check_runner.py#L132), [check_runner.py:151](../../../src/wastech_orchestrator/check_runner.py#L151)).
- **No state, no git, no overwrite.** The runner only spawns processes and appends to fresh log files; recording `check_runs` and routing are the node's job.
- **Redaction before disk.** stderr is run through `redact_text` before any byte is written ([check_runner.py:249](../../../src/wastech_orchestrator/check_runner.py#L249), [B21](B21-secret-redaction.md)).

## Dependencies

- **Uses:** [B19](B19-subprocess-runner.md) (`run_process` — the safe argv launcher), [B23](B23-check-discovery.md) (`ResolvedCheck` / `ResolvedCheckSet` / `normalize_command_sets` — the check model), [B25](B25-security-policy.md) (`build_child_env` — env allowlist), [B21](B21-secret-redaction.md) (`redact_text` — stderr), [B20](B20-artifact-layout.md) (`task_artifact_dir` — the `checks/` location), [B27](B27-observability.md) (`bind` logging + `run_with_heartbeat`); `shutil.which` (the `which` seam) for the availability probe.
- **Used by:** [B30](B30-flow-node-runners.md) — the `checks` node's `command_profile` mode selects the sets via `select_check_sets` ([B23](B23-check-discovery.md)) and calls `CheckRunner.run(selected=...)` ([nodes/checks.py:233](../../../src/wastech_orchestrator/core/flow/nodes/checks.py#L233)), records one `check_runs` row per command (including skipped, [nodes/checks.py:240-249](../../../src/wastech_orchestrator/core/flow/nodes/checks.py#L240), [B07](B07-state-machine-and-store.md)), and maps `any_launch_failed or nothing_ran` → `NodeManualRequired`, else `any_quality_failed` → `fail`→`fixing`, else (after the mutation guard) `pass` ([nodes/checks.py:89-114](../../../src/wastech_orchestrator/core/flow/nodes/checks.py#L89)). Constructed once in `build_orchestrator` ([orchestrator.py:2135](../../../src/wastech_orchestrator/core/orchestrator.py#L2135)). The `citation` / `dependency_scan` checkers ([B32](B32-flow-checkers.md)) are separate node methods, not this runner.

## Tests

- `tests/check/test_check_runner.py` — empty selection → vacuous pass (`test_no_sets_passes_vacuously`); all-pass aggregation (`test_all_checks_pass`) and run-all-no-fail-fast (`test_runs_all_no_fail_fast`); argv split without shell (`test_argv_no_shell`); `selected=None` falling back to config command sets (`test_selected_none_uses_config_command_sets`); timeout is a quality (not launch) failure (`test_timeout_is_quality_failure`); a required missing binary is an infra launch failure (`test_required_launch_failure_is_infra`); `skip_if_unavailable` skip-when-absent vs run-when-present (`test_skip_if_unavailable_skips_when_binary_absent`, `test_skip_if_unavailable_runs_when_binary_present`); a partial skip still passes but flags the skip (`test_partial_skip_still_passes_but_flags_skip`); per-command `cwd` (`test_per_command_cwd`) and per-set timeout override (`test_per_set_timeout_overrides_global`); logs never overwritten across re-runs and subtask log prefixing; stderr redaction; structured start/completion + duration logging.
- `tests/security/test_no_shell_interpolation.py` — `normalize_check_command` splits a command into argv tokens and does not expand shell substitutions, anchoring the no-shell guarantee on the real resolver path.
