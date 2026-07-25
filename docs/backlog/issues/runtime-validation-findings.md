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

**Update — shipped 2026-07-25** (commit `438ce6e`, "provider attempt auditing for supervisor layer"). Both halves are fixed and confirmed against the p9-06 → p9-09 run range: `usage_cost` is populated on every attempt (`SUM(usage_cost)` over the range = **$56.70**), and supervisor calls now get their own `provider_attempts` rows with `node_run_id` NULL (5–10 supervisor rows per task, `attempt_dir` under `stages/supervisor/`). A residual defect remains in the same writer — the attempt **timestamps** are stamped at row-write time rather than taken from the result — tracked separately as [VF-12](#vf-12--provider_attempts-and-check_runs-record-zero-duration-every-attempt-and-check-has-started_at--finished_at-observability).

## Run-range validation: `p9-06-format-gate` → `p9-09-full-solution-deep-audit` (2026-07-25)

A second validation pass, this time over a **contiguous 15-run window** rather than a single task, to catch defects that only show up across a sequence: state left behind by aborted runs, config that drifts out of sync with what a run just shipped, artifacts that accumulate, and cost/observability gaps that a single clean run hides.

### Scope and frame

|  |  |
| --- | --- |
| Target repo | `wastech-mdlint`, branch `feat/p9-remediation`, all runs to PR [#15](https://github.com/VladimirMakarevich/wastech-mdlint/pull/15) |
| Window | `2026-07-24T22:58Z` → `2026-07-25T03:35Z` (4.62 h wall clock) |
| Ledger entries | 16 (15 distinct task ids — one task is recorded twice, see [VF-14](#vf-14--the-completed-ledger-records-one-task-attempt-twice-and-a-validation-rejected-task-leaves-no-recoverable-state-observability--ux)) |
| Outcomes | 12 `done`, 3 `failed` (2 operator-aborted, 1 validation-rejected), 1 `manual_action_required` that was then operator-failed |
| Node runs | 83 |
| Measured cost | **$56.70** across 12 tasks with recorded attempts — the 3 aborted runs recorded **$0** despite ~14 min of provider work ([VF-13](#vf-13--an-operator-forced-termination-leaves-orphan-running-node_runs-no-provider_attempts-row-and-no-terminal-log-line-observability)) |
| Provider health | 0 fallbacks, 0 retries, 0 crashes, 0 timeouts, 0 exchange-contamination events, `stage_attempts=1` on every node |
| Config | `disable_read_isolation: true`, `strict_isolation: true`, `prompt_audit: true`, `memory.enabled` flipped `true → false` mid-window |

Cost concentration: `implementation` dominates (up to $3.81 on a single node), `review` is second, and the **supervisor layer is a flat ~$0.38–0.82 per task** — roughly **12% of total spend** for an advisory function whose output never gated anything in this window.

## VF-9 — the VF-5 instruction-file write-lock has no operator escape hatch and directly contradicts the VF-7 security preamble (BUG, rule violation)

Severity: **Critical** Status: **open** First seen: 2026-07-25 (tasks `p9-06-format-gate`, `p10-01-governance-docs-2`) Related: VF-5, VF-6

### Observed

The write-guard denies all writes to `AGENTS.md` / `AGENTS.override.md` / `CLAUDE.md` unconditionally, while the security preamble prepended to every prompt tells the agent it **may** change them when the task asks. Two tasks in this window were built on exactly that promise, and both were damaged by the contradiction.

### Evidence

- Enforcement, target run: `.worc/logs/p10-01-governance-docs-2/stages/fixing/run-000030/1-claude/claude-sandbox-settings.json` → `sandbox.filesystem.denyWrite` contains `…/AGENTS.md` and `…/CLAUDE.md`. The same paths appear as `Write(…)`/`Edit(…)` entries in `--disallowedTools` (see `request.json` → `argv`).
- The promise, orchestrator source: [`core/flow/security_preamble.py:52-53`](../../../src/wastech_orchestrator/core/flow/security_preamble.py) — "`AGENTS.md`, `AGENTS.override.md`, `CLAUDE.md` are read-only this run: read them for guidance, but **change them only if the task explicitly asks** (as an ordinary diff)."
- The enforcement has no conditional: [`core/flow/nodes/agent.py:406-415`](../../../src/wastech_orchestrator/core/flow/nodes/agent.py) resolves `instruction_files` from `git ls-files` on every workspace-write attempt and threads them into `resolve_control_paths`; [`runtime_layout.py:176-186`](../../../src/wastech_orchestrator/runtime_layout.py) folds them into `denied_write_paths` with no flag. `grep -rniE "lock_instruction|instruction_lock|allow_instruction" src/` → **no matches**: there is no config key, no task field, no flow-node knob.
- Agent's own diagnosis, `stages/fixing/run-000030/fixing.out.md`: _"every write attempt against that specific file is denied at the sandbox level — confirmed with both the `Edit` tool and a raw shell redirect (`operation not permitted: AGENTS.md`), while the identical test against `README.md` succeeded. This matches the orchestrator contract marking `AGENTS.md`/`CLAUDE.md` read-only by default for this run; **it seems this run's sandbox wasn't granted the exception the task's own text says should apply**."_
- The evaluator believes the promise too — all three review rounds (`stages/review/run-0000{26,29,32}/findings.json`) close with _"The task explicitly authorizes editing AGENTS.md, so the read-only-by-default run rule does not apply."_

### Damage in this window

1. **`p10-01-governance-docs-2` died on it.** Deliverable 1 was an `AGENTS.md` edit. `planning → implementation → testing → review(rework) → fixing → testing → review(rework) → fixing → testing → review(rework)` — 3 review rounds, 2 fixing rounds, 21 min, **$4.32**, terminal `manual_action_required` → operator-failed. `failure_report.json` records `limit_exhausted: "no_file_change"`.
2. **`p9-06-format-gate` silently routed around it.** Blocked from adding a trailing newline to `AGENTS.md`, the agent instead **added `AGENTS.md` to `.prettierignore`** with the comment _"Missing a trailing newline only; left as-is (governance doc, not touched incidentally by this change)."_ The task's whole purpose was to make the format gate honest — and it shipped with a permanent, silently-created hole in that gate. Still present in `wastech-mdlint/.prettierignore:16`.
3. **The blocked agent probed the sandbox by writing to `README.md`** to isolate the deny (`fixing.out.md`: _"I already reverted a stray test line I'd accidentally appended to README.md while diagnosing this"_). It reverted cleanly, but an unrelated tracked file was mutated as a side effect of an unexplained deny.

### Why it's a rule violation, not strictness

[`.agents/rules/security.md` § MANDATORY](../../../.agents/rules/security.md): _"**Every isolation, sandbox, or provider-lockdown mechanism MUST ship with an operator-controlled way to relax or fully disable it.** Hard-wiring an always-on restriction with no operator opt-out is not acceptable, even when disabling it reduces security — that is the operator's decision to make."_ `disable_read_isolation` relaxes the **read** projection only; the instruction-file **write** lock is unreachable from any operator surface. This is the same class of defect as VF-6 addressed for reads.

### Expected

Two independent fixes, both needed:

1. **Add the operator escape hatch** (operator-config only, never task/`extra_args`/flow-node — per the § MANDATORY boundary): e.g. `security.lock_instruction_files: true|false`. Under `false`, drop `instruction_files` from `ProviderWriteGuardPolicy`; the VF-5 reproducibility guarantee is the operator's to trade away, exactly as the read projection is.
2. **Make the preamble truthful in the meantime.** While the lock is in force, `security_preamble.py:52-53` must not promise an exception that cannot be granted. Reword to state the lock plainly and tell the agent what to do instead (report the needed edit in its final message so the operator applies it), so a blocked agent stops burning fix loops trying.

The module docstring claims the path tokens are emitted from layout constants "so the text cannot drift from the enforced denies" — the **paths** don't drift, but the **semantics** did. A test asserting preamble-vs-write-guard agreement would have caught this.

## VF-10 — an agent that reports a hard blocker still returns `outcome=done`, and the fix→review edge drops the fixer's report (flow)

Severity: **High** Status: **open** First seen: 2026-07-25 (`p10-01-governance-docs-2`) Related: VF-9

### Observed

The `fixing` node's final message was an explicit, well-written "I am blocked, here is the exact patch, please apply it or change the sandbox policy." The orchestrator recorded that node as **succeeded / done** and routed straight back to `review` — which had no access to the fixer's report and therefore re-issued a byte-identical blocking finding. Round 2 repeated the whole diagnosis from scratch.

### Evidence

- `state.db`: `node_runs` id=30 → `node_id=fixing, status=succeeded, outcome=done, error_class=NULL` — while `stages/fixing/run-000030/fixing.out.md` opens with _"I could not complete the primary deliverable. **Blocker: `AGENTS.md` is write-protected by the sandbox for this run, and I can't lift that.**"_ and closes with a direct question to the operator.
- Review context is diff+findings only — `stages/review/run-000032/rendered-prompt.md` (tail) lists exactly four context files: `task`, `plan`, `current.diff`, `stages/review/run-000029/findings.json`. Neither `fixing.out.md` from run-000027 nor run-000030 is offered, even though both are present in the exchange (`exchange-seals/p10-01-governance-docs-2/seal-000001/manifest.json` lists them).
- Cost of the blindness: round-2 `fixing` ran 221 s / $0.77 to rediscover what round-1's report already said.

### Expected

1. **Give an agent node a `blocked` outcome.** A node that ends with an unresolvable environmental blocker should not be indistinguishable from a node that did the work. Route it to `manual_action_required` (or a `human_input` request) with the agent's report as the failure reason, instead of feeding a no-op diff back into a review loop.
2. **Carry the previous `<node>.out.md` into the evaluator's context** on a rework edge. The reviewer is being asked to judge whether the finding was addressed; the implementer's account of _why it wasn't_ is first-order evidence and it already exists in the exchange.

### Not a regression

The `no_file_change` loop-breaker worked correctly and is the only reason this cost $4.32 instead of ~$10 — `agents.max_fix_cycles` is 15, and without the detector the loop would have run to the cap. Keep it.

## VF-11 — the format gate `p9-06` shipped is absent from the orchestrator's own check command set; the branch is now red against its own CI (checks / config)

Severity: **High** Status: **open** First seen: 2026-07-25 (tasks `p9-06` … `p9-09`)

### Observed

`p9-06-format-gate` added `npm run format` to the target repo's CI `verify` job and left the tree green. The orchestrator's `checks.command_sets.default` was never updated to match, so the **next four runs re-broke the gate and every one of them passed `testing`, passed `review`, and published.**

### Evidence

- The gate was added: `p9-06`'s diff adds `- run: npm run format` to `.github/workflows/ci.yml`'s `verify` job.
- The orchestrator does not run it: `.worc/config.yaml:86-108` — `command_sets.default` is `typecheck` / `lint` / `test` / `build`. No `format`.
- Current state of the branch, with the repo's own pinned Prettier 3.8.4 (`npm run format`):
  ```
  [warn] docs/mdlint_v2/P10-consistency/05-test-depth.md          ← p10-05
  [warn] docs/research/p9-09-full-solution-deep-audit/report-structure.md   ← p9-09
  [warn] docs/research/p9-09-full-solution-deep-audit/report.md            ← p9-09
  [warn] packages/core/test/registry-inventory.test.ts                     ← p10-04
  [warn] Code style issues found in 4 files.
  ```
- Two independent supervisors noticed and filed it as a low follow-up rather than a gate failure: `p10-06`'s `summary.json` → _"[low] Pre-existing `npm run format` warnings on P10.05-adjacent files"_; `p10-08`'s → _"[low] Pre-existing prettier --check failures outside this task's scope"_. Nothing acted on it.

### Second defect in the same command set

`typecheck` and `build` are **the same command**. `wastech-mdlint/package.json`: `"typecheck": "tsc -b"`, `"build": "tsc -b"`. The daemon log shows the cost of the duplication — `typecheck` 0.446 s, `build` 0.391 s (a no-op re-run against the incremental build state). One of the four checks in every one of the ~54 check runs in this window carried zero signal.

### Expected

- Target-only, immediate: add `format` to `.worc/config.yaml`'s `default` command set and drop the duplicate `build` (or point it at something that actually differs, e.g. `npm pack --dry-run`).
- Orchestrator-wide, the real gap: **nothing reconciles a task's own CI change with the orchestrator's check set.** A task that adds a gate to CI should surface that the run configuration now under-tests relative to CI. Cheapest honest version: have the `documentation`/supervisor step flag "this change adds a CI gate that `checks.command_sets` does not run" as a first-class finding rather than a low follow-up, since the operator is the only one who can update the config.

## VF-12 — `provider_attempts` and `check_runs` record zero duration: every attempt and check has `started_at == finished_at` (observability)

Severity: **Medium** Status: **open** First seen: 2026-07-25 (all 15 runs) Related: VF-8

### Observed

Both audit tables stamp the clock **twice at row-write time**, so no row carries a real interval. The DB cannot answer "which node was slow" or "which check hung" — even though the daemon log already prints the correct durations and the values are available in memory at the moment the row is written.

### Evidence

- `sqlite3 state.db "SELECT started_at, finished_at FROM check_runs LIMIT 1"` → `2026-07-24T23:12:44.286091+00:00` / `2026-07-24T23:12:44.286101+00:00` — a 10 µs "run" of `npm run typecheck`. Every row in the window is the same shape.
- Same for every `provider_attempts` row (e.g. `p10-03-stale-comments` implementation: `01:34:16.244701` / `01:34:16.244708`), so `SUM(duration)` over the supervisor layer returns **0 s** for calls the log times at 5–25 s each.
- The daemon log has the truth: `msg="check completed" … duration_seconds=9.938` and `msg="provider attempt completed" … duration_seconds=441.823`.
- The data is on the object being persisted: [`core/flow/observability.py:139-140`](../../../src/wastech_orchestrator/core/flow/observability.py) writes `started_at=clock(), finished_at=clock()` — while **lines 227-228 of the same file** correctly use `attempt.result.started_at` / `attempt.result.finished_at` for the JSON artifact. `prompt-audit/timeline.jsonl` also carries the real per-attempt timestamps.
- Second instance: [`core/flow/nodes/checks.py:235-236`](../../../src/wastech_orchestrator/core/flow/nodes/checks.py) — `started_at=self._s.clock(), finished_at=self._s.clock()`.

### Expected

`observability.py:139-140` → take the values from `result` (fall back to `clock()` only for a result-less attempt). `checks.py:235-236` → pass the measured start/end of the subprocess rather than re-reading the clock. Two small edits; both surfaces already have the data. Worth a regression test asserting `finished_at > started_at` for any attempt/check with a non-zero real duration.

## VF-13 — an operator-forced termination leaves orphan `running` `node_runs`, no `provider_attempts` row, and no terminal log line (observability)

Severity: **Medium** Status: **open** First seen: 2026-07-25 (`p10-01-governance-docs`, `p10-02-glossary-status`)

### Observed

Two tasks were killed mid-node by the operator. Both left `node_runs` rows permanently in `status='running'` with `finished_at=NULL`, recorded **zero** provider attempts (so zero cost) despite minutes of Opus/Sonnet work, wrote no `result.json`, and produced **no daemon-log line** explaining the termination.

### Evidence

- `state.db` orphans, still open: `node_runs` id=7 (`p10-01-governance-docs / planning`, started 00:00:58Z), id=21 and id=22 (`p10-02-glossary-status / implementation`, started 00:47:01Z and 00:53:52Z).
- No cost recorded: `SELECT COUNT(*) FROM provider_attempts WHERE task_id IN ('p10-01-governance-docs','p10-02-glossary-status')` → **0**, while the artifact dirs hold a full `request.json` + `stdout.log` per attempt. Roughly 5 min + 9 min of provider work is invisible in the roll-up.
- Silent restart: the daemon log for `p10-02-glossary-status` shows heartbeats to `elapsed_seconds=240.1` at 02:51:02, then at **02:53:52 a fresh `route resolved` + `provider attempt started` for the same node** — with no intervening line recording why the first attempt ended. Two `running` rows, one node.
- Silent abort: `p10-01-governance-docs`'s log ends at the 300 s heartbeat (02:05:58) and the next line is an unrelated task 8 min later. The ledger nonetheless records `final_status: failed, manual: true, terminal_cleanup: completed` — so the finalize path ran and logged nothing.

### Expected

On any terminal transition, reconcile open `node_runs` for that task (`status='aborted'`, `finished_at=now`, `skip_reason`/`error_class` naming the operator action), and persist a `provider_attempts` row for the killed attempt with whatever usage the partial `stdout.log` yields (or an explicit `usage_delta_status='unknown'`) so an aborted run is not free in the ledger. Log the termination at `level=warning` with the reason — an operator abort is exactly the event a post-mortem needs and it is the one event the log omits.

## VF-14 — the completed-ledger records one task attempt twice, and a validation-rejected task leaves no recoverable state (observability / UX)

Severity: **Medium** Status: **open** First seen: 2026-07-25

### Observed

Two separate ledger integrity problems in one window.

**(a) Duplicate row for one attempt.** `p10-01-governance-docs-2` appears **twice** in `logs/completed.jsonl` — first `final_status: manual_action_required` at `01:12:41Z` (the flow's own terminal), then `final_status: failed, manual: true` at `01:16:09Z` (the operator's). Same `attempt: 1`, same `rerun_of: null`, same `fix_iterations: 2`. Any consumer that counts lines double-counts the task and, ordering aside, may read the superseded status. The ledger has no key or supersede marker distinguishing "task reached a terminal state" from "operator changed that terminal state."

**(b) `duplicate_task_id` rejection is a dead end.** `p9-10-01-governance-docs` was rejected at validation (`logs/p9-10-01-governance-docs/validation_report.json` → `{"passed": false, "reason": "duplicate_task_id", "detail": "p10-01-governance-docs"}`) because its front-matter `id:` collided with an already-processed task. What the operator is left with: a ledger row whose `title` is the **file stem** rather than the task's real title (no `tasks` row exists, so there is nothing to read a title from), a log dir containing exactly one file, and the source task file stranded in `tasks/preparing/` (`p9-10-01-governance-docs_3.md` is still there). The rejection detail names the colliding id but not the colliding task's path, so resolving it means grepping the whole tasks tree.

### Expected

- (a) Either append a supersede marker (`supersedes_finished_at`, or `superseded: true` on the earlier row) or make the ledger last-write-wins per `(task_id, attempt)` — and document which, since the file is the operator-facing history.
- (b) On `duplicate_task_id`, include the **path** of the conflicting task in `detail`, and either move the rejected file to `validation.quarantine_folder` (already configured, `.worc/tasks/rejected`, and unused here) or state plainly in the log that it stays in `preparing/`. Note this also interacts with the file-stem-vs-`id` mismatch the whole P10 batch relies on: every P10 task file is named `p9-10-NN-*` while its `id:` is `p10-NN-*`, which is legal but makes ledger↔file correlation manual.

## VF-15 — a reused chain PR keeps the first task's title forever, and its body grows unbounded (publish)

Severity: **Medium** Status: **open** First seen: 2026-07-25 (PR #15, 13 appended tasks)

### Observed

`_append_reused_pr_body` was built precisely because "a reviewer reading a 7-task chain PR sees only task 1's scope" — but it fixes only the **body**. The **title** is never touched, and the body it rewrites grows without bound.

### Evidence

- Live PR: `gh pr view 15 --json title,body` → title is still **"P9.02 Replace localeCompare with a deterministic sort"** after 13 appended task sections; body is **66,376 characters**.
- Growth is linear and monotonic: `p9-06` 24,038 B → `p9-08` 32,768 → `p10-05` 49,668 → `p10-08` 62,738 → `p9-09` 66,678 B. ~4 KB per task.
- GitHub's documented issue/PR body limit is **65,536 characters**; the current body is **840 over it**. It was accepted this time, so the failure has not yet fired — but the next one or two tasks push further past a documented boundary with no headroom.
- The failure will be quiet when it comes: [`git_manager.py:1853-1858`](../../../src/wastech_orchestrator/git_manager.py) — `gh pr edit` failure is caught and downgraded to a `warning` ("never block publish for a cosmetic body update"). The task still reports `done` with a `pr_url`, and the operator's only signal is one log line.

### Expected

- **Update the title on a reused PR.** The same call that appends the section should retitle to something that describes the chain (branch name, or "N tasks on `<branch>`"), not leave task 1's scope as the PR's identity.
- **Bound the body.** Cap total length and elide the oldest sections (each already carries a `<!-- worc-task:<id> -->` marker, so they are individually addressable) — the per-task summary is also on disk in `logs/<task>/summary.md`, so the PR body does not need to be the archive.
- **Promote a size overflow above `warning`.** "The PR body silently stopped reflecting the last N tasks" is a publish-integrity problem, not a cosmetic one; at minimum it belongs in the task's `summary.json` follow-ups where the operator will actually see it.

## VF-16 — per-node model/reasoning allocation is inverted, and the review/documentation pin is a generation behind (model / config)

Severity: **Medium** Status: **open** First seen: 2026-07-25 (all runs in window)

### Observed

Resolved per-node routing across the window (from each `stages/<node>/run-*/1-claude/request.json`):

| Node                | Model             | Reasoning      | Permission      |
| ------------------- | ----------------- | -------------- | --------------- |
| `planning`          | `claude-sonnet-5` | `max`          | read-only       |
| `implementation`    | `claude-sonnet-5` | `xhigh`        | workspace-write |
| `fixing`            | `claude-sonnet-5` | `max`          | workspace-write |
| `review`            | `claude-opus-4-8` | `xhigh`        | read-only       |
| `documentation`     | `claude-opus-4-8` | `medium`       | workspace-write |
| `supervisor`        | `claude-sonnet-5` | `medium`       | read-only       |
| deep-research nodes | `claude-opus-4-8` | `high`/`xhigh` | mixed           |

Three things are off:

1. **The producer is the cheaper model and the reviewers are the expensive one.** `implementation` — the node that writes the code, dominates cost, and whose mistakes drive every fix loop — runs Sonnet 5 ($3/$15 per MTok), while `review` and `documentation` run Opus 4.8 ($5/$25). Reviewing more expensively than you build is backwards: `review` accepted first-pass on 11 of 12 tasks in this window, so the extra capability is not being spent where it changes an outcome.
2. **`documentation` is the worst-priced node in the flow.** A prose summarizer at `medium` reasoning on the most expensive model. Its output (`documentation.out.md`) is a doc-status update and a summary paragraph — Sonnet 5 territory, at 60% of the price.
3. **`claude-opus-4-8` is a generation behind at identical pricing.** Verified against the `claude-api` skill's model table: **`claude-opus-5` is $5/$25 per MTok — exactly Opus 4.8's price** — and is described as a drop-in upgrade at that pricing, specifically stronger on code review (high precision _and_ high recall, and accurate at lower effort). There is no cost argument for staying on 4.8 for `review`.

### Expected

Config is `.worc/config.yaml` (`agents.providers.claude.model`, `supervisor.model`) plus per-node `model`/`reasoning` overrides in `.worc/flows/implementation.yaml` and `deep_research.yaml`; packaged defaults in [`packaged/flows/`](../../../src/wastech_orchestrator/packaged/flows/).

- `review`: `claude-opus-4-8` → **`claude-opus-5`**, same price, better at exactly this node's job. Then test `xhigh` → `high`; Opus 5 review is documented as staying accurate at lower effort, and review is the second-largest cost line.
- `documentation`: → **`claude-sonnet-5`** at `medium`. Nothing this node does needs Opus.
- `implementation`: if any node deserves the Opus tier it is this one — worth an A/B on the next batch, since a single avoided review→fix round (~$0.65–1.10 here) offsets a lot of the per-token delta.
- Scope: target-only until the A/B says otherwise. Do **not** blanket-bump the packaged default.

Two smaller items in the same area: every node runs `timeout_seconds: 7200` (2 h) regardless of shape — a `checks` node that finishes in 14 s and a deep-research synthesis get the same ceiling, so a genuinely hung cheap node burns two hours before anything notices. And `max_turns: 400` with `max_turns_gate: false` was never approached in this window (no attempt came close), so it is currently inert rather than protective.

## VF-17 — the implementation role prompt tells the agent to "follow the plan" when planning is disabled, and renders an empty context heading (prompt)

Severity: **Low** Status: **open** First seen: 2026-07-25 (9 of 12 successful runs)

### Observed

Two prompt-hygiene defects visible in every rendered prompt in the window.

**(a) Dangling plan reference.** [`packaged/flows/implementation/implementation.md:1`](../../../src/wastech_orchestrator/packaged/flows/implementation/implementation.md) opens _"Implement the assigned task in the working tree **by following the plan**."_ In 9 of the 12 successful runs the task set `nodes.planning.enabled: false`, so no plan exists — and the prompt's own context-file list confirms it (`p10-03-stale-comments/stages/implementation/run-000040/rendered-prompt.md` offers exactly one file: `task`). The agent is told to follow an artifact it is not given.

**(b) Empty section header.** Every rendered prompt ends with `## Additional Project Context` followed by 4–7 blank lines and then the context-file list — the heading is emitted unconditionally, with nothing under it when there is no extra context.

### Expected

(a) Condition the clause on the plan's presence — "…following the plan if one is provided in your context files" is enough, and the target's customized copy at `.worc/flows/implementation/implementation.md` should be updated in step with the packaged default. (b) Suppress the heading when the section is empty.

## VF-18 — review findings below the rework threshold are recorded and then dropped (flow)

Severity: **Low** Status: **open** First seen: 2026-07-25 (`p9-06-format-gate`)

### Observed

The `review` evaluator's non-blocking findings are persisted to `evaluations.findings_json` and then go nowhere: they do not gate, they are not merged into the task's follow-ups, and they are not surfaced to the operator. Anything the supervisor doesn't happen to rediscover independently is lost.

### Evidence

`p9-06`'s review returned `verdict: accept` with two `low` findings:

1. _"`AGENTS.md` is added to `.prettierignore` … the enforced gate has a standing hole. It's a defensible choice here because AGENTS.md is read-only for this run, but **it should not stay permanently exempt**."_
2. _"`.worc/` is listed in `.prettierignore`. `.worc/` is orchestrator-private runtime created per-run; it is absent from a clean contributor checkout and from the GitHub Actions checkout the committed CI workflow runs against, so **this entry never matches anything and is dead weight committed into product config**."_

Finding 1 survived only because the supervisor independently produced an equivalent follow-up (`summary.json` → _"[low] AGENTS.md needs a trailing-newline formatting fix"_). Finding 2 was not rediscovered and vanished — and `.worc/` is still in `wastech-mdlint/.prettierignore:12`, committed into the target repo's product config.

### Expected

Merge sub-threshold review findings into the task's `summary.json` `follow_ups` (dedup against the supervisor's own list) so they reach the PR body. The reviewer is already doing the work and the finding is already structured; the only thing missing is the edge from `findings.json` to the follow-ups tracker.

## VF-19 — under read-isolation OFF the env-file loses its `Read` deny at the tool layer, and read-only nodes have no sandbox at all (security)

Severity: **Low** Status: **open** First seen: 2026-07-25 Related: VF-6

### Observed

With `disable_read_isolation: true` the private set is correctly downgraded to Write/Edit-only denies — but the **env file is downgraded with it**, contrary to the code's own stated intent, and read-only nodes have no second layer to catch it.

### Evidence

- [`providers/claude.py:667`](../../../src/wastech_orchestrator/providers/claude.py): `internal_deny_kinds = ("Write", "Edit") if read_isolation_off else ("Read", "Write", "Edit")` — applied uniformly to `read_deny_paths`, which includes the resolved env file.
- Confirmed in the run: `p10-03-stale-comments/stages/implementation/run-000040/1-claude/request.json` → `--disallowedTools` contains `Write(…/.worc/.env)` and `Edit(…/.worc/.env)` but **no `Read(…)`** entry. The only `Read` denies are the public blacklist `Read(.env)` and `Read(secrets/**)` — cwd-relative patterns that do not match `.worc/.env`.
- The intent says otherwise: [`claude.py:493-494`](../../../src/wastech_orchestrator/providers/claude.py) — _"The env-file `credentials` deny below is a targeted secret protection and is **kept regardless**."_ It is kept — but only in `build_sandbox_settings`, i.e. only for the OS Bash sandbox.
- And the sandbox is not always there: `needs_sandbox` is true only for a workspace-write attempt that keeps `Bash`. Read-only nodes (`planning`, `review`, `supervisor`, and every deep-research read node) get **no `claude-sandbox-settings.json` at all** — confirmed by the artifact tree: `stages/implementation/` and `stages/documentation/` have one, `stages/review/` and `stages/planning/` do not. For those nodes the tool-glob list is the entire protection, and it has no env-file `Read` deny.

Net: with read-isolation off, an evaluator node's `Read` tool can read `.worc/.env`. Nothing in this window did so — every node's `events.jsonl` is clean — and the preamble does ask the agent not to. But the mitigation is advisory where the code comment claims it is enforced.

### Expected

Keep the env file (and only the env file) in the `Read` deny set regardless of `read_isolation_off` — it is a targeted secret protection, not part of the native-discovery surface that VF-6 deliberately reopened, so excluding it costs the operator nothing. Mirror the same carve-out in the Codex profile.

## What held up well across the range

Worth recording so these aren't traded away in a later change:

- **Provider layer was flawless.** 83 node runs, 0 fallbacks, 0 retries, 0 crashes, 0 timeouts, `stage_attempts=1` everywhere, `route_source=flow_node` throughout. Not one infrastructure-class failure in 4.6 h.
- **`no_file_change` loop-breaker earned its keep.** Stopped `p10-01-governance-docs-2` at 2 fix rounds against a cap of 15 — the single largest cost saving in the window (see VF-10).
- **VF-8 is genuinely fixed.** Cost is now measurable end to end ($56.70 over the range) and the supervisor layer is on-ledger. Only the timestamps regressed out (VF-12).
- **The reformat constraint actually held.** `p9-06`'s "formatting-only" claim was verified mechanically after the fact: extracting `278cb4f^`, running the repo's Prettier over it, and diffing against `278cb4f` leaves 19 differing files — all either intentional content edits documented in the summary (`ci.yml`, `README`, `glossary`, the phase task file, `generate-docs.mjs` + its two sync tests) or union-type line-wrapping variants that are stable fixed points under `prettier --check` either way. No semantic change rode along. Note this check is cheap and deterministic, and the run did not do it — the supervisor summary itself flags that _"the review step accepted without a detailed diff walk being reported."_
- **Deep-research citations are real.** All 41 sources in `p9-09`'s `sources.json` were re-checked independently: 39 in-repo citations, **0 line mismatches**. The `citation_check` gate is doing something. Its snippet check is file-scoped rather than line-scoped ([`checkers/citation.py:140-141`](../../../src/wastech_orchestrator/core/flow/checkers/citation.py): `on_line or snippet.strip() in text`), so a wrong line number would still pass — worth tightening, but it did not bite here.
- **Exchange isolation was clean.** `exchange_contaminated=0` / `exchange_active_unsafe=0` on all 15 tasks, no `exchange-quarantine/` directory was ever created, and every terminal produced a checksum-verified seal.
- **Planning earns its cost when it runs.** `p10-05-test-depth` spent 780 s / $2.71 on planning and produced a 19 KB, hand-traced plan that named its own riskiest step ("a wrong line number is the single most likely bug source in this whole change"). Implementation then passed review first try. Planning was disabled on 9 of 12 tasks; on the one non-trivial task where it ran, it paid.

## Data gaps in this pass

- **No attempt-level timing from the DB** (VF-12) — node-level wall time came from `node_runs`, per-attempt timing from parsing `daemon.log`. Slow-node analysis below the node level is not currently possible without log parsing.
- **Aborted-run cost is unknowable** (VF-13) — the ~14 min of provider work in `p10-01-governance-docs` and `p10-02-glossary-status` has no usage record, so $56.70 is a floor, not the total.
- **`daemon.log` timestamps are naive local time** while every JSON artifact and DB row is UTC with an offset. Correlating the two requires knowing the host's offset out-of-band (here UTC+2). Worth emitting the offset, or UTC, in the log.
- **`daemon-startup.log` is truncated per launch**, so evidence of earlier daemon restarts within the window was already gone by the time of analysis — only the final session (05:03–05:35 local) survives.
- **No `human_input` events occurred**, so the HITL path is unexercised in this range — including the one case that arguably warranted it (VF-10).
