# Runtime validation findings: agent read-isolation branch

Status: **open — collecting** Date: 2026-07-23 Owner: Vladimir Makarevich

A running log of nuances and defects found while exercising the `feat/agent-worc-read-isolation` build against a real target repo (`wastech-mdlint`, via `worc run` / `worc rerun`), beyond the deterministic unit/integration suite. Each entry records the observed behavior, the evidence, whether it is a regression, and the likely area to change. This is a findings tracker, not an ADR; a confirmed fix graduates to its own task or to [follow_ups.md](../follow_ups.md).

## VF-1 — `rerun --continue --from <node>` aborts on the unaccounted-changes guard when the working tree is dirty (regression)

Severity: **High** Status: **shipped 2026-07-24** (terminal cleanup preserves the task's own WIP on a resumable manual park, the true node reason is no longer masked, and the note is reworded — see [follow_ups](../follow_ups.md)) First seen: 2026-07-23 (task `p9-01-import-positions`, attempt 2)

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

Severity: **Medium** Status: **shipped 2026-07-24** (resolved via VF-3; the dry-run note is now truthful and keys off bundle-level drift) First seen: 2026-07-23 (task `p9-01-import-positions`, attempt 3)

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

Severity: **High** Status: **shipped 2026-07-24** (operator `rerun --continue` adopts the edited control plane by re-freezing from live; automatic crash-recovery still refuses; stale-`running` recovery landed too) First seen: 2026-07-24 Related: VF-1, VF-2

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

Severity: **Low** Status: **shipped 2026-07-24** (confirmed a real defect: `finalize` did not mirror the operator-facing counter columns from the authoritative flow checkpoint, so a task killed mid-flow and finished by hand under-reported its churn; `finalize_task` now re-syncs them via `LoopCounters.from_run_state`) First seen: 2026-07-24 (task `p9-01-import-positions`)

### Observed

`state.db` records `review_fix_cycles=1` and `review_fix_total=1` for `p9-01-import-positions`, but the on-disk logs show **8** review runs (`stages/review/history.jsonl`) and **7** fixing runs (`stages/fixing/history.jsonl`) — the loop churned far more than the counters suggest.

### Evidence

- `sqlite3 .worc/state.db` → `review_fix_cycles=1`, `review_fix_total=1`, `test_fix_total=0`, `status=done`, `current_node=testing`, `cleanup_last_error="Completed by operator by hand (commit 0e922dc …); orchestrator run was stopped mid-flow for flow retuning"`.
- Log run indices: review runs `4,6,9,12,15,18,21,24` (8); fixing runs `5,7,10,13,16,19,22,25` (8 dirs, `history.jsonl` counts 7). The findings above (VF-1/2/3) confirm this spanned attempts 1–3 (multiple reruns).

### Open question (resolved)

The confirmed semantics: `*_fix_total` are **cumulative within an attempt** — preserved across `rerun --continue` (`revive_task_for_continue` keeps the counters + `flow_run_counters` checkpoint) — and **reset on a fresh rerun** (`reset_task_for_rerun` archives the prior attempt and zeroes them). They are **pure audit/observability (F49); they never gate any budget** — the review/fix loop is bounded by the consecutive per-loop counters and the global `fix_iterations` backstop, both of which live in the authoritative `flow_run_counters` JSON and are checkpointed after every transition. So the "could exceed the round cap across reruns" worry does **not** apply, and the observed `review_fix_total=1` is not the intended value.

The real cause: the operator-facing counter columns (`*_fix_total` / `*_fix_cycles`) are mirrored **only** at a clean orchestrator terminal transition (`_sync_counters_from_run_state`), whereas `fix_iterations` is mirrored on **every** checkpoint (`save_flow_checkpoint`). When a run is **killed mid-flow** and finished with `finalize`, no terminal transition runs, so the `*_total` columns stay stale at the last clean sync (here `1`) while the authoritative `flow_run_counters` held the true churn — an internally inconsistent row (`fix_iterations` current, `review_fix_total` lagging).

### Fix

`finalize_task` now hydrates the persisted flow checkpoint and re-mirrors the operator-facing columns from it (`LoopCounters.from_run_state`) before recording the terminal state — so a hand-finished task reports the real fix-loop totals. The run-state→mirror mapping is consolidated on `LoopCounters.from_run_state` (used by both the terminal sync and `finalize`).

### Likely area (addressed)

`core/loop_control.py` (`LoopCounters.from_run_state`) and `core/orchestrator.py` (`finalize_task` counter reconciliation; `_sync_counters_from_run_state` delegates to the shared mapping).

## VF-5 — disabling provider-native instruction discovery and re-injecting a frozen subset does not scale to N providers; roll the requirement back to native discovery + filesystem immutability (architecture)

Severity: **High (architectural / maintainability)** Status: **shipped 2026-07-24** (rolled back the discovery-disable + freeze-and-inject; native `AGENTS.md` discovery for Codex, agent-reads-root-files for Claude, write-deny immutability + kept digest — see Resolution) First seen: 2026-07-24.

### Resolution (shipped 2026-07-24)

The rollback landed as proposed. The Claude "memory-only load path" open question (below) was **verified against the Claude Code docs and answered: no** — `--setting-sources` gates `CLAUDE.md` discovery **and** hooks/MCP/skills/plugins on the same switch, with no flag to load memory without re-opening the settings surface. So:

- **Codex → native discovery.** Dropped `-c project_doc_max_bytes=0` and the `_stdin_text` `<repository-instructions>` block; the `.codex` trust / `--ignore-user-config` / `--disable` feature controls stay. The agent discovers `AGENTS.md` natively.
- **Claude → agent reads it itself.** `--setting-sources ""` stays (security: no hooks/MCP/skills), so native `CLAUDE.md` auto-load is off; the `--append-system-prompt-file` injection is removed. The agent reads the repo's root files with its Read tool, directed by the flow role prompts.
- **Reproducibility via immutability + digest.** The tracked root instruction files are added to `ProviderWriteGuardPolicy.denied_write_paths` — resolved by the core node runner via `discover_repository_instructions` (so `git_manager` stays free of a `core` import) — making them readable-but-immutable for the run. The per-source freeze + manifest `instruction_manifest_digest` are kept; only the injected concat payload (`instructions/repository.md`) and the `repository_instructions_path` plumbing are removed.
- **Kept:** the whole read/write filesystem sandbox, the task-packet freeze+publish, and the skill-package freeze — unchanged.
- **Deferred (own analysis):** the same-smell review of native **skill** discovery-disable and the **task-packet** snapshot (see Scope below); and the optional deeper simplification of computing the digest from the live files instead of keeping the freeze-copy.

Tracked in [follow_ups.md](../follow_ups.md); the ADR §1/§3 in [README.md](README.md) is amended accordingly.

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

## VF-6 — operator escape hatch `disable_read_isolation`: fully disable read-isolation, restoring native `CLAUDE.md` (+ hooks/MCP/skills) discovery (requirement)

Severity: **Medium (flexibility / functionality — operator-mandated)** Status: **shipped 2026-07-24** (operator-config `security.disable_read_isolation`; effective value computed once on `SecurityConfig.read_isolation_off` = `disable_read_isolation OR NOT strict_isolation`; read-side only — write side + `denied_read_paths` blacklist unchanged — see Resolution) First seen: 2026-07-24. Related: VF-5.

### Resolution (shipped 2026-07-24)

Shipped as specified: a global **operator-config** `security.disable_read_isolation` (bool; default later flipped to `true` — see the 2026-07-24 update at the end of this Resolution), sibling of `strict_isolation`. The effective state is computed in **one** place — the `SecurityConfig.read_isolation_off` property (`disable_read_isolation OR NOT strict_isolation`) — and both providers read the property, so the formula is never re-derived per adapter. `strict_isolation` is the master switch and wins toward relaxation (an explicit `disable_read_isolation: false` is overridden when `strict_isolation: false`). This closed the drift where [security.md](../../../.agents/rules/security.md) §MANDATORY + rule #3 already specified the flag but no code existed.

**Placement (open question resolved):** the **global** `security.*` key was chosen (not a per-provider `agents.providers.claude.*` override) for the clean `strict_isolation` coupling and because the flag governs every provider.

**Read-deny scope (decided with the operator):** only the **private** `InternalDenyPolicy` projection is lifted; the public `security.denied_read_paths` blacklist (target-repo `.env`/`secrets/**`) **stays enforced**, symmetric across both providers — matching the rule #3 wording ("lifts the private read-deny projection"). The redaction net is untouched regardless.

When read-isolation is off:

- **Claude** (`build_claude_argv`): `--setting-sources project` instead of `""`, and no `--strict-mcp-config` (native `CLAUDE.md` + project settings/hooks/MCP/skills load); the internal `Read()` denies and the F37 `~/.claude` native-memory deny are dropped (Write/Edit kept); `build_sandbox_settings` drops `denyRead` (keeps `denyWrite` + the env-file credential deny). The `--append-system-prompt-file` injection stays retired (VF-5); `--tools`/`--permission-mode`, the command denies, and the write-guard denies are unchanged. The `_REQUIRED_CLAUDE_FLAGS` preflight no longer requires `--strict-mcp-config` when off.
- **Codex** (`build_codex_argv` / `codex_profile`): the `deny_policy` carve-outs are downgraded `deny`→`read` in the permission profile (readable, still write-denied — the control plane stays immutable). **Decided:** `--ignore-user-config` and the project-untrusted trust ARE also lifted (project marked `trust_level="trusted"`, user config loaded) and the `hooks` feature is re-enabled — they are semantically part of "read-isolation off" and symmetric with Claude's project settings; the heavier autonomous feature-disables (`multi_agent`/`computer_use`/`browser_use`/`apps`/`plugins`) stay off (execution surfaces, not read discovery — reach them via the full-access escape). The pre-launch canary is kept and adjusted: the private-read probe flips to a positive control and a private-**write**-denied probe is added, so it still proves the write boundary.

Operator-config only: the node-override validator rejects `disable_read_isolation`/`strict_isolation` from a task/flow, and it is not an argv flag so `extra_args` cannot reach it. Loud `read-isolation: OFF` line in `worc preflight` and a run-start log warning — no silent weakening. `security/isolation.py` is functionally unchanged (it validates the write/permission/sandbox ceiling, which stays in force; a doc note records that the opt-out is sanctioned and never itself a preflight reason). Byte-identical argv + deny-sets when read-isolation is explicitly ON (regression-tested).

**Update 2026-07-24 (operator decision — shipped default flipped to `true`):** at the operator's explicit direction the shipped default was flipped so `disable_read_isolation` defaults to **`true`** — read-isolation is **OFF out of the box**. This deliberately departs from the original "off by default" of the requirement below and from the § MANDATORY default-safe guidance; it is a deployment-posture choice the operator owns. Reconciled across [security.md](../../../.agents/rules/security.md) (rule #3 + § MANDATORY now own the departure), the packaged `config.example.yaml` (ships `disable_read_isolation: true`), `docs/configuration.md` + `docs/operations.md` + the packaged config reference, and the regression tests (the "byte-identical at defaults" checks now key off an explicit `disable_read_isolation: false`; the provider-test fixtures pin read-isolation ON so the isolation machinery stays covered). Set `disable_read_isolation: false` to keep read-isolation on. The write side, the public `denied_read_paths` blacklist, and the operator-config-only boundary are unchanged.

### The requirement (operator-stated, mandatory)

Provide an operator-facing flag that **fully disables the Claude read-isolation envelope** so Claude runs "natively" — in particular it **auto-detects `CLAUDE.md`** again (native project memory), which as a coupled consequence re-enables project settings / hooks / MCP / skills / plugins. This is a mandatory flexibility requirement: when the operator turns it on, we deliberately accept the reduced isolation. The driver is that VF-5's `--setting-sources ""` (kept for security) also kills native `CLAUDE.md` auto-load, and some workflows want that native behavior back.

### Why this is in-policy (not a rule violation)

- [security.md](../../.agents/rules/security.md) **§ "MANDATORY"**: "Security mechanisms must not unnecessarily limit the orchestrator's functionality or degrade the user experience … priority should be given to preserving existing capabilities, usability, and predictable behavior … the least restrictive solution that provides the necessary level of protection should be preferred." An always-on lockdown with no operator opt-out is exactly the over-restriction this rule warns against.
- security.md **§10**: full-access / permission-bypass modes are "**not hard-forbidden** but gated by `strict_isolation`." An operator-gated escape hatch already has precedent — `strict_isolation: false`, Codex `sandbox: danger-full-access`, and the per-provider `agents.providers.claude.allow_native_memory` opt-in.
- The AGENTS.md hard invariant ("the security envelope cannot be weakened **through a task, `extra_args`, or a flow node**") is about **untrusted surfaces** — it stays intact as long as this flag is **operator-config only**, off by default, and never reachable from a task / `extra_args` / flow. (Its "absolutely forbidden flags" wording deserves a one-line clarification so it reads alongside §10 — see Next step.)

### What to build

- A new **operator-config** switch **`disable_read_isolation`**, off by default, a sibling of `security.strict_isolation` (it governs read-isolation for the provider run; its most visible effect is Claude). Config-only; the validator must still reject the same switch arriving via task / flow / `extra_args`.
- **Precedence — `strict_isolation` always wins toward relaxation.** Effective value: `disable_read_isolation OR NOT strict_isolation`:
  - `strict_isolation: true` + `disable_read_isolation: true` → read-isolation **off** (the operator's explicit surgical opt-out is honored under strict — this must work).
  - `strict_isolation: true` + `disable_read_isolation` unset/false → read-isolation **on** (today's default).
  - `strict_isolation: false` → read-isolation **off regardless** of `disable_read_isolation` (even an explicit `false` is overridden — `strict_isolation: false` already means "relax everything").
- When read-isolation is off, `build_claude_argv` drops `--setting-sources ""` (use `--setting-sources project` / the CLI default) so Claude natively loads `CLAUDE.md` + project settings/hooks/MCP/skills/plugins, and the private read-deny projection is lifted for the run. The `--append-system-prompt-file` injection stays retired (VF-5) — native discovery replaces it. (Codex is already native; there the flag mainly lifts the read-denies.)
- Loud, explicit signalling: a preflight/log warning that read-isolation is off, plus a docs banner. No silent weakening.

### Scope — what "read-isolation" covers

The flag is named `disable_read_isolation`, so it targets the **read side** of the envelope: (a) the provider-native-discovery-disable (Claude `--setting-sources ""` and its consequences), and (b) the private **read-deny** projection (`InternalDenyPolicy`: `.worc`/`.env`/provider homes/bundles). The **write side stays in force** — the write-guard (exchange/`.git`/`tasks/` + the VF-5 instruction write-deny), the WRI-009 commit/staging gates, and the PR control layer are write/publish controls, not read-isolation, and are out of this flag's scope. Note that a re-enabled hook can already run arbitrary commands, so lifting read-isolation is already a large reduction; that is the accepted trade-off. **Placement open:** global `security.disable_read_isolation` as specified here (chosen for the clean `strict_isolation` coupling) vs a per-provider `agents.providers.claude.*` override — confirm during the task.

### Trade-offs accepted (documented, only when enabled)

- A committed project `.claude/settings.json` in a target repo can define hooks that execute during the run; MCP servers / skills / plugins load. That is exactly the surface VF-5's `--setting-sources ""` exists to close — accepted only under this explicit operator opt-in.
- Instruction reproducibility is no longer guaranteed by the run (native discovery over live files); the VF-5 write-deny of the tracked root files still applies unless the maximum tier also drops it.

### Likely area

`config/schema.py` + the config validator (new operator-only key; reject the task/flow/`extra_args` path), `providers/claude.py` (`build_claude_argv` — conditional `--setting-sources`; `_REQUIRED_CLAUDE_FLAGS` / preflight gated on the flag), `security/isolation.py` (`isolation_reasons` permits it under the opt-in, like `strict_isolation`), `composition.py` (thread the flag), docs (`configuration`, `operations`, the security.md §10 note) + the packaged config guide, and tests (argv with/without the flag; validator rejects the untrusted-surface path).

### Next step

Graduate to a task plus a one-line **ADR / AGENTS.md invariant clarification** (operator-config escape hatches are sanctioned; the "absolutely forbidden" wording targets the task / `extra_args` / flow surfaces, per security.md §10), with a [follow_ups.md](../follow_ups.md) entry.

## VF-8 — run cost is unmeasurable: `usage_cost` is never populated and the supervisor's provider calls are absent from `provider_attempts` (observability)

Severity: **Medium (observability)** Status: **shipped 2026-07-25** (both gaps closed — `usage_cost` filled from Claude `total_cost_usd`; `provider_attempts` gains `task_id` + nullable `node_run_id` so the supervisor's own calls are on the ledger and a roll-up is `WHERE task_id=?`; see Resolution) First seen: 2026-07-24 (task `p9-05-custom-heading-target`, second-iteration validation on the dev43 branch build)

### Resolution (shipped 2026-07-25)

Both root causes fixed; see the [follow_ups](../follow_ups.md) entry for the full change list.

- **`usage_cost` filled.** Claude's stream-json terminal `result` event carries `total_cost_usd` as a **sibling** of `usage` (not a member), so `parse_stream_json` captures it and threads it into `_normalize_claude_usage` → `NormalizedUsage.cost` (new shared `coerce_usage_cost`). It rides the per-invocation scope, so the delta math preserves it unchanged. **Codex reports no dollar figure**, so its `usage_cost` stays NULL by design — never a guessed value.
- **Supervisor calls on-ledger.** `provider_attempts` gains a `task_id` column and its `node_run_id` becomes **nullable** (state.db v18→v19, additive). A whole-task roll-up is now `SELECT … WHERE task_id = ?` (no `node_runs` join) via the new `get_provider_attempts_for_task`, and the constant supervisor layer records its own billable calls with `node_run_id` NULL. Fabricating a `node_runs` row for the supervisor was **rejected** — `recorder.hydrate_run_state` builds the resume trace from **all** `get_node_runs`, so a synthetic `supervisor` row would corrupt resume. `record_provider_attempts` was decoupled from `NodeServices` (now takes `store` + `clock` + `task_id` + `node_run_id`) so the node runners and the supervisor share one recorder; the supervisor's per-turn usage is a **summation-safe delta** against its own resumed-session baseline (`usage_snapshot` persisted on the `__supervisor__` lineage), so token counts are correct on a cumulative provider (Codex) too, not just cost on Claude.
- **Still deferred:** the read/report surface — there is no `worc` cost/token report or PR-summary line yet; the roll-up is SQL / `get_provider_attempts_for_task` only (a `worc cost <task>` reading `SUM(usage_cost) WHERE task_id=?`, or an `analyze-task-run` cost section, is the natural next step). This is the v16 row's deferred item (b), still open.

### Observed

On a clean, fully successful run the `state.db` audit tables cannot answer "what did this run cost". Two independent gaps compound: (a) `provider_attempts.usage_cost` is `NULL` for every attempt, and (b) the supervisor's provider calls are not recorded in `provider_attempts` at all — only the flow's `agent`/`evaluator` nodes are. The run therefore has no per-attempt dollar figure and undercounts its own billable provider calls.

### Evidence

- The run-log shows **8** `provider attempt started` lines (implementation + review + documentation + **5 supervisor turns**: four observe turns between nodes plus the finalize summary), but `provider_attempts` holds only **3** rows for the task — `SELECT ... FROM provider_attempts pa JOIN node_runs nr ON pa.node_run_id=nr.id WHERE nr.task_id='p9-05-custom-heading-target'` returns implementation / review / documentation only.
- `SELECT usage_cost FROM provider_attempts WHERE attempt_dir LIKE '%p9-05-custom-heading-target%'` → `NULL` for all three; `SUM(usage_cost)` → `NULL`. Token counters (`usage_input_total`, `usage_output_total`, `usage_cache_read`) _are_ populated, so the usage plumbing runs — only the cost field is unfilled.
- The supervisor artifacts _do_ exist on disk (`.worc/logs/<task>/stages/supervisor/…`), so this is a persistence-to-`provider_attempts` gap, not a missing-run gap.

### Root cause (two parts)

1. **`usage_cost` unfilled.** The adapter normalizes token usage but does not derive a cost for the attempt (the provider CLI payloads carry a cost figure — e.g. Claude stream-json `total_cost_usd` — that is not mapped into `usage_cost`).
2. **Supervisor calls off-ledger.** The supervisor is a constant orchestrator layer above the flow, not a graph node, so it is intentionally excluded from `node_runs`. But `provider_attempts` records provider _calls_, and >60% of this run's real, billable calls (5 of 8) are supervisor calls that never get a row — so any cost/usage roll-up built on `provider_attempts` silently omits the entire supervisor spend.

### Net effect

Cost and full call-count are not derivable from the audit — the `analyze-task-run` cost dimension degrades to "unknown", and any future budget/telemetry keyed off `provider_attempts` under-reports. Not a security or correctness defect and not read-isolation-specific (almost certainly pre-existing, surfaced during read-isolation validation); purely an observability limitation.

### Likely area

`providers/_adapter_base.py` (usage extraction — map the provider-reported cost into `usage_cost`; verify both Claude and Codex payload shapes), and the supervisor persistence path (`core/supervisor.py` + the state store that writes `provider_attempts`) — decide whether supervisor provider calls earn their own `provider_attempts` rows (a `node_run_id`/sentinel for the constant layer) or a parallel record, so the roll-up is complete. `prompt_audit` was ON for this run (the per-prompt timeline is available), so that is not the gap.

### Next step

Confirm the Claude/Codex CLI cost fields, then a small task to (1) populate `usage_cost` and (2) give supervisor calls an audit home; add a [follow_ups.md](../follow_ups.md) entry.
