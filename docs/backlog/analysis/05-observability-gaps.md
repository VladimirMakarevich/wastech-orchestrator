# 05 — Observability and audit gaps

What the audit tables could not tell us, and what it cost to work around. Each of these made this post-mortem slower or nearly produced a wrong conclusion.

## O1 — `node_runs` does not record which model ran {#o1}

**Category** infra (audit) · **Severity** high for post-mortem work · **Confidence** high · **Scope** orchestrator default

`node_runs` stores `provider_used` but not `model` or `reasoning`. `provider_attempts` stores neither. So **no SQL query can recover what model executed a node** — the only authoritative record is `stages/<node>/run-*/1-claude/request.json` on the filesystem, or the frozen `runs/control-bundles/<task-id>/`.

This nearly invalidated the entire analysis. The live `.worc/flows/implementation.yaml` reads as one configuration, but it was edited mid-campaign (Jul 28 00:21), so applying it to all 20 runs misattributes era-A runs by a whole model tier. Four appendices independently caught it, each by reading `request.json` and reconciling against `usage_cost`. `tasks.flow_fingerprint` exists but does not resolve to per-node model/reasoning.

**Lever.** Add `model TEXT` and `reasoning TEXT` to the `node_runs` DDL in `state_store.py` (and/or `provider_attempts`), populated from the resolved request. Then grouping a campaign by model is a one-line query instead of a filesystem crawl.

**Compounding factor.** `logging.clean_runs_on_success` defaults to `true`, so a successful task evicts its own `runs/` subtree — including the control bundle that _does_ carry the binding. For a campaign you intend to analyse, set it to `false` beforehand. (It happened to survive here because the bundles live under `.worc/control-bundles/` rather than `.worc/runs/`.)

## O2 — Reasoning volume is invisible in the DB but recoverable from the event stream {#o2}

**Category** infra (instrumentation) · **Severity** medium · **Confidence** high · **Scope** orchestrator default

`usage_reasoning_output` is NULL for all 247 attempts. That is **documented behaviour, not a bug** — `providers/claude.py:824-843` states the Claude CLI folds reasoning into output tokens, so the field stays `None`.

But the stream carries it. `events.jsonl` contains `{"type":"system","subtype":"thinking_tokens","estimated_tokens":N,"estimated_tokens_delta":D}` events — 634 of them in one fixing run, peaking at 9,300 in a single turn. Summing `estimated_tokens_delta` in `parse_stream_json` and threading it into the existing `NormalizedUsage.reasoning_output` field (`providers/base.py:314`, which `providers/codex.py:544` already populates for its provider) would close the gap.

Caveat to respect in the surfacing: the field is named `estimated_`, so it is Claude Code's own estimate, not a billed count. Label it as an estimate.

**Why it is worth doing.** Reasoning volume was the _decisive_ diagnostic for two of this report's largest findings — the era-A effort trap ([01](01-frame-and-economics.md)) and the transcription collapse in `implementation` ([03](03-prompt-findings.md#p1), where implementation thought 880 tokens against planning's 71,534). Both required hand-parsing event logs across dozens of files. This is the highest-value instrumentation fix in the report.

## O3 — `commit_sha_before` is never written {#o3}

**Category** infra (audit) · **Severity** medium · **Confidence** high · **Scope** orchestrator default

`commit_sha_before` is written by nothing anywhere in the codebase, and `commit_sha_after` only by `publish.py:109` — and there it holds a PR URL, not a SHA. Both are NULL for all 289 non-publish node runs in the database.

Consequence: **there is no way to attribute a hunk of the committed diff to the node that authored it.** Every per-node diff claim in this report — which files `documentation` touched, whether `fixing` introduced a new defect, whether implementation stayed inside the plan's file list — had to be reconstructed by reading `events.jsonl` tool calls and correlating them against `current.diff` by hand.

**Lever.** Stamp `HEAD` into `commit_sha_before` at node entry and `commit_sha_after` at node exit in `core/flow/nodes/agent.py`. Per-node diff attribution then becomes a one-line query, and several findings in this report become continuously monitorable rather than archaeological.

## O4 — There is no cost roll-up surface {#o4}

**Category** infra (observability) · **Severity** medium · **Confidence** high · **Scope** orchestrator default

`grep usage_cost` across the source returns a writer and a reader and nothing else — no aggregation, no per-task total, no per-node breakdown, nothing surfaced to the operator.

This produced a real analytical error in this very post-mortem. The supervisor's 147 invocations are the `provider_attempts` rows with `node_run_id IS NULL` (documented in the schema). A natural `GROUP BY` on the `node_runs` join silently drops them into a NULL bucket, so the per-node table sums to $306.02 while the true total is $325.16. The missing $19.14 _is_ the supervisor — and its absence from an obvious query is precisely why the `token-optimization` backlog could be aimed at the wrong layer ([04](04-flow-and-config-findings.md#t6)).

**Lever.** A `worc cost <task-id>` / `--campaign` reporting command, or at minimum a per-task cost line in `summary.md`, splitting flow nodes from the supervisor layer. Any surface that makes the 5.9% / 46% split visible without hand-written SQL.

## O5 — Accepted-finding closure is not recorded {#o5}

**Category** infra (audit) · **Severity** medium · **Confidence** high · **Scope** orchestrator default

The `documentation` node closes accepted review findings — 9 of 15 in appendix E's sample, verified line by line against `current.diff`, including one case where it used the reviewer's exact recommended wording. But nothing records _which_ findings a later node closed.

Two costs follow. First, measuring the real accept-with-findings leak requires diffing the accept-round `findings.json` against the final diff hunk by hunk — which is how this report established that the leak is much smaller than the raw count of 62 suggests. Second, and worse, the supervisor's follow-up list is never reconciled against the post-documentation diff, so **already-fixed findings are re-emitted as outstanding debt**: appendix E measured **60% of reported debt already fixed** (100% for p9-11-12), and appendix B found p9-11-04's follow-up list internally contradictory — item 1 is the documentation node's own note about editing a file, while item 3 says that same file was "skipped".

**Lever.** A `closed_by` field on the finding record, or a reconciliation pass in `finalize` that re-reads the final diff before emitting follow-ups. Either fixes the follow-up quality problem in [02](02-critical-defects.md#c4) as a side effect. A follow-up list that is 60% already-done is a list operators stop reading.

## O6 — Per-step supervisor advisories carry no schema {#o6}

**Category** infra · **Severity** low-medium · **Confidence** high · **Scope** orchestrator default

`stages/supervisor/run-<node>/1-claude/result.json` has `structured_output: null` for every non-terminal step; only the finalize turn carries `{summary, follow_ups}`. The per-step notes are free prose.

Two consequences. Analytically, there is no way to query whether the advisory layer noticed a given problem — every assessment in this report required reading 127 prose notes. Operationally, the notes cannot drive anything: appendix G traced all three call sites that read `evaluations` and two of them explicitly filter to `in_flow_verdict` (`orchestrator.py:2024`, comment: _"skip the supervisor_step/final rows"_), while `FinalizeResult.follow_ups` is returned and never read at the call site. The observation path is `supervisor_step → its own finalize digest → summary.md → PR body → human`, and nothing else.

That is design-intentional and defensible. But if per-step advisories are meant to be actionable, they need a schema.

**A concrete quality defect this surfaced.** **22 of 127 notes declare the task closed on a non-final node** — 11 of them at `review`, which is always followed by `documentation` — with two written self-retractions in the transcript. Cause: the observe prompt never states the flow shape, so the observer cannot know which node is terminal. That is a one-paragraph prompt fix in `supervisor.md`: name the node sequence and say which node closes the run.

## Two non-findings, recorded so they are not re-investigated

- **`memory_delta: false` on every run is deterministic, not a signal.** `emit_delta = memory_on = false`, so `delta` is unconditionally `None`. Nothing to interpret.
- **The supervisor's "noted in memory" phrasing is not confabulation.** It looked like a hallucination given `memory.enabled: false`, but appendix G found the precedent verbatim in both the step report and `plan.md:34` — "memory" there means its own warm session, an unlucky collision with the product feature name. The separate, real finding about host-side memory is [C2](02-critical-defects.md#c2).
