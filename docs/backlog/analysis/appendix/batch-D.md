# Batch D — the four clean, cheap runs (p9-11-05, -06, -08, -11)

## Verdict

The "over-powered pipeline" hypothesis does **not** survive contact with the data, and the central reason is that these four runs did not share one model configuration: **p9-11-05 and p9-11-06 ran planning/implementation on `claude-sonnet-5`, p9-11-08 and p9-11-11 on `claude-opus-5`.** The flow was retuned mid-campaign (`.worc/flows/implementation.yaml`, mtime `Jul 28 00:21`), so the campaign contains a near-clean A/B — 7 Sonnet-era P11 tasks vs 7 Opus-era P11 tasks with an identical declared size mix — and the cheaper tier lost: **71% rework vs 23%, and 1.29 vs 0.23 `high`-severity findings per run.** The famous p9-11-05 planning inversion (7.18× wall time over implementation) is an artifact of `reasoning: max` on Sonnet and is **already gone**: Opus/`xhigh` planning averages 0.95× implementation wall time.

"Accept" did not mean quality on p9-11-06. Its diff carries the exact markdown defect class the same reviewer, same model, same reasoning, same prompt rated **medium/rework** on p9-11-07 — and `events.jsonl` proves the reviewer read the offending line and returned `[]` anyway.

The single biggest improvement is **not** a model change. It is giving the review evaluator a severity floor and an explicit accept/rework rule: the words "accept", "rework", and the severity enum appear nowhere in `review.md`, while the actual gate is `gate_severity: "high"` in code.

---

## Per-run frame

All four: `done`, attempt 1, `validation_passed=1`, `test_fix_cycles=0`, `review_fix_cycles=0`, `fix_iterations=0`, zero retries, zero fallbacks, zero HITL, zero skipped nodes, PR published. Path: `planning → implementation → testing(pass) → review(accept) → documentation → publish`.

| run | model: plan / impl / review / doc | plan s/$ | impl s/$ | review s/$ | doc s/$ | total $ | diff (+/−, files) | findings |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| p9-11-05-table-primitive-scope | **sonnet-5**/max, **sonnet-5**/xhigh, opus-5/**xhigh**, opus-5/medium | 883 / 3.28 | 123 / 0.88 | 175 / 1.29 | 80 / 0.75 | 7.20 | +129 −16, 6 files | 2 (1 med, 1 low) |
| p9-11-06-regex-substitution-safety | **sonnet-5**/max, **sonnet-5**/xhigh, opus-5/**xhigh**, opus-5/medium | 715 / 2.37 | 255 / 1.37 | 391 / 2.18 | 88 / 0.77 | 7.60 | +185 −27, 9 files | **0** (`[]`, 2 bytes) |
| p9-11-08-init-exclude-anchoring | opus-5/xhigh, opus-5/xhigh, opus-5/**high**, opus-5/medium | 394 / 3.08 | 393 / 1.99 | 187 / 1.24 | 91 / 0.86 | 7.96 | +168 −27, 8 files | 2 (2 low) |
| p9-11-11-llm-dedup | opus-5/xhigh, opus-5/xhigh, opus-5/**high**, opus-5/medium | 486 / 2.41 | 432 / 3.16 | 144 / 1.11 | 68 / 0.60 | 7.97 | +251 −24, 4 files | 1 (low) |

**Actual production-code size** (the yardstick for "was this over-powered?"):

- p9-11-05 — **2 statements** in `packages/core/src/engine/primitives/table.ts`: `regex.lastIndex = 0;` and `if (options.files !== undefined && !fileMatches(...))` → `if (!fileMatches(...))`. Everything else is comments, 4 tests, a `mkdir(recursive)` in a test helper, 2 guide bullets.
- p9-11-06 — a 3-line `escapeRegExp` in `regex.ts`, one boundary group → lookahead in `ctx.ts`, `Set<string>` → `Map<string, RegExp>` in `ref.ts`. ~15 production lines.
- p9-11-08 — **one line**: ``.map((name) => `${name}/**`)`` → ``.map((name) => `**/${name}/**`)`` in `config-writer.ts`. The opus plan opened with exactly that: _"## The change in one line"_.
- p9-11-11 — ~40 production lines (`findingKey`, a dedup `Map`, `reportEntrypoint` → `collectEntrypointFindings`).

Cost model **verified** against every attempt to within 0.4%: `claude-opus-5` $5/$25 per MTok, `claude-sonnet-5` $3/$15, cache read 0.1×, cache write 2× (1-hour TTL). Confirmed with the `claude-api` skill (Current Models table + `shared/prompt-caching.md` economics). Worked example, p9-11-08 planning: 2,749,698 cache-read × $0.50/M + 105,448 cache-write × $10/M + 26,111 output × $25/M = **$3.082** vs recorded `$3.084`. This is what licenses the counterfactual arithmetic below.

---

## Findings, ranked by impact

### F1 — The batch is not homogeneous: two runs used Sonnet, two used Opus. The brief's flow pins describe only the second half.

- **category** model / spec-of-record · **severity** high (it invalidates the framing of the question) · **confidence** high

**EVIDENCE** — `request.json` is the authoritative record of what was asked for:

```
.worc/logs/p9-11-05-table-primitive-scope/stages/planning/run-000129/1-claude/request.json
  → {'model': 'claude-sonnet-5', 'reasoning': 'max', ...}
.worc/logs/p9-11-05-table-primitive-scope/stages/implementation/run-000130/1-claude/request.json
  → {'model': 'claude-sonnet-5', 'reasoning': 'xhigh', ...}
.worc/logs/p9-11-05-table-primitive-scope/stages/review/run-000132/1-claude/request.json
  → {'model': 'claude-opus-5', 'reasoning': 'xhigh', ...}

.worc/logs/p9-11-08-init-exclude-anchoring/stages/planning/run-000150/1-claude/request.json
  → {'model': 'claude-opus-5', 'reasoning': 'xhigh', ...}
.worc/logs/p9-11-08-init-exclude-anchoring/stages/review/run-000153/1-claude/request.json
  → {'model': 'claude-opus-5', 'reasoning': 'high', ...}
```

Corroborated independently by `prompt-audit/timeline.jsonl` for all 20 runs. The cut is clean at the task boundary: **p9-11-01 … p9-11-07** ran planning `sonnet-5`/`max`, implementation `sonnet-5`/`xhigh`, fixing `sonnet-5`/`max`, review `opus-5`/`xhigh`. **p9-11-08 … p9-12-06** ran planning/implementation/fixing `opus-5`/`xhigh`, review `opus-5`/`high`. `documentation` was `opus-5`/`medium` throughout; supervisor `sonnet-5`/`medium` throughout.

`ls -la /Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/` → `implementation.yaml` mtime **`Jul 28 00:21`**, i.e. the active flow was edited _during_ the campaign (p9-11-07 finished Jul 26, p9-11-08 started Jul 27 22:45). The file's current content matches the brief exactly — so the brief read the post-edit file and attributed it to all 20 runs.

**root cause** — The flow YAML is mutable and unversioned in the target's `.worc/`; the run record of what actually executed lives only in `request.json` / `prompt-audit`. `node_runs` records `provider_used` but **not** `model` / `reasoning`, so no SQL query can recover the model without touching the filesystem.

**lever** — Two, both in the orchestrator:

1. `src/wastech_orchestrator/state_store.py` (`node_runs` DDL, ~line 465) and `provider_attempts`: add `model TEXT` / `reasoning TEXT` columns so the audit tables are self-sufficient. `flow_fingerprint` already exists on `tasks` but does not resolve to per-node model/reasoning.
2. Operator practice: the frozen control bundle already captures this (`runs/control-bundles/<task-id>/`) but `logging.clean_runs_on_success: true` evicts it on success — the same gap the skill flags. Setting it to `false` for a campaign preserves the binding.

**scope** — orchestrator default (every repo). **expected impact** — future post-mortems can group by model in SQL instead of misattributing a config to a whole campaign.

---

### F2 — The cheaper tier was already tried and was measurably worse. Keep `claude-opus-5` on planning / implementation / fixing.

- **category** model · **severity** high · **confidence** medium-high (observational A/B, confound analysed below)

**EVIDENCE** — SQL over `node_runs` + `evaluations`, split at the Jul-27 config change. Declared task sizes from `docs/mdlint_v2/P11-remediation/index.md` are an identical mix in both halves (4× `S–M` + 3× `S` each):

|  | Sonnet-5 era (P11.01–07) | Opus-5 era (P11.08–14) |
| --- | --- | --- |
| runs | 7 | 7 |
| runs that took a review→fixing loop | **5 (71%)** | **2 (29%)** |
| `fixing` node runs | 6 | 2 |
| `high`-severity findings raised | **9** | **3** (across all 13 opus-era runs incl. P12) |
| `high` findings per run | **1.29** | **0.23** |
| `low` findings on `accept` per run | 1.9 (13 / 7) | **2.4** (31 / 13) |
| total spend | $109.17 | $142.02 |
| total wall time | 332 min | 272 min |

**The confound is real but points the wrong way.** The review node's reasoning also dropped `xhigh → high` at the same cut, so a naive reading is "the reviewer got laxer." That is contradicted by the last row: the Opus-era reviewer at _lower_ reasoning found **more** `low` findings per run (2.4 vs 1.9) while finding 5.6× fewer `high` ones. A less thorough reviewer does not simultaneously raise its nit rate. `gate_severity` was `high` (the default) in both eras, so the `high`-finding count is a like-for-like measure of blocking defects the implementer shipped.

**Counterfactual for these four runs, using the verified price model.** A pure model swap at identical token counts costs exactly **0.6×** (both input and output scale 5:3). For p9-11-08: node cost $7.169 (total $7.96 − $0.791 supervisor) → $4.30 on Sonnet, saving **$2.87 (36%)**. Against that, the observed rework tax: Sonnet-era rework rounds cost `fixing + extra review` of $10.47 (p9-11-02), $10.48 (p9-11-03); Opus-era $6.06 (p9-11-10), $11.69 (p9-11-14), $2.57 (p9-12-05) — mean ≈$10.5 vs ≈$6.8. Expected cost per run at base $7.2: **Opus $7.2 + 0.23 × 6.8 = $8.76; Sonnet $4.3 + 0.71 × 10.5 = $11.76.** Opus wins in expectation _even on the cheapest runs_, because the rework-probability gap dominates the 40% unit saving.

**root cause** — Not a defect; a settled experiment whose result was not written down anywhere on the branch, so it is at risk of being re-litigated.

**lever** — `.worc/flows/implementation.yaml`: **keep as-is** for `planning` / `implementation` / `fixing` (`claude-opus-5`, `reasoning: xhigh`). Do _not_ downgrade to `claude-sonnet-5` on the strength of the small-diff argument. If the operator wants the finding preserved, the honest home is a note in the flow YAML's per-node tuning comment block (lines 34–42 already document the slots) recording that Sonnet-5 was measured at 71% rework on the same task class.

**scope** — target-only (this is target-specific evidence; the packaged flow ships no model pins at all, which is correct). **expected impact** — avoids re-running a $30+ experiment; protects a measured 3× reduction in rework.

---

### F3 — The planning cost/wall-time inversion was `reasoning: max`, not the planning role and not the task size. It is already fixed.

- **category** reasoning · **severity** high (as a correction to brief signal #7) · **confidence** high

**EVIDENCE** — planning ÷ implementation, all 14 P11 runs:

| run | plan s | impl s | ratio | plan $ | impl $ | ratio | plan output tok |
| --- | --- | --- | --- | --- | --- | --- | --- |
| p9-11-01 | 943 | 264 | 3.57 | 3.04 | 1.47 | 2.07 | 85.5k |
| p9-11-02 | 810 | 245 | 3.31 | 3.17 | 1.67 | 1.90 | 73.5k |
| p9-11-03 | 857 | 282 | 3.04 | 3.32 | 1.75 | 1.90 | 79.4k |
| p9-11-04 | 1306 | 378 | 3.46 | 5.63 | 2.90 | 1.94 | 120.8k |
| **p9-11-05** | **883** | **123** | **7.18** | **3.28** | **0.88** | **3.73** | **80.8k** |
| p9-11-06 | 715 | 255 | 2.80 | 2.37 | 1.37 | 1.73 | 63.9k |
| p9-11-07 | 952 | 208 | 4.58 | 3.18 | 1.03 | 3.09 | 85.9k |
| p9-11-08 | 394 | 393 | **1.00** | 3.08 | 1.99 | 1.55 | **26.1k** |
| p9-11-09 | 774 | 1018 | 0.76 | 4.72 | 10.57 | 0.45 | 56.2k |
| p9-11-10 | 599 | 1004 | 0.60 | 4.27 | 9.68 | 0.44 | 46.2k |
| **p9-11-11** | **486** | **432** | **1.12** | **2.41** | **3.16** | **0.76** | **37.0k** |
| p9-11-12 | 1122 | 523 | 2.15 | 3.58 | 4.13 | 0.87 | 39.0k |
| p9-11-13 | 558 | 834 | 0.67 | 4.31 | 7.77 | 0.55 | 37.5k |
| p9-11-14 | 658 | 1957 | 0.34 | 5.65 | 27.47 | 0.21 | 47.6k |
| **Sonnet/`max` mean** |  |  | **3.99** |  |  | **2.34** | **84.2k** |
| **Opus/`xhigh` mean** |  |  | **0.95** |  |  | **0.69** | **41.4k** |

The inversion is 100% inside the Sonnet-`max` era. It was **not more exploration** — planning tool calls were comparable (`p9-11-05`: 66 = 28 Read + 27 Grep + 10 Glob; `p9-11-08`: 55 = 26 Read + 24 Grep + 4 Glob). It was **2× the reasoning/output tokens** (84.2k vs 41.4k) at 2× the wall clock. The plans themselves inflate with it: `plan.md` is 280 lines / 14,427 bytes on p9-11-05 vs **170 lines / 11,201 bytes** on p9-11-08 — the smaller plan for the smaller change, on the stronger model.

The `claude-api` skill's own guidance (verified via the Skill tool) matches: `max` "can deliver gains in some use cases but may show diminishing returns from increased token usage; can be prone to overthinking", while `xhigh` is "the best setting for most coding and agentic use cases … used as the default in Claude Code."

**root cause** — `reasoning: max` on planning. Effort is the dominant control on planning verbosity and wall time; the model tier is secondary.

**lever** — `.worc/flows/implementation.yaml` → `nodes[planning].reasoning`: **keep `xhigh`. Do not reintroduce `max` on any node.** Residual: `p9-11-12` at 2.15× is the one Opus-era outlier (1122 s planning) — worth one look if the parent wants a per-node ceiling, but not a pattern (n=1/7).

**scope** — target-only. **expected impact** — none needed; this documents that the fix already landed, and prevents a regression toward `max`.

---

### F4 — p9-11-06 was accepted with `[]` while its diff carries the exact defect the same reviewer rated `medium`/`rework` on p9-11-07. The reviewer read the line.

- **category** prompt (evaluator calibration) · **severity** high · **confidence** high

**EVIDENCE — the defect.** `docs/mdlint_v2/P11-remediation/06-regex-substitution-safety.md`, lines 63–64 as committed:

```
63|- **M-1 fix (`ref.ts`)**: `REF-004`'s `allZones: Set<string>` became `zoneMentionRegexes: Map<string,|
64|RegExp>`, built in the same single pass over `context.projectFiles` the old `Set` used, guarded by|
```

Line 63 has an **odd number of backticks** — the inline code span opens and does not close. Line 64 starts at **column 0**, losing the 2-space list-content indent that every other continuation in the file (lines 56–62, 65–70) uses. This is verbatim recurring defect class A from the brief.

**EVIDENCE — the format gate is blind to it.** `.worc/logs/p9-11-06-regex-substitution-safety/checks/003.log`:

```
> wastech-mdlint@0.0.0 format
> prettier --check .
Checking formatting...
All matched files use Prettier code style!
```

`/Users/a1234/Documents/GitHub/wastech-mdlint/.prettierrc.json` is `{"singleQuote": false}` — no `proseWrap`, so Prettier's default `"preserve"` applies and the wrap is never touched.

**EVIDENCE — the reviewer saw it.** `stages/review/run-000138/1-claude/events.jsonl` contains the string in three forms:

```
zoneMentionRegexes: Map<string,\n+RegExp>`, built i     ← the diff hunk it was given
zoneMentionRegexes: Map<string,\n39\t+RegExp>`, bui     ← its own Read of current.diff, line 39
zoneMentionRegexes: Map<string,\n64\tRegExp>`, buil     ← its own Read of the FILE, line 64
```

The `\n64\t` form is a numbered `Read` of the phase doc itself: the reviewer opened the file, saw the column-0 continuation at line 64, and returned `findings: []`.

**EVIDENCE — the same reviewer flags this class elsewhere.** `SELECT findings_json FROM evaluations WHERE kind='in_flow_verdict'`:

- p9-11-07, **`rework` / `medium`**, `docs/mdlint_v2/P11-remediation/07-custom-missing-id.md`: _"In the 'Implementation notes' first bullet, the continuation line … starts at column 0 instead of the 2-space list-content indent used by every other continuation line in the file."_
- p9-11-03, `rework` / `low`, `README.md`: _"the inserted sentence pushed the line break inside the `` `--on-existing merge` `` code span … so the continuation line loses the file's 2-space list indent. Prettier preserves multi-line code spans verbatim, so the format gate will not correct it."_
- p9-11-02, `rework` / `low`, `docs/guide/rules/SEC-003.md`: _"splits an inline code span across a line break, and the continuation line … starts at column 0."_

p9-11-06 and p9-11-07 ran the **same model at the same reasoning** (`claude-opus-5` / `xhigh`), the **same** `review.md`, on the **same file kind and the same section** (`## Implementation notes` of a P11 phase doc). One got `medium`/rework; the other got `[]`.

**root cause** — Two compounding causes, both in the target's active `review.md`:

1. **No floor.** Nothing in the prompt names a class of finding that must always be reported. Severity assignment and whether to report at all are both free, so a defect worth `medium` on Monday is worth nothing on Tuesday.
2. **A blind spot the prompt creates itself.** `.worc/flows/implementation/review.md:15`: _"The diff you see is captured **before** the documentation step runs, so do not block on a phase doc not yet flipped to Done or missing 'Implementation notes' — that is the documentation step's job, not a defect in this change."_ On this run the _implementation_ node had already written Status→Done and the Implementation notes (confirmed by the doc node's own report: _"Status/exit criteria were already flipped by the code step"_), so the reviewer was looking at a phase doc it had been told was somebody else's job.

**lever** —

- **Target copy** `/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/review.md`:
  - Line 15 — narrow it from "the phase doc is the documentation step's job" to "do not flag those as **missing**". Content quality of any file _in the diff_ stays in scope.
  - Add to `## Requirements And Correctness` a named always-report item: _"Markdown source hygiene in every edited `.md`: a line break inside an inline code span, or a list-continuation line that loses its indent, is always a finding (`low`) — the Prettier gate cannot see either (`proseWrap: preserve`)."_
- **Packaged Aug-3 default** `src/wastech_orchestrator/packaged/flows/implementation/review.md`: **partially fixes the blind spot already.** The diff replaces target line 15 with: _"Documentation, changelog, and status-doc updates run in a later step of this flow, so do not flag those as missing."_ That is the narrower wording recommended above. It does **not** add the always-report floor — that gap exists in both copies.
- **Note on drift direction, correcting brief signal #9:** for the five main roles the _target_ copies are **newer** (`Jul 25 23:30–23:32`) than the packaged ones (`Jul 25 22:41`) — the drift is target-side customization for mdlint, not packaged-side evolution. Only `summary.md` and `supervisor.md` are newer in `packaged/` (`Aug 3 14:07`).

**scope** — the floor: orchestrator default (every repo suffers a format gate that cannot see prose wrap). The line-15 narrowing: target-only (packaged already has it). **expected impact** — removes the single largest source of reviewer non-determinism on doc-touching diffs; converts the 0/1/2/2 spread into a predictable floor.

---

### F5 — `review.md` never states the accept/rework rule or the severity vocabulary. The real gate lives in code at `gate_severity: "high"`.

- **category** prompt / config · **severity** high · **confidence** high

**EVIDENCE — what the prompt does say** (`.worc/flows/implementation/review.md`):

- Line 1: _"Report each finding with a severity, and mark anything that must change before merge as **blocking**. Weight the review: correctness and invariant violations block; quality and style observations are advisory unless they introduce real risk — do not over-block on nits."_
- Line 14: _"No findings means the diff is clean — return an empty `findings` array, not prose."_
- Lines 25–34: a well-specified enumerated `## Blocking Invariant Violations` list (nondeterminism, zero test coverage, undeclared dependency, `info` severity / library `process.exit`, core-ownership violation, scope drift).
- Lines 46–52: `## Test Coverage` explicitly marked _"Advisory (raise these, but do **not** block on them unless a real correctness risk is untested)"_.

**EVIDENCE — what it does not say.** `grep -ni 'accept\|rework\|verdict' .worc/flows/implementation/review.md` returns only line 7 ("the downstream LLM agent that will do the rework") and line 19 ("the plan's acceptance criteria"). **There is no statement of the verdict rule and no enumeration of the severity values.**

**EVIDENCE — where the rule actually lives.** `src/wastech_orchestrator/core/flow/schema.py:26-30`:

```python
SEVERITY_ORDER: tuple[str, ...] = ("blocking", "critical", "high", "medium", "low")
#: Default evaluator gate: block on ``high`` and above (``high``/``critical``/``blocking``), leaving
#: ``medium``/``low`` advisory — the historical, hardcoded behavior.
DEFAULT_GATE_SEVERITY = "high"
```

`src/wastech_orchestrator/core/flow/nodes/evaluator.py:289-293`:

```python
gate_rank = _severity_rank(node.gate_severity)
if not any(self._is_blocking(f, gate_rank) for f in raw_findings):
    return "accept", False
```

The output schema (`evaluator.py:117`) offers all five enum values; the observed findings across all 20 runs use only `high` / `medium` / `low`. `.worc/flows/implementation.yaml` does not set `gate_severity` on the `review` node, so the default `high` applies.

**root cause** — A vocabulary mismatch between prompt and gate. The prompt's mental model is binary (**blocking** vs advisory); the gate's model is a 5-value ordinal with a threshold at `high`. An evaluator that considers something must-fix but writes `severity: "medium"` — the natural label for "important but not catastrophic" — produces a **silent `accept`**. That is the precise mechanism behind brief signal #1 (62 findings raised on `accept`, 44 low + 18 medium, never fixed): **by construction, every `medium` on an `accept` was un-actionable.** My batch contributes one: p9-11-05's `medium` on `ColumnUniqueOptions.files` being dead.

**lever** — `.worc/flows/implementation/review.md` (target) and `src/wastech_orchestrator/packaged/flows/implementation/review.md` (default — the packaged Aug-3 copy has the same omission, so this is **not** already fixed): add an explicit severity contract, e.g.

> Severity is one of `blocking` | `critical` | `high` | `medium` | `low`, and it is load-bearing: a finding at **`high` or above sends the diff back for rework**; `medium` and `low` are recorded as advisory and no step in this flow will fix them. If a finding must be fixed before merge, say `high` — do not soften it to `medium`.

Complementary config lever, if the operator wants medium findings to gate: `.worc/flows/implementation.yaml` → `nodes[review].gate_severity: medium` (already supported per `schema.py:121-126`; no code change needed). Trade-off: on these four runs that would have turned p9-11-05 from `accept` into one extra rework round (≈+$6), and left 06/08/11 untouched.

**scope** — orchestrator default. **expected impact** — makes the verdict predictable and eliminates the "must-fix labelled medium" silent-accept path; directly attacks the 62-orphaned-findings number.

---

### F6 — The `documentation` node's output is never reviewed by anything, and neither role prompt has any markdown-wrap discipline.

- **category** flow / prompt · **severity** medium · **confidence** high

**EVIDENCE — the graph.** `.worc/flows/implementation.yaml` edges: `{ from: review, to: documentation, outcome: accept }` then `{ from: documentation, to: publish }`. `documentation` has `permission_profile: workspace-write` and its edits join the same commit. There is no evaluator or checks node after it.

**EVIDENCE — it is a substantial doc author.** On p9-11-06 the documentation node wrote **all three** doc files in the final diff — `events.jsonl` tool calls: `Edit docs/mdlint_v2/P11-remediation/06-regex-substitution-safety.md`, `Edit docs/guide/rules/REF-004.md`, `Edit docs/guide/rules/CTX-003.md`. Its own report (`documentation.out.md`): _"`docs/guide/rules/CTX-003.md:97` — note that alias boundaries are asserted rather than consumed."_

**EVIDENCE — an unreviewed inaccuracy it introduced.** The committed `docs/guide/rules/CTX-003.md` bullet says:

```
+- The word boundaries around an alias are only _checked_, not consumed, so repeated aliases are
+  counted independently even when they are back to back: `gql gql gql` reports three findings, not
+  two.
```

Only the **trailing** boundary became a lookahead. `packages/core/src/engine/rules/ctx.ts:76` still consumes the leading one:

```ts
return new RegExp(`(^|[^A-Za-z0-9_])(${escaped})(?=[^A-Za-z0-9_]|$)`, "g");
```

The user-visible claim (`gql gql gql` → 3) is correct — I hand-simulated it: `^`+`gql` [0,3); `' '`+`gql` [3,7); `' '`+`gql` [7,11) — but the stated _reason_ ("boundaries … only checked, not consumed") is false for `match[1]`. This is precisely the overstated-invariant class the reviewer **did** catch on p9-11-11 ("_The rationale comment above `findingKey` … is not true for all three finding shapes_"). Nothing reviewed the doc node, so nobody caught it here.

**EVIDENCE — no wrap guidance in either copy.** `.worc/flows/implementation/documentation.md:1` says only _"Follow the project's existing documentation conventions and formatting; if the project ships a docs formatter or linter, run it"_, and line 5 ends _"Run the project's Prettier if it is configured (`npm run format`)"_. `grep -n "wrap\|line break\|code span\|indent\|column"` finds nothing in either the target copy or `packaged/flows/implementation/documentation.md`. The doc node did run `npm run format` (its Bash log: `npm run format 2>&1 | tail -5 && git diff --stat`) and reported _"Prettier passes"_ — which is exactly the problem: the prompt's only quality instrument is the one gate that provably cannot see the defect.

**root cause** — The flow reviews the _code_ step's output and then lets a workspace-write agent append doc prose straight to `publish`, with a formatter as its sole check.

**lever** — ranked cheapest-first:

1. **Prompt (both copies)** — add one line to `documentation.md`: _"Match the file's existing wrap width and never let a line break land inside an inline code span; a continuation line inside a list item keeps the item's content indent. A Prettier config without `proseWrap` will not fix either for you."_ This is a shipped-default-worthy change: the same trap exists in any repo that hard-wraps prose under default Prettier. **Packaged Aug-3 does not fix it.**
2. **Flow (target-only)** — add a second evaluator after `documentation` in `.worc/flows/implementation.yaml`, with `blocking: true`, `gate_severity: medium`, and a `doc_review` role file, edged `documentation → doc_review`, `doc_review → publish (accept)`, `doc_review → documentation (rework, loop: review_fix)`. This is legitimate per the packaged flow's own precedent (its header comment: _"An optional non-blocking `testing_quality` evaluator … is NOT shipped in this default flow: it is a pure graph-shape choice — an operator who wants it adds the node to their own flow YAML"_). Cost: one more `opus-5` evaluator pass, ≈$1.1–2.2/run on this workload. Recommend only if F6.1 proves insufficient.

**scope** — prompt line: orchestrator default. Extra node: target-only. **expected impact** — closes the one unreviewed write path in the flow; removes the recurring defect class A at its most common source.

---

### F7 — A confirmed, still-open defect the pipeline flagged, half-fixed, and shipped: `llm.ts` carries a claim its own reviewer proved false.

- **category** flow / diff · **severity** medium · **confidence** high

**EVIDENCE.** The p9-11-11 review finding (`evaluations.findings_json`, `accept` / `low`):

> _"The rationale comment above `findingKey` ('Every LLM-001 `data` payload is derived 1:1 from the message it accompanies (raw/resolved target, cycle path)') is the stated safety argument for first-writer-wins dedup, but it is not true for all three finding shapes: the over-budget finding carries `data.importedFiles`, which appears nowhere in its message. … The same overstatement is repeated in `docs/mdlint_v2/P11-remediation/11-llm-dedup.md`."_

The documentation node fixed the **doc** half and said so in `documentation.out.md`:

> _"The same overstatement exists in the code comment above `findingKey` (`packages/core/src/engine/rules/llm.ts:115-118`). I am read-only to code in this step, so it is still there and still wrong. That is a follow-up for a code step."_

The supervisor emitted a follow-up (`summary.json`, severity `low`) naming the file and the risk. **And the claim is still in HEAD today:**

```
$ grep -n "derived 1:1 from the message" packages/core/src/engine/rules/llm.ts
136:// payload is derived 1:1 from the message it accompanies (raw/resolved target, cycle path), so equal
$ grep -n "importedFiles" packages/core/src/engine/rules/llm.ts
191:        importedFiles: traversal.importedPaths.size,
```

So the phase doc now says the right thing and the code comment says the wrong thing — the diff shipped **internally inconsistent**, and every layer of the pipeline knew.

**root cause** — Three constraints intersecting, each individually correct: (a) the finding was `low`, so `gate_severity: high` accepted it (F5); (b) the only post-review write step is `documentation`, which is contractually read-only to code (`documentation.md:7`: _"You are read-only to the code … your only job is editing docs files"_); (c) nothing re-enters `fixing`.

**lever** — This is the concrete case for F5's `gate_severity` / severity-contract change, plus the follow-up-register change in F8. A narrower alternative worth considering: an optional `nodes[fixing].when`-gated closing pass is **not** available (see F10), so the sound levers are the severity contract or an operator-side follow-up queue.

**scope** — orchestrator default (F5) + target practice (F8). **expected impact** — findings the pipeline itself identified stop shipping as known-wrong code.

---

### F8 — Follow-ups are surfaced perfectly (98/98) but the only repo-visible register self-compacts. Corrects brief signal #1.

- **category** infra / config · **severity** medium · **confidence** high

**EVIDENCE.** `supervisor.emit_follow_ups: true` is set in the flow. Across all 20 runs the supervisor emitted **98 follow-ups**, and **every single one** appears in that task's `pr_body_appended.md`:

```
p9-11-05  follow_ups=4  in_pr_body=4  body=55054
p9-11-06  follow_ups=1  in_pr_body=1  body=51909
p9-11-08  follow_ups=4  in_pr_body=4  body=57838
p9-11-11  follow_ups=2  in_pr_body=2  body=55932
...
TOTAL follow_ups=98  landed_in_pr_body=98
```

They are high quality — p9-11-11's carries `title`, `rationale`, `severity`, `paths`, `evidence` (quoting the doc node's own report), and `action_hint`.

**But** the chain PR body is a self-compacting rolling document. `src/wastech_orchestrator/git_manager.py:244` `_GITHUB_PR_BODY_LIMIT = 65_536`; `git_manager.py:391` `_bound_pr_body`:

> _"Bounds the body by **compacting the oldest task sections** — replacing each one's summary with a one-line stub pointing at `logs/<id>/summary.md` — from oldest toward newest until it fits."_

Observed bodies grow 15,001 → 47,490 bytes over p9-11-01…04 and then plateau/oscillate at **46,928–59,487 bytes** — i.e. the campaign has been running inside the compaction regime since roughly p9-11-05. The stub points at `logs/<id>/summary.md`, and in this target `.worc/` is gitignored, so **the durable copy of every early follow-up is outside the repo**.

**root cause** — Not an orchestrator defect (the compaction is deliberate, documented, and lossless on disk). The gap is that the only _repo-visible_, reviewable register of follow-ups is a channel designed to shed the oldest entries first, and there is no step that converts a follow-up into a queued task.

**lever** —

1. **Operator, immediate:** harvest `.worc/logs/*/summary.json → follow_ups` into a tracked backlog file before the next campaign (98 items, already structured with severity/paths/evidence).
2. **Orchestrator, optional:** the `worc-task` skill already converts free-form text into a valid task file. A `follow_ups → tasks/preparing/` materialization step would close the loop without touching the state machine or the "only the orchestrator publishes" invariant. Would need a backlog item, not a prompt tweak.
3. **Do not** raise `_PR_BODY_MAX_CHARS` — the compaction is correct behavior against a hard GitHub limit.

**scope** — target practice + one orchestrator feature. **expected impact** — the 98 already-diagnosed items become actionable instead of decaying out of the PR body.

---

### F9 — The planning role prompt has exactly one proportionality clause, and it is about output length, not effort. Neither copy has one for exploration.

- **category** prompt · **severity** low-medium · **confidence** high

**EVIDENCE — the clause exists** (`.worc/flows/implementation/planning.md:8`):

> _"Keep it concrete and no longer than an implementer needs to execute without re-deriving the approach."_

That is the **only** proportionality instruction, and it constrains the plan's length. Everything above it is unconditional maximalism (lines 12–19, `## Explore Before You Plan`):

> _"Trace the relevant code paths end to end — real call sites, types, and package boundaries — so the plan never assumes an interface that isn't there. **Verify every path you cite against the current tree.**"_ _"Find the conventions and patterns this change must follow, and **name a similar existing feature** to model the work on…"_ _"When you enumerate a product surface a downstream author will reference …, bind each item to the specific command or type that owns it and **cite the source line**."_

There is no clause of the form "if the task already names the exact lines to change, verify them and stop." `diff -u` against `src/wastech_orchestrator/packaged/flows/implementation/planning.md` (Aug-3 default) shows the changes are all mdlint-specific de-scoping (removing the `## Roadmap And Architecture` block and the core-primitives list) — **the packaged default does not add a proportionality clause either.**

**Judgement: the prompt is not the main culprit here.** Under Opus/`xhigh` the same prompt produced a plan that opened with _"## The change in one line"_ (p9-11-08 `result.json`) and _"## Start here (the only uncertain part)"_ — exactly proportional behaviour, at 170 lines for a one-line fix. The prompt's absence of an effort clause was only exploitable at `reasoning: max` (F3). So this is a **hardening** finding, not a fix-now finding.

**lever** — `packaged/flows/implementation/planning.md` and the target copy, appended to line 8:

> _"Scale the plan to the change. When the task already names the file, the lines, and the fix, your job is to verify those references against the tree, name the one thing most likely to make the fix wrong, and stop — not to re-derive the surrounding design."_

**scope** — orchestrator default. **expected impact** — modest; protects the observed Opus behaviour from prompt drift and would have cut ~40% off p9-11-05's planning output had it been present.

---

### F10 — A "small task" gate is _not_ an available lever, and should not be built. Stating this so it is not proposed.

- **category** flow / architecture · **severity** informational · **confidence** high

Three candidate levers for "skip or shorten planning on small tasks", assessed against `.agents/rules/` and the `no-hardcoding-flow-agnostic-engine` memory:

1. **A `when` gate on the planning node — not available and not acceptable.** `WhenPredicate` is `{fact: str, equals: bool}` ("_Deterministic skip predicate: node is skipped when `fact != equals`_", `core/flow/schema.py`). The full fact vocabulary is two entries (`core/orchestrator.py:3155-3179`): `derived.needs_refinement` and `config.external_research`; _"unknown fact → default off"_, so a made-up fact would silently **always** skip the node. Adding a `derived.task_is_small` fact would be the engine classifying task shape — exactly the branching `no-hardcoding-flow-agnostic-engine` forbids. **Not a valid finding; do not propose it.**
2. **A code-side size heuristic — same objection, worse.** Any threshold on diff size, file count, or task-file length is the core reasoning about task content.
3. **Per-task node disable — available, documented, and human-decided.** `implementation.yaml` header: _"Disabling a node is PER-TASK, not here: set `nodes.<id>.enabled: false` in the task file … (driven by node id, not a `when` fact — the bounded per-task exception, PRE.3)."_ For a genuinely trivial task (p9-11-08's one-line glob change) the operator can author the task with `nodes.planning.enabled: false`, which would have saved **$3.08 and 394 s** on that run. This is the rule-compliant answer, and it keeps the judgement with the human.

**expected impact** — up to ~40% of a small run's cost, at the operator's discretion and risk. Note the risk is real: on p9-11-08 the plan's "start here" item (does a leading `**/` still match a root-level segment?) became the test that pins the fix's one actual hazard.

---

## What's already good

- **Prompt caching is working and correctly instrumented.** 97–99% of input is cache-read on every attempt; the recorded `usage_cost` reconciles to the published price model to within 0.4% across all 16 node attempts in this batch, including the 2× 1-hour-TTL cache-write premium.
- **The retune from Sonnet to Opus was the right call and is visible in the data** — 71% → 23% rework, 1.29 → 0.23 `high` findings per run, at ~flat cost per run and _lower_ total wall time.
- **The `## Blocking Invariant Violations` half of `review.md` is genuinely well calibrated** — six named, checkable classes with explicit "this is advisory, do not block" carve-outs for test-coverage polish. The problem is the _un_-enumerated half, not this one.
- **Both non-empty reviews in this batch were correct and non-trivial.** p9-11-05's `medium` (the now-dead `ColumnUniqueOptions.files`) is a genuine "same failure class, one level up" catch. p9-11-08's low about `load-documents.test.ts` is right: every fixture file in that case (`packages/foo/node_modules/lib/x.md`, `packages/foo/dist/out.md`, `node_modules/root-lib/y.md`) is also rejected by the file-level `matchesConfigGlob` filter, so the test cannot fail on the directory prune its comment claims to exercise. p9-11-11's low is right too (verified in F7).
- **The three non-p9-11-06 diffs hold up under independent review.** I re-derived the semantics myself: `regex.lastIndex = 0` before each per-row `test()` is the correct minimal fix for a `g`/`y` `columnMatches` (and the hoisted `zoneMentionRegexes` in `ref.ts` is safe precisely because it carries **no** flags, so `test()` never touches `lastIndex` — the same trap p9-11-05 fixed, correctly avoided here); `**/<name>/**` does match zero leading segments; the CTX-003 lookahead does yield 3 matches on `gql gql gql` and 0 on `gqlgql`; `match.index + match[1].length` still computes the right offset after the trailing group became non-capturing. **Only p9-11-06 has a defect the reviewer missed, and it is markdown source, not behaviour.**
- **The supervisor is the sharpest layer in the pipeline.** Its per-step advisory on p9-11-05 independently noticed the deliberate override of the source research report and correctly ruled the task file outranks it; its follow-ups quote the doc node's own admissions as evidence. All 98 reached the PR body.
- **The documentation node is honest about its own limits** — _"I ran only `npm run format` … I did not run typecheck, tests, or the build, so the 'all green' exit-criteria box in the phase file reflects the code step's verification, not mine"_ (p9-11-11). That is exactly the behaviour the role prompt asks for.
- **Recurring defect class C (exit criteria ticked without verification) did not recur in this batch.** p9-11-08's four criteria are each backed by a new assertion, including a genuine red→green e2e (`init.e2e.test.ts`, _"That e2e failed against the pre-fix build with exactly the audit's reported corpus"_), and the checks did pass for all four runs (`check_runs`: 0 failures, 0 timeouts).

---

## Data gaps

1. **`node_runs` / `provider_attempts` do not record `model` or `reasoning`.** Recovering what actually ran required reading 40 `request.json` files. This is what made F1 possible to miss. (Lever in F1.)
2. **`usage_reasoning_output` is NULL on every attempt** (already in the brief). It matters more than the brief implies: F3's whole argument rests on total `usage_output_total` as a proxy for thinking volume. With reasoning tokens broken out, the Sonnet-`max` vs Opus-`xhigh` comparison would be direct rather than inferred.
3. **`runs/control-bundles/` and `runs/exchange-seals/` are absent** for all four runs — expected, `logging.clean_runs_on_success` defaults to `true` and every task ended `done`. Consequence: I cannot see the frozen flow snapshot each run was bound to, which is the artifact that would have made F1 a one-line check. Recommend `logging.clean_runs_on_success: false` for the next campaign.
4. **The A/B in F2 is observational, not randomized.** The Sonnet era was also the _first_ 7 tasks of the campaign, on a branch with less accumulated prior work, and the review reasoning changed at the same cut. I de-confounded the reviewer-laxity hypothesis (the Opus-era reviewer raised _more_ low findings per run) but cannot rule out an ordering/learning effect. Confidence is medium-high, not high.
5. **`memory.enabled: false`** throughout, so the `{?memory_path}` block in every role prompt was never rendered. Whether repository memory would have suppressed the repeat markdown-wrap defects (flagged on 02, 03, 07, 12 and missed on 06) is untested — and it is the cheapest untried lever for recurring defect class A.
6. **I did not verify the p9-11-06 fix by execution.** `ctx.ts`/`ref.ts` semantics above are hand-derived from source plus the passing suite (`checks/004.log`, vitest green); I did not run the tests myself, per the analysis-only constraint.
