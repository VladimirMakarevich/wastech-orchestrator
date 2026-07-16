# B20 — Run Artifact Layout

> Reconstructed from code (`providers/artifacts.py`) and tests (`tests/providers/test_artifacts.py`). The code is the only source of truth; this document was rebuilt from the implementation, not from prose or comments. Significant claims carry a `file:line` reference.

**Status:** documented · **Source modules:** `src/wastech_orchestrator/providers/artifacts.py`

## Responsibility

Single owner of the on-disk artifact layout and the **"logs are never overwritten"** invariant. It derives deterministic paths for every attempt and serializes `request.json`, `result.json`, and the provider-supplied `capabilities.json` mapping as UTF-8 JSON ([artifacts.py:222-240](../../../src/wastech_orchestrator/providers/artifacts.py#L222)). It is the source of truth callers join onto rather than reconstructing path segments themselves.

The module is deliberately content-blind and provider-agnostic: it imports neither the redaction module nor any provider syntax — it imports only `AgentRunResult` from `providers/base` for result serialization ([artifacts.py:23](../../../src/wastech_orchestrator/providers/artifacts.py#L23)). It does **not** import the `Stage` enum: a node run is identified by its flow `node_id` (a string), not a `Stage`. Redaction (B21) is the caller's job; the request passed in is already redacted ([artifacts.py:112-113](../../../src/wastech_orchestrator/providers/artifacts.py#L112)). The stream files (`stdout.log`, `stderr.log`, `events.jsonl`) and the optional `output-schema.json` are written directly by the providers (B18) using the paths this module hands back, not by this module.

## Public surface

- `ArtifactPaths` ([artifacts.py:34](../../../src/wastech_orchestrator/providers/artifacts.py#L34)) — frozen dataclass of six absolute paths: `attempt_dir`, `request_path`, `stdout_path`, `stderr_path`, `events_path`, `result_path`. The directory exists; the files may not yet.
- `task_artifact_dir(artifacts_root, task_id)` ([artifacts.py:46](../../../src/wastech_orchestrator/providers/artifacts.py#L46)) — returns `<artifacts_root>/logs/<task-id>/`, the per-task root that task-level writers (plan, summary, subtasks, checks, validation reports) join onto.
- `create_attempt_dir(artifacts_root, task_id, node_id, attempt, provider, *, node_run_id, subtask=None)` ([artifacts.py:80](../../../src/wastech_orchestrator/providers/artifacts.py#L80)) — creates the attempt directory (`exist_ok=False`) and returns its `ArtifactPaths`.
- `node_run_dir(artifacts_root, task_id, node_id, node_run_id)` — **pure path** (no mkdir) to `stages/<node_id>/run-<node_run_id:06d>/`, the **parent** of the `<attempt>-<provider>/` dirs. The single owner of where a node run's operator-facing per-run artifacts land (review findings/summary, rendered prompt, generic `<node_id>.out.md`, checks reports, tool streams). Callers `mkdir(parents=True, exist_ok=True)` themselves — `checks`/`tool` nodes never reach the provider adapter, so their run dir is not pre-created by `create_attempt_dir`.
- `node_history_path(artifacts_root, task_id, node_id)` / `append_node_history(artifacts_root, task_id, node_id, entry)` — the per-node `stages/<node_id>/history.jsonl` chronological index and its append-only, **best-effort** (swallows `OSError`) writer (`newline="\n"`, one compact JSON line per run).
- `latest_run_file(artifacts_root, task_id, node_id, filename)` — the newest `run-*/<filename>` that **exists**, scanning runs in descending int-parsed `node_run_id` order; the content-aware resolver behind the downstream `{<node_id>_path}` fan-in channel (an empty/infra-failed newest run does not shadow a prior run's real output).
- `archive_task_artifacts(artifacts_root, task_id, attempt)` ([artifacts.py:56](../../../src/wastech_orchestrator/providers/artifacts.py#L56)) — moves a prior attempt's artifacts into `attempt-<N>/` on rerun; returns the archive dir or `None`.
- `write_request_artifact(paths, redacted_request)` ([artifacts.py:112](../../../src/wastech_orchestrator/providers/artifacts.py#L112)) — writes the already-redacted request mapping to `request.json`.
- `write_result_artifact(paths, result)` ([artifacts.py:117](../../../src/wastech_orchestrator/providers/artifacts.py#L117)) — writes `dataclasses.asdict(result)` to `result.json`.
- `write_capabilities_artifact(paths, manifest)` ([artifacts.py:232](../../../src/wastech_orchestrator/providers/artifacts.py#L232)) — writes a provider-constructed, credential-free effective-capability mapping to `capabilities.json`.
- `sha256_file(path)` ([artifacts.py:128](../../../src/wastech_orchestrator/providers/artifacts.py#L128)) — hex SHA-256 of a file's bytes, read in 64 KiB chunks ([artifacts.py:25](../../../src/wastech_orchestrator/providers/artifacts.py#L25)), for the SQLite artifact registry (B07).

## Behavior

### On-disk directory shape

The per-attempt directory is assembled segment by segment in `create_attempt_dir` ([artifacts.py:97-101](../../../src/wastech_orchestrator/providers/artifacts.py#L97)). The exact segment names, verified from the code (not assumed):

```text
<artifacts_root>/logs/<task-id>/stages/<node_id>/[sub-<NN>/]run-<node_run_id:06d>/<attempt>-<provider>/
```

- `logs` / `<task-id>` / `stages` are literal segments; `<node_id>` is the request's flow `node_id` used verbatim (no `Stage` lookup) ([artifacts.py:97](../../../src/wastech_orchestrator/providers/artifacts.py#L97)). In the implementation flow the node ids equal the old stage values (`planning`, `implementation`, `fixing`, `review`, …), so the segment reads the same there.
- `sub-<NN>/` is inserted only for a decomposed subtask, zero-padded to two digits (`f"sub-{subtask:02d}"`) ([artifacts.py:98-99](../../../src/wastech_orchestrator/providers/artifacts.py#L99)).
- `run-<node_run_id:06d>` is zero-padded to six digits; `<attempt>-<provider>` is the leaf, e.g. `1-codex` ([artifacts.py:100](../../../src/wastech_orchestrator/providers/artifacts.py#L100)). The two tests pin this exactly: `logs/task-001/stages/planning/run-000042/1-codex` ([test_artifacts.py:25](../../../tests/providers/test_artifacts.py#L25)) and the subtask form `…/implementation/sub-02/run-000007/2-codex` ([test_artifacts.py:42-44](../../../tests/providers/test_artifacts.py#L42)).

The files inside one attempt directory:

| File | Constant / origin | Written by |
| --- | --- | --- |
| `request.json` | `REQUEST_FILENAME` ([artifacts.py:27](../../../src/wastech_orchestrator/providers/artifacts.py#L27)) | B20 `write_request_artifact` |
| `result.json` | `RESULT_FILENAME` ([artifacts.py:31](../../../src/wastech_orchestrator/providers/artifacts.py#L31)) | B20 `write_result_artifact` |
| `capabilities.json` (Codex) | `CAPABILITIES_FILENAME` ([artifacts.py:35](../../../src/wastech_orchestrator/providers/artifacts.py#L35)) | B20 `write_capabilities_artifact`; content from B18 |
| `stdout.log` | `STDOUT_FILENAME` ([artifacts.py:28](../../../src/wastech_orchestrator/providers/artifacts.py#L28)) | B18 provider ([codex.py:434](../../../src/wastech_orchestrator/providers/codex.py#L434)) |
| `stderr.log` | `STDERR_FILENAME` ([artifacts.py:29](../../../src/wastech_orchestrator/providers/artifacts.py#L29)) | B18 provider ([codex.py:435](../../../src/wastech_orchestrator/providers/codex.py#L435)) |
| `events.jsonl` | `EVENTS_FILENAME` ([artifacts.py:30](../../../src/wastech_orchestrator/providers/artifacts.py#L30)) | B18 provider ([codex.py:438](../../../src/wastech_orchestrator/providers/codex.py#L438)) |
| `output-schema.json` (optional) | not in `ArtifactPaths` — joined onto `attempt_dir` | B18 provider ([codex.py:520](../../../src/wastech_orchestrator/providers/codex.py#L520), [claude.py:601](../../../src/wastech_orchestrator/providers/claude.py#L601)) |

`ArtifactPaths` names the six fixed common paths; `capabilities.json`, `output-schema.json`, and
`last-message.txt` are joined onto `attempt_dir` because they are provider-specific/optional.
Codex constructs the capability manifest only from effective policy booleans and labels — never
from auth/config discovery — before handing it to this content-blind writer (B18).

### Per-run operator-facing artifacts

The human-readable artifacts a node produces each run live **beside** its provider attempts, under `node_run_dir(...)` = `stages/<node_id>/run-<node_run_id:06d>/` (the parent of the `<attempt>-<provider>/` leaves):

```text
stages/<node_id>/
  history.jsonl                      # one line per run: {run_id, node_id, kind, outcome, findings, dir}
  run-<node_run_id:06d>/
    rendered-prompt.md               # B27 write_rendered_prompt
    findings.json / summary.md       # evaluator (review / test_quality) findings + summary
    <node_id>.out.md                 # generic node-output channel → {<node_id>_path}
    citation.json / dependency_scan.json  # checks-node reports
    stdout.txt / stderr.txt          # tool-node redacted streams
    <attempt>-<provider>/            # provider attempts (create_attempt_dir)
```

This extends the never-overwrite rule from the per-attempt provider dirs to every task-level artifact a re-running node writes: keyed by the reserved `node_run_id`, a `review → fixing → testing → review` loop keeps each pass's findings instead of clobbering the last. The **once-only** contract slots (`plan.md`, the enriched spec, the finalize `summary.md`) stay flat at the task root — they run once and are read back by fixed name in resume/finalize/PR-body. `history.jsonl` is written once per executed node run by the orchestrator's `post_node` hook (B06), so it covers every node kind with no duplication (a run that raises before returning — evaluator schema-fail, checks/tool manual — is absent, having produced no complete payload). Downstream fan-in resolves `{<node_id>_path}` to the latest run's output via `latest_run_file`; resume rebuilds `review_path` from the store's latest `in_flow_verdict` run.

### Never-overwrite rule

`create_attempt_dir` calls `mkdir(parents=True, exist_ok=False)` ([artifacts.py:101](../../../src/wastech_orchestrator/providers/artifacts.py#L101)). If the directory already exists the standard library raises `FileExistsError` — logs are never silently overwritten ([test_artifacts.py:50-53](../../../tests/providers/test_artifacts.py#L50)). Uniqueness is carried by `node_run_id`, which is reserved in SQLite (B07) before the provider starts, so a repeated fixing cycle or a recovery run lands in a distinct `run-<id>` directory even though the provider's per-stage `attempt` counter restarts at 1 ([test_artifacts.py:62-65](../../../tests/providers/test_artifacts.py#L62)). Distinct `attempt` values within one run also produce distinct leaves ([test_artifacts.py:56-59](../../../tests/providers/test_artifacts.py#L56)).

### Archiving on rerun

`archive_task_artifacts` clears the task dir for a fresh attempt while keeping the prior failure auditable ([artifacts.py:56-77](../../../src/wastech_orchestrator/providers/artifacts.py#L56)). It collects every entry under `logs/<task-id>/` whose name does not start with `attempt-` ([artifacts.py:67](../../../src/wastech_orchestrator/providers/artifacts.py#L67)), then `rename`s each into a new `attempt-<N>/` directory ([artifacts.py:70-76](../../../src/wastech_orchestrator/providers/artifacts.py#L70)). It returns `None` when the task dir is absent or holds nothing to archive ([artifacts.py:65-69](../../../src/wastech_orchestrator/providers/artifacts.py#L65)). It is idempotent for an interrupted rerun: a destination name already present is skipped rather than overwritten ([artifacts.py:73-75](../../../src/wastech_orchestrator/providers/artifacts.py#L73)). Because the archive lives under the same task root, sibling task-level dirs (`checks/`, `prompt-audit/`, `summary.md`, …) are swept along with the stage logs.

### Serialization

All three writers funnel through `_write_json`, which dumps deterministic indented UTF-8 JSON with
a trailing newline. Request/capability writers copy their mappings; the result writer flattens the
dataclass with `dataclasses.asdict`.

### What this module does NOT do (caller boundary)

`write_result_artifact` serializes whatever `AgentRunResult` it is handed — it performs no normalization itself. The non-secret session id in `result.json` is produced by the **provider** (B18), which calls `_redact_result_session` to replace the raw session id with `normalized_session_id(...)` (B21) before invoking the writer ([codex.py:505](../../../src/wastech_orchestrator/providers/codex.py#L505), [codex.py:613-621](../../../src/wastech_orchestrator/providers/codex.py#L613)). The raw session id is kept on the in-memory result for the `editing_lineage` store (state.db only, B07) and never reaches disk ([codex.py:503-504](../../../src/wastech_orchestrator/providers/codex.py#L503)). Likewise the redaction of `request.json` is done by the provider before `write_request_artifact` ([codex.py:526-531](../../../src/wastech_orchestrator/providers/codex.py#L526)). This module never imports B21 ([artifacts.py:13-23](../../../src/wastech_orchestrator/providers/artifacts.py#L13)).

## Invariants & guarantees

- **`<artifacts_root>` is the gitignored `<repo>/.worc/` home** — `worc_home_for(config)` returns `Path(repo.local_path) / ".worc"`, and the whole dir is gitignored ([cli.py:503-510](../../../src/wastech_orchestrator/cli.py#L503), `WORC_HOME = ".worc"` [cli.py:71](../../../src/wastech_orchestrator/cli.py#L71)). All run traces live under `<repo>/.worc/logs/<task-id>/...`. The committed audit trail is the exception: the task file and its `<id>.summary.md` stay at the repo-root `tasks/`, since `tasks_root_for` is the repo root, not `.worc/` ([cli.py:513-519](../../../src/wastech_orchestrator/cli.py#L513)). The `logs/<task-id>/summary.md` is only a working copy / PR-body fallback ([orchestrator.py:1512-1513](../../../src/wastech_orchestrator/core/orchestrator.py#L1512)).
- **Logs are never overwritten** — `exist_ok=False` ([artifacts.py:101](../../../src/wastech_orchestrator/providers/artifacts.py#L101)); collision is a hard `FileExistsError`.
- **No state is held** — every path is recomputed from arguments; the layout's single source of truth is the path-construction code itself.
- **Provider/redaction-agnostic** — imports only `providers/base` ([artifacts.py:23](../../../src/wastech_orchestrator/providers/artifacts.py#L23)); content arrives already redacted.
- **Archiving is non-destructive and idempotent** — `rename` (move) not delete; existing archive entries are preserved ([artifacts.py:73-76](../../../src/wastech_orchestrator/providers/artifacts.py#L73)).
- **Per-run history survives `logging.artifacts` pruning by placement** — `prune_attempt_artifacts` iterates only the leaf `<attempt>-<provider>/` dir it is handed, so the operator-facing payloads written at the `run-<id>/` level (one dir up) are never touched by a `minimal`/`standard` level. The only thing that removes this history is an explicit `worc logs clean` of the whole task tree; no code change to pruning is needed.
- **The capability security audit survives every retention level** — both `minimal` and `standard`
  keep `capabilities.json` beside `result.json` ([artifacts.py:45-49](../../../src/wastech_orchestrator/providers/artifacts.py#L45)); only verbose request/events/schema files are pruned.

## Dependencies

- **Uses:** B18 (`providers/base` — `AgentRunResult` for result serialization; the `node_id` segment is the request's flow node id, not a `Stage`). **Used by:** B18 (providers call `create_attempt_dir` and request/result writers; Codex also calls the capability writer), B24 (check execution), B07 (artifact checksums), and B06 (rerun archival/task artifact roots). B21 supplies request/result redaction; the Codex capability manifest is secret-free by construction.

## Tests

- `tests/providers/test_artifacts.py` — pins the exact attempt-dir layout including the six-digit `run-` and `provider` leaf ([test_artifacts.py:24-31](../../../tests/providers/test_artifacts.py#L24)), the zero-padded `sub-NN` segment ([test_artifacts.py:33-47](../../../tests/providers/test_artifacts.py#L33)), the never-overwrite `FileExistsError` ([test_artifacts.py:50-53](../../../tests/providers/test_artifacts.py#L50)), distinct dirs per attempt and per `node_run_id` when the attempt counter resets ([test_artifacts.py:56-65](../../../tests/providers/test_artifacts.py#L56)), the `request.json` round-trip ([test_artifacts.py:68-72](../../../tests/providers/test_artifacts.py#L68)), and enum/`NormalizedError` serialization in `result.json` ([test_artifacts.py:75-92](../../../tests/providers/test_artifacts.py#L75)). The test suite does not directly cover `archive_task_artifacts` or `sha256_file` here (exercised via the orchestrator/rerun path).
