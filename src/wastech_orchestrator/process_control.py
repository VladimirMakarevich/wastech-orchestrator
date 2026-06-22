"""PID-file and graceful-shutdown plumbing for the ``watch`` daemon (backlog: stop/restart).

Pure and print-free by design: the CLI owns all operator output and exit codes; this module only
reads/writes the PID file, probes liveness, signals a process, and bridges ``SIGTERM`` to a
``threading.Event`` the watch loop polls between ticks. Every OS seam (:func:`os.kill`,
:func:`signal.signal`, sleeping, the clock) is injectable so the whole module is unit-testable
without real processes or signals.

The handler **sets an event rather than raising**, so a ``SIGTERM`` that arrives mid-tick lets the
in-flight task finish its current stage; the loop then exits cleanly at the next top-of-loop check.
This is the same idiom as :func:`wastech_orchestrator.observability.progress.run_with_heartbeat`.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import FrameType, TracebackType

# Injectable OS seams (defaults are the real calls; tests pass fakes).
KillFn = Callable[[int, int], None]  # os.kill(pid, sig)
SleepFn = Callable[[float], None]  # time.sleep
NowFn = Callable[[], float]  # time.monotonic
StartTimeFn = Callable[[int], str | None]  # opaque per-pid start-time token (recycling guard)
# The handler/return type accepted by signal.signal (mirrors typeshed's _HANDLER).
SignalHandler = Callable[[int, FrameType | None], object] | int | signal.Handlers | None
SignalFn = Callable[[int, SignalHandler], SignalHandler]

PID_FILENAME = "orchestrator.pid"


@dataclass(frozen=True)
class ProcessIdentity:
    """A recorded daemon identity: its PID plus an opaque start-time token.

    ``start_time`` is a platform-specific, monotonically-unique-per-PID token used only for equality
    (Linux: ``/proc/<pid>/stat`` field 22; macOS/BSD: ``ps -o lstart=``). ``None`` when it could not
    be read — liveness then falls back to a bare PID probe (no recycling protection).
    """

    pid: int
    start_time: str | None


def _read_proc_start_time(pid: int) -> str | None:
    """Best-effort process start-time token for ``pid`` (recycling guard); Linux ``/proc`` only.

    Returns the 22nd ``/proc/<pid>/stat`` field (``starttime``, ticks since boot) on Linux,
    else ``None``. Non-Linux POSIX has no dependency-free start-time source and this module never
    launches a child process (the no-shell-out invariant), so there liveness degrades to a bare PID
    probe — the documented fallback. Never raises.
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # Field 2 is "(comm)" and may contain spaces/parens; split after the final ')'. The token after
    # comm at 0-based index 19 is starttime (the 22nd stat field), ticks since boot.
    rparen = stat.rfind(")")
    if rparen == -1:
        return None
    fields = stat[rparen + 2 :].split()
    return fields[19] if len(fields) > 19 else None


def pid_file_path(artifacts_root: str | os.PathLike[str]) -> Path:
    """The canonical PID-file location under an artifacts root (``<root>/orchestrator.pid``)."""
    return Path(artifacts_root) / PID_FILENAME


def write_pid_file(
    path: Path, *, pid: int | None = None, start_time_fn: StartTimeFn = _read_proc_start_time
) -> None:
    """Atomically write the current (or given) PID + its start-time token to ``path``.

    The file is small JSON (``{"pid": ..., "start_time": ...}``); the start-time lets a later probe
    tell our daemon apart from an unrelated process that recycled the PID. Atomic via a temp file in
    the same directory + :func:`os.replace`, so a concurrent ``stop`` never observes a half-write.
    """
    pid = os.getpid() if pid is None else pid
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = {"pid": pid, "start_time": start_time_fn(pid)}
    tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_pid_record(path: Path) -> ProcessIdentity | None:
    """Return the :class:`ProcessIdentity` recorded in ``path``, or ``None`` if absent/malformed.

    Tolerant on purpose: a corrupt or missing PID file is treated as "no daemon recorded", which is
    what keeps ``stop`` idempotent. A pre-JSON (bare-integer) file is treated as malformed (returns
    ``None``); greenfield, so a stale pre-upgrade PID file is safely ignored, not migrated.
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    start = data.get("start_time")
    return ProcessIdentity(pid=pid, start_time=start if isinstance(start, str) else None)


def read_pid(path: Path) -> int | None:
    """Return just the PID recorded in ``path``, or ``None`` if absent/malformed (see
    :func:`read_pid_record`)."""
    record = read_pid_record(path)
    return record.pid if record is not None else None


def is_running(
    pid: int,
    *,
    expected_start: str | None = None,
    kill_fn: KillFn = os.kill,
    start_time_fn: StartTimeFn = _read_proc_start_time,
) -> bool:
    """True iff a process with ``pid`` exists, is signalable by us, and is not a recycled PID.

    ``ProcessLookupError`` (ESRCH) → not running; ``PermissionError`` (EPERM) → running but owned by
    another user (treated as alive — we cannot read its start-time). When ``expected_start`` is
    given and the live process's start-time differs, the PID was recycled → ``False``. When either
    start-time is unavailable the check degrades to the bare PID probe.
    """
    try:
        kill_fn(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if expected_start is None:
        return True
    actual_start = start_time_fn(pid)
    if actual_start is None:
        return True  # cannot determine the live process's start-time → trust the bare probe
    return actual_start == expected_start


def running_daemon_pid(
    path: Path, *, kill_fn: KillFn = os.kill, start_time_fn: StartTimeFn = _read_proc_start_time
) -> int | None:
    """The PID of the genuinely-running daemon recorded in ``path``, or ``None``.

    Combines :func:`read_pid_record` with :func:`is_running`'s start-time recycling guard: a
    recorded PID that is dead — or alive but whose start-time no longer matches (recycled) — reads
    as ``None``.
    """
    record = read_pid_record(path)
    if record is None:
        return None
    if is_running(
        record.pid,
        expected_start=record.start_time,
        kill_fn=kill_fn,
        start_time_fn=start_time_fn,
    ):
        return record.pid
    return None


@dataclass(frozen=True)
class StopOutcome:
    """Result of :func:`stop_process`; the CLI maps it to a message + exit code (module stays mute).

    The module never prints, so all operator-facing wording lives at the call site.
    """

    found: bool  # was a PID recorded at all?
    pid: int | None
    signaled: bool  # did we send SIGTERM?
    killed: bool  # did we escalate to SIGKILL after the timeout?
    already_dead: bool  # PID file present but the process was not running (stale)


def stop_process(
    path: Path,
    *,
    timeout: float = 30.0,
    poll: float = 0.2,
    term_sig: int = signal.SIGTERM,
    kill_sig: int = getattr(signal, "SIGKILL", signal.SIGTERM),
    kill_fn: KillFn = os.kill,
    sleep_fn: SleepFn = time.sleep,
    now_fn: NowFn = time.monotonic,
    start_time_fn: StartTimeFn = _read_proc_start_time,
) -> StopOutcome:
    """Signal the daemon recorded in ``path`` to stop, escalating to SIGKILL after ``timeout``.

    Idempotent: an absent, stale, or recycled-PID file sends no signal. Otherwise send SIGTERM and
    poll liveness up to ``timeout`` seconds; if still alive, send SIGKILL. The recorded start-time
    guards every liveness probe so a recycled PID is never signaled. The PID file is removed in
    every terminal branch (the daemon removes its own file on a graceful exit, but cannot after a
    SIGKILL — so this is the backstop). Pure under test thanks to the injected seams.
    """
    record = read_pid_record(path)
    if record is None:
        return StopOutcome(found=False, pid=None, signaled=False, killed=False, already_dead=False)
    pid = record.pid
    expected = record.start_time
    if not is_running(pid, expected_start=expected, kill_fn=kill_fn, start_time_fn=start_time_fn):
        path.unlink(missing_ok=True)  # reap the stale (or recycled) file
        return StopOutcome(found=True, pid=pid, signaled=False, killed=False, already_dead=True)

    try:
        kill_fn(pid, term_sig)
    except ProcessLookupError:  # raced to exit between the probe and the signal
        path.unlink(missing_ok=True)
        return StopOutcome(found=True, pid=pid, signaled=False, killed=False, already_dead=True)

    killed = False
    deadline = now_fn() + timeout
    while is_running(pid, expected_start=expected, kill_fn=kill_fn, start_time_fn=start_time_fn):
        if now_fn() >= deadline:
            try:
                kill_fn(pid, kill_sig)
                killed = True
            except ProcessLookupError:  # exited just as we escalated
                killed = False
            break
        sleep_fn(poll)

    path.unlink(missing_ok=True)
    return StopOutcome(found=True, pid=pid, signaled=True, killed=killed, already_dead=False)


class StopController:
    """Bridge ``SIGTERM`` to a :class:`threading.Event` the watch loop polls between ticks.

    Use as a context manager so the previous signal disposition is always restored on exit — a
    leaked handler corrupts later pytest tests. ``signal.signal`` only works on the main thread, so
    enter this on the main thread (the CLI does); tests inject ``signal_fn`` to avoid the real call.
    """

    def __init__(
        self,
        *,
        signals: tuple[int, ...] = (signal.SIGTERM,),
        signal_fn: SignalFn = signal.signal,
    ) -> None:
        self.event = threading.Event()
        self._signals = signals
        self._signal_fn = signal_fn
        self._previous: dict[int, SignalHandler] = {}

    def _handle(self, signum: int, frame: FrameType | None) -> None:
        self.event.set()

    def __enter__(self) -> StopController:
        for sig in self._signals:
            self._previous[sig] = self._signal_fn(sig, self._handle)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for sig, prev in self._previous.items():
            if prev is not None:  # None = handler not installed from Python; cannot restore it
                self._signal_fn(sig, prev)
        self._previous.clear()
