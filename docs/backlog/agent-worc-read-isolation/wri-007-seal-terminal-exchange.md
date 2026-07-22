# WRI-007 — Seal terminal exchanges and restore only for continue

**Status:** open **Milestone:** 1 **Source:** [decision record](README.md), [happy-path.md](happy-path.md) **Dependencies:** WRI-001, WRI-012

## Problem

The exchange is an agent-readable, in-repository surface. Cleaning only successful tasks leaves failed and manual-action tasks visible to the next task; retaining successful exchanges behind a configuration toggle does the same. Deleting the exchange outright also discards the curated plan, diff, findings, checks input, and HITL packet while the previous plan claimed the audit trail was unchanged.

The required invariant is stronger: immediately before any provider launch, `.worc-io/` contains at most the current task's verified active exchange. Terminal outcome, debugging preference, and Windows cleanup behavior must not relax it.

## Required outcome

After WRI-012 proves the provider process containment empty, every terminal status (`DONE`, `FAILED`, `MANUAL_ACTION_REQUIRED`) seals a checksum-verified snapshot of the current exchange into that task's private audit and removes the active in-repo directory. `rerun --continue` from such a terminal resumable state may restore and verify the latest sealed snapshot before resuming. A stopped/crashed/parked nonterminal task keeps its same-task active exchange and verifies it on continue. Fresh/restart creates a clean exchange. In CLI terms: fresh/restart is `rerun` (including its restart-in-place branch for pre-checkpoint tasks), continue is `rerun --continue`; the daemon `restart` command is unrelated to this lifecycle. There is no configuration option that retains an agent-readable terminal exchange.

## Gap handed over by WRI-001 (decoupled build)

WRI-001 shipped a **minimal interim** terminal teardown that this task replaces with the real sealing protocol:

- `core/orchestrator.py._go_terminal` calls `clear_exchange_task_dir(self._exchange_root, p.task.id)` with **no** seal, checksum-verify, or contaminated-tree quarantine. Replace it with the WRI-012-quiescence-gated flow: seal a checksum-verified snapshot into the task's private audit, then remove the active directory (and quarantine a mutation-flagged tree instead of sealing it).
- The `rerun --continue`-from-terminal path does **not** restore a sealed snapshot yet — WRI-001's `_restore_engine_inputs` re-publishes the private artifacts into a fresh exchange on resume. WRI-007 adds the restore-and-verify-the-latest-sealed-snapshot path.
- The primitives to build on already exist in `providers/exchange.py`: `build_exchange_manifest` (file type, link identity/count, relative name, size, content digest) is the checksum/manifest surface to seal and diff, and `assert_exchange_current_task_only` is the pre-launch "at most the current task dir" gate to extend with the stale-exchange next-task block.
- The fresh/restart `clear_exchange_task_dir` calls in `rerun_task` / `restart_task_in_place` stay as-is (fresh starts clean); only the terminal-seal semantics change.

## In scope

- Introduce explicit `seal`, `restore_for_continue`, and `ensure_current_exchange` operations owned by the artifact/lifecycle layer.
- Hook sealing into every terminal producer, including normal pipeline completion and out-of-band finalize/status paths. Centralize at a terminal transition seam where possible; otherwise enumerate and test all producers.
- Store snapshots under the private task audit with a manifest containing task id, run/attempt identity, source layout version, relative file names, sizes, and cryptographic checksums. Never archive symlink/junction/reparse targets.
- If WRI-002 reports agent-side exchange mutation, move the tree to a clearly contaminated evidence location and record the parent-held expected manifest plus observed manifest. Never label or restore that tree as a clean sealed snapshot; if no independent clean seal exists, continue is refused and fresh/restart is required.
- Seal all terminal outcomes. Debugging uses the private snapshot and raw audit, not an in-repo retention escape. Update the operator debugging guidance and every tool that reads terminal-task artifacts in the same change — `worc logs`/status docs and the repository's own `.claude/skills/analyze-task-run` skill must point at the sealed/private locations.
- Restore a sealed snapshot only for an authorized `rerun --continue`/HITL continuation of the same terminal resumable task. For a nonterminal parked/crashed task, verify/reuse the already-active same-task exchange instead of overwriting it from an older seal. A fresh/restart rerun archives old private state per existing semantics and starts empty.
- Preserve run-number fan-in and the exact exchange layout. Restoration must not shadow newer private/checkpoint state.
- Before every provider launch, reject a missing expected current exchange, multiple task directories, a mismatched task id, an unverified restore, or any stale terminal exchange. Never silently clean unknown data and continue.
- Never build a final manifest or seal while provider process-tree quiescence is unproven; a surviving/unknown descendant blocks cleanup and every later launch.
- Keep already-recorded terminal status stable if post-status cleanup encounters a Windows lock, but prevent any later provider launch until the stale directory is safely sealed/removed. Surface the cleanup condition operationally.

## Cross-platform file operations

- Prefer an atomic rename when source and private destination share a filesystem, then verify the manifest.
- For cross-volume relocation, copy into a private temporary sibling, flush/close files, verify sizes/checksums, atomically rename the completed snapshot, and only then remove the source.
- Handle Windows read-only attributes and transient sharing violations with bounded, observable retries. Do not use `ignore_errors=True`; it makes failure state unknowable.
- Use `pathlib`; use `newline=""` or bytes for copied/manifested files; store relative paths in POSIX form.
- Validate every ancestor against symlink/junction/reparse escape before copy, restore, and deletion. A discovered escape is a security failure, never a recursive cleanup target.

## Acceptance criteria

- [ ] `DONE`, `FAILED`, and `MANUAL_ACTION_REQUIRED` all leave no active task directory after successful sealing and retain a verified private snapshot.
- [ ] Both normal pipeline success and operator finalize/merge/PR-sync terminal paths seal the exchange.
- [ ] Terminal-state `rerun --continue` and HITL continuation restore only the same task's verified latest snapshot; parked/crashed nonterminal continue reuses the verified active exchange; fresh/restart starts clean.
- [ ] The snapshot retains every curated exchange artifact and its manifest while existing raw provider/prompt/state audit remains untouched.
- [ ] An agent-mutated exchange is quarantined as contaminated evidence, removed from the active root, and excluded from restore selection.
- [ ] A stale/foreign/multiple exchange blocks the next provider before model execution.
- [ ] A Windows lock/read-only failure is logged with the exact target, does not falsely report cleanup success, and blocks later launches until resolved without changing an already terminal task's status.
- [ ] No retention setting can keep a terminal exchange agent-readable.
- [ ] Repeated seal/restore calls are idempotent or fail with a precise state-conflict error; they never merge unrelated task contents.

## Verification

- Full fake-provider pipeline tests for every terminal status and both success producers.
- Continue-after-`FAILED` and continue-after-HITL tests proving verified restore and downstream input reconstruction, plus parked/crashed continue proving an active exchange is not replaced by an older snapshot.
- Fresh/restart tests proving no old exchange artifact survives.
- Corrupt manifest, checksum mismatch, foreign task id, multiple directories, symlink/junction/reparse escape, and interrupted cross-volume copy tests.
- Injected Windows lock/read-only and retry-exhaustion tests; POSIX same-filesystem rename tests.
- WRI-006 cross-platform gate.

## Out of scope

- Deleting private audit data; existing private retention/`logs clean` remains separate.
- Cross-task parallel worktrees. This task enforces the single-active-exchange model chosen by this decision record.
- Provider-specific filesystem policy (WRI-002/003).

## Likely implementation areas

- src/wastech_orchestrator/core/orchestrator.py
- src/wastech_orchestrator/providers/artifacts.py
- src/wastech_orchestrator/core/recovery.py
- tests/core/ and tests/providers/
- docs/operations.md, packaged guide, and .claude/skills/analyze-task-run/
