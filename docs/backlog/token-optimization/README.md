# Token optimization (2026-07-16 analysis)

Status: **open — measurement + content hygiene shipped; supervisor roadmap pending** Date: 2026-07-16 Owner: Vladimir Makarevich

This folder groups the backlog items that came out of the 2026-07-16 token investigation into a single campaign with one execution order. The code and the mandatory rules remain authoritative; these documents are the design detail, not an implementation contract, and must not override the hard invariants in [../../../CLAUDE.md](../../../CLAUDE.md) / [../../../AGENTS.md](../../../AGENTS.md) / [../../../.agents/rules/](../../../.agents/rules/).

## Initial context

The trigger was a single content run (`blog-review-happy-in-my-misfortunes-4`) whose token cost was dominated by the supervisor and inflated by broken accounting. Two analyses frame the work: the token analysis (F1–F5) and [../../analysis/2026-07-16-supervisor-token-optimization-options.md](../../analysis/2026-07-16-supervisor-token-optimization-options.md) (variants A–I, target architecture §6, phasing §8).

Three findings drive everything here:

- **Accounting was wrong before anything could be tuned.** Codex resume usage is cumulative for the whole session, so naive per-node summation double-counted (a reported 424 163 vs. a true 282 699 input tokens, +50%); Claude's real input is split across three fields that were never summed. No honest token A/B is possible until usage is normalized and persisted per attempt.
- **The supervisor is the largest Claude-context consumer.** On the analyzed run seven supervisor calls spent 480 293 input tokens (~70% of the task's Claude input); six of the seven were intermediate observations, and the supervisor is advisory-only (it never routes). The savings are large and safe — but only if `finalize` stops depending on the warm session first.
- **Content flows resume heavy sessions for tiny edits.** `polish` re-ran a 31–37k-token transcript four times for a one-word edit because it inherited `revise`'s editing lineage; the session-scope docs never warned that resume grows input tokens on every turn.

## Items in this folder

| Item | What it does | Status |
| --- | --- | --- |
| [Normalized token-usage accounting](normalized-usage-accounting.md) | Provider-aware normalized usage persisted per attempt in SQLite: Codex resume delta stops double-counting, Claude input = sum of its three fields, raw payload kept for audit; also fixes the latent `_produced_no_work` bug on resumed Codex runs. The measurement substrate every token A/B depends on. | **implemented 2026-07-16** |
| [Content-flow token hygiene](content-flow-token-hygiene.md) | Two engine-free tweaks: a token-cost / history-growth warning in the session-scope docs, and packaged `blog_article_revise` defaults (`polish` → `fresh_disposable`, read-once / one-patch / one-diff role-prompt budget). | **implemented 2026-07-16** |
| [Supervisor P0 — SupervisorPacket → fresh finalize → skip tool/checks](supervisor-finalize-packet-and-cadence.md) | Build a deterministic `SupervisorPacket` from durable state, make `finalize` always run fresh seeded by it, then stop observing `tool`/`checks` nodes. The order is mandatory. | proposal (next) |
| [Supervisor P1 — observation cadence](supervisor-observation-cadence-p1.md) | `observation_mode: all\|selected\|events\|none`, event triggers, split observe/finalize model+reasoning, and `max_calls` / `max_digest_tokens` budgets. The main saver. | proposal |
| [Supervisor P2 — responsibility split + telemetry](supervisor-responsibility-split-p2.md) | Extract a deterministic `StepRecorder`, make `SubtaskHandoff`/`SkillProposer` separately budgeted, and persist per-function usage/cost with a supervisor report in the task summary. | proposal |

## Execution sequence

The order is not a suggestion — two of the dependencies are correctness constraints, not preferences.

| # | Item | Depends on | Why the order is fixed |
| --- | --- | --- | --- |
| 1 | Normalized usage accounting | — | Foundation. Without a summation-safe per-attempt usage record, every downstream token claim (the A/Bs below, P1 budgets, P2 telemetry) is unmeasurable or wrong. |
| 2 | Content-flow token hygiene | ships independently; A/B confirmation needs #1 | The default flip and docs ship without waiting; the "~60–80k saved, quality no worse" claim is only honest once #1 exists to measure it. |
| 3 | Supervisor P0 | measurement from #1 for its A/B | **`finalize` must become packet-first and fresh before any cadence change.** Cutting observations first strands `finalize` with an empty digest — it re-reads the repo or thins the summary and degrades `follow_ups` / `memory_delta`. |
| 4 | Supervisor P1 | **requires #3**; token budgets use #1 | `observation_mode: none`/`events` is safe only over the deterministic P0 packet; `max_digest_tokens` in real tokens needs #1 (char/count bounds suffice until then). |
| 5 | Supervisor P2 | **requires #4**; per-function telemetry uses #1 | Structural cleanup after the savings land — extracts the deterministic recorder P1 introduced and reports per-function usage on top of #1. Not on the savings critical path. |

Items 1–2 are done. The live critical path is **P0 → P1 → P2**; do not start a later phase before its predecessor is merged, and never change supervisor cadence before the P0 packet exists.

## Expected effect

For a comparable `blog_article_revise` pass: P0's `tool`/`checks` skip and fresh finalize alone remove ~44k + a large slice of the 104 567-token final call; P1's finalize-only cadence removes the historical 375 726 observation input tokens. Target: supervisor input falls from ~480k to ~30–60k with zero missed blocking issues — all A/B-gated on the normalized usage from item #1.
