# 03 — Prompt findings

Why 8 of 20 runs took a rework loop, and the exact wording that would prevent it. Every recommendation here is a role-prompt edit; none needs a code change.

## A note on drift direction

Before recommending any role-prompt edit, check which copy is newer. The direction is not what it looks like:

- **Five role files** (`planning`, `implementation`, `review`, `fixing`, `documentation`) — the packaged copies are at commit `61ef90f` (Jul 25) and have been **genericised** for repo-neutrality; the target's `.worc/flows/implementation/*.md` are the operator's hand-specialisations for this monorepo (npm scripts, `docs/guide/` layout, mdlint invariants). Packaged is not "newer and already fixed" — it is the generic base.
- **Two role files** (`summary.md`, `supervisor.md`) — packaged is genuinely newer (`dd51d39`, Aug 3) and the target is running copies dated Jul 13. **The target should be refreshed regardless of anything else in this report.**

For every finding below, appendices A, B and D independently verified that the packaged Aug-3 copy does **not** already address it. Both copies need each edit.

## P1 — In 6 of 6 analysed rework loops, the plan authored the defect and implementation never diverged {#p1}

**Category** prompt (planning + plan handoff) · **Severity** high · **Confidence** high · **Scope** both

This is the central finding of the whole post-mortem. Appendices A and B traced every round-1 blocking/high finding back to a quoted line in `plan.md`. Implementation executed the plan faithfully in all six runs — it diverged in zero load-bearing ways.

**The three sharpest cases.**

_p9-11-03_ — the plan contains its own refutation, 32 lines apart. `plan.md:23` proves the route unreachable:

> So a schema write is only ever _attempted_ when `existingConfigAction === "merge"` — never `"overwrite"`, never `"none"`.

`plan.md:55` then specifies the user-facing string that tells the user to take it:

> `` `Kept existing schema.json at ${schema.path} (custom rules present); run again with --on-existing overwrite to replace it.` ``

The reviewer's **blocking** finding is exactly that contradiction. A single self-consistency pass over the plan would have caught it.

_p9-11-07_ — the plan told the implementer not to look where the bug was. It attached a `.refine()` to the exported `ruleEntrySchema`, then included a section headed:

> `## Confirmed non-impact (don't touch, and don't spend time re-verifying)` → `- **CLI/MCP packages** — no changes.`

That symbol is the MCP `lint` tool's wire input schema (`packages/mcp-server/src/tools/lint.ts:44`). Planning made 28 greps — **13 into `node_modules/zod/` internals, zero into `packages/mcp-server/`**. Implementation made 33 tool calls, 16 of them `npm` runs, and **zero greps**. The `fixing` node's first two real calls were `Read packages/mcp-server/src/tools/lint.ts` and `Grep ruleEntrySchema` — precisely the lookup the plan had forbidden. Cost of that one omission: **$3.88**.

_p9-11-10_ — the plan contradicted itself across 20 lines. `plan.md:53` says _"For schema, use `command.out` as typed"_; `plan.md:73` instructs the implementer to write the doc claim that `command.out` echoing violates. The reviewer's **high** finding is that internal contradiction, against an exit criterion the same diff ticked `[x]`.

**Root cause.** `planning.md` asks for a plan the implementer can run _"without re-deriving the approach"_. At `max`/`xhigh` effort the model complies maximally: it emits literal source, a confident rationale, and sometimes an explicit do-not-check list. Assertions and verified facts are typographically indistinguishable. `implementation.md` then says only _"Implement the assigned task in the working tree by following the plan"_ — with no instruction to validate anything. The reviewer is the first node with a mandate to disagree, one full rework round too late.

The measurable signature is thinking volume. In era A, summing `estimated_tokens_delta` from `events.jsonl`:

| node               | thinking tokens (p9-11-01 / 02 / 03) |
| ------------------ | ------------------------------------ |
| planning           | 71,534 / 59,082 / 62,013             |
| **implementation** | **880 / 992 / 1,516**                |
| review             | 14,869 / 19,045 / 16,305             |
| fixing             | 141,387 / 97,220 / 80,330            |
| documentation      | 1,905 / 2,335 / 1,225                |

**The implementation node thought less than the documentation node.** An implementer that does not reason cannot catch a bad plan whatever tier it runs on.

**Lever — three edits.**

`planning.md`, `## What To Produce`:

> Label every load-bearing claim as **verified** (you read the code that proves it — cite file:line) or **assumed** (you did not check). Never write a "do not re-verify" instruction: if you did not verify it, say so and name the check the implementer should run. Before returning, re-read your own plan end to end — every user-facing string, flag, or route you specify must be reachable by the code path you specified, and no statement may contradict another.

`planning.md`, `## Explore Before You Plan`:

> Before planning a change to any exported symbol, signature, schema, or error message, enumerate its consumers with a repo-wide search and list them in the plan — including tests and other packages or hosts. A narrowing change is only safe once every consumer is named. Your test list must cover every call site your file list creates or modifies.

`implementation.md`, first paragraph:

> Follow the plan, but treat its factual claims as leads, not ground truth. Before you write code that depends on one, re-open the authoritative source and confirm it. Resolve every item the plan flags as unresolved or deferred — do not carry a deferral into the diff. If the plan contradicts itself, or contradicts a task constraint or acceptance criterion, say so and follow the more specific source.

Note the asymmetry this fixes: `fixing.md` **already** tells its node to distrust its input (_"treating the `fix:` hint as a lead, not ground truth… re-open the authoritative source"_). `implementation.md` has no equivalent. Mirroring the clause it already ships elsewhere is the cheapest available intervention.

**A stronger option, if you want a structural fix.** Appendix A shows a `plan_review` evaluator between `planning` and `implementation` is schema-valid today — `EvaluatorRole.CRITIC` exists in `core/flow/contracts.py`, and `budgets` is free-form. A read-only pass costs ~$1.5–2.2 against rework rounds that cost $6–11. This is a flow change and deserves its own ADR; the prompt edits above should land first and be measured.

**Expected impact.** In appendix B's three runs alone, the spend attributable to this class is **$15.90 of $55.66 (29%)** plus ~1,900 s of wall time.

## P2 — `review.md` never states the accept/rework rule or the severity enum {#p2}

**Category** prompt · **Severity** high · **Confidence** high · **Scope** orchestrator default

The words "accept" and "rework" appear nowhere in `review.md` as criteria, and the severity enum is never defined. The actual gate is `DEFAULT_GATE_SEVERITY = "high"` in `core/flow/schema.py`, and the active flow never mentions `gate_severity` at all. So **a must-fix finding labelled `medium` is a silent accept** — the evaluator has no way to know that its severity choice is the routing decision.

This is the real mechanism behind the 62 accepted findings, and it produced at least three verified consequences:

- _p9-11-02_ shipped a task-constraint violation. The task said _"one shared containment helper … rather than adding a second, divergent check"_; the fix round added a **third**, and the accept verdict recorded it as `medium`. The exit criterion was ticked `[x]`.
- _p9-11-09_ shipped a real correctness defect: `stageWrite` leaks a `.tmp` file on ENOSPC/EIO — _"precisely the ENOSPC scenario the module header calls out"_ — filed `medium`, structurally un-routable.
- _p9-11-06_ returned an **empty findings array** while its diff carried the exact markdown defect the same reviewer, same model, same reasoning, same prompt rated **medium/rework** on p9-11-07. The reviewer's own `events.jsonl` shows it read the offending line.

**Lever.** `packaged/flows/implementation/review.md` — state the contract the code implements:

> Your verdict is derived from severity: a finding at or above the flow's gate severity routes the change back for rework; anything below is recorded as an accepted residual and reaches the operator as a follow-up, with no code step to close it. Choose severity accordingly. If a finding must be fixed before merge, it is not `medium`. If you are unwilling to block on it, phrase it as a follow-up rather than a finding, so the verdict and the findings list agree. Severities are `blocking`/`critical`, `high`, `medium`, `low`.

Also worth removing from the target copy: `review.md:15` tells the reviewer the phase doc is _"the documentation step's job"_, which appendix D identifies as contributing to p9-11-06's empty verdict. The packaged Aug-3 copy already narrows this correctly to _"do not flag those as **missing**"_ — one case where refreshing from packaged helps.

**On lowering `gate_severity` to `medium`:** tempting, and appendix A recommends it — but sequence it. Do it _after_ the `proseWrap` fix in [04](04-flow-and-config-findings.md#t1), or you will gate on markdown-wrap nits. Appendix C prices the change at +$50–95 per 20 runs. Severity _calibration_ via the prompt above is the cheaper first move.

## P3 — The verify-your-claims clause covers only "this product", not third-party tools {#p3}

**Category** prompt · **Severity** medium · **Confidence** high · **Scope** orchestrator default

Six wrong claims across appendix A's three runs, every one about a third-party tool, every one causing a finding:

| wrong claim | tool | caught as |
| --- | --- | --- |
| Windows `.cmd` shims resolve the real target without a symlink | npm bin-links | round-1 `low` |
| `shell: true` + argv array is safe on Windows | Node `spawnSync` / `cmd.exe` | round-1 `medium` |
| npx's lookup can't be redirected into a temp dir | `libnpmexec` | round-2 `high` |
| `--no-install` is defence-in-depth | npm 7+ config | accept `low` |
| `path.isAbsolute` covers all Windows absolute forms | `node:path` | round-1 `high` |
| byte comparison is safe for a generated file | git `core.autocrlf` | accept `medium` |

`fixing.md` scopes its verification clause to _"a factual claim about **this product**"_; `planning.md` to the product's own surfaces. Third-party runtime behaviour falls outside both. The reviewer _did_ do this work — it read npm's bundled `bin-links`, `@npmcli/config` and `libnpmexec` — so the capability exists; it simply is not asked for upstream.

**Lever.** Extend the clause in `planning.md`, `implementation.md` and `fixing.md`:

> The same standard applies to a claim about a third-party tool's behaviour — a package manager, its bin-linking or exec resolution, the test runner's defaults, the formatter's options, the platform's path semantics. Confirm it against that tool's own source, its `--help`, or a live probe in this workspace; do not recall it. A load-bearing `why` comment that states a wrong fact about a tool is a defect, not documentation.

One clause covers 6 of ~20 findings in that batch.

## P4 — In-node gate re-runs and serial single-hunk edits are the reducible turn cost {#p4}

**Category** prompt (efficiency) · **Severity** medium · **Confidence** high · **Scope** orchestrator default

Given the `tool_calls^1.651` cost law in [01](01-frame-and-economics.md#the-empirical-cost-law), turns are the expensive resource. Two sources are pure waste.

**Gate re-runs inside writer nodes.** p9-11-14's implementation node ran the full five-command check set **35 times**; its fixing node **20 times**. The `testing` node then ran the identical five commands in 24 s. Appendix E measures 14 in-node invocations including 3 full sweeps on p9-11-09. Both role prompts instruct this, and the packaged Aug-3 wording is slightly _worse_ — it adds _"catching a failure now saves a full review/fix round trip later"_ with no frequency discipline.

**Serial single-hunk edits.** p9-11-10 edited `program.ts` **13 times** and `load-config.ts` **7 times** in one node; each of the 51 Edit round-trips re-read a 100–180 k-token prefix. Collapsing 13 edits into ~3 passes removes ~10 round trips ≈ 1.5 M cache-read tokens ≈ **$0.75 on that node alone**.

**Lever.** `implementation.md` and `fixing.md` — add a short `## Working Efficiently` section:

> Every tool round trip re-processes the whole conversation, so turns are the expensive resource. When you have several changes to the same file, apply them in one pass rather than one edit per hunk, and read a file once at the offset you need. While iterating, run only the narrowest check that covers what you just changed; run the project's full gate once, at the end, when you believe the work is complete.

Do **not** add a turn cap — p9-11-14 used 234 of 400 turns and `max_turns_gate: false` means a cap would truncate rather than fail.

**Expected impact.** ~8–12% on large-diff implementation nodes. Against $100.63 of implementation spend across 20 runs, ~$10 per campaign of this size, plus a proportional wall-clock saving.

## P5 — Exit criteria are ticked without evidence, and no prompt forbids it {#p5}

**Category** prompt · **Severity** medium · **Confidence** high · **Scope** orchestrator default

The only recurring class that is a _truthfulness_ defect rather than a code defect, and the one no automated gate can catch. Appendix A found it on p9-11-01 as a round-2 **high**:

> Two exit criteria are checked off with no verification behind them … The only test covering either is `it.skipIf(!ambientBinIsUsable())(…)`, which the file's own header explains can never run on a CI host … **the phase doc records a release-blocking audit item as verified-green on the strength of a test that executes nowhere.**

Same class on p9-11-02 (criterion 4 ticked while two sites stayed on the weaker predicate), p9-12-02 (criterion 2 ticked by the very edit that broke it), and p9-12-06 (criterion 3 ticked while half of it was deliberately not delivered — correctly reasoned, but recorded as a bare tick).

The target's `documentation.md` says only _"check its exit-criteria boxes"_ — an instruction to tick, with no evidence requirement. Neither copy of either file says anything about verifying first.

**Lever.** `packaged/flows/implementation/documentation.md`:

> Tick an exit-criterion box only when you can name the artifact that proves it — a test that executes on every host the criterion claims, a check command's output, or a quoted source line. If a criterion is satisfied by an equivalent-but-different mechanism, qualify the box inline rather than ticking it bare. If it is not verified, leave it unticked and say why in Implementation notes. A ticked box that no executing test backs is a false record, and the format and test gates cannot detect it.

## P6 — A skipped test shipped, then became the next round's blocking finding {#p6}

**Category** prompt · **Severity** medium · **Confidence** high · **Scope** orchestrator default

p9-11-01 is the only run that needed two rework rounds, and round 2 was the residue of round 1's own fix. Round 1's `high` was the ambient-shim dependency; the fixing node solved it for the primary tests but for the `npx` check it **skipped instead of fixing**, justified by a claim it invented (_"there is no way to redirect that lookup into an isolated temp dir"_). Round 2's `high` refuted it by reading `libnpmexec`: pointing `cwd` at a manufactured temp dir _does_ redirect it. Round 2 was avoidable, and cost ~$4.7.

Nothing in `implementation.md`, `fixing.md` or `review.md` — active or packaged — mentions skipped tests. `review.md`'s blocking list covers _"zero test coverage for new core user-visible behavior"_, and a test that exists but never runs is not zero coverage by that wording.

**Lever.** In `implementation.md` and `fixing.md`:

> Never ship a test that cannot run on a host the task's criteria cover. A conditional skip is acceptable only for a genuine platform capability gap, and must name the capability and the coverage that substitutes for it on that host. If you find yourself skipping a test to satisfy a claim about how a third-party tool behaves, verify the claim against that tool first — a skip justified by an unverified claim is a defect, not a mitigation.

And in `review.md`'s blocking list:

> A new test that is unconditionally or effectively-always skipped — its guard is false on every CI host — especially when an acceptance criterion rests on it.

## P7 — Doc claims are verified for citation, not for quantifier {#p7}

**Category** prompt · **Severity** medium · **Confidence** high · **Scope** both

p9-12-02 is the cheapest and cleanest-looking run in the campaign — `[]` findings, 3 files, 100% docs — and it shipped a wrong claim. Its glossary edit states that default severity _"derive[s] from the assert `kind`, not from config"_. But `rules/custom.ts:92` is a flat constant `defaultSeverity: "error"` for all 13 kinds, and `run-rules.ts:42` + `lint-files.ts:47` show config **does** override it. Three gates checked the first half of an `A and B` claim and stopped: review returned `[]`, and the documentation node listed that exact claim under _"Claims I verified against the code (all accurate)"_.

**Lever.** Strengthen the doc-verification bullet in `review.md` and `documentation.md` (packaged does not fix this):

> When you verify a claim about this product, check its quantifier and its direction, not just that the cited literal appears at the cited line. "Derived from X, not from Y" requires confirming both that X determines it and that Y cannot override it. A claim of the form "A and B" is unverified until both halves are.
