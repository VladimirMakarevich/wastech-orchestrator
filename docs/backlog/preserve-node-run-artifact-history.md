# Preserve per-run history of operator-facing node artifacts

Status: **implemented** (2026-07-11) Date: 2026-07-10 Owner: Vladimir Makarevich

> **Implemented 2026-07-11.** Per-run operator artifacts now live under `stages/<node_id>/run-<node_run_id:06d>/` (via the new `artifacts.node_run_dir`), with a per-node `stages/<node_id>/history.jsonl` index written centrally in the orchestrator's `post_node` hook (one line per executed node run of every kind — an improvement over the per-writer sketch below). Three refinements surfaced during build: (1) resume rebuilds `review_path` from the store's latest **`in_flow_verdict`** row (supervisor rows carry no `node_id`/`run_id`); (2) `node_run_dir` is a pure path and callers mkdir — `checks`/`tool` run dirs are not pre-created by the provider adapter; (3) the downstream `{<node_id>_path}` resolver (`latest_run_file`) picks the newest run **containing** the file, so an empty/infra-failed newest run doesn't shadow a real one. Retention needed **no** `prune_attempt_artifacts` change — payloads sit above the pruned leaf `<attempt>-<provider>/` dir — and the "total wipe" is the existing `worc logs clean`, not a new command.

A concrete observability fix (not exploratory): make the human-readable, on-disk artifacts a node produces keep their per-run history instead of being overwritten on every re-run. Today only the machine tiers (per-attempt provider logs, the SQLite audit tables) preserve history; the operator-facing files an engineer actually opens to debug a run keep only the last pass. The reported symptom is the `review` node — in a fix→review loop its `findings.json`/`summary.md` are clobbered each cycle — but the root cause is one keying rule applied inconsistently, so the fix is systematic across every task-level surface a re-running node writes.

## The problem

The orchestrator keeps artifacts in two tiers with opposite retention behavior.

**Preserved (keyed by a unique `run_id`, or append-only).** Per-attempt provider logs live at `logs/<task>/stages/<node>/run-<node_run_id:06d>/<attempt>-<provider>/{stdout.log,stderr.log,events.jsonl,request.json,result.json}`; the directory is created with `mkdir(..., exist_ok=False)` and `node_run_id` is a fresh SQLite id reserved before each run, so re-runs never collide ([artifacts.py:120-149](../../src/wastech_orchestrator/providers/artifacts.py#L120-L149)). The prompt-audit step is keyed by `run_id` and also appends to a `timeline.jsonl` ([observability.py:171-175](../../src/wastech_orchestrator/core/flow/observability.py#L171-L175)). The SQLite `evaluations` / `provider_attempts` / `node_runs` tables are append-only and namespaced by `source_node_run_id` ([evaluator.py:157-167](../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L157-L167)).

**Overwritten (keyed by `node_id` or a constant filename, `write_text` = truncate).** The operator-facing task-level artifacts are written to fixed paths, so any node that runs more than once leaves only its last pass on disk:

| Surface | Path | Site |
| --- | --- | --- |
| Review findings + summary | `review/findings.json`, `review/summary.md` | [evaluator.py:174-186](../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L174-L186) |
| Rendered prompt | `stages/<node>/rendered-prompt.md` | [observability.py:103-124](../../src/wastech_orchestrator/core/flow/observability.py#L103-L124) |
| Generic node output `{<node>_path}` | `<node_id>.out.md` | [postprocess.py:83-118](../../src/wastech_orchestrator/core/flow/postprocess.py#L83-L118) |
| Checks reports | `checks/citation.json`, `checks/dependency_scan.json` | [checks.py:120-167](../../src/wastech_orchestrator/core/flow/nodes/checks.py#L120-L167) |
| Tool-node output | `tools/<node>/stdout.txt`, `stderr.txt` | [tool.py:190-198](../../src/wastech_orchestrator/core/flow/nodes/tool.py#L190-L198) |

The canonical case is the `review → fixing → testing → review` loop ([implementation.yaml:148-162](../../src/wastech_orchestrator/packaged/flows/implementation.yaml#L148-L162)): the engine re-enters `review` each cycle with a fresh `node_run_id`, writes the per-run provider logs and the prompt-audit step to distinct locations — but writes `review/findings.json` and `summary.md` to the same fixed path, so cycle N clobbers cycle N-1. The history of what each review pass found is gone from disk. `archive_task_artifacts` only protects across a whole-task `rerun` ([artifacts.py:96-117](../../src/wastech_orchestrator/providers/artifacts.py#L96-L117)); it does nothing within a single run's loop.

Two aggravations. The evaluator directory is the constant string `"review"` regardless of `node.id`, so a second evaluator node (e.g. `test_quality`) writes to the same directory and clobbers the review file too. And the irony: the SQLite `evaluations` row already holds each pass's findings, append-only — so the data exists; the human-readable copy an operator opens to answer "what was found, and when?" is the one tier that throws it away. That is exactly the debugging use the feature request is about.

## Constraints

**No secrets in artifacts** ([security.md](../../.agents/rules/security.md)). Every writer above already redacts its content before writing; the new per-run paths carry the same content to a different filename, so no new secret surface is introduced — redaction must be preserved at each moved write.

**Cross-platform** ([CLAUDE.md](../../CLAUDE.md) hard invariants). Reuse the existing zero-padded `run-<id:06d>` directory convention; do **not** introduce a `latest` symlink (Windows symlink creation is privileged) — the downstream `{<node>_path}` / `{review_path}` input is already set in-process to the file just written ([evaluator.py:186](../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L186)), so retargeting it at the current run's file needs no symlink or copy. The `history.jsonl` index appends with `newline="\n"` like the existing `timeline.jsonl`.

**Never-overwrite is already the documented rule** for per-attempt dirs ([artifacts.py:130-136](../../src/wastech_orchestrator/providers/artifacts.py#L130-L136)). This item extends that same rule to the task-level artifacts, making the codebase consistent rather than adding a new principle.

**Retention: keep everything; delete only on an explicit total wipe (decided).** The history is never removed by the incremental `logging.artifacts` level policy — the new per-run history artifacts are **exempt** from `prune_attempt_artifacts` (a `minimal`/`standard` level must no longer silently delete run history). The only thing that removes this history from disk is a single hard, explicit "delete absolutely everything" command (a purge of the whole task/logs tree, e.g. a `worc clean --all`-style command — new or existing); there is no partial/selective auto-deletion. This tightens the current behavior, where level-based pruning deletes per-attempt logs in the background.

**Greenfield MVP, no migration.** No back-compat for the old flat layout — just change the write path. No reader depends on the fixed `review/findings.json` location except the in-process downstream input, which is retargeted.

## Alternatives considered

| Option | Why rejected |
| --- | --- |
| Do nothing | History stays lost on disk; directly contradicts the debugging goal. |
| CLI over SQLite (`worc task-history` reading `evaluations`) | No file churn and a single source of truth, but produces no grep-able on-disk files (the stated want), and doesn't cover surfaces not in SQLite (rendered prompts, node outputs, tool outputs). Viable later **complement**, not the fix. |
| Single append-only `findings.jsonl` per node | Mirrors the prompt-audit `timeline.jsonl`, but stuffing full structured findings + summary prose into one growing JSONL is awkward to read per-pass. We adopt only the _index_ idea from this (the `history.jsonl` line-per-run) and keep per-run files for the payloads. |
| Timestamp / counter suffix (`findings-1.json`) | `node_run_id` is the natural key and is already reserved in SQLite; a separate counter would duplicate run-identity state. |

## Decision

Extend the existing "never overwrite, key by `run_id`" rule — today applied only to per-attempt provider directories — to **all** operator-facing task-level artifacts that a re-running node produces. Each such artifact moves under a per-run directory keyed by the node's reserved `node_run_id`, mirroring `logs/<task>/stages/<node>/run-<id>/`, and each node keeps a chronological `history.jsonl` index (one line per run: `run_id`, verdict/status, finding counts, relpath) so an operator can read the sequence without listing directories. The rendered prompt is co-located under that same `stages/<node>/run-<id>/rendered-prompt.md`, next to the run's provider attempts. The downstream `{<node>_path}` contract is unchanged — it already receives an in-process path, now pointed at the current run's file. Additionally, key the evaluator's output directory by `node.id` instead of the constant `"review"` so distinct evaluators stop clobbering each other.

History is exposed as on-disk files only (v1) — no CLI reader command. An operator browses `stages/<node>/run-*/` and reads the per-node `history.jsonl`; a `worc`-level view over the same data is a possible later complement, not part of this item.

**Scope of the per-run treatment.** It applies to the artifacts of nodes that re-run in a loop and produce different content each pass: review findings + summary, the rendered prompt, the generic `{<node>_path}` output (`<node>.out.md`), the checks reports (`citation.json` / `dependency_scan.json`), and tool-node stdout/stderr. It does **not** apply to the once-per-task output slots — `plan.md`, the enriched spec, and the finalize `summary.md` — which run exactly once and are read back by fixed name in resume recovery / finalize / the PR body; those stay flat. So the writer code carries two rules (per-run for loop artifacts, flat for the once-only contract slots), which is the deliberate trade for not disturbing the fixed-path readers.

The cost of the rejected alternatives: doing nothing keeps the debugging blind spot; the SQLite-only CLI leaves the non-SQLite surfaces (prompts, node outputs) unsolved and produces nothing to open in a file browser; a single JSONL trades per-pass readability for one file. The cost of this decision: more small files per task (bounded by the loop budgets — `review_fix: 15` / `global_fix_iterations: 30`), and a few writers must be threaded a `run_id` they don't all currently receive (notably `write_node_output`).

## Decided during shaping

All four shaping questions are resolved; nothing blocks moving this to "accepted".

- **Retention / pruning.** History is exempt from the incremental `logging.artifacts` level policy and removed only by an explicit total wipe (see Constraints).
- **`rendered-prompt.md` home.** Co-located under `stages/<node>/run-<id>/` (see Decision).
- **CLI view.** On-disk files only for v1; no reader command (see Decision).
- **Loop-reachable vs once-only boundary.** Per-run history for loop artifacts only; the once-only contract slots (`plan.md`, enriched spec, finalize `summary.md`) stay flat because they are reconstructed by fixed name in resume recovery ([orchestrator.py:1516](../../src/wastech_orchestrator/core/orchestrator.py#L1516)), the finalize "degraded" check ([orchestrator.py:2066](../../src/wastech_orchestrator/core/orchestrator.py#L2066)), and the PR body (see Decision → Scope).

## Implementation notes

- Add a single-source helper mirroring `create_attempt_dir` — e.g. `artifacts.node_run_dir(artifacts_root, task_id, node_id, node_run_id)` for the human-readable per-run payloads, plus a `node_history_path` helper for `<node>/history.jsonl` — so the layout rule stays in one place.
- [evaluator.py:174-186](../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L174-L186) `_write_findings`: key by `node.id` + `run_id`, append the index line, retarget `self._in.review_path`.
- [observability.py:103-124](../../src/wastech_orchestrator/core/flow/observability.py#L103-L124) `write_rendered_prompt`: accept `run_id`, write under the run dir.
- [postprocess.py:83-118](../../src/wastech_orchestrator/core/flow/postprocess.py#L83-L118) `write_node_output`: thread `run_id` (its caller does not pass one today) and key by it.
- [checks.py:120-167](../../src/wastech_orchestrator/core/flow/nodes/checks.py#L120-L167): key `citation.json` / `dependency_scan.json` by `run_id` (the per-command `checks/<NNN>.log` already uses a monotonic counter and is safe).
- [tool.py:190-198](../../src/wastech_orchestrator/core/flow/nodes/tool.py#L190-L198): key tool-node stdout/stderr by `run_id`.
- Retention: exclude the per-run history artifacts from `prune_attempt_artifacts` ([artifacts.py:162-177](../../src/wastech_orchestrator/providers/artifacts.py#L162-L177)) so a `minimal`/`standard` `logging.artifacts` level no longer silently deletes run history; the only removal path is an explicit total-wipe command (a `worc clean --all`-style purge — reuse an existing destructive command if there is one, otherwise a new one) that clears the whole task/logs tree at once.
- Doc-sync: the artifacts/observability layout docs under `docs/` and the operator guide under `src/wastech_orchestrator/packaged/guide/` if either documents the `logs/` tree; record any deferred slice in [follow_ups.md](follow_ups.md).
