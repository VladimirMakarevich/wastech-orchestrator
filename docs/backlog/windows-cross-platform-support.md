# Windows / Cross-Platform Support

Status: **implemented (dev loop + daemon)** Date: 2026-06-25 · Implemented: 2026-06-26 Owner: Vladimir Makarevich

The orchestrator runs on macOS and Linux today; this item tracks the work to make it fully functional on Windows — including `worc run`, the `worc watch`/`worc stop` daemon, the test suite (pytest), and CI.

> **Implementation outcome (2026-06-26).** Implemented on a real Windows 10 / Python 3.14 machine, validating each premise against the actual behaviour rather than theory. Two premises turned out wrong; see [Implementation outcome](#implementation-outcome) below. Delivered: a green dev loop (`/run-checks` — ruff, mypy, full pytest — passes on Windows) and a working, cross-platform `worc watch`/`worc stop`/`worc restart` daemon. CI (the Windows runner matrix) was intentionally left out of scope.

## The problem

Three concrete failures block Windows usage:

1. **pytest suite falls at collection** — `conftest.py`'s `fake_cli` fixture creates `.cmd` launcher files on Windows, but `subprocess.Popen(["path/to/foo.cmd"])` without `shell=True` fails on Windows because `CreateProcess` cannot execute `.cmd` files directly. The `shell=True` escape is barred by the security policy.
2. **`worc stop` cannot gracefully shut down the watcher** — `os.kill(pid, signal.SIGTERM)` sent cross-process on Windows calls `TerminateProcess()` (immediate kill); the Python signal handler in `StopController` is never invoked, so in-flight tasks are cut mid-stage without cleanup.
3. **CI never validates Windows** — `ci.yml` only runs `ubuntu-latest`; regressions on Windows go undetected.

The Python core (pathlib, subprocess with `shell=False`, `tempfile`, atomic `os.replace`, env allowlist) is already cross-platform-clean. The gaps are confined to test infrastructure and the daemon's IPC layer.

## Constraints

- `shell=True` is barred (security invariant in `.agents/rules/security.md`). All process launches must be argv lists.
- No new Windows-specific production dependencies (no `pywin32`, no `ctypes` bridges). The fix must work with the Python standard library only.
- POSIX signal behavior must not regress: `SIGTERM` on Linux/macOS stays the primary stop path.

## Alternatives considered

| Option | Why rejected |
| --- | --- |
| Declare `watch`/`stop` POSIX-only, document Windows limitation | User explicitly needs full daemon support on Windows. Acceptable for a later phase if stop-file proves complex, but not the chosen path. |
| Win32 Named Events (`CreateEvent` / `SetEvent` via ctypes) | Requires a platform-specific bridge and parallel code paths. Standard-library stop-file is simpler and equally reliable. |
| `atexit` handler instead of SIGTERM for graceful cleanup | `TerminateProcess()` does not run `atexit` handlers; in-flight task still gets cut. Not a fix. |
| Replace `.cmd` with a `.bat` wrapper that invokes `cmd.exe` | Still requires `shell=True` or explicit `cmd.exe /c` wrapping in the adapter — production code change for a test concern. |

## Decision

Three targeted changes, no new dependencies:

**1. Stop-file IPC** (`process_control.py`, `cli.py`): `worc stop` writes an `orchestrator.stop` sentinel file alongside the PID file. The `watch` loop polls for it at each tick (same poll interval as today). On POSIX the existing `SIGTERM` path is kept as the primary signal; the stop-file is the cross-platform fallback that `worc stop` always writes. This keeps behavior identical on Linux/macOS and adds reliable graceful shutdown on Windows.

**2. Python-launcher `fake_cli`** (`tests/conftest.py`): Replace the OS-branch (POSIX shebang / Windows `.cmd`) with a single cross-platform Python launcher: a `.py` script executed as `[sys.executable, launcher_path, *args]`. The fixture returns `sys.executable` as the command and the launcher path as the first fixed argument (or stores them as a `(executable, script)` pair). No shell, no `.cmd`, no platform branch in the fixture.

**3. CI matrix** (`.github/workflows/ci.yml`): Add `windows-latest` to the runner matrix alongside `ubuntu-latest`. Same Python versions (3.12, 3.13).

Cost of not choosing: Windows stays broken indefinitely; the test suite provides no coverage guarantee for Windows operators.

## Open questions

- Should `worc watch` on Windows also install a `CTRL_BREAK_EVENT` handler so `Ctrl+C` in a terminal triggers graceful stop? (Likely yes — low cost, matches UX on POSIX.)
- macOS `_read_proc_start_time` currently returns `None` (no `/proc`). The code comment mentions `ps -o lstart=`. Implementing it would tighten the PID-recycling guard on macOS. Worth doing in the same pass or defer?

## Implementation notes

Key files and seams:

- `src/wastech_orchestrator/process_control.py` — add `stop_file_path()` helper, write stop-file in `stop_process()`, add `stop_file_requested()` probe for the watch loop.
- `src/wastech_orchestrator/cli.py` — `_watch_loop` polls `stop_file_requested()` at each tick; `cmd_stop` writes the stop file in addition to (or instead of, on Windows) `os.kill`.
- `tests/conftest.py` — `fake_cli` fixture: single `.py` launcher on all platforms; return `(sys.executable, str(launcher))` or equivalent for adapter invocation.
- `.github/workflows/ci.yml` — add `windows-latest` to the `runs-on` matrix.

## Implementation outcome

Implemented 2026-06-26 on Windows 10 / Python 3.14, each premise checked against real behaviour.

**Premise that was wrong #1 — the `.cmd` `fake_cli`.** `subprocess.run(["foo.cmd"], shell=False)` works on Python 3.14 / Windows 10, and every provider/router integration test passes with the existing `.cmd` launcher. The proposed Python-launcher rework (change #2) addressed a problem that does not reproduce, and would have required a production change (`ProviderConfig.command` is a single `argv[0]`). **Skipped** — `tests/conftest.py` is unchanged.

**Premise that was wrong #2 — `os.kill` cross-process.** The ADR assumed `os.kill(pid, SIGTERM)` merely hard-kills on Windows. In fact Python's `os.kill` opens the target with `OpenProcess(PROCESS_ALL_ACCESS)`, which **fails (winerror 87) for any process the caller holds no handle to** — and `worc stop` runs in a different process than `worc watch`. So `os.kill` can neither probe (`is_running`) nor signal the daemon cross-process on Windows; a stop-file alone is necessary but not sufficient. The daemon control was therefore built as a **platform split** (`process_control._can_signal`):

- **POSIX** keeps the existing path: `SIGTERM` for an immediate wakeup, poll `is_running`, escalate to `SIGKILL` after `--timeout`. The stop-file is also written (a harmless cross-platform fallback).
- **Windows** uses no `os.kill`: `stop` writes the `orchestrator.stop` sentinel the watch loop polls; the daemon notices it between ticks, exits, and removes its own PID file, whose disappearance is how `stop` confirms shutdown (`_stop_via_pid_file`). A wedged Windows daemon (one no longer polling) cannot be force-killed by `stop` — after the timeout it clears the PID file and reports `timed_out`; the operator stops the survivor via Task Manager. This is the **accepted limitation** of the file-based design (see the deferred `taskkill` backstop in [follow_ups.md](follow_ups.md)).

**Test suite — the 8 real Windows failures fixed.** `tests/test_process_control.py` used a bare `signal.SIGKILL` (absent on Windows) → an OS-independent `KILL = getattr(signal, "SIGKILL", 9)` sentinel + explicit injected signals; `SkillRef.path` stored native backslashes → `Path.as_posix()` (deterministic cross-platform reference paths); `_install_atomic_write` wrote text-mode (CRLF on Windows) → `newline=""` so installed/templated files stay LF; `tests/test_stage_site_docs.py` passed a POSIX `/repo` whose `.resolve()` injects a drive on Windows → resolve the root up front; `tests/core/test_recovery.py` hand-wrote a persisted skill path as `str()` → `as_posix()` to mirror production. Also fixed a pre-existing ruff `F401`. `is_running`/`stop_process` were hardened to treat the Windows "no such process" `OSError` (winerror 87) as dead (`_is_no_such_process`).

**Environment.** A stale non-editable install of `wastech-orchestrator` in site-packages was shadowing the working tree's `src/`; reinstalled editable (`pip install -e .`). This is the intended dev install per CLAUDE.md.

**Deferred** (tracked in [follow_ups.md](follow_ups.md)): the Windows CI runner matrix (change #3); a `taskkill /F /PID` hard-kill backstop for a wedged Windows daemon; the `CTRL_BREAK_EVENT` Ctrl+C handler; and the macOS `_read_proc_start_time` recycling guard.
