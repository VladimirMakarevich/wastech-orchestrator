# 04 — Flow and config findings

## T1 — `proseWrap` is unset in the target, so the format gate cannot see the defect class it is blamed for {#t1}

**Category** config (target) · **Severity** medium-high · **Confidence** high · **Scope** target-only

The cleanest fix in this report, and the highest ratio of benefit to effort.

`wastech-mdlint/.prettierrc` is `{"singleQuote": false}` — no `proseWrap`, so Prettier's default `"preserve"` applies and **prose is never reflowed**. `npm run format` is `prettier --check .`, which therefore passes no matter where a line breaks. Meanwhile the repo's markdown _is_ hand-wrapped to ~99 columns by convention, and that convention is documented nowhere — `grep -rn -i "wrap|99|column|prose" AGENTS.md .agents/rules/*.md` in the target returns nothing relevant.

The consequence is the recurring "line break inside an inline code span / continuation loses its list indent" finding that appears in 4 tasks — and the reviewer names the mechanism precisely: _"Prettier preserves multi-line code spans verbatim, so the format gate will not correct it; it renders acceptably but reads as broken in source."_

The cost is not cosmetic. Appendix A measured it as **the largest single consumer of the most expensive node in that batch**: of `fixing`'s 59 Bash calls on p9-11-03, roughly **30 were manual markdown re-wrapping**. It wrote a Python wrapping script from scratch **six separate times** and ran it at **three different widths** (96, 98, 99) because the convention is unwritten and it had to guess.

**Lever.** Set `"proseWrap": "never"` in `wastech-mdlint/.prettierrc` — exactly what this repo does for itself (see [AGENTS.md](../../../AGENTS.md) and `.prettierrc.json`). Then `prettier --write` reflows automatically, `--check` enforces it, the defect class becomes mechanically impossible, and ~30 Bash calls per fixing round disappear.

If hand-wrapping is deliberate, the fallback is to document the exact column in the target's `AGENTS.md` and add a check that detects a code span split across a newline — so `testing` catches it in 20 s instead of `review` catching it in a $6–11 round.

**Sequence note.** Do this _before_ lowering `gate_severity` ([03](03-prompt-findings.md#p2)), or the lower gate will start blocking on these nits.

## T2 — The check command set cannot reproduce CI conditions {#t2}

**Category** checks (target) · **Severity** medium · **Confidence** high · **Scope** target-only

`checks.command_sets.default` runs `npm run typecheck`, `npm run lint`, `npm run format`, `npm test`, `npm run build` — 145 invocations, all passed, 0 timeouts, 20–27 s per pass.

The target's own CI (`.github/workflows/ci.yml:27-40`) runs **`npm ci` first**, then the same five commands, across a **three-OS matrix** (`ubuntu-latest`, `windows-latest`, `macos-latest`).

**Gap 1 — no clean install.** Checks run against the developer's already-installed, already-built `node_modules`. This is exactly p9-11-01's `high` finding:

> `assertBuilt()` runs at module scope and throws for `cliBin`; CI runs `npm ci` **before** `npm run typecheck`, and `dist/` is gitignored, so at install time `packages/cli/dist/index.js` does not exist. npm's bin-links skips creating a bin link when the bin target is absent … Result: `npm test` fails at collection on ubuntu/windows/macos. It passes locally only because this checkout has a shim from an earlier install.

A deterministic gate would have caught that; instead it cost a full review→fix round. Prepend an install step to the command set in `.worc/config.yaml` — there is ample headroom, since `checks.timeout_seconds` is 7200 against a current 27 s.

**Gap 2 — single OS, and not closable here.** Cross-platform issues are the **largest defect class by finding count: 14 findings across 5 tasks** (Windows path semantics, `path.win32.relative` cross-drive escape, CRLF, junctions, `shell: true` quoting). The orchestrator runs on macOS and cannot spawn Windows runners, so this cannot be moved into `testing`. The review evaluator is the only pre-merge cross-platform gate and it is performing that role well — the right response is to keep it explicit in the review prompt (the target's copy has this depth; the generic packaged copy has much less) rather than pretend checks can cover it.

**The generalisable point.** Both gaps are the same shape: every defect class the checks cannot see costs a full rework round instead of a 20-second gate run. Worth a standing practice of auditing `command_sets` coverage against the review findings that recur.

## T3 — p9-11-14's $52 is task granularity, and decomposition is a one-key experiment {#t3}

**Category** spec / config · **Severity** medium · **Confidence** high · **Scope** target-only, plus a task-authoring rule

p9-11-14 bundles audit findings **L-7 … L-11** (L-11 itself a basket of six micro-fixes). Planning expanded it into **18 numbered file-level steps** across 18 files; the diff is 30 files, +2071/−422. The edits are loosely coupled by construction — `load-documents.ts`, `commands.ts`, `index.ts` and `workspace-packages.ts` have no dependency on one another; they were batched because all were filed `low`. For contrast, p9-12-05's plan has **zero** numbered steps.

Given the `tool_calls^1.651` law, batching independent work into one node instance is the most expensive possible arrangement: five 47-turn instances cost ~$9.95, one 233-turn instance ~$28.00.

**Lever, in order of durability.**

1. **A task-authoring rule** (best): a task should be one coherent change. A basket of unrelated `low` findings should be N tasks, or one task with operator-authored subtasks. This is a `/worc-task` and backlog-authoring convention, not a code change.
2. **`agents.decomposition.enabled: true`** (cheapest to try): a one-key change in `.worc/config.yaml`. The flow already declares `sub_flow: [implementation, testing, review, fixing]` with `shared_budget: global_fix_iterations`, and sessions are `fresh_disposable`. Estimated saving on a run like p9-11-14 is $15–17 — but frame it as an A/B, because per-subtask review count rises and that partly offsets it.
3. **Do not** downgrade model or reasoning to address this. Output is only 11% of that run's cost; the model is not the driver.

Also confirmed: p9-11-14 was never near a limit — 234/400 turns, 1951/7200 s, and `max_turns_gate: false` means an overrun would not have failed it anyway.

## T4 — HITL approvals are correct but the cost is almost all cache re-priming {#t4}

**Category** flow / config · **Severity** medium · **Confidence** high · **Scope** both

The "double planning" on p9-12-01 and p9-12-04 is a HITL approval round-trip, not a retry. `planning` returned a complete plan plus a `human_input` block of kind `approval`; the answer arrived via Telegram; `_run_with_hitl` (`core/flow/nodes/agent.py:140-187` — _"First-time signal: one durable round-trip, then re-run with the answer"_) re-ran the node with `--resume` on the same session id, with prompts differing by one line. The second `_invoke_with_turn_gate` is what creates the second `node_runs` row. `planning` is the only `hitl:`-enabled node in the flow. Working as designed.

The waste is in the wait, and it is exact:

- **p9-12-01** — the human took **2 h 01 m**, blowing the 1-hour prompt-cache TTL. The resume re-wrote the entire 146,231-token transcript at 2× base rate: **$1.4623 of that run's $1.6980 (86%) was cache re-priming with zero future readers.**
- **p9-12-04** — missed the TTL by **5 minutes 25 seconds**, paying $1.6391 of $2.1269 (77%).

Avoidable: ~$2.95 across the two.

**The shared trigger is upstream of the orchestrator.** Both questions were maintainer decisions the source task docs deliberately left open — `docs/mdlint_v2/P12-consistency/04-mcp-custom-rules.md:29` literally reads _"Decide the intent (maintainer call), then …"_.

**Levers, in order.**

1. **Resolve maintainer decisions before queueing the task.** A task doc that defers a decision to the agent guarantees a HITL stop.
2. **Surface the deadline in the ask.** `notify/telegram.py:557` `_format_ask_message` includes **no deadline at all**, while `telegram.ask_timeout_s: 28800` grants 8 hours — 8× the economically cheap window. Telling the operator "answering within the hour avoids a full context re-write" converts an invisible cost into an actionable one.
3. **A task-file pre-answer** would remove the round trip entirely, but `core/node_overrides.py` covers only provider/model/reasoning today. That is a feature request, not a config change.

## T5 — `documentation` is terminal, writes code-adjacent prose, and nothing reviews it {#t5}

**Category** flow · **Severity** medium · **Confidence** high · **Scope** target flow, optionally packaged

Edges are `review --accept--> documentation --> publish`. There is no gate between `documentation` and `publish`, and the role prompt sanctions running `npm run format`, which mutates files. So the last node to touch the committed diff is the one node whose output nothing checks.

Quantified across the 20 runs (appendix C):

- `documentation` edited files in **19 of 20 runs**, 84 `Edit` calls, **all `.md`** — the read-only-to-code contract held 20/20.
- **20/20 voluntarily ran a format gate; 0/20 ran typecheck, test or build.**

The harm is real but narrow, and it is prose-shaped:

- p9-12-05 — documentation fixed the accept-verdict `medium` in `context-graph.md` **and introduced a fresh defect**: a hard line break mid-sentence (`"Many small components are fine"` / newline / `"however large the corpus is"`) that `prettier --check` cannot see. Recurring defect class A, shipped by the node that was closing a finding.
- p9-11-13 — documentation authored +22/−14 across three files no reviewer saw, two of them in the repo's "locked" requirements tier. The supervisor caught it and asked for sign-off.

**Recommendation: do not add a `documentation → testing` edge.** The only gate a `.md` edit can break is the format gate, which already ran 20/20 — and it would not have caught either defect above, because both are prose. It would also risk re-entering the review loop for a prose change, since `testing → review` fires on pass.

Better options, in order:

1. **Stop handing `documentation` a findings file it is forbidden to act on**, or state the contract in the prompt. Today it receives the review's `findings.json` in its context footer while being read-only to code, so it partially closes finding sets — appendix E verified it closed 9 of 15 in one sample, and appendix B saw it close doc-scoped findings the graph has no other path for. That behaviour is _valuable_; it is just undeclared, which is why it half-completes.
2. **A `when`-gated, non-blocking evaluator after `documentation`** — the only lever that catches the observed harm, at ~$0.5–1/run. Target flow YAML; not obviously right as a packaged default.
3. **Accept and document the residual risk.** Defensible: it is prose-only, permission-scoped, and the supervisor does surface it.

## T6 — The `token-optimization` backlog is aimed at 5.9% of the cost {#t6}

**Category** spec / prioritisation · **Severity** medium · **Confidence** high · **Scope** orchestrator

`docs/backlog/token-optimization/` is entirely supervisor-layer work. Measured across these 20 runs the supervisor is **$19.14 of $325.16 — 5.9%**, at $0.13 per invocation on `claude-sonnet-5`/`medium`. `implementation` + `fixing` is **~46%**.

There _is_ real supervisor waste — 58 of 147 invocations (39%) observed nothing at all, because the observe prompt for `testing` and `review` steps carried no findings and no check detail, so the observer wrote things like _"Review step came back requesting rework, but no details were included about what issues were found."_ That is $1.81 and 2.65 M input tokens for no signal. **Both are already fixed at HEAD** — the runtime was `61ef90f` (Jul 25); `findings=outcome.findings` and `_render_findings_digest` landed later, and the in-flight `supervisor_packet.py` adds `check_runs`. The action is operational: **refresh the target's install**, and add a regression test that a `rework` observation prompt contains the findings digest.

But the campaign's leverage is elsewhere. If the goal is token reduction, the ranked targets are turn count in writer nodes ([03](03-prompt-findings.md#p4)), task granularity ([T3](#t3)), rework avoidance ([03](03-prompt-findings.md#p1)), and HITL cache-TTL loss ([T4](#t4)) — in that order. Worth re-scoping the backlog item accordingly.
