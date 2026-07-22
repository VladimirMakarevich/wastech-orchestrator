# WRI-012 — Prove provider process-tree quiescence

**Status:** implemented **Milestone:** 0 (security prerequisite) **Source:** [decision record](README.md) **Dependencies:** —

Shipped as an injected `ProcessContainment` seam in `providers/process.py` (a POSIX process-group + during-run descendant tracker, and a Windows kill-on-close Job Object via ctypes), a `run_process` quiescence barrier on every exit path, a non-fallback/non-park `ErrorClass.CONTAINMENT_UNVERIFIED` that the Core routes to `manual_action_required`, and an adapter-side fail-closed gate before parse. Windows Job Object real-behaviour is verified under the WRI-006 native gate (unit-tested here via the injected Win32 seam); residual hardening (cgroup v2 / PID-namespace, the sub-poll `setsid`+reparent gap, the Job assign micro-race, a longer-lived PID-reuse guard) is recorded in [follow_ups.md](../follow_ups.md).

## Problem

`providers.process.run_process` kills the provider subtree after timeout or a propagating interrupt, but after an ordinary successful/failed CLI exit it only reaps the root process and clears the recorded handle. A provider command can start a detached/background descendant, close inherited stdio, and let the root exit. That process may keep editing the repository after exchange/Git/control manifests have been checked, after review/checks, or while terminal sealing/commit starts.

This invalidates every “verify immediately after provider exit” guarantee unless “exit” also means no provider-owned process remains. A parent-PID walk performed only after the root exits is insufficient because descendants may already be reparented. On native Windows, `taskkill /T /PID <root>` also cannot be the sole proof after the root PID has disappeared; on POSIX, a process can leave the original process group/session.

## Required outcome

Every provider attempt runs inside a platform-appropriate process containment object whose lifetime the orchestrator owns. Before accepting any result or reading a post-attempt manifest, the orchestrator closes/kills the containment, waits boundedly for all members, and proves it is empty. Failure to prove quiescence is a security/manual-action condition; no downstream check, review, Git command, exchange seal, fallback, or next task may run.

## In scope

- Introduce an injected provider-process containment interface in the process runner; keep provider adapters and Core free of platform-specific process syntax.
- Native Windows: assign the root and descendants to a Job Object (or an equivalently strong supported primitive) with kill-on-close semantics and waitable membership. Do not rely only on `taskkill` after root exit; test nested processes and breakaway restrictions.
- Linux/WSL: prefer a dedicated cgroup/process containment primitive where available. A process group alone is accepted only if the implementation can also detect/prevent descendants escaping via a new session and can prove no members remain.
- macOS: combine the provider-owned process group with bounded descendant tracking/termination that remains valid across reparenting. Strict isolation fails if the host implementation cannot demonstrate containment against a detached `setsid`/daemon-style fixture.
- Run final containment shutdown on every path: exit 0, nonzero, parse/schema failure, timeout, cancellation, exception, and orchestrator stop. Keep the existing children-file behavior for external hard stop, but do not clear it before quiescence is proven.
- Preserve, by name: the `(pid, pgid)` children-file external hard-stop contract, the `start_new_session` group-leader launch model (or a containment replacement with equivalent external-stop semantics), router cancellation-before-fallback, and the pending-graceful-stop invariant.
- Kill-on-close containment deliberately changes orchestrator-crash semantics: today an in-flight provider process survives a daemon crash and can keep writing; under containment it dies with the orchestrator. This is the intended fix for uncontained writers — recovery treats such a task as parked/crashed exactly as it does today, and the change is documented as such.
- Order post-attempt work as: close input → terminate/wait containment → prove empty → verify exchange/Git/control manifests → parse/accept output → route. A provider result is untrusted until this sequence completes.
- Record only PID/container identifiers and outcomes needed for audit; guard against PID reuse and never log argv secrets.
- Define bounded failure behavior for locked files and unkillable processes. Quarantine the task/exchange and block future launches rather than hanging forever or pretending cleanup succeeded.

## Acceptance criteria

- [x] A provider that returns exit 0 after spawning a background writer cannot modify the repository/exchange after `run_process` returns. (POSIX real-process fixture `test_background_in_group_writer_is_reaped_after_exit`; Windows via the Job Object, real-behaviour under WRI-006.)
- [x] Nested, reparented, new-session/process-group, and stdio-detached fixtures are terminated or make strict isolation fail before any manifest/check/Git action. (`test_process_quiescence_posix.py`: nested/reparented in-group + `setsid`-detached tracked-and-reaped; the sub-poll `setsid`+immediate-reparent gap is the documented residual closable only by a kernel container.)
- [x] Windows uses a containment primitive that still owns descendants after the root exits; missing Job Object support cannot silently fall back to unverifiable `taskkill` semantics. (Kill-on-close Job Object with a `QueryInformationJobObject` emptiness proof; if the job cannot be created/assigned the proof **fails closed** rather than degrading to `taskkill`.)
- [x] Normal exit, timeout, exception, cancellation, soft/hard stop, and fake-provider parse failure all clear the handle only after bounded quiescence proof. (`run_process` clears the children-file only when `quiescence.proven`; `test_recorder_handle_retained_when_quiescence_unproven`.)
- [x] An unkillable/unknown subtree blocks fallback and the next task and preserves actionable, secret-free diagnostics. (`CONTAINMENT_UNVERIFIED` → non-fallback → `manual_action_required` (terminal, blocks the next task) with a platform+count+pids detail; `test_containment_unverified_*`.)
- [x] Existing cross-platform stop semantics remain intact; this task does not make a graceful operator stop implicitly hard-kill an active node before the existing contract says so. (Children-file `(pid,pgid)` contract, `start_new_session` group-leader launch, router cancellation-before-fallback, and pending-graceful-stop preserved by name; `test_process_control.py` + routing/process regressions green.)

## Verification

- Native Windows/macOS/Linux integration fixtures that spawn multi-generation, detached, reparented, and delayed-writer children.
- Race tests around root exit, PID reuse, handle clearing, timeout, and manifest ordering.
- Tests proving no checks/review/Git/seal callback runs before the quiescence barrier.
- Daemon/watch and foreground paths, including `stop --force-full`, cancellation, and recovery after an intentionally failed containment proof.

## Out of scope

- General host cleanup of processes not created inside the provider containment.
- Changing the existing pending graceful-stop versus explicit hard-stop operator contract.
- Treating a sleep-and-recheck heuristic as proof of quiescence.

## Likely implementation areas

- src/wastech_orchestrator/providers/process.py
- src/wastech_orchestrator/process_control.py and cli.py stop/watch wiring
- provider-attempt lifecycle before postprocessing
- tests/providers/test_process.py, tests/test_process_control.py, and native-OS integration tests
- docs/operations.md and packaged guide
