# `deep_research` run 2 — bound the rework cost and make named fixes land

Priority: **P0–P2** Status: **open** Date: 2026-08-05 Source: post-mortem of `audit-v2-implementation` (target `wastech-mdlint`, 2026-08-04/05, `done`, PR [#17](https://github.com/VladimirMakarevich/wastech-mdlint/pull/17))

## Why this exists

`audit-v2-implementation` is the **second** production run of the `deep_research` flow and the first one after the eleven-item `deep_research` post-mortem campaign landed. That campaign's fixes work — see [What run 2 validated](#what-run-2-validated) — and they moved the failure mode. Run 1 failed by reading 18% of the in-scope files with nothing measuring it. Run 2 read **100% of both code remits** and produced a materially better audit, then spent **~32% of its budget ($63 of $197.08) on rework rounds that were structurally unable to be cheap or to land their fixes**.

The theme of every P0 item below is the same: the flow bounds work through **prompt-level instructions** ("close the named gaps, do not re-derive", "apply the fix") while the **mechanism** makes obeying them impossible or unverifiable. Fixing the wording is not the lever; fixing what the node receives and what it must report is.

Nothing here proposes a new node, a new provider, or a model change. Reasoning tiers came out well-fitted (see [Not in scope](#not-in-scope)).

## Run frame

|  |  |
| --- | --- |
| Task / flow | `audit-v2-implementation` / `deep_research`, task type `deep_research` |
| Outcome | `done`, PR #17 (+4098, exactly the two deliverable files), `fix_iterations=4` of 12, `test_fix_cycles=0`, `review_fix_cycles=0` |
| Spec going in | `validation_report.json`: `passed`, `completeness=complete` — the task was well-formed, so nothing below is a task-authoring finding |
| Cost / duration | **$197.08**, ~5h15m of node time, 31 `node_runs` rows |
| Infra | `stage_attempts=1` on every node, `route_fallback` never fired, `error_class` empty except the operator's mid-run kill, `check_runs` 8/8 passed with no timeouts |
| Per-task change | `nodes.external_research.enabled: false` — correctly recorded as `skipped / "disabled by task: nodes.external_research.enabled=false"` |
| Interruption | The operator killed the run mid-`synthesis` and resumed with `rerun --continue`; recorded as `synthesis / aborted / error_class=cancelled / "terminal transition to done"` |

Cost by node, all runs summed:

| Node | $ | Runs | Note |
| --- | --- | --- | --- |
| `synthesis` | **67.32** | 4 | 34% of the run |
| `fact_verification` | **30.54** | 4 | **$25.10** of it in three rounds that could only accept |
| `analysis_docs_tests` | 26.97 | 2 | the rework that paid |
| `analysis_surfaces` | 23.80 | 2 | re-swept for one `low` finding |
| `analysis_core` | 17.51 | 2 | re-swept for **zero** findings |
| `architecture_design` | 14.87 | 1 | the empirical layer, worth its price |
| `critical_review` | 9.31 | 3 | best value in the run |
| `coverage_gate` | 4.38 | 2 | best value per dollar |
| `refinement` | 1.49 | 1 |  |
| `supervisor` | 0.90 | 8 |  |

## Priorities

| Item | Category | Severity | Avoidable spend on this run | Lever |
| --- | --- | --- | --- | --- |
| [P0.1](#p01--give-a-re-entering-node-its-own-prior-output) — a re-entering node never receives its own prior output | mechanism | high | ~$21 | `core/flow/context_paths.py` + the three analysis prompts |
| [P0.2](#p02--make-synthesiss-rework-contract-explicit-and-class-wide) — `synthesis` applies a named fix to one site and not its siblings | prompt | high | ~$17 | `deep_research/synthesis.md` |
| [P0.3](#p03--skip-an-evaluator-whose-rework-budget-is-spent) — spent evaluators re-run with no power to act | flow/engine | high | **$25.10** | `core/flow/engine.py` |
| [P1.4](#p14--make-the-citation-checker-verify-manifest-completeness) — `citation_check` grades only what the agent chose to declare | checks | high | $5.45 of verifier time | `core/flow/checkers/citation.py` |
| [P1.5](#p15--make-coverage_gate-measure-standard-tiers-not-only-files) — `coverage_gate` measures files, not standard tiers | prompt | high | ~$20 | `deep_research/coverage.md` |
| [P1.6](#p16--correct-the-architecture_design-node-comment-and-drop-the-downgrade-advice) — the `architecture_design` comment mis-describes the node and advises a harmful downgrade | flow/docs | med-high | — (prevents a regression) | `packaged/flows/deep_research.yaml` |
| [P1.7](#p17--align-fact_verifications-rework-budget-with-the-flows-own-convention) — `fact_verification` budget breaks the flow's stated edge/node convention | config | medium | compounds P0.3 | `packaged/flows/deep_research.yaml` |
| [P2.8](#p28--require-an-empirical-probe-to-state-and-prove-its-precondition) — empirical probes are not self-validating | prompt | medium | one false finding | `deep_research/architecture_design.md` |
| [P2.9](#p29--give-worc-run-a-node-boundary-stop) — `worc run` has no node-boundary stop | CLI/UX | medium | 28 min of node time | `cli.py` |
| [P2.10](#p210--stabilise-finding-ids-across-rework-rounds) — finding ids are unstable across rework rounds | prompt | low-med | triage friction | `deep_research/synthesis.md` |
| [P2.11](#p211--log-checks-nodes-and-skips-at-operator-visible-level) — `checks` nodes and skips are invisible in the run log | infra | low | operator confusion | logging in the checks/skip paths |

---

## P0.1 — give a re-entering node its own prior output

### Problem

The previous campaign stated the design contract for a `coverage_gate` rework:

> The cost of a second full sweep is bounded by the findings rather than by the corpus: each analysis prompt carries a `{?review_path}` re-entry section that hands it the gate's findings and tells it to close the named gaps in its own remit first and not re-derive what the previous round covered, **so a pass with nothing named for it is a cheap turn**.

Run 2 falsifies the last clause. `coverage_gate` round 1 filed four findings, all owned by `analysis_docs_tests` (tests, guide, plan) and one `low` by `analysis_surfaces` (`tool-docs.ts`). **None was addressed to `analysis_core`.** `analysis_core` nevertheless re-read all 72 core files and emitted a fresh 40.4 KB report:

| Pass | Round 1 | Round 2 | Findings owned |
| --- | --- | --- | --- |
| `analysis_core` | $8.45 / 941s / 72 files | **$9.06 / 828s / 72 files** | **0** |
| `analysis_surfaces` | $12.00 / 1272s / 18 files | **$11.80 / 1191s / 18 files** | 1 `low` |
| `analysis_docs_tests` | $13.38 / 1080s | $13.59 / 1060s | 3 `medium` |

Round 2 cost 98% of round 1 for the two passes that had nothing (or almost nothing) to close.

### Root cause — mechanism, not wording

The instruction is present verbatim in all three packaged prompts (`analysis_core.md`, `analysis_surfaces.md`, `analysis_docs_tests.md`):

> A coverage gate reviewed an earlier analysis round; its findings are at {review_path}. Close every gap it names that falls inside this remit — those files and properties first, ahead of anything else — and do not re-derive what the earlier round already covered.

It is unfollowable as built. From `prompt-audit/`:

- `000002-analysis_core.json` — round 1 prompt, **5374** chars.
- `000006-analysis_core.json` — round 2 prompt, **5913** chars. The whole delta (+539) is the `Gaps to close` section pointing at `{review_path}`.

The node is **not given its own round-1 report**. Combined with the same prompt's output contract —

> **Your report is your final message** — it is persisted as this node's output and is all that later nodes and the coverage gate receive

— a "cheap turn" is impossible by construction: the node's new output _replaces_ the old one for everything downstream, it has no copy of the old one to carry forward, and a thin report would therefore destroy the round-1 analysis. Re-reading the corpus is the only way to emit a complete report honestly. The node behaved correctly given what it was handed.

### Fix steps

1. Add a self-prior output channel — e.g. `{?self_prior_path}` — resolved in [`core/flow/context_paths.py`](../../src/wastech_orchestrator/core/flow/context_paths.py) (the module the previous campaign created when it moved `build_node_output_paths` out of `agent.py`), pointing at the node's own most recent output for this task.
2. Render it for `agent` nodes on re-entry only (absent on the first pass, so round 1 is unchanged), and extend the prompt-variable allowlist/lint the same way P1.4 did for evaluators.
3. Rewrite the `{?review_path}` section in the three analysis prompts to state the achievable contract: carry the prior report forward **verbatim**, re-derive only what the named gaps require, and mark every carried-forward section as unchanged from the previous round.
4. Require the `## Coverage` section to distinguish "opened this round" from "carried forward from round N", so `coverage_gate` can still tell read from asserted.

### Scope and expected impact

Orchestrator default (`packaged/`), with the target copy refreshed via the [`upgrade-flows`](upgrade-flows.md) / `install --reconfigure` path already tracked in `target-resync-after-deep-research.md`. On run 2's numbers this returns **~$21** and ~34 minutes, and it makes the P1.4 contract true rather than aspirational.

Deliberately **not** proposed: routing the rework edge to the owning pass only. `coverage_gate` findings can span remits, the head-of-chain re-entry is a considered decision recorded in P1.4, and once step 1 lands a pass with nothing named for it _is_ cheap. Re-routing would trade a correct guarantee for a saving this item already delivers.

---

## P0.2 — make `synthesis`'s rework contract explicit and class-wide

### Problem

`synthesis` is the most expensive node in the run ($67.32 across four rounds) and the one that consumes every evaluator's findings. It applies a named fix at the cited line and does not propagate it to co-referring prose.

Evidence, `evaluations` table, `critical_review` verdicts:

- Round 2 (`rework`, severity `high`): "The report's only process-level recommendation **still** rests on a citation read backwards — flagged as medium in the previous fact-verification round **with a named fix, and unchanged here**."
- Round 2: "F6's sub-count is **still wrong by one, unchanged** after the previous round flagged it and named the correct five files."
- Round 2: "`packages/core/src/skills` … **still** has no traced property, in a coverage cell **unchanged** from the version flagged last round."
- Round 3 (`accept`, the residual `low`): "F6's corrected sub-count **was applied in one sentence and not in its sibling four lines below**, so the finding now contradicts itself."

So a full 1396s / **$16.88** synthesis round left three named fixes untouched, and the round that did fix one introduced a self-contradiction by fixing a single site.

### Root cause

The flow demands class-wide discipline of the **audit** — the task's own acceptance criterion is "Each finding is reported as a class, not a single instance: the report names every site of the same defect shape it found, and states that the corpus was grepped for the class" — and demands nothing equivalent of the **fixer**. `synthesis` has no obligation to report what it did with each incoming finding, so a dropped fix is invisible until the next evaluator round pays to rediscover it.

### Fix steps

1. In [`packaged/flows/deep_research/synthesis.md`](../../src/wastech_orchestrator/packaged/flows/deep_research/synthesis.md), add a rework contract: for every finding in `{?review_path}`, emit one line — `applied` (with the sites changed) or `rejected` (with the reason) — and forbid silence.
2. Require the same class sweep the audit owes: after correcting a fact, grep the deliverable for every co-reference of it and list the sites updated. A corrected number that survives elsewhere in the document is a new defect, not a partial fix.
3. Put the applied/rejected ledger in the node's output so `fact_verification` and `critical_review` can check it against their own prior findings instead of re-deriving them.
4. Optional, decide separately: have the engine warn when a rework round reports zero `applied` — the same shape as the existing "accepted after exhausting its rework budget" warning, and cheap once step 3 gives it a machine-readable ledger.

### Scope and expected impact

Orchestrator default. This is the highest-leverage item in the document: on run 2 it likely removes one full `critical_review`+`synthesis` cycle (~$20) and, more importantly, it stops the run from shipping defects that were found and named. Note the ordering constraint — raising any rework budget before this lands buys more rounds that drop fixes, so **P0.2 precedes P1.7**.

---

## P0.3 — skip an evaluator whose rework budget is spent

### Problem

`fact_verification` ran **four** times and cost **$30.54**. Its `max_rework_per_stage` is 1, spent on the first round. Rounds 2, 3 and 4 were structurally incapable of any outcome but `accept`:

```
level=warning stage=fact_verification max_rework_per_stage=1 findings=9 msg="evaluator accepted after exhausting its rework budget — continuing; stage may need follow-up"
level=warning stage=fact_verification max_rework_per_stage=1 findings=8 msg="…"
level=warning stage=fact_verification max_rework_per_stage=1 findings=7 msg="…"
```

Round 3 ($6.49) found 3 `medium` and accepted them. Round 4 ($8.48, 108 turns) found 3 `medium` and accepted them. **$25.10 for passes with no power to act**, and five `medium` findings force-accepted into the deliverable.

### Root cause

`critical_review` rework re-enters at `synthesis`, and the linear graph then walks the whole downstream chain — `synthesis → citation_check → document_checks → fact_verification → critical_review` — with no check on whether a gate it passes through can still gate. `critical_review` has three rework rounds and `fact_verification` one, so every critic round pays this toll again.

### Fix steps

1. In [`core/flow/engine.py`](../../src/wastech_orchestrator/core/flow/engine.py) (around the existing exhaustion handling at `engine.py:131`), skip an evaluator whose per-stage rework budget is already spent **and** whose verdict therefore cannot change the path.
2. Record it with the mechanism that already exists for a disabled node — `node_runs.skipped = 1`, `skip_reason = "rework budget spent"` — so the ledger shows the skip instead of a silent absence. `external_research` proves the field is honest and readable.
3. Keep the existing warning for the round that actually exhausts the budget; it is the operator's signal that findings remain open.
4. Test both orders: an evaluator whose budget is spent by its own rework, and one spent before a _later_ gate's rework re-enters upstream of it.

### Scope and expected impact

Orchestrator default; affects every flow with two evaluators of unequal budget on one chain. Returns **$25.10** on a run of this shape, and more the deeper the critic loops.

---

## P1.4 — make the citation checker verify manifest completeness

### Problem

`citation_check` reported `passed: true`, `220/220 verified` on a manifest that was missing a substantial part of the report's citations. `fact_verification` round 1 caught it:

> A substantial set of the report's `path:line` citations are absent from the citation manifest, so the deterministic check never saw them and they carry no snippet to check against.

The rework closed it — the manifest went 220 → 261 → **424** entries, and the final state has **0** inline `path:line` citations absent from the manifest, all 424 `verified`. But the gap was found by an **$5.45 LLM verifier**, not by the free deterministic checker whose whole job it is.

### Root cause

The previous campaign's citation-checker hardening (implemented 2026-07-26) made the _cited line authoritative for entries that are in the manifest_. It did not give the checker any view of the deliverable's own citations, so **checked coverage is self-declared**: the agent decides what enters `sources.json`, and the checker grades exactly that set. A report can cite fifty lines, declare five, and pass.

### Fix steps

1. In [`core/flow/checkers/citation.py`](../../src/wastech_orchestrator/core/flow/checkers/citation.py), extract `path:line` occurrences from the node's `output_file` (the deliverable, which the checker already knows via the manifest's sibling path).
2. Report each extracted citation with no manifest entry as a new status — `undeclared` — carrying the report line it was found on.
3. Follow P1.6's precedent on gating: P1.6 deliberately took `weak` rather than a hard fail because the `citation_check → synthesis` fail edge has `budget: 1` and one imprecise line would park the run. Apply the same reasoning here — start `undeclared` as non-gating and measured, route it to the verifier through the `{checks_path}` channel P1.6 built, and decide on gating from the numbers.
4. Guard the extraction against false positives: prose that names a path without citing a line, code fences, and the report's own filename.

### Scope and expected impact

Orchestrator default. Moves a real class of defect from an $5+ evaluator round to a free deterministic pass, and closes the "self-declared coverage" hole P1.6 left open.

---

## P1.5 — make `coverage_gate` measure standard tiers, not only files

### Problem

`critical_review` round 1 filed the run's only evaluator `high`:

> A whole precedence tier — `docs/mdlint_v2/decisions/` — was never traversed as a standard, and the omission is not disclosed.

The task names "code contradicts a documented invariant, requirement, **decision record**, or exit criterion" as its first finding class. The `decisions/` files **were opened** (pass 3 read all four), so `coverage_gate`'s file-based metric was green across two rounds while nobody judged the code against that tier.

### Root cause

`coverage.md` asks two questions — "was it really read" and "does it show a traced property" — both keyed to _file sets_. A tier of the standard can be fully read and never used as a standard, and that is invisible to a file-count metric. The gate cost $4.38 across two rounds; the miss instead cost a $3.30 critic round plus a $16.88 synthesis round.

### Fix steps

1. In [`packaged/flows/deep_research/coverage.md`](../../src/wastech_orchestrator/packaged/flows/deep_research/coverage.md), add a third check: every standard the task names (invariants, requirements, decisions, exit criteria, repository rules) must appear as a **judgment axis** — findings filed against it, or an explicit nil return stating it was used and produced none.
2. Require the analysis prompts' `## Coverage` section to list the standards applied alongside the files opened, so the gate has something to measure rather than having to infer it.
3. Keep the existing scope rule from P1.4 — "scope is what the task declares plus what the reports themselves claim" — so a narrowly scoped task is still not punished.
4. Follow the established severity convention: state the mechanism, do not restate `medium`, so the prompt cannot go stale against the YAML.

### Scope and expected impact

Orchestrator default. Catches a class of miss at the $2 gate that currently reaches the $3–17 gates downstream, and closes the gap between "we read the standard" and "we judged against it".

---

## P1.6 — correct the `architecture_design` node comment and drop the downgrade advice

### Problem

[`packaged/flows/deep_research.yaml`](../../src/wastech_orchestrator/packaged/flows/deep_research.yaml) describes the node as an organizing pass and advises lowering its reasoning:

> An organizing pass over evidence three passes already gathered: measured at xhigh, every one of its repository reads was a re-read and it produced no new finding. `high` is the fit.

Run 2 measures the opposite. Tool mix: **12 `Read`, 0 `Grep`, 100 `Bash`, 0 writes**, 136k output tokens — the highest of any node. It built the project, ran `npm test` / `typecheck` / `tsc -b --force` / `vitest`, created ~12 throwaway probe repositories under `$TMPDIR`, ran the built CLI against synthetic fixtures, matrix-tested a config default to confirm an inversion finding, ran `npm pack --dry-run` per workspace, used `chmod 000` to exercise an unhandled error path, and settled the exact claim `analysis_core` could not — `node -e "require('micromatch')"` on negated globs, which pass 1 had to caveat as "rests on reading micromatch source rather than executing it".

This is the run's **only empirical verification layer**. Acting on the comment's advice would remove it.

### Root cause

The comment's premise is arithmetically true and materially misleading: it counts `Read` calls (12, all re-reads) and is blind to the 100 shell executions that are the node's actual work. The observation was probably accurate for run 1, where `read-only` denials and a different task shape gave the node nothing to run.

Related and worth recording in the same edit: the neighbouring comment on the node's `workspace-write` profile says "`read-only` executes nothing on Claude". Run 2 contradicts that too — read-only nodes ran 26 of 34 Bash calls successfully (`find`, `wc`, `grep`, `sed`, `git log`, `git show`, and in one case a `python3` heredoc), while `node -e` and `find -exec` were denied. The accurate statement is that `read-only` cannot reliably run an arbitrary interpreter, not that it executes nothing.

### Fix steps

1. Rewrite the node comment to describe it as the empirical-verification pass, with run 2's tool mix as the evidence.
2. **Remove** the commented-out `# reasoning: high` suggestion for this node; keep the `xhigh` pin.
3. Correct the `read-only` claim in the adjacent comment to "cannot reliably execute an arbitrary interpreter", and note that the boundary is not stable enough for a role prompt to be written against.
4. In the three analysis prompts, replace any expectation of settling a claim by execution with an explicit handoff: name the probe and the precondition, and leave it to `architecture_design` (which pairs with [P2.8](#p28--require-an-empirical-probe-to-state-and-prove-its-precondition)).

### Scope and expected impact

Orchestrator default, docs-only except step 4. Prevents a regression that would silently remove the layer that converts read-based claims into executed evidence.

---

## P1.7 — align `fact_verification`'s rework budget with the flow's own convention

### Problem

The previous campaign stated the convention explicitly for `coverage_gate`: "`budget: 2` on the edge matches `max_rework_per_stage: 2`, so the non-blocking self-cap always fires before the edge budget does." `fact_verification` breaks it — the edge is `{ from: fact_verification, to: synthesis, outcome: rework, budget: 2 }` while the node sets `max_rework_per_stage: 1`. The node cap binds; the edge budget is dead. An operator reading `edges` expects two repair rounds and gets one.

`critical_review` (edge 3 / node 3) and `coverage_gate` (edge 2 / node 2) both agree, so `fact_verification` is the lone outlier.

Note also that `max_rework_per_stage: 1` equals the schema default ([`core/flow/schema.py:121,274`](../../src/wastech_orchestrator/core/flow/schema.py)), so the line restates a default while contradicting its own edge.

### Fix steps

1. Decide the intent: either raise `max_rework_per_stage` to 2 to match the edge, or lower the edge budget to 1 to match the node. Given `fact_verification` left **five `medium` findings** force-accepted on run 2, 2 is the better default.
2. Apply it in `packaged/flows/deep_research.yaml` and state the reason in the node comment, the way the other nodes' budgets are justified.
3. Consider a flow-validator warning when an edge `budget` and the target node's `max_rework_per_stage` disagree — the operator-facing failure here was a documentation trap, not an engine bug. Lever: [`core/flow/validator.py`](../../src/wastech_orchestrator/core/flow/validator.py).

### Ordering

**Do this after [P0.2](#p02--make-synthesiss-rework-contract-explicit-and-class-wide) and [P0.3](#p03--skip-an-evaluator-whose-rework-budget-is-spent).** Raising the budget before `synthesis` reliably lands fixes buys extra rounds that drop them; raising it before P0.3 multiplies the powerless re-runs.

---

## P2.8 — require an empirical probe to state and prove its precondition

### Problem

`fact_verification` round 1, severity `medium`:

> F14's conclusion does not follow from what was run. The reproduction is `npm run build` on an already-up-to-date tree, which was a no-op; the conclusion is about a different state (source mtime newer than dist without a content change).

An executed probe produced a confidently wrong finding, which is worse than an un-executed claim because the report presents it as verified.

### Fix steps

1. In [`packaged/flows/deep_research/architecture_design.md`](../../src/wastech_orchestrator/packaged/flows/deep_research/architecture_design.md), require every probe to state the precondition it needs, show the command that established it, and only then run the probe.
2. Require the negative control where one exists (a probe that must fail before the fix state is created), which is what would have caught F14.
3. Require the finding text to carry the probe transcript's decisive line, so a verifier can judge the inference without re-running it.

### Scope

Orchestrator default. Pairs with [P1.6](#p16--correct-the-architecture_design-node-comment-and-drop-the-downgrade-advice) step 4.

---

## P2.9 — give `worc run` a node-boundary stop

### Problem

`StopController` and the `is_cancelled` seam are installed only in `cmd_watch` ([`cli.py:3069,3084`](../../src/wastech_orchestrator/cli.py)); `cmd_run` ([`cli.py:1780`](../../src/wastech_orchestrator/cli.py)) has neither. For a multi-hour flow the operator's only stop is `kill -9`, which forfeits the in-flight node. On run 2 that cost **28 minutes of `xhigh` `synthesis`**: the checkpoint stayed at `synthesis`, nothing had reached disk, and the resumed node started over.

The docstring at [`process_control.py:10`](../../src/wastech_orchestrator/process_control.py) already describes the right behaviour — "The handler **sets an event rather than raising**, so a `SIGTERM` that arrives mid-node lets that [node finish]" — it is simply not wired into `run`.

### Fix steps

1. Install `StopController` in `cmd_run` and pass the cancellation seam to the engine, exactly as `cmd_watch` does.
2. On a stop, let the current node finish, persist its checkpoint, and exit leaving the task `running`-with-no-owner — the state that already reads as `parked (no daemon)` and resumes with `rerun --continue` ([`cli.py:1243-1248`](../../src/wastech_orchestrator/cli.py)).
3. Until then, document in the operator guide that long flows should be driven by `watch` + `stop`, not `run`.

### What already works, and must not regress

Resume was exemplary and is worth an explicit test: `rerun --continue` reported `bundle_digest=bdce0fc65201` and `instruction_digest=f1b76715838e`, **identical to the original run** — the frozen control plane was adopted, not rebuilt, so the interruption did not drift prompts or config.

---

## P2.10 — stabilise finding ids across rework rounds

`critical_review` round 2: the report's findings run F1–F34, F36, F37, F38 — **F35 is missing with no explanation**, and a triager cross-referencing the upstream pass reports cannot tell whether it was dropped or renumbered. The upstream cause is visible in the pass reports: `analysis_core` labelled its findings `F1…F10` in round 1 and `C-1…` in round 2, and `synthesis` re-sequences on every merge.

Fix in `synthesis.md`: carry a stable provenance id (pass + original id) alongside any display number, and never reuse or silently retire one. Cheap, and it makes the deliverable auditable against the pass reports the seals no longer keep.

---

## P2.11 — log `checks` nodes and skips at operator-visible level

`state.db` records everything correctly: `citation_check` and `document_checks` appear in `node_runs` with `passed / pass`, `check_runs` holds all eight command rows, and the disabled node carries its `skip_reason`. The **run log** shows none of it — no `route resolved`, no attempt line — so an operator watching the console sees the flow jump `synthesis → fact_verification`. Both gates that could have parked the task (`document_checks` is fail-closed) passed invisibly, and the skipped node left no trace.

Fix: emit an info line for a `checks` node's start/outcome and for a skipped node's reason, at the level the agent nodes already use.

---

## What run 2 validated

Checked deliberately, so the campaign's cost is on record as having bought something:

- **The three-pass remit split works.** Run 1 opened 18% of in-scope files. Run 2 opened **72/72** core files with per-subdirectory counts matching the task's declared denominators exactly, and **18/18** entry-point/adapter files. `analysis_docs_tests` declared its sampling instead of hiding it — 47/132 plan files opened in full, the remaining 85 covered by three whole-corpus extractions over **132/132**, with anomaly-triggered full reads that produced five of its findings — and it **corrected the task's own denominators** (the guide is 51 files, not 53; 25 per-rule pages, not 19).
- **`gate_severity: medium` works.** `coverage_gate` round 1: three `medium` → `rework`. Round 2: three `low` → `accept`. The exact mechanism run 1 lacked.
- **The coverage gate earns its keep at $2.19/round.** It caught a cross-pass attribution error before `synthesis` could build on it: pass 3 justified skipping the 25 per-rule guide pages by claiming pass 1 had validated them against each rule's Zod schema, when pass 1's remit excludes the guide.
- **Evaluator findings reach the record.** `evaluations.findings_json` carries every verdict with severity and reason, and the supervisor's `advisory` rows sit alongside them.
- **`resume_own_lineage` on `critical_review` pays for itself.** Round 1 (fresh): 37 turns / $3.30. Round 2 (resumed, one `node_lineage` row): **12 turns / $2.04** — cheaper _and_ strictly more capable, because it can assert "unchanged after the previous round flagged it".
- **The read-only git-evidence grant is used as intended.** All three passes ran `git log` / `git show --stat`; pass 1 established that P10–P12 landed as a single squashed commit, which is a delivery-history finding no changelog would give.
- **Path hygiene holds** — stored logs redact the absolute repository path as `[REDACTED]`.
- **`disable_read_isolation: true` bought a gate pass.** `synthesis` produced Markdown that passed `prettier --check` under `proseWrap: never` on the first try, almost certainly because it could read the target's own `AGENTS.md` and `.prettierrc.json`. Worth remembering when weighing that relaxation.
- **`refinement` did not stop for a human.** The task was `completeness=complete`; the node resolved two genuine ambiguities from repository evidence and wrote "apply these, do not re-ask" into the brief. No HITL round in a flow that allows one.

## Trust verdict on the deliverable

Recorded here because it is the measure of whether the flow is fit for purpose, not just cheap.

**The audit's foundation is sound; its individual claims are leads, not settled facts.** Coverage against the declared scope is closed and measurable, and citations resolve completely — **424 manifest entries, 0 inline citations undeclared, every one verified as "snippet present at the cited line"** (text on the line, not merely a file that exists).

Three caveats belong with any use of the report:

1. **Five `medium` findings from `fact_verification` shipped unaddressed** — not undetected, but force-accepted when the node's budget died on round 1. Among them the verifier states one of the report's conclusions is outright false, and another rests on a plan clause read backwards. [P0.3](#p03--skip-an-evaluator-whose-rework-budget-is-spent) and [P1.7](#p17--align-fact_verifications-rework-budget-with-the-flows-own-convention) address the mechanism; the findings themselves need a human pass.
2. **At least one finding (F14) stood on an invalid probe**, and whether the repair landed cannot be confirmed from the artifacts.
3. **The verifier bounded itself**: claim-vs-snippet confirmed by opening the file for ~180 of ~215 entries, the rest machine-located only, and four git-based verifications could not be re-checked and remain unverified.

The flow caught its own blind spots three times through three different gates — the untraversed `decisions/` tier, the incomplete manifest, the false F32 conclusion. A pipeline that surfaces its own defects deserves more confidence than a smooth report with no gates; it does not deserve to be read as an acceptance verdict.

## Not in scope

- **Model and reasoning tuning.** No node looks mis-tiered. `architecture_design` at `xhigh` earned its cost (see [P1.6](#p16--correct-the-architecture_design-node-comment-and-drop-the-downgrade-advice)); `critical_review` at `xhigh` found the run's only evaluator `high` for $3.30, the best return in the run; turn budgets never bound (peak 151 of `max_turns: 400`). The lever set here is the graph, the prompts and the checker — not the models. Per-node `{model,reasoning}` defaults remain the separate `node_defaults` backlog item.
- **Re-routing the `coverage_gate` rework edge** — considered and rejected under [P0.1](#p01--give-a-re-entering-node-its-own-prior-output).
- **`fact_verification`'s `network_access: true`** — measured `web_search_requests: 0, web_fetch_requests: 0`, because with `external_research` disabled there are no external `url` entries to fetch. Unused surface on this task shape, not a defect; a per-task concern, not a packaged-default change.
- **An empty `.claude/` directory** appeared inside the deliverable folder (a provider initialised it there). Git does not track empty directories, so it did not reach the pull request. Cosmetic; recorded only so the next reader is not alarmed.
- **Target re-sync.** Everything here lands in `packaged/`; getting it into `wastech-mdlint` is the existing `target-resync-after-deep-research.md` + [upgrade-flows.md](upgrade-flows.md) work, and this document adds to that queue rather than duplicating it.

## Execution order

1. **P0.2** — the fixer contract. Everything else buys rounds; this makes a round worth buying.
2. **P0.3** — skip spent evaluators. Independent of P0.2, biggest single saving, small change.
3. **P0.1** — the self-prior channel. Largest code surface of the three; makes the P1.4 contract true.
4. **P1.4, P1.5** — move two classes of miss from expensive evaluators to the cheap gate and the free checker. Independent of each other and of the P0 items.
5. **P1.6** — docs correction. Do it before anyone acts on the stale comment.
6. **P1.7** — budget alignment, **only after P0.2 and P0.3**.
7. **P2.8–P2.11** — independent, schedulable individually.

## Data gaps

Almost none: `prompt_audit: true`, so `prompt-audit/timeline.jsonl` plus 31 per-node prompt files are on record, and they are what proved [P0.1](#p01--give-a-re-entering-node-its-own-prior-output). `runs/exchange-seals/` were evicted at the terminal transition, which is expected under `logging.clean_runs_on_success: true` for a `done` task — set it to `false` before a run to analyse the sealed exchange afterwards. What could **not** be assessed: whether each individually named fix landed in the rounds where no later evaluator re-raised it (F14 in particular) — which is exactly the visibility [P0.2](#p02--make-synthesiss-rework-contract-explicit-and-class-wide) step 3 would add.
