# VF-20 — governance/instruction files must never be write-blocked; report the change to the operator instead

Status: **shipped 2026-07-25** (write-deny removed; operator notice on four surfaces — run-log/console WARNING, PR/commit summary, completed-ledger `governance_changed`, Telegram) Date: 2026-07-25 Owner: Vladimir Makarevich Related: [VF-5](runtime-validation-findings.md) (the rollback that introduced the instruction write-deny), [VF-6](runtime-validation-findings.md) (`disable_read_isolation`), [VF-7](vf7-security-preamble-investigation.md) (the advisory preamble text), [VF-10](runtime-validation-findings.md) (the blocked-agent flow defect this surfaced through)

## Requirement (operator-stated, mandatory)

The orchestrator **must not block** an agent from editing repository governance files — `AGENTS.md`, `AGENTS.override.md`, `CLAUDE.md`, any equivalent per-tool instruction file, or the rules they reference (`.agents/rules/**`). Editing them is ordinary repository work: the change lands as a normal proposed diff and the operator reviews it in the commit/PR like any other change.

**The only hard-denied location is the orchestrator's private runtime directory `.worc/`.** Everything else the agent may write, subject to the existing Git/publish invariants (see Non-goals).

What is allowed instead of a block: **tell the operator, explicitly and loudly, that this run changed governance files.** A notice — never a refusal, and never an approval gate by default.

## Current behavior (the defect)

VF-5's shipped resolution replaced repo-instruction _injection_ with **filesystem immutability**: the tracked root instruction files are added to the provider write-deny set for every workspace-write attempt, so the agent can read them but cannot edit them. So, we must remove this restrictions and all related code/tests.

- The deny set carries them: `instruction_files` on `ProviderWriteGuardPolicy` and its inclusion in `denied_write_paths` — [runtime_layout.py:174-186](../../../src/wastech_orchestrator/runtime_layout.py#L174-L186).
- They are resolved per workspace-write attempt and threaded onto the request — [nodes/agent.py:402-415](../../../src/wastech_orchestrator/core/flow/nodes/agent.py#L402-L415) (discovery via `discover_repository_instructions`) → [git_manager.py:1158-1166](../../../src/wastech_orchestrator/git_manager.py#L1158-L1166) (`resolve_control_paths`) → `AgentRunRequest.write_guard` [providers/base.py:197-206](../../../src/wastech_orchestrator/providers/base.py#L197-L206).
- Each adapter renders them into its own deny syntax — Claude `denyWrite` sandbox settings [claude.py:497-498](../../../src/wastech_orchestrator/providers/claude.py#L497-L498) and `Write`/`Edit` tool denies [claude.py:669-671](../../../src/wastech_orchestrator/providers/claude.py#L669-L671); Codex via the permission profile [codex.py:373](../../../src/wastech_orchestrator/providers/codex.py#L373).
- The advisory preamble reinforces it in prose: _"`AGENTS.md`, `AGENTS.override.md`, `CLAUDE.md` are read-only this run"_ — [security_preamble.py:52-53](../../../src/wastech_orchestrator/core/flow/security_preamble.py#L52-L53).

### Runtime evidence

Task `p10-01-governance-docs-2` (a documentation-only task whose deliverables are edits to `AGENTS.md` and `.agents/rules/architecture.md` — see the pending task file `tasks/pending/p9-10-01-governance-docs_3.md` in `wastech-mdlint`) could not perform its primary deliverable. The `fixing` node's report opens with:

> _"I could not complete the primary deliverable. **Blocker: `AGENTS.md` is write-protected by the sandbox for this run, and I can't lift that.**"_

Recorded in [VF-10](runtime-validation-findings.md) (`stages/fixing/run-000030/fixing.out.md`). The run then burned a full review→fix round rediscovering the same wall and finished at **$4.32** without ever producing the change. The task remains unrunnable: the `AGENTS.md` half of its deliverable is impossible under the current envelope, no matter how the task is written.

### The documented trade-off was wrong

VF-5 accepted this explicitly: _"a task whose subject is editing repository guidance under strict isolation is already an unsupported/edge case"_. That assessment is rejected — governance-doc maintenance is normal, recurring work in a repo that keeps its agent rules under version control, and it is exactly the kind of change an orchestrator should be able to make.

The docs already contradict the code. [docs/operations.md:331](../../operations.md) claims _"Tasks that legitimately edit repository guidance/skills still work — the edit lands as an ordinary proposed diff"_. For skills that is true (skill directories are not write-denied); for `AGENTS.md`/`CLAUDE.md` it is false — the agent's `Write`/`Edit` is denied at the tool **and** sandbox layer.

## What to build

### 1. Remove the instruction write-deny

Drop `instruction_files` from the write-guard: the field and its `denied_write_paths` contribution in `runtime_layout.py`, the `instruction_files` parameter of `GitManager.resolve_control_paths`, and the per-attempt discovery call in `nodes/agent.py`. `discover_repository_instructions` / `freeze_repository_instructions` **stay** — the per-source freeze still folds into `instruction_manifest_digest` for audit, which is the record of what the agent read.

Confirm no resume/continue path fail-closes on a live instruction-file edit. It should not: `load_instruction_bundle` re-hashes the **frozen copies** under the bundle dir, not the live files, and `instruction_bundle.py` states there is deliberately no post-node live-mutation gate for these ([instruction_bundle.py:21-23](../../../src/wastech_orchestrator/core/flow/instruction_bundle.py#L21-L23)). Add a regression test that pins it.

### 2. Reword the advisory preamble

Replace the "read-only this run" line with an accurate one: the agent may change governance files when the task calls for it, as an ordinary diff, and should not rewrite its own rules opportunistically. Keep the `.worc/` read+write prohibition, the `.worc-io/` and `.git/` lines, and the never-commit/push line exactly as they are.

### 3. Report governance changes to the operator (the replacement for the block)

When a workspace-write attempt's diff touches a governance path, emit an explicit **notice** — non-blocking, no approval, no state change:

- a run-log line naming the changed governance paths;
- the same list on the task's completion/ledger record, so it is visible in `worc status` / the completed-ledger without reading logs;
- a line in the PR/commit summary, so the reviewer sees it at review time.

Natural seam: the diff is already classified per workspace-write attempt at [nodes/agent.py:489-491](../../../src/wastech_orchestrator/core/flow/nodes/agent.py#L489-L491) (`changed_code_entries()` → `evaluate_diff_gate`). Emit the notice there, **before and independent of** the gate. Reuse a fixed governance path set (`REPO_INSTRUCTION_NAMES` plus `.agents/rules/**`) — deterministic, no new config key.

### 4. Reproducibility: record drift, do not prevent it

VF-5 bought exactly one genuinely valuable property with the deny — _"reproducible instructions across nodes / resume / fallback"_. Removing the deny gives that up as an enforced guarantee. The replacement is **observation, not prevention**: the `instruction_manifest_digest` already captures the task-start state, so a mid-task governance edit becomes a recorded, reportable fact (fold it into the §3 notice when the live file no longer matches its frozen digest). A later node reading the edited file is the correct outcome — it is reading the repository as it now is.

## Non-goals / explicitly out of scope

- **`.worc/` stays fully denied** (read and write): the private runtime — state, `state.db`, logs, secrets, frozen bundles. Unchanged.
- **`.git/`, its hooks and config stay write-denied.** Not governance: this is the hard invariant _"only the orchestrator does commit / push / PR"_. Lifting it would let an agent publish, and is not what this requirement asks for.
- **`tasks/` stays write-denied**, and **`.worc-io/` stays write-denied.** Both are orchestrator-owned control surfaces (lifecycle bookkeeping and the curated read-only exchange projection), not repository governance content. Reading the requirement's "only `.worc` is denied" to also cover these would break the lifecycle and the invariant above; if the operator does intend them, that is a separate decision with its own task.
- **No new config flag to restore the block.** The requirement is "never block", so a `block_governance_writes` switch would re-introduce exactly what is being removed. Operators who want an approval step on these paths already have `security.protected_paths` — an opt-in always-ask floor over any glob ([dangerous_diff.py:85-117](../../../src/wastech_orchestrator/core/dangerous_diff.py#L85-L117), [docs/configuration.md](../../configuration.md) §`protected_paths`). That is an operator choice, not a default.
- **No change to the read side.** `disable_read_isolation` (VF-6) and `denied_read_paths` are untouched.

## Acceptance criteria

- [x] An agent node can create, edit, and delete `AGENTS.md`, `AGENTS.override.md`, `CLAUDE.md`, and any file under `.agents/rules/`, and the change lands in the task's diff and commit like any other file.
- [x] `ProviderWriteGuardPolicy.denied_write_paths` no longer contains any repository-instruction path; the rendered Claude `denyWrite`/tool-deny and Codex permission profile carry none either (asserted per adapter).
- [x] The governance-path deny is not reachable through any config key, task front-matter field, `extra_args`, or flow node — it is gone, not gated (the field and its threading are deleted).
- [x] `.worc/` remains read- and write-denied; `.git/`, its hooks/config, `tasks/`, and `.worc-io/` remain write-denied (regression-tested — this change must not widen the envelope beyond governance files).
- [x] The security preamble no longer claims the instruction files are read-only, and its `.worc/`/`.worc-io/`/`.git/`/`tasks/`/no-publish lines are byte-identical to today.
- [x] A run whose diff touches a governance path emits the operator notice on all three surfaces (run log, task record, PR/commit summary) — plus the Telegram completion message (operator addition) — and **still completes normally** — no approval request, no `manual_action_required`, no state-machine change.
- [x] A run whose diff touches no governance path emits no notice (no noise on ordinary tasks).
- [x] Continue/resume/fallback succeed after a governance file was edited mid-task — no digest fail-closed, no `manual_action_required`.
- [ ] `wastech-mdlint` task `p9-10-01-governance-docs` completes its `AGENTS.md` deliverable end to end (the real-target verification for this change — pending an actual run against that target repo).

## Docs to update in the same change

- [docs/operations.md:202](../../operations.md), [:331](../../operations.md) — drop "instruction write-deny" from the write-side list; correct the "tasks that edit repository guidance still work" paragraph so it is true; document the notice.
- [docs/configuration.md:319](../../configuration.md), [:351](../../configuration.md) — both places list the instruction write-deny as part of the write side that "stays in force" under `disable_read_isolation`; remove it, and cross-reference `protected_paths` as the opt-in way to gate governance paths.
- [.agents/rules/security.md](../../../.agents/rules/security.md) — record the decision: governance/instruction files are ordinary repository content; `.worc/` is the private boundary; notification replaces prevention. This is a § MANDATORY-aligned narrowing (least restrictive solution), so state it as such.
- [runtime-validation-findings.md](runtime-validation-findings.md) — amend the VF-5 Resolution: the "write-deny the tracked instruction files" half is reverted, and the accepted trade-off ("editing repository guidance is an unsupported edge case") is withdrawn. Add the VF-20 pointer.
- Packaged operator-facing docs under `src/wastech_orchestrator/packaged/` — the `guide/` quickstarts and the config reference wherever the instruction write-deny is described.

## Likely area

`runtime_layout.py` (drop the field), `git_manager.py` (`resolve_control_paths` signature), `core/flow/nodes/agent.py` (drop the discovery/threading; add the notice at the diff-classification seam), `core/flow/security_preamble.py` (reword), `providers/claude.py` + `providers/codex.py` (deny-set assertions in tests only — the rendering code is generic), plus the notice's reporting surfaces (run log, task record, PR body builder).
