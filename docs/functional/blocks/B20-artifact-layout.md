# B20 — Run Artifact Layout

> Reconstructed from code (`providers/artifacts.py`) and tests (`tests/providers/test_artifacts.py`). The code is the only source of truth; this document was rebuilt from the implementation, not from prose or comments. Significant claims carry a `file:line` reference.

**Status:** documented · **Source modules:** `src/wastech_orchestrator/providers/artifacts.py`

## Responsibility

Single owner of the on-disk artifact layout and the **"logs are never overwritten"** invariant. It derives a deterministic directory and file paths for every attempt of a node run, and serializes two of the per-attempt files — `request.json` and `result.json` — as UTF-8 JSON ([artifacts.py:112](../../../src/wastech_orchestrator/providers/artifacts.py#L112), [artifacts.py:117](../../../src/wastech_orchestrator/providers/artifacts.py#L117)). It is the source of truth callers join onto rather than reconstructing path segments themselves ([artifacts.py:46](../../../src/wastech_orchestrator/providers/artifacts.py#L46)).

The module is deliberately content-blind and provider-agnostic: it imports neither the redaction module nor any provider syntax — it imports only `AgentRunResult` from `providers/base` for result serialization ([artifacts.py:23](../../../src/wastech_orchestrator/providers/artifacts.py#L23)). It does **not** import the `Stage` enum: a node run is identified by its flow `node_id` (a string), not a `Stage`. Redaction (B21) is the caller's job; the request passed in is already redacted ([artifacts.py:112-113](../../../src/wastech_orchestrator/providers/artifacts.py#L112)). The stream files (`stdout.log`, `stderr.log`, `events.jsonl`) and the optional `output-schema.json` are written directly by the providers (B18) using the paths this module hands back, not by this module.

## Public surface

- `ArtifactPaths` ([artifacts.py:34](../../../src/wastech_orchestrator/providers/artifacts.py#L34)) — frozen dataclass of six absolute paths: `attempt_dir`, `request_path`, `stdout_path`, `stderr_path`, `events_path`, `result_path`. The directory exists; the files may not yet.
- `task_artifact_dir(artifacts_root, task_id)` ([artifacts.py:46](../../../src/wastech_orchestrator/providers/artifacts.py#L46)) — returns `<artifacts_root>/logs/<task-id>/`, the per-task root that task-level writers (plan, summary, subtasks, checks, validation reports) join onto.
- `create_attempt_dir(artifacts_root, task_id, node_id, attempt, provider, *, node_run_id, subtask=None)` ([artifacts.py:80](../../../src/wastech_orchestrator/providers/artifacts.py#L80)) — creates the attempt directory (`exist_ok=False`) and returns its `ArtifactPaths`.
- `archive_task_artifacts(artifacts_root, task_id, attempt)` ([artifacts.py:56](../../../src/wastech_orchestrator/providers/artifacts.py#L56)) — moves a prior attempt's artifacts into `attempt-<N>/` on rerun; returns the archive dir or `None`.
- `write_request_artifact(paths, redacted_request)` ([artifacts.py:112](../../../src/wastech_orchestrator/providers/artifacts.py#L112)) — writes the already-redacted request mapping to `request.json`.
- `write_result_artifact(paths, result)` ([artifacts.py:117](../../../src/wastech_orchestrator/providers/artifacts.py#L117)) — writes `dataclasses.asdict(result)` to `result.json`.
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
| `stdout.log` | `STDOUT_FILENAME` ([artifacts.py:28](../../../src/wastech_orchestrator/providers/artifacts.py#L28)) | B18 provider ([codex.py:434](../../../src/wastech_orchestrator/providers/codex.py#L434)) |
| `stderr.log` | `STDERR_FILENAME` ([artifacts.py:29](../../../src/wastech_orchestrator/providers/artifacts.py#L29)) | B18 provider ([codex.py:435](../../../src/wastech_orchestrator/providers/codex.py#L435)) |
| `events.jsonl` | `EVENTS_FILENAME` ([artifacts.py:30](../../../src/wastech_orchestrator/providers/artifacts.py#L30)) | B18 provider ([codex.py:438](../../../src/wastech_orchestrator/providers/codex.py#L438)) |
| `output-schema.json` (optional) | not in `ArtifactPaths` — joined onto `attempt_dir` | B18 provider ([codex.py:520](../../../src/wastech_orchestrator/providers/codex.py#L520), [claude.py:601](../../../src/wastech_orchestrator/providers/claude.py#L601)) |

`ArtifactPaths` names only the five fixed paths ([artifacts.py:38-43](../../../src/wastech_orchestrator/providers/artifacts.py#L38)). `output-schema.json` is **not** an `ArtifactPaths` field — the provider writes it (when `request.output_schema` is present) by joining the filename onto `paths.attempt_dir` ([codex.py:520](../../../src/wastech_orchestrator/providers/codex.py#L520)); Codex also joins a `last-message.txt` onto `attempt_dir` the same way ([codex.py:388](../../../src/wastech_orchestrator/providers/codex.py#L388)).

### Never-overwrite rule

`create_attempt_dir` calls `mkdir(parents=True, exist_ok=False)` ([artifacts.py:101](../../../src/wastech_orchestrator/providers/artifacts.py#L101)). If the directory already exists the standard library raises `FileExistsError` — logs are never silently overwritten ([test_artifacts.py:50-53](../../../tests/providers/test_artifacts.py#L50)). Uniqueness is carried by `node_run_id`, which is reserved in SQLite (B07) before the provider starts, so a repeated fixing cycle or a recovery run lands in a distinct `run-<id>` directory even though the provider's per-stage `attempt` counter restarts at 1 ([test_artifacts.py:62-65](../../../tests/providers/test_artifacts.py#L62)). Distinct `attempt` values within one run also produce distinct leaves ([test_artifacts.py:56-59](../../../tests/providers/test_artifacts.py#L56)).

### Archiving on rerun

`archive_task_artifacts` clears the task dir for a fresh attempt while keeping the prior failure auditable ([artifacts.py:56-77](../../../src/wastech_orchestrator/providers/artifacts.py#L56)). It collects every entry under `logs/<task-id>/` whose name does not start with `attempt-` ([artifacts.py:67](../../../src/wastech_orchestrator/providers/artifacts.py#L67)), then `rename`s each into a new `attempt-<N>/` directory ([artifacts.py:70-76](../../../src/wastech_orchestrator/providers/artifacts.py#L70)). It returns `None` when the task dir is absent or holds nothing to archive ([artifacts.py:65-69](../../../src/wastech_orchestrator/providers/artifacts.py#L65)). It is idempotent for an interrupted rerun: a destination name already present is skipped rather than overwritten ([artifacts.py:73-75](../../../src/wastech_orchestrator/providers/artifacts.py#L73)). Because the archive lives under the same task root, sibling task-level dirs (`checks/`, `prompt-audit/`, `summary.md`, …) are swept along with the stage logs.

### Serialization

Both writers funnel through `_write_json`, which dumps `indent=2, ensure_ascii=False, sort_keys=False` and appends a trailing newline, UTF-8 ([artifacts.py:122-125](../../../src/wastech_orchestrator/providers/artifacts.py#L122)). `write_request_artifact` copies the mapping (`dict(redacted_request)`) ([artifacts.py:114](../../../src/wastech_orchestrator/providers/artifacts.py#L114)); `write_result_artifact` flattens the dataclass with `dataclasses.asdict`, which serializes nested enums and the `NormalizedError` ([artifacts.py:119](../../../src/wastech_orchestrator/providers/artifacts.py#L119)) — the test confirms `status`, `node_id`, and `error.error_class` land in `result.json` (the enum fields as their `.value` strings, `node_id` as the plain string) ([test_artifacts.py:88-91](../../../tests/providers/test_artifacts.py#L88)).

### What this module does NOT do (caller boundary)

`write_result_artifact` serializes whatever `AgentRunResult` it is handed — it performs no normalization itself. The non-secret session id in `result.json` is produced by the **provider** (B18), which calls `_redact_result_session` to replace the raw session id with `normalized_session_id(...)` (B21) before invoking the writer ([codex.py:505](../../../src/wastech_orchestrator/providers/codex.py#L505), [codex.py:613-621](../../../src/wastech_orchestrator/providers/codex.py#L613)). The raw session id is kept on the in-memory result for the `editing_lineage` store (state.db only, B07) and never reaches disk ([codex.py:503-504](../../../src/wastech_orchestrator/providers/codex.py#L503)). Likewise the redaction of `request.json` is done by the provider before `write_request_artifact` ([codex.py:526-531](../../../src/wastech_orchestrator/providers/codex.py#L526)). This module never imports B21 ([artifacts.py:13-23](../../../src/wastech_orchestrator/providers/artifacts.py#L13)).

## Invariants & guarantees

- **`<artifacts_root>` is the gitignored `<repo>/.worc/` home** — `worc_home_for(config)` returns `Path(repo.local_path) / ".worc"`, and the whole dir is gitignored ([cli.py:503-510](../../../src/wastech_orchestrator/cli.py#L503), `WORC_HOME = ".worc"` [cli.py:71](../../../src/wastech_orchestrator/cli.py#L71)). All run traces live under `<repo>/.worc/logs/<task-id>/...`. The committed audit trail is the exception: the task file and its `<id>.summary.md` stay at the repo-root `tasks/`, since `tasks_root_for` is the repo root, not `.worc/` ([cli.py:513-519](../../../src/wastech_orchestrator/cli.py#L513)). The `logs/<task-id>/summary.md` is only a working copy / PR-body fallback ([orchestrator.py:1512-1513](../../../src/wastech_orchestrator/core/orchestrator.py#L1512)).
- **Logs are never overwritten** — `exist_ok=False` ([artifacts.py:101](../../../src/wastech_orchestrator/providers/artifacts.py#L101)); collision is a hard `FileExistsError`.
- **No state is held** — every path is recomputed from arguments; the layout's single source of truth is the path-construction code itself.
- **Provider/redaction-agnostic** — imports only `providers/base` ([artifacts.py:23](../../../src/wastech_orchestrator/providers/artifacts.py#L23)); content arrives already redacted.
- **Archiving is non-destructive and idempotent** — `rename` (move) not delete; existing archive entries are preserved ([artifacts.py:73-76](../../../src/wastech_orchestrator/providers/artifacts.py#L73)).

## Dependencies

- **Uses:** B18 (`providers/base` — `AgentRunResult` for result serialization; the `node_id` segment is the request's flow node id, not a `Stage`). **Used by:** B18 (Codex/Claude providers call `create_attempt_dir` and the two writers, and join `output-schema.json`/`last-message.txt` onto `attempt_dir`), B24 (check execution joins `task_artifact_dir(...) / "checks"` and writes `<run-id>.log` with its own never-overwrite scheme — [check_runner.py:114](../../../src/wastech_orchestrator/check_runner.py#L114), [check_runner.py:184-188](../../../src/wastech_orchestrator/check_runner.py#L184)), B07 (the artifact row registers `sha256_file(path)` — [orchestrator.py:1779-1780](../../../src/wastech_orchestrator/core/orchestrator.py#L1779)), B06 (drives `archive_task_artifacts` on rerun and joins `task_artifact_dir` for plan/summary/subtasks/HITL). B21 supplies the redaction/normalization the providers apply before calling the writers.

## Audit candidates

- See [the audit](../../backlog/2026-06-21-audit.md). No defects were found in this module.

## Tests

- `tests/providers/test_artifacts.py` — pins the exact attempt-dir layout including the six-digit `run-` and `provider` leaf ([test_artifacts.py:24-31](../../../tests/providers/test_artifacts.py#L24)), the zero-padded `sub-NN` segment ([test_artifacts.py:33-47](../../../tests/providers/test_artifacts.py#L33)), the never-overwrite `FileExistsError` ([test_artifacts.py:50-53](../../../tests/providers/test_artifacts.py#L50)), distinct dirs per attempt and per `node_run_id` when the attempt counter resets ([test_artifacts.py:56-65](../../../tests/providers/test_artifacts.py#L56)), the `request.json` round-trip ([test_artifacts.py:68-72](../../../tests/providers/test_artifacts.py#L68)), and enum/`NormalizedError` serialization in `result.json` ([test_artifacts.py:75-92](../../../tests/providers/test_artifacts.py#L75)). The test suite does not directly cover `archive_task_artifacts` or `sha256_file` here (exercised via the orchestrator/rerun path).
