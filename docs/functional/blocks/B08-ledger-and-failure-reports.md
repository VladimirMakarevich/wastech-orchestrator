# B08 — Ledger and Failure Reports

> Reconstructed from code (`src/wastech_orchestrator/ledger.py`) and tests (`tests/core/test_ledger.py`). The code is the only source of truth; this document was rebuilt from the implementation, not from prose or comments. Significant claims carry a `file:line` reference.

**Status:** documented · **Source modules:** `src/wastech_orchestrator/ledger.py`

## Responsibility

Owns three artifacts that live outside SQLite: the append-only completed-tasks ledger (`<artifacts_root>/logs/completed.jsonl`), the two stuck artifacts (`failure_report.json` machine-readable + `stuck.md` human-readable), and the deterministic minimal summary fallback (`summary.md` + `summary.json`). SQLite is the authoritative state; the ledger is a convenience index of terminal outcomes and the ledger half of the duplicate-id gate ([ledger.py:1](../../../src/wastech_orchestrator/ledger.py#L1)).

This module is pure I/O over paths and dataclasses: it imports only `task_artifact_dir` for the per-task directory and otherwise touches no provider syntax, no redaction, and no state machine. Callers (the orchestrator/recorder) decide _when_ to write and supply already-redacted content; this module decides only the file layout and the record/JSON shape ([ledger.py:21](../../../src/wastech_orchestrator/ledger.py#L21)).

## Public surface

- `LedgerRecord` ([ledger.py:30](../../../src/wastech_orchestrator/ledger.py#L30)) — frozen dataclass, one terminal-transition record; `to_json()` ([ledger.py:66](../../../src/wastech_orchestrator/ledger.py#L66)) serializes it.
- `Ledger(logs_root)` ([ledger.py:92](../../../src/wastech_orchestrator/ledger.py#L92)) — append-only access to `<logs_root>/completed.jsonl`.
- `Ledger.append(record)` ([ledger.py:104](../../../src/wastech_orchestrator/ledger.py#L104)) — append exactly one JSON line; never rewrites.
- `Ledger.records()` ([ledger.py:111](../../../src/wastech_orchestrator/ledger.py#L111)) — read all records (skips blank lines); `[]` when the file is absent.
- `Ledger.has_task_id(task_id)` ([ledger.py:121](../../../src/wastech_orchestrator/ledger.py#L121)) — ledger half of the §19 duplicate-id gate.
- `Ledger.path` ([ledger.py:100](../../../src/wastech_orchestrator/ledger.py#L100)) — the `completed.jsonl` path.
- `DecomposedFailureInfo` ([ledger.py:126](../../../src/wastech_orchestrator/ledger.py#L126)) — extra failure-report fields for a decomposed task.
- `write_failure_report(...)` ([ledger.py:136](../../../src/wastech_orchestrator/ledger.py#L136)) — write `failure_report.json` + `stuck.md`, return both paths.
- `write_minimal_summary(...)` ([ledger.py:206](../../../src/wastech_orchestrator/ledger.py#L206)) — write the compact fallback `summary.md` + `summary.json`, return both paths.

## Behavior

### The append-only ledger

`Ledger` is constructed against `<artifacts_root>/logs` (the directory that holds both `completed.jsonl` and the per-task dirs) and resolves its file path once in `__init__` ([ledger.py:95](../../../src/wastech_orchestrator/ledger.py#L95)). `append` creates the parent on first use, serializes the record with `json.dumps(..., ensure_ascii=False)`, and writes a single `line + "\n"` in `"a"` mode ([ledger.py:106](../../../src/wastech_orchestrator/ledger.py#L106)) — the file is never opened for rewrite, so it grows strictly by one line per terminal transition. `records()` reads the whole file and `json.loads` each non-blank line ([ledger.py:116](../../../src/wastech_orchestrator/ledger.py#L116)); a missing file yields `[]` ([ledger.py:113](../../../src/wastech_orchestrator/ledger.py#L113)). `has_task_id` is a linear `any(rec.get("id") == task_id ...)` over `records()` ([ledger.py:123](../../../src/wastech_orchestrator/ledger.py#L123)).

`LedgerRecord` has a small required core — `id`, `title`, `final_status`, `finished_at` — and everything else defaults, so older records that predate a field read back harmlessly ([ledger.py:34](../../../src/wastech_orchestrator/ledger.py#L34)). The full field set, all serialized by `to_json` ([ledger.py:66](../../../src/wastech_orchestrator/ledger.py#L66)):

- Identity/result: `id`, `title`, `final_status`, `finished_at`.
- Git outcome: `branch`, `pr_url`.
- Auto-merge audit: `auto_merged` (true iff the orchestrator merged without review), `merge_outcome` (merge SHA, `"merged"`, or `"armed"`) ([ledger.py:40](../../../src/wastech_orchestrator/ledger.py#L40)).
- Fix/cleanup: `fix_iterations`, `terminal_cleanup` (`"completed"`/`"blocked"`), `failure_report` (path to the stuck report).
- Validation: `validation_reason` (the §16 gate rejection reason).
- Decomposition: `decomposed`, `subtask_count`, `subtasks_completed`.
- Re-run linkage: `attempt` (1 for the original, increments per re-run), `rerun_of` (the re-attempted id, set only when `attempt > 1`) ([ledger.py:53](../../../src/wastech_orchestrator/ledger.py#L53)).
- Operator-finalized marker: `manual` (recorded out-of-band by the `finalize` command), `outcome` (`"abandoned"` vs. plain failure), `note` (operator reason) ([ledger.py:59](../../../src/wastech_orchestrator/ledger.py#L59)).

The orchestrator constructs `LedgerRecord` at every terminal path — normal terminal via `_append_ledger` ([orchestrator.py:1922](../../../src/wastech_orchestrator/core/orchestrator.py#L1922)), validation-gate rejection ([orchestrator.py:1685](../../../src/wastech_orchestrator/core/orchestrator.py#L1685)), `finalize` ([orchestrator.py:610](../../../src/wastech_orchestrator/core/orchestrator.py#L610)), and the recovery paths `_resume_manual`/`_resume_cleanup` ([orchestrator.py:668](../../../src/wastech_orchestrator/core/orchestrator.py#L668)). The recovery paths guard the append with `has_task_id` so a re-reconciled task is not double-recorded ([orchestrator.py:667](../../../src/wastech_orchestrator/core/orchestrator.py#L667)).

### Failure report (`failure_report.json` + `stuck.md`)

`write_failure_report` resolves the per-task dir via `task_artifact_dir`, mkdirs it, and writes two files ([ledger.py:155](../../../src/wastech_orchestrator/ledger.py#L155)). The JSON report always carries the flow-neutral base — `task_id`, `node_id`, `loop`, `limit_exhausted` (from the `limit_name` arg), and a copied `counters` map — plus the implementation-specific `last_check_log`, `last_review_findings` (each finding dict-copied), and `final_diff`, which stay empty when the flow has no such nodes ([ledger.py:158](../../../src/wastech_orchestrator/ledger.py#L158)). When a `DecomposedFailureInfo` is supplied, a `decomposed` block (`subtask_count`, `subtasks_completed`, `failing_subtask`, `committed_shas`) is added ([ledger.py:168](../../../src/wastech_orchestrator/ledger.py#L168)). The human `stuck.md` mirrors the same data as Markdown: a title, the loop/limit sentence, a Counters list, an optional Decomposition section, the last failing check output, the last blocking review findings (rendered from each finding's `title`, falling back to the whole dict), and the final diff in a `diff` fence ([ledger.py:181](../../../src/wastech_orchestrator/ledger.py#L181)). It returns `(report_path, stuck_path)`.

This is the single writer for the engine's failure report: the flow recorder's `StateStoreRunRecorder.write_failure_report` calls straight through to it with `last_check_log=None`, `last_review_findings=None`, `final_diff=""`, building the `DecomposedFailureInfo` from `node_runs`/subtasks and then stamping `failure_report_path` on the task row ([recorder.py:48](../../../src/wastech_orchestrator/core/flow/recorder.py#L48)).

### Minimal summary fallback

`write_minimal_summary` writes a deliberately small `summary.md` + `summary.json` under the per-task dir ([ledger.py:226](../../../src/wastech_orchestrator/ledger.py#L226)). The JSON keeps exactly four keys — `what` (the title), `how`, `integration`, `why` — where `why` links to the task file by `task_ref` (e.g. `<id>.md`) or a generic line when `task_ref` is `None` ([ledger.py:229](../../../src/wastech_orchestrator/ledger.py#L229)). The Markdown renders those four sections plus a `Changes` block holding the `git diff --stat` of the task change vs the base branch — the working tree, since the summary is built before the publish-node commit — or `(no changes detected)` when empty, and a pointer line to `logs/<task_id>/current.diff` ([ledger.py:244](../../../src/wastech_orchestrator/ledger.py#L244)). It deliberately inlines neither the full task description nor the patch body — the full, already-redacted patch lives in `current.diff` — to keep the committed summary small and avoid an unredacted diff landing in git ([ledger.py:214](../../../src/wastech_orchestrator/ledger.py#L214)).

**Framing:** the whole-task summary is normally authored by the supervisor oversight layer ([B31](B31-supervisor.md)), which is not a graph node and not a stage. This minimal summary is the deterministic fallback the orchestrator falls back to when that advisory synthesis is unavailable — `_summary_md_body` calls it only when `summary.md` does not already exist on disk ([orchestrator.py:1518](../../../src/wastech_orchestrator/core/orchestrator.py#L1518)). Either way a summary is always written. The committed `<id>.summary.md` lands next to the moved task file via [B22](B22-git-manager.md); this module only produces the `logs/<task-id>/summary.md` working copy.

## Invariants & guarantees

- **Append-only.** `completed.jsonl` is only ever opened in append mode and gains exactly one line per terminal transition — never truncated or rewritten ([ledger.py:108](../../../src/wastech_orchestrator/ledger.py#L108)); the test asserts the file holds exactly the number of appended lines ([test_ledger.py:28](../../../tests/core/test_ledger.py#L28)).
- **Forward/backward tolerant.** Every non-core `LedgerRecord` field defaults, and `records()` deserializes whatever keys are present, so a record written before a field existed reads back with that field's default ([test_ledger.py:55](../../../tests/core/test_ledger.py#L55)).
- **Missing file is empty, not an error** ([ledger.py:113](../../../src/wastech_orchestrator/ledger.py#L113), [test_ledger.py:39](../../../tests/core/test_ledger.py#L39)).
- **Stable summary contract.** `summary.json` keeps exactly `{what, how, integration, why}` ([test_ledger.py:162](../../../tests/core/test_ledger.py#L162)); the fallback never inlines a raw patch (`diff --git`/`@@` absent) and stays compact ([test_ledger.py:182](../../../tests/core/test_ledger.py#L182)).
- **Flow-neutral failure report.** Base fields are always present; the implementation-specific sections are empty (not omitted) when the flow has no checker/review nodes ([recorder.py:57](../../../src/wastech_orchestrator/core/flow/recorder.py#L57)).

## Dependencies

- **Uses:** B20 (`task_artifact_dir` for the per-task directory layout).
- **Used by:** B06 (constructs and appends a `LedgerRecord` at every terminal transition; reads via `_ledger_attempt_count`/`_ledger_has_manual`; calls `write_minimal_summary`), B16 (the §19 duplicate-id gate consumes `has_task_id`), B28 (the flow engine's failure report is written through `write_failure_report`, called by B30's recorder), B31 (the supervisor authors the primary summary; this module is the fallback writer).

## Audit candidates

- `src/wastech_orchestrator/ledger.py:9` — see [the audit](../../backlog/2026-06-21-audit.md): the module docstring and `write_minimal_summary`'s docstring frame the fallback as triggered when "no provider can produce the `summary` stage (§5.2)" ([ledger.py:206](../../../src/wastech_orchestrator/ledger.py#L206)), but the whole-task summary is now authored by the supervisor layer (B31), not a provider `summary` stage — stale framing.

## Tests

- `tests/core/test_ledger.py` — covers append-only growth and round-trip ([test_ledger.py:17](../../../tests/core/test_ledger.py#L17)); `has_task_id` ([test_ledger.py:31](../../../tests/core/test_ledger.py#L31)); empty/missing file ([test_ledger.py:39](../../../tests/core/test_ledger.py#L39)); re-run linkage and tolerance of missing rerun keys ([test_ledger.py:43](../../../tests/core/test_ledger.py#L43)); the `finalize` manual/note/outcome marker and the pipeline default ([test_ledger.py:65](../../../tests/core/test_ledger.py#L65)); decomposition fields ([test_ledger.py:92](../../../tests/core/test_ledger.py#L92)); failure report incl. the decomposed block ([test_ledger.py:113](../../../tests/core/test_ledger.py#L113)); and the minimal summary's four-key contract, compactness, and no-task-ref path ([test_ledger.py:152](../../../tests/core/test_ledger.py#L152)).
