# 02 — Critical defects

Four defects that are genuinely broken rather than merely tunable. All four are present at HEAD; none is specific to wastech-mdlint.

## C1 — A finalize schema deadlock published `summary: "test"` as a whole-task summary {#c1}

**Category** infra (structured output) · **Severity** high · **Confidence** high · **Scope** orchestrator default

The single worst outcome in the 20-run range, and nothing flagged it.

**Evidence.** `.worc/logs/p9-12-06-process-boundary-tests/summary.json`:

```json
{
  "what": "P12.06 Process-boundary test guards and format-gate publish process",
  "summary": "test",
  "follow_ups": [ … 4 entries … ]
}
```

`summary.md` renders as the heading followed by the single word `test`. That file is the operator's primary deliverable and it reached the PR body.

The finalize turn had written a full ~9,300-character summary **three times**. Each was rejected:

```
$ grep -c "Output does not match required schema: root: must have required property 'follow_ups'" \
    .worc/logs/p9-12-06-process-boundary-tests/stages/supervisor/run-000000/1-claude/events.jsonl
3
```

After the third rejection the model collapsed to a minimal probe — `{"summary": "test", "follow_ups": []}` — which validated. The follow-ups were then repopulated from the evaluator findings, which is why four survive alongside a four-byte summary.

**Root cause — a three-way contradiction.** `_finalize_schema` in `core/supervisor.py` puts `follow_ups` in `required` (needed for OpenAI strict mode); `_FOLLOW_UPS_SCHEMA` carries **no description** telling the model to emit `null` rather than omit the key; and the role prompt says the opposite — _"Leave the array empty when nothing qualifies"_. A model that has nothing to report follows the prose, omits the key, and gets rejected with a message that does not explain the fix.

**The degradation guard cannot see it.** `degraded = not summary_md_path.exists()` — a four-byte summary passes, so no warning fired and `supervisor_final` recorded `summary_written: true`.

**This also explains the anomalous 106 s publish** on p9-12-06: 98.8 s of finalize (237 k input, three discarded 9 KB generations) plus ~7 s of git. Publish elsewhere is ~30 s finalize + ~7 s git.

**Lever.** Three edits, all in the orchestrator:

1. Add a `description` to `_FOLLOW_UPS_SCHEMA` stating the contract explicitly — _"always present; use an empty array when nothing qualifies"_ — so the schema and the prose agree.
2. Reword `packaged/flows/implementation/summary.md` to match, and say that the key must be emitted.
3. Give `finalize` a minimum-length floor on `summary` and treat a violation as `degraded`, so a collapse is loud instead of silent.

Not fixed in the Aug-3 packaged version. Evidence: appendix [G](appendix/batch-G.md).

## C2 — `disable_read_isolation` silently drops the `~/.claude` write deny; agents wrote to the host memory store {#c2}

**Category** security / hermeticity · **Severity** high · **Confidence** high · **Scope** orchestrator default

**Evidence — the code.** `providers/claude.py:737`:

```python
if not config.allow_native_memory and not read_isolation_off:
    denied_tools += _native_memory_deny_tools()
```

and three lines below, `read_deny_paths` excludes `claude_config_home()` so the internal deny does not cover it either. With `read_isolation_off = True`, `~/.claude/**` is therefore **neither read-denied nor write-denied**. The comment at `claude.py:709` asserts the opposite: _"The WRITE side (denyWrite / Write/Edit denies / command denies) below still applies."_ For `~/.claude` it does not, because the native-memory rule was the only thing carrying that deny.

**Evidence — the actual argv.** From a frozen `request.json` in this campaign, the `--disallowedTools` blob contains four rules for `~/.codex` and **zero** for `~/.claude`:

```
Write(//Users/a1234/.codex), Edit(//Users/a1234/.codex),
Write(//Users/a1234/.codex/**), Edit(//Users/a1234/.codex/**)
```

**Evidence — it was exercised.** Eight files in `~/.claude/projects/-Users-a1234-Documents-GitHub-wastech-mdlint/memory/` have mtimes inside this campaign, and **all eight fall inside a task's node window**:

| memory file written | mtime | inside task window |
| --- | --- | --- |
| `npm-bin-linking-and-npx-quirks.md` | 07-26 00:55 | p9-11-01 (00:04–01:25) |
| `prettier-mangles-glob-in-bold-markdown.md` | 07-28 00:58 | p9-11-08 (00:45–01:05) |
| `plan-snippets-can-hide-control-chars.md` | 07-28 03:04 | p9-11-11 (02:49–03:10) |
| `build-before-test-cross-package.md` | 07-28 03:42 | p9-11-12 (03:15–03:53) |
| `p9-06-format-gate-byte-sync.md` | 07-28 05:56 | p9-11-14 (04:37–05:57) |
| `p9-remediation-task-pattern.md` | 07-28 08:52 | p9-12-02 (08:44–08:58) |
| `MEMORY.md`, `mcp-wire-schema-validates-before-handler.md` | 07-28 10:59 | p9-12-04 (09:30–11:13) |

Appendix E confirms the mechanism directly on p9-11-12: the `Write` succeeded while the equivalent `Bash` was denied. Appendix B independently caught the read side on p9-11-07, where the implementation node read `p9-remediation-task-pattern.md`.

**Why it matters, in three ways.**

1. **Audit.** These writes land outside the workspace clone, outside the frozen `instruction-bundles/` manifest, and outside the redaction net. "The frozen bundle is the complete input set" is not currently true.
2. **Hermeticity.** Later tasks read what earlier tasks wrote, so a replay on another machine behaves differently. `memory.enabled: false` in `config.yaml` is misleading — cross-task memory was operating, just not the orchestrator's.
3. **It was load-bearing for quality.** The filenames encode exactly the lessons from the review findings of the tasks that wrote them — `npm-bin-linking-and-npx-quirks.md` from p9-11-01's npm bin-linking findings, `mcp-wire-schema-validates-before-handler.md` from the MCP wire-schema class p9-11-07 was caught by. Appendix F verified programmatically that recurring defect classes A (markdown source breakage) and B (incomplete doc sweep) **did not recur in P12**. The most likely reason is this unsanctioned channel. Closing it without enabling the orchestrator's own memory would probably make quality _worse_.

**Lever.** Decide the intent, then make the code say it:

- If host-side native memory should be denied whenever the operator has not opted in, the `read_isolation_off` term at `claude.py:737` is wrong — gate only on `allow_native_memory` — and the comment at `:709` needs correcting either way.
- If it should be permitted, say so in the comment and surface it: log it per run the way `governance_changed` is logged, and note it in the instruction manifest, so the audit trail stops claiming completeness it does not have.
- Either way, if you close it, weigh turning on `memory.enabled` in the same change so the learning survives inside the audited surface.

Per [security.md](../../../.agents/rules/security.md)'s flexibility-first rule this is an escape hatch that must keep an operator opt-out — the finding is that the _write_ side of the hatch is undocumented and invisible, not that the hatch should be removed.

## C3 — The PR-body compactor elides most follow-ups behind a dead link {#c3}

**Category** infra (operator surface) · **Severity** medium-high · **Confidence** high · **Scope** orchestrator default

All 98 follow-ups across the 20 runs were written to the PR body — appendix D verified 98/98. But all 20 tasks share PR #16, and `_bound_pr_body` in `core/supervisor.py` compacts oldest-first as later tasks append, replacing each with a stub pointing at `logs/<task-id>/summary.md`.

That path is inside `.worc/`, which is git-excluded. **For anyone reading the PR on GitHub it is a dead link**, and it covers roughly 65 of the 98 follow-ups — 14 of 20 runs' worth. Bodies were 47–59 KB against a 65,536-byte limit, so compaction was triggered by genuine pressure, not a bug in the threshold.

**Lever.** The stub should point at something a reviewer can open — a gist, an artifact upload, or the `summary.md` content inlined in a collapsed `<details>` block — or the compactor should preserve follow-ups preferentially over prose, since follow-ups are the actionable part. `core/supervisor.py` `_bound_pr_body`.

## C4 — Two latent defects in follow-up assembly {#c4}

**Category** infra · **Severity** medium (one is unreachable on this flow, one is live) · **Confidence** high · **Scope** orchestrator default

**C4a — severity relabelling.** `core/supervisor.py:393` silently relabels `blocking` and `critical` findings as `"medium"`. Unreachable for `implementation.yaml` (whose gate stops those upstream) but **live for `deep_research` and `security_audit`**, where a critical finding would be reported to the operator as medium.

**C4b — the dedup key cannot match.** `core/supervisor.py:481` de-duplicates follow-ups on exact normalised text, but the supervisor paraphrases the evaluator's `reason`, so a paraphrase can never match the raw original. Consequence, measured on p9-12-06's PR body: **10 bullets for ~6 distinct issues, with two pairs carrying contradictory severities** (the same issue as `low` in one bullet and `medium` in another).

**Also visible in the same artifact** (`p9-12-06/summary.json`, quoted under C1), two quality defects in the follow-up records themselves:

- `title` is a truncated prefix of `rationale`, cut mid-word with an ellipsis — e.g. `"The checklist↔inventory pairing the deliverable rests on is not actually enforced. \`.agents/rules/testing.md\` claims \"Ad…"`. A work queue whose titles duplicate their own bodies cannot be triaged without opening every item.
- `action_hint` is `null` on every single follow-up across all 20 runs, because `_to_finding`/`_findings_json` drop the reviewer's `fix` field — which is where the actual remedy lives. Every mechanical follow-up therefore arrives without its fix.

**Lever.** Require `title` to be an independently written imperative of ≤80 characters, distinct from `rationale`, in the finalize schema and the `summary.md` role file; carry the evaluator's `fix` through into `action_hint`; key the dedup on `(path, severity)` or a finding id rather than prose.
