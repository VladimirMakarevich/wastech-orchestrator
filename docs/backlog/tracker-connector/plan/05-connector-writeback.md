# Phase 05 — Write-back and reconciliation

- **Status:** ☐
- **Depends on:** 04
- **Delivers:** FR-C9, FR-C10, FR-C11, FR-C12 — state labels, comments, the PR link, close on merge and the failure path, all derived from `worc list --format json` and the PR found by branch; the first real end-to-end run.

## Goal

Make the connector visible to the people who use the tracker and make its state recoverable from the sources of truth. Moves AC-9, AC-10, AC-11, AC-12, AC-E3, AC-E4, AC-E5, AC-E6, AC-N3, AC-N6.

## Steps

1. `trackers/github/` write side: `set_state` (remove the previous `worc:*` label, add the new one, in one `gh issue edit` call), `comment` via `--body-file`, `close` via `gh issue close --comment`, `ensure_labels` via `gh label create` (skip when present; Q-9).
2. `core/writeback.py` — the six states, "exactly one label, re-apply is a no-op", comment templates (task id; PR URL; status + `worc status <id>`; rejection reason string) — templates only, never item text.
3. `core/reconcile.py` (second half) — the phase table from design "Control flow & state": `queued`/`running` from `worc list --format json --all`, matching the leading token of the `status` label (`new`/`validated`/`preparing`/`pending` → queued, `running` → in-progress, including `running (paused…)` and `parked (no daemon)`); `done` → find the PR by branch **once** and store its number and URL on the row; from then on read the PR by number (`get_pull_request`, for GitHub `gh pr view <n> --json state,mergedAt,url,title`) and recompute the PR-derived phase every tick — `mergedAt` → close (if configured) → done; `CLOSED` unmerged → failed with one comment, back to `pr-open` if reopened; `OPEN` → stay, whatever commits, title or branch changes happened (D14). `failed`/`manual_action_required` from `worc list` → failed; a gate reject (file gone from `pending/`, no row in `--all`) → failed with a comment that names the task id and `worc status <id>` and, in this phase, no reason — the connector does not read `.worc/`; phase 06 adds the `validation_reason` from the listing's `rejected` section (FR-W3).
4. Rebuild rows from labels + `worc list` when the state database is missing (AC-12); a `worc:pr-open` label with no stored number re-runs the by-branch discovery.
5. Follow-up on an open PR (Q-11, decided yes): a re-trigger from `pr-open` builds the new task with `branch_mode: existing` / `branch_ref: <previous branch>`; from a terminal phase, or once the PR is merged or closed, a fresh branch.
6. Log lines per action; `status` shows rows, phases, watermark, last tick.
7. First real end-to-end run on a throwaway repository, recorded in the connector README.

## Files touched

- connector: `src/worc_connect/core/{writeback,reconcile}.py`, `src/worc_connect/trackers/github/`, `tests/`.

## Invariants in play

- Comment bodies travel as files; label names come from configuration; nothing item-derived but the number and the URL appears in a comment or a log line.
- No log, diff, or error text from worc is posted to the tracker — the comment points at `worc status <id>` on the host.
- The connector follows worc's outcome; it never edits, re-queues or reruns a task on its own.
- The connector reads `worc list --format json` only — not `state.db`, not the ledger.

## Tests

- Label transitions and idempotency (AC-9); the three comment kinds and their contents (AC-10); close on merge on/off (AC-11); rebuild from labels (AC-12); rejection with reason string (AC-E3); PR closed unmerged (AC-E4); label removed while queued (AC-E5); watermark skew (AC-E6); no token anywhere (AC-N3); quiet tick writes nothing (AC-N6); owner's commits + retitle + squash merge + deleted branch (AC-16); follow-up on an open PR vs a merged one (AC-17); PR closed then reopened (AC-E9).

## Docs to sync in this phase

- Connector README: what each `worc:*` label means, what the operator does on `worc:failed`, the auto-merge caveat, and the recorded end-to-end run.

## Acceptance for this phase

- [ ] One real run: label → `worc:queued` + comment → `worc:in-progress` → `worc:pr-open` + PR link → merge → issue closed + `worc:done`.
- [ ] A second real run in which the owner pushes a commit to the PR, retitles it and squash-merges it with branch deletion: the issue still closes and the row ends `done`.
- [ ] AC-9 … AC-12, AC-E3 … AC-E6, AC-N3, AC-N6 pass on both CI families.
