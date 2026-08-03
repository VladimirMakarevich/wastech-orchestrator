# Batch C — `p9-11-14-init-cli-lows` ($52.41, the cost outlier) and `p9-12-05-recursion-depth` (the unclosed finding)

**Verdict.** Both runs ended clean (`done`, attempt 1, one review→fixing loop each, no retries/HITL). The $52 on p9-11-14 is not a model, prompt, or infra fault — it is **task granularity**: the task bundles 5 audit findings that the plan expanded into **18 ordered file-level steps**, all executed inside one `implementation` node whose context grew monotonically from 19,898 to 323,982 tokens over 234 turns. Cost across the 20 implementation runs fits `usd = 0.00345 × tool_calls^1.651` (R²=0.963), so cost is **super-linear in turns-per-node**, and the single biggest available lever is splitting the work across node instances (`agents.decomposition.enabled: true`, already wired in the flow) rather than any prompt or model change.

Question B's second half does **not** hold up as a flow defect the way the supervisor framed it: the unclosed `llm.ts` finding is real and shipped, but it is _designed_ behavior (`gate_severity` defaults to `high`, and sub-gate findings are escalated to the operator through the PR body — they were, three times over). The genuine defect next door is smaller and provable: **`documentation` performs review-remediation with nothing downstream to check it**, and in p9-12-05 its "fix" introduced a fresh markdown-source defect that shipped.

---

## Run frames

### `p9-11-14-init-cli-lows` — "P11.14 init-scan honesty and CLI-plumbing micro-fixes"

|  |  |
| --- | --- |
| status / attempt / loops | `done` / 1 / `fix_iterations=1` (one `review→fixing`), `test_fix_cycles=0`, `review_fix_cycles=0` |
| wall clock | 2026-07-28T02:37:43 → 03:57:33 UTC = **4,790 s** |
| cost | **$52.41**, 68.40M input, 295,405 output, 15 provider attempts, 0 retries, 0 fallbacks |
| validation | `passed: true`, `completeness: "complete"` — spec was not the problem |
| diff | **30 files, +2071 / −422** (12 code, 6 test, 11 doc, 1 new module `gitignore-layers.ts`) |
| review findings | **14** (8 on the `rework` verdict: 1 high / 3 medium / 4 low; 6 on the `accept`: 1 medium / 5 low) |

Path (from `node_runs` 189–197):

| node | secs | USD | input | turns | notes |
| --- | --: | --: | --: | --: | --- |
| planning | 658 | 5.65 | 5.44M | 66 |  |
| **implementation** | **1957** | **27.47** | **42.87M** | **234** | 52.4% of run cost |
| testing (checks) | 24 | — | — | — | 5/5 pass |
| review → rework | 551 | 4.81 | 4.80M |  | 8 findings |
| fixing | 847 | 8.23 | 10.62M | 99 |  |
| testing (checks) | 25 | — | — | — | 5/5 pass |
| review → accept | 399 | 3.46 | 2.83M |  | 6 findings, unfixed |
| documentation | 178 | 1.60 | 1.45M | 26 | 6 files edited |
| publish | 51 | — | — | — | PR opened |
| supervisor (9 turns) | ~129 | 1.18 | 0.37M |  | **2.3% of run cost** |

### `p9-12-05-recursion-depth` — "P12.05 document or guard recursive DFS traversal depth"

|  |  |
| --- | --- |
| status / attempt / loops | `done` / 1 / `fix_iterations=1` (one `review→fixing`) |
| wall clock | 09:18:13 → 09:48:30 UTC = **1,817 s** |
| cost | **$10.07**, 7.59M input, 98,430 output |
| diff | **10 files, +187 / −13** — doc/test-only by design; product code = comments only |
| review findings | 3 on `rework` (1 high / 2 low), **2 on `accept` (1 medium / 1 low)** |

Path: planning 396s/$2.60 → implementation 585s/$2.56/49 turns → testing pass → review `rework` 195s/$1.08 → fixing 317s/$1.87/? → testing pass → review `accept` 94s/$0.70 → documentation 50s/$0.52 (1 file) → publish. Supervisor $0.74 = 7.3%.

---

# Findings, ranked by impact

## F1 — Task granularity, not model or prompt, is the cost driver: node cost ≈ `turns^1.65`

**Category** spec / config (decomposition) · **Severity** high · **Confidence** high

### Evidence

**(a) The task is a basket of N deliverables.** `/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/logs/p9-11-14-init-cli-lows/task.normalized.json` — the description lists findings **L-7 … L-11**, and L-11 is itself a basket:

> `- **L-11 — micro-fixes.** \`schema --out <relative>\` resolves from \`process.cwd()\` not the io-seam \`cwd\` … \`pnpm-workspace.yaml\` is truncated at the first blank line …; \`detectPackageManager\` looks only at the root …; \`readExistingRuleIds\` … has no production caller yet 11 tests over 127 lines; two CI-workflow decline paths return \`undefined\` silently …; no top-level rejection handler in the bin`

That is **5 findings ≈ 10 discrete deliverables**, and the acceptance criteria has 6 independent boxes. Planning expanded it into an explicit ordered work list — `.worc/logs/p9-11-14-init-cli-lows/plan.md` §"Files, in order" contains **18 numbered steps** across 18 distinct files:

```
1. packages/core/src/discovery/gitignore-layers.ts     10. packages/cli/src/commands.ts
2. packages/core/src/markdown/load-documents.ts        11. packages/cli/src/program.ts
3. packages/core/src/discovery/repo-scan-constants.ts  12. packages/cli/src/index.ts
4. packages/core/src/discovery/repo-scan.ts            13. docs/…/14-init-cli-lows.md
5. packages/core/src/discovery/workspace-packages.ts   14. docs/mdlint_v2/glossary.md
6. packages/core/src/discovery/package-manager.ts      15. docs/guide/cli.md
7. packages/core/src/discovery/config-writer.ts        16. docs/guide/configuration.md
8. packages/core/src/index.ts                          17. README.md
9. packages/cli/src/init-command.ts                    18. docs/…/01-repo-scan-detection.md
```

By contrast `p9-12-05`'s plan has **0** numbered file steps — it is one coherent deliverable. The diff confirms the hypothesis: p9-11-14's 30 files are **loosely related** — they share only the `init`/config-writer surface. `load-documents.ts` (−52 lines, a pure module move for L-7), `commands.ts` (+8, the `schema --out` cwd fix), `program.ts` (+1), `index.ts` (+16, the bin rejection handler), `workspace-packages.ts` (+22, the pnpm-yaml parse) have **no logical dependency on each other** — they were batched because they were all filed as `low`.

**(b) What burned 42.9M input.** `.worc/logs/p9-11-14-init-cli-lows/stages/implementation/run-000190/1-claude/events.jsonl` (6.2 MB, 845 lines):

- **233 unique tool calls**: `Edit` 112, `Bash` 86, `Read` 33, `Write` 2. `result.json` reports `num_turns = 234`.
- Context grew **monotonically, never compacted**. Per-assistant-message prompt size (`input_tokens + cache_read + cache_creation`), by decile:

| decile        |     0% |     20% |     40% |     60% |     80% |        100% |
| ------------- | -----: | ------: | ------: | ------: | ------: | ----------: |
| prompt tokens | 19,898 | 112,453 | 152,023 | 212,423 | 263,009 | **323,982** |

- The arithmetic closes exactly: `42,872,645 / 233 = 183,999` — **233 billed turns × ~184K average context**. There is no anomaly to find; the cost _is_ turns × context.
- `result.json` cost decomposition against confirmed Opus 5 pricing (input $5, output $25, 1h-cache write $10, cache read $0.50 per MTok — verified via the `claude-api` skill):

| component | tokens | USD | share |
| --- | --: | --: | --: |
| cache read | 42,548,221 | **21.27** | 77.4% |
| 1h cache write | 323,980 | 3.24 | 11.8% |
| output | 117,995 | 2.95 | 10.7% |
| uncached input | 444 | 0.00 | — |
| **total** |  | **27.47** | (matches `normalized_usage.cost = 27.468216`) |

So **89% of the spend is re-reading accumulated context**, and only 11% is generation. `duration_api_ms = 1,766,407` of `duration_ms = 1,950,598` → **90.6% of the 1957 s was model inference**, not tool execution (tools ≈ 184 s total). Slow tests are not the problem; a large context re-read 233 times is.

**(c) p9-11-14 is a double outlier, not a shape change.** Tool-call count and peak context per implementation node, all 20 runs:

| run | tools | ctx_max | ctx_mean | node USD |
| --- | --: | --: | --: | --: |
| **p9-11-14-init-cli-lows** | **233** | **323,982** | **193,120** | **27.47** |
| p9-11-10-cli-exit-contract | 125 | 181,088 | 108,958 | 9.68 |
| p9-11-09-atomic-writes | 117 | 198,821 | 134,150 | 10.57 |
| p9-11-13-grp-size-hygiene | 116 | 150,427 | 100,120 | 7.77 |
| … median (≈p9-12-03) | 64 | 102,005 | 67,144 | 3.40 |
| p9-11-05-table-primitive-scope | 25 | 65,066 | 52,069 | 0.88 |

Log-log fit over all 20 implementation node runs (tool calls vs node USD):

```
usd = 0.00345 × tools^1.651        R² = 0.963   (n = 20)
predicted at 233 tools: $28.00     (actual $27.47)
predicted at  47 tools: $1.99
```

**Cost is super-linear in turns _within one node instance_, because context never shrinks.** Five 47-turn node instances cost `5 × $1.99 = $9.95` for the same 235 turns of work — **2.8× cheaper than one 233-turn node ($28.00)**.

**(d) Neither limit was near.** `request.json` argv ends `--max-turns 400`; `num_turns = 234` = **58.5%** of budget (and `max_turns_gate: false` in config, so exceeding it would not have failed the task anyway — see `core/flow/nodes/agent.py:304`). `timeout_seconds: 7200` vs `duration_ms = 1,950,598` = **27.1%** of budget. **Answer to A3: no, not remotely at risk** — this run could have gone 3.7× longer before timing out.

### Root cause

The flow has no mechanism that bounds turns-per-node-instance, and `agents.decomposition.enabled: false`. A task that bundles N independent deliverables therefore executes as one N-deliverable node whose context accumulates all N sets of file reads, edits and check output — and pays for the whole accumulation on every subsequent turn.

### Precise lever (ranked)

1. **`agents.decomposition.enabled: true`** in `/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/config.yaml:27-29` (currently `enabled: false`, `max_subtasks: 8`). This is a **one-key change with the wiring already in place**: `.worc/flows/implementation.yaml` already declares

   ```yaml
   decomposition:
     proposed_by: planning
     sub_flow: [implementation, testing, review, fixing]
     shared_budget: global_fix_iterations
   ```

   and every sub-flow node is `session_scope: fresh_disposable`, so each subtask's implementation **starts from a small context**. Planning runs once (so the $5.65 is not multiplied), and only impl/testing/review/fixing repeat.
   - **Expected impact on this run**: implementation ~$27.5 → ~$10–11.5 (fit-based, 4–5 subtasks). Against that, `review` runs per subtask instead of twice on the whole diff — reviews here cost $4.81 + $3.46 = $8.27 on a 2493-line diff, and per-subtask reviews see ~1/5 the diff each, so the review line is roughly flat. **Net estimate: −$15 to −$17 on this run (−30%).**
   - **Caveat, stated honestly**: this is an A/B, not a certainty. `max_subtasks: 8` bounds the blast radius, and only 3 of 20 runs (p9-11-14, p9-11-10, p9-11-09) are in the range where it pays. Recommend enabling and measuring on the next multi-deliverable task, not blanket-on.
   - **Scope**: target-only (`.worc/config.yaml`). The packaged default in `config/loader.py` / `packaged/config.example.yaml` should stay `false` — decomposition is a per-repo throughput choice.

2. **A task-authoring rule** — the cheapest and most durable fix. Nothing in the target's task guidance discourages "batch all the LOWs into one task". A one-line rule in the phase-authoring docs ("a task whose plan will produce more than ~8 ordered file steps should be authored as N tasks, or run with decomposition enabled") prevents the class. Scope: target repo docs; **not** an orchestrator change.

3. **Do _not_ downgrade the model or reasoning for this.** Output was only 118K tokens ($2.95, 10.7%); a reasoning downgrade attacks the 11% and risks more rework loops on the 14-finding node. There is no evidence of over-powering: the node produced a 2493-line change that passed 5/5 checks twice and drew a `high` finding only once.

---

## F2 — Every writer node re-runs the whole `testing` command set 11–35× _inside_ the node; the role prompts instruct it

**Category** prompt · **Severity** medium-high · **Confidence** high

### Evidence

Check-suite invocations counted from each node's own `Bash` tool calls:

| run / node | bash calls | gate invocations | breakdown |
| --- | --: | --: | --- |
| p9-11-14 / implementation | 86 | **35** | vitest 13, `npm test` 6, build 5, format 5, typecheck 3, lint 3 |
| p9-11-14 / fixing | 28 | **20** | build 4, format 4, vitest 3, typecheck 3, lint 3, `npm test` 3 |
| p9-12-05 / implementation | 21 | **15** | vitest 4, build 3, `npm test` 2, typecheck 2, lint 2, format 2 |
| p9-12-05 / fixing | 8 | **11** | format 3, typecheck 2, lint 2, `npm test` 2, build 2 |

Sixteen of p9-11-14's implementation Bash calls were backgrounded (`system`/`task_started`, `task_type: "local_bash"`) and **every one is a gate run** — e.g.

```
"description": "npm run typecheck 2>&1 | tail -5 && npm run build 2>&1 | tail -3 && npm test 2>&1 | tail -8 && npm run lint 2>&1 | tail -3 && npm run format 2>&1 | tail -3"
"description": "npx vitest run --project cli init.e2e 2>&1 | grep -E \"^ FAIL|→ |Tests |AssertionError\" | head -40"   ← 5 near-identical repeats
```

The `testing` node then ran **the same five commands** in 24 s and 25 s (`check_runs`: `npm run typecheck`, `npm run lint`, `npm run format`, `npm test`, `npm run build`; all `passed`, `timed_out=0`).

The prompt asks for it. `.worc/logs/p9-11-14-init-cli-lows/prompt-audit/000190-implementation.json` → `prompt`:

````
## Verify

Before finishing, run the checks that apply to the touched scope and confirm they pass:

```bash
npm run typecheck
npm test
npm run build
````

Use `npm run lint` and `npm run format` when the touched scope requires style verification.

```

And `fixing.md` is explicitly iterative — `/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/fixing.md:17-28`:

```

## Quality Gate

The project's gate is:

```bash
npm run typecheck
npm run lint
npm test
npm run build
```

Work one failure at a time: reproduce it with the matching command, fix it minimally, then re-run that same command to confirm it passes before moving on.

````

**Packaged-default check (Jul-25 target copy vs packaged).** Correction to brief item #9: only **`summary.md` and `supervisor.md`** actually drifted orchestrator-side (`git log -1`: commit `dd51d39` "feat: Introduce SupervisorPacket…", 2026-08-03). `implementation.md`, `fixing.md`, `planning.md`, `review.md`, `documentation.md` are all still at commit `61ef90f` (2026-07-25) — the target's copies differ because the **operator hand-customized them** (added project-specific "TypeScript Style" / "Hard Invariants" sections and the hardcoded command block), not because packaging moved.

**The packaged default does not fix this — it arguably strengthens it.** `packaged/flows/implementation/implementation.md` replaces the command block with:

> `Before finishing, run whatever check commands this project defines for the code you touched (build, type-check, lint, test) and confirm they pass — **catching a failure now saves a full review/fix round trip later**.`

and `packaged/flows/implementation/fixing.md` keeps the loop verbatim: *"Work one failure at a time: reproduce it with the project's own check command for that failure … then re-run that same command to confirm it passes before moving on."* Neither version carries any **frequency** or **scope** discipline, and neither is aware that a `checks` node runs 24 s downstream.

### Root cause

Self-verification is the right instinct (it is what keeps `test_fix_cycles` at 0 across all 20 runs) but it is unbounded. Each gate run's output is permanently appended to a context that is re-read on every subsequent turn, so the marginal cost of run #30 is ~184K tokens of cache-read, not the 24 s of CPU.

### Precise lever

`src/wastech_orchestrator/packaged/flows/implementation/implementation.md` §Verify and `…/fixing.md` §Quality Gate (**orchestrator default — affects every repo**), plus the target's hand-customized copies at `/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/{implementation,fixing}.md`. Add frequency/scope discipline, e.g.:

> Run the narrowest command that covers what you changed while you iterate (a single test file, not the suite). Run the project's full gate **once**, at the end, after all edits are in place — a `checks` stage re-runs it immediately after you finish, so repeating the full suite mid-work buys nothing and every run's output stays in your context for the rest of the node.

Do **not** forbid self-verification: the pre-emptive gate runs are why `testing` never failed. The change is "narrow while iterating, full once at the end", not "stop checking".

**Expected impact**: removes ~20 of 35 gate runs from a large implementation node. Since each avoided round-trip also removes ~2 turns and their permanent context contribution, the effect compounds against the `turns^1.65` curve — order **10–20% off implementation/fixing** on turn-heavy runs, ~0 on small ones. Lower confidence on the magnitude than F1 (the gate runs are only part of what fills the context; 112 `Edit` calls across 30 files and 33 `Read`s are the rest).

**Good discipline already present, worth keeping**: the agent self-limits gate output — every gate command pipes through `| tail -80`, `| grep -E …`, `| head -40`. Without that the 42.9M would have been far worse.

---

## F3 — `documentation` remediates review findings with no gate and no reviewer downstream, and shipped a fresh defect doing it

**Category** flow · **Severity** medium · **Confidence** high

### Evidence — the flow shape (confirmed)

`.worc/flows/implementation.yaml` edges:

```yaml
- { from: review, to: documentation, outcome: accept } # accepted code -> docs update -> publish
- { from: documentation, to: publish }
````

There is **no `checks` node and no evaluator between `documentation` and `publish`**. Documentation is `permission_profile: workspace-write`, `model: claude-opus-5`, `reasoning: medium`, and — critically — it **receives the accept-verdict findings as a context file**. From `prompt-audit/000231-documentation.json`:

```
Context files (read them as needed; do not assume their contents):
- task:   …/.worc-io/p9-12-05-recursion-depth/task.md
- plan:   …/plan.md
- diff:   …/current.diff
- review: …/stages/review/run-000230/findings.json      ← the accept verdict's findings
```

(written by `core/flow/nodes/evaluator.py:259` → `self._in.review_path = publish_node_run_file(…)`).

Yet the same prompt forbids acting on any finding that lives in code — `.worc/flows/implementation/documentation.md` (identical in the packaged default on this point):

> **Stay strictly within documentation.** You are read-only to the code and to the build state — your only job is editing docs files … The docs formatter (`npm run format`) is the one sanctioned command.

**That contradiction is exactly what the supervisor observed.** Verified verbatim from `evaluations` id 262 (`supervisor_step`, node `documentation`):

> "this step explicitly declines to address a review low finding in `packages/core/src/engine/rules/llm.ts` … That means **there is still an outstanding, acknowledged review finding with no code step yet to close it.**"

> "this step did not run `typecheck`/`test`/`build` itself and says so explicitly … Since a file was edited here after that gate run, it would be worth one more gate pass"

### Evidence — B1: the `llm.ts` finding was never addressed

`evaluations` id 260 (`verdict = accept`) carries a `low`:

> `severity: "low"`, `paths: ["packages/core/src/engine/rules/llm.ts"]` — _"In the new `traverse` why-comment, \"Both are single digits in practice\" has no clear antecedent after the rewording…"_

The **final committed** `current.diff` still contains the flagged text unchanged:

```diff
+// `visit` recurses once per hop along the current DFS path … not by any single
+// authored chain, since `visited` is never unwound …
+// Both are single digits in practice: `@path` imports are hand-authored, not a corpus-wide link
+// graph. …
```

So yes — **the finding shipped**. But it did **not** vanish: `pr_body_appended.md` carries it three separate times (lines 371, 377, 380), including as a supervisor follow-up with an action hint:

> `- **[low] Ambiguous antecedent left unfixed in llm.ts why-comment** — … The documentation-fix step explicitly declined to touch it because it's a product-code file … Suggested: Add a small follow-up commit clarifying the comment wording in llm.ts; low risk, single-line fix.`

**This is designed behavior, not a bug.** `gate_severity` defaults to `high` (`core/flow/schema.py:126,255`), and `implementation.yaml`'s `review` node does not override it — so `medium` and `low` never gate (`core/flow/nodes/evaluator.py:290`: `if not any(self._is_blocking(f, gate_rank) …): return "accept", False`). The packaged guide states the intent explicitly (`packaged/guide/flows/roles.md:25`): _"A finding below the gate is not discarded: it is carried to the operator in the run summary and the pull-request body, so 'file it anyway' is the right instruction."_ The escalation path worked. **The supervisor's alarm is factually correct but describes the contract, not a violation** — worth telling the operator plainly rather than treating as a defect.

### Evidence — B2: how often `documentation` changes files

`node_runs.commit_sha_before` / `commit_sha_after` **cannot answer this** — see the data gap below; both are NULL for every non-publish node run DB-wide. Reconstructed from `Edit`/`Write` tool calls in each `documentation` node's `events.jsonl` instead:

- **19 of 20 runs edited files** (only `p9-12-02-glossary-custom-target` made 0 edits, having verified and re-touched nothing).
- **84 `Edit` calls, 0 `Write` calls, 1–10 files per run.**
- **All 84 targets are `.md`.** No `documentation` node in any of the 20 runs touched a `.ts` file. The read-only-to-code instruction held **20/20**.
- **20/20 runs voluntarily ran a format gate** — `npm run format` (17 runs) or `npx prettier --check <files>` (3 runs). **0/20 ran `typecheck`, `test`, or `build`.**

So the "committed unverified" risk is narrower than it first looks: the only gate a `.md` edit can break is `format`, and the agent ran it every single time. The compliance came from the prompt, not the flow — there is no mechanical enforcement.

### Evidence — the real harm: documentation's unreviewed remediation

The accept verdict (`evaluations` 260) also raised a **medium** on `docs/guide/context-graph.md` — a sentence that "has no main clause … does not parse". `documentation` **did** fix that one (its single `Edit` in p9-12-05 targets exactly that file). Nobody reviewed the fix. Here is what shipped, from the final `current.diff`:

```diff
+- Cycle detection walks the graph recursively, so its depth is the longest simple path the traversal
+  takes inside one **connected component**. In a densely cross-linked component that depth
+  approaches the component's document count, with no long authored chain involved — so the
+  assumption is that no single connected component runs to many thousands of documents. Many small
+  components are fine
+  however large the corpus is — the traversal restarts, and the stack unwinds, at each one.
```

Two problems, both shipped:

1. **A new hard line break mid-sentence** — `"Many small components are fine"` / newline / `"however large the corpus is"`. This is precisely **recurring defect class A** from the brief, and Prettier's `proseWrap` will not repair it, so the format gate stayed green.
2. The `"In X …, so Y"` structure the reviewer flagged is **still there** — the fix inserted "that depth" as a subject but left the same non-parsing construction.

The same pattern shows on p9-11-14: of the 6 accept-verdict findings, `documentation` closed exactly the one that lived in a doc (`docs/mdlint_v2/P6-init/03-interactive-prompts.md`, finding #3) and left 5 — including the **medium** _"The draft the user confirms never mentions the second file `init` now writes"_ in `init-command.ts`.

### Root cause

`documentation` is handed the review findings but is scoped to docs only, so it partially closes them; and being the terminal agent node with `documentation → publish`, its own output is the one part of the diff that no gate and no evaluator ever sees.

### Precise lever — recommendation

**Do not add `documentation → testing`.** Cost/benefit does not support it: 20/20 runs already ran the only gate a `.md` edit can break, and a `testing` node costs 20–27 s plus a `node_runs` row for a class of failure that occurred 0/20 times. It also would not have caught either p9-12-05 defect — both are prose, invisible to `prettier --check`.

Ranked instead:

1. **Stop handing `documentation` a findings file it is forbidden to act on** (`core/flow/nodes/evaluator.py` / the input wiring that sets `review_path`) — _or_, cheaper and prompt-only, make the contradiction explicit in `packaged/flows/implementation/documentation.md`: _"The review findings are provided for context. Fix only findings whose `paths` are documentation files; leave code-file findings alone — they are escalated to the operator as follow-ups, and silently 'partially closing' a finding set is worse than leaving it whole."_ Scope: **orchestrator default**. This turns today's implicit, per-run coin-flip into a stated contract.
2. **Add a `when`-gated re-review, not a re-check** — a non-blocking evaluator after `documentation` (`blocking: false`, `max_rework_per_stage: 1`, `gate_severity: medium`) would have caught both p9-12-05 prose defects, which a `checks` node cannot. Cost: ~$0.5–1 per run (documentation's diff is small). This is the only lever that addresses the actual observed harm. **Scope: flow-graph choice — recommend adding to the target's `.worc/flows/implementation.yaml`, not the packaged default** (the packaged flow's own header already frames an extra evaluator as "a pure graph-shape choice — an operator who wants it adds the node to their own flow YAML").
3. **Documentation read-only is wrong** — 19/20 runs made a needed doc change; the node earns its keep.
4. **If the operator wants `medium` findings to actually gate**, the lever is one line in `.worc/flows/implementation.yaml` under the `review` node: `gate_severity: medium` (built-in default `high`). Over these 20 runs that would have converted 18 medium findings from PR-body follow-ups into rework loops. At the observed `fixing` cost ($5.31 avg) that is roughly **+$50–95 across 20 runs** — a real trade, and the operator's call, not a defect to fix. Note the coupling documented in `packaged/guide/flows/reference.md:144`: `review` is `blocking: true`, so lowering the gate means the `review_fix: 15` budget matters more.

---

## F4 — Open findings reach the operator duplicated, with contradictory severities and truncated raw titles

**Category** prompt/code (supervisor) · **Severity** medium · **Confidence** high

### Evidence

`p9-11-14`'s `pr_body_appended.md` §"Technical debt / follow-ups" (lines 340–351) has **10 bullets for ~6 distinct issues**. Three pairs are the same finding twice, at conflicting severities:

| supervisor-authored bullet | evaluator-raw bullet |
| --- | --- |
| `[low] init's confirmation draft doesn't name the project-local schema.json it will write under npx` | `[medium] The draft the user confirms never mentions the second file \`init\` now writes. \`formatDraftSummary\` prints only \`Existing…` |
| `[medium] resolveSchemaWriteOutcome has a review-flagged unreachable overwritten branch` | `[low] \`resolveSchemaWriteOutcome\`'s \`if (existingConfigAction === "overwrite" && reason === "custom-rules")\` branch is now unr…` |
| `[low] gitignore-layers.ts header comment reportedly overclaims shared use` | `[low] The module header claims "the pre-config repo scan (P11.14 / audit L-7) must skip exactly the trees the lint corpus will…` |

Note the first two pairs **invert** severity between the two renderings of the same issue.

Root cause is in `src/wastech_orchestrator/core/supervisor.py:481-503`. The dedup key is exact normalized text:

```python
def _follow_up_key(follow_up: FollowUp) -> tuple[str, tuple[str, ...]]:
    """Exact-match dedup key for a follow-up: its normalized text plus its paths."""
    text = " ".join(f"{follow_up.title} {follow_up.rationale}".lower().split())
    return (text, tuple(sorted(follow_up.paths)))
```

`_merge_follow_ups(primary, extra)` then appends any evaluator finding whose key is not already in the supervisor's list. Because the supervisor is asked to write its own follow-ups _in its own words_, its paraphrase can never exact-match the evaluator's raw `reason` — so **the dominant case is structurally undedupable**. And the truncated titles come from `_finding_to_follow_up` (line 386-389): reasons over `_FINDING_TITLE_MAX = 120` become `reason[:120] + "…"` as the bold title, so the operator's headline is a mid-sentence fragment of raw review prose.

**Latent severity bug, same function, line 393:**

```python
severity=severity if severity in ("low", "medium", "high") else "medium",
```

The evaluator output schema's enum is `["blocking", "critical", "high", "medium", "low"]` (`stages/review/run-000230/1-claude/output-schema.json`), so a `blocking` or `critical` finding is **silently relabelled `medium`** in the PR body. Currently unreachable for `implementation.yaml` (its `review` is `blocking: true` at `gate_severity: high`, so those severities gate → rework, and exhaustion → manual). It **is** reachable for the packaged flows with non-blocking evaluators — `deep_research.yaml:136,228,246` (`blocking: false`) and `security_audit.yaml:82,87` — where `rework_exhausted` reaches `accept` with findings still open.

### Precise lever

`src/wastech_orchestrator/core/supervisor.py` (**orchestrator default**):

- `_follow_up_key` — add path-overlap dedup as a second pass: when an evaluator finding's `paths` set is already covered by a supervisor follow-up's `paths`, drop the evaluator copy (or attach it as evidence under the supervisor's bullet). Keep exact-match as the fast path.
- `_finding_to_follow_up:393` — map `blocking`/`critical` to `"high"` rather than `"medium"`, or widen `FollowUp.severity` to the full five-value enum.
- `_finding_to_follow_up:386-389` — a truncated raw `reason` is a poor title; prefer the finding's `path` + a short generated label, keeping the full reason in `rationale`.

**Expected impact**: the operator-facing escalation list — the only surface that closes the 62 unfixed findings the brief counted — becomes ~40% shorter and internally consistent. Low risk: advisory layer only, no routing effect.

---

## F5 — Planning cost is disproportionate to its output on a small task

**Category** reasoning · **Severity** low-medium · **Confidence** medium

### Evidence

`p9-12-05`: planning **396 s / $2.60 / 2.19M input** vs implementation **585 s / $2.56 / 2.32M**. Planning cost slightly _more_ than the implementation it planned, for a task whose plan has **0 numbered file steps** and whose final diff is **+187/−13 across 10 files**, product-code changes being _comments only_. Same picture on p9-11-14 at a larger scale ($5.65 planning) — but there the 18-step plan visibly earned it: the supervisor's own note on the plan (`evaluations` id 253) credits it with a pre-committed escalation gate and a correctly-declined Tarjan rewrite.

Both `planning` nodes ran `claude-opus-5` / **`reasoning: xhigh`** (`.worc/flows/implementation.yaml`, `prompt-audit/000224-planning.json`).

### Precise lever

This is a genuine judgment call and I am flagging it, not asserting it. `xhigh` on planning is _earning its cost on the hard tasks_ (p9-11-14's plan is the reason a 30-file change passed checks first try). On doc/test-only tasks it is over-powered. The clean lever is **per-task**, not global: `nodes.planning.reasoning: high` in the task file for scoped doc-only tasks, or `nodes.planning.enabled: false` where the task file already _is_ the plan. Do **not** lower `planning.reasoning` globally in `.worc/flows/implementation.yaml` — the evidence points the other way on the tasks that matter. Scope: target, per-task. Expected impact ~$1–1.5 per small task; ~$10 across a P12-sized batch.

---

# What's already good

- **Prompt caching is working essentially perfectly.** 42,548,221 of 42,872,645 implementation input tokens (**99.24%**) were cache reads at $0.50/MTok, with a single 323,980-token 1h-cache write. Without it, p9-11-14's implementation node would have cost ~$217 instead of $27.47. Nothing to fix here.
- **Self-verification prevents test_fix loops entirely.** `test_fix_cycles = 0` on both runs and on all 20; `check_runs` shows 0 timeouts and 5/5 passing every time. The gate-run frequency is the problem, not the practice — the recommendation in F2 deliberately preserves it.
- **The `review` evaluator is doing real work.** p9-11-14's `high` finding is a genuine, subtle regression the checks could never catch: _"L-10 makes a previously-unreachable destructive branch live: `resolveSchemaWriteOutcome` returns `{ shouldWrite: true, kind: "overwritten" }` whenever `existingConfigAction === "overwrite"` and an existing `schema.json` differs. Before this change that branch could never fire in production."_ p9-12-05's `high` is equally sharp — it caught that the shipped docs claimed the recursion bound was "one unbroken chain" when the real bound is **component size regardless of shape**, i.e. a docs-only task shipping a _wrong safety claim_. One review→fixing loop each, resolved substantively on the first attempt.
- **`fixing` fixed the class, not just the instance, and disclosed its deviation.** Supervisor note on p9-12-05's fixing step: it _"didn't stop at the five reviewer-cited locations — it proactively found and corrected the same 'chain-shaped' framing defect in `cyclePath`'s `walk` comment, the `llm.ts` import-visit comment, and the LLM-001 doc note"_, and _"rather than silently complying with a suggested wording … that the implementer determined was itself inaccurate, it explained why and wrote the correct version instead."_ That is exactly what `fixing.md` §"Fix The Finding, Then Its Class" asks for — the prompt is working.
- **Both runs passed validation `complete`** with no refinement needed — the task specs going in were sound. p9-11-14's problem is the spec's _granularity_, not its quality.
- **Documentation honored its read-only-to-code boundary 20/20** and ran the format gate 20/20 without being mechanically forced to. Voluntary compliance from a prompt, sustained across 20 runs, is a good signal.
- **`security` envelope held.** No `permission_denials` (`result.json: permission_denials: []`), and the `--disallowedTools` list in `request.json` correctly denies `Bash(git commit:*)`, `Bash(git push:*)`, `Bash(gh pr create:*)`, `Read(.env)`, and all writes under `.worc/`, `.worc-io/`, `.git/`, `tasks/` — even with `disable_read_isolation: true`. Session ids are `[REDACTED]` in `result.json`. No secret leakage found in any artifact I read.
- **Cost accounting is trustworthy.** Every attempt reports `usage_delta_status = ok`, `usage_scope = per_invocation`, and `normalized_usage.cost` reproduces exactly from token counts × published Opus 5 rates. The token-optimization campaign can trust its own instrumentation.

---

# Data gaps

1. **`node_runs.commit_sha_before` and `commit_sha_after` are NULL for every non-`publish` node run in the entire database.** Confirmed:
   ```sql
   SELECT node_id, COUNT(*) n, SUM(commit_sha_before IS NULL) before_null,
          SUM(commit_sha_after IS NULL) after_null FROM node_runs GROUP BY node_id;
   -- documentation 31/31/31 · implementation 34/34/34 · fixing 11/11/11
   -- review 43/43/43 · planning 36/36/36 · testing 43/43/43
   -- publish 32 · 32 before_null · 0 after_null
   ```
   `grep -rn "commit_sha_before\|commit_sha_after" src/` shows the columns are declared (`state_store.py:291-292`), read back (`:1710-1711`), and writable (`:1039,1048`) — but the **only writer anywhere is `core/flow/nodes/publish.py:109`**, and it stores the PR URL, not a SHA (`# commit_sha_after is the node's result reference; for a publish node that is the PR…`). **`commit_sha_before` is never written by anything.** Consequence: there is no per-node commit attribution in the audit trail, so "which node changed which files" must be reconstructed from `events.jsonl` tool calls — which is provider-shaped, brittle, and impossible for a provider that doesn't emit them. Recommend a small change in `core/flow/nodes/agent.py` to stamp `HEAD` before/after each agent node; that alone would make Question B2 a one-line SQL query and would let the diff be attributed per node.
2. **`usage_reasoning_output` is NULL for all 30 attempts across both runs** (already noted in the brief; not re-investigated).
3. **`runs/exchange-seals/` is absent for both tasks** — expected, not a gap: `logging.clean_runs_on_success: true` evicts the subtree at a `done` terminal. To analyze the exact agent-facing exchange on a future run, the operator must set `logging.clean_runs_on_success: false` beforehand.
4. **`p9-11-14` has no `<NNNNNN>-planning.json`** structured artifact in `logs/<task-id>/` — only `plan.md` and `skill_map.json` (3 bytes, `{}`). I could not confirm from a structured artifact whether `planning` _proposed_ subtasks; with `decomposition.enabled: false` the proposal path is inert, so this is a moot point for these runs but would matter for measuring the F1 recommendation.
5. **Not assessable: whether decomposition would actually reduce total cost.** The F1 estimate is a log-log extrapolation from single-node behavior; it cannot predict how per-subtask `review` and `planning`-proposal overhead composes. It needs one A/B run to settle. I have deliberately framed F1's number as an estimate with the counter-cost stated rather than a promise.
