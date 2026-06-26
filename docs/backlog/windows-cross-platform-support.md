# Windows / Cross-Platform Support

Status: **proposed** Date: 2026-06-25 Owner: Vladimir Makarevich

The orchestrator runs on macOS and Linux today; this item tracks the work to make it fully functional on Windows — including `worc run`, the `worc watch`/`worc stop` daemon, the test suite (pytest), and CI.

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
