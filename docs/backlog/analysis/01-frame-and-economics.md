# 01 — Frame and economics

## What ran

20 tasks, `p9-11-01-cli-bin-noop` → `p9-12-06-process-boundary-tests`, executed 2026-07-26 and 2026-07-28 on the `implementation` flow. Every one reached `done` on attempt 1.

Path taken by all 20: `planning → implementation → testing(checks) → review(evaluator) → [fixing → testing → review]* → documentation → publish`. Refinement is not in this flow; decomposition was disabled.

| Signal | Value |
| --- | --- |
| Final status | 20 × `done`, attempt 1 |
| Provider retries | 0 (`stage_attempts` = 1 on all 149 `node_runs`) |
| Route fallbacks | 0 (`provider_used` = `claude` throughout; `codex` configured as standby, never exercised) |
| HITL stops | 2 (`planning` approvals on p9-12-01, p9-12-04) |
| Skipped nodes | 0 |
| Check failures / timeouts | 0 of 145 `check_runs` |
| Rework loops | 8 tasks took one; p9-11-01 took two |
| Exchange contamination / quarantine | 0 |
| Total spend | **$325.16** — $306.02 flow nodes + $19.14 supervisor |
| Total input / output tokens | 355.5 M / 3.22 M (cache-read 97–99% of input) |
| Total node wall-clock | ~12.85 h (~39 min/task) |

## Cost and time by node

| node           | runs | USD    | avg  | input   | total min | avg s |
| -------------- | ---- | ------ | ---- | ------- | --------- | ----- |
| implementation | 20   | 100.63 | 5.03 | 137.0 M | 190.6     | 572   |
| planning       | 22   | 74.94  | 3.41 | 69.2 M  | 235.3     | 642   |
| review         | 29   | 61.16  | 2.11 | 43.5 M  | 148.4     | 307   |
| fixing         | 9    | 47.77  | 5.31 | 81.6 M  | 131.0     | 873   |
| documentation  | 20   | 21.52  | 1.08 | 15.9 M  | 39.9      | 120   |
| supervisor     | 147  | 19.14  | 0.13 | —       | —         | —     |
| publish        | 20   | —      | —    | —       | 14.4      | 43    |
| testing        | 29   | —      | —    | —       | 11.3      | 23    |

Two things follow immediately. **`implementation` + `fixing` is ~46% of spend**; the supervisor is **5.9%**. And `planning` is the largest consumer of wall-clock — but see the era split below before drawing a conclusion from that.

## The natural A/B in the middle of the campaign

`.worc/flows/implementation.yaml` was edited **Jul 28 00:21**, between p9-11-07 (finished Jul 26 06:18) and p9-11-08 (started Jul 28 00:45). The frozen control bundles under `.worc/control-bundles/<task-id>/flows/` pin what each run was actually bound to, and a `diff -q` across the boundary shows **all seven role prompts byte-identical**. Only model and effort changed. That makes this a clean single-variable experiment.

| node           | era A — p9-11-01…07          | era B — p9-11-08…p9-12-06    |
| -------------- | ---------------------------- | ---------------------------- |
| planning       | `claude-sonnet-5` / `max`    | `claude-opus-5` / `xhigh`    |
| implementation | `claude-sonnet-5` / `xhigh`  | `claude-opus-5` / `xhigh`    |
| review         | `claude-opus-5` / `xhigh`    | `claude-opus-5` / `high`     |
| fixing         | `claude-sonnet-5` / `max`    | `claude-opus-5` / `xhigh`    |
| documentation  | `claude-opus-5` / `medium`   | `claude-opus-5` / `medium`   |
| supervisor     | `claude-sonnet-5` / `medium` | `claude-sonnet-5` / `medium` |

Outcomes:

|                                  | era A (7 tasks) | era B (13 tasks) |
| -------------------------------- | --------------- | ---------------- |
| tasks needing rework             | **5 (71%)**     | **3 (23%)**      |
| rework rounds                    | 6               | 3                |
| `high`-severity findings per run | **1.29**        | **0.23**         |
| review findings per task         | 7.6             | 4.4              |
| $/task                           | 15.60           | 16.61            |
| min/task                         | 47.4            | 33.8             |

Restricted to P11 alone — same phase, same remediation-task character, identical declared size mix of 4×`S–M` + 3×`S` in each half — tasks 01–07 took 5/7 rework and tasks 08–14 took 2/7.

**Rework rate fell ~68% and wall-clock ~29% for +$1.01/task (+6%).** The eliminated `fixing` rounds paid for the more expensive model: `fixing` averages $5.31 per invocation, more than the entire per-task cost delta. Pricing verified via the `claude-api` skill (Opus 5 $5/$25 per MTok, Sonnet 5 $3/$15, cache read 0.1×, 1 h cache write 2×) and reconciled against recorded `usage_cost` to within 0.4% on every attempt in appendix D.

The obvious confound — `review` reasoning also dropped `xhigh → high` at the same cut, so "the reviewer got laxer" — **points the wrong way**: the era-B reviewer at lower effort raised _more_ `low` findings per run (2.4 vs 1.9) while finding 5.6× fewer `high` ones. A less thorough reviewer does not simultaneously raise its nit rate.

Counterfactual for the cheapest runs, using the verified price model: a pure Sonnet swap saves exactly 40% of node cost (~$2.9/run), but expected total is **$8.76 on Opus vs $11.76 on Sonnet** once rework probability is priced in. Opus wins in expectation even on the smallest tasks.

**Recommendation: keep era-B settings.** Consider promoting them to `packaged/flows/implementation.yaml`, which currently ships every per-node slot commented out so all nodes inherit the global provider default. Restoring `review: reasoning: xhigh` is worth testing — that setting is what read npm's bundled sources and found a `path.win32.relative` cross-device escape.

**Caveat stated plainly:** the two cohorts are different tasks, not the same tasks re-run. The within-P11 comparison controls for phase and declared size but not for individual task difficulty. Four independent metrics move the same way, so treat the direction as solid and the magnitude as indicative.

## Where the planning "inversion" went

Planning wall-time exceeding implementation was real but is an era-A artifact of `reasoning: max` on Sonnet:

- era A planning: mean **924 s**, 3.99× implementation wall time, 84.2 k output tokens
- era B planning: mean **573 s**, **0.95×** implementation wall time, 41.4 k output tokens

Tool-call counts were comparable (66 vs 55) — so `max` bought thinking, not exploration. p9-11-04's planning emitted **120,752 output tokens with 46 thinking blocks and 734 characters of visible text** to produce a 15 KB plan: ~96% of output was thinking. The worst single ratio, p9-11-05 at 7.18×, is entirely in era A.

Do not cut planning depth on the strength of the era-A numbers. `xhigh` is the documented recommendation for coding and agentic work; `max` should not be reintroduced.

## The empirical cost law

Fitted across all 20 `implementation` node instances (appendix C):

```
usd = 0.00345 × tool_calls^1.651        R² = 0.963
```

Cost is **super-linear in turns per node instance**, because every tool round-trip re-reads the whole accumulated context from cache. Five 47-turn instances cost ~$9.95; one 233-turn instance costs ~$28.00.

p9-11-14, the $52.41 outlier, is this law in action: **233 tool calls × ~184 k average context = 42.87 M input tokens** (`42,872,645 / 233 = 183,999`). Context grew monotonically 19,898 → 323,982 and never compacted. Its cost decomposes as **77% cache-read of accumulated context, 12% cache-write, 11% output** — 89% is re-reading, not generating. It was never at risk of a limit: 234/400 turns, 1951/7200 s.

The same shape at p9-11-10: 189 turns, 125 tool calls, peak context 181 k, but **total unique tool output ever ingested was only ~48 k tokens** and just 6 greps. Cache-read is 98.6% of input = **$6.34 of the $9.68**. There is no re-exploration waste; there is turn-count waste.

**Consequences for what to tune.** Output is 11–16% of spend, so model and effort changes move the small term. Turn count moves the big one. The three reducible sources of turns, in order:

1. **Task granularity** — p9-11-14 bundles audit findings L-7…L-11 (L-11 itself a basket of six micro-fixes) into 18 numbered plan steps across 30 files whose edits have no dependency on each other. See [04](04-flow-and-config-findings.md#t3).
2. **In-node gate re-runs** — 35 full check-set invocations inside p9-11-14's implementation node, 20 inside its fixing node, duplicating a `testing` node that runs the same five commands in 24 s. See [03](03-prompt-findings.md#p4).
3. **Serial single-hunk edits** — p9-11-10 edited `program.ts` 13 times and `load-config.ts` 7 times, each round-trip re-reading a 100–180 k prefix.

## Diff composition

Averaged across the 20 committed diffs: **~48% tests, ~33% docs, ~18% product code**. That is healthy test discipline and a working doc-sync policy — but it means added-line count is a poor proxy for change risk in this repo. The extreme cases bracket the range: p9-12-02 shipped 3 files / +46 lines / 100% docs for $5.21; p9-11-14 shipped 30 files / +2071 lines for $52.41.

Several tasks' _product_ changes are tiny relative to their cost — p9-11-08's is a single line (``.map((name) => `${name}/**`)`` → ``.map((name) => `**/${name}/**`)``), p9-11-05's is two statements. Those runs still cost $7.96 and $7.20, because planning + review + documentation + supervisor is a fixed floor of roughly $6–7 per task regardless of size. That floor, not the marginal cost, is what makes small tasks look inefficient per line.
