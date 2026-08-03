# Batch A — post-mortem: `p9-11-01`, `p9-11-02`, `p9-11-03`

Target: `/Users/a1234/Documents/GitHub/wastech-mdlint` · Levers: `/Users/a1234/Documents/GitHub/wastech-orchestrator`

## Verdict (3 lines)

All three runs ended `done`/attempt 1 with clean diffs and zero infra noise — but **all three rework loops were caused by defects authored in the `planning` node's own plan text, not by the implementation node**. I traced every round‑1 blocking/high finding back to a quoted line in `plan.md`; implementation executed the plan faithfully and reasoned almost not at all (**880–1,516 thinking tokens vs planning's 59–72k**), because the plan was an over‑specified code‑level spec that left nothing to decide. The single highest‑value change is a **plan‑review gate between `planning` and `implementation`** (flow), closely followed by fixing the **model/effort inversion** (planning + implementation ran `claude-sonnet-5`; the reviewer that caught everything ran `claude-opus-5`).

---

## ⚠️ Correction to the shared brief (verify before reusing)

The brief's config section says the flow "pins per node: planning `claude-opus-5`/`xhigh`, implementation `claude-opus-5`/`xhigh`, review `claude-opus-5`/`high`, fixing `claude-opus-5`/`xhigh`". **That describes the file as it sits on disk today, not what ran.** `.worc/flows/implementation.yaml` has mtime **Jul 28 00:21**, two days after the last of these runs finished (Jul 26 03:33 local). The frozen per‑request record says:

| node | model that ran | effort that ran | source |
| --- | --- | --- | --- |
| planning | **claude-sonnet-5** | **max** | `stages/planning/run-*/1-claude/request.json` |
| implementation | **claude-sonnet-5** | xhigh | `stages/implementation/run-*/1-claude/request.json` |
| review | claude-opus-5 | **xhigh** | `stages/review/run-*/1-claude/request.json` |
| fixing | **claude-sonnet-5** | **max** | `stages/fixing/run-*/1-claude/request.json` |
| documentation | claude-opus-5 | medium | `stages/documentation/run-*/1-claude/request.json` |

Verified two independent ways. (1) `request.json` `model`/`reasoning` and the literal `argv` (`"--model","claude-sonnet-5","--effort","max"`). (2) Cost arithmetic against confirmed pricing (`claude-opus-5` $5/$25 per MTok, `claude-sonnet-5` $3/$15; cache read 0.1×, 1h cache write 2×) — for `p9-11-03`:

- implementation: 3,078,351 cache-read + 91,141 1h-cache-write + 18,470 out → Sonnet **$1.748** vs reported **$1.7499** ✓ (Opus would be $5.52)
- review: 1,018,776 + 74,409 + 23,530 → Opus **$1.842** vs reported **$1.8445** ✓ (Sonnet would be $0.90)

`config.yaml` (mtime Jul 25 22:47, _before_ the runs) is genuine run-time state; the provider default `claude-opus-5`/`high` is correct as the brief states. It is only the per‑node flow pins that post-date the runs.

---

## Per-run frame

### `p9-11-01-cli-bin-noop` — 2 rework rounds, $23.73, worst of 20

`planning`(943s, $3.04) → `implementation`(264s, $1.47) → `testing`(pass 21s) → `review`(**rework**, $1.57) → `fixing`(1472s, $6.62) → `testing`(pass) → `review`(**rework**, $2.15) → `fixing`(871s, $4.69) → `testing`(pass) → `review`(accept, $1.84) → `documentation`($1.01) → `publish`. `fix_iterations=2`, `test_fix_cycles=0`, `review_fix_cycles=0` (the `tasks` row's cycle counters are 0 while `fix_iterations=2` — worth a separate look). Diff: 4 files, +390/−12. Two `fixing` rounds cost **$11.31 — 7.7× the implementation node**.

### `p9-11-02-sec003-path-escape` — 1 rework, $20.22

`planning`(810s, $3.17) → `implementation`(245s, $1.67) → `testing`(pass) → `review`(**rework**, $2.17) → `fixing`(1464s, **$8.27**) → `testing`(pass) → `review`(accept, $2.20) → `documentation`($1.42) → `publish`. Diff: 16 files, +499/−20.

### `p9-11-03-init-schema-clobber` — 1 rework, $19.32

`planning`(857s, $3.32) → `implementation`(282s, $1.75) → `testing`(pass) → `review`(**rework**, $1.84) → `fixing`(1474s, **$8.97**) → `testing`(pass) → `review`(accept, $1.51) → `documentation`($1.11) → `publish`. Diff: 6 files, +404/−26.

All three: `stage_attempts=1` everywhere, `provider_used='claude'`, `route_fallback` never used, `skipped=0`, no HITL, `validation_report.json` = `passed:true, completeness:"complete"`, all 10 `check_runs` per task green with `timed_out=0`.

---

# Findings, ranked by impact

## F1 — The plan authored every round-1 blocking/high finding, and no node reviews the plan

**Category** flow (+spec) · **Severity** high · **Confidence** high

### Evidence

**`p9-11-03`** — the plan contains, 32 lines apart, a proof and its own contradiction. `/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/logs/p9-11-03-init-schema-clobber/plan.md:23`:

> So a schema write is only ever _attempted_ when `existingConfigAction === "merge"` — never `"overwrite"`, never `"none"`.

…and `plan.md:55` specifies the user-facing string:

> `"kept"` → `` `Kept existing schema.json at ${schema.path} (custom rules present); run again with --on-existing overwrite to replace it.` ``

Review verdict (eval `124`, `state.db`.`evaluations`), severity **blocking**:

> `formatSchemaWriteLine`, case `"kept"` prints "…; run again with --on-existing overwrite to replace it." — advice for a route that provably cannot work. … The same false claim was copied into three docs.

The plan also authored the round‑1 **high** verbatim at `plan.md:44`: `if (schemaAlreadyExists && existingConfigAction !== "overwrite")` — the existence-only predicate. And propagated the false remediation into the docs spec at `plan.md:118` ("replaced only under an explicit `--on-existing overwrite`").

**`p9-11-01`** — the plan authored the test's fatal dependency and the inverted comment. `.../p9-11-01-cli-bin-noop/plan.md` (~:180) specifies literal source:

```
// npm/pnpm/yarn create a real symlink at this exact name on POSIX; on Windows they instead
// generate wastech-mdlint.cmd/.ps1/(bash) shims that resolve the real target path themselves
// without a symlink — which is why H-1 never reproduced there (audit finding H-1).
const cliBin = path.join(repoRoot, process.platform === "win32" ? "node_modules/.bin/wastech-mdlint.cmd" : "node_modules/.bin/wastech-mdlint");
function assertBuilt(): void { for (const target of [cliDistIndex, cliBin]) { if (!existsSync(target)) { throw new Error(...
```

Review round 1 (eval `98`) **high**: "`assertBuilt()` runs at module scope and throws for `cliBin`; CI runs `npm ci` **before** `npm run typecheck`, and `dist/` is gitignored… Result: `npm test` fails at collection on ubuntu/windows/macos." Round 1 **low**: "A load-bearing `why` comment that is factually inverted." Round 1 **medium** on `shell: process.platform === "win32"` — mandated by `plan.md:26-33`, complete with the wrong justification that an argv array satisfies the security rule. Round 2 **medium** on missing timeouts — the plan explicitly deferred it (`plan.md:~303`): "bump the other tests' timeouts too if CI (especially `windows-latest`) shows they're tight against the 5s default."

**`p9-11-02`** — the plan asserted a false platform claim and knowingly overrode a task constraint. `plan.md:59`: "correctly rejecting Windows drive-absolute/UNC forms via `path.isAbsolute` on that platform." Review round 1 (eval `113`) **high**: "The plan's claim that `path.isAbsolute` covers all Windows absolute forms holds for `C:\...`/UNC but not for the drive-relative `X:name` form." `plan.md:173`+`182-190`+`206` decided to leave `reference.ts:36,142` and `graph/coverage.ts:95` on the weaker predicate ("Left unchanged", "**Don't touch `escapesRoot`'s existing signature/behavior**") while the task's Constraints say "one shared containment helper … rather than adding a second, divergent check" and acceptance criterion 4 requires no other site can escape. Review round 1 **high** #2 is exactly that.

### Root cause

`packaged/flows/implementation.yaml` `edges` go `planning → implementation` with **no evaluator between them**. The plan is not a sketch — it is a code-level specification (both `p9-11-01` and `p9-11-03` plans contain literal TypeScript for the files to be written), and it is binding. Nothing validates it before ~$1.7 of implementation and ~$9 of fixing are spent executing it. The `review` evaluator is the first and only check, and by then the plan's defects are shipped code. Planning is also the second-largest cost centre in the whole batch ($74.94 / 22 runs per the brief) yet is the only expensive node with no downstream verdict.

The supervisor is **not** a substitute: `evaluations` eval `95` (supervisor_step, node=planning, `p9-11-01`) says _"This is a solid, well-reasoned plan for P11.01"_ and praises the `--no-install` deferral as _"good practice, not a defect"_ — the very item that became a `low` finding on the final accept. Supervisor runs `claude-sonnet-5`/`medium`.

### Lever

- **Flow, orchestrator default** — `src/wastech_orchestrator/packaged/flows/implementation.yaml`: insert an `evaluator` node between `planning` and `implementation`. Schema-valid today: `core/flow/contracts.py:32-45` ships `EvaluatorRole.CRITIC` and `VERIFIER` and states "operator-authored flows may use other role strings"; `core/flow/schema.py:101-130` gives `blocking`, `gate_severity`, `max_rework_per_stage`, `model`, `reasoning`; `core/flow/validator.py:252-266` only requires that a named `loop` on a rework edge appears in `budgets`, and `budgets` is a free-form `MappingProxyType[str,int]` (`schema.py:279`). So:
  ```yaml
  - id: plan_review
    kind: evaluator
    role: critic
    role_file: implementation/plan_review.md   # new role file needed
    blocking: true
    max_rework_per_stage: 1
  edges:
    - { from: planning, to: plan_review }
    - { from: plan_review, to: implementation, outcome: accept }
    - { from: plan_review, to: planning, outcome: rework, loop: plan_fix }
  budgets: { plan_fix: 1, ... }
  ```
  A read-only evaluator pass costs roughly what `review` costs (~$1.5–2.2) against a rework round that costs **$8–11**.
- **Cheaper interim (target-only, no new node)** — add to `.worc/flows/implementation/planning.md` a self-consistency clause: _"Before returning, re-read your own plan end to end. Every user-facing string, flag, or route you specify must be reachable by the code path you specified; no statement may contradict another. If your own analysis proves a route unreachable, do not also instruct the user to take it."_ That single rule would have caught `p9-11-03`'s blocking finding.

**Scope** orchestrator default (the gap is in the packaged flow, not mdlint-specific). **Expected impact** the direct cause of 3/3 rework loops here and, by the brief's count, likely a large share of the 8/20 batch-wide.

---

## F2 — Model/effort inversion: the builder was weaker and thought less than everyone

**Category** model + reasoning · **Severity** high · **Confidence** high

### Evidence

Exact thinking-token totals, summed from `estimated_tokens_delta` on `system`/`thinking_tokens` events in each node's `events.jsonl`:

| node | effort | p9-11-01 | p9-11-02 | p9-11-03 |
| --- | --- | --- | --- | --- |
| planning (sonnet-5) | max | 71,534 | 59,082 | 62,013 |
| **implementation (sonnet-5)** | **xhigh** | **880** | **992** | **1,516** |
| review (opus-5) | xhigh | 14,869 / 19,874 / 20,056 | 19,045 / 14,062 | 16,305 / 11,905 |
| fixing (sonnet-5) | max | 93,008 + 48,379 | 97,220 | 80,330 |
| documentation (opus-5) | medium | 1,905 | 2,335 | 1,225 |

**The implementation node thought less than the documentation node.** 880 tokens across 16 thinking events in 86 assistant turns for `p9-11-01`. Its `fixing` counterpart thought **141,387** tokens — **161×** more. `review` at the same nominal `xhigh` produced 15–20k, so the setting works; implementation simply had nothing to decide.

### Root cause

Two compounding errors, and they are the same error seen from two sides:

1. **Tier inversion.** The reviewer (`claude-opus-5`) is strictly stronger than the planner and builder (`claude-sonnet-5`). A stronger auditor grading a weaker author's work _will_ find defects — that is the design, and it is why the review findings are so good (see "what's already good"). But it makes a rework loop the expected outcome rather than the exception, and it routes the expensive discovery to the most expensive point in the flow.
2. **Effort inversion → transcription collapse.** `planning` at `max` produced a plan so fully specified (literal source for the files to write) that `implementation` at `xhigh` degenerated into transcription. Its unused reasoning budget is the measurable signature: ~1k thinking tokens. An implementer that does not reason cannot catch a bad plan, no matter what tier it runs.

### Lever

- **The operator has already partially applied this** — the Jul 28 edit to `.worc/flows/implementation.yaml` promoted planning/implementation/fixing to `claude-opus-5` and normalised effort. That is the right direction and is not yet validated by a run. **But the same edit lowered `review` from `xhigh` → `high`**: `review` at `xhigh` is what read npm's bundled `bin-links`/`libnpmexec` sources and found a `path.win32.relative` cross-device escape. Recommend restoring `review: reasoning: xhigh` (it is the cheapest node per unit of value in the batch: $1.5–2.2 to prevent an $8–11 round).
- **Orchestrator default** — `packaged/flows/implementation.yaml` currently ships every per-node `model`/`reasoning` **commented out** except `documentation: reasoning: medium`, so every node inherits the provider default. That is a reasonable default, but add a comment documenting the invariant an operator must not break: _"never pin `implementation` to a weaker model or lower effort than `review` — a stronger auditor over a weaker author converts review from a safety net into a guaranteed rework loop."_

**Scope** the pins are target-only; the guidance comment is an orchestrator default. **Expected impact** removes the structural guarantee of rework; frees the implementation node to reason instead of transcribe.

---

## F3 — `implementation.md` treats the plan as ground truth; `fixing.md` explicitly does not

**Category** prompt · **Severity** high · **Confidence** high

### Evidence

`.worc/flows/implementation/implementation.md:1` (rendered at `stages/implementation/run-000112/rendered-prompt.md:13`) — the entire instruction about the plan:

> Implement the assigned task in the working tree by following the plan.

Contrast `.worc/flows/implementation/fixing.md:13` (rendered at `stages/fixing/run-000115/rendered-prompt.md:25`):

> Fix each finding at its cited location, **treating the `fix:` hint as a lead, not ground truth.** When the finding concerns a factual claim about this product … **re-open the authoritative source and confirm the corrected claim there** rather than trusting the finding's wording.

The `fixing` node is told to distrust its input. The `implementation` node is told to follow its input. The consequence is measurable in F2's table — and in `p9-11-01`, the plan itself begged to be questioned (`plan.md:~298`: "Notes on this sketch the implementer should resolve, **not defer**") and implementation deferred all three items, two of which became review findings.

**Packaged Aug-3 copy does not fix this.** Diffing `packaged/flows/implementation/implementation.md` against the target's active Jul-25 copy shows the drift runs the _other_ way: the packaged copy is the **generic** version (the operator hand-specialised the target's copies for TypeScript/npm/mdlint; packaged was subsequently de-specialised). The opening sentence is identical in both. `grep -rn -i "skip|\[x\]|checkbox|tick|third-party|wrap|exit criteri"` across both role-prompt trees returns **only** an unrelated `summary.md` hit. None of F3, F7, F8, F9 are addressed in packaged.

### Lever

`src/wastech_orchestrator/packaged/flows/implementation/implementation.md` — add a "The Plan Is A Lead, Not Ground Truth" section mirroring `fixing.md`'s existing one:

> Follow the plan, but treat its factual claims as leads, not ground truth. Before you write code that depends on one, re-open the authoritative source and confirm it: a claim about this product's surfaces against the current tree, a claim about a third-party tool (a package manager, test runner, or formatter) against that tool's own source, `--help`, or a live probe. Resolve every item the plan flags as unresolved or deferred — do not carry a deferral into the diff. If the plan contradicts itself, or contradicts a task constraint or acceptance criterion, say so and follow the more specific source.

**Scope** orchestrator default (mirror into the target's active copy for the next mdlint run). **Expected impact** the cheapest of all the F1 mitigations — a prompt edit, no new node, no extra provider call. Would have caught `p9-11-01`'s ambient-shim high and `p9-11-03`'s blocking finding at implementation time.

---

## F4 — `fixing` is expensive because of `reasoning: max`, not repo re-exploration; the findings payload is fine

**Category** reasoning · **Severity** medium-high · **Confidence** high

Direct answer to the assigned question: **no, it is not re-exploring the whole repo, and yes, the review findings reach it in a usable form and cost nothing.**

### Evidence (`p9-11-03`: fixing $8.97 / 18.7M in vs implementation $1.75 / 3.2M)

| measure | implementation | fixing | ratio |
| --- | --- | --- | --- |
| **baseline (first request) context** | **29,952 tok** | **29,526 tok** | **1.0×** |
| median request context | 64,105 | 177,237 | 2.8× |
| max request context | 91,143 | 271,143 | 3.0× |
| API round-trips (distinct requests) | 50 | 103 | 2.1× |
| assistant messages | 86 | 215 | 2.5× |
| tool calls | 51 | 111 | 2.2× |
| `Read` / `Grep` | 16 / 2 | 34 / 4 | 2.1× / 2× |
| total tool_result bytes | 86 KB | 270 KB | 3.1× |
| **thinking tokens** | **1,516** | **80,330** | **53×** |
| output tokens (billed) | 18,470 | 121,305 | 6.6× |

Three conclusions follow:

1. **The findings payload is not the problem.** Fixing's first request is _smaller_ than implementation's (29,526 vs 29,952) despite carrying two extra context files (`diff`, `review` findings.json) — see `stages/fixing/run-000115/1-claude/request.json` `context_paths`. The payload is compact and usable: fixing's `final_message` in `result.json` addresses all four findings by severity with specifics ("**Blocking** — the `"kept"` message told users to fix a stale `schema.json` via `--on-existing overwrite`, which provably can't work…").
2. **It is not re-exploring.** 34 `Read` + 4 `Grep` against implementation's 16 + 2. More, but nowhere near a repo sweep; 270 KB of total tool output ≈ 69k tokens, a fraction of the 18.7M billed.
3. **The cost is 2.1× round-trips × 2.8× context, and the context growth is thinking.** 80,330 thinking tokens accumulate across ~103 round-trips; because thinking blocks are echoed back into an agentic loop, the integral is ≈ 80k × 103/2 ≈ **4.1M extra cache-read tokens ≈ $1.24**, plus ~$1.20 of billed thinking output — roughly **$2.5 of the $8.97 attributable directly to `effort: max`**, with the rest being the same conversation carried across 2× more turns.

### Lever

- `.worc/flows/implementation.yaml`, node `fixing`: `reasoning: max` → `xhigh` (or `high`). The work is bounded and pre-diagnosed — a reviewer has already named each defect and supplied a `fix:` hint. `max` is for open-ended correctness-critical reasoning; this node is closing a checklist. The already-applied Jul 28 edit moved fixing to `xhigh` — **correct, keep it.**
- `packaged/flows/implementation.yaml` — the `documentation` node carries an exemplary inline rationale for its `reasoning: medium` pin ("a mechanical write-up of what already shipped… no quality loss, less wall-time on the tail"). Add the parallel note to the commented `fixing` block: _"fixing works from an itemised review verdict, so it does not need the flow's top effort — the findings are the reasoning."_

**Scope** target pin + orchestrator guidance. **Expected impact** ~25–30% off the most expensive node, no loss of fix quality (fixing's actual fixes were excellent — see F‑ok).

---

## F5 — An unwritten, unenforceable markdown wrap convention consumed >50% of the most expensive node's tool work

**Category** checks (+spec) · **Severity** medium-high · **Confidence** high

### Evidence

Of `fixing`'s 59 Bash calls in `p9-11-03` (`stages/fixing/run-000115/1-claude/events.jsonl`), roughly **30 are manual markdown re-wrapping**. It wrote a Python wrapping script from scratch **six separate times** (calls 9, 11, 16, 38, 40, 48 — `cat <<'PYEOF' > "$TMPDIR/wrap.py"`), ran it at **three different widths** (`wrap.py 96 "  "`, `wrap.py 98 "  "`, `wrap.py 99 "  "`), and repeatedly measured line lengths by hand (`awk 'NR>=108 && NR<=137 {print NR": "length($0)}' README.md`).

What triggered it — review eval `124`, severity `low`:

> In the `init` bullet the inserted sentence pushed the line break inside the `` `--on-existing merge` `` code span … **Prettier preserves multi-line code spans verbatim, so the format gate will not correct it**; it renders acceptably but reads as broken in source.

The root cause is a config fact: `/Users/a1234/Documents/GitHub/wastech-mdlint/.prettierrc` is

```json
{ "singleQuote": false }
```

— no `proseWrap`, so Prettier's default `"preserve"` applies and **prose is never reflowed**. `npm run format` is `prettier --check .`, which therefore passes no matter where lines break. Meanwhile the repo's markdown _is_ hand-wrapped to ~99 columns by convention, and `grep -rn -i "wrap|99|column|prose" AGENTS.md .agents/rules/*.md` in the target returns **nothing relevant** — the convention is documented nowhere. Hence three guessed widths.

The same class appears on the accept verdict too (eval `128`, `low`): "glossary.md: the rewrap left the stub line `  (default no). See **Init &` mid-paragraph… Prettier's proseWrap will not correct any of these, so the format gate stays green while the source reads broken." This is the brief's recurring defect class A, and this run shows its true cost: it is not a cosmetic nit, it is the largest single consumer of the batch's most expensive node.

### Lever

- **Target repo, primary** — set `"proseWrap": "never"` in `wastech-mdlint/.prettierrc` (exactly what `wastech-orchestrator` itself does per `AGENTS.md` + `.prettierrc.json`). Then `prettier --write` reflows automatically, `--check` enforces it, the defect class becomes mechanically impossible, and the manual rewrapping disappears. This is the single cleanest fix in the report.
- **Target repo, fallback if hand-wrapping is deliberate** — document the exact column in `wastech-mdlint/AGENTS.md` (the agent guessed 96/98/99) and add a check to `.worc/config.yaml` `checks.command_sets.default` that detects a code span split across a newline, so `testing` catches it instead of `review`.
- **Orchestrator, secondary** — this is the second instance of the same structural problem as the brief's signal #6 (`npm ci` / clean-CI class): the `review` evaluator is doing the checks' job because `checks.command_sets` cannot see the defect. Worth a backlog note that `command_sets` coverage should be audited against the review findings that recur, since every class the checks can't see costs a full rework round instead of a 20-second gate run.

**Scope** target-only for the fix; orchestrator for the pattern. **Expected impact** removes a `low` finding from nearly every run in the batch and ~30 bash calls / ~half the tool work from each `fixing` round.

---

## F6 — `gate_severity: high` let a task-constraint violation ship, and `fixing` made it worse

**Category** config + flow · **Severity** medium · **Confidence** high

### Evidence

`p9-11-02`'s task Constraints (`task.normalized.json`): _"Containment logic belongs in `packages/core` (**one shared helper** — extend the existing `path-resolve.ts` rather than adding a second, divergent check)."_

Round 1 review (eval `113`, **high**) flagged that the diff created two divergent-strength checks. The `fixing` node's response was to add a **third** helper. Final `current.diff` for `packages/core/src/engine/path-resolve.ts` ships `escapesRoot` (pre-existing), **`candidateEscapesRoot`** (new in the fix round), and **`resolvesOutsideRoot`** — all exported. The accept verdict (eval `117`) says so, at `medium`:

> Three containment helpers now coexist (`escapesRoot`, `candidateEscapesRoot`, `resolvesOutsideRoot`) where the task's deliverable 3 asks for "one shared containment helper". … `escapesRoot` remains `export`ed with no cross-module consumer left, so the weakest check is still the most reachable one for a future call site — **the divergent-strength footgun the previous round flagged.**

So the run shipped, on an explicit `[x]`-ticked acceptance criterion, the exact architectural defect the task forbade — and the flow accepted it because `medium < high`.

`core/flow/schema.py:122-128` documents the knob:

> `gate_severity: str = DEFAULT_GATE_SEVERITY` — "Minimum finding severity that gates (drives `rework`)… Default `high` = block on high/critical/blocking (historical behavior). Lower it (e.g. `low`) to make a content critic block on any finding."

This is the concrete, traceable instance of the brief's cross-cutting signal #1 (62 findings on `accept` verdicts, never closed).

### Lever

- `.worc/flows/implementation.yaml`, node `review`: set `gate_severity: medium` (target-only). With `max_rework_per_stage`/`review_fix` budgets already bounding the loop, the downside is bounded and the upside is that a violated task constraint stops being shippable. Do **not** go to `low` — that would gate on the markdown-wrap nits of F5 and be strictly worse until F5 is fixed. Sequence: fix F5 first, then lower `gate_severity`.
- Alternative that costs no rework rounds: have the `documentation` node's role file require that every unresolved accept-verdict finding be recorded in the phase file's Implementation notes as a named residual, so a medium finding cannot vanish silently. `packaged/flows/implementation/documentation.md` is the file.

**Scope** target for the knob; orchestrator for the documentation-node rule. **Expected impact** closes the accept-with-findings leak that the supervisor itself flagged on `p9-12-05`.

---

## F7 — Acceptance-criteria checkboxes ticked without evidence; no role prompt forbids it

**Category** prompt · **Severity** medium · **Confidence** high

### Evidence

`p9-11-01` round 2 (eval `102`), **high**:

> Two exit criteria are checked off with no verification behind them: "- [x] `./node_modules/.bin/wastech-mdlint --version` prints the version and exits `0`." and "- [x] `npx wastech-mdlint lint <fixture-with-error>` exits non-zero and prints findings." The only test covering either is … `it.skipIf(!ambientBinIsUsable())(…)`, which the file's own header explains can never run on a CI host … **the phase doc records a release-blocking audit item as verified-green on the strength of a test that executes nowhere.**

It survived into the final accept as a `medium` (eval `106`): "The exit criterion … is checked off unqualified, but nothing in the repo invokes the ambient `node_modules/.bin/wastech-mdlint`". Same class on `p9-11-02` (accept criterion 4 ticked while two sites stayed on the weaker predicate — F6). This is the brief's defect class C, and it is the _only_ recurring class that is a truthfulness defect rather than a code defect.

Neither `documentation.md` nor `implementation.md` — in the target's active copies **or** the packaged Aug-3 defaults — says anything about verifying a checkbox before ticking it. The target's `documentation.md` only says: _"When the change **completes a phase**, update that phase's task file: set **Status → Done**, check its exit-criteria boxes"_ — an instruction to tick, with no evidence requirement.

### Lever

`packaged/flows/implementation/documentation.md` (and the target's active copy) — add:

> Tick an exit-criterion box only when you can name the artifact that proves it — a test that executes on every host the criterion claims, a check command's output, or a quoted source line. If a criterion is satisfied by an equivalent-but-different mechanism, qualify the box inline rather than ticking it bare. If it is not verified, leave it unticked and say why in Implementation notes. A ticked box that no executing test backs is a false record, and the format/test gates cannot detect it.

**Scope** orchestrator default. **Expected impact** removes a recurring `high`/`medium` class that no automated gate can catch, at zero runtime cost.

---

## F8 — A permanently-skipped test shipped in round 1 and became round 2's blocking finding

**Category** prompt (+checks) · **Severity** medium · **Confidence** high

Direct answer to the "did the same finding class recur" question: **yes — round 2 was the residue of round 1's own fix.**

### Evidence

Round 1's **high** was "the suite hard-depends on an ambient `node_modules/.bin` shim that a clean CI install does not produce." The fixing node solved this correctly for the _primary_ tests (manufacturing a POSIX symlink / Windows junction in a temp dir) but for the `npx` check it **skipped instead of fixing**, on the strength of a claim it had invented:

> "npx resolves a local bin by walking ancestor node_modules/.bin directories from `cwd`; there is no way to redirect that lookup into an isolated temp dir"

Round 2's **high** (eval `102`) refuted it:

> the repo gains its first permanently-skipped test (no precedent anywhere under `packages/*/test`) … The `why` comment that justifies the skip is also wrong … **npm exec derives `localBin` from the `localPrefix` it detects by walking up from its own `cwd`, so pointing `cwd` at a manufactured temp dir does redirect it.**

So round 1 and round 2 are the same root cause one layer down: a _partial_ fix (skip where a fix was needed) plus a self-authored, unverified technical claim. Round 2 was avoidable.

Nothing in `implementation.md`, `fixing.md`, or `review.md` — active or packaged — mentions skipped tests (verified by grep). `review.md`'s blocking list covers "**Zero test coverage** for new core user-visible behavior" but a test that exists and never runs is not zero coverage by that wording; it took the reviewer's own judgment to flag it.

### Lever

- `packaged/flows/implementation/implementation.md` and `fixing.md` — add to the Tests section: _"Never ship a test that cannot run on a host the task's criteria cover. A conditional skip is acceptable only for a genuine platform capability gap, and must name the capability and the coverage that substitutes for it on that host. If you find yourself skipping a test to satisfy a claim about how a third-party tool behaves, verify the claim against that tool first — a skip justified by an unverified claim is a defect, not a mitigation."_
- `packaged/flows/implementation/review.md` — add to Blocking Invariant Violations: _"A new test that is unconditionally or effectively-always skipped (its guard is false on every CI host), especially when an acceptance criterion rests on it."_

**Scope** orchestrator default. **Expected impact** would have collapsed `p9-11-01` from 2 rework rounds to 1 — worth ~$4.7 on that task alone.

---

## F9 — The "verify the claim" clause covers only _this product_, not third-party tools

**Category** prompt · **Severity** medium · **Confidence** high

### Evidence

Every one of the following wrong claims is about a **third-party tool**, and every one caused a finding:

| wrong claim | tool | authored in | caught as |
| --- | --- | --- | --- |
| Windows `.cmd` shims "resolve the real target path themselves without a symlink" | npm bin-links | plan | round-1 `low` |
| `shell: true` + argv array is safe on Windows | Node `spawnSync` / `cmd.exe` | plan | round-1 `medium` |
| npx's lookup "can't be redirected into a temp dir" | `libnpmexec` | impl + fixing | round-2 `high` |
| `--no-install` is defense-in-depth | npm 7+ config | plan + impl | accept `low` |
| `path.isAbsolute` covers all Windows absolute forms | `node:path` | plan | round-1 `high` |
| byte comparison is safe for a generated file | git `core.autocrlf` | fixing | accept `medium` |

The relevant clauses are scoped to the product. `fixing.md:13`: _"When the finding concerns a factual claim about **this product** — a CLI command/flag/option value, a core contract or result shape, an MCP tool or its schema — re-open the authoritative source…"_ `planning.md`: _"Verify every path you cite against the current tree"_ and _"bind each item to the specific command or type that owns it"_ — all about the product's own surfaces. Third-party runtime behavior is outside both.

Notably the reviewer _did_ do this work — it read npm's bundled `bin-links` (`link-gently.js`, `shim-bin.js`), `@npmcli/config`, and `libnpmexec`. So the capability exists at the opus tier; it is simply not asked for upstream.

### Lever

`packaged/flows/implementation/planning.md`, `implementation.md`, `fixing.md` — extend the verification clause: _"…and the same standard applies to a claim about a third-party tool's behavior (a package manager, its bin-linking or exec resolution, the test runner's defaults, the formatter's options, the platform's path semantics). Confirm it against that tool's own source, its `--help`, or a live probe in this workspace — do not recall it. A load-bearing `why` comment that states a wrong fact about a tool is a defect, not documentation."_

**Scope** orchestrator default. **Expected impact** high leverage relative to effort — this one clause covers 6 of the ~20 findings across these three runs.

---

## F10 — The supervisor observes `rework` and `pass` verdicts blind (already in flight)

**Category** infra / instrumentation · **Severity** low-medium · **Confidence** high

`evaluations` eval `99` (supervisor_step, node=review, `p9-11-01`):

> "Review step came back requesting rework, **but no details were included about what issues were found.**"

And eval `97`/`101` (node=testing): _"Testing step reported a bare 'pass' with no accompanying detail (no test counts, no output)."_

So the advisory layer is blind on precisely the two node kinds whose detail matters. **This is already being fixed on the current branch**: `src/wastech_orchestrator/core/supervisor.py:354` now has `_render_findings_digest` ("Render an evaluator's findings as bounded lines for the observation prompt"), and the untracked `core/supervisor_packet.py` carries `check_runs`, a `_checks()` split by result, and `findings_path`. No new recommendation — noting it as independently confirmed by these artifacts, and worth a regression test that a `rework` observation prompt contains the findings digest.

---

## F11 — Checked and _not_ a problem: redundant gate runs

Worth recording so it is not mistaken for a cost driver. `fixing` ran the project gate ~16 times inside its own session, including five near-complete final sweeps (bash calls 29, 53+54, 56, and 59: `npm run typecheck && npm run lint && npm run format && npm test && npm run build && echo "ALL GATES GREEN"`), and the orchestrator's `testing` node then ran the identical five commands again:

```sql
SELECT command, exit_code, secs FROM check_runs WHERE task_id LIKE 'p9-11-03%';
-- 00:54:20 typecheck 0 0s / lint 0 3s / format 0 4s / test 0 11s / build 0 0s
-- 01:25:23 typecheck 0 0s / lint 0 3s / format 0 4s / test 0 11s / build 0 0s
```

Total 19s per pass, and fixing's Bash output across all 59 calls is only **66 KB** (piped through `| tail -N`). So the redundancy costs ~2% of wall time and a rounding error in tokens. The `fixing.md` "Work one failure at a time… re-run that same command" instruction is fine as written; no change recommended.

---

## Category verdict on the central question

For these three runs the answer is unambiguous and uniform:

- **(a) ambiguous/under-constrained role prompt** — contributing, and the precise gap is nameable: `implementation.md` trusts the plan absolutely (F3), and the verify-your-claims clause excludes third-party tools (F9).
- **(b) task-spec gap** — **no.** All three `task.normalized.json` specs are strong: explicit deliverables, acceptance criteria, and constraints, with `validation_report.json` = `complete`. In `p9-11-02` the spec _correctly forbade_ the exact defect that shipped ("do not add a divergent second check") and in `p9-11-01` it correctly warned "branch explicitly rather than assuming POSIX". The specs were right and were overridden.
- **(c) checks gap** — **yes, real but secondary.** Two classes are invisible to `command_sets`: clean-CI (`npm ci` before build) and markdown source wrapping (`proseWrap: preserve`). F5 is the expensive one.
- **(d) model/reasoning fit** — **yes, and it is the enabling condition.** F2: sonnet planner + sonnet builder + opus reviewer, with the builder reasoning ~1k tokens. Partly fixed already on Jul 28.
- **(e) irreducible** — **only a thin slice.** `p9-11-02`'s Windows drive-relative `path.win32.relative` cross-device escape is genuinely obscure. Everything else was either provable from the plan's own text (`p9-11-03`'s self-contradiction), readable in the repo's own CI workflow (`p9-11-01`'s `npm ci` ordering), or written in the task's own Constraints (`p9-11-02`'s "one shared helper").

**The dominant cause is (f) — a flow gap the brief's categories don't name: an unreviewed, over-specified plan.**

---

## What's already good (checked, keep it)

- **The `review` evaluator is exceptional and is the reason these runs are safe.** It traced `existingConfigAction → action === "fresh" → existingRules = [] → projectSchema undefined` to prove a documented remediation route unreachable; it read npm's bundled `bin-links`, `@npmcli/config`, and `libnpmexec` to disprove two claims; it found a `path.win32.relative` cross-device escape. `$1.5–2.2` per pass. Do not weaken it — and reconsider the Jul 28 `xhigh → high` downgrade.
- **Diff scope discipline is excellent, 3/3.** `p9-11-01` 4 files, `p9-11-03` 6, `p9-11-02` 16 — every file traceable to a deliverable, zero unrequested files, no gold-plating. The supervisor independently confirmed it (eval `96`: "Files touched exactly match the plan's scope … No incidental touches").
- **`p9-11-01`'s final state is better than acceptable.** The fixing node read npm's own sources, **disproved its own earlier wrong claim**, and converted the permanently-skipped `npx` test into one that actually runs on POSIX by manufacturing a local-bin fixture; it added `30_000`ms timeouts, made `assertBuilt()` mtime-aware (catching _stale_, not just missing, `dist/`), pinned the deterministic finding count (`1 problem (1 error, 0 warnings)`), removed the `shell: true` hazard entirely, and made the realpath guard two-sided for `--preserve-symlinks`. Two residuals remain open on the accept verdict, both correctly characterised as follow-ups.
- **The `Fix The Finding, Then Its Class` section in `fixing.md` demonstrably works.** `p9-11-03`'s fixer swept one false claim across four files in a single round (`init-command.ts`, `README.md`, `docs/guide/cli.md`, `docs/mdlint_v2/glossary.md`) rather than one per round. Keep this section verbatim.
- **Zero infra noise across 29 node runs**: `stage_attempts=1` everywhere, no `route_fallback`, no `error_class`, no `process_crashed`, no HITL, no `timed_out`, `terminal_cleanup: completed` on all three.
- **Caching and prompt scaffolding are healthy.** 97–99% cache-read, and the _baseline_ prompt is the same size for `fixing` (29,526) as for `implementation` (29,952) — the four-file context payload is compact and the exchange plumbing is not the cost.
- **`documentation: reasoning: medium` is correctly calibrated** and carries an exemplary inline rationale in the packaged flow. $1.01–$1.42, 100–159s, 1.2–2.3k thinking tokens. Use it as the template for the `fixing` pin comment in F4.

---

## Data gaps

1. **`usage_reasoning_output` is NULL everywhere — cause found.** The brief asked not to re-report this without a cause; here it is. `providers/claude.py:_normalize_claude_usage` sets `reasoning_output=None` **by design**, with an accurate docstring: _"Claude … folds reasoning into output, so `reasoning_output` stays `None`."_ But the stream **does** carry it: `events.jsonl` contains `{"type":"system","subtype":"thinking_tokens","estimated_tokens":N,"estimated_tokens_delta":D}` — 634 such events in one `fixing` run, peaking at 9,300 in a single turn. Summing `estimated_tokens_delta` in `parse_stream_json` and threading it into `NormalizedUsage.reasoning_output` (the field already exists at `providers/base.py:314`, and `providers/codex.py:544` already populates its equivalent) would close the gap. Caveat: the field is named `estimated_`, so it is Claude Code's own estimate, not a billed count — surface it as an estimate. **This is the highest-value instrumentation fix in the report**, because reasoning volume turned out to be the decisive diagnostic for F2 and F4 and it currently requires hand-parsing event logs.
2. **No `runs/exchange-seals/` for these tasks.** `.worc/runs/` is empty — expected, since `logging.clean_runs_on_success` defaults true and all three succeeded. I could not read the exact `.worc-io/.../findings.json` the fixing node saw, only the equivalent `evaluations.findings_json` + the review `result.json` `structured_output` (which are byte-identical in content). Not a real gap here, but set `logging.clean_runs_on_success: false` before the next batch to inspect the curated agent-facing surface directly.
3. **`tasks.review_fix_cycles = 0` while `fix_iterations = 2`** on `p9-11-01` (and `=1`/`=1` mismatches on 02/03). All three rework loops were `review → fixing` with `loop: review_fix`, so `review_fix_cycles` should be non-zero. Either the counter is not written or it means something other than its name. Small but it means the ledger's own rework accounting can't be trusted without joining `node_runs`. Worth a look at the engine's loop bookkeeping.
4. **No CI evidence.** Every cross-platform claim in these three runs (Windows junction behavior, `windows-latest` timeouts, `.cmd` `PATHEXT` resolution) is unverified on a real Windows or macOS host — checks ran only locally on darwin. The reviewer flagged this repeatedly and it remains open. This is the strongest argument for F5's sibling recommendation: `command_sets` that cannot reproduce CI conditions push the whole class into the review evaluator.
