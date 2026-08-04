# Batch B post-mortem — p9-11-04, p9-11-07, p9-11-10

**Verdict (3 lines).** All three runs shipped correct code, and in all three the rework loop was caused by a defect the **plan itself specified** — implementation diverged from the plan in exactly zero load-bearing ways. Planning is therefore earning its cost as an _architecture_ artifact and losing money as a _correctness_ artifact: at `max`/`xhigh` effort the plan acquires false authority (a literal code block + a rationale) that implementation transcribes and the reviewer is the first node to challenge. The single biggest improvement is not more planning — it is two paragraphs in `planning.md` (blast-radius enumeration + verified-vs-assumed labelling) and one in `implementation.md` (verify the plan's load-bearing claims, don't transcribe them).

> **Correction to the shared brief, §"Config that was active".** The brief states the flow pinned `planning`/`implementation`/`fixing` to `claude-opus-5`. That is true only for runs from **p9-11-08 onward**. `.worc/flows/implementation.yaml` was last written **Jul 28 00:21** — _after_ p9-11-01…07 ran (Jul 26) and _during_ p9-11-10. The frozen per-attempt `request.json` files are authoritative and show two distinct configuration eras. Two of my three runs are in era A. This is load-bearing for every model/reasoning finding below.
>
> | era | runs | planning | implementation | fixing | review |
> | --- | --- | --- | --- | --- | --- |
> | **A** (Jul 26) | p9-11-01…07 | `claude-sonnet-5` / **`max`** | `claude-sonnet-5` / `xhigh` | `claude-sonnet-5` / **`max`** | `claude-opus-5` / **`xhigh`** |
> | **B** (Jul 28+) | p9-11-08…14, all p9-12 | `claude-opus-5` / `xhigh` | `claude-opus-5` / `xhigh` | `claude-opus-5` / `xhigh` | `claude-opus-5` / `high` |
>
> Evidence: `/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/logs/p9-11-04-findconfig-boundary/stages/planning/run-000120/1-claude/request.json` → `"model": "claude-sonnet-5", "reasoning": "max"`; `…/p9-11-10-cli-exit-contract/stages/planning/run-000162/1-claude/request.json` → `"model": "claude-opus-5", "reasoning": "xhigh"`. Per-run frozen flow snapshots exist at `.worc/control-bundles/<task-id>/flows/implementation.yaml`. Model ids and effort ladder confirmed via the `claude-api` skill (Opus 5 $5/$25 per MTok, cache read ≈0.1× = $0.50/MTok, cache write 1.25× = $6.25/MTok; Sonnet 5 $3/$15; effort levels `low|medium|high|xhigh|max`, Opus 5 default `high`).

---

## Run frames

All three: `done`, attempt 1, `stage_attempts=1` everywhere, zero fallbacks/HITL/skips, no decomposition, all 10 check commands green in 20–24s per pass, path `planning → implementation → testing → review(rework) → fixing → testing → review(accept) → documentation → publish`.

### p9-11-04-findconfig-boundary — era A

`node_runs`/`provider_attempts` (SQL below): planning 1306s/$5.63 · implementation 378s/$2.90 · review 390s/$2.29 (**rework**) · fixing 592s/$3.51 · review 349s/$2.47 (accept, **6 findings attached**) · documentation 114s/$0.97. Total $19.13. Diff: 15 files, +396/−92 — src +97, test +131, **docs +168 (42%)**. Review round 1 raised **1 blocking** + 1 medium + 4 low.

### p9-11-07-custom-missing-id — era A

planning 952s/$3.18 · implementation 208s/$1.03 · review 367s/$2.13 (**rework**) · fixing 312s/$1.74 · review 289s/$1.81 (accept, 2 low) · documentation 89s/$0.82. Total $11.97. Diff: **4 files, +136/−7** — src **+21**, test +68, docs +47. Review round 1 raised **1 high** + 1 medium + 1 low. Planning cost **3.1× implementation** and ran **4.6× longer** for a 21-line source change.

### p9-11-10-cli-exit-contract — era B

planning 599s/$4.27 · implementation **1004s/$9.68 / 12.86M input** · review 308s/$2.37 (**rework**) · fixing 512s/$3.86 · review 287s/$2.20 (accept, 2 medium + 1 low) · documentation 123s/$1.24. Total $24.56. Diff: 15 files, +799/−87 — src +266 (incl. a new 99-line module), test +344, docs +189. Review round 1 raised **1 high** + 1 medium + 4 low.

SQL used throughout:

```sql
SELECT nr.task_id, nr.node_id, nr.status, nr.outcome, nr.stage_attempts,
  CAST((julianday(nr.finished_at)-julianday(nr.started_at))*86400 AS INT) AS secs,
  pa.usage_input_total, pa.usage_cache_read, pa.usage_cache_write,
  pa.usage_uncached_input, pa.usage_output_total, ROUND(pa.usage_cost,3)
FROM node_runs nr LEFT JOIN provider_attempts pa ON pa.node_run_id = nr.id
WHERE nr.task_id IN ('p9-11-04-findconfig-boundary','p9-11-07-custom-missing-id',
                     'p9-11-10-cli-exit-contract') ORDER BY nr.task_id, nr.id;
```

---

## Findings, ranked by impact

### F1 — 3/3 rework loops were caused by defects the plan specified verbatim; implementation never diverged

**Category:** prompt (planning + plan-handoff) · **Severity:** high · **Confidence:** high

**EVIDENCE — p9-11-04.** The plan authored the blocking bug, with a wrong rationale. `/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/logs/p9-11-04-findconfig-boundary/plan.md:25` and its code block at `:39-42`:

> "Add the same `os.homedir()` boundary … **checked _before_ testing each directory for the config file** (mirrors `findAncestor`'s `if (directory === resolvedBoundary) return undefined;` ordering, so the home directory itself is never treated as a hit — same rationale as the CLI siblings: a dotfiles repo at `$HOME` must never be mistaken for the project's own config)."
>
> ```ts
>   for (;;) {
>     if (directory === homeDir) {
>       return undefined;
>     }
> ```

`…/stages/review/run-000123/findings.json` finding 1, severity **blocking**:

> "the bound `if (directory === homeDir) { return undefined; }` is evaluated before the candidate test on the first iteration, so a config located at the caller's OWN directory is never found when that directory is `$HOME`. … reaches the unconditional `await writeFile(configPath, result.configText, "utf8")` — destroying the existing config with no prompt … **The dotfiles rationale in the comment only justifies rejecting an ANCESTOR at/above `$HOME`, not the directory the caller named.**"

**EVIDENCE — p9-11-07.** Same shape, plus an explicit don't-check directive. `…/p9-11-07-custom-missing-id/plan.md:24-41` proposes attaching the refine directly to the exported schema:

> ```ts
> // packages/core/src/config/config-schema.ts — replace the ruleEntrySchema definition (~lines 74-81)
> export const ruleEntrySchema = z
>   .object({
>     rule: z.string().min(1).refine((value) => value !== "custom", { … }),
> ```

and `plan.md:120,135` — a section titled **"## Confirmed non-impact (don't touch, and don't spend time re-verifying)"** whose last bullet reads `- **CLI/MCP packages** — no changes.`

`…/stages/review/run-000144/findings.json` finding 1, severity **high**:

> "The `.refine(…)` was attached to `export const ruleEntrySchema`, which is **not** config-private: it is a public core export (`packages/core/src/index.ts:281`) and is the MCP `lint` tool's wire input schema (`packages/mcp-server/src/tools/lint.ts:44` — `rules: z.array(ruleEntrySchema)`) … an MCP `lint` call with `{"rule":"custom"}` now fails SDK-level input validation (protocol invalid-params) instead of reaching `handleLint` … the contract it claims to pin is now hollow."

**EVIDENCE — p9-11-10.** Two plan lines produced the high and the medium. `…/p9-11-10-cli-exit-contract/plan.md:53`:

> "`handleSchema` (`:385-386`) and `handleCompile` (`:433-434`): wrap `mkdir` + `writeFileAtomic` in try/catch → `throw new CliUsageError(formatWriteFailure(relativePath, error))`. … **For schema, use `command.out` as typed, matching `:381`.**"

…while `plan.md:73` in the _same document_ instructs the implementer to write the doc claim it contradicts (`docs/guide/cli.md` exit-code table). Review round 1, severity **high**:

> "`handleSchema`'s new write guard … echoes `command.out` verbatim, so `schema --out /abs/dir/schema.json` emits `Could not write /abs/dir/schema.json (EACCES).` … **This directly contradicts two claims this same diff adds and ticks off**: the exit criterion 'Operational error messages print repo-relative POSIX paths, not absolute ones' … marked `[x]`."

And `plan.md:43` produced the medium verbatim: "`formatOperationalError(error, cwd)` — if the error carries a string `code`, render `` `${code}…` `` and **never** `error.message`" → review medium: "`formatOperationalError` discards `error.message` for _any_ `Error` carrying a string `code` … prints `Operational error: ERR_INVALID_ARG_TYPE` with zero diagnostic content, a regression."

**EVIDENCE — implementation never diverged.** `events.jsonl` traces show the implementation node read the plan as its second tool call in all three runs, then executed the plan's file list in the plan's order:

- p9-11-04: 110 turns / 64 tool calls; sequence is `Read task.md → Read plan.md → find-config.ts → init-command.ts → commands.ts → program.ts → init-prompter.ts` then Edits in plan-step order 1→6, then the full gate. No file outside the plan's list was edited.
- p9-11-07: 59 turns / **33 tool calls, 16 of them `npm` check runs**. Non-check calls: `Read task.md`, `Read plan.md`, `Read config-schema.ts`, 2 Edits, `Read rules-custom.test.ts`, `Read config-error.ts`, 2 Edits, then phase-doc edits. **Zero `Grep`s.** It never looked at any consumer of `ruleEntrySchema`.
- p9-11-10: 189 turns / 125 tool calls, all inside the plan's declared file set plus the two new files the plan named.

**Root cause.** `planning.md` asks for a plan the implementer can run "without re-deriving the approach" (`.worc/flows/implementation/planning.md:8`: _"Keep it concrete and no longer than an implementer needs to execute without re-deriving the approach."_). At `max`/`xhigh` effort the model complies maximally: it emits literal code, a confident rationale, and (p9-11-07) an explicit list of things not to re-verify. `implementation.md:1` then says _"Implement the assigned task in the working tree by following the plan"_ with no instruction to independently validate the plan's load-bearing claims. The plan's assertions and its verified facts are typographically indistinguishable, so the implementer treats both as ground truth. The reviewer is the first node with a mandate to disagree — which is exactly one full rework round too late.

**Lever.**

1. `src/wastech_orchestrator/packaged/flows/implementation/planning.md`, `## What To Produce` — add: _"Label every load-bearing claim as **verified** (you read the code that proves it — cite file:line) or **assumed** (you did not check). Never write a 'do not re-verify' instruction: if you did not verify it, say so and name the check the implementer should run."_
2. Same file, `## Explore Before You Plan` — add a blast-radius bullet: _"Before you plan a change to any exported symbol, signature, schema, or error message, enumerate its consumers with a repo-wide search and list them in the plan (including tests and other packages/hosts). A narrowing change is only safe once every consumer is named."_
3. `src/wastech_orchestrator/packaged/flows/implementation/implementation.md`, first paragraph — add: _"The plan is a strong hypothesis, not a specification. Before you transcribe a code block or accept a 'no impact here' claim, confirm it against the code — one search per load-bearing claim. If the plan is wrong, implement what is correct and say so in your final message."_

**Role-prompt drift check (required).** Diffed the target's Jul-25 active copies (`/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/{planning,implementation,fixing}.md`) against the Aug-3 packaged defaults (`src/wastech_orchestrator/packaged/flows/implementation/…`). The drift runs the _opposite_ direction from a staleness problem: the packaged copies have been **genericized** (project-agnostic wording, "if present", "the project's own check command"), and the target's copies are hand-specialized for wastech-mdlint (monorepo layout, `npm run typecheck|lint|format|test|build`, mdlint invariants). **The packaged Aug-3 copy does NOT address this finding** — neither file contains the word "consumer", "blast radius", "verified", or any reverse-dependency instruction. Both copies need the edit.

**Scope.** Orchestrator default (all three edits) — this defect class is provider- and repo-neutral. Also apply to the target's copies, since this repo's active files are customized and will not pick up the packaged change.

**Expected impact.** Directly targets the 8/20 review→fixing loops in the batch. In these three runs the wasted spend attributable to F1 is $2.29+$3.51 (p9-11-04), $2.13+$1.74 (p9-11-07), $2.37+$3.86 (p9-11-10) = **$15.90 of $55.66 (29%)** plus ~1900s of wall time.

---

### F2 — `max` effort on Sonnet 5 was a pure cost trap; the Jul-28 switch to Opus 5/`xhigh` already proves it

**Category:** reasoning/model · **Severity:** high · **Confidence:** high

**EVIDENCE.** Cross-run matrix from `request.json` + `node_runs`/`provider_attempts` (era boundary at p9-11-08):

| node | era A (`sonnet-5`) | era B (`opus-5`/`xhigh`) |
| --- | --- | --- |
| planning secs | 943, 810, 857, **1306**, 883, 715, **952** → mean **924** | 394, 774, 599, 486, 1122, 558, 658, 475, 232, 439, 675, 396, 640 → mean **573** |
| planning USD | 3.04, 3.17, 3.32, **5.63**, 3.28, 2.37, **3.18** → mean **3.43** | mean **3.63** |
| fixing | 1472/$6.62, 1464/$8.27, 1474/$8.97, 592/$3.51, 312/$1.74 (`max`) | 512/$3.86, 847/$8.23, 317/$1.87 (`xhigh`) |

Era-A `fixing` at `max` cost **4.5×–5.1× its own implementation** on p9-11-01/02/03 ($6.62 vs $1.47; $8.27 vs $1.67; $8.97 vs $1.75) — same model, only the effort differed (`max` vs `xhigh`). Era B inverts cleanly: fixing/implementation = 0.40, 0.30, 0.73. The 1472/1464/1474s cluster is a saturation signature, not task variance.

Where the tokens went: p9-11-04 planning emitted **120,752 output tokens** (`provider_attempts.usage_output_total`) to deliver a 15,388-byte plan (~4K tokens) — `events.jsonl` contains **46 `thinking` blocks and only 734 characters of visible `text`**, so ≈96% of output was thinking. p9-11-07 planning: 85,886 output for an 11,223-byte plan. Compare era-B p9-11-10 planning: **46,246 output / 599s** for a materially _larger_ change (799 vs 396 added lines).

This matches the documented behavior confirmed via the `claude-api` skill for Sonnet 5 `max`: _"Can deliver gains in some use cases but may show diminishing returns from increased token usage; can be prone to overthinking — test before committing."_ And for Opus 5: _"start `xhigh` for coding/agentic … then sweep down — `low`/`medium` are unusually strong here."_

**Root cause.** `max` was set on the two nodes that are most rewarded for confident output (planning, fixing) on the weaker of the two models. That buys thinking tokens, not correctness — F1 shows the plan's _content_ was wrong at `max`.

**Lever.** `.worc/flows/implementation.yaml` (target) — already partly done as of Jul 28; **finish it** by dropping planning from `xhigh` to `high` and running a sweep:

```yaml
- id: planning
  model: claude-opus-5
  reasoning: high # was xhigh (era B) / sonnet-5 max (era A)
- id: fixing
  model: claude-opus-5
  reasoning: high # findings arrive pre-diagnosed; xhigh is not buying anything
```

Packaged default `src/wastech_orchestrator/packaged/flows/implementation.yaml` ships all per-node `model`/`reasoning` slots commented out (inheriting `agents.providers.claude.reasoning`), so **no packaged change is needed** — this is a target-only tuning finding. Do **not** raise `implementation` below `xhigh` (era-B implementation is where the real work happens and p9-11-10/13/14 needed the depth).

**Concrete per-node recommendation for p9-11-07-class tasks (question 3).** For a task whose shipped source change is **21 added lines in one file** (`packages/core/src/config/config-schema.ts`, +21/−1) plus 68 test lines, `max` on planning is unjustifiable: planning spent 952s/$3.18/85,886 output tokens and its 11,223-byte plan devoted **13 of 28 greps and 8 of 26 reads to `node_modules/zod/src/v4/core/` internals** (`handleUnionResults`, `util.aborted`, `$ZodObject.catchall`) to predict *message wording*, which the plan itself concedes is not the safety property (`plan.md:64-66`: *"The *safety* property (no `TypeError`, always `CONFIG_INVALID`) does **not** depend on which union member's issue zod surfaces"*). Meanwhile **zero** tool calls touched `packages/mdlint/../mcp-server`(verified:`grep -c "packages/mcp-server" …/planning/run-000141/1-claude/events.jsonl`→`0`). Recommend `planning: reasoning: high`on`claude-opus-5`, and let F1's blast-radius bullet redirect the saved budget to the one grep that mattered. Expected: planning ≈$1.5–2.0 / ≈400s on a task this size (cf. p9-12-02, an era-B S-size task: planning 232s/$1.47).

**Scope.** Target-only.

**Expected impact.** On the era-A profile the switch already realized ≈38% planning wall-time reduction at flat cost and removed the fixing-cost inversion. A further `xhigh→high` step on planning/fixing should recover ~$0.5–1.0/node with no quality signal to lose (F1 shows correctness is prompt-bound, not effort-bound).

---

### F3 — Planning is handed to implementation as a bare file path with an explicit "do not assume" hedge and no obligation to read it

**Category:** prompt / infra (prompt-building code) · **Severity:** medium · **Confidence:** high

**EVIDENCE.** `…/p9-11-04-findconfig-boundary/stages/implementation/run-000121/rendered-prompt.md:73-75` — the last three lines of the entire prompt:

```
Context files (read them as needed; do not assume their contents):
- task: /Users/a1234/Documents/GitHub/wastech-mdlint/.worc-io/p9-11-04-findconfig-boundary/task.md
- plan: /Users/a1234/Documents/GitHub/wastech-mdlint/.worc-io/p9-11-04-findconfig-boundary/plan.md
```

The plan text is **not embedded** — only referenced. Mechanism confirmed in `/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/providers/base.py:235-257`:

```python
def build_context_footer(request: AgentRunRequest) -> str:
    """Render the non-``None`` context file paths as a deterministic footer (paths only)."""
    fields = (("task", request.task_path), ("plan", request.plan_path), …)
    …
    lines = ["Context files (read them as needed; do not assume their contents):"]
    lines += [f"- {label}: {path}" for label, path in present]
```

and `base.py:260-274` (`build_effective_prompt` = `preamble → prompt → footer`, pure concatenation). The seal `/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/exchange-seals/<task-id>/seal-000001/plan.md` confirms the agent-facing plan is byte-identical to `logs/<task-id>/plan.md`, so nothing is lost or truncated in transit.

**Root cause.** Two competing signals in the same prompt: the body says _"by following the plan"_, the footer says _"read them as needed"_. In these three runs the implementer always read it first, so the handoff is empirically fine — but it is fine by luck, not by contract. Worse for the **plan itself as an artifact**: the planning `output-schema.json` (`…/stages/planning/run-000120/1-claude/output-schema.json`) has exactly four properties — `content` (a single freeform string), `human_input`, `decompose`, `subtasks`. There is **no structured slot** for the files-to-touch list, the risks, the hardest part, or the open questions the role prompt asks for. Consequence: nothing downstream can be validated, gated, or diffed against the plan — no node can check "did the diff touch a file the plan didn't list?", and no node can surface the plan's unresolved uncertainties to the reviewer or to a human.

**Lever (two options, pick one).**

- _Low-risk:_ `src/wastech_orchestrator/packaged/flows/implementation/implementation.md` line 1 — make the read mandatory and typed: _"Read the `plan` context file before your first edit. Treat its file list as the intended scope and its rationale as a hypothesis to confirm."_ No code change; keeps the footer generic.
- _Higher-value:_ extend the planning output schema in `src/wastech_orchestrator/core/flow/nodes/agent.py` / the planning schema builder to add `files: [{path, change}]`, `risks: [string]`, `unverified_assumptions: [string]`. Then the `review` role prompt can be told to check the diff against `files`, and `unverified_assumptions` becomes the natural input to the reviewer's attention budget. This is the structural fix for F1 as well.

**Scope.** Orchestrator default. The `implementation.md` wording change is safe for every repo; the schema change touches `core/flow` and needs its own ADR under `docs/backlog/`.

**Expected impact.** Makes the plan-handoff contractual rather than incidental; enables a cheap machine check for scope drift that no node performs today.

---

### F4 — p9-11-10's 12.9M input tokens is turn-count × context, not re-exploration; the only reducible term is edit batching

**Category:** prompt (efficiency) · **Severity:** medium · **Confidence:** high

**EVIDENCE.** `…/p9-11-10-cli-exit-contract/stages/implementation/run-000163/1-claude/events.jsonl`: **189 assistant turns, 125 tool calls** — `Edit ×51, Bash ×33, Read ×32, Grep ×6, Write ×2, Glob ×1`. Summed per-turn `usage`: `uncached_input = 378`, `cache_creation = 368,203`, `cache_read = 19,267,814`, `output = 4,014` visible + 42 thinking blocks. **Peak single-turn `cache_read_input_tokens` = 180,762** (the context ceiling reached). DB row: `usage_input_total = 12,857,152`, `usage_cache_read = 12,675,830` (98.6%), `usage_uncached_input = 236`.

Total _unique_ content ever ingested from tools, measured by summing `tool_result` payload bytes: **193,580 chars ≈ 48K tokens** (`Read` 130,569 / `Bash` 45,840 / `Grep` 9,309 / `Edit` 7,533). So there is **no wide-search or re-read waste**: 6 greps total, and the only files read more than twice are large ones read in offset windows (`commands.ts` ×3, `cli.test.ts` ×3, `bin.e2e.test.ts` ×3, `init.e2e.test.ts` ×3).

Cost decomposition at Opus 5 rates: cache-read 12.676M × $0.50 = **$6.34 (65%)**, cache-write 181,086 × $6.25 = $1.13 (12%), output 61,262 × $25 = $1.53 (16%). Reported $9.684.

The reducible term is **serial single-hunk edits on the same file**: `program.ts` **×13**, `load-config.ts` **×7**, `lint.e2e.test.ts` ×6, `commands.ts` ×5, `cli.test.ts` ×5, `docs/guide/cli.md` ×5. Each of those 51 Edit round-trips re-reads a 100–180K-token prefix from cache. Collapsing 13 `program.ts` edits into ~3 passes would remove ~10 round trips ≈ 1.5M cache-read tokens ≈ **$0.75** on this node alone, and proportionally on every large implementation (p9-11-14 implementation was 1957s/$27.47 on the same profile).

**Root cause.** Nothing in `implementation.md` mentions edit batching or the cost of a round trip. The task genuinely needed 15 files and 2 new modules — this is not scope creep (see F6) — so the fix is mechanical, not scoping.

**Lever.** `src/wastech_orchestrator/packaged/flows/implementation/implementation.md`, `## Verify` section or a new short `## Working Efficiently` — add: _"When you have several changes to the same file, apply them in one pass rather than one edit per hunk — every tool round trip re-processes the whole conversation. Read a file once at the offset you need; don't re-open it to re-check a line you already have."_ Do **not** add a turn cap: `agents.providers.claude.max_turns: 400` is not the binding constraint (189 turns used) and `max_turns_gate: false` means a cap would just truncate.

**Scope.** Orchestrator default — this is provider-neutral and repo-neutral.

**Expected impact.** ~8–12% on large-diff implementation nodes. Given `implementation` is the batch's largest line item ($100.63 / 137.0M input across 20 runs), a 10% reduction is ≈$10 across a P11-sized batch.

---

### F5 — A `medium` finding raised on the accept round was a _new_ defect introduced by `fixing`, and it shipped

**Category:** flow / checks · **Severity:** medium · **Confidence:** high

**EVIDENCE.** `…/p9-11-04-findconfig-boundary/stages/review/run-000126/findings.json` (verdict `accept`), finding 3, severity `medium`:

> "Step 1's new sentence '**do not stop the walk short of the home-directory boundary just because a config might be found sooner**' instructs the agent to keep walking past a config it has already found, which contradicts both `findConfig`'s first-match-wins semantics and the next line 'Report the actual existing config path you find rather than assuming a root filename.'"

That sentence was written by the `fixing` node (`…/stages/fixing/run-000124/1-claude/events.jsonl` shows `Edit skills/wastech-mdlint-init/SKILL.md`) and is present verbatim in the final committed diff (`…/p9-11-04-findconfig-boundary/current.diff`, `skills/wastech-mdlint-init/SKILL.md` hunk `@@ -25,8 +25,11 @@`). `evaluations` for `source_node_run_id=126` records `verdict='accept'`. The `review → fixing` edge only fires on `outcome: rework` (`.worc/flows/implementation.yaml` edges), so an `accept`-with-medium-findings verdict has no consumer.

Batch contribution: p9-11-04 shipped **3 medium + 3 low** on accept, p9-11-10 **2 medium + 1 low**, p9-11-07 **2 low**. Of those, the `documentation` node subsequently closed the doc-scoped ones (see _What's already good_), leaving genuinely open: p9-11-04's `skills/wastech-mdlint-init/SKILL.md` medium + `skills/wastech-mdlint-fix/SKILL.md` low + `config-v2.test.ts` low; p9-11-10's `program.ts` "no e2e coverage for the catch-all" medium + `docs/guide/output.md` low (documentation added a cross-ref but left the false "goes to stderr" claim — `init`'s write-failure path writes to **stdout** via `runCli`).

**Root cause.** The `review` evaluator's verdict is binary but its findings are graded, and the flow graph has no path for "accept, but these N findings are real". The supervisor does capture them (F7) but nothing acts.

**Lever.** Two options, both flow-level and both compatible with the "no hardcoding / flow-agnostic engine" invariant because they are expressed as _data in the flow YAML_, not engine branches:

- Add a second `review`-driven edge gated on severity, if `core/flow/schema.py` supports an outcome for it; if not, the cheaper option is
- `src/wastech_orchestrator/packaged/flows/implementation/review.md` — instruct the evaluator that a `medium` finding it is unwilling to block on must be phrased as a follow-up, not a finding, so the accept/rework line matches the findings list. Then the residual is honestly a backlog item, not an unclosed review comment.

**Scope.** Orchestrator default (this is the brief's cross-cutting signal #1 confirmed on three more runs, with one _verified shipped defect_ as the concrete cost).

**Expected impact.** Removes the class where `fixing` introduces a new factual error that review notices and nobody fixes.

---

### F6 — Mild scope expansion in p9-11-10 (`resolveDirectoryArgument` wired to 4 commands, tested on 3); no gold-plating elsewhere

**Category:** diff · **Severity:** low · **Confidence:** high

**EVIDENCE.** `…/p9-11-10-cli-exit-contract/task.normalized.json` acceptance criteria name only `lint [path]` and the exit mapping: _"A nonexistent explicit `lint [path]` does not report `0 \"No problems found.\"`"_. The plan (`plan.md:48`) widened it: _"Wire it into `lint`/`scan` …, `graph` (`:168-180`), `init` …, and `compile --cwd` (`:260-275`)"_, calling the `cwd`-resolution part _"a small deliberate side-fix"_ (`plan.md:27`). The reviewer caught the consequence (`…/review/run-000165/findings.json`, low): _"`resolveDirectoryArgument` is wired into four call sites … but the new tests cover only `lint`, `compile --cwd`, and `init`. **`graph <missing path>` has no regression test**, so a future refactor could drop the call there silently."_

Otherwise the diffs are on-scope. p9-11-04 touched 4 files the plan explicitly excluded (`plan.md:120`: _"Skill docs … are deliberately left untouched"_), but each is traceable: `docs/mdlint_v2/P8-skills/02-skill-init.md` + `skills/wastech-mdlint-init/SKILL.md` were added by `fixing` in response to review low #4, and `docs/guide/configuration.md` + `docs/mdlint_v2/P2-rule-engine/04-config-model-loader.md` by `documentation`. So the plan's scope call was overruled by review — a plan-quality signal, not gold-plating. p9-11-07's `+21/−1` source change against a `+136` diff is the leanest of the batch. p9-11-10's new 99-line `operational-errors.ts` module is justified in the plan (import-cycle avoidance) and consumed by two handlers.

Worth noting the shape: **docs are 42% / 34% / 23% of added lines** across the three diffs (p9-11-04 `+168` docs vs `+97` src). That is the doc-sync policy working as designed, not drift — but it means "diff size" is a poor proxy for change risk in this repo.

**Lever.** `src/wastech_orchestrator/packaged/flows/implementation/planning.md`, `## What To Produce` — add: _"Your test list must cover every call site your file list creates or modifies. If you wire a new guard into N places, name N tests."_ Target copy too.

**Scope.** Orchestrator default.

**Expected impact.** Removes a recurring low-finding class (untested new call site) at zero cost.

---

### F7 — Supervisor follow-ups re-emit review findings that the `documentation` node already closed in the same diff

**Category:** supervisor · **Severity:** low · **Confidence:** high

**EVIDENCE.** `…/p9-11-04-findconfig-boundary/summary.json` `follow_ups` has 7 entries. Items 2 and 3 are the accept-round mediums for `docs/guide/configuration.md` and `docs/mdlint_v2/P2-rule-engine/04-config-model-loader.md`, carried verbatim with `"evidence": ["review evaluator finding (accepted with findings)"]`. Both were **fixed by the `documentation` node in the same commit** — `current.diff` shows `docs/guide/configuration.md` `+12/−2` replacing _"walks up from the target directory to the filesystem root"_ with the correct home-boundary prose, and `04-config-model-loader.md` `+3/−0`. The list is also internally contradictory: item **1** is the documentation node's own note _about editing that same file_, while item **3** says the file was _"skipped"_. Same pattern in p9-11-10: `follow_ups` item 3 (medium, `docs/guide/cli.md` echo claim) was closed by documentation — the final diff reads _"`schema --out` is echoed back exactly as you typed it, because rewriting your own argument inside the error that quotes it back to you is more confusing than printing it."_

Timing confirms the ordering: `evaluations` shows `supervisor_final` at `2026-07-26T02:33:27` for p9-11-04, after `documentation` (node_run 127) finished.

**Root cause.** The finalize turn assembles `follow_ups` from the last review's `findings.json` without re-reading the post-documentation diff, so a finding closed by a later node is still emitted as outstanding.

**Lever.** `src/wastech_orchestrator/packaged/flows/implementation/summary.md` (finalize role file; **note this file is one of only two that changed on Aug-3** — `summary.md` and `supervisor.md` are dated `Aug 3 14:07` vs the target's `Jul 13 18:32`, so the target is running a _materially older_ finalize prompt). Diff them before editing; then add: _"Before emitting a review finding as a follow-up, check the final diff — later nodes (documentation, fixing) may already have closed it. Emit only findings still unaddressed in the committed change, and say which node closed the rest."_

**Scope.** Orchestrator default + target (the target's `summary.md`/`supervisor.md` are 3 weeks behind packaged and should be refreshed regardless).

**Expected impact.** Follow-up lists become actionable instead of ~40% already-done, which is what makes an operator stop reading them.

---

### F8 — Out-of-repo Claude Code memory read makes one run's inputs non-reproducible

**Category:** infra/config · **Severity:** low · **Confidence:** high

**EVIDENCE.** `…/p9-11-07-custom-missing-id/stages/implementation/run-000142/1-claude/events.jsonl` contains a `Read` of `/Users/a1234/.claude/projects/-Users-a1234-Documents-GitHub-wastech-mdlint/memory/p9-remediation-task-pattern.md` — a host path outside the workspace clone and outside the frozen instruction bundle. Only occurrence across the three runs (`grep -ro "\.claude/projects/[^\"]*memory/[a-z0-9-]*\.md"` over all nine agent nodes returns exactly this one hit). This is permitted, not a violation: `security.disable_read_isolation: true` in `.worc/config.yaml`, and the preamble's read prohibitions cover only `.worc/`, `.env`, and provider auth homes (`…/rendered-prompt.md:4-11`). No secret was read or logged.

**Root cause.** With read isolation relaxed, the agent can pull host-side Claude Code memory that `memory.enabled: false` deliberately excludes from the orchestrator's own memory packet — so a run's effective instructions include a file the `instruction-bundles/` manifest does not cover, and a replay on another machine would behave differently.

**Lever.** Either `security.denied_read_paths` in `.worc/config.yaml` (add `~/.claude/**` — validate against `config/schema.py` for glob/home-expansion support), or add `.claude/projects/**/memory/**` to the security preamble's read prohibitions in `src/wastech_orchestrator/core/flow/security_preamble.py`. The preamble route is preferable because it is advisory and provider-neutral and does not weaken the envelope.

**Scope.** Target config for the quick fix; orchestrator default for the preamble line (any repo run with `disable_read_isolation: true` has the same reproducibility hole).

**Expected impact.** Restores "the frozen bundle is the complete input set" as a true statement.

---

## What's already good

1. **The `documentation` node is the batch's best value per dollar.** At `claude-opus-5`/`medium` it cost $0.97 / $0.82 / $1.24 (89–123s) and in all three runs it (a) closed the recurring "incomplete doc-surface sweep" defect class the brief flags as #4 — p9-11-04's `docs/guide/configuration.md` and `docs/mdlint_v2/P2-rule-engine/04-config-model-loader.md` were found by `Grep: walk(s|ing)? up|walk-up|nearest|ancestor|filesystem root|home directory` and neither appeared in the plan's doc list — and (b) **closed accept-round review findings the flow has no other path for**, having read `stages/review/run-<N>/findings.json` from its context footer (verified for p9-11-07 and p9-11-10; p9-11-04's documentation found the same files independently). `medium` is the right effort here. Do not raise it.
2. **The `review` evaluator earns every dollar.** Three rounds, three genuinely load-bearing catches (a silent-data-loss regression, a cross-package contract break, a shipped doc/behavior contradiction), each with a file:line-cited counter-argument. p9-11-04's blocking finding even names the exact reproduction path through `runInitCommand` to the unconditional `writeFile`. The `xhigh`→`high` step at the era boundary did not visibly degrade it.
3. **Plan-to-implementation fidelity is excellent.** The mechanism works: implementation read `plan.md` as tool call #2 in all three runs and executed the declared file list in the declared order. Whatever is wrong with planning is _content_, not _delivery_.
4. **p9-11-10's implementation did real verification.** It built the CLI and ran an ad-hoc repro harness in `$TMPDIR/p1110repro` across 8 Bash invocations (`bogus-command`, `lint ./nope-missing`, `graph ./nope`, `init ./does-not-exist`, `schema --out …`), read `node_modules/commander/lib/command.js:1555-1640,1930-1960,2155-2185` to confirm the dispatch order the plan claimed, and **cleaned up after itself** (`rm -rf "$TMPDIR/p1110repro"` as its second-to-last call). That is the behavior you want and it is not prompted for anywhere — worth promoting into `implementation.md`.
5. **Zero infra noise.** `stage_attempts=1`, `route_fallback='codex'` configured and never used, `provider_attempts.error_class` NULL everywhere, no crashes, no timeouts, 10/10 check commands green in 20–24s. Nothing here is an env problem.
6. **Caching is working as well as it can.** `usage_cache_read / usage_input_total` = 97.3%–98.6% on every node. The remaining spend is structural (turns × context), not a cache miss.
7. **The exchange seals are intact and verifiable.** `.worc/exchange-seals/<task-id>/seal-000001/` carries `manifest.json`, `plan.md`, `task.md`, `current.diff`, and per-node outputs, byte-identical to `logs/`. No `exchange-quarantine/` entries for any of the three runs and `tasks.exchange_contaminated = 0` — no agent tried to write the read-only surface.

## Data gaps

1. **`usage_reasoning_output` is NULL for all 18 attempts** — as the brief notes. I found the cause and it is not a bug: `providers/claude.py:824-843` documents that the Claude CLI _"folds reasoning into output, so `reasoning_output` stays `None`."_ So thinking spend is only measurable indirectly (I used `thinking` block **counts** from `events.jsonl` — 46 vs 17 for p9-11-04 planning vs implementation — since block text is redacted, `chars=0`). Recommendation: nothing to fix in the orchestrator; if thinking spend needs to be tracked, it must come from `output_total` minus rendered-artifact size, which is what I did here.
2. **No A/B on the F1 prompt edits.** The era-A/era-B split gives a natural experiment for _model/effort_ (F2) but every run in both eras used the same planning role prompt, so I cannot measure how much of the rework rate is prompt-attributable vs effort-attributable. The 3/3 "plan authored the defect" result holds across both eras (p9-11-04/07 sonnet-max, p9-11-10 opus-xhigh), which is itself evidence that F1 is prompt-bound — but a single-variable test would confirm it.
3. **`documentation`'s finding-closure rate is undercounted.** Nothing records _which_ accept-round findings a later node closed, so I had to reconstruct it by diffing the accept `findings.json` against the final `current.diff` hunk by hunk. A `evaluations`-side or `summary.json`-side "closed_by" field would make this measurable (and would fix F7 as a side effect).
4. **Per-step supervisor advisories carry no parseable structure.** `stages/supervisor/run-<node>/1-claude/result.json` has `structured_output: null` for every non-terminal step (only `run-000000` finalize carries `{summary, follow_ups}`), so I could not assess whether the advisory layer noticed the planning-cost inversion or the plan-authored defects in real time. If per-step advisories are meant to be actionable, they need a schema.
5. **Clean-CI blindness persists.** `checks.command_sets.default` runs `npm run typecheck|lint|format|test|build` against the existing `node_modules` — no `npm ci`. p9-11-10's implementation ran `npm run build` before `bin.e2e.test.ts` (which spawns compiled output), so the ordering hazard the brief's signal #6 describes is live in this batch too; I saw no failure, but the command_set cannot detect that class. Out of scope for these three runs' findings, noted for completeness.
