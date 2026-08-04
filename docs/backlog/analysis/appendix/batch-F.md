# Batch F — P12.01 / P12.02 / P12.03 post-mortem (+ P12.04 cross-check)

## Verdict

All three runs are clean: `done`, attempt 1, zero provider retries, zero fallbacks, all checks green, scope-disciplined diffs. **The double `planning` on `p9-12-01` is a HITL approval round-trip — an approval request the `planning` node itself raised, answered 2h later by a human via Telegram, after which the orchestrator re-invoked the node as a second `node_run` with `--resume`. It is architecturally correct and the wait is not the orchestrator's fault; the waste is that the resume re-primed an _expired 1-hour prompt cache_, so 86% of that second run's $1.70 bought nothing.** `p9-12-04` is the same mechanism and missed the cache TTL by 5 minutes 25 seconds, paying $1.64 for it.

The single most valuable finding is not the double planning, though: **`p9-12-02` accepted with `findings: []`, and its diff writes a factually false claim into the repository's canonical vocabulary document — then the `documentation` node explicitly re-verified that same claim as "accurate".** Three gates, one shared blind spot.

---

## Run frames

Levers referenced live in `/Users/a1234/Documents/GitHub/wastech-orchestrator`; symptoms in `/Users/a1234/Documents/GitHub/wastech-mdlint/.worc`.

Effective per-token rates, **derived from the artifacts** (3-point solve, reconciles to 0.03% — e.g. `p9-12-01` implementation predicted $7.637 vs recorded $7.6392) and independently confirmed against the `claude-api` skill: `claude-opus-5` = **$5.00/Mtok input, $25.00/Mtok output**; cache read = 0.1× base = **$0.50/Mtok**; cache write at **1-hour TTL = 2× base = $10.00/Mtok** (5-minute TTL would be 1.25× = $6.25). The runs use the 1h tier — every attempt reports `"cache_creation": {"ephemeral_1h_input_tokens": N, "ephemeral_5m_input_tokens": 0}`.

### `p9-12-01-exclude-coverage` — $18.77, 19 files, 886 `+` lines

| node | run | wall | USD | cache read | cache **write** | output |
| --- | --- | --- | --- | --- | --- | --- |
| planning | 000198 | 475s | 3.6185 | 2,942,517 | 136,466 | 31,210 |
| **planning** | **000199** | **82s** | **1.6980** | **145,469** | **146,231** | **6,519** |
| implementation | 000200 | 777s | 7.6392 | 9,296,283 | 179,723 | 47,289 |
| testing (checks) | — | 25s | 0 | — | — | — |
| review | 000202 | 412s | 3.2996 | 2,477,376 | 139,122 | 26,574 |
| documentation | 000203 | 212s | 1.5739 | 1,429,362 | 59,575 | 10,435 |
| supervisor (6 calls) | — | 97s | 0.9420 | — | — | — |

Path: `planning → [HITL approval, 2h01m human wait] → planning → implementation → testing → review(accept, 1 medium) → documentation → publish`. Extra dir vs the others: **`logs/<task>/hitl/planning.json`** — that is the whole answer to "what is the 15th entry".

### `p9-12-02-glossary-custom-target` — $5.21 (cheapest of 20), 3 files, 49 `+` lines

planning 231s/$1.4669 · implementation 286s/$1.6033 · testing 26s · review 129s/$0.7788 (**`{"findings":[]}`**) · documentation 103s/$0.8066 (**zero edits**) · supervisor $0.552.

### `p9-12-03-quadratic-hotpaths` — $8.04, 12 files, 541 `+` lines

planning 438s/$2.19 · implementation 530s/$3.4037 · testing 21s · review 123s/$0.9622 (accept, 2 low) · documentation 91s/$0.723 · supervisor $0.759.

### `p9-12-04-mcp-custom-rules` (cross-check) — $17.04

planning 674s/$5.1588 → **HITL approval, 1h05m25s wait** → planning 122s/**$2.1269** (cache read 482,609 / cache **write 163,905**) → implementation 576s → review 499s (accept) → documentation 187s. Also has `hitl/planning.json`.

---

# Findings

## F1 — THE DOUBLE PLANNING: HITL approval + prompt-cache TTL expiry. 86% of the second planning run bought nothing.

**Category** infra/config + spec · **Severity** medium (correctness fine; $2.95 of pure waste across two runs, ~9% of each run's own cost) · **Confidence** high — mechanism proven from `argv`, `context_paths`, `session_id`, and the source.

### Evidence — the mechanism, step by step

**1. It is not a retry.** `stage_attempts=1` on both `node_runs` rows and `attempt=1` on both `provider_attempts` rows; both `status='succeeded'`, `error_class` NULL, `route_fallback` never taken. Two distinct node executions.

**2. The `planning` node asked for an approval.** `logs/p9-12-01-exclude-coverage/stages/planning/run-000198/1-claude/result.json` → `structured_output` carries **both** a complete plan and a `human_input` block:

```
structured_output keys: ['content', 'human_input', 'decompose', 'subtasks']
   decompose= False subtasks= []
   content len= 11257
   human_input= {"kind": "approval", "question": "May the implementer include a one-line
   product-code fix in `packages/core/src/engine/rules/tbl.ts:156` as part of this test-only task?...
```

**3. It was delivered to a human and answered.** `logs/p9-12-01-exclude-coverage/hitl/planning.json`:

```json
{
  "answer": "approved",
  "approved": true,
  "status": "consumed",
  "deadline": 1785240633.373987,
  "node_id": "planning",
  "handle": {
    "delivered": true,
    "kind": "approval",
    "message_id": 1556,
    "interaction_id": "hc92b391036195bce8780b714"
  },
  "telegram_message_id": 1556,
  "failure": null
}
```

`deadline` = `2026-07-28T12:10:33Z` = exactly 8h after planning #1 ended → `telegram.ask_timeout_s: 28800` in `.worc/config.yaml:129`.

**4. The second run resumed the same session, with the answer added as one extra context path.** `run-000199/1-claude/request.json`:

```
context_paths:
  task_path = '[REDACTED]/.worc-io/p9-12-01-exclude-coverage/task.md'
  human_input_path = '[REDACTED]/.worc-io/p9-12-01-exclude-coverage/hitl/planning.answer.json'
argv contains: --resume [REDACTED]
```

`session_id` is identical across both results (`session:d5ea3f715a11`). `diff run-000198/rendered-prompt.md run-000199/rendered-prompt.md` is **one line**:

```
> - human_input: /Users/.../.worc-io/p9-12-01-exclude-coverage/hitl/planning.answer.json
```

**5. The code path.** `src/wastech_orchestrator/core/flow/nodes/agent.py:140-187`, `_run_with_hitl` — the comment states the design intent verbatim:

```python
# First-time signal: one durable round-trip, then re-run with the answer.
result = self._gate().request(...)
self._require_human(node, typed.human_input.kind, result)
# Resume the first run's session so the agent continues the same conversation with the
# operator's answer (it does not re-derive from scratch). Same-provider only; across a
# restart the first-run outcome is gone, so resume falls back to fresh + the answer file.
run_id2, outcome2 = self._invoke_with_turn_gate(
    node, ctx, route,
    human_input_path=self._exchange_human_input(node, ctx, path),
    resume_session_id=_same_provider_session_id(outcome, route), ...)
```

Second `_invoke_with_turn_gate` → second `node_runs` row for the same node. That is the only mechanism that produces two rows here; a HITL-capable node is the only node type that reaches it (`_wants_hitl(node)`, line 120), and `planning` is the only such node in the active flow — `.worc/flows/implementation.yaml:52`, `hitl: { allow_question: true, allow_approval: true }`.

### Was it correct? Yes. Was it wasteful? Partly, and precisely measurably so.

The re-run was **substantive**, not ceremonial: the plan grew 11,257 → 12,266 chars and was restructured around the decision (the conditional step 10 "conditional on approval" was promoted to step 1 "do this first; it is the only product-code change", and 9 subsequent steps renumbered). So the second invocation earned its 6,519 output tokens.

What it did **not** earn is the cache write. Cost decomposition of run-000199's $1.6980:

| component            | tokens  | rate     | USD        | share     |
| -------------------- | ------- | -------- | ---------- | --------- |
| cache **write** (1h) | 146,231 | $10.00/M | **1.4623** | **86.1%** |
| cache read           | 145,469 | $0.50/M  | 0.0727     | 4.3%      |
| output               | 6,519   | $25.00/M | 0.1630     | 9.6%      |
| uncached input       | 4       | $5.00/M  | 0.0000     | —         |

**Root cause.** The human took **2h 01m 31s** (planning #1 finished `04:10:32Z`, planning #2 started `06:12:03Z`). The 1-hour cache entries written by planning #1 had expired ~1h earlier, so `--resume` re-sent the entire ~146K-token transcript and re-wrote it at 2× base rate. And the write has **zero future readers** — the node completes and the flow moves to `implementation`, a `fresh_disposable` session with a different prompt. The orchestrator paid the 2× premium for a cache entry nothing will ever read.

Cross-check makes this sharper. `p9-12-04`: planning #1 ended `07:42:07Z`, planning #2 started `08:47:32Z` — **1h 05m 25s**. It **missed the 1-hour TTL by 5 minutes and 25 seconds** and paid 163,905 cache-write tokens = **$1.6391 of that run's $2.1269 (77.1%)**.

Had each answer arrived inside the TTL, the same tokens would have been cache _reads_: run-000199 ≈ $0.31 instead of $1.70; `p9-12-04`'s ≈ $0.57 instead of $2.13. **Avoidable: ≈ $2.95.**

### The trigger both runs share — and it is upstream of the orchestrator

Both questions were **decisions the source task doc had deliberately left open**, so the round trip was guaranteed at authoring time:

- `docs/mdlint_v2/P12-consistency/04-mcp-custom-rules.md:29` — "**Decide the intent (maintainer call)**, then make the requirement, the schema, and the description agree"; header `:5-6` "needs confirmation → maintainer decision"; `index.md:19-22` "two `p9-09` items that are **decisions**, not code bugs". The planning agent's own framing echoes it: _"P12.04 is explicitly a maintainer decision between (A) … and (B) …"_.
- `p9-12-01` is the softer case: the ambiguity came from a constraint colliding with a discovery — task Constraints say _"Out of scope: fixing scope bugs themselves (those are P11 tasks)"_, and planning found an unreported one of exactly that class in the `--fix` path. Question quality was high: it cited `fix.ts:99-133`, `sec.ts:49`, `tbl.ts:156`, gave Option A/B with risks, and closed with _"Everything else in the plan is test- and docs-only and is unaffected by this answer."_

The role prompt behaved exactly as written — `.worc/flows/implementation/planning.md:21-24`:

```
## Clarification And Approval

- Use `human_input` only for a material clarification or approval of a risky change — state the precise risk and use repository-relative paths.
- If a `human_input` context file is already present, apply that answer and do not repeat the same request.
```

Both requests were material. **This is not a prompt defect and the role prompt should not be touched** (`packaged/flows/implementation/planning.md` carries this section unchanged — the Jul-25 vs Aug-3 drift in `planning.md` is confined to the "Roadmap And Architecture" / "Testing" sections being de-repo-specialized; the HITL section is byte-identical, so packaged does not address and does not need to address this).

### Levers, in order of value

**L1 — task authoring (the real fix, operator-side, zero code).** A task whose deliverable is _"decide X (maintainer call), then implement"_ guarantees a HITL pause and, empirically, a >1h wait. Resolve the decision **before** the task enters `tasks/pending/` and write the chosen branch into the task text. For `p9-12-04` that was one sentence ("go with A: widen the MCP `lint` input schema"). Saves $2.13 and 65 minutes of wall clock on that run.

**L2 — surface the economic deadline in the ask.** `src/wastech_orchestrator/notify/telegram.py:557-571`, `_format_ask_message`, renders only:

```python
header = f"[{task_id}] {kind}"
...
return f"{header}{contact_line}\n{body}\n\nContext:\n{ctx}"
```

No deadline, no urgency, no cost signal — the operator has no way to know a cliff exists. Meanwhile `.worc/config.yaml:129` gives them `ask_timeout_s: 28800` (8h), i.e. **the permitted window is 8× the economically cheap window**. Add one line to the ask body (e.g. "answer within ~55 min to reuse the cached session; later answers cost ≈$1.5 extra to re-prime"). Scope: orchestrator default (helps every repo). This is a UX change, not a behavior change — it does not weaken the security envelope or touch the state machine.

**L3 — consider lowering `telegram.ask_timeout_s`** in `.worc/config.yaml` toward the cache TTL so the deadline the operator is told matches the deadline that matters. Target-only. Trade-off: a shorter timeout means more `manual_action_required` parks, so this is a judgement call for the operator, not an obvious win — L2 is strictly better if only one is done.

**L4 — feature request, not a config change: a task-file pre-answer.** The mechanism already exists but is undocumented. `_run_with_hitl` calls `load_interaction(path)` **before** invoking the provider (`agent.py:144`), and `_resume_interaction` (`agent.py:189-202`) accepts `status` in `("answered", "consumed")` via `_require_persisted_human` — so a pre-seeded `.worc/logs/<task-id>/hitl/planning.json` with `{"status":"answered","request":{"kind":"approval",...},"approved":true,"failure":null}` is consumed on the **first** invocation: no Telegram round trip, no second `node_run`, no cache re-prime. `sanitized_answer_packet` (`core/hitl.py:395`) reads only `request.kind`, `request.question`, `answer`, `approved`. **I am not recommending the operator hand-write that file** — it is an internal durable artifact keyed by `interaction_path()` (`core/hitl.py:243`) and nothing documents or tests that use. The clean version is a first-class field: `task/model.py` already carries per-node overrides, but `core/node_overrides.py:57-117` supports only `provider`/`model`/`reasoning` — there is no pre-answer slot. Adding `nodes.<id>.human_input: {approved: true, answer: "..."}` to the task schema (`task/model.py`, `task/parser.py`, threaded to `agent.py:144`) would make "pre-decide the maintainer call in the task file" supported rather than a trick.

**Expected impact.** L1 alone: ~$2–3 and 1–2h wall clock per decision-shaped task. L2: turns a silent 20× cost step into a visible one.

---

## F2 — `p9-12-02` accepted with `[]` while writing a false claim into the canonical glossary; the `documentation` node then re-verified that claim as accurate.

**Category** prompt (review + documentation role) · **Severity** medium · **Confidence** high — the code is unambiguous.

### Evidence

The diff's glossary edit, as committed (`git show 45c40eb:docs/mdlint_v2/glossary.md`, lines 285-288):

```
285:   required keys are exactly `rule`, `id`, and `options`. `description`, `severity`,
286:   `options.files`/`options.exclude`, and `target` are optional; scope (`columnUnique` ⇒
287:   `project`, else `document`) and default severity (`error`) derive from the assert `kind`, not
288:   from config. Decision [R9](requirements/02-rules-engine.md); see
```

Ground truth, `packages/core/src/engine/rules/custom.ts:81` and `:91-92`:

```typescript
const scope = isProjectAssertion(assert.kind) ? "project" : "document";
...
    // Custom rules assert invariants → default error; config `severity` overrides via the runner.
    defaultSeverity: "error",
```

`isProjectAssertion` (`primitives/assert.ts:156-158`) is `return kind === "columnUnique";` — so the **scope** half is correct. The **severity** half is wrong twice over:

1. `defaultSeverity: "error"` is a **single constant**, identical for all 13 assert kinds. It does not "derive from the assert `kind`" at all. A reader of the canonical vocabulary doc will conclude some kinds default to `warning`.
2. "**not from config**" is contradicted by the code's own comment and by the runner: `engine/lint-files.ts:47` sets `severityOverride: configured.severity` and `engine/run-rules.ts:42` resolves `severity: severityOverride ?? finding.severity ?? rule.defaultSeverity`. The same glossary bullet lists `severity` as an optional config key two clauses earlier.

**Three gates let it through.**

- **implementation** authored it (`stages/implementation/run-000206/.../result.json` final message: _"derived scope/severity were added"_).
- **review** returned `{"findings":[]}` (`stages/review/run-000208/1-claude/result.json`; `evaluations.findings_json` = `[]`, 2 bytes).
- **documentation** explicitly re-checked it and passed it (`stages/documentation/run-000209/1-claude/result.json`), under a heading _"## Claims I verified against the code (all accurate)"_:

```
| Scope `columnUnique ⇒ project`, else `document`; default severity `error` | `engine/rules/custom.ts:81,92` |
```

The failure is legible: the doc claim **conjoins** two facts, the verifier bound it to **two source lines**, confirmed line `:81` supports the first half, saw `"error"` literally present at line `:92`, and never asked whether `:92` _varies by kind_ or _is config-overridable_. Both gates checked the first half of an `A and B` and stopped.

**And exit criterion 2 was ticked over it.** `current.diff` lines 37-39:

```
+- [x] `glossary.md` documents `custom.target` as optional, consistent with code, schema, and guide.
+- [x] No other `custom`-entry glossary claim contradicts the shipped schema.
```

Criterion 2 certifies the absence of exactly the contradiction the same edit introduced — a live instance of the brief's recurring defect class C, with the tick applied by the agent that created the contradiction.

### Levers

- **Review role, target `\.worc/flows/implementation/review.md:19-23` (and packaged `src/wastech_orchestrator/packaged/flows/implementation/review.md`).** The doc-verification bullet already exists and is close: _"When the diff is an authoring/documentation deliverable … enumerate every product-surface reference it makes — each command, flag, option value, output field, MCP tool — and verify each against current source in this one pass."_ It enumerates **named surfaces**, not **asserted relationships**. Add a clause: when a doc sentence claims a value is _derived from_ / _defaults to_ / _not settable via_ something, check the claim's **quantifier and direction** — that the value actually varies with the named input across its range, and that no override path exists — not merely that the literal appears at the cited line. This is a genuine gap in both copies: the Jul-25 target and the Aug-3 packaged version have functionally the same bullet (packaged generalizes "MCP tools" → "public API surfaces" and drops the repo-specific `docs/mdlint_v2/` line), so **packaged does not already address this** and the fix belongs in the packaged default.
- **Same clause in `implementation/documentation.md`** — the documentation node is the one that produced the false verification table, and on `p9-12-01` it is the node that caught an analogous over-claim. It is the right place for the strengthened rule.
- **Exit-criteria discipline.** The `[x]` on a self-assessed criterion is worth one line in `implementation/implementation.md`: tick a criterion only against evidence you can cite in the implementation notes; for a criterion of the form "no other X contradicts Y", list the X's you checked. Both target and packaged copies.

**Scope** orchestrator default (both role prompts). **Expected impact** closes the highest-value class in this batch: a doc-only task whose entire purpose is removing a code/doc contradiction shipped a new one, unflagged, into the file the repo treats as canonical.

---

## F3 — The task-authoring pattern that predicts cost: a closed, line-anchored deliverable set beats a set-predicate the agent must resolve by search.

**Category** spec · **Severity** medium-high (largest controllable cost lever in the batch) · **Confidence** high.

### Evidence

|  | `p9-12-02` ($5.21) | `p9-12-03` ($8.04) | `p9-12-01` ($18.77) |
| --- | --- | --- | --- |
| task description size | 2,305 B | 2,898 B | 2,921 B |
| planning cache read (exploration) | 1,014,768 | 1,216,326 | **2,942,517** |
| planning wall | 232s | 439s | 475s (+82s) |
| implementation cache read | 1,207,658 | 3,120,202 | **9,296,283** |
| implementation wall | 286s | 530s | 777s |
| review cache read | 434,677 | 337,994 | 2,477,376 |
| `current.diff` | 6,418 B / 3 files | 37,547 B / 12 files | 64,381 B / 19 files |

Cost is **not** a function of task-text length — the three task descriptions are within 21% of each other. It tracks **exploration volume**, and exploration volume tracks how much of the target set the agent has to _discover_:

- `p9-12-02` deliverable 1: _"Update `glossary.md:263-265` so `target` is documented as **optional**, matching `config-schema.ts:91` and the generated schema."_ Every claim in the Problem section carries a `file:line` (`config-schema.ts:91`, `engine/schema.ts:88`, `custom.ts:74`, `guide/rules/custom.md:38`, `:162`). The Constraints state the negative scope explicitly: _"Documentation-only: do not change product code, the schema, or the rule implementation — they are already correct."_ Target set: **closed, 1 file, pre-verified**.
- `p9-12-01` acceptance criterion 1: _"**Every** rule family that accepts the file-scope shape has an `exclude` e2e test, including the `exclude`-only (no `files`) case."_ That is a **predicate over a set the agent must first compute**. Planning did exactly that — its output opens with a 9-row table binding 14 built-in rules + `custom` to their gate sites (`tbl.ts:38,:146,:179,:213,:246`, `sec.ts:38,:94,:199`, `ctx.ts:32,:58,:135`, `ref.ts:115`, `grp.ts:79`, `custom.ts:109,:116`). Planning became a registry audit.

**Compounding factor: premise staleness, but only one kind is expensive.** `p9-12-01`'s Problem section asserts _"`grep -c exclude` across … returns **0 in all eight**"_. Planning had to refute it — _"**Coverage is no longer zero** (the audit's grep is stale): `rules-tbl.test.ts:142` … `primitives.test.ts:169` … `rules-grp.test.ts:116` … already exist. Extend, don't duplicate."_ — and produced a three-item contradiction list before it could plan anything. `p9-12-02`'s premise was _also_ stale (`glossary.md:263-265` had moved) but repairing a **line number** is one `Read`; repairing a **coverage claim** is a 15-rule re-audit. So: stale anchors are cheap, stale set-claims are expensive.

### Lever

Operator-side, task authoring (`/worc-task` guidance and the task templates the operator writes from). Two concrete rules, both drawn from `p9-12-02`:

1. **Close the set.** Replace "every X that Y" with the enumerated list, or state who owns the enumeration. If the enumeration genuinely must be discovered, say so and expect planning to cost 2–3×; do not treat it as an S-sized task.
2. **State the negative scope.** `p9-12-02`'s _"the code is already correct"_ line let planning prune whole search branches. `p9-12-01`'s out-of-scope line did the opposite — it collided with a discovery and triggered F1's HITL.

Optional third: re-verify audit greps at queue time, not authoring time, or mark them "as of <date>, re-verify".

**Scope** target-only (task authoring), but the shape generalizes. **Expected impact** the dominant term in the 3.6× spread between `p9-12-02` and `p9-12-01`. Not all of it is recoverable — `p9-12-01` is genuinely a bigger job — but the exploration multiple (2.9× planning, 7.7× implementation) is larger than the delivery multiple (10× diff bytes at 1/3 the unit cost), which is the signature of avoidable search.

---

## F4 — `p9-12-01`'s review raised a `medium` saying the task's own prevention deliverable is inert, and the flow accepted it.

**Category** flow + prompt · **Severity** medium · **Confidence** high.

### Evidence

`evaluations` row, `p9-12-01-exclude-coverage / review / in_flow_verdict / accept`, one finding, `severity: "medium"`:

> "The advertised coverage guard is inert. `} as const satisfies Record<Assertion["kind"], CustomScopeCase>;` cannot fail CI: `npm run typecheck` is `tsc -b` over the three package projects, and `packages/core/tsconfig.json` has `"include": ["src/**/*.ts", "src/**/*.d.ts"]` — no tsconfig includes `test/`, and Vitest transpiles without type checking … So a 14th assert kind would ship with no `exclude` case and nothing would go red — **the exact L-4 class this task exists to prevent.** … The same false claim is written into `docs/mdlint_v2/P12-consistency/01-exclude-coverage.md`."

The routing: `core/flow/nodes/evaluator.py:289` compares each finding's rank against `node.gate_severity`; the default is `high` (`core/flow/engine.py:97`, `core/flow/schema.py:126`). Neither the target flow (`.worc/flows/implementation.yaml:74-85`) nor the packaged default (`packaged/flows/implementation.yaml:94-110`, where the line is commented out) pins it. So `medium` → advisory → `accept`.

Two distinct problems, and the second is the real one:

1. **Severity mis-calibration.** This finding _is_ acceptance-criterion failure — criterion 1 asks for coverage of "every rule family", and the mechanism that guarantees the set stays covered does not fire. Under the target review role's own rubric it is arguably the blocking invariant _"Zero test coverage for new core user-visible behavior"_ for a future 14th kind. Rating it `medium` is what let it through, not the gate value.
2. **The correct outcome was reached by luck.** The `documentation` node independently rediscovered it and downgraded the phase-file claim to an honest "known gap" — supervisor observation on `documentation`: _"the implementation step asserted the `custom` test's `satisfies Record<…>` would make `npm run typecheck` fail on a new assert kind — but `tsconfig.json` only includes `src/**` … The documentation step caught this, verified it, and downgraded the phase-file claim … That's the right call, but it means one of the two drift guards touted at the implementation step is not real."_ Shipped state: honest doc, inert guard. Nothing in the flow closes a `medium`.

### Levers

- **Do not lower `gate_severity` to `medium` as a reflex.** `review` is a blocking node; `packaged/guide/flows/reference.md:144` spells out the consequence — "on a **`blocking: true`** node a gating finding loops the named-loop budget and then parks the task in `manual_action_required`, so lowering the gate there means raising that budget too". With 62 accepted findings across 20 runs (44 low + 18 medium), a blanket `medium` gate would have converted a large share into rework loops or parks. Not worth it.
- **Fix the calibration instead.** `\.worc/flows/implementation/review.md` line 1 says _"mark anything that must change before merge as **blocking**"_ but never ties severity to the task's acceptance criteria. Add: a finding that means a stated acceptance criterion is **not** actually satisfied — including a guard/test that cannot fail — is at least `high`. Same edit in `packaged/flows/implementation/review.md`. This is the surgical version of the brief's cross-cutting signal #1: it promotes the findings that _should_ loop without promoting the 44 nits.
- Structural alternative (bigger, flag only): a non-blocking `findings_closure` evaluator after `documentation` with `gate_severity: medium` and `max_rework_per_stage: 1`, which self-caps and accepts with a ⚠️ rather than parking. Pure graph-shape choice, operator-authored flow — mentioning it as an option, not recommending it on this evidence.

---

## F5 — Accepted findings _are_ carried forward, but as duplicated, mis-titled follow-ups.

**Category** infra (supervisor) · **Severity** low · **Confidence** high. _Refines_ the brief's signal #1: the findings are not lost — `supervisor.emit_follow_ups: true` converts them — but the conversion is mechanical.

`summary.json` → `follow_ups`. `p9-12-01` ships **two entries for one issue**: the supervisor's own well-written one ("Custom-rule assert-kind exhaustiveness guard doesn't actually fire at typecheck", with a real `action_hint`), plus a second whose `title` is the first 120 characters of the review finding and whose `action_hint` is `null`:

```json
{
  "title": "The advertised coverage guard is inert. `} as const satisfies Record<Assertion[\"kind\"], CustomScopeCase>;` cannot fail C…",
  "evidence": ["review evaluator finding (accepted with findings)"],
  "action_hint": null
}
```

`p9-12-03` ships four for three (entries 1 and 3 are the same `compile-context.ts` key-identity issue). `p9-12-02` ships one, clean.

Cause: `core/supervisor.py:371-396`, `_finding_to_follow_up` — `title, rationale = reason[:_FINDING_TITLE_MAX].rstrip() + "…", reason` with `_FINDING_TITLE_MAX = 120` (line 351), and `action_hint` unset. Dedup at `supervisor.py:482` is an **exact-match** key ("its normalized text plus its paths"), so a supervisor-authored paraphrase of the same finding never collides with the mechanical copy.

Destination: `logs/<task>/pr_body_appended.md` under `## Technical debt / follow-ups`, human-readable only — no task file is generated. On a shared branch that file accumulates every task's follow-ups (54KB here, all 20 runs → PR #16).

**Lever** `core/supervisor.py:371-396`. Two small changes: (a) when the supervisor's own follow-up list already covers a finding's `paths`, skip the mechanical duplicate (loosen the dedup key from exact text to path-overlap + severity, or have the summary turn receive the findings and own the conversion — the digest that feeds it already exists at `_render_gate_digest`, `supervisor.py:446`); (b) derive a real title (first sentence, or the `paths` + severity) rather than a 120-char truncation. **Scope** orchestrator default. **Expected impact** cosmetic-to-real: the PR body is where the operator actually reads these, and a truncated code-fragment title is not actionable.

---

## F6 — `usage_reasoning_output` is NULL: cause identified, and it is **not** an orchestrator bug.

**Category** infra/instrumentation · **Severity** informational · **Confidence** high. (Brief asked not to re-report unless the cause was found.)

The Claude CLI's `usage` payload carries no reasoning/thinking token field at any level. Full key set from `stages/planning/run-000205/1-claude/result.json`:

```
top-level usage keys: ['input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens',
 'output_tokens', 'server_tool_use', 'service_tier', 'cache_creation', 'inference_geo',
 'iterations', 'speed']
iterations[0] keys: ['input_tokens', 'output_tokens', 'cache_read_input_tokens',
 'cache_creation_input_tokens', 'cache_creation', 'type']
any reasoning key anywhere? False
any thinking key anywhere? False
```

Thinking tokens are folded into `output_tokens`. The column in `provider_attempts` is correctly nullable ("All nullable (a result-less attempt)") and the adapter has nothing to populate it from. **Recommendation: none** — do not add a synthetic estimate. Consequence to accept: reasoning spend cannot be separated from answer spend, so `xhigh`-vs-`high` tuning has to be judged on total output tokens (see Data gaps).

---

## F7 — Recurring defect classes A and B did **not** recur in this batch.

**Category** diff · **Severity** none — reported so the brief's classes are closed out rather than assumed.

- **Class A (markdown source breakage).** Checked programmatically two ways over all three commits: (i) every added non-fenced markdown line in all three `current.diff` files has an **even** backtick count → no inline code span split across a line break; (ii) every markdown file in `7ad9273`, `45c40eb`, `3491d07` re-parsed at that commit for list continuations indented below their marker's content column → **zero hits**. The class that hit "nearly every task" is absent here.
- **Class B (incomplete doc-surface sweep).** Present only in a form I judge **correct, not a defect**. `p9-12-01` swept 6 `docs/guide/*` files + `glossary.md` + `requirements/02-rules-engine.md` beyond its plan's list, because the approved `tbl.ts` guard is a user-visible `--fix` behavior change. `p9-12-02`'s documentation node explicitly enumerated what it left alone and why (`stages/documentation/run-000209/.../result.json`): `guide/rules/custom.md:38,171-174` and `guide/rules/README.md` already correct (verified — `custom.md:42` reads `| `target` | … | no | Optional redundant declaration…`); `README.md:230`, `use-cases/custom-rule.md:16`, `requirements/02-rules-engine.md:47` only _use_ `"target"` in examples, _"which is legal for an optional key … requirements outrank the glossary — rewriting it would exceed this task."_ `requirements/02-rules-engine.md:47` does still carry the pre-alphabetical enum order in a comment (`// table | section | content | checklist | link`) — a cosmetic inconsistency with the glossary's new alphabetical list, correctly declined with a reason. **No lever.**

---

## Scope check — `current.diff` vs `task.normalized.json` (Q4)

| run | verdict | notes |
| --- | --- | --- |
| `p9-12-01` | **in scope** | 19 files. Only product-code file is `packages/core/src/engine/rules/tbl.ts` — the one-line guard, HITL-approved. 6 guide docs beyond the plan's list, justified by the user-visible `--fix` change (supervisor concurred: _"I don't read this as drift; it's closing a real doc gap the plan under-scoped"_). Phase index and frozen audit report untouched, per repo pattern. **Residual:** criterion 1 met in letter, not in mechanism — see F4. |
| `p9-12-02` | **in scope** | 3 files, all docs; zero product-code bytes changed (independently confirmed: `config-schema.ts`, `engine/schema.ts`, `rules/custom.ts` untouched by `45c40eb`). `docs/guide/config-reference.md` beyond the named deliverable, same defect class, flagged as optional in the plan and disclosed on delivery. **Residual:** criterion 2's `[x]` is false — see F2. |
| `p9-12-03` | **in scope** | 12 files. `primitives/content.ts`, `graph/build-context-graph.ts`, `rules/ctx.ts` are not drift — they are the `findLineNumber` call sites the fix requires (`git show 3491d07` shows each swapping `findLineNumber` → `createLineNumberLookup` hoisted out of the per-match loop, with a why-comment citing audit L-5). Delivered `O(L + M·log L)` rather than the deliverable's suggested `O(M + L)`; the **acceptance criterion** is the looser _"`findLineNumber` no longer rescans from zero per match (or the assumption is documented)"_, which is met, and the shortfall was recorded in the task file rather than hidden. Criteria 1, 3, 4 verified by `check_runs` + the added tests. |

---

## What's already good

1. **The HITL mechanism worked as designed and was used sparingly and well.** Two round trips in 20 runs, both on genuinely material decisions, both with precise questions carrying real source citations and named options with risks. `_run_with_hitl` resumed the same session rather than re-deriving from scratch — the design comment at `agent.py:171-173` is right, and a fresh re-derivation would have cost far more than $1.70 (planning #1 took 475s and $3.62). Do not "fix" the resume.
2. **Usage accounting is correct under resume.** `agent.py:180-182` passes `resume_usage_baseline` so a cumulative-scope provider's second run doesn't double-count; Claude reports `per_invocation` so it is a no-op here, but the recorded per-attempt deltas reconcile against derived pricing to 0.03% and `usage_delta_status='ok'` on all 42 attempts. The $18.77 is real, not double-counted.
3. **Prompt caching is working hard.** Cache read is 95–99% of input on every attempt (`p9-12-01` implementation: 9,296,283 read vs 179,723 written vs 1,887 uncached). F1 is not a caching failure — it is the one case where a write has no reader.
4. **The `documentation` node earns its cost.** At $0.72–$1.57 it is the cheapest agent node, and on `p9-12-01` it caught the false drift-guard claim that `review` had rated `medium` and accepted, then corrected the phase doc. On `p9-12-02` it made **zero edits** ($0.81, 15.5% of that run) but produced a 7-row claim-verification table and explicitly declined three out-of-scope surfaces with reasons. I would **not** gate or disable it.
5. **Honest disclosure throughout.** `p9-12-03` recorded a complexity-target shortfall rather than shipping it silently; `p9-12-02`'s documentation node volunteered that _"I did not run tests or builds … the notes' `npm test` claim is inherited from the implementation step, not something I re-verified."_ `p9-12-01`'s implementation verified the P11.05-sensitivity claim by temporarily reverting the `columnUnique` guard and confirming exactly 3 tests went red. That is the behavior you want.
6. **Checks are fast and stable.** 15 check runs across the three tasks, all `exit_code=0`, zero `timed_out`, 21–26s per full gate.
7. **Markdown source hygiene** — clean in this batch (F7).

---

## Data gaps

1. **`runs/exchange-seals/` is absent** (`.worc/runs` does not exist). Expected, not a gap: `logging.clean_runs_on_success` defaults to `true` and all three tasks reached `done`. Consequence: I could not read the exact sealed exchange the agent last saw, and fell back to `logs/<task-id>/` + `stages/*/rendered-prompt.md`, which was sufficient. To analyze future runs from the seals, set `logging.clean_runs_on_success: false` before the run.
2. **Reasoning vs answer tokens are unseparable** (F6). Every node in this flow is pinned to `claude-opus-5` at `xhigh` (planning/implementation/fixing), `high` (review), `medium` (documentation), so a per-node reasoning-fit judgement would need thinking-token attribution the provider payload does not supply. I therefore make **no model/reasoning recommendation** for these three runs — the observable signals (zero retries, zero rework on 12-03 and 12-02, one non-blocking `medium` on 12-01) show no under-powering, and I cannot evidence over-powering without that split.
3. **What the operator actually saw in Telegram.** I reconstructed the delivered message from the persisted `request` block plus `_format_ask_message` (`notify/telegram.py:557`) and `handle.delivered: true`; the sent text itself is not archived. The claim in F1/L2 that no deadline was surfaced rests on reading the formatter, not on the wire message.
4. **Why the human took 2h / 1h05m** is outside the artifacts. The 5m25s TTL overshoot on `p9-12-04` is arithmetic on the timestamps, not evidence about operator intent — it may simply be that nobody was watching. That strengthens L2 (surface the deadline) rather than weakening it.
5. **`p9-12-02`'s `[]` review is a single sample.** F2 shows the review+documentation pair sharing one blind spot on one claim. I did not audit the other 19 runs' doc claims for the same conjoined-assertion pattern, so I cannot say how often it recurs — worth a targeted sweep if the operator wants F2 sized before acting on it.
