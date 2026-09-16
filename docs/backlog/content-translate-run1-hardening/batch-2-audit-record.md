# Batch 2 — the audit record

Covers **P1.5, P1.6, P1.7, P2.11, P2.12, P2.14, P2.15, P2.16**. One pull request against `dev`; three phases, three commits.

Nothing in this batch changes what the orchestrator does — only what it records about it and what it says out loud. It runs second because phase 1 of [batch 1](batch-1-concurrency-and-loops.md) is what makes the tables it fixes trustworthy in the first place.

Back to the [campaign index](README.md); the evidence is in [inventory.md](inventory.md).

---

## Phase 4 — record what actually ran

**Items:** P1.5, P1.6, P1.7. **Touches:** [`providers/artifacts.py`](../../../src/wastech_orchestrator/providers/artifacts.py), [`providers/base.py`](../../../src/wastech_orchestrator/providers/base.py), [`providers/claude.py`](../../../src/wastech_orchestrator/providers/claude.py), [`providers/codex.py`](../../../src/wastech_orchestrator/providers/codex.py), [`state_store.py`](../../../src/wastech_orchestrator/state_store.py), the summary and `list` / `status` surfaces, the operator guide's `logging.artifacts` row.

### What is wrong

Three gaps in one record, all pointing the same way — the mandatory accounting table cannot answer the questions a post-mortem opens with.

- **`request.json` is pruned at every level except `full`.** `_ARTIFACT_KEEP["standard"]` holds `result`, `stdout` and `stderr` and nothing else, so the one artifact carrying `argv` and the permission profile is deleted by default. This run has zero of them, beside 36 `stdout.log` files totalling 14.2 MB. For a product whose central invariant is the permission ceiling, that is the wrong file to drop. Both [`ledger.py`](../../../src/wastech_orchestrator/ledger.py) and [`observability.py`](../../../src/wastech_orchestrator/core/flow/observability.py) already document it as load-bearing.
- **No model anywhere mandatory.** `result.json` carries eleven fields and not the model; `provider_attempts` has no model column. The value is recorded well in `prompt-audit/timeline.jsonl` — effective and configured side by side — but `prompt_audit` defaults to `false`, so out of the box the model that ran a node survives only inside `stdout.log`, which itself disappears at `logging.artifacts: minimal`.
- **Codex attempts carry no cost.** Claude sets `cost=coerce_usage_cost(total_cost_usd)`; `codex.py` sets none. The run reports \$38.21 for a pair of tasks in which Codex handled 23% of the input tokens and was the single heaviest node.

### What to build

1. Move `REQUEST_FILENAME` into the `standard` keep set, and into `minimal` as well — `result.json` is not interpretable without knowing what was requested, and the file is kilobytes per attempt.
2. State the retention sets in the operator guide's `logging.artifacts` row, which today lists stdout/stderr and does not mention the request at all.
3. Add effective `model` and `reasoning` to `result.json`.
4. Add `provider_attempts.model` and `provider_attempts.reasoning`, written from the **same source** as the prompt-audit record so the two cannot disagree. Additive column plus the idempotent `_migrate_*` step, matching the existing pattern.
5. Leave the prompt-audit record untouched. It serves a different reader; the point is that the mandatory table must not depend on an optional reading aid.
6. **State the Codex cost gap, do not estimate it.** No price table, no `usage_cost_source` column. Where a cost total is reported — the summary, `list`, `status` — say what is missing beside it: `Codex cost not accounted (13 attempts, 8.4 M input tokens)`. A silent zero is worse than an honest gap.

### Definition of done

- A run at the shipped default `logging.artifacts: standard` leaves a `request.json` per attempt; so does `minimal`.
- `SELECT model, SUM(usage_cost) FROM provider_attempts GROUP BY model` answers per-model cost without a grep, and without `prompt_audit` enabled.
- Where both providers ran, no total claims to be the run's whole cost; the Codex gap is named with its attempt and token counts.
- `provider_attempts.model` agrees with `prompt-audit/timeline.jsonl` on every node when both are present — asserted, not assumed.

---

## Phase 5 — stop one column carrying two meanings

**Item:** P2.15. **Touches:** [`state_store.py`](../../../src/wastech_orchestrator/state_store.py), [`ledger.py`](../../../src/wastech_orchestrator/ledger.py), [`core/orchestrator.py`](../../../src/wastech_orchestrator/core/orchestrator.py).

### What is wrong

Five small record defects, each cheap, together the reason this run's audit trail had to be read with a caveat.

- **`skip_reason` carries two unrelated meanings.** `record_skipped_node_run` uses it for a deterministic `when`-false skip (`status='skipped'`, `skipped=True`); `reconcile_open_node_runs` reuses the same column for the **abort** reason of an orphaned run, with `skipped` left at 0. The result is this run's `node_runs.id=24`: `skipped=0`, `status='succeeded'`, and a `skip_reason` explaining an exchange mutation.
- **`stuck.md` prints a Python repr.** Its "Last blocking review findings" section renders `{'severity': 'low', 'reason': "…", 'gating': True, 'fix': '…'}` — single quotes, capitalised `True`. It is a document written for a human or a handoff agent.
- **The stuck report has no provider evidence.** `failure_report.json` carries `provider_attempts: []` although more than twenty attempts existed, eight of them on the stuck node, and `last_check_log: null` because that field reads `check_runs` and tool nodes do not write there. The infra path already passes `_provider_attempt_evidence`; the loop-guard path does not.
- **`artifacts.path` mixes conventions.** 160 of 162 rows are absolute and embed a home directory; the two `summary_md` rows are relative. One column, two conventions, and the absolute form makes `state.db` non-portable — which sits badly with the stored-path rule in [coding-style.md](../../../.agents/rules/coding-style.md).
- **Ledger attempt numbers have gaps.** `completed.jsonl` for `en-adapt-01a` records attempts 1, 1, 1, 2, 5, 6, 7 — 3 and 4 never appear and the first entry is duplicated.

### What to build

1. Add `node_runs.abort_reason` and stop overloading `skip_reason`. `reconcile_open_node_runs` writes the new column; `skip_reason` goes back to meaning only what its name says.
2. Render findings in `stuck.md` as formatted list items with named fields — severity, reason, paths, gating, fix — not as a repr.
3. Pass `_provider_attempt_evidence` on the loop-guard report path too, so the report can answer "which provider call produced the repeated finding". Where `last_check_log` is null because the node was a tool node, say that rather than leaving a bare null.
4. **One convention for `artifacts.path`: POSIX, relative to the worc home**, via `Path.as_posix()`. The table has exactly one writer ([`core/orchestrator.py`](../../../src/wastech_orchestrator/core/orchestrator.py)'s `_register_artifact`) and no reader in `src/`, so the change is contained; normalize at the writer. Greenfield, so old rows are not migrated — they are not read.
5. **Determine the ledger gaps, then act.** Re-check after batch 1 phase 1 lands: if the gaps were the concurrency, they disappear and the fix is a regression test. If they survive, either fix the write path or document the ledger as "terminal transitions observed", not "every attempt" — `list` and `status` are built on it and must not over-promise.

### Definition of done

- No row can have a non-null `skip_reason` with `skipped=0`; an aborted orphan carries `abort_reason` instead.
- `stuck.md` renders findings as Markdown a human reads without decoding Python syntax.
- A loop-guard `failure_report.json` names the provider attempts of the stuck node.
- Every `artifacts.path` written by a fresh run is relative and contains no home directory; asserted on Windows and POSIX both.
- The ledger question is answered in writing, with the outcome either a test or a documentation change.

---

## Phase 6 — make the signals that exist actually arrive

**Items:** P2.11, P2.12, P2.14, P2.16. **Touches:** [`cli.py`](../../../src/wastech_orchestrator/cli.py), [`core/orchestrator.py`](../../../src/wastech_orchestrator/core/orchestrator.py).

### What is wrong

Four places where the orchestrator knows something the operator needed and did not say it, or said it where nobody was listening.

- **The task packet is frozen and nothing says so.** The frozen `task.md` is 5 051 bytes; the file on disk at the end of the run is 5 352 and contains a named ceiling exception the earlier text lacked. The operator corrected the task mid-run and the run finished on the stale text — which is exactly the contradiction the critic then reported ten times. Freezing is correct and stays; the silence is the defect.
- **`settled_own_file` guards only `watch`.** On the `run` path a `duplicate_task_id` rejection moved the operator's own task file into `.worc/tasks/rejected/` — under the private home, which the operator does not browse and agents cannot read — and did not print where.
- **A narrowed observe cadence is announced at `info`.** `_announce_observe_cadence` is a thoughtful mechanism: it prints the configured mode, the mode in force and the dropped triggers. The config in force set `logging.level: warning`, so nobody saw it, and twelve `rework` events produced zero observations.
- **Nobody says what the terminal kept.** `logging.clean_runs_on_success: true` worked exactly as documented, and the operator correctly reported "the logs did not get cleaned" — because per-task log directories are out of its scope by design and nothing connects the two facts. `.worc/` stands at 17 MB, 14.2 MB of it raw `stdout.log`.

### What to build

1. On every resume, compare the live task file's sha256 with the frozen packet's; when they differ, emit a `WARNING` saying the file changed after start, that the run continues on the packet frozen at `<time>`, and that `worc stop` plus a fresh start is what applies the edit. Put the same sentence in `summary.md` when it fires, so it survives into the pull request.
2. Apply `settled_own_file` in `cmd_run` as well, and print the quarantine destination on **every** reject, whichever path took it. The operator must learn where their file went from the command's output.
3. Raise `_announce_observe_cadence` to `WARNING`. It reports a configured setting being discarded, which is what that level is for.
4. Print one line at the successful terminal: runs cleaned, logs kept, the size, and the command that reclaims them — and mention `logging.artifacts: minimal` in the same breath, since it would cut that 14.2 MB by roughly thirty times for an operator who does not need the raw event stream. **Nothing about leftover branches**: out of scope by decision.

### Definition of done

- Editing the task file mid-run produces the warning on the next resume, and the sentence appears in `summary.md`.
- A `run` of an already-settled task is skipped rather than quarantined, matching `watch`.
- Every reject prints its destination path.
- The observe-cadence line is visible at `logging.level: warning`.
- A successful terminal prints one line naming what was kept, its size, and the reclaim command — and says nothing about branches.
