# Batch E — accepted-first-time but expensive: `p9-11-09`, `p9-11-12`, `p9-11-13`

**Verdict in three lines.** All three runs were genuinely clean work — zero scope drift, zero missed acceptance criteria, zero retries, and the "15 dropped findings" turn out to be **9 silently closed in-flow by the `documentation` node and 6 shipped**, of which exactly **one is a real correctness defect** (`stageWrite` temp-file leak, still on `main`). The money went almost entirely to **turn count at high context** in `implementation`: 173 assistant turns on a context that grew to 199 K, of which ~20 % were a documentation pass the `documentation` node then redid, and ~12 were in-node reruns of the same gate the `testing` node runs next. And the single most serious thing I found is not a cost issue at all: **`security.disable_read_isolation: true` silently un-denies Claude's native memory writes**, so `p9-11-12`'s implementation node wrote two files into `~/.claude/projects/.../memory/` — outside the workspace, outside `.worc/`, outside the redaction net, and read back by later runs.

---

## 1. Run frames

Ledger: all three `done`, attempt 1, `fix_iterations: 0`, `terminal_cleanup: completed`, branch `feat/p10-p11-remediation`, PR #16, `auto_merged: false`. `validation_report.json` = `passed: true, completeness: "complete"` for all three (no under-specification going in). Path taken: `planning → implementation → testing(pass) → review(accept) → documentation → publish`. No `fixing`, no HITL, no fallback (`provider_used='claude'`, `stage_attempts=1` everywhere).

`SELECT … FROM provider_attempts p JOIN node_runs n ON n.id=p.node_run_id` + `node_runs` timings:

| task | node | secs | input tok | cost | assistant turns | tool calls | peak ctx |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **p9-11-09** ($18.99 / 19.76 M) | planning | 774 | 3.61 M | $4.72 | 87 | 57 (37 Read, 14 Grep) | 162 K |
|  | **implementation** | **1018** | **13.95 M** | **$10.57** | **173** | **117** (50 Edit, 40 Bash, 23 Read, 4 Write) | **199 K** |
|  | testing | 23 | — | — | — | — | — |
|  | review | 260 | 1.36 M | $2.03 | — | — | — |
|  | documentation | 101 | 0.54 M | $0.90 | 29 | 16 | 52 K |
|  | publish | 41 | — | — | — | — | — |
|  | supervisor (6 turns) | — | 0.31 M | $0.76 | — | — | — |
| **p9-11-12** ($12.10 / 11.13 M) | **planning** | **1122** | 2.96 M | $3.58 | 103 | 65 (27 Read, 27 Grep, 10 Glob) | 122 K |
|  | implementation | 523 | 4.68 M | $4.13 | 121 | 75 (33 Bash, 23 Edit, 18 Read) | 110 K |
|  | review | 362 | 2.20 M | $2.54 | — | — | — |
|  | documentation | 108 | 0.69 M | $0.96 | 31 | 18 | 49 K |
|  | supervisor (6) | — | 0.59 M | $0.89 | — | — | — |
| **p9-11-13** ($18.04 / 19.45 M) | planning | 558 | 4.01 M | $4.31 | 117 | 76 (39 Read, 32 Grep) | 147 K |
|  | **implementation** | 834 | **10.31 M** | **$7.77** | **187** | **116** (51 Bash, 38 Edit, 25 Read) | 150 K |
|  | review | 327 | 3.02 M | $3.14 | — | — | — |
|  | **documentation** | 191 | **1.77 M** | **$1.90** | 54 | 33 (12 Read, 9 Edit, 8 Bash, 4 Grep) | 80 K |
|  | supervisor (6) | — | 0.34 M | $0.92 | — | — | — |

**The cost law.** Input tokens ≈ Σ(context at each turn) ≈ turns × avg-context, and context grows monotonically, so cost is quadratic in turns. `p9-11-09` implementation: context went `19.8 K → 46.9 K → 93.9 K → 141.4 K → 198.8 K` over 173 turns (sampled every 10th assistant event). Empirical blended rate from the artifacts themselves: `$10.574 / 13.95 M = $0.758 per 1 M` input (cache-read dominated; `p9-11-13` impl gives $0.753/M — consistent). **A turn at 150 K context costs ≈ $0.11.** That is the unit every recommendation below is priced in. `max_turns: 400` was never the binding constraint (173 assistant events ≈ 117 tool calls).

**Scope check (`current.diff` vs `task.normalized.json`) — clean on all three.** No drift, no gold-plating, no missed criteria:

- **09** (18 files): new `packages/core/src/atomic-write.ts` + `markdown/newline.ts`, routed through `fix.ts`/`rules/sec.ts`/`init-command.ts`/`commands.ts`, 4 test files, 5 doc files. The one addition beyond the stated deliverables — the `existingSchemaUnreadable` flag and new `"unreadable"` schema-write outcome — is _causally required_ by the change (under `rename`, P11.03's "unreadable ⇒ absent ⇒ write it" degrades to a silent clobber) and is explicitly surfaced in `summary.md`. Not gold-plating. Constraint "helper belongs in `packages/core`" honored.
- **12** (17 files): chose direction (A), reused P11.02's `resolvesOutsideRoot` as the constraint demanded. The two files that _look_ like drift are not: `packages/mcp-server/src/tools/lint.ts` is a one-line tool-description change (`"REF-001/REF-003, SEC-003 and STR-001 may probe or read paths"`) required because STR-001 now reaches the filesystem, and `packages/core/src/types/micromatch.d.ts` adds one already-installed method (`scan`) with a why-comment. No new dependency.
- **13** (19 files): removed the dead GRP options (direction recorded), fixed SIZE-001 supersession, updated the `grp.ts:21` comment per the constraint. `docs/mdlint_v2/P3-rules/07-llm-rules.md` is not the out-of-scope LLM-001 work — SIZE-001's spec lives in that file. The `requirements/` edits are the reviewer's own request (finding 1) and the supervisor correctly asked for human sign-off on them.

---

## 2. Findings, ranked by impact

### F1 — `disable_read_isolation: true` silently un-denies Claude's **native memory writes**; an agent wrote outside the workspace

- **category** infra / config / security · **severity** high · **confidence** high (argv + sandbox settings + tool results + on-disk mtimes all agree)

**EVIDENCE.** The adapter-owned OS sandbox file does deny the path — `/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/logs/p9-11-12-str001-reach/stages/implementation/run-000178/1-claude/claude-sandbox-settings.json`:

```json
"filesystem": {"denyRead": [], "denyWrite": ["…/.worc", "…/.worc/.env", "/Users/a1234/.claude", "/Users/a1234/.codex", …]}
```

But the **tool-level** deny list in the same run's `request.json` `argv` omits `~/.claude` while including `~/.codex`:

```
--disallowedTools Bash(git commit:*),…,Write(//Users/a1234/.codex),Edit(//Users/a1234/.codex),
  Write(//Users/a1234/.codex/**),Edit(//Users/a1234/.codex/**),…
```

Result, from `…/stages/implementation/run-000178/1-claude/events.jsonl` (tool_use → tool_result pairs):

```
Write -> /Users/a1234/.claude/projects/-Users-a1234-Documents-GitHub-wastech-mdlint/memory/build-before-test-cross-package.md
    is_error: None | "File created successfully at: …"
Bash  -> cat >> ".../memory/MEMORY.md" <<'EOF'
    "(eval):1: operation not permitted: /Users/a1234/.claude/…/memory/MEMORY.md"
Edit  -> /Users/a1234/.claude/…/memory/MEMORY.md
    is_error: None | "The file … has been updated successfully."
```

The shell write was blocked; the **native `Write`/`Edit` succeeded**. On disk: `ls -la ~/.claude/projects/-Users-a1234-Documents-GitHub-wastech-mdlint/memory/` shows `build-before-test-cross-package.md` mtime **Jul 28 03:42**, exactly inside p9-11-12's implementation window (01:34–01:43 UTC = 03:34–03:43 local). A sweep of all 21 tasks' `events.jsonl` for tool calls referencing that tree finds **28 such calls across 9 tasks** (`p9-11-01` fixing/planning, `p9-11-07/08/11/12` implementation, `p9-11-14` documentation, `p9-12-02/04/06`) — including `p9-11-14/documentation` reading `/Users/a1234/.claude/projects/…/tool-results/b3hj359gy.txt`, another session's cached tool output.

**Root cause.** Two things compound.

1. Claude Code's `sandbox.filesystem.denyWrite` governs **only sandboxed command execution**; the built-in `Read`/`Edit`/`Write` tools go through the permission system instead (confirmed against the official docs: _"Built-in file tools: Read, Edit, and Write use the permission system directly rather than running through the sandbox"_ — `code.claude.com/docs/en/sandboxing.md#scope`). So `denyWrite` on `~/.claude` is dead weight for `Write`/`Edit`; only a `Write(//…)`/`Edit(//…)` deny rule (which _does_ beat `--permission-mode acceptEdits`) closes it.
2. The orchestrator knows this and has the rule — but skips it. `src/wastech_orchestrator/providers/claude.py:737`:

```python
if not config.allow_native_memory and not read_isolation_off:
    denied_tools += _native_memory_deny_tools()
claude_home = claude_config_home()
read_deny_paths = [p for p in internal_deny_read_paths if p != claude_home]
```

`read_isolation_off` alone suppresses the deny, and line 740 additionally drops `claude_home` from the internal deny set — so with `disable_read_isolation: true` there is **no** `Write`/`Edit` rule for `~/.claude` at all. The comment at `claude.py:709` asserts the opposite: _"The WRITE side (denyWrite / Write/Edit denies / command denies) below still applies."_ For `~/.claude` it does not. `allow_native_memory` defaults to `False` (`config/schema.py:321`) and the target never sets it, so the operator never opted in. This is present in `HEAD` (`git show HEAD:…/claude.py | grep -n` → line 737), not just in what ran.

The orchestrator's own docstring states the stakes (`claude.py:393-401`): _"a durable store OUTSIDE the repo, so anything written there escapes `current.diff`, the commit, the redaction net, and the orchestrator's own audit (an unredacted `originSessionId` was observed leaking)."_

**Consequences beyond the boundary breach.** (a) The runs are **not hermetic**: `p9-11-07`, `p9-11-08`, `p9-11-11`, `p9-11-12`, `p9-12-02` read notes written by _earlier_ runs in the same batch, invisible to `state.db`, `.worc/logs/`, and the frozen instruction bundles — so the whole post-mortem's premise "`memory.enabled: false`, therefore no memory" is wrong, and cross-run behavior differences may be memory effects. (b) The rendered security contract every node receives says _"Make only the changes this task requires, and only inside your assigned workspace clone"_ — the run violated it. (c) The `Read` of `MEMORY.md` put its full contents into `.worc/logs/…/events.jsonl`, i.e. external, un-redaction-scanned content entered the audit artifacts.

**Lever** (orchestrator default, every repo — this is not a target-config workaround):

- `src/wastech_orchestrator/providers/claude.py:737` — decouple the two switches: emit `_native_memory_deny_tools()` whenever `not config.allow_native_memory`, regardless of `read_isolation_off`. Read-isolation-off is about _native project discovery_ (`--setting-sources project`, `CLAUDE.md`); it has nothing to do with granting a write to an unaudited external store. Keep the `claude_home` exclusion at line 740 for the **read** kind only.
- Belt-and-braces in the same adapter: add `"autoMemoryEnabled": false` to the `--settings` file built by `build_sandbox_settings` (`claude.py:532-582`) and/or export `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` in the allowlisted env — an env var survives every setting scope and `--setting-sources`. Gate both on `allow_native_memory`.
- Fix the now-false comment at `claude.py:709`.

**Expected impact.** Restores workspace-write confinement and run reproducibility; removes an off-audit, off-redaction persistence channel; makes `memory.enabled: false` mean what the operator thinks it means. No cost change.

---

### F2 — Accepted findings _do_ survive as follow-ups, but the list is never reconciled against the post-review diff: **9 of 15 are already fixed**

- **category** flow / supervisor · **severity** high · **confidence** high (each closure verified line-by-line in `current.diff`)

**EVIDENCE — the mechanism exists and works.** `.worc/flows/implementation.yaml` sets `supervisor: {emit_follow_ups: true}`. `src/wastech_orchestrator/core/supervisor.py:784` merges the supervisor's own list with `_evaluator_finding_follow_ups(evaluations)` (added 2026-07-25, commit `61ef90f` — live for these runs), which converts every finding on each evaluator's **last** in-flow verdict into a `FollowUp` (`supervisor.py:430-443`) and renders them as `## Technical debt / follow-ups` in `summary.md` (`_render_follow_ups_section`, line 333) and appends the section to the PR body (line 817-818). Verified present: `summary.json` `follow_ups` has 10 / 6 / 7 entries; `grep -c "Temp+rename silently bypasses" p9-11-09-atomic-writes/pr_body_appended.md` → `1`. **So the raw premise "silently lost" is false — they reach the operator.**

**EVIDENCE — but most were already fixed before the summary was written.** The `documentation` node receives the review artifact as a context file (`stages/documentation/run-000187/rendered-prompt.md`, last line):

```
- review: /Users/…/.worc-io/p9-11-13-grp-size-hygiene/stages/review/run-000186/findings.json
```

and it reads it in **all three** runs (`events.jsonl` tool call #5 or #6: `cat …/findings.json` / `Read …/findings.json`) and then fixes the doc-scoped findings. Closures verified in the final `current.diff`:

| run | finding | severity | closed in final diff? | evidence |
| --- | --- | --- | --- | --- |
| 09 | F2 temp+rename bypasses target write perm | medium | **yes (docs half)** | doc node's `Edit` payload adds to `docs/guide/output.md`: _"on Linux/macOS a **read-only document no longer blocks a fix** … Keep a file out of `--fix` with [`exclude`] rather than with its file mode"_ + a full "Accepted, documented side effect" note to `09-atomic-writes.md`. Exactly the reviewer's option (a). Only the `why`-comment in `stageWrite` (code) is missing. |
| 09 | F3 `cli.md` overstates the guarantee | low | **yes** | doc node rewrote it to _"a failure while staging leaves the repository entirely untouched; once the renames begin, a prefix of them may already have landed"_ — the reviewer's exact wording. |
| 12 | F1 slash-free glob silently `**/`-rewritten | medium | **yes** | `STR-001.md` gains _"**A glob entry is not root-relative unless it contains a `/`.** A slash-free pattern is matched at any depth — `*.md` behaves as `**/*.md`"_. |
| 12 | F2 "indistinguishable from missing" contradiction | low | **yes** | reworded to _"finding is emitted at the same severity as a missing file and is identical whether or not the [path exists]"_ — the property the reviewer named. |
| 12 | F3 `mcp-server.md` 120-col line | low | **yes** | hunk shows the paragraph rewrapped. |
| 12 | F4 `SEC-003.md` ragged wrap | low | **yes** | hunk shows the bullet rejoined. |
| 13 | F1 R5/R7/C5 rows advertise removed keys | low | **yes** | R7 now reads _"…`SIZE-001`'s own `overrides[].pattern`, and `GRP-001` — see [P11.13]…"_; R5 and C5 both updated. |
| 13 | F2 execution notes record dead keys as live | low | **yes** | _"**Закрыто в [P11.13]…: ключи удалены, а не подключены**"_ added. |
| 13 | F5 missing `#settingssiterouter` anchor | low | **yes** | both `GRP-001.md` and `GRP-002.md` now link `../configuration.md#settingssiterouter`. |
| **09** | **F1 `stageWrite` leaks a `.tmp` on partial write** | medium | **NO** | real correctness defect, shipped. |
| 09 | F5 `chmod` restore not in `finally` | low | **NO** | real test-fragility bug (cascading `EACCES` in shared `afterEach`), shipped. |
| 09 | F4 no CLI-level e2e for `FixWriteError → exit 2` | low | **NO** | coverage gap. |
| 09 | F6 `offerCiWorkflow` discards the errno | low | **NO** | minor diagnostic quality. |
| 13 | F3 untested warn-only-crossed arm | low | **NO** | coverage gap. |
| 13 | F4 duplicate cross-metric test pinning order | low | **NO** | test cleanup. |

**So: 9 closed, 6 shipped, and the follow-ups list reports all 15 as open debt — 60 % false positives, 100 % for `p9-11-12`.** All four of p9-11-12's `follow_ups[3..6]` are already-fixed items with `action_hint: null`.

**Secondary defects in the same mechanism, all visible in `p9-11-09/summary.md`:**

- _Duplicate with conflicting severity._ Follow-up #1 (supervisor-authored, `severity: high`, with an `action_hint`) and #5 (mechanical copy, `severity: medium`, `action_hint: null`) are the **same** `stageWrite` defect. `_merge_follow_ups` (`supervisor.py:487-503`) dedups on `_follow_up_key` = _exact_ normalized `title + rationale + paths` — a paraphrase never collides. The operator sees one bug twice at two severities.
- _Unreadable titles._ `_finding_to_follow_up` (`supervisor.py:386-389`) uses the finding's `reason` verbatim as the title, truncating at `_FINDING_TITLE_MAX = 120`, so `summary.md` renders `- **[medium] `stageWrite`leaks a temp file on a partial write. It computes`tempPath`, calls `await writeFile(tempPath, ..., { flag:…** — <the same text again>`.
- _The reviewer's remedy is thrown away._ `findings.json` carries a `fix` field per finding (schema `_FINDINGS_SCHEMA`, `core/flow/nodes/evaluator.py:121`) with genuinely actionable text — e.g. for 09/F1: _"In `stageWrite`, wrap everything after the `tempPath` computation in a try/catch that does `await unlink(tempPath).catch(() => undefined)` and rethrows the original error… Add a test for it"_. But `_to_finding` (`evaluator.py:546-570`) projects only `severity/reason/paths`, and `_findings_json` (line 573-578) persists only those three, so `_finding_to_follow_up` has nothing to put in `action_hint` — hence `null` on every mechanical follow-up. The remedy exists on disk at `stages/review/run-*/findings.json` and never reaches the operator's debt list.

**Levers** (orchestrator default):

1. **Reconcile before emitting.** `supervisor.py:784` — filter `_evaluator_finding_follow_ups(...)` against the paths changed _after_ the verdict's `source_node_run_id`. `node_runs.commit_sha_before/after` per node already exists, and the in-flight `core/supervisor_packet.py` is building exactly the deterministic diff facts this needs (`_DIFF_NEW_PATH_RE`, changed-paths list) — land it in the same change. Minimum viable version: drop a mechanical follow-up whose `paths` are all touched by a post-review node's diff, or downgrade it to `- (may already be addressed by the documentation step)`. Path-level is enough to have removed 9 of 9 false positives here (every closure was in a file the doc node edited).
2. **Carry the `fix` through.** `core/flow/nodes/evaluator.py:546-578` — add `fix` to the typed `Finding` (`core/flow/engine.py:92-112`) and to `_findings_json`, then set `action_hint=finding["fix"]` in `supervisor.py:390-396`.
3. **Better dedup + real titles.** `_follow_up_key` (`supervisor.py:481`) should also match on `paths`-only overlap + a severity max, so a paraphrase collides; and `_finding_to_follow_up` should synthesize a short title (first sentence, ≤ 80 chars) rather than a 120-char prose slice.

**Expected impact.** The follow-ups section becomes a list an operator will act on instead of skim past — which matters because item #1 on it is the only real bug that shipped in this batch.

---

### F3 — The routing gate (`gate_severity: high`) is never disclosed to the reviewer, so a genuine correctness bug was filed `medium` and shipped

- **category** prompt / flow · **severity** high · **confidence** high

**EVIDENCE.** The dropped finding, verbatim from `sqlite3 state.db "SELECT findings_json FROM evaluations WHERE task_id='p9-11-09-atomic-writes' AND kind='in_flow_verdict'"`:

> **[medium]** `stageWrite` leaks a temp file on a partial write. It computes `tempPath`, calls `await writeFile(tempPath, ..., { flag: "wx" })`, and only returns the `StagedWrite` on success — so if `writeFile` fails _after_ creating the file (ENOSPC, EDQUOT, EIO), the temp exists on disk but is never pushed into `staged`, and `discardTemps(staged)` in `writeFilesAtomic` cannot remove it. **That is precisely the ENOSPC scenario the module header calls out**, and it contradicts the "no `.tmp` residue" property the new tests assert on every other failure path (existing tests only exercise EEXIST/ENOENT/EACCES failures, where no file is created, so the hole is untested).

and the second-most-serious:

> **[medium]** Temp+rename silently bypasses the _target's_ write permission, an unflagged behavior change. `rename()` needs write permission on the directory only, so `--fix` now rewrites a `chmod 0444` Markdown file and `init --on-existing overwrite` now replaces a read-only `wastech-mdlint.config.json`, where the previous truncate-and-write failed with EACCES. … so the user gets no signal their write-protected file was rewritten.

The supervisor — a _cheaper_ model (`claude-sonnet-5`/`medium`) — got the grading right and said so, in the documentation-step advisory (`evaluations`, `kind='supervisor_step'`, `p9-11-09-atomic-writes`):

> **Real bug found, correctly left unfixed here**: `atomic-write.ts:100`'s `stageWrite` can leak a `.tmp` file if `writeFile` fails after temp creation (e.g. `ENOSPC`/`EIO`) — this directly **contradicts the "no `.tmp` residue on any failure path" guarantee** that the atomic-write test suite from the implementation step claims to assert. … **Verdict**: … the **temp-leak bug and the un-guarded `chmod` restore are real defects that contradict this task's own stated invariants** and should block sign-off or at minimum get an explicit follow-up ticket before merge — they're not cosmetic.

**Root cause — the routing rule is invisible in the prompt.** `core/flow/schema.py:28-31` defines `SEVERITY_ORDER = ("blocking","critical","high","medium","low")` and `DEFAULT_GATE_SEVERITY = "high"`; `core/flow/nodes/evaluator.py:289-291` routes:

```python
gate_rank = _severity_rank(node.gate_severity)
if not any(self._is_blocking(f, gate_rank) for f in raw_findings):
    return "accept", False
```

Neither the target's `.worc/flows/implementation.yaml` `review` node nor the packaged default sets `gate_severity`, so `high` applies and **`medium` is structurally un-actionable**. The reviewer was never told. `stages/review/run-000159/rendered-prompt.md` (the literal text) says only:

> Report each finding with a severity, and mark anything that must change before merge as **blocking**. Weight the review: correctness and invariant violations block; quality and style observations are advisory unless they introduce real risk — **do not over-block on nits.**

`blocking` _is_ a legal enum value (`output-schema.json` `enum: ["blocking","critical","high","medium","low"]`), so the instruction is implementable — but nothing states the consequence of _not_ using it, and two separate sentences ("do not over-block on nits", "Coverage completeness … is **advisory**, not blocking") push the calibration down. A reviewer that reads `medium` as "important but not a hill to die on" is behaving reasonably and still ships the bug.

**Levers.**

1. **Role prompt (primary).** Add one paragraph to the target's `.worc/flows/implementation/review.md` **and** the packaged `src/wastech_orchestrator/packaged/flows/implementation/review.md`, e.g.: _"Severity is routing, not commentary. `blocking`/`critical`/`high` send the diff back to a `fixing` step; `medium` and `low` are recorded and **ship as-is** — nothing in this flow will fix them. If a finding names a defect that must not ship, it is `high` or above, whatever its blast radius. Reserve `medium`/`low` for things you are content to see merged."_ — **Packaged-default check:** I diffed the target's active Jul-25 copies against the packaged defaults. `review.md` differs only in de-specialization (mdlint-specific invariants → generic wording); **neither version mentions `gate_severity` or the accept/rework consequence.** The packaged default does _not_ already fix this.
2. **Optional flow tightening (target-only, per-repo judgement).** `.worc/flows/implementation.yaml` `review` node → `gate_severity: medium`. The packaged flow already documents the knob (`packaged/flows/implementation.yaml:36` and `:106` — `# gate_severity: high  # min finding severity that gates`), so it needs no code change. Cost: it would have added one `fixing` round to `p9-11-09` and `p9-11-12` (the 12 mediums were doc-only and got fixed anyway) — from the brief, `fixing` averages $5.31, so ~+$5/run on ~35 % of runs. Recommend (1) first and measure; only lower the gate if reviewers keep mis-grading after the prompt states the rule.
3. **Cheap safety net, no rework cost.** `_finding_to_follow_up` currently keeps the finding's own severity. Have the supervisor's finalize prompt explicitly re-grade accepted findings for "would this have blocked?" — it demonstrably already does this well (it upgraded `stageWrite` to `high` unprompted) — and mark such items `**must-fix before merge**` in the PR-body section rather than "technical debt".

**Expected impact.** The one real defect in this batch becomes either a `fixing` round or an unmistakable must-fix line in the PR, instead of item #5 of 10 in a debt list where 6 entries are already done.

---

### F4 — `implementation` does the documentation pass that the `documentation` node then redoes — ~20 % of the most expensive node's turns, at its highest context

- **category** prompt · **severity** high · **confidence** high

**EVIDENCE.** From `stages/implementation/run-*/1-claude/events.jsonl`, tool calls filtered to `docs/**` and `README.md`:

| run | impl doc edits | doc-node doc edits | files **both** nodes edited | files only the doc node touched |
| --- | --- | --- | --- | --- |
| 09 | **10** across 5 files | 3 across 3 | `cli.md`, `output.md`, `09-atomic-writes.md` | _(none)_ |
| 12 | **10** across 7 files | 6 across 4 | `STR-001.md`, `SEC-003.md`, `mcp-server.md`, `12-str001-reach.md` | _(none)_ |
| 13 | **23** across 9 files | 9 across 6 | `GRP-001.md`, `GRP-002.md`, `13-grp-size-hygiene.md` | `requirements/01-…`, `requirements/02-…`, `p1-p3-execution-notes.md` |

In **09 and 12 the `documentation` node touched not one file the implementation node had left alone** — its entire contribution was revising the implementation node's prose (which is exactly how it closed the review's doc findings, per F2). On `p9-11-09` the doc pass is tool calls **98–110** of 117 — 13 calls sitting at 141–160 K context, i.e. ~19 assistant turns × ~$0.11 ≈ **$2.1 on that node alone**, and it also inflates every one of the ~20 turns after it. On `p9-11-13` it is 23 of 116 tool calls (20 %).

**Root cause.** The flow's design intent is explicit — `packaged/flows/implementation.yaml` header: _"After review accepts the code, `documentation` updates the target project's docs to match the change just shipped"_, and `review.md` tells the reviewer _"Documentation, changelog, and status-doc updates run in a later step of this flow, so do not flag those as missing."_ But **`implementation.md` never says "don't"** — in either the target's Jul-25 copy or the packaged Aug-3 default (I diffed them; the packaged version keeps the "Authoring And Documentation Deliverables" section, which is about doc-_deliverable tasks_, and adds nothing about deferring the project's own docs). Meanwhile the target repo's `AGENTS.md` tells every agent to update docs _in the same change_, so the implementation agent is obeying the repo and duplicating the flow.

**Lever.** Add to `.worc/flows/implementation/implementation.md` **and** `src/wastech_orchestrator/packaged/flows/implementation/implementation.md`, next to `## Verify`: _"**Do not update the project's documentation.** A later `documentation` step in this flow owns every README/guide/changelog/status-doc edit and will see your diff. Confine yourself to code, tests, and any doc file the task's acceptance criteria name explicitly (e.g. its own phase task file). Note anything a doc will need to say in your closing message instead of editing it."_ Pair it with one line in `documentation.md`: _"The implementation step may have drafted doc text already — verify and correct it rather than assuming the docs are untouched."_

Also worth surfacing in the same edit (F2 makes it load-bearing): `documentation.md` says nothing about the `review` context file it is handed, even though closing the reviewer's doc findings is empirically its highest-value output (9 of 15 findings in this batch). Add: _"You are given the review's `findings.json`. Close every finding whose fix is a documentation change; for each one you cannot close (it needs code or tests), say so explicitly in your closing message so it is carried forward."_ The doc node already did this by initiative on all three runs — making it instruction turns luck into design, and gives the supervisor a clean signal for F2's reconciliation.

**Expected impact.** ~15–25 turns removed from the most expensive node, at its most expensive context: **≈ $2–3 per run on `implementation` (20–28 %)**, plus a cheaper, better-targeted `documentation` node. No loss of coverage — the doc node was already redoing the work.

---

### F5 — `implementation` re-runs the whole `testing` command set 3–4 times in-node; 10–15 gate invocations per run

- **category** prompt · **severity** medium · **confidence** high

**EVIDENCE.** Gate/test commands issued _inside_ `implementation` (from `events.jsonl`): **09: 14 · 12: 15 · 13: 10.** On `p9-11-09` the last three are near-identical full sweeps:

```
 #97 npm run typecheck 2>&1 | tail -5 && npm test 2>&1 | tail -6 && npm run build 2>&1 | tail -5
#113 npm run typecheck … && npm run lint … && npm run format … && npm test …
```

`node_runs` then shows the `testing` node running the same `checks.command_sets.default` (typecheck, lint, format, test, build) in **23 s** — `status: passed` on all three, as on all 29 runs in the batch. So the in-node sweeps bought nothing the 23-second gate would not have caught.

**Root cause.** The target's `implementation.md` `## Verify` block is an unbounded standing instruction:

> Before finishing, run the checks that apply to the touched scope and confirm they pass: `npm run typecheck` / `npm test` / `npm run build`. Use `npm run lint` and `npm run format` when the touched scope requires style verification.

Nothing says _once_, and nothing tells the agent a dedicated gate follows that will route failures back to it. The packaged default is _worse_ on this axis — it generalizes to _"run whatever check commands this project defines for the code you touched … catching a failure now saves a full review/fix round trip later"_, i.e. it actively encourages repetition without bounding it.

**Lever.** In both `implementation.md` copies, replace the standing instruction with a bounded one: _"Run the project's checks **once**, at the end, after your last edit. A separate `testing` gate runs the same command set immediately after you finish and routes any failure straight back to you with the logs — so do not iterate the full suite; use a single targeted test file while developing and one full pass to close."_ **Packaged-default check: does not fix it; makes it slightly worse.**

**Expected impact.** ~6–10 turns at 100–190 K context per run ≈ **$0.7–1.1** on `implementation`, with no added risk (the gate is 23 s and catches the same class).

---

### F6 — `documentation` runs `workspace-write` straight into `publish` with no re-review; on `p9-11-13` it edited "locked" requirement docs unreviewed

- **category** flow · **severity** medium · **confidence** high

**EVIDENCE.** `.worc/flows/implementation.yaml` edges: `{from: review, to: documentation, outcome: accept}` then `{from: documentation, to: publish}`. The `documentation` node is `permission_profile: workspace-write` and _"its edits join the same diff the orchestrator commits"_ (flow header). The reviewer is explicitly told it is seeing a pre-documentation diff (`review.md`: _"The diff you see is captured **before** the documentation step runs"_). On `p9-11-13` the doc node authored **+22/−14 lines across three files no reviewer ever saw** (`requirements/01-configuration.md` +5/−1, `requirements/02-rules-engine.md` +14/−13, `p1-p3-execution-notes.md` +3/−0) out of a `+355/−169` diff — and two of those are the target's `AGENTS.md`-designated _locked_ planning tier. The supervisor caught it and filed the only genuinely valuable supervisor-authored follow-up in the batch (`p9-11-13/summary.json` follow-up #2, `severity: medium`):

> Requirements-tier docs reconciled for a config-schema change — confirm this precedent is acceptable going forward … altering 'locked' requirements docs during a remediation task is unusual enough to warrant explicit human sign-off rather than assuming the precedent generalizes.

**Root cause.** The graph has no gate after `documentation`; the supervisor is advisory by construction. Note the doc node was not freelancing — review finding 1 asked for exactly this — but nothing verified that it interpreted the reviewer correctly, and nothing checked the locked-tier boundary.

**Lever — cheapest first.** Do **not** add a second `review` pass (a $2–3 node to check ~20 doc lines). Instead:

- (a) `documentation.md` (target + packaged): _"Report every file you edited in your closing message, and flag any file the project marks as locked/authoritative/frozen — never edit one without saying so."_ Turns the supervisor's catch from luck into a guaranteed signal.
- (b) If a real gate is wanted, it is a graph choice, not a code change: add an evaluator node between `documentation` and `publish` in the operator's own flow YAML with `blocking: false`, `gate_severity: high`, and a cheap per-node override (`model: claude-sonnet-5`, `reasoning: medium`) — the per-node `provider`/`model`/`reasoning` slots are already supported on evaluator nodes. Budget ≈ $0.3–0.5/run for the ~10–30 % of the diff that currently ships unreviewed.

**Expected impact.** Closes the last unreviewed slice of the committed diff, and makes locked-tier edits an explicit operator decision rather than an inferred precedent.

---

### F7 — The supervisor observes without knowing the flow graph, so it declares the task closed at `review` and then has to retract

- **category** prompt / supervisor · **severity** low–medium · **confidence** high

**EVIDENCE.** Two consecutive `supervisor_step` advisories on `p9-11-09-atomic-writes`:

> **[review]** "Review step accepted — **this is the final step of the P11.09 (atomic writes) task, so the whole task closes here.** ## End-of-task summary …" _(a full ~300-word end-of-task write-up)_
>
> **[documentation]** "**Correction to my last summary: review acceptance wasn't the task's final step after all** — a documentation step followed it, so the task was still open. Good to flag now."

**Root cause.** The observation turn is handed the completed step's outcome but not the flow's remaining nodes. `core/supervisor.py` has no notion of it (`grep -n "remaining\|final step\|next node"` → only `supervisor.py:702`, about which node kinds get observed). So the observer guesses from step semantics and guesses wrong on the one flow where `review → documentation` follows an `accept`.

**Cost of the bug:** one wasted 300-word finalize-shaped turn plus a retraction turn (~$0.1–0.15/run at Sonnet rates, 20 runs ≈ $2–3), and — more importantly — the risk that a premature "task closes here" summary is the one an operator reads.

**Lever.** `src/wastech_orchestrator/core/supervisor.py` observation-prompt builder (around `supervisor.py:1452-1500`, where `with_follow_ups` and the digests are assembled) — add one deterministic line from the snapshot: `Flow position: node 4 of 6; nodes still to run: documentation, publish.` The snapshot adjacency is already available to the orchestrator (`ctx.snapshot.adjacency`, used at `core/flow/nodes/evaluator.py:386`). **In-flight-work check:** the uncommitted `core/supervisor_packet.py` on this branch addresses the _finalize_ turn's determinism and bounding (`_DIFF_INLINE_MAX`, `_STEP_MESSAGE_MAX`, `_OBSERVATIONS_MAX`) — it does **not** give the observation turn the graph, so this finding is not already fixed by that work. It is, however, the natural place to add it, and F2's reconciliation wants the same packet.

---

### F8 — `planning` costs $3.6–4.7 and 557–1122 s to produce a plan the implementation node then re-derives from scratch

- **category** prompt / model · **severity** medium · **confidence** medium-high

**EVIDENCE.** `planning` is the #2 cost centre in all three runs, and `p9-11-12`'s is the second-longest in P11 (`node_runs`: 1122 s, 103 assistant turns, 65 tool calls — 27 `Read` + 27 `Grep` + 10 `Glob`, peak context 122 K). It emitted 38,986 output tokens for a 12.5 KB `plan.md` (≈ 3 K tokens) — the rest is `xhigh` deliberation. Then `p9-11-09`'s implementation node opens with **31 consecutive discovery calls before its first write** (`events.jsonl` calls 1–31: `plan.md`, then `fix.ts`, `init-command.ts`, `commands.ts`, `sec.ts`, `index.ts`, `errors.ts`, `deterministic-sort.ts`, `tbl.ts`, `document-types.ts`, `path-resolve.ts`, five `sed -n` windows into `init.e2e.test.ts`, `tsconfig`, `eslint.config`, …) — i.e. it re-walks the same tree the planner just walked, at 20–80 K context per turn.

**Root cause.** Two prompts, one gap. `planning.md` asks for tracing and citation (_"Trace the relevant code paths end to end … Verify every path you cite against the current tree"_) but never asks for a **file manifest with the reason each file is touched**, and never states that the plan is the implementer's discovery budget. Symmetrically, `implementation.md` says only _"by following the plan"_ — it never says _trust the plan's manifest; do not re-derive it_. Contrast the handling of decomposition, where the same file gets an explicit anti-re-exploration instruction (`implementation.md`, `{?predecessor_context}` block): _"Read it first: build on what they established, **do not re-explore or duplicate it**"_ — exactly the sentence the plan handoff is missing.

**Lever.**

- `planning.md` (target + packaged): require a closing section, e.g. _"**Files to change** — one line per file: repo-relative path, what changes in it, and the symbol/line you verified. This list is the implementer's discovery budget; anything you leave off, they will have to find themselves."_
- `implementation.md` (target + packaged): _"The plan's file list is verified ground truth for **which** files and **where**. Read those files; do not re-derive the plan's survey of the codebase."_
- Consider `reasoning: high` instead of `xhigh` on the `planning` node in `.worc/flows/implementation.yaml` and measure: `p9-11-12` spent 1122 s and 39 K output tokens on a plan whose implementation then ran clean in 523 s, and the brief notes planning wall-time routinely exceeds implementation (p9-11-05: 883 s vs 123 s). This is a measurable experiment, not a certainty — flag it as such.

**Expected impact.** Plausibly 10–20 turns off `implementation`'s front end (~$1–2) plus a shorter `planning`; lower confidence than F4/F5 because the handoff quality is model-dependent, so treat it as the third change, after F4 and F5, and A/B it.

---

### F9 — 50 `Edit` calls where a handful of `Write`s would do

- **category** prompt · **severity** low · **confidence** medium

**EVIDENCE.** `p9-11-09` implementation: `init-command.ts` edited **12** times, `commands.ts` **7**, `init.e2e.test.ts` **6** — with re-`Read`s interleaved (calls 47–51: `grep wasConfirmed` → `Read init-command.ts` → `Edit` → `Read init-command.ts` → `Edit`). `p9-11-13`: 38 `Edit` calls, `rules-grp.test.ts` ×5, `glossary.md` ×4. Each edit is a full round trip charged the whole context — at 150 K that is ~$0.11 per hunk.

**Root cause.** No guidance on edit granularity in either `implementation.md` copy.

**Lever.** One line in both copies: _"Batch your edits. Plan all changes to a file, then apply them in as few operations as possible — a whole-file rewrite is cheaper than eight hunks, and re-reading a file you just edited is never necessary."_ Low confidence on magnitude (some of these are genuinely sequential discoveries), but it is a one-line change with no downside; ~$0.5–1/run if it halves the count.

---

## 3. What's already good (checked, keep it)

- **Cost control that is working:** cache read is 97–99 % of input on every attempt (`usage_cache_read / usage_input_total`), which is the only reason a 199 K-context 173-turn node costs $10.57 and not 10×. The empirical blended rate is $0.75/M input.
- **Reliability:** 0 provider retries, 0 route fallbacks, 0 crashes, 0 HITL, 0 skipped nodes, 0 timeouts across 18 node runs. `checks` green in 23–24 s every time.
- **Task specs:** all three `validation_report.json` = `complete`, and it shows — the task files carry audit-finding provenance, explicit deliverables, checkbox acceptance criteria, _and_ explicit out-of-scope constraints. Every constraint was honored (the P11.02 containment-helper reuse in 12; `packages/core` placement in 09; the `grp.ts:21` comment in 13). This is the upstream reason there was no rework.
- **Scope discipline:** genuinely no drift and no gold-plating in 55 changed files. The two additions that _look_ like drift (09's `"unreadable"` schema outcome, 12's MCP tool-description line) are both causally required by the change and both explicitly surfaced.
- **The `documentation` node is doing more real work than its role advertises** — it closed 9 of 15 accepted review findings, including the reviewer's own recommended wording verbatim, at $0.90–1.90. That is the cheapest quality per dollar in the flow. F4/F2 are about _acknowledging_ that in the prompts and the follow-ups, not about changing it.
- **The supervisor punches above its price.** At `claude-sonnet-5`/`medium` and $0.76–0.92/run it correctly re-graded a `medium` to `high` with the right verdict (_"should block sign-off … they're not cosmetic"_), flagged the locked-requirements-doc edit for human sign-off, tracked the deferred `EXIT_CODE_USAGE_ERROR` rename across tasks, and self-corrected its own premature closure. Its per-step advisories are the highest-signal artifact in `.worc/`. Its problem is authority (F3) and grounding (F7), not capability — do not "upgrade" its model.
- **The evaluator plumbing fails closed correctly.** `evaluator.py:199-207` raises rather than accepting when `findings` is missing or malformed, with the reasoning recorded in the docstring. Findings are persisted immutably per `source_node_run_id`, and `findings.json` (with the `fix` field) is written per run so a fix→review loop never clobbers a prior pass.
- **The write-isolation architecture is right in principle** — `security.strict_isolation: true` produced a genuinely tight `--disallowedTools` list (46 rules) plus an OS sandbox, and the Bash write to `~/.claude` was correctly denied. F1 is one omitted rule and one coupled boolean, not a design flaw.

## 4. Data gaps

1. **`runs/exchange-seals/` are absent** for all three (`logging.clean_runs_on_success: true` evicts them at the terminal transition — expected, not a fault). Consequence: I could not read the _exact_ `current.diff` the reviewer saw, so F2's "closed by the documentation node" attributions rest on the doc node's own `Edit` payloads in `events.jsonl` (old_string → new_string, which is decisive for 09 and unambiguous for 12/13) rather than on a pre/post diff comparison. To make this auditable next time: `logging.clean_runs_on_success: false`.
2. **`usage_reasoning_output` is NULL on every attempt** (per the brief) — so I cannot separate thinking tokens from prose in the `xhigh` planning nodes, which is exactly the number needed to decide F8's `xhigh → high` experiment on evidence rather than inference.
3. **Cross-run native memory is unlogged** (F1). Because `~/.claude/projects/.../memory/*.md` was read by `p9-11-07`, `p9-11-08`, `p9-11-11`, `p9-11-12` and `p9-12-02`, some behavior in this batch is a memory effect that no `.worc/` artifact records. Anything comparative across the 20 runs — including "why was p9-11-12's implementation 3× cheaper than p9-11-09's" — carries this confound. Note also that `~/.claude/projects/.../memory/MEMORY.md` was mutated on **Jul 28 10:59**, _after_ the last run in the batch finished (02:32 UTC / p9-12-06), so the store's current state is not the state any single run saw.
4. **The supervisor's finalize prompt is not in the artifacts** (only `stages/supervisor/run-*/`), so F2's recommendation to have finalize re-grade accepted findings is aimed at `supervisor.py:1452-1500` by code reading, not by inspecting a rendered prompt.
5. **`review` node `events.jsonl` not profiled** — I read its `findings.json`, `rendered-prompt.md`, `output-schema.json`, and DB row, but not its turn-by-turn behavior. At $2.03–3.14 and 260–362 s it is the #3 cost centre and probably has an F4/F5-shaped inefficiency of its own; worth a follow-up pass.

## 5. Recommended order

| # | Change | File | Scope | Expected |
| --- | --- | --- | --- | --- |
| 1 | Decouple native-memory deny from `read_isolation_off`; add `autoMemoryEnabled:false` / `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` | `providers/claude.py:737`, `:709`, `build_sandbox_settings` | orchestrator | closes a workspace-write escape + restores run hermeticity |
| 2 | "Severity is routing" paragraph in the review role | `.worc/flows/implementation/review.md` + `packaged/…/review.md` | both | the real defect gets fixed or flagged must-fix |
| 3 | "Do not update the project's documentation" in the implementation role; "you are given the review findings — close the doc ones" in the documentation role | both `implementation.md` + both `documentation.md` | both | **−$2–3/run** on the priciest node |
| 4 | "Run the checks once, at the end" | both `implementation.md` | both | **−$0.7–1.1/run** |
| 5 | Reconcile follow-ups against the post-review diff; carry the finding's `fix` into `action_hint`; dedup on path overlap | `core/supervisor.py:784`, `:481`, `:390`; `core/flow/nodes/evaluator.py:546-578` + `core/flow/engine.py:92` | orchestrator | 60 % false positives → ~0; debt list becomes actionable |
| 6 | Flow position in the observation prompt | `core/supervisor.py` observe builder (+ the in-flight `supervisor_packet.py`) | orchestrator | no premature "task closes here" |
| 7 | Plan file-manifest ⇄ "don't re-derive it" | both `planning.md` + both `implementation.md` | both | −$1–2/run (A/B it) |
| 8 | Batch edits; report locked-tier doc edits | both `implementation.md`; both `documentation.md` | both | −$0.5–1/run; closes F6 cheaply |

Items 2–4 and 8 are role-prompt text only. **For every one of them I diffed the target's active Jul-25 copy against the packaged default: none of these gaps is already fixed upstream** — the packaged versions differ only by de-specialization (mdlint-specific rules → generic wording), and on F5 the packaged wording is marginally worse. So each needs the change in _both_ places: the target copy to affect this repo's next run, the packaged default to affect every repo.
