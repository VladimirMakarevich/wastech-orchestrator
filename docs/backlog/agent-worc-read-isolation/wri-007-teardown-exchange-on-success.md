# WRI-007 — Tear down the task exchange on successful completion

**Status:** open **Phase:** 1 (hygiene) **Source:** [decision record](README.md), [happy-path.md](happy-path.md) **Dependencies:** WRI-001

## Problem

The exchange is agent-readable and in-repo (`<repo>/.worc-io/`). Once a task publishes successfully, its curated intermediate results (`plan.md`, `current.diff`, `findings.json`, `<node>.out.md`, the checks report, the memory packet) are no longer needed by any node — the flow has reached its terminal node and no further agent runs. But nothing removes them: task exchange dirs accumulate under `.worc-io/` until a manual `worc logs clean`. For the launched agent of the **next** task (whose `cwd` is the repo root), those leftover dirs are exactly the "other tasks' logs" the ADR classifies as _hide_ — they clutter the read surface and grow unbounded. This is the disposal half of the lifecycle WRI-001 introduced: WRI-001 keeps the exchange and private home in lockstep for `logs clean` / `rerun`; this task adds the automatic disposal on the successful-completion path.

## Required outcome

When a task reaches the terminal success status (`Status.DONE`), the orchestrator removes that task's exchange dir automatically — best-effort, idempotent, cross-platform — leaving the private-home audit trail fully intact. Non-success terminals (`FAILED`, `MANUAL_ACTION_REQUIRED`) **retain** the exchange for debugging, resume, and rework.

## In scope

- Add the teardown as a single helper (in `providers/artifacts.py`, e.g. `remove_task_exchange`) that removes the task's exchange dir, and call it from **both** code paths that land a task on `Status.DONE` — there are two, and they do **not** share a transition chokepoint:
  - pipeline success → `_go_terminal` ([orchestrator.py:2989](../../../src/wastech_orchestrator/core/orchestrator.py#L2989)), the normal happy-path close after `publish` commits/pushes/PRs (this is the common case — an earlier draft that hooked only `finalize_task` would have missed it);
  - operator finalize / merge-task (`MANUAL_ACTION_REQUIRED → DONE`) / `prs --sync` → `finalize_task` ([orchestrator.py:1367](../../../src/wastech_orchestrator/core/orchestrator.py#L1367)), which sets the status out-of-band via `set_status`. Hooking only one leaves exchange dirs behind for the other. Neither path reads any exchange artifact at finalize time (verified: `finalize_task`/`merge_task`/`plan_finalize` touch only the store row and git), so removal after the DONE write is safe.
- **Remove only the exchange task dir, never the private home.** The teardown deletes `<exchange_root>/<task-id>/` and nothing else. The private home keeps not just the audit trail but also the resume manifests (`normalized.json`, `skill_map.json`) and the `hitl/` subtree — deleting those would break crash-recovery/continue of a non-DONE task, which is exactly why teardown is scoped to the exchange dir alone.
- Fire **only** on `DONE`. On `FAILED` / `MANUAL_ACTION_REQUIRED`, retain the exchange — this is **required for resume, not merely for debugging**: `rerun --continue` and HITL resume re-read `plan.md` / `current.diff` / `findings.json` / the latest check log from the exchange via `_restore_engine_inputs` ([orchestrator.py:1729-1764](../../../src/wastech_orchestrator/core/orchestrator.py#L1729-L1764)), and `rerun` is refused for a `DONE` task ([orchestrator.py:939](../../../src/wastech_orchestrator/core/orchestrator.py#L939)). So continue/HITL only ever run against `FAILED`/`MANUAL_ACTION_REQUIRED` — the states this task keeps — and can never hit a torn-down exchange.
- **Do not touch the private home.** The audit trail (`rendered-prompt.md`, `prompt-audit/`, the `<attempt>-<provider>/` raw streams, supervisor `summary.md`/`summary.json`, `state.db`, the `memory/` store) is retained and is still cleaned only via the manual `worc logs clean` — this task does not change the private home's retention (audit-completeness invariant unchanged).
- Best-effort and non-fatal: a delete error (e.g. a Windows file lock) must not change the already-successful task outcome; log it, do not raise. Idempotent (a partially-removed dir on a retry is fine — `shutil.rmtree(..., ignore_errors=True)`, matching the existing `worc logs clean` behavior).
- Cross-platform: `pathlib`; tolerate Windows locked/read-only files via `ignore_errors`.
- Operator retention escape via the `logging` block: add `logging.clean_exchange_on_success: bool`, default `true` (clean on success). Setting it `false` keeps the succeeded task's exchange for debugging. The `logging` block already owns on-disk artifact retention (`level`, `artifacts`) ([schema.py:491](../../../src/wastech_orchestrator/config/schema.py#L491)), so the knob belongs there, not in a new block. The teardown reads this flag and no-ops when it is `false`. This is not a security relaxation (it retains already-agent-readable curated results and never exposes the private home), so it does not fall under the non-weakening deny invariant; the config validator needs no special guard for it.

## Acceptance criteria

- [ ] After a successful happy-path run, the task's exchange dir no longer exists; `.worc-io/` holds no dir for a `DONE` task.
- [ ] Teardown fires on **both** DONE producers — a pipeline-success DONE (`_go_terminal`) and an operator finalize / merge-task / `prs --sync` DONE (`finalize_task`) — a merged/finalized task leaves no exchange dir behind either.
- [ ] The private-home task dir is unchanged by teardown: `rendered-prompt.md`, `prompt-audit/`, raw provider streams, supervisor `summary.md`, `state.db`, and the resume manifests (`normalized.json`, `skill_map.json`, `hitl/`) all remain.
- [ ] After a `FAILED` or `MANUAL_ACTION_REQUIRED` run, the exchange dir is retained in full, and a subsequent `rerun --continue` / HITL resume successfully re-reads plan/diff/findings from it.
- [ ] Teardown runs strictly after `publish` (or the operator finalize) completes; no agent node runs for the task afterward.
- [ ] A delete failure is logged and does not flip the task off `DONE` or raise.
- [ ] The engine branches on no specific node id — teardown is keyed off the terminal status, generic across flows.
- [ ] Verified-unaffected by teardown (no exchange read): cross-task `depends_on` scheduling (store + ledger + git only), crash-recovery (never inspects a DONE task), and all inspection commands (`status` / `list` / `top` / `tasks`; there is no `logs show`).
- [ ] `logging.clean_exchange_on_success: false` keeps a succeeded task's exchange; the default (`true` / unset) cleans it. The flag is honored for both DONE paths.
- [ ] Docs (operations, configuration, guide) describe the success-teardown and the retention default; `/sync-docs` clean.

## Verification

- Pipeline/integration test with fake CLIs: a full successful run leaves no exchange dir and an intact private-home audit trail.
- Both-DONE-paths test: an operator `finalize` / merge-task (`MANUAL_ACTION_REQUIRED → DONE`) also tears down the exchange, not only the pipeline-success path.
- Resume-after-retain test: a `MANUAL_ACTION_REQUIRED` (or `FAILED`) task keeps its exchange, and a subsequent `rerun --continue` re-reads plan/diff/findings and completes.
- A failing run (checks/review exhausts, or publish fails) retains the exchange dir.
- Non-fatal test: teardown swallowing a delete error still reports `DONE`.
- Cross-platform path test for the teardown helper (POSIX + Windows).
- Retention-escape test: `logging.clean_exchange_on_success: false` keeps the exchange on a successful run; default/`true` cleans it. Config round-trip test (schema default, example, unknown-key fail-closed unaffected).

## Out of scope

- Cleaning the private home on success — the audit trail is retained; `worc logs clean` remains the manual lever (unchanged).
- Cross-task exchange visibility for **concurrent** tasks (a second in-flight task's exchange is still visible under the single-tree model) — that is the worktree/concurrency direction ([concurrent-task-worktrees.md](../archive/concurrent-task-worktrees.md)), not this task.
- The exchange↔private-home lockstep for `logs clean` / `rerun` — that is WRI-001.

## Open questions

- **Retention escape.** Resolved: `logging.clean_exchange_on_success` (bool, default `true`) in the existing `logging` block — clean on success by default, `false` keeps the exchange for debugging a completed run.
- **Fold-back vs delete.** This task deletes the exchange outright; the agent-facing outputs then survive only as the raw provider streams / `prompt-audit` in the private home (the plan/findings are the nodes' structured output, captured there; the diff is in the PR). If a curated, redacted copy of `plan.md`/`findings.json`/the diff must remain in the audit trail, fold them into the private-home task log before deleting the exchange instead of deleting outright. Decide during implementation; default is delete-outright (audit trail already carries the reconstructable record).

## Likely implementation areas

- src/wastech_orchestrator/core/orchestrator.py (call the teardown from both DONE producers: `_go_terminal` and `finalize_task`)
- src/wastech_orchestrator/providers/artifacts.py (a `remove_task_exchange` / teardown helper)
- src/wastech_orchestrator/config/schema.py (`LoggingConfig.clean_exchange_on_success: bool = True`); no validator guard needed
- src/wastech_orchestrator/packaged/config.example.yaml (the `logging:` block) and packaged/guide/config/reference.md (the `logging` table)
- tests/ (pipeline success + failure retention, both DONE paths, resume-after-retain, non-fatal, cross-platform, retention-flag round-trip)
- docs/operations.md, docs/configuration.md, src/wastech_orchestrator/packaged/guide
