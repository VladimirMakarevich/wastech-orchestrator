# P5 remediation — SKIPPED / deferred items

Status: **open** (target-repo + owner-smoke work, plus two in-repo deferrals) Date: 2026-07-08 Owner: Vladimir Makarevich

The orchestrator-code items of the P5 findings remediation shipped 2026-07-08 (branch `feat/p5-findings-remediation`, gate green) — the plan is archived at [archive/done/p5-findings-remediation-plan.md](archive/done/p5-findings-remediation-plan.md). This doc tracks the pieces deliberately **left out** of that change, so they are not lost. Three buckets: (1) work that lives in the **target repo** `wastech-mdlint/.worc/flows/` (a different repository, gitignored + install-seeded — not the orchestrator repo); (2) an **owner live-smoke** that cannot be driven non-interactively; (3) two **in-repo deferrals** whose value did not justify the added risk/complexity now.

## Target-repo work (not in this repository)

- **A1-substantive (F42) — demote "missing test coverage" from blocking to advisory in the target `review.md`.** The orchestrator fix only de-inerted the `max_rework_per_stage` footgun (doc/comment; the packaged `review.md` is already minimal and does **not** declare test-coverage blocking). The actual driver of the 7-cycle rework loop on `p5-04` was the target repo's **edited** copy `wastech-mdlint/.worc/flows/implementation/review.md`, which declares "Missing test coverage for user-visible behavior" a blocking invariant. Next step (in the target repo): demote coverage completeness to advisory, or narrow coverage-blocking to core user-visible behavior; keep correctness/invariants blocking. Optionally set a tighter `budgets.review_fix` on the target flow for a deterministic ceiling. Verify on the next large task: review on `high` + demoted coverage-blocking converges in ≤2 cycles without losing correctness findings.
- **D1-step 1 (F40) — task-authoring hygiene in the target repo.** Do not combine `depends_on: [predecessor]` (a merge-gate) with `branch_mode: existing` + a same-`branch_ref` shared branch (accumulation whose PR stays open until phase end) — the two are mutually exclusive chaining mechanisms and stalled all of P5 at step 2. Already applied for `p5-02..06`. The orchestrator now emits an **advisory warning** for this combination ([task/validation_gate.py](../../src/wastech_orchestrator/task/validation_gate.py)); the authoring rule itself stays a target-repo convention.

## Owner live-smoke (not drivable non-interactively)

- **E1 / F37 — prove Claude native-memory isolation is structural, not incidental.** Over all of P5 there were **0** new cards written to `~/.claude/projects/<target>/memory/` (the `--disallowedTools` deny on the config dir held by result), but the path is still announced to the spawned agent (~1 mention/node in `events.jsonl`, no `tool_use` against it) — so the agent merely chose not to write, which is not hard enforcement. Owner step: spawn a claude node with a prompt that provokes a native-memory write and confirm refusal (the agent neither reads nor writes `~/.claude/.../memory/`). If a write still lands, escalate to structural isolation (an isolated `CLAUDE_CONFIG_DIR`/settings, or confining `Write`/`Edit` to the working tree), cross-platform (do not hard-code `~/.claude`). Lever: [providers/claude.py](../../src/wastech_orchestrator/providers/claude.py). Pairs with the existing F37/F21-allowlist live-verify rows in [follow_ups.md](follow_ups.md).

## In-repo deferrals

- **A3 delta-observe (F50).** The shipped A3 fix caps the supervisor's per-step observe reasoning to `high`. The richer lever — observe only the **delta** (new/changed node-runs) instead of re-observing every repeated step in a fix loop — was deferred: it is fuzzier and risks dropping genuine supervision signal (each loop iteration produces new output). Revisit only if observe cost still dominates a deep-loop task after the reasoning cap (measure via `result.json.usage`). Lever: the observe cycle in [core/supervisor.py](../../src/wastech_orchestrator/core/supervisor.py).
- **C2 model/schema-400 split.** C2 shipped `ErrorClass.INVALID_INVOCATION` for argparse exit-2. The secondary step — a distinct class for a model/schema `400` (currently `process_crashed`) so codex-node 400s are triaged apart from generic crashes — was deferred as YAGNI (moot for the resume path after F38). Add only if a future codex grammar/schema drift makes the conflation hard to triage. Lever: [providers/codex.py](../../src/wastech_orchestrator/providers/codex.py) `_CODEX_SIGNATURES` / [providers/errors.py](../../src/wastech_orchestrator/providers/errors.py) `classify`.

## References

- Archived plan: [archive/done/p5-findings-remediation-plan.md](archive/done/p5-findings-remediation-plan.md)
- Implementation + SKIPPED rows: [follow_ups.md](follow_ups.md) (2026-07-08)
- Findings: [TEST-FINDINGS.md](../../TEST-FINDINGS.md) F37, F40, F42, F50
