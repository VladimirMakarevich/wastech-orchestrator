# Batch G — p9-12-04, p9-12-06, and a cross-cutting SUPERVISOR-layer audit

Target: `/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/` · Levers: `/Users/a1234/Documents/GitHub/wastech-orchestrator/` Runs analyzed in Half 1: `p9-12-04-mcp-custom-rules` ($17.04), `p9-12-06-process-boundary-tests` ($14.84). Half 2 covers all 20.

## Verdict

Both assigned runs were mechanically clean (`done`, attempt 1, no retries, no fallbacks) and both diffs are tightly scoped to intent. But **the final task of the whole 20-run range shipped the single word `test` as its PR body** — the supervisor's finalize turn wrote a full 9,300-char summary three times, had it rejected three times by the output-schema validator over a missing `follow_ups` key, then emitted a minimal probe that validated and was published. Nothing in the orchestrator noticed.

On the supervisor layer: it is **not** free ($19.14, 147 invocations, 8.26 M input tokens — 5.9 % of the $325.16 total, and it _is_ recorded in `provider_attempts`, contrary to the brief), it is **not** consumed by anything except itself, and **39 % of its invocations observed a prompt containing nothing but a node name and an outcome word**. Its prose, where it had material, is genuinely high-signal and one sampled claim fact-checks as correct — but it was writing every PR body without ever being handed the diff.

---

## Corrections to the shared brief (verify before reusing)

| Brief claim | Actual | Evidence |
| --- | --- | --- |
| "145 supervisor_step rows and 20 supervisor_final" | **127** + 20 = **147** | `SELECT kind,COUNT(*) FROM evaluations WHERE task_id GLOB 'p9-1[12]*' AND kind LIKE 'supervisor%' GROUP BY kind;` → `supervisor_final\|20`, `supervisor_step\|127` |
| "165 supervisor invocations do NOT appear in `provider_attempts` (which totals $325.16 across 247 attempts, all flow nodes)" | **147 of the 247 attempts ARE the supervisor.** $325.16 = **$306.02 flow nodes (100 attempts) + $19.14 supervisor (147 attempts)**. Supervisor rows carry `node_run_id IS NULL` by design | `SELECT COUNT(*) FROM provider_attempts WHERE node_run_id IS NULL;` → `222` (all tasks); `SELECT nr.node_id,COUNT(*),ROUND(SUM(pa.usage_cost),2) FROM provider_attempts pa LEFT JOIN node_runs nr ON pa.node_run_id=nr.id WHERE pa.task_id GLOB 'p9-1[12]*' GROUP BY nr.node_id;` → NULL row = `147\|19.14` |
| "zero HITL" across all 20 | **2 HITL approvals** — `p9-12-04` and `p9-12-01`, both at `planning`, both `approved`. This is the entire explanation of the "double planning" | `logs/p9-12-04-mcp-custom-rules/hitl/planning.json` → `"kind": "approval"`, `"answer": "approved"`, `"status": "consumed"`; same file exists under `logs/p9-12-01-exclude-coverage/hitl/` |
| `supervisor_final` payload key `recovered_from_digest` | Correct for what ran, but **HEAD writes `packet_built` instead** — so the runtime predates `dd51d39` (2026-08-03, "Introduce SupervisorPacket"). Several findings below are already fixed on `dev` | `git show HEAD:src/wastech_orchestrator/core/supervisor.py \| grep -n "packet_built"` → line 797 |
| "62 findings raised on `accept` verdicts … nothing in the flow closes them" | The **first half is right and the second half is wrong**: all 62 are converted to follow-ups and written into `summary.{json,md}` = the PR body. What is missing is a _code step_, not an operator surface | 98 follow-ups across 20 `summary.json` files split **36 supervisor-authored / 62 evaluator-derived** (evidence string `"review evaluator finding (accepted with findings)"`), by `_evaluator_finding_follow_ups` at `core/supervisor.py:430` |

---

# HALF 1 — the two runs

## Frame

**`p9-12-04-mcp-custom-rules`** — `done`, attempt 1, 0 retries, 0 fallbacks, **1 HITL approval**, review accepted first pass with 3 low findings. Path: `planning(675 s) → [HITL 65 min] → planning(122 s) → implementation(576 s) → testing(24 s, pass) → review(499 s, accept) → documentation(187 s) → publish(43 s)`. Diff 14 files / 40 KB.

**`p9-12-06-process-boundary-tests`** — `done`, attempt 1, 0 retries, 0 HITL, review accepted first pass with 1 medium + 3 low. Path: `planning(640) → implementation(760) → testing(25, pass) → review(427, accept) → documentation(136) → publish(106)`. Diff 18 files, +527/−17.

## H-1 · The "double planning" is a HITL approval round-trip, and the re-entry cost $2.13

**category** flow/HITL · **severity** low (working as designed) · **confidence** certain

**Evidence** — `logs/p9-12-04-mcp-custom-rules/hitl/planning.json`:

> `"kind": "approval"`, `"answer": "approved"`, `"status": "consumed"`, `"question": "P12.04 is explicitly a maintainer decision between (A) widen the MCP lint input schema … and (B) document ad-hoc lint as built-in-only. I recommend **(A)**. Approve (A), or reply \"B\"?"`

`node_runs`: `217\|planning\|…07:30:51→07:42:07\|675 s` then `218\|planning\|…08:47:32→08:49:35\|122 s` — a **65-minute human gap** between them. Per-attempt cost:

```
stages/planning/run-000217/1-claude | planning | 5,375,496 in | 43,555 out | $5.159
stages/planning/run-000218/1-claude | planning |   646,522 in |  9,860 out | $2.127
```

**Root cause** — not a retry and not a defect: the planning node parked for approval, the answer arrived, planning re-entered. The re-entry is cheap in tokens (646 K vs 5.38 M) but still **$2.13**, because the re-entry re-renders the plan rather than resuming from the parked plan. Both HITL requests in the range were high-quality, genuinely load-bearing questions (p9-12-01's asked permission to include a one-line product fix in a test-only task) — the HITL layer is earning its keep.

**Lever** — none needed for correctness. If the $2.13 matters: `planning`'s `session_scope` in `.worc/flows/implementation.yaml` (target) / `packaged/flows/implementation.yaml` (default) controls whether the re-entry resumes the parked lineage. Note the *first* planning pass burned **5.38 M input tokens / $5.16** — planning is the second-largest cost centre in the range and the single most expensive node on this task, more than implementation ($3.94).

**Scope** target-only observation, orchestrator-wide if session reuse across HITL is changed. **Impact** ~$2/HITL round-trip; matters only if HITL becomes routine.

## H-2 · The 106 s publish is 98.8 s of supervisor finalize + 7 s of git — and the finalize failed its schema three times

**category** infra/config · **severity** high · **confidence** certain

**Evidence** — `_engine_finalize` runs the whole-task summary **inside the publish node** (`core/orchestrator.py:2995-3007`: _"The constant supervisor layer synthesizes `summary.{md,json}` at whole-task close (before publish, so the `summary.md` is the PR body)"_). Timings line up to the second:

|  |  |
| --- | --- |
| publish node | `10:27:55` → `10:29:41` (**106 s**) |
| supervisor finalize (`stages/supervisor/run-000000`) | `10:27:55` → `10:29:34` (**98.8 s**) |
| `publish_operations` code_commit / push / audit_commit | all `10:29:34` |
| `publish_operations` pr | `10:29:41` |

Finalize usage on this run: **236,902 input / 9,257 output / $0.473** vs a 19-run median of ~88 K / 2.6 K / $0.30. The excess is fully explained by `stages/supervisor/run-000000/1-claude/events.jsonl` — see G-1. Publish itself is ~7 s, identical to the other 19 runs; the 40 s "typical publish" is really 30 s finalize + 7 s git.

**Root cause** — publish wall-time is dominated by an LLM call that has nothing to do with git. Not a defect, but it means "publish is slow" is never a git diagnosis.

**Lever** — none (informational). Worth noting in `docs/operations.md` on `main` so an operator debugging a slow publish looks at `stages/supervisor/run-000000/` first. **Scope** orchestrator-wide.

## H-3 · A test-authoring task is served _well_ by the review evaluator's general sections — but "the requirement is not actually satisfied" has no blocking hook

**category** prompt/flow · **severity** medium · **confidence** high

The flow ships no `testing_quality` evaluator, and the YAML says so deliberately (`.worc/flows/implementation.yaml:14-16`):

> `# An optional non-blocking testing_quality evaluator (the shared evaluator runner already supports its self-cap) is NOT shipped in this default flow: it is a pure graph-shape choice — an operator who wants it adds the node to their own flow YAML.`

**The absence did not hurt this run.** The `review.md` role prompt's `## Test Coverage` section (`.worc/flows/implementation/review.md:46-52`) is framed entirely as _coverage of a production change_ — "A unit test per new/changed rule or algorithm", "fixtures small enough that a failure points at one behavior" — which inverts uselessly when the deliverable _is_ the tests. Yet the review's four findings are excellent test-quality critique, produced by the generic `Requirements And Correctness` + `Code Quality` sections applied to test code. The best one (medium):

> "The checklist↔inventory pairing the deliverable rests on is not actually enforced. `.agents/rules/testing.md` claims 'Adding a category here means adding it to that inventory too, and vice versa. The pairing is what stops this table from claiming coverage the tree no longer has' — but the test hardcodes the four names (`expect(categories).toEqual(["determinism","installed-bin-spawn","shared-exclude","write-failure"])`) and its own `BOUNDARY_GUARDS` file lists, and reads nothing from `testing.md`. A fifth category added to the doc's table, or a guard file listed there but dropped from `BOUNDARY_GUARDS`, fails nothing — the exact prose-rot mode the file header says it exists to prevent."

I verified this against the diff: the six pre-existing test files gained only `@boundary-guard <category>` annotation comments (2–6 lines each, e.g. `packages/core/test/primitives.test.ts` `+ // @boundary-guard determinism`), and `boundary-guards.test.ts` (+99) indeed hardcodes both lists. **The reviewer is correct: the annotations exist and nothing cross-checks them.** Also caught: an `afterAll` that throws `TypeError` and masks the real `beforeAll` setup error on Windows, and an exit criterion ticked `[x]` while half of it was deliberately not delivered.

**Root cause of the real problem** — the finding says the deliverable's central mechanism does not work, i.e. acceptance criterion 1 is arguably unmet. It shipped anyway, and **not because the model judged loosely**: `DEFAULT_GATE_SEVERITY = "high"` (`core/flow/schema.py:31`) and the active flow never overrides it, so `medium` is non-gating **by construction** (`core/flow/nodes/evaluator.py:289`: `gate_rank = _severity_rank(node.gate_severity)`). Separately, `review.md`'s `## Blocking Invariant Violations` list has **no entry for "the change does not satisfy the task's requirement"** — that lives in the non-blocking `Requirements And Correctness` section, so the model had no blocking category to reach for even if it wanted one.

**Lever (two, independent)**

1. `.worc/flows/implementation.yaml` review node (currently lines 74-85, no `gate_severity` key): add `gate_severity: medium`. See G-5 — the packaged Aug-3 default already documents this key; the active copy does not.
2. `packaged/flows/implementation/review.md` `## Blocking Invariant Violations`: add an entry for _"the change does not satisfy a stated acceptance criterion, or its central mechanism does not do what the deliverable claims"_. Currently the four blocking entries are all invariant/dependency/scope shaped.

**Scope** (1) target-only; (2) orchestrator default. **Impact** the single medium finding on p9-12-06 would have driven one `fixing` round (~$5) instead of shipping an unenforced guard; across the range, 18 medium findings become gating.

**A `testing_quality` node is NOT the recommendation here.** The evidence says the review evaluator already does this job well; adding a node adds a turn per rework cycle for signal that is already being produced.

## H-4 · Diff-vs-intent: both clean

**p9-12-04** — 14 files: 3 code (`mcp-server/src/tools/lint.ts`, 3 mcp-server test files), 1 core file, 9 docs. The task's constraint _"no change to `lint-files`, core's `ruleEntryUnionSchema`, or `packages/cli/schema.json`"_ is honored: `packages/core/src/config/config-schema.ts` is touched but **the hunk is comment-only** (verified — `@@ -72,11 +72,13 @@` replaces 5 comment lines with 7, `export const ruleEntrySchema` unchanged). The named deviation from the task's literal `ruleEntryUnionSchema` was surfaced through HITL at planning and documented. The two extra doc files (`P11-remediation/07-custom-missing-id.md`, `P7-mcp-server/02-lint-tools.md`) are "superseded by" pointers — proportionate. **No scope drift; no under-delivery.**

**p9-12-06** — 18 files, +527/−17, and every file maps to one of the four deliverables: `.agents/rules/testing.md` (+46, deliverable 1), `boundary-guards.test.ts` (+99, enforcement), `bin-entrypoint.test.ts` (+118, deliverable 2), `.github/workflows/publish.yml` + `.prettierignore` (deliverable 3), `accepted-behaviors.md` (+62, deliverable 4). The constraint _"Out of scope: writing the individual P11/P12 fixes' tests"_ is honored — the six pre-existing test files gained annotations only (2–6 lines each). **Scope clean.** Under-delivery is disclosed, not hidden: criterion 3 asked CI's format job to cover `tasks/**` and the run reworded the criterion to `docs/` with `tasks/` recorded as intentionally ungated — the reviewer flagged exactly that as a low finding.

---

# HALF 2 — the supervisor layer across all 20 runs

## Q1 — Where do the supervisor's observations go? Precisely: nowhere but itself and one Markdown file.

**Three call sites read `evaluations`. Two of them filter the supervisor out explicitly.**

| Call site | What it reads |
| --- | --- |
| `core/supervisor.py:767` (`finalize`) | **all** rows — including its own `supervisor_step` notes via `_finalize_digest` (`:877-903`) and evaluator findings via `_evaluator_finding_follow_ups` (`:430`) and `_render_gate_digest` (`:446`) |
| `core/orchestrator.py:2024` | `in_flow_verdict` **only** — with the exclusion written into the comment: _"take the last in_flow_verdict row (**skip the supervisor_step/final rows**, which carry no node_id/run_id)"_ |
| `core/flow/recorder.py:34` | `[e for e in store.get_evaluations(task_id) if e.kind == "in_flow_verdict"]` |

The module docstring states the contract outright (`core/supervisor.py:18-20`):

> "Each observation is recorded as an immutable `evaluations` row (`supervisor_step` / `supervisor_final`, `verdict='advisory'`) and surfaced to the human (the summary becomes the PR body), **but the engine never consumes it to route**. Blocking is the job of in-flow `review` / `test_quality` evaluators."

And the return value is discarded. `FinalizeResult` carries `follow_ups`, but at the only call site (`core/orchestrator.py:3013-3020`) **only `candidate_delta` is read**, and only when memory is on:

```python
finalized = self._supervisor.finalize(task_id=…, emit_delta=memory_on)
if memory_on:
    self._write_memory(p, finalized.candidate_delta, WriteSource.SUCCESS)
```

`finalized.follow_ups` is never referenced. The follow-ups reach the operator only because `finalize()` itself writes them into `summary.json` / `summary.md` before returning (`:806-819`).

**So the complete path is:** `supervisor_step` → _(its own finalize digest)_ → `summary.md` → PR body → a human. No task file is created, no backlog entry, no gate, no route, no `node_runs` row. The one case where a supervisor observation _did_ change something downstream is the supervisor's own summary prose. **This is design-intentional and documented — the finding is not that it's advisory, it's that the advisory surface is lossy (G-9) and 39 % of the invocations feeding it had nothing to observe (G-3).**

## Q2 — Cost: $19.14, and it _is_ tracked; it is just invisible to every natural query

`provider_attempts.node_run_id` is documented as the supervisor's slot:

```sql
-- the ``node_runs`` id this attempt belongs to (a plain monotonic id, not an FK), or NULL for
-- the constant supervisor layer, which is not a graph node and has no ``node_runs`` row (VF-8).
node_run_id INTEGER,
```

and `_record_provider_attempts` (`core/supervisor.py:1296-1316`) writes them with `node_run_id=None  # the supervisor is a constant layer, not a graph node`. All 222 NULL-`node_run_id` rows in the DB have `attempt_dir LIKE '%supervisor%'` (222/222) — the mapping is exact.

Reconstructed by turn kind (joining `stages/supervisor/run-<node_run_id>/…/result.json` back through `node_runs`):

| turn kind | n | USD | avg | input tok | output tok |
| --- | --: | --: | --: | --: | --: |
| **FINALIZE** (whole-task) | 20 | **6.50** | 0.325 | 2,050,562 | 61,495 |
| observe: implementation | 20 | 3.55 | 0.178 | 1,242,680 | 15,705 |
| observe: documentation | 20 | 3.19 | 0.159 | 1,074,425 | 19,184 |
| observe: planning | 20 | 2.87 | 0.144 | 548,132 | 12,565 |
| observe: fixing | 9 | 1.22 | 0.135 | 695,188 | 7,970 |
| observe: review | 29 | 0.95 | 0.033 | 1,328,487 | 18,250 |
| observe: testing | 29 | 0.86 | 0.030 | 1,322,055 | 7,621 |
| **TOTAL** | **147** | **19.14** |  | 8,261,529 | 142,790 |

**The observability finding is not "untracked" — it is "unattributable by the obvious query".** Any per-node roll-up joins `provider_attempts → node_runs`, which drops the supervisor into a `NULL` bucket that a `GROUP BY node_id` silently labels empty. That is exactly how the shared brief's own cost table came to omit $19.14 and mislabel the remaining $306.02 as the whole $325.16. There is **no cost-reporting surface at all** in the orchestrator: `grep -rn "usage_cost" src/` returns only the writer (`state_store.py:1204`), the reader (`:1690`), and `flow/observability.py:151` — no CLI command, no ledger field, no summary line sums it.

**Lever** — `src/wastech_orchestrator/ledger.py` / `logs/completed.jsonl`: add a `cost_usd` (and optionally `supervisor_cost_usd`) field per finished task, sourced from `SELECT SUM(usage_cost) FROM provider_attempts WHERE task_id=?` split on `node_run_id IS NULL`. **Scope** orchestrator-wide. **Impact** an operator learns a task cost $17 without writing SQL, and the supervisor layer stops being free-by-omission.

## Q3 — Quality and calibration of the observations

**Volume**: 127 notes, 172,856 chars (avg 1,361). Zero failed observations (`observation_failed` is `0` for all 127).

### Genuinely high signal — where the observer had material

Sampled across `p9-11-01`, `p9-11-03`, `p9-12-05` (8 notes read in full plus 8 `testing` notes). Real examples:

- `p9-11-01 / fixing → done`: _"the npx smoke check was **unconditionally skipped in every environment** before this cycle — meaning it provided zero actual coverage despite looking like a test."_
- `p9-11-01 / documentation → done`: _"caught and corrected several previously-inaccurate claims in the P11.01 task doc (an overstated exit criterion, an **arithmetically wrong** '3 of 4 assertions fail' claim, and a false 'verified defense-in-depth' claim about `--no-install`)."_
- `p9-12-05 / implementation → done`: _"the initial acyclic-vs-cyclic asymmetry (~4,750 vs ~9,600) was investigated rather than accepted, correctly diagnosed as JIT warmup, and the plan used the conservative floor (~4,750) rather than the flattering number."_

### Fact-check: the p9-12-05 claim is **TRUE**

The note the brief quotes (`p9-12-05 / documentation → done`) says:

> "this step explicitly declines to address a review low finding in `packages/core/src/engine/rules/llm.ts` (a broken antecedent — 'Both are single digits' — in a why-comment) … **there is still an outstanding, acknowledged review finding with no code step yet to close it.** My prior 'review — accept' summary described the task as complete; this step shows that isn't quite right."

Verified against `evaluations`:

```
== review accept
  - medium | The new `## Limitations` bullet contains a broken sentence … | paths ['docs/guide/context-graph.md']
  - low    | In the new `traverse` why-comment, "Both are single digits in practice" has no clear
             antecedent after the rewording … | paths ['packages/core/src/engine/rules/llm.ts']
```

Correct on every particular — the file, the quoted phrase, the severity, and the fact that no later node closed it. Also worth noting the **self-correction**: the observer retracted its own prior "review-accept closed things out" claim one step later. That is real calibration, not boilerplate.

### The "noted in memory" phrase is **NOT a confabulation**

`p9-12-05 / implementation` says _"matching the P12.01–.04 precedent **noted in memory**"_ while `memory.enabled: false`. It is grounded twice over:

1. The implementation step's own `final_message`, verbatim in the observation prompt (`logs/p9-12-05-recursion-depth/stages/supervisor/run-000225/rendered-prompt.md`): _"Left `P12-consistency/index.md` and the audit reports untouched, **per the P12.01–.04 precedent**."_
2. `logs/p9-12-05-recursion-depth/plan.md:34`: _"**Do not touch** `P12-consistency/index.md` or the audit reports — completed **P12.01–.04 left both untouched**."_

"Memory" = the observer's own warm `resume_own_lineage` session, which had read the plan at the planning observation. **But the word collides with a named product feature**, so an operator reading the note reasonably suspects hallucination — as happened here. **Lever**: `packaged/flows/implementation/supervisor.md` — instruct the observer to attribute a cross-step recollection as _"noted at the \<node\> step"_, never "in memory". Tiny prompt change, removes a recurring false alarm. **Scope** orchestrator default.

### Boilerplate density — the stock phrases are the _rubric_, not filler

| phrase                        | occurrences in 127 notes |
| ----------------------------- | -----------------------: |
| `scope drift`                 |                       59 |
| `repeated-failure`            |                       75 |
| `no scope drift`              |                       50 |
| `no repeated-failure pattern` |                       30 |

These trace directly to the role prompt's instruction (`.worc/flows/implementation/supervisor.md`): _"Call out two patterns explicitly when you see them: (a) the run repeating the same failure across fix cycles … and (b) the change drifting beyond the task's stated scope."_ So ~40 % of every note is a two-item checklist restated. Not hallucination, not waste in principle — but it means the _negative_ case ("neither pattern present") is paid for at full Sonnet price 80 times.

### The observer is a compressor, not an auditor — 98 of 127 turns made zero tool calls

```
observe turns: 127   with ZERO tool calls: 98   (77%)
tool calls total: {'Read': 40, 'Grep': 22, 'Glob': 3}
```

77 % of observations are a Sonnet-`medium` re-reading of what the Opus-`xhigh` step already wrote in its own `final_message`. That is a legitimate function (compression + pattern flagging for a human), but it is _not_ independent verification — no note in the range is grounded in anything the step didn't already claim. Framing matters for the value question: the layer buys a **short, skimmable, cross-step-aware digest**, not a second opinion.

## Q4 — The `supervisor_final` payload

**`memory_delta: false` on all 20 is exactly expected and carries zero information.** `core/orchestrator.py:3012-3013` passes `emit_delta=memory_on` where `memory_on = self._config.memory.enabled` (`false`), and `_finalize_turn` (`core/supervisor.py:871`) computes `delta = parse_delta(...) if emit_delta else None` — so `delta` is unconditionally `None` and the `memory_delta` key is not even added to the output schema (`_finalize_schema:263-264`). It is a deterministic restatement of config, not a signal.

**`follow_ups: N` — the follow-ups were written, and an operator can see them, with one important caveat.** All 20 `summary.json` files carry the declared count (6,5,8,7,4,1,3,4,10,5,2,6,7,10,2,1,4,5,4,4 = **98**), and `summary.md` renders them as a `## Technical debt / follow-ups` section that becomes the PR body. Verified on `p9-12-04`: `summary.json` has 5 records, and `summary.md`'s tail carries all 5 as `- **[low] …** — … Paths: … Suggested: …`. **Caveat: see G-9 — 14 of the 20 sections were elided from the actual PR.**

---

## Findings, ranked

### G-1 · CRITICAL — a required-but-empty `follow_ups` key cost p9-12-06 its entire PR body

**category** config (output schema) + prompt · **severity** critical · **confidence** certain

**Evidence** — `logs/p9-12-06-process-boundary-tests/stages/supervisor/run-000000/1-claude/events.jsonl`, in order:

```
ev5  tool_use StructuredOutput len=9293   {"summary": "P12.06 is the \"prevent the class\" task closing out the post-P9 audit's second remediation round. …"}
ev6  tool_result is_error=True            "Output does not match required schema: root: must have required property 'follow_ups'"
ev12 tool_use StructuredOutput len=9339   {"summary": "P12.06 is the \"prevent the class\" task …"}
ev13 tool_result is_error=True            "Output does not match required schema: root: must have required property 'follow_ups'"
ev18 tool_use StructuredOutput len=5808   {"summary": "P12.06 is the \"prevent the class\" task …"}
ev19 tool_result is_error=True            "Output does not match required schema: root: must have required property 'follow_ups'"
ev24 tool_use StructuredOutput len=37     {"summary": "test", "follow_ups": []}
ev25 tool_result is_error=None            "Structured output provided successfully"
```

Result — `logs/p9-12-06-process-boundary-tests/summary.md` in full above the follow-ups section:

```markdown
# P12.06 Process-boundary test guards and format-gate publish process

test

## Technical debt / follow-ups
```

and in the live PR body (`pr_body_appended.md:388-390`):

```
# P12.06 Process-boundary test guards and format-gate publish process

test
```

`summary.json` → `summary field: 'test'`. All 4 follow-ups on this run are evaluator-derived (`evidence: ['review evaluator finding (accepted with findings)']`), so **the supervisor's LLM contribution to the final deliverable of the whole 20-task range was the four-character string `test`** — for $0.473, 236,902 input tokens and 98.8 s. Isolated to this run (`schema_errors=0` on the other 19), but it is the last task and the failure is silent.

**Root cause** — three-way tension:

1. `_finalize_schema` (`core/supervisor.py:267-275`) puts every key in `required`: `"required": list(properties)`, with the comment _"OpenAI strict mode: `required` must list every present key. `memory_delta` and `follow_ups` are nullable at their roots, so requiring them still lets the model emit `null`."_
2. `_FOLLOW_UPS_SCHEMA` (`:171-186`) has **no `description`** anywhere telling the model that omitting the key is illegal and `null`/`[]` is the way to say "none".
3. The finalize role prompt says the opposite of "always include it" — `.worc/flows/implementation/summary.md` / the rendered footer: _"**Leave the array empty when nothing qualifies**; a record without evidence is dropped."_ A model with no follow-ups of its own reads that as "omit".

Given three rejections of a 9 KB payload whose only diagnostic is the missing property name, collapsing to a minimal probe (`{"summary":"test","follow_ups":[]}`) is rational model behavior — and the probe validated and was accepted as final.

**Lever (do both; they are independent and both small)**

1. `src/wastech_orchestrator/core/supervisor.py:171` — add a `description` to `_FOLLOW_UPS_SCHEMA`: _"Always include this key. Use `null` (or `[]`) when nothing qualifies — omitting the key fails validation."_ Same for `DELTA_OUTPUT_SCHEMA`'s nesting. This is the minimal fix and keeps OpenAI-strict compatibility.
2. `src/wastech_orchestrator/packaged/flows/implementation/summary.md` — change _"Leave the array empty when nothing qualifies"_ to _"Return `follow_ups: []` when nothing qualifies — never omit the field."_ (target copy `.worc/flows/implementation/summary.md` for this repo only). **The Aug-3 packaged version has NOT fixed this** — I diffed it: it rewrote the grounding paragraph for the packet but left the follow-ups guidance untouched.

**Scope** orchestrator default — any flow with `emit_follow_ups: true` (or memory on) is exposed. **Impact** eliminates a silent total-loss failure mode on the run's most visible artifact.

### G-2 · HIGH — the degradation guard checks that `summary.md` _exists_, not that it says anything

**category** code · **severity** high · **confidence** certain

**Evidence** — `core/orchestrator.py:3029-3036`:

<!-- Fenced as `text`, not `python`: a verbatim source excerpt that `ruff format` would rewrite (it is not a formattable top-level statement), which would corrupt the evidence and fail the format gate. -->

```text
summary_md_path = task_artifact_dir(self._artifacts_root, p.task.id) / "summary.md"
degraded = not summary_md_path.exists()
if degraded:
    log.warning("task finalize: summary degraded to deterministic fallback "
                "(no provider-authored synthesis)")
```

On p9-12-06 the file existed and contained a 4-character body, so `degraded` was `False`, no warning fired, and `supervisor_final` recorded `"summary_written": true`. The intent is explicit in the surrounding comment — _"make that degradation loud (WARNING + a visible callout in the fallback body) instead of shipping a stub as if it were the full synthesis"_ — and a 4-byte summary is precisely "shipping a stub as if it were the full synthesis". The finalize role prompt already forbids it in words (`.worc/flows/implementation/summary.md`: _"Always produce a real summary — never an empty or placeholder one."_), which is why a code-side check is needed: the prompt was obeyed in spirit and defeated by the validator.

**Root cause** — existence is a proxy for substance, and G-1 breaks the proxy.

**Lever** — `src/wastech_orchestrator/core/supervisor.py:804-808`: `_sanitize_summary` already normalizes the text; treat a summary below a small floor (e.g. `len(clean_summary) < 200` or no newline and < 200 chars) as no summary, so `finalize` returns `summary_path=None`, `summary.md` is not written, and the orchestrator's existing `degraded` path fires with its WARNING and deterministic fallback body. Purely additive, no new config key. **Scope** orchestrator default. **Impact** a degenerate synthesis becomes a loud fallback instead of a published stub.

### G-3 · HIGH — 58 of 147 supervisor invocations (39 %) observed a prompt with no content

**category** flow/config · **severity** high · **confidence** certain · **already fixed on `dev`**

**Evidence** — **zero of 147** supervisor rendered prompts contain a findings section:

```
grep -l "Findings it recorded" p9-1[12]*/stages/supervisor/run-*/rendered-prompt.md | wc -l   →  0
ls p9-1[12]*/stages/supervisor/run-*/rendered-prompt.md | wc -l                              →  147
```

Two whole classes of turn were therefore content-free. Verbatim from `logs/p9-12-05-recursion-depth/stages/supervisor/run-000227/rendered-prompt.md` — the _entire_ observed block after the role text:

```
## Step observed
Node: review
Outcome: rework
```

and `run-000226`:

```
## Step observed
Node: testing
Outcome: pass
```

The observer said so itself (`p9-12-05 / review → rework`): _"**No review feedback content was included in this report, so I can't yet assess what triggered the rework request.**"_ And on every one of the 29 `testing` observations, e.g. `p9-11-03`: _"Testing step passed … **No further detail was provided in this step's report**"_; `p9-11-01`: _"Testing step reported a bare 'pass' with no accompanying detail (no test counts, no output)."_

Cost of the blind classes: `observe: testing` 29 calls / $0.86 / 1.32 M input + `observe: review` 29 calls / $0.95 / 1.33 M input = **58 calls, $1.81, 2.65 M input tokens** — 39 % of invocations for 9.5 % of supervisor spend, producing nothing an artifact didn't already state.

**Root cause** — the runtime predates two fixes. The version that ran matches `61ef90f` (2026-07-25 22:41), whose call site is:

```python
self._supervisor.observe(task_id=…, node_id=node.id, node_run_id=node_run_id,
                         outcome_kind=outcome.kind, final_message=outcome.final_message)
```

— no `findings`. Runs began `2026-07-25T22:04:59Z`.

**Both are already fixed at HEAD**, with comments that read like a transcript of this batch:

- `core/orchestrator.py:252` — `_UNOBSERVED_NODE_KINDS = frozenset({"tool", "checks", "publish"})`, documented as _"the deterministic `tool` / `checks` nodes, whose result is already a durable fact the finalize packet carries — **a note about a pass/fail bought nothing and cost a full turn on every run**."_ → kills all 29 `testing` observations.
- `core/orchestrator.py:3231` — `findings=outcome.findings`, documented as _"Passing only the outcome label had the supervisor acknowledge `accept` for a node that had filed a substantive finding, and then describe the gate as having passed in the whole-task summary."_ Landed in `ee1d224` (2026-07-27 22:24), **21 minutes before the second batch started** — the operator's install was not refreshed mid-batch.

**Lever** — no code change. **Operational**: reinstall/refresh the orchestrator (`pip install -e .` / `worc install`) before the next batch, and re-run one task to confirm `stages/supervisor/run-*/rendered-prompt.md` now contains `Findings it recorded:` and that no `stages/supervisor/run-<testing-node-run-id>/` directory is created. **Scope** target-only (operational). **Impact** −29 invocations outright; the remaining 29 review observations become substantive instead of "I can't yet assess".

### G-4 · HIGH — every PR body in the range was written without the diff

**category** prompt/code · **severity** high · **confidence** certain · **already fixed on `dev`**

**Evidence** — `logs/p9-12-06-process-boundary-tests/stages/supervisor/run-000000/1-claude/request.json`:

```json
"context_paths": {"task_path": "[REDACTED]/.worc-io/p9-12-06-process-boundary-tests/task.md"}
```

One path. No `diff_path`, no `plan_path`, no `review_path`. The rendered prompt's footer confirms it:

```
Context files (read them as needed; do not assume their contents):
- task: /Users/…/.worc-io/p9-12-06-process-boundary-tests/task.md
```

…while the role text three paragraphs earlier says _"Synthesize a short plain-language summary of the whole task, **grounded in the change that was actually committed**"_. Across 20 finalize turns there are **21 `Read` calls total** (≈1 per run — the task file), and the security preamble in the same prompt tells the agent _"do not read `.worc/`"_, where `current.diff` lives. So every PR body was synthesized from the warm session's recollection of step reports.

**Root cause** — the finalize turn's grounding was session memory, and the one artifact it was handed was the task spec.

**Already fixed** by `dd51d39` (2026-08-03, "Introduce SupervisorPacket"): `finalize` now runs `resume_session=False` on a fresh session seeded by a deterministic `SupervisorPacket` (`core/supervisor_packet.py`), and the packaged `summary.md` role prompt was rewritten to match — _"Read the run's facts from the `packet` file named in your context: **the changed paths and diff stat (with a pointer to the full diff)**, each executed step with its outcome and what it reported, which checks passed or failed, and the notes you recorded while observing. Ground every claim in it… If something is absent from the packet, say so plainly instead of inferring it."_ The `recovered_from_digest` → `packet_built` key change in `supervisor_final` is the version marker.

**Lever** — none; verify on the next run that `stages/supervisor/run-000000/1-claude/request.json` `context_paths` includes the packet, and that the finalize turn opens the diff. **Scope** orchestrator default. **Impact** removes the largest correctness risk in the most-read artifact.

### G-5 · HIGH — `gate_severity` was undiscoverable in the active flow, so 62 findings could never gate

**category** flow/config · **severity** high · **confidence** certain · **already fixed in the packaged default**

**Evidence** — `DEFAULT_GATE_SEVERITY = "high"` (`core/flow/schema.py:31`), applied at `core/flow/nodes/evaluator.py:289` (`gate_rank = _severity_rank(node.gate_severity)`). The **active** flow never mentions the key:

```
grep -c "gate_severity" .worc/flows/implementation.yaml                                  → 0
grep -c "gate_severity" src/…/packaged/flows/implementation.yaml                         → 2
```

Worse, the active flow's own per-node tuning legend (`.worc/flows/implementation.yaml:34`) enumerates the evaluator knobs and **omits this one**: `#   evaluator nodes also: blocking (failing verdict blocks vs advisory), max_rework_per_stage`. An operator reading the shipped flow to find "how do I make mediums block?" cannot find out. Consequence across the range: **18 medium + 44 low findings were non-gating by construction**, including p9-12-06's _"the checklist↔inventory pairing … is not actually enforced"_.

**Already fixed on `dev`** — `packaged/flows/implementation.yaml:35-36` now lists it in the legend (`gate_severity (min finding severity that gates: high|medium|low|critical|blocking; default high)`) and `:106-107` ships it as a commented slot on the review node with guidance (_"Default high = block on high/critical/blocking; lower it (e.g. medium) to gate on nits too."_).

**Lever** — target-only: add `gate_severity: medium` to the `review` node in `.worc/flows/implementation.yaml` (lines 74-85), or re-run `worc install` to pick up the documented packaged flow. **Scope** target-only (the doc fix is already orchestrator-wide). **Impact** the 18 mediums become one `fixing` round each instead of PR-body debt — at ~$5/round that is a deliberate spend/quality trade the operator can now actually see and choose.

### G-6 · MEDIUM — 22 notes declare the task finished on a node that is never last

**category** prompt · **severity** medium · **confidence** high

**Evidence** — notes claiming closure, by observed node:

```sql
SELECT json_extract(findings_json,'$.node'), COUNT(*) FROM evaluations
WHERE task_id GLOB 'p9-1[12]*' AND kind='supervisor_step'
  AND (note LIKE '%closes out%' OR note LIKE '%closes here%'
       OR note LIKE '%end of the task%' OR note LIKE '%task is complete%')
GROUP BY 1;
→ documentation|2  fixing|1  implementation|3  review|11  testing|5
```

In this flow `review` is **always** followed by `documentation` then `publish`, so all 11 `review` claims and all 3 `implementation` claims are wrong. Two notes then retract themselves in writing:

- `p9-12-05 / documentation`: _"**My prior 'review — accept' summary described the task as complete; this step shows that isn't quite right.**"_
- `p9-11-07 / review`: _"**this reverses my prior assessment that the task was rea[dy]…**"_

And the wasted work is concrete: on `p9-12-05` the `review → accept` observation wrote a full 2,668-char note headed `## End-of-task summary` — a whole-task synthesis produced at the _wrong_ step, which the real finalize turn then produced again.

**Root cause** — the observation prompt contains the security preamble, the role file, and `## Step observed` (node + outcome + step report). **It never states the flow's shape or which nodes remain.** The observer is asked to judge "is the run closing?" with no way to know.

**Lever** — `src/wastech_orchestrator/core/supervisor.py:1399-1414` (`_step_prompt`) / `_base_prompt`: add one deterministic line from the already-loaded snapshot, e.g. `Flow: planning → implementation → testing → review → documentation → publish (this node: review; remaining: documentation, publish)`. Purely orchestrator-side and flow-agnostic — it reads the graph, never branches on node names. Pair with a role-prompt line in `packaged/flows/implementation/supervisor.md`: _"Do not declare the task closed; the orchestrator's finalize turn does that."_ **Scope** orchestrator default. **Impact** removes 22 wrong claims from the material the finalize digest synthesizes from, and stops one duplicated whole-task synthesis per run (~$0.15/run, plus a cleaner PR body).

### G-7 · MEDIUM — the PR-body debt section prints the first 120 characters of every long finding twice

**category** code (rendering) · **severity** medium · **confidence** certain

**Evidence** — `_finding_to_follow_up` (`core/supervisor.py:386-389`) with `_FINDING_TITLE_MAX = 120`:

```python
if len(reason) <= _FINDING_TITLE_MAX:
    title, rationale = reason, ""
else:  # keep the bold title a label; the full text still reaches the operator via the rationale
    title, rationale = reason[:_FINDING_TITLE_MAX].rstrip() + "…", reason
```

and `_render_follow_ups_section` (`:333-345`) emits `- **[sev] {title}** — {rationale}`. Since `rationale` _is_ `reason`, the truncated prefix is immediately repeated in full. Live output, `logs/p9-12-04-mcp-custom-rules/summary.md`:

> `- **[low] Residual gap against the acceptance criterion "a `custom`entry either runs or fails with a clear, documented message":…** — Residual gap against the acceptance criterion "a`custom`entry either runs or fails with a clear, documented message": only custom-shaped entries the *permissive* branch still accepts reach`handleLint`. …`

62 of 98 follow-ups are evaluator-derived and most review findings exceed 120 chars, so this affects the majority of debt lines in every PR body — and it inflates the body against the 60,000-char cap that G-9 is about.

**Root cause** — the title/rationale split was designed for a model that authors both fields; a derived follow-up has only one field of text and gets it printed twice.

**Lever** — `src/wastech_orchestrator/core/supervisor.py:333-345` (`_render_follow_ups_section`): when `rationale.startswith(title.rstrip("…"))`, print the rationale alone (bolding only the `[severity]`); or in `_finding_to_follow_up`, set `title` to the truncated label and `rationale` to `reason[_FINDING_TITLE_MAX:]`. **Scope** orchestrator default. **Impact** roughly halves the debt section's length, delaying G-9's elision.

### G-8 · MEDIUM — the PR-body elision stub points at a gitignored path, so 14 of 20 tasks' follow-ups are unreachable for a reviewer

**category** code · **severity** medium · **confidence** certain

**Evidence** — all 20 runs published to **one** PR:

```sql
SELECT result_ref, COUNT(*) FROM publish_operations WHERE task_id GLOB 'p9-1[12]*' AND kind='pr' GROUP BY result_ref;
→ https://github.com/VladimirMakarevich/wastech-mdlint/pull/16 | 20
```

`_bound_pr_body` (`git_manager.py:391-401`, cap `_PR_BODY_MAX_CHARS = 60_000` at `:245`) compacts oldest-first. In the final body (`logs/p9-12-06-process-boundary-tests/pr_body_appended.md`): **20 `worc-task:` markers, 14 elided, ~6 full sections, only 7 `## Technical debt / follow-ups` headings survive.** The stub reads:

> `_Summary elided to keep the PR body under GitHub's limit; see `logs/p9-11-01-cli-bin-noop/summary.md`._`

But that path is not in the repository:

```
$ git check-ignore -v .worc/logs/p9-11-01-cli-bin-noop/summary.md
.git/info/exclude:13:.worc/	.worc/logs/p9-11-01-cli-bin-noop/summary.md
```

So for 14 of 20 tasks — roughly 65 of the 98 follow-ups, including the medium-severity ones — the _only_ operator surface the supervisor has is a dead link for anyone reading the PR on GitHub.

**Root cause** — the compaction mechanism is well designed (markers preserved, nothing lost on disk, `_bound_pr_body`'s docstring is explicit) but the stub's pointer is written as a repo-relative path to a git-excluded directory, and the compaction is content-blind: it elides the debt section along with the prose.

**Lever (pick one)**

1. `src/wastech_orchestrator/git_manager.py:426` — make the stub unambiguous about where the file lives: ``see `<repo>/.worc/logs/<id>/summary.md` on the machine that ran the task``.
2. Better: make `_bound_pr_body` compact the **prose** and keep the `## Technical debt / follow-ups` section, since that is the actionable half. The section is delimited by a stable heading, so the split is deterministic.
3. Combine with G-7, which cuts the section's size roughly in half and pushes the cap out.

**Scope** orchestrator default (affects any chained-branch flow). **Impact** the advisory layer's output stays visible on the PR for a 20-task chain instead of the last 6.

### G-9 · LOW-MEDIUM — the observe turn's step report is unbounded, so a chatty node inflates the whole layer

**category** prompt/code · **severity** low-medium · **confidence** high · **already fixed on `dev`**

**Evidence** — the `planning` observation on `p9-12-05` (`stages/supervisor/run-000224/rendered-prompt.md`) inlines the entire plan JSON as `The step reported:` — a single ~5,200-character block including a fenced TypeScript contingency snippet. Aggregate: `observe: planning` 20 calls carried 548 K input; `observe: implementation` 20 calls carried **1.24 M**; `observe: documentation` 20 calls carried 1.07 M. Total observe input 6.21 M tokens for 127 notes averaging 1,361 characters.

**Already fixed** — HEAD's `_step_prompt` (`core/supervisor.py:1410-1413`) wraps it: `observed += f"\nThe step reported:\n{bound_step_message(final_message)}\n"`, with the comment _"Bounded by the same per-step cap the packet uses: unbounded, a chatty node's closing message inflated every observation turn, and each rework round paid for it again."_ `bound_step_message` lives in the new `core/supervisor_packet.py`.

**Lever** — none; refresh the install (same action as G-3). **Scope** target-only (operational). **Impact** proportional cut to the 6.21 M observe input tokens.

### G-10 · LOW — `config.supervisor.role_file` is dead when a flow sets its own

**category** config · **severity** low · **confidence** certain

**Evidence** — `.worc/config.yaml:134-138` sets `supervisor: {role_file: roles/supervisor.md, model: claude-sonnet-5, reasoning: medium, provider: claude}`, and `.worc/flows/roles/supervisor.md` exists (1,690 bytes). But `.worc/flows/implementation.yaml:132-135` sets `supervisor: {role_file: implementation/supervisor.md, …}`, and `Supervisor.__init__` (`core/supervisor.py:661`) takes the flow value unconditionally: `self._flow_role_file = flow_supervisor.role_file if flow_supervisor else None`. The rendered prompts confirm the flow copy won — they carry `implementation/supervisor.md`'s wastech-mdlint paragraph, which `flows/roles/supervisor.md` does not have.

Related, worth noting rather than fixing: the **active** `implementation/supervisor.md` hardcodes the target repo — _"This repository is `wastech-mdlint`, a TypeScript/Node linter mid-rebuild to v2… Watch specifically for scope drift into the post-P0 monorepo/package layout"_. This is operator-authored, **not** a packaged default (`git log -S "wastech-mdlint" -- src/…/packaged/` → no commits), and the Aug-3 packaged version replaced that paragraph with a generic _"If the repository documents its own quality invariants or architecture rules (e.g. in a `CLAUDE.md`/`AGENTS.md`/`.agents/rules/`)…"_. Keeping the repo-specific version in the target copy is a legitimate, well-placed customization — it is a large part of why the notes are domain-sharp. Flag only so nobody "fixes" it by overwriting with the packaged default.

**Lever** — documentation, not code: note in `packaged/config.example.yaml` next to `supervisor.role_file` that a flow's `supervisor.role_file` overrides it, so an operator editing the config key does not wonder why nothing changed. **Scope** orchestrator default. **Impact** removes a silent-no-op config trap.

---

## What's already good

- **Cost accounting for the supervisor exists and is correct.** `provider_attempts` carries all 147 turns with full normalized usage (`node_run_id IS NULL`, `attempt_dir` under `stages/supervisor/`), and the schema comment tells you why. The problem is presentation, not instrumentation.
- **The advisory boundary is honored end to end, not just claimed.** Two independent read sites (`orchestrator.py:2024`, `flow/recorder.py:34`) filter `kind == 'in_flow_verdict'`, so a supervisor note is structurally incapable of routing the task. The docstring at `core/supervisor.py:18-20` states the contract and the code matches it.
- **`_evaluator_finding_follow_ups` closes a real gap.** Findings attached to an `accept` verdict would otherwise die in `state.db`; instead all 62 reach `summary.{json,md}`, evidence-tagged, deduped against the supervisor's own 36 (`_merge_follow_ups`, `_follow_up_key`), taking only each node's **last** verdict so superseded rework findings don't resurface.
- **`parse_follow_ups` is evidence-gated** (`:290-330`) — a record without a non-empty `evidence` array is dropped, so the model cannot inject speculative refactor ideas into a PR body. It shows: all 98 records carry evidence.
- **`_sanitize_summary`** stops a `<follow_ups>[JSON]</follow_ups>` text dump from riding into the PR body — a real failure mode, defended in code rather than only in the prompt.
- **`_schema_safe_reasoning`** caps structured turns to `high` with a documented reason (`:221-228`: _"at `xhigh` the provider spends the turn on thinking and fails to emit a valid tool call"_), and it worked — 19/20 finalize turns validated first try.
- **The observations' prose is worth reading.** Where the observer had material it produced specific, checkable claims, one of which fact-checks exactly against `evaluations`, plus two explicit self-corrections. It is a compressor rather than an auditor, but a good one.
- **`_bound_pr_body`'s design** — preserve every marker, compact oldest-first, keep titles, never lose data on disk — is the right shape; only the stub's pointer and its content-blindness need work.
- **`_last_verdict_per_node` keys on `(node_id, subtask_order)`** with the bug it prevents written into the docstring — the kind of comment that stops a regression.
- **Half 1 diffs**: both tightly scoped, both with disclosed rather than hidden divergence (p9-12-04's named schema deviation went through HITL and into the phase notes; p9-12-06's reworded exit criterion is stated in `## Implementation notes` and was caught by the reviewer anyway).

## Data gaps

1. **No per-invocation reasoning-token data.** `usage_reasoning_output` is NULL for all 147 supervisor attempts (as for flow nodes). Since 77 % of observe turns make no tool calls, the split between thinking and prose is unknowable — so "is `medium` the right tier for observe?" cannot be answered from this data. To close it, instrument the Claude adapter's reasoning-token extraction (`providers/claude.py`).
2. **The exact runtime commit is not recorded anywhere.** I dated it by behavioral archaeology (`recovered_from_digest` present + `findings` absent + `checks` observed ⇒ at/near `61ef90f`, 2026-07-25). `logs/<task-id>/` has no orchestrator version stamp, so "was this fixed before or after the run?" always costs a git bisect. **Recommend**: write the orchestrator version/commit into `task.normalized.json` or the control bundle manifest.
3. **`runs/exchange-seals/` is absent for all 20 tasks** (expected — `logging.clean_runs_on_success` defaults true and every task ended `done`), so I could not inspect the exact exchange tree the finalize turn saw. `request.json`'s `context_paths` substituted adequately. For future forensic runs set `logging.clean_runs_on_success: false`.
4. **`node_lineage` was not queried** for the supervisor's `__supervisor__` sentinel row, so I cannot confirm whether the warm observe session survived across the two batch windows (Jul 25-26 and Jul 27-28) or restarted. It affects the interpretation of "in memory" only marginally, since that claim is separately grounded in the step report and the plan.
5. **Whether the medium follow-ups were ever acted on by a human** is outside the artifacts — the PR is still open (`auto_merge: false`) and 14 of 20 sections are elided from its body, so "did a human read them" is unanswerable from `.worc/`.
6. **Only p9-12-06's finalize event log was read line-by-line**; the other 19 were checked by grep for the schema-error string (all clean) and by tool-call counting. A degenerate-but-valid summary that produced no schema error would not have been caught by that scan — though the summary sizes (5.5-13.8 KB) make one unlikely.
