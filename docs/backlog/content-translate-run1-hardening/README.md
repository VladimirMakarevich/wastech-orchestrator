# `content_translate` run 1 hardening — campaign

Status: **planned, decisions closed** Date: 2026-09-16 Owner: Vladimir Makarevich

## What this folder is

[inventory.md](inventory.md) is the evidence: sixteen defects (P0.1…P2.16) found in the orchestrator around the flow's first production pair, each with the row, artifact or source line that proves it. It is a record, not a work order — it states what is broken and why, not the order anyone should fix it in.

This README is the work order. The sixteen items are grouped into **three batches of three phases each**, in the order they must land, with every design question closed. Each batch is one pull request; each phase inside it is one reviewable commit.

The batches:

| Batch | Phases | What it buys |
| --- | --- | --- |
| [1 — concurrency and loop guards](batch-1-concurrency-and-loops.md) | 1–3 | Stops two processes writing one task; gives the flow the fix budget it declared; ends a non-converging loop on the third pass instead of the eleventh; stops a recovered task shipping as `done` with a failure report |
| [2 — the audit record](batch-2-audit-record.md) | 4–6 | Makes `state.db` answer "which model, under which flags, at what cost"; stops one column carrying two meanings; makes three existing-but-unheard signals reach the operator |
| [3 — the packet and the seams](batch-3-packet-and-seams.md) | 7–9 | Gives the supervisor the gate verdicts it writes the pull request from; stops minting evidence-free quarantine bundles; closes the node row an aborted merge leaves open; lets a repository name its own governance paths |

Ordering between batches is a correctness constraint, not a preference. Batch 1 phase 1 lands first or every later investigation reads rows two processes wrote — which is exactly how this document's own contradictory `node_runs` row came to exist.

## Decisions taken 2026-09-16

Six questions were open in the inventory. All are closed; the reasoning is recorded here so the phases can be read as instructions rather than as options.

| Question | Decision | Why |
| --- | --- | --- |
| **P0.2** — the tool stall detector versus a declared `budgets` entry | **Three-tier.** `findings` present → detector off. `findings` absent, `data` non-empty → the failure is actionable, the detector becomes a backstop firing at the declared budget. Neither → park on the second repeat, as today. | The detector exists to stop a tool repeating a _non-actionable_ failure. When the failure is actionable the fixer can close it, so the budget decides and the detector only guards against an endless loop. A node with no declared budget keeps the threshold of 2. |
| **P0.3** — where the persistent "the evaluator repeats itself" signal lives | **Derived from `evaluations`.** No new state at all: the table already stores `findings_json` per verdict with `node_id`. The engine gains a second injected callable beside `_diff_fingerprint`. Threshold: **3** consecutive identical finding sets. | Survives a restart by construction, keeps the engine domain-free, needs no migration. On this run the loop would have ended on the third pass instead of the eleventh. Its one dependency — that `evaluations` rows are written without loss — is what phase 1 fixes. |
| **P1.7** — the missing Codex cost | **Honest gap, no estimate.** No price table, no `usage_cost_source` column. The summary and `list` / `status` say what they do not know: "Codex cost not accounted (N attempts, M input tokens)". | A silent zero is worse than an honest gap, and a price table in the adapter is a number that goes stale between releases and is then read as fact. Stating the gap costs nothing and cannot be wrong. |
| **P1.8** — what a tool / evaluator step carries in the supervisor packet | **Inline for the agent, paths for the human.** The tool node's `data` and a bounded head of its stdout, and each evaluator step's findings, go **inline** under per-field caps beside `_STEP_MESSAGE_MAX`. Paths are rewritten to the private copies under `logs/` and serve the post-mortem reader only. | The private read-deny projection keeps `.worc` unreadable to agents at **either** value of `security.disable_read_isolation` ([isolation.py](../../../src/wastech_orchestrator/security/isolation.py)). So a path under `logs/` is durable but useless to the supervisor — only inline content reaches it. The inventory's step 4 is therefore necessary but not sufficient. |
| **P2.13** — how a repository names its governance paths | **An additive config key.** It appends to `GOVERNANCE_PATH_GLOBS` and cannot shrink it — there is no exclusion syntax to express a removal, and the validator rejects one. Bumps `CONFIG_SCHEMA_VERSION` 40 → 41. | Where a repository keeps its rules is repository knowledge worc cannot guess, and guessing wrong produces exactly the defect: a notice covering part of the governance and silently omitting the rest. The non-gateable guarantee survives because the key can only add. |
| **P2.16** — the branch a merged task leaves behind | **Out of scope, and not queued.** Only the terminal line ships: what was cleaned, what was kept, its size, and the command that reclaims it. No `worc branches clean`, no auto-delete, no follow-up item. | Owner's call: leftover task branches are not a problem worth code. Deleting branches is the one irreversible operation in this campaign and it is not being bought. |

Two further corrections to the inventory were made in the same pass, both recorded inline in [inventory.md](inventory.md):

- **P0.1's root cause was stale.** `cmd_run` _does_ check for a live executor — `_executor_owner` has been there since 2026-09-03 and shipped in the version that produced this run. The defect is that the check is not atomic: real work sits between it and `write_pid_file`, and the write clobbers unconditionally. `cmd_watch` has the same shape and a wider window.
- **`_STALL_REPEAT_LIMIT` does not exist.** P0.2 step 3 named a constant the codebase never had; the second-occurrence threshold is hard-coded through the in-memory `_last_no_finding_failure` map.

## Out of scope

- Everything belonging to the **target repository's** own flow and tools — the critic role that emits unfixable findings, the project tool that never populates `findings`, the gate that hides its measurements from the fixer. Those are the operator's copies and are tracked with that repository.
- Leftover task branches (see P2.16 above).
- Any change to the publication contract, the permission ceiling, or the isolation floor. This run proved all three held; see "What this run proves is working" in [inventory.md](inventory.md), which nothing in these nine phases may disturb.

## Working agreements for every phase

- One phase, one commit; one batch, one pull request against `dev`.
- Every phase updates the docs that live on `dev` in the same change — [.agents/rules/](../../../.agents/rules/), [README.md](../../../README.md), and the shipped operator-facing copy under `src/wastech_orchestrator/packaged/`. The derived `docs/` tree is not on this branch; leave a one-line doc-impact note in the pull request instead.
- `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports`, `pytest` and `python tools/mdlint.py` are green before a phase is called done.
- State-store columns are added to the `CREATE TABLE` **and** to the idempotent `_migrate_*` step, matching the existing additive pattern. No version gate, no backfill — the project is greenfield.
