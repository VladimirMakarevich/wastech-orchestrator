# Post-mortem: P11-remediation → P12-consistency (20 runs on wastech-mdlint)

Analysis of every orchestrator run from `p9-11-01-cli-bin-noop` through `p9-12-06-process-boundary-tests` — the tasks behind `docs/mdlint_v2/P11-remediation/` and `docs/mdlint_v2/P12-consistency/` in the [wastech-mdlint](https://github.com/VladimirMakarevich/wastech-mdlint) repo. Produced with the [`/analyze-task-run`](../../../.claude/skills/analyze-task-run/SKILL.md) methodology applied per task, then cross-synthesised.

**Read-only analysis.** Nothing in either repo was modified. Every finding names the lever; none of them has been pulled.

## Verdict

The pipeline works. All 20 tasks reached `done` on the first attempt with zero provider retries, zero fallbacks, zero crashes and zero quarantine events, and the shipped diffs are on-scope with genuinely good test discipline (~48% of added lines are tests). The review evaluator is the standout component — it repeatedly read third-party sources (npm's bundled `bin-links`, `libnpmexec`, `@npmcli/config`, Commander's dispatch code) to disprove confident-but-wrong claims, and it caught real regressions that no automated gate in this setup could see.

Two things are genuinely broken and are still broken at HEAD: a structured-output schema deadlock that published the word `test` as one task's entire whole-task summary, and a permission gap that let agents write durable notes into Claude Code's host-side memory store while the orchestrator believed memory was disabled.

The single largest quality lever has already been pulled, by you, mid-campaign — and the data proves it was the right call. Everything else worth doing is a prompt edit or a config key, not an architecture change.

## The one-line version of each ranked action

| # | Action | Lever | Scope | Evidence |
| --- | --- | --- | --- | --- |
| 1 | Fix the finalize schema deadlock that published `summary: "test"` | `core/supervisor.py` `_finalize_schema` / `_FOLLOW_UPS_SCHEMA` + `summary.md` role file | orchestrator | [02](02-critical-defects.md#c1) |
| 2 | Close the `~/.claude` write-deny gap under `disable_read_isolation` | `providers/claude.py:737` | orchestrator | [02](02-critical-defects.md#c2) |
| 3 | Stop the PR-body compactor pointing at a gitignored path | `core/supervisor.py` `_bound_pr_body` | orchestrator | [02](02-critical-defects.md#c3) |
| 4 | Teach `planning.md` to label claims verified-vs-assumed, and ban "don't re-verify" | `packaged/flows/implementation/planning.md` | both | [03](03-prompt-findings.md#p1) |
| 5 | Tell `implementation.md` the plan is a hypothesis, not a specification | `packaged/flows/implementation/implementation.md` | both | [03](03-prompt-findings.md#p1) |
| 6 | Give `review.md` an explicit accept/rework rule and the severity enum | `packaged/flows/implementation/review.md` | orchestrator | [03](03-prompt-findings.md#p2) |
| 7 | Extend the verify-your-claims clause to third-party tool behaviour | `planning.md` / `implementation.md` / `fixing.md` | orchestrator | [03](03-prompt-findings.md#p3) |
| 8 | Set `proseWrap: never` in the target's `.prettierrc` | `wastech-mdlint/.prettierrc` | target | [04](04-flow-and-config-findings.md#t1) |
| 9 | Add `npm ci` to the target's check command set | `wastech-mdlint/.worc/config.yaml` | target | [04](04-flow-and-config-findings.md#t2) |
| 10 | Curb in-node gate re-runs and serial single-hunk edits | `implementation.md` / `fixing.md` | orchestrator | [03](03-prompt-findings.md#p4) |
| 11 | Record `model`/`reasoning` on `node_runs`; stamp `commit_sha_before` | `state_store.py`, `core/flow/nodes/agent.py` | orchestrator | [05](05-observability-gaps.md#o1) |
| 12 | Resolve maintainer decisions before queueing; show the HITL deadline | task authoring; `notify/telegram.py:557` | both | [04](04-flow-and-config-findings.md#t4) |

## Document map

- **[01 — Frame and economics](01-frame-and-economics.md)** — what ran, what it cost, the natural A/B experiment in the middle of the campaign, and the empirical cost law.
- **[02 — Critical defects](02-critical-defects.md)** — the four things that are actually broken, all present at HEAD.
- **[03 — Prompt findings](03-prompt-findings.md)** — why 8 of 20 runs took a rework loop, and the exact wording to change.
- **[04 — Flow and config findings](04-flow-and-config-findings.md)** — the graph, the checks set, decomposition, HITL.
- **[05 — Observability and audit gaps](05-observability-gaps.md)** — what the audit tables cannot currently tell you.
- **[appendix/](appendix/)** — the seven per-task deep-dive reports (`batch-A` … `batch-G`), each covering 2–4 runs with full artifact citations. These are the primary evidence; the numbered documents above are the synthesis.

Task coverage by appendix: **A** = p9-11-01/02/03 · **B** = p9-11-04/07/10 · **C** = p9-11-14, p9-12-05 · **D** = p9-11-05/06/08/11 · **E** = p9-11-09/12/13 · **F** = p9-12-01/02/03 · **G** = p9-12-04/06 plus the supervisor-layer audit.

## Corrections to assumptions this analysis started from

Recorded because each one changed a conclusion, and because a later reader working from the same artifacts will hit them too.

- **The 20 runs did not share one model configuration.** `.worc/flows/implementation.yaml` has mtime `Jul 28 00:21` — it was edited _between_ p9-11-07 and p9-11-08. Reading the file as it sits today and attributing it to the whole campaign is wrong for the first seven runs. The authoritative per-run record is `stages/<node>/run-*/1-claude/request.json` and `runs/control-bundles/<task-id>/`, not the live YAML. Independently caught by appendices A, B, D and F.
- **`node_runs` cannot tell you which model ran.** It stores `provider_used` but not `model`/`reasoning`, which is why the above was easy to miss — no SQL query can recover it.
- **The supervisor's cost _is_ tracked.** Its 147 invocations are the `provider_attempts` rows with `node_run_id IS NULL`. A `GROUP BY` on the node join silently drops them into a NULL bucket: $325.16 total = **$306.02 flow nodes + $19.14 supervisor**. There is no cost roll-up surface anywhere in the codebase, which is the real finding.
- **There were two HITL stops, not zero** — `planning` approvals on p9-12-01 and p9-12-04. They are the entire explanation for the "double planning" anomaly.
- **Accepted review findings are not silently dropped.** All 62 become PR-body follow-ups, and the `documentation` node — which receives the review's `findings.json` as a context file — closed 9 of the 15 in one appendix's sample. The leak is narrower and different from what it looks like from the DB alone.
- **`usage_reasoning_output` being NULL is documented behaviour, not a bug** (`providers/claude.py:824-843`): the CLI folds reasoning into output tokens. It is nonetheless _recoverable_ — see [05](05-observability-gaps.md#o2).
- **A `when`-gated "small task" skip is not an available lever.** Only two flow facts exist, unknown facts default to `False`, and adding a task-size fact would violate the flow-agnostic-engine invariant. The compliant path is per-task `nodes.planning.enabled: false`.

## What to keep

- **The `review` evaluator.** Best value per dollar in the pipeline at $1.1–4.8 per pass against $6–11 rework rounds. Do not weaken it — and reconsider the `xhigh → high` downgrade it took on Jul 28.
- **`documentation` at `reasoning: medium`.** $0.60–1.90, 68–213 s, and it closes accept-verdict doc findings that the graph has no other path for.
- **The `Fix The Finding, Then Its Class` section in `fixing.md`.** 7 of 8 rework loops resolved in a single round. Keep it verbatim; only scope the class sweep by severity.
- **Governance-change detection.** p9-12-06 modified `AGENTS.md` and `.agents/rules/testing.md`; the daemon logged it and it propagated to the PR body and Telegram. Verified legitimate — it was the task's deliverable.
- **The exchange seals and the security envelope.** Byte-identical to `logs/`, no quarantine entries, `exchange_contaminated = 0` on all 20 tasks. No agent tried to write the read-only surface.
