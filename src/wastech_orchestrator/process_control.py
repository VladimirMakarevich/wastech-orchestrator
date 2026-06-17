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

import os
import signal
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
# The handler/return type accepted by signal.signal (mirrors typeshed's _HANDLER).
SignalHandler = Callable[[int, FrameType | None], object] | int | signal.Handlers | None
SignalFn = Callable[[int, SignalHandler], SignalHandler]

PID_FILENAME = "orchestrator.pid"


def pid_file_path(artifacts_root: str | os.PathLike[str]) -> Path:
    """The canonical PID-file location under an artifacts root (``<root>/orchestrator.pid``)."""
    return Path(artifacts_root) / PID_FILENAME


def write_pid_file(path: Path, *, pid: int | None = None) -> None:
    """Atomically write the current (or given) PID to ``path``, creating parent dirs as needed.

    Atomic via a temp file in the same directory + :func:`os.replace`, mirroring the installer's
    config writer, so a concurrent ``stop`` never observes a half-written PID file.
    """
    pid = os.getpid() if pid is None else pid
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(f"{pid}\n", encoding="utf-8")
    os.replace(tmp, path)


def read_pid(path: Path) -> int | None:
    """Return the PID recorded in ``path``, or ``None`` if it is absent, empty, or malformed.

    Tolerant on purpose: a corrupt or missing PID file is treated as "no daemon recorded", which is
    what keeps ``stop`` idempotent.
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    try:
        pid = int(text)
    except ValueError:
        return None
    return pid if pid > 0 else None


def is_running(pid: int, *, kill_fn: KillFn = os.kill) -> bool:
    """True iff a process with ``pid`` exists and is signalable by us (probe via signal ``0``).

    ``ProcessLookupError`` (ESRCH) → not running; ``PermissionError`` (EPERM) → running but owned by
    another user. Cannot distinguish our daemon from a recycled PID — see the staleness note in the
    module's operator docs.
    """
    try:
        kill_fn(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
) -> StopOutcome:
    """Signal the daemon recorded in ``path`` to stop, escalating to SIGKILL after ``timeout``.

    Idempotent: an absent or stale PID file sends no signal. Otherwise send SIGTERM and poll
    liveness up to ``timeout`` seconds; if still alive, send SIGKILL. The PID file is removed in
    every terminal branch (the daemon removes its own file on a graceful exit, but cannot after a
    SIGKILL — so this is the backstop). Pure under test thanks to the injected seams.
    """
    pid = read_pid(path)
    if pid is None:
        return StopOutcome(found=False, pid=None, signaled=False, killed=False, already_dead=False)
    if not is_running(pid, kill_fn=kill_fn):
        path.unlink(missing_ok=True)  # reap the stale file
        return StopOutcome(found=True, pid=pid, signaled=False, killed=False, already_dead=True)

    try:
        kill_fn(pid, term_sig)
    except ProcessLookupError:  # raced to exit between the probe and the signal
        path.unlink(missing_ok=True)
        return StopOutcome(found=True, pid=pid, signaled=False, killed=False, already_dead=True)

    killed = False
    deadline = now_fn() + timeout
    while is_running(pid, kill_fn=kill_fn):
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
