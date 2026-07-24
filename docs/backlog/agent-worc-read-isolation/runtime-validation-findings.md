# Runtime validation findings: agent read-isolation branch

Status: **open — collecting** Date: 2026-07-23 Owner: Vladimir Makarevich

A running log of nuances and defects found while exercising the `feat/agent-worc-read-isolation` build against a real target repo (`wastech-mdlint`, via `worc run` / `worc rerun`), beyond the deterministic unit/integration suite. Each entry records the observed behavior, the evidence, whether it is a regression, and the likely area to change. This is a findings tracker, not an ADR; a confirmed fix graduates to its own task or to [follow_ups.md](../follow_ups.md).

## VF-1 — `rerun --continue --from <node>` aborts on the unaccounted-changes guard when the working tree is dirty (regression)

Severity: **High** Status: **open** First seen: 2026-07-23 (task `p9-01-import-positions`, attempt 2)

### Observed

`worc rerun <id> --continue --from review` on a task in `manual_action_required` whose working tree still has uncommitted changes revives at the target node and then, within ~0.24 s, runs terminal cleanup and returns to `manual_action_required` (exit 2) **without ever invoking the node**. `last_error` is the same `working tree has unaccounted changes: <files>` that produced the original terminal state.

### Evidence

- `worc status`: `node=review`, `status=manual_action_required`, `last_error=working tree has unaccounted changes: docs/mdlint_v2/P9-remediation/01-import-positions.md, packages/core/src/markdown/parse-document.ts, packages/core/test/compile-doc-profile.test.ts, packages/core/test/parse-document.test.ts`.
- Run-log (stdout) sequence: `rerun --continue: applied controls` (`from_node=review`) → `rerun --continue: revived` → `terminal cleanup started/completed` → `to_status=manual_action_required` → `terminal exchange sealed (seal-000002)`. No `provider attempt started` for `review` ever appears.
- `git log` unchanged (HEAD still the pre-run commit) and `git status` still shows the same four files modified — so no reconciliation ran.

### Contradiction with `--dry-run`

Both `--dry-run` and the real run print `note: uncommitted changes (<files>) will be committed into the task`, but the commit never happens: the revive-time unaccounted-changes guard fires first and aborts. Net effect is a chicken-and-egg — the run would commit the pending changes, but the guard refuses to start the run _because_ they are pending — so `--from <node>` cannot run against a dirty tree and the advertised commit-into-task reconciliation is unreachable on this path.

### Regression

The orchestrator previously allowed continuing work from a specific node with a dirty working tree. The read-isolation work introduced a stricter working-tree integrity guard (the same "unaccounted changes" check that correctly fail-closes a normal run) and it now also blocks the `rerun --continue --from <node>` resume path. Losing dirty-tree resume-from-node is the unintended regression to fix.

### Expected

One of: (a) on `--continue`, perform the advertised commit-into-task reconciliation _before_ the working-tree guard so the resume can proceed; or (b) keep the guard but stop printing a commit note that will not execute, document that `--continue --from <node>` requires an accounted tree, and provide a supported reconciliation path (e.g. an explicit operator-commit / `--commit-pending` step). Either way, restore the prior ability to resume from a node with pending work.

### Likely area

Ordering of the `rerun --continue` reconciliation vs. the working-tree "unaccounted changes" guard (lifecycle §4 / WRI-007 restore-for-continue; the guard itself is most likely WRI-009's staged-set / working-tree integrity check). Confirm which check pre-empts the commit and reorder or gate it.

### Workaround (this session)

Operator committed the pending agent files onto the task branch to make the tree accounted, then reran `--continue --from review`.

### Origin note

The dirty tree here was produced deliberately: a concurrent operator commit to the task branch _during_ attempt 1 (to test the isolation gate). That gate behaved correctly — it fail-closed the original run to `manual_action_required`. VF-1 is specifically about the _resume_ path afterward, not that first fail-closed.

## VF-2 — `rerun --continue` refuses after a control-plane edit while parked; `--dry-run` still claims it will "resume using the current on-disk flow"

Severity: **Medium** Status: **open** (the guard is intended; the `--dry-run` note is wrong) First seen: 2026-07-23 (task `p9-01-import-positions`, attempt 3)

### Observed

After committing the pending work (the VF-1 workaround) so the tree is clean (`cleanup_safe=true`), `worc rerun <id> --continue --from review` _still_ aborts to `manual_action_required` in ~1 s without running `review`. `last_error=control plane: live control plane was edited while the task was parked; use a fresh rerun/restart to adopt it`.

### Cause (intended guard, WRI-010)

WRI-010 freezes the control plane at task start (`msg="control plane frozen"`, digest `9a3d5ecaf620`). The operator's commit `1c3af51` edited `.worc/flows/implementation.yaml` (a control-plane file) while the task was parked. On `--continue` (which binds the frozen bundle), WRI-010 detects the live-vs-frozen mutation and refuses, directing to a fresh rerun/restart. This is WRI-010 behaving as designed — not a regression like VF-1.

### Discrepancy with `--dry-run`

`--dry-run` prints `note: the flow changed since the checkpoint; --from 'review' will resume using the current on-disk flow`. The real `--continue` does the opposite: it refuses to resume _because_ the flow changed. The dry-run should predict the WRI-010 refusal (or state that a fresh rerun is required to adopt the edited flow), not promise a resume-with-current-flow that cannot happen. Second dry-run/actual mismatch on this path (see also VF-1's commit-into-task note).

### Combined effect (with VF-1)

After any concurrent edit during/after a run — working-tree changes (VF-1) or control-plane edits (VF-2) — `rerun --continue --from <node>` cannot proceed: fix VF-1's tree guard and you hit VF-2's control-plane guard. VF-1 is a regression to fix; VF-2 is an intended guard, but there is no cheap "adopt the edited control plane and resume from node" path — only a full fresh rerun. Note the guard is coarse-grained: the `review` node here is unaffected by the edit (review runs on **codex**; the edit changed a **claude** implementation-node reasoning level), yet `--continue` is blocked wholesale, which is expected for a digest-level freeze but worth noting for UX.

**Update — superseded by VF-3:** the "intended guard" framing above is wrong. The operator requires that an edited flow _be adopted_ on `rerun --continue --from <node>`; refusing it (instead of re-freezing the operator's edit and resuming) is a bug, not acceptable strictness.

## VF-3 — `rerun --continue --from <node>` must adopt an operator-edited flow (fix-then-resume); today it cannot (BUG)

Severity: **High** Status: **open (bug — mandatory operator workflow)** First seen: 2026-07-24 Related: VF-1, VF-2

### The required workflow (operator-stated, mandatory)

The whole point of resume-from-node is iterative tuning against a real task: the operator hits a defect mid-flow — a wrong node provider/model/reasoning, a churn-prone review/fix loop, any flow-level fix — **edits the flow to correct it**, then **re-runs the SAME task from a chosen step** (`review`, `fixing`/rework, …). Completed upstream work (planning, implementation, …) is preserved and not re-paid; the fix applies from the resume point onward. This must be supported — it is the reason resume-from-node exists.

### Current behavior (the bug)

`rerun --continue --from <node>` binds the control-plane bundle **frozen at the task's first start** (WRI-010). Consequently it (a) would run the OLD flow, ignoring the operator's edits, and (b) when the live flow differs from the frozen bundle it **refuses outright** (`live control plane was edited while the task was parked; use a fresh rerun/restart to adopt it` — VF-2). So the operator's only way to adopt an edited flow is a **fresh rerun from the top**, which discards every completed upstream node and re-pays for it. Resume-from-node with a corrected flow is therefore impossible today.

### Why it's a bug, not strictness

WRI-010's freeze exists to stop the **agent** from mutating the control plane _during_ a run — a real security property. But an **operator editing the flow between runs** is a legitimate, intended act, not tampering. The current guard conflates the two and blocks the operator along with the agent. The fix must separate them: adopt operator-intended flow edits on a deliberate `--continue`, while still detecting agent-side mutation within a live run.

### Suggested direction

On `rerun --continue --from <node>`: **re-freeze the control plane from the current on-disk (operator-edited) flow** and resume at the chosen node, keeping the completed upstream artifacts/exchange but running the remaining nodes under the new flow. Make it the default for `--continue` (the operator invoked the CLI, so the edit is trusted) or gate it behind an explicit `--adopt-flow` / `--refreeze` flag. The `--dry-run` "will resume using the current on-disk flow" note must then become TRUE (today it is misleading — VF-2). Agent-tamper detection stays scoped to mutation _during_ a run, independent of an operator's between-runs edit.

### Related friction (killed-task recovery)

Surfaced while reproducing this: after the operator stops a run mid-flight, the task is left in DB status `running`, and both `rerun` and `finalize` refuse it (they accept only `failed` / `manual_action_required`) and also refuse a dirty working tree. Recovery today is a 3-step manual dance — clean the working tree → `worc finalize <id> --as failed` → `worc rerun <id>`. A stopped/killed task should be directly recoverable (accept a stale `running` lease, or a one-step reset), not require `finalize --as failed` just to re-run it.

## VF-4 — `review_fix_*` counters in `state.db` do not reflect the actual number of review/fix rounds in the logs (observation — needs confirmation)

Severity: **Low** Status: **open (needs confirmation — may be expected after multi-attempt rerun + manual finalize)** First seen: 2026-07-24 (task `p9-01-import-positions`)

### Observed

`state.db` records `review_fix_cycles=1` and `review_fix_total=1` for `p9-01-import-positions`, but the on-disk logs show **8** review runs (`stages/review/history.jsonl`) and **7** fixing runs (`stages/fixing/history.jsonl`) — the loop churned far more than the counters suggest.

### Evidence

- `sqlite3 .worc/state.db` → `review_fix_cycles=1`, `review_fix_total=1`, `test_fix_total=0`, `status=done`, `current_node=testing`, `cleanup_last_error="Completed by operator by hand (commit 0e922dc …); orchestrator run was stopped mid-flow for flow retuning"`.
- Log run indices: review runs `4,6,9,12,15,18,21,24` (8); fixing runs `5,7,10,13,16,19,22,25` (8 dirs, `history.jsonl` counts 7). The findings above (VF-1/2/3) confirm this spanned attempts 1–3 (multiple reruns).

### Open question

The task went through 3 rerun attempts and a manual `finalize`/by-hand completion. It is unclear whether `review_fix_total` is intended to be cumulative across attempts (in which case 1 is wrong and it is under-counting) or is deliberately reset per attempt / on manual completion (in which case the value is expected but the operator loses the true historical churn count from the DB). If the counters gate the review/fix loop budget, an incorrect reset could also let a churn-prone task exceed its intended round cap across reruns. Confirm the intended semantics before treating this as a defect.

### Likely area

Counter update/reset logic around rerun and `finalize` in `core/orchestrator.py` (the `review_fix_cycles` / `review_fix_total` fields on the tasks row) and the DB write path on manual completion.

## VF-5 — disabling provider-native instruction discovery and re-injecting a frozen subset does not scale to N providers; roll the requirement back to native discovery + filesystem immutability (architecture)

Severity: **High (architectural / maintainability)** Status: **open — proposed rollback, needs decision (reverses a Milestone-1 decision)** First seen: 2026-07-24.

### The requirement under challenge

WRI-011's core mandate — _"disable provider-native live project instruction discovery"_ and _"inject the frozen repository instruction set through an orchestrator-owned supported high-priority instruction surface"_ — together with the discovery-disable halves of WRI-002 (Claude `--setting-sources ""`) and WRI-003 (Codex `project_doc_max_bytes=0`). This finding does **not** challenge the filesystem sandbox itself (see Scope below).

### Why it does not scale (the N-providers argument)

The design makes the orchestrator own a **shadow instruction-discovery engine** for every provider it supports. For each provider — Claude and Codex today, Gemini / Kimi / whatever next — the adapter must supply and keep current:

1. a **provably complete** switch that suppresses _every_ native instruction surface (root + nested per-directory docs + user-global home docs + dynamically-discovered files + native skills/plugins), not just the obvious one;
2. a **high-precedence injection surface** to put the frozen text back at a comparable weight to native memory; and
3. **replication of the native discovery semantics** we just suppressed — which files, in what order, `@`-import closure, `AGENTS.override.md` precedence — so behavior does not silently diverge from what repo authors wrote for that tool.

Every one of these is per-provider and version-drifting. WRI-011's own acceptance criteria make the cost explicit: _"Codex project `.codex` trust and live `AGENTS.md` discovery are independently disabled/**proven**"_ — "proven" is an expensive verification that must be redone for each provider and each CLI release. This also strains the repo's own invariant that provider-specific logic stay thin inside the adapters: "disable + replicate discovery" is not thin. Adding a provider should not require reverse-engineering and shadowing its context-assembly logic.

### What the requirement actually buys (so the rollback is informed)

Being honest about what is lost if we roll back:

1. **Redaction of secrets from instruction files before the agent sees them** — marginal: these are committed, agent-readable repo files; the agent can read them directly from the workspace regardless.
2. **The agent can _propose_ an edit to `AGENTS.md` while the run uses the frozen copy** — rare in practice; most tasks edit code, not their own guidance.
3. **Reproducible instructions across nodes / resume / fallback** — the one genuinely valuable property.

### The provider-agnostic replacement

Achieve property #3 (the valuable one) with a filesystem primitive every sandbox already has, instead of per-provider prompt-injection:

- **Write-deny the tracked instruction closure** for the duration of a run — root + nested `AGENTS.md` / `AGENTS.override.md` / `CLAUDE.md` plus their `@`-import targets (`.agents/rules/*`, `RTK.md`, …). Immutable files ⇒ native discovery returns identical instructions on every node, resume, and fallback **by construction, for every provider, with zero discovery code**. (Reproducibility can still be recorded by hashing the closure into a digest, exactly as today — the hash is provider-neutral; the _enforcement_ moves from "reimplement discovery" to "the files can't change".)
- **Keep the existing read-deny on provider home dirs** (`~/.claude`, `~/.codex`, and future equivalents) to stop external / user-global instruction leak — already in place, already provider-agnostic.
- **Let each provider run its own native discovery** over the now-immutable repo instructions: root, nested, `@`-closure, and precedence are handled by the tool that actually owns those semantics.

### Net effect on adding a new provider

The default path for a new provider (Gemini, Kimi, …) becomes **"sandbox write-deny + home read-deny"** — no per-provider discovery or injection code at all; the provider's own instruction handling just works against files it cannot mutate. Prompt-injection drops to a **fallback used only where a CLI couples its disable-switch**: Claude's `--setting-sources ""` (required to kill hooks/MCP/skills) also kills `CLAUDE.md` discovery, so Claude may still need injection. **Open verification:** whether Claude Code exposes a memory-only load path (load `CLAUDE.md` without re-enabling settings/hooks/skills); if it does, even Claude returns to the native path. Principle: **native-first, inject-as-exception** — the opposite of the current default.

### Trade-offs accepted by rolling back

- Lose pre-agent redaction of instruction files → mitigated (already agent-readable).
- Lose "propose-edit-to-own-guidance during a run" → mitigated: make instruction files write-denied _during a run_ an explicit, documented constraint; a task whose subject is editing repository guidance under strict isolation is already an unsupported/edge case (cf. the `tasks/` lifecycle write-deny).

### Scope of the rollback (do NOT throw the baby out)

- **Roll back:** the repository-instruction _discovery-disable + freeze-and-inject_ path (WRI-011 instruction half; WRI-002 `--setting-sources ""` insofar as it targets instructions; WRI-003 `project_doc_max_bytes=0`).
- **Keep:** the filesystem read/write sandbox — deny reads of `.worc` / `.env` / provider homes / bundles, and the exchange / `.git` / `tasks/` write-deny. This is the real, provider-agnostic security win and is unaffected.
- **Review separately (same smell, not auto-in-scope):** native **skill** discovery disable and the task-packet-as-injected-snapshot share the "disable native + reimplement" pattern; evaluate them under the same lens but do not fold them into this rollback without their own analysis.

### Likely area

`core/flow/instruction_bundle.py` (retire the inject payload; reduce to computing the write-deny closure + its digest), `providers/claude.py` and `providers/codex.py` (drop the discovery-disable + injection; keep the sandbox denies), `runtime_layout.py` (add the instruction closure to `write_guard.denied_write_paths`), and the WRI-011 / WRI-002 / WRI-003 acceptance criteria + `docs/` (architecture, security, configuration) + packaged guide that currently mandate discovery-disable.

### Next step

This reverses a Milestone-1 decision, so it graduates to an **ADR amendment** in [README.md](README.md) plus a task, with an entry in [follow_ups.md](../follow_ups.md). Confirm the Claude memory-only open question during that task.
