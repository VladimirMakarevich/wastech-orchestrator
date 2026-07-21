# WRI-012 — Prove provider process-tree quiescence

**Status:** open **Milestone:** 0 (security prerequisite) **Source:** [decision record](README.md) **Dependencies:** —

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
- Order post-attempt work as: close input → terminate/wait containment → prove empty → verify exchange/Git/control manifests → parse/accept output → route. A provider result is untrusted until this sequence completes.
- Record only PID/container identifiers and outcomes needed for audit; guard against PID reuse and never log argv secrets.
- Define bounded failure behavior for locked files and unkillable processes. Quarantine the task/exchange and block future launches rather than hanging forever or pretending cleanup succeeded.

## Acceptance criteria

- [ ] A provider that returns exit 0 after spawning a background writer cannot modify the repository/exchange after `run_process` returns.
- [ ] Nested, reparented, new-session/process-group, and stdio-detached fixtures are terminated or make strict isolation fail before any manifest/check/Git action.
- [ ] Windows uses a containment primitive that still owns descendants after the root exits; missing Job Object support cannot silently fall back to unverifiable `taskkill` semantics.
- [ ] Normal exit, timeout, exception, cancellation, soft/hard stop, and fake-provider parse failure all clear the handle only after bounded quiescence proof.
- [ ] An unkillable/unknown subtree blocks fallback and the next task and preserves actionable, secret-free diagnostics.
- [ ] Existing cross-platform stop semantics remain intact; this task does not make a graceful operator stop implicitly hard-kill an active node before the existing contract says so.

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
