# Memory curation campaign (2026-07-26)

Status: **open — all items proposed (operator sign-off pending)** Date: 2026-07-26 Owner: Vladimir Makarevich

Residue from the items that land — open decisions, watch items, operational consequences, deliberate non-goals, and what the `main` docs refresh must pick up — is collected in [follow_ups.md](follow_ups.md) (empty until the first item merges).

This folder groups everything that came out of two documents into one campaign with one execution order: the [memory audit on WastimeApp](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/analysis/2026-07-24-wastimeapp-memory-audit.md) (2026-07-24 — the first audit of a real, 21-task-old memory store, maturity **3/10**) and the [auto-dream curation analysis](2026-07-25-auto-dream-memory-curation-analysis.md) (2026-07-25 — the operator's idea of a periodic supervisor pass over memory, assessed against the code). The audit says what is broken; the analysis says what may and may not be automated on top of it. The files below are the implementable tasks.

`docs/analysis/` lives only on `main` (see `chore(dev): drop derived docs`), so the audit is linked by absolute URL. The analysis document itself is kept **inside this folder** so the campaign is self-contained on every branch.

These documents are design detail, not an implementation contract, and must not override the hard invariants in [../../../CLAUDE.md](../../../CLAUDE.md) / [../../../AGENTS.md](../../../AGENTS.md) / [../../../.agents/rules/](../../../.agents/rules/).

## Campaign-wide constraint: the model proposes, deterministic code decides

Every item below touches the memory store, and none of them may hand a model write authority over it. The existing safety argument for the curation layer is a four-line invariant in `CleanupJob` (`memory/cleanup.py:1-17`): **demote / expire / quarantine / merge only** (there is no promote code path), **snapshot first, then bounded**, **quarantine, never silent delete**, **model-free and deterministic**. The campaign extends that layer; it does not carve an exception into it.

Concretely, for every item:

- **A model output is a proposal, never a mutation.** The curator writes `proposals.jsonl`; deterministic code decides what is appliable; the operator approves anything that is not mechanically verifiable. This is the third application of a contract the repository already runs twice — the supervisor is advisory by construction (`core/supervisor.py:1-21`, "it never reworks, reopens, or routes") and `propose_skill_map` is read-only propose-only with the Core resolving every token against the real inventory (`supervisor.py:809`).
- **A curator claim can never self-certify trust.** `assign_trust` (`memory/lifecycle.py:45`) stays the only source of a trust level, and a synthesized claim is capped at `artifact-backed` — which is deliberately outside `_AUTO_PROMOTE` (`lifecycle.py:40`) — until a validator or the operator says otherwise.
- **Nothing runs while a task is active.** `worc memory compact` / `restore` already refuse on `has_active_task` (`cli.py:2424`, `2453`) and the idle-gap hook re-checks it (`cli.py:1673`). Curation runs in the idle gap or behind an explicit operator verb, never inside a task — a store that mutates mid-task hands successive nodes of the same task different memory, and there is no retrieval trace to reconstruct which.
- **A pass with nothing to do writes nothing.** Today 393 of 398 snapshots were produced by cleanup passes with zero mutations. Repeating that pattern with a paid model pass is the failure mode this campaign must not ship.
- **Evidence-gated by construction.** A proposal without resolvable evidence is dropped, never logged-and-kept — the `parse_follow_ups` rule (`supervisor.py:258`), applied to curation.

## What the audit and the analysis showed

The store is not rotting from volume. It is 71 records / 73 495 B after 21 tasks — small enough to fit in one model call. It is damaged at the moment of writing, by four defects that compound:

1. **Identity collapses distinct claims.** `derive_long_term_id` keys a lesson on `kind` + `scope.paths` and ignores `subject` when paths are present (`memory/service.py:647`), and `_merge_long_term` replaces only `statement` + evidence (`service.py:698`). Different thoughts about the same file are forced into one record whose `subject` / `rationale` / `remedy` belong to an older claim. Four such collision groups are confirmed in the live data.
2. **Trust is self-certified by a string.** Evidence typed `file` / `doc` / `code` yields `repo-observed` with no existence check for lessons, and `repo-observed` auto-promotes on first sight. A fabricated `missing-proof.md` reference was reproduced reaching a packet at durable trust.
3. **Quarantine leaks back as ordinary memory.** One file mixes "awaiting recurrence" with "stale / unresolvable", and `_durable_quarantine` (`memory/packet.py:177`) surfaces any durable-trust lesson kind from it without a status. Seven known-stale lessons are retrieval-eligible right now.
4. **The audit trail cannot explain a removal.** Snapshots are taken before it is known whether a mutation will happen, cleanup events sometimes name the kept rows instead of the removed one, reads are not audited at all, and `AuditAction.PROMOTE` is unused.

On top of that, three real terminal failures are absent from failure memory (the failure seam writes an episode and discards the delta), and retrieval does not know the task's target — two byte-identical packets were served to different chapters.

The operator's `auto-dream` idea aims at a real, self-declared gap ("lessons can still accrete contradiction-rot **until a human curates them**"), and the class of work it names — semantic dedup, contradiction against current reality, evidence entailment, claim atomicity — is genuinely model-shaped and unreachable by deterministic code. But applied to the store as it is today, a **writing** curation pass amplifies defect 1 and industrializes defect 2. Hence the order below: fix the write path, make the audit trail explain itself, extract everything deterministic code can do for free, and only then let a model into the loop — as a proposer.

## Items, in priority order

| # | Item | What it does | Effort | Scope |
| --- | --- | --- | --- | --- |
| P0.1 | [Claim identity and field-consistent merge](p0-1-claim-identity-and-merge.md) | Key a claim on `kind` + claim fingerprint + scope; a different claim becomes a new record or a conflict, not a merge | medium + migration | memory write path |
| P0.2 | [Evidence validation and the trust ceiling](p0-2-evidence-validation-and-trust.md) | Resolve every evidence ref before it grounds trust; synthesized claims cap at `artifact-backed`; wire contradiction | medium | memory write path |
| P0.3 | [Typed quarantine state and retrieval eligibility](p0-3-quarantine-state.md) | `quarantine_reason` / `retrieval_eligible`; stale / conflicted / unsafe records never reach a packet | small | memory read + cleanup |
| P1.4 | [An audit trail that explains a mutation](p1-4-memory-audit-trail.md) | Plan-then-snapshot, per-ID reasons, tier/file on every event, retention for snapshots | medium | memory audit / cleanup |
| P1.5 | [Deterministic store health report](p1-5-deterministic-health-report.md) | `worc memory validate` becomes a full health report with a non-zero exit on a P0 invariant | small | CLI (no model) |
| P2.6 | [The curator: read-only, propose-only](p2-6-curator-propose-only.md) | `worc memory audit` — a model pass that writes `proposals.jsonl` and never touches the store | medium | new module + CLI + config |
| P2.7 | [Review and apply the approved proposals](p2-7-review-and-apply.md) | `worc memory review` + the rejection ledger, applying through the audited seam | medium | CLI + memory service |
| P2.8 | [Post-task verification of what a task wrote](p2-8-post-task-verification.md) | Verify the 1–5 records the finished task just wrote, in the idle gap, while its artifacts still exist | medium | orchestrator + curator |
| P3.9 | [Auto-apply whitelist, gated on measured precision](p3-9-auto-apply-whitelist.md) | Promote specific proposal classes to automatic application once precision is measured | small | curator + config |

## Execution sequence

The first four items are ordered by dependency, not by taste. The last five are the campaign's actual subject and each one gates the next.

| Order | Item | Depends on | Why |
| --- | --- | --- | --- |
| 1 | P0.3 | — | The only item that removes proven-stale claims from live prompts today, and it is small. Fail-closed: excluding a record from a packet cannot break anything. |
| 2 | P0.1 | — | Until identity stops collapsing distinct claims, **every** later write — including a curator's correction — deepens the damage rather than fixing it. |
| 3 | P0.2 | P0.1 | The trust ceiling only means something once a corrected claim can exist as its own record. Also the item that makes a curator safe to run at all. |
| 4 | P1.4 | P0.1 | Before anything rewrites a record, a removal must be explainable and replayable. Also kills the 393/398 no-op snapshot churn. |
| 5 | P1.5 | P0.3 | Deterministic, model-free, and it is 60–70% of the "auto-dream" value for free. It also becomes the curator's input, so it must exist first. |
| 6 | P2.6 | P0.1–P1.5 | The model enters here, with no write authority, on a store whose invariants hold and whose deterministic findings are already extracted. |
| 7 | P2.7 | P2.6 | Closes the loop: an approved proposal reaches the store through the audited seam; a rejected one never returns. |
| 8 | P2.8 | P2.6, P2.7 | The highest-value item of the curation half, but it needs the proposal contract and the apply path to exist. |
| 9 | P3.9 | P2.7, P2.8 | Requires a **measured** `proposal_precision` from at least two or three real passes. Without the number this item has no criterion. |

A useful checkpoint after step 5: run `worc memory validate` against the WastimeApp store and diff it against the audit's baseline — 7 stale retrievable lessons, ≥3 mixed records of 19, 8 of 19 unresolved relationships, 0 of 3 represented terminal failures. Steps 1–5 alone should move the first three to zero with no model involved. That is also the cheapest proof that the campaign is worth continuing.

## Not in this campaign

- **V2 / V3 / V4 memory** (SQLite + FTS, embeddings, entity graph). Gated on a measured recall lift (AC-O4) in the memory backlog row; curation does not lift that gate and must not become a back door to it.
- **Retrieval relevance** (the audit's P1: planning nodes rank without the task's target, `task_type` / `touched_symbols` unused, byte-identical packets for different chapters). A real defect and a large one, but orthogonal: it is about what memory is _read_, this campaign is about whether memory is _true_. It needs its own item — see Open below.
- **Terminal failures as typed events** (the audit's P1: 0 of 3 real failures represented). Same reason: a write-seam feature, not a curation one. Also needs its own item.
- **Supervisor cadence and cost.** Already designed in `../token-optimization/`. P2.6 borrows its measurements for the cost estimate and otherwise defers to it.

## Open

1. **Two audit P1 items are not yet written up** (retrieval target-awareness; terminal failure as a typed event). They are named in the audit with acceptance criteria and metrics; they belong either in this folder as P1.10 / P1.11 or in a sibling campaign. Decide before starting step 6 — P2.8 partially overlaps the failure item, and doing them blind risks two mechanisms for one job.
2. **`proposal_precision` is unmeasured**, so P3.9 has no threshold yet and P2.6's value remains a hypothesis. The verifiable half of the analysis is only the harm of the writing variant; the benefit of the proposing variant is measured at step 6, not argued.
3. **The post-fix validation corpus** the audit specifies (36+ replayed tasks with gold relevant IDs and forbidden stale claims) does not exist. Steps 1–5 can be accepted on unit tests plus the WastimeApp before/after diff; steps 6–9 want the corpus, because "did curation help?" is not answerable from counts alone.
