# 04.3 — Idle hook

[phase](index.md) · [design §2,§7](../../design.md) · [acceptance: AC-C2, AC-X2](../../acceptance-criteria.md)

**Goal:** run one bounded `CleanupJob` pass in the `watch_loop` idle gap — never under an active task, never delaying the next pickup.

## Scope

In: call `CleanupJob.run_once()` in the `watch_loop` idle gap (after `watch_once`, before the poll sleep), work-gated and rate-limited. Out: the job itself (04.2).

## Approach

- Hook into `watch_loop` (`src/wastech_orchestrator/cli.py` ~line 1066): after `results.extend(watch_once(...))` (~line 1104) and **before** the poll sleep (~lines 1110–1114). The single-slot invariant guarantees **no active task** in that gap (design §2).
- **Opportunistic + work-gated** (NFR6): a cheap "is there work?" check gates the pass; rate-limited by the configurable `min_interval` (Q1). The pass is short, interruptible, and does no network.
- Cross-process control uses the **self-managed PID / stop-file**, never `os.kill` / `signal` (NFR8/AC-X2).
- Disabled (Q10) → no cleanup is scheduled.

## Files

- `src/wastech_orchestrator/cli.py` (`watch_loop` idle gap).

## Tests

- Cleanup runs only when no task is active and never delays the next pickup (AC-C2).
- Rate-limited by `min_interval`.
- Disabled → no run (Q10).
- No `os.kill` / `signal` assumptions; green on Windows and POSIX (AC-X2).

## Done when

AC-C2 holds; the idle cleanup is bounded, interruptible, and cross-platform.
