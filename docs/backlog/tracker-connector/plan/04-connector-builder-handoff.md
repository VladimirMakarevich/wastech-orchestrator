# Phase 04 — Task builder and handoff

- **Status:** ☐
- **Depends on:** 03
- **Delivers:** FR-C3, FR-C4, FR-C5, FR-C6, FR-C7 — a gated item becomes a worc task file in `tasks/preparing/`, promoted with `worc promote`, exactly once, and the file passes worc's gate unchanged.

## Goal

Cross the boundary into worc through its public ingress and nothing else. Moves AC-3, AC-4, AC-5, AC-6, AC-7, AC-E2, AC-E8, AC-N2, AC-N4, AC-N7.

## Steps

1. `core/builder.py` — id allocation (`<id_prefix>-<number>[.<seq>]`, worc grammar), branch allocation (`<branch_prefix>/<task-id>-<slug>` ≤ 50 chars, valid ref, ≠ base branch), title sanitizer (D8), body assembly (provenance line + verbatim body), truncation to worc's three limits with a marker and the URL, front matter emitted only for configured keys, `commit_type_by_label`.
2. `core/handoff.py` — read `paths.tasks_dir` and the three `validation.max_*` limits from worc's `config.yaml` if present (defaults `tasks`, 262 144 / 5 000 / 8 192); ensure `tasks/preparing/` exists; atomic write (temp + `os.replace`, UTF-8, `newline=""`); run `worc promote <id>` as argv (`shutil.which("worc")`). `cmd_promote` prints each outcome to **stdout** as `promote: …` and exits 1 on any error — treat exit 1 with a stdout line containing `already in pending` as success (the file is worc's), any other non-zero exit as "keep `staged`, retry".
3. `core/state.py` — phases `gated → staged → queued`; re-trigger allocates `seq + 1`.
4. `core/reconcile.py` (first half) — on tick, a `staged` row re-runs promote; a file missing from both folders is resolved through `worc list --format json --all` (row present → queued; absent → failed — this is also what a gate reject looks like from outside `.worc/`; the `rejected` section of the same listing names the reason once phase 06 adopts FR-W3).
5. Fake `worc` executable for tests: `promote` moves the file (refusing an existing target), `list --format json` returns a fixture.
6. Add worc as a **test** dependency so AC-4 can run the generated file through the real `task.validation_gate`.

## Files touched

- connector: `src/worc_connect/core/{builder,handoff,reconcile,state}.py`, `tests/` (fake `worc`, builder and handoff suites).

## Invariants in play

- The only path written in the clone is `tasks/preparing/<id>.md`; never `tasks/pending/`, never `.worc/`, never `git`.
- Item text reaches only the task file body; ids and URLs are the only item-derived values in logs.
- The builder's key set is a strict subset of worc's `ALLOWED_TASK_KEYS` and never emits `nodes`, `subtasks`, `decomposition`, `trust_level`, `prompt_audit`.
- Cross-platform file writing (`newline=""`, atomic replace).

## Tests

- Sanitizer: each rejected token, the `Issue #n` fallback, whitespace collapse, length cap.
- Id/branch: grammar, 50-char cap, base-branch clash, sequence suffix.
- Truncation: each of the three worc limits.
- Handoff: crash between write and promote (AC-3), "already in pending" (AC-E2), missing `preparing/` (AC-E8), argv recording (AC-7), sentinel string never leaves the task file (AC-N2).
- Real-gate test: the generated file passes worc's Phase A (AC-4).

## Docs to sync in this phase

- Connector configuration reference (`task.*` keys, what is emitted when).

## Acceptance for this phase

- [ ] A labelled issue on a throwaway repository becomes a task in `tasks/pending/` that `worc watch` claims and runs to a PR, with no connector write-back yet.
- [ ] AC-3 … AC-7, AC-E2, AC-E8, AC-N2, AC-N4, AC-N7 pass on both CI families.
