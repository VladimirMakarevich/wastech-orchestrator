# A settled task's own file is re-run, rejected as a duplicate, and quarantined out of Git

Status: **proposed** Date: 2026-09-09 Owner: Vladimir Makarevich

## Problem

Under the **default** configuration, five minutes after a task finishes successfully the daemon picks its task file up again, the gate rejects it as `duplicate_task_id`, and the reject path files a `failed` ledger record, sends a failure notification, and **moves a git-tracked file out of the operator's working tree**. The work is fine — branch pushed, checks green, PR open — but every operator-facing signal says the task failed, and `git status` on the base branch shows the task file deleted.

A guard against exactly this exists and is documented. It cannot fire, because it compares against a value that the same publish step rewrote.

Found on `wastechlab-mobile-template` (Ionic/Angular target repo) on 2026-09-09, orchestrator `0.12.0a1`, task `003-01-orm-read-coercion`, PR #8.

## Current behavior (verified)

The chain, with the code that does each step:

1. **Promote is committed to the base branch.** The operator promotes, `tasks/preparing/<id>.md → tasks/pending/<id>.md`, and that move is committed on `main`. This is by design: `git.footprint` commits the task file and its summary under `tasks/` as the audit trail.
2. **The run succeeds.** Publish prep moves the file to its lifecycle folder — [`orchestrator.py:4756`](../../src/wastech_orchestrator/core/orchestrator.py) — and the same call rewrites the ledger:

   ```python
   src.replace(dest)
   self._store.update_task(task_id, source_path=str(dest))
   ```

   `source_path` now reads `tasks/done/<id>.md`.
3. **That move is committed on the task branch**, not on base: `git.footprint.audit_on_branch: task` is the **default** (`config.example.yaml`).
4. **Terminal cleanup returns the working tree to base.** Base's `HEAD` still carries the file at `tasks/pending/<id>.md` — step 1 committed it there and step 3 filed it away somewhere else. The file resurfaces in `pending/`.
5. **The next watch tick scans `pending/`** — `orchestrator.poll_interval_seconds` defaults to `300`. In the observed run the terminal record is `18:24:31` and the reject is `18:29:35`: 5 minutes 4 seconds, the first tick after success.
6. **The guard is consulted and returns `False`.** [`_already_settled`](../../src/wastech_orchestrator/cli.py) (`cli.py:1728`, called at `cli.py:1806`) ends in:

   ```python
   return Path(row.source_path).resolve() == task_file.resolve()
   ```

   with, in the observed run:

   ```text
   ledger  row.source_path = <repo>/tasks/done/003-01-orm-read-coercion.md
   scanner task_file       = <repo>/tasks/pending/003-01-orm-read-coercion.md
   ```

7. **The gate rejects and `_reject` runs** (`orchestrator.py:4788`): `write_validation_report`, `_quarantine(task_file)`, a `failed` `LedgerRecord`, and `_notify_terminal(..., final_status=Status.FAILED, ...)`.

The guard's own docstring names step 4 as a case it must handle:

> A `manual_action_required` task keeps its file in `pending/` by design …; **a committed-`tasks/` done/failed move can also resurface in `pending/` after a base-branch checkout.** Either way the daemon must **not** re-run it — that would re-reject it as `duplicate_task_id` and quarantine the operator's file.

## The defects

**D1 — the guard never fires in the case it was written for.** `_already_settled` decides "is this my own leftover?" by exact-matching a `source_path` that is stale by construction: the publish step that causes the resurface is the step that repoints `source_path` at `done/`. For the resurface case the two paths can never be equal, so the guard is dead code on the path it was added for. It still works for `manual_action_required`, where no move happens and `source_path` still reads `pending/`.

The codebase already knows this field desyncs. [`_resolve_task_source`](../../src/wastech_orchestrator/core/orchestrator.py) (`orchestrator.py:1159`), on the rerun path, exists solely to cope with it:

> The stored `source_path` can point at a stale lifecycle folder (e.g. `tasks/failed/`) while the file now lives in another (`tasks/pending/`) — a manual or external move then makes the task un-rerunnable if we trust the single stored path.

One path is tolerant, the other trusts the single stored path. Same problem, two rigors.

**D2 — the guard is `watch`-only.** `_already_settled` has exactly one call site, in `watch_once`. `worc run <file>` on a resurfaced task file goes straight to the gate with no guard at all, so it produces the same reject, the same false `failed`, and the same quarantine.

**D3 — a successful task reports as failed.** The reject appends a second terminal record for an id that is already terminal. In the observed run `completed.jsonl` holds both:

```json
{"id": "003-01-…", "final_status": "done",   "pr_url": ".../pull/8", "validation_reason": null}
{"id": "003-01-…", "final_status": "failed", "pr_url": null,         "validation_reason": "duplicate_task_id"}
```

The `tasks` row itself stays `done`, so `worc status` is correct — but the run journal, and `_notify_terminal`, are not. With Telegram enabled the operator gets a failure message for a task whose PR is green and open. That is what made this visible: the operator read it as the task having failed or parked.

**D4 — the quarantine relocates a git-tracked file.** `_quarantine` (`orchestrator.py:4817`) moves the rejected file into the gitignored `.worc/tasks/rejected/`. But `tasks/` is tracked **by design** — `git.footprint` is built on committing exactly these files. The result is a dangling deletion on the base branch:

```text
 D tasks/pending/003-01-orm-read-coercion.md
```

The operator did not delete anything; the orchestrator mutated tracked content in their working tree as a side effect of a duplicate it was supposed to skip. Worth deciding independently of D1: even for a *genuine* duplicate, silently moving a tracked file out of the tree is a surprising remedy.

**D5 — no test coverage.** `grep -rn "already_settled" tests/` returns nothing. The guard has never been exercised, which is why a condition that can never be true survived.

## Scope

Not an edge case. Every element of the chain is a default:

| Element | Value | Default? |
| --- | --- | --- |
| `git.footprint.audit_on_branch` | `task` | yes |
| `orchestrator.poll_interval_seconds` | `300` | yes |
| `tasks/` tracked in Git | yes | yes — it is the audit trail |
| Flow | `implementation` | yes |

The trigger is "a task completed under `worc watch` and its PR is not merged before the next tick", which is the ordinary path rather than an unusual one. It does not reproduce under a bare `worc run` with no daemon, because nothing re-scans — but see D2 for the case where the operator runs the resurfaced file by hand.

## Proposed minimal design

1. **Make the leftover test tolerant of the lifecycle folder, like the rerun path already is.** Treat a scanned file as the task's own leftover when its id is terminal in the ledger and the file sits in any `tasks/{pending,done,failed}/` under the same tasks root — reusing `_LIFECYCLE_FOLDERS` (`orchestrator.py:271`) rather than a single stored path. The reason the equality check exists — telling "my own leftover" from "a different file that reuses a used id" — is better served by comparing **content** (or the recorded task hash) than by comparing the folder the file happens to sit in.
2. **Move the guard behind the gate call, not beside it**, so `worc run` gets it too (D2). One decision point for "this id is already terminal and this is its own file", consulted by every entry path.
3. **Never quarantine a file Git tracks** (D4). At minimum, skip the move and say so; the report and ledger record already carry the reject.
4. **Do not append a second terminal record for an id already terminal**, and do not notify for it (D3). A duplicate that the guard should have caught is an operator-visible warning at most.
5. **Cover it**: a regression test that runs a task to `done` with `audit_on_branch: task`, restores the base-branch tree, ticks the scanner once, and asserts no reject, no second ledger record, no notification, and the file untouched.

## Open questions

1. **Content comparison or recorded hash?** Comparing bytes is simple but re-reads the file; the normalized task is already persisted, so a stored hash may be cheaper and more precise. Either way it must not treat a *reworded* file as a new task — that is a duplicate id and should still be rejected loudly.
2. **Should the resurface be prevented rather than tolerated?** The file only reappears because the `done/` move is committed on the branch while the promote is on base. An alternative is filing the audit move on base at merge time — but that reintroduces base-branch writes the current footprint policy deliberately avoids. Tolerating the resurface looks correct; this question is whether it should *also* stop happening.
3. **What should the operator see?** Today: nothing (guard fires) or a failure (guard misses). A third option is one quiet line per settled leftover — useful the first time, noise on every tick for a long-lived unmerged PR.
4. **Does the same hole exist for `failed`?** A task that ends `failed` gets its file moved to `tasks/failed/` with the same `source_path` rewrite, so the identical resurface should occur. Not observed, not tested — worth confirming before fixing, since it widens the regression test rather than the design.

## Scope / risk

CLI and orchestrator-internal: no schema change, no flow change, nothing on the publish path. The risk is in the direction of the fix, not its size — a leftover test that is too permissive would let a genuinely different file reuse a settled id and silently skip it, which is worse than today's noisy reject. That is why the content/hash comparison, not the folder, carries the identity decision.

## Depends on

Nothing.

---

## Adjacent, unrelated: `validate-flow` prints no violations

Found in the same session and needing its own item. [`cmd_validate_flow`](../../src/wastech_orchestrator/cli.py) (`cli.py:3688`) reports a failure as:

```python
print(f"flow {check.name}: FAIL — {check.error.splitlines()[0]}")
```

`FlowValidationError` builds its message as a header line followed by one line per violation, so `.splitlines()[0]` prints the header and discards every finding. The operator sees:

```text
flow implementation: FAIL — flow validation failed (2 violation(s)):
```

and nothing else — a trailing colon promising a list that never comes, at any `--log-level`. The violations were only recoverable by calling `FlowRegistry.check_flows` directly from Python. In the observed case they were two clear, actionable lines:

```text
[ceiling] agent 'implementation': resume_role_file requires session_scope editing_lineage
[ceiling] agent 'fixing':         resume_role_file requires session_scope editing_lineage
```

Fix is one line — print the whole `check.error`, indented, rather than its first line.
