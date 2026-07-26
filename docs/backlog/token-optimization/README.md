# Token optimization (2026-07-16 analysis)

Status: **open — measurement + content hygiene shipped; supervisor roadmap in acceptance review (P0 first)** Date: 2026-07-16 Owner: Vladimir Makarevich

This folder groups the backlog items that came out of the 2026-07-16 token investigation into a single campaign with one execution order. The code and the mandatory rules remain authoritative; these documents are the design detail, not an implementation contract, and must not override the hard invariants in [../../../CLAUDE.md](../../../CLAUDE.md) / [../../../AGENTS.md](../../../AGENTS.md) / [../../../.agents/rules/](../../../.agents/rules/).

> **Actualized 2026-07-23.** All five statuses were re-verified against the current code. Items #1–#2 are genuinely shipped (code + tests confirm every acceptance criterion); P0/P1/P2 are still unimplemented proposals (the supervisor code is unchanged since the analysis except for the WRI-011 finalize change). Two premises moved and are reflected in the item docs: **(a)** WRI-011 already made `finalize` read the task from a **frozen exchange packet by path** (context-footer), so P0's `SupervisorPacket` must be authored the same way (a frozen read-only artifact referenced by path in the two-root exchange layout), not as inline JSON; **(b)** the measurement substrate (#1) is merged, so P1's `max_digest_tokens` uses **real tokens from the start** — the char/count interim is dropped. Source line references in the item docs were refreshed (WRI churn shifted `orchestrator.py`/`supervisor.py`); the cited `tests/core/test_supervisor.py` references still hold. Still open and now unblocked: the **A/B** that all the headline numbers are gated on (~60–80k for #2; ~480k→30–60k for the supervisor) has not been run, and there is no read/report surface to observe usage yet — both are the cheapest next moves.

> **Actualized 2026-07-26 — acceptance review started.** The three supervisor items stay `proposal` not because the intent is unsettled but because each leaves concrete forks to implementation time; those forks are now enumerated below as decision IDs and are being closed one at a time, each answer written into its item doc as a `## Decision` entry. The review also found three text-level defects that must be fixed regardless of the answers: the items list `docs/worc_architecture.md` / `docs/configuration.md` as in-scope doc-sync although neither file exists on `dev` (X2); every headline number descends from a run on code that no longer exists — WRI-011 and content-flow hygiene both landed since — so the absolute acceptance thresholds are not currently checkable (X1); and P1's packaged defaults are phrased as per-flow-name defaults (`blog_article*`, `content_*`, `implementation`), which the engine may never implement as name matching (P1-D4).

## Initial context

The trigger was a single content run (`blog-review-happy-in-my-misfortunes-4`) whose token cost was dominated by the supervisor and inflated by broken accounting. Two analyses frame the work: the token analysis (F1–F5) and [../../analysis/2026-07-16-supervisor-token-optimization-options.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/analysis/2026-07-16-supervisor-token-optimization-options.md) (variants A–I, target architecture §6, phasing §8).

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

See [happy-path.md](happy-path.md) for a plain-language **before/after** walk-through of the analyzed `blog_article_revise` run — what the supervisor costs today (7 LLM calls, 480k input) versus after the whole roadmap ships (1 fresh finalize, < 60k), with diagrams and the quality guards that stay in place.

## Execution sequence

The order is not a suggestion — two of the dependencies are correctness constraints, not preferences.

| # | Item | Depends on | Why the order is fixed |
| --- | --- | --- | --- |
| 1 | Normalized usage accounting | — | Foundation. Without a summation-safe per-attempt usage record, every downstream token claim (the A/Bs below, P1 budgets, P2 telemetry) is unmeasurable or wrong. |
| 2 | Content-flow token hygiene | ships independently; A/B confirmation needs #1 | The default flip and docs ship without waiting; the "~60–80k saved, quality no worse" claim is only honest once #1 exists to measure it. |
| 3 | Supervisor P0 | measurement from #1 for its A/B | **`finalize` must become packet-first and fresh before any cadence change.** Cutting observations first strands `finalize` with an empty digest — it re-reads the repo or thins the summary and degrades `follow_ups` / `memory_delta`. |
| 4 | Supervisor P1 | **requires #3**; token budgets use #1 | `observation_mode: none`/`events` is safe only over the deterministic P0 packet; `max_digest_tokens` in real tokens uses #1 — which is **shipped**, so real-token budgets are available from the start (a char/count bound is only a fallback, no longer an interim step). |
| 5 | Supervisor P2 | **requires #4**; per-function telemetry uses #1 | Structural cleanup after the savings land — extracts the deterministic recorder P1 introduced and reports per-function usage on top of #1. Not on the savings critical path. |

Items 1–2 are done. The live critical path is **P0 → P1 → P2**; do not start a later phase before its predecessor is merged, and never change supervisor cadence before the P0 packet exists.

## From `proposal` to `accepted`

The repo convention (see [../deep-research-postmortem/](../deep-research-postmortem/README.md), where all ten items are accepted) is that an accepted item carries `Status: **accepted**` in its header plus a `## Decision (date)` section recording the chosen option and any explicitly dropped sub-item. Moving P0/P1/P2 out of `proposal` therefore means: close every fork the doc currently defers to implementation time, and make each acceptance criterion checkable as written.

Two rules for this review: **one decision at a time**, and **re-verify against the code before each question** — the 2026-07-23 pass already found two premises that had moved under the docs, so the assumption is that more have.

### Cross-cutting decisions (close before P0)

| ID | Open question | Why it blocks acceptance |
| --- | --- | --- |
| X1 | The A/B baseline and how supervisor usage is read: every headline number (480 293 / 104 567 / 44 107) comes from the 2026-07-16 run, but WRI-011 and content-flow hygiene have since changed both the finalize prompt and the packaged `blog_article_revise` defaults. There is also no operator-facing usage surface — `cli.py` has `status` / `top` / `logs` / `memory`, nothing for tokens or cost — so normalized usage is reachable only by querying `state.db`. | P0's "supervisor input < 60 000 (baseline 480 293)" and P1's "~480k → 30–60k" are not verifiable against a baseline that no longer exists, and nothing today produces the number to compare. |
| X2 | ~~Doc-sync scope on `dev`~~ — **decided 2026-07-26: name the exact files that exist here, plus a PR doc-impact note.** Each item now lists its real targets instead of the `main`-only derived docs: P0 → `packaged/guide/flows/roles.md` (its `:63` claim that the supervisor "observes **each step**" becomes false); P1 → `packaged/config.example.yaml`, `guide/config/reference.md:173-182` (the flat `supervisor.*` key table), `guide/flows/reference.md:22` (`SupervisorBlock`), `guide/flows/roles.md`, packaged flows; P2 → `guide/flows/roles.md`. `worc_architecture.md` / `configuration.md` are covered by a one-line doc-impact note in the PR description, never created on `dev`. | Closed — [AGENTS.md](../../../AGENTS.md) forbids creating the derived docs on `dev`, and naming the packaged files explicitly keeps the most-often-forgotten half of a doc change in scope. |

### P0 decisions — [supervisor-finalize-packet-and-cadence.md](supervisor-finalize-packet-and-cadence.md)

| ID | Open question |
| --- | --- |
| P0-D1 | Where the packet physically lives and by which mechanism: which root of the two-root exchange layout, the file name, whether it is frozen through the existing seal/bundle machinery (`core/flow/exchange_seal.py`, `core/flow/frozen_bundle.py`), and the name of the new context-footer path variable next to `task_path` / `repository_instructions_path`. |
| P0-D2 | What "the packet is identical on a normal run and after revive" means field by field — a revived task has extra attempts, timestamps and durations, so the criterion is untestable until the compared projection is defined. |
| P0-D3 | The concrete bounded limits: the diff-size threshold under which the full diff is inlined, the per-step message cap, the digest cap. |
| P0-D4 | Whether the warm-resume finalize branch (`warm = self._session_live`, `core/supervisor.py:646-647`) is deleted outright or kept behind a config switch. |
| P0-D5 | One PR or two: the 2026-07-23 note allows landing the `tool`/`checks` observe skip (`core/orchestrator.py:3176`) as its own small PR ahead of the packet. |
| P0-D6 | Whether the packet — assembled from findings / checks / diff, i.e. a new provider-facing surface — goes through the same redaction path as the other provider surfaces (`providers/redaction.py`). |
| P0-D7 | Cross-platform authoring of the packet file: `as_posix()` for every path inside it, `newline=""` on write. |

### P1 decisions — [supervisor-observation-cadence-p1.md](supervisor-observation-cadence-p1.md)

| ID | Open question |
| --- | --- |
| P1-D1 | Config shape and compatibility: nested `observe` / `finalize` / `handoff` with a hard cut, or nested plus the flat `model` / `reasoning` / `provider` kept working. The doc deliberately leaves both open. |
| P1-D2 | One name for the setting — the global block says `observe.mode`, the flow-local block says `observation_mode`. |
| P1-D3 | How the schema bump is stated: the doc says 31 → 32, the code is at 31 today, and another item may bump first. |
| P1-D4 | Packaged defaults (content → `none`, `implementation` → `events`) must be expressed in the packaged flow files, never as flow-name matching in the engine — a textual fix required by the no-hardcoding invariant, independent of the other answers. |
| P1-D5 | The strictness order behind flow-local narrowing: `all` / `selected` / `events` / `none` are not one axis, so the validator needs an explicit table saying what "narrow, never widen" means. |
| P1-D6 | Budget semantics: whether `max_calls` counts per task / per node / per attempt, whether the counter survives restart and revive, and how `max_digest_tokens` is enforced — normalized usage is post-hoc, so a pre-send bound needs an estimator. |
| P1-D7 | The trigger list against the data actually available in the post-node hook, which receives only `node`, `outcome`, `node_run_id` (`core/orchestrator.py:3167`): `rework` / `failure` are derivable, `hitl` / `fallback` / `dangerous_diff` / `subtask_boundary` are not yet. Either the hook gains those facts or the list shrinks. |
| P1-D8 | Whether the optional `session: fresh_digest` for observe stays in P1 at all once most observations are gone. |

### P2 decisions — [supervisor-responsibility-split-p2.md](supervisor-responsibility-split-p2.md)

| ID | Open question |
| --- | --- |
| P2-D1 | Scope trim: which of the five in-scope items survive, given P0+P1 have already banked the savings and P2 is structure plus observability. |
| P2-D2 | Whether `StepRecorder` takes over the existing ledger writes or records alongside them, where the module lives, and the import-contract update that keeps it free of provider dependencies. |
| P2-D3 | Storage shape for per-function usage: extra columns on `provider_attempts` or a separate table, and what happens to existing databases (greenfield — recreate, no migration). |
| P2-D4 | The dominance-warning threshold and where it surfaces, if that item survives P2-D1. |

### Acceptance sequence

| # | Step | Note |
| --- | --- | --- |
| 1 | X2 — fix the doc-sync scope in all three items | Text-only, no dependency; removes a conflict with the branch rule before any code decision |
| 2 | X1 — decide the measurement contract, then re-measure the baseline on current `dev` | Everything numeric in P0 and P1 hangs on this |
| 3 | P0-D1 … P0-D7 → set P0 to `accepted` with its `## Decision` section | The only item whose forks can all be closed today |
| 4 | Implement P0 (per P0-D5: the observe skip first, then packet + fresh finalize) | Banks the cheap saving immediately |
| 5 | P1-D4 as a text fix now; P1-D1 … P1-D8 → `accepted` after P0 is merged | D6 and D7 are answered honestly only once the P0 work has touched the same hook |
| 6 | Implement P1 |  |
| 7 | P2-D1 … P2-D4 → `accepted` after P1 is merged | P2 extracts the recorder that P1 introduces |

## Expected effect

For a comparable `blog_article_revise` pass: P0's `tool`/`checks` skip and fresh finalize alone remove ~44k + a large slice of the 104 567-token final call; P1's finalize-only cadence removes the historical 375 726 observation input tokens. Target: supervisor input falls from ~480k to ~30–60k with zero missed blocking issues — all A/B-gated on the normalized usage from item #1.
