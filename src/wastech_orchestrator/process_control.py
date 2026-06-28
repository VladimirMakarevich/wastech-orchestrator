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
from dataclasses import dataclass, replace
from pathlib import Path
from types import FrameType, TracebackType

# Injectable OS seams (defaults are the real calls; tests pass fakes).
KillFn = Callable[[int, int], None]  # os.kill(pid, sig)
SleepFn = Callable[[float], None]  # time.sleep
NowFn = Callable[[], float]  # time.monotonic
StartTimeFn = Callable[[int], str | None]  # opaque per-pid start-time token (recycling guard)
GetpgidFn = Callable[[int], int]  # os.getpgid(pid) -> the process group id (POSIX)
KillpgFn = Callable[[int, int], None]  # os.killpg(pgid, sig) (POSIX)
# The handler/return type accepted by signal.signal (mirrors typeshed's _HANDLER).
SignalHandler = Callable[[int, FrameType | None], object] | int | signal.Handlers | None
SignalFn = Callable[[int, SignalHandler], SignalHandler]

PID_FILENAME = "orchestrator.pid"
STOP_FILENAME = "orchestrator.stop"


def _can_signal() -> bool:
    """Whether this platform can probe/signal a process by PID that the caller does not own.

    POSIX: yes — ``os.kill`` (including the signal-0 liveness probe) works on any same-user process,
    so ``stop`` can SIGTERM/SIGKILL the daemon directly. Windows: no — ``os.kill`` opens the target
    with ``OpenProcess(PROCESS_ALL_ACCESS)``, which fails for a process the caller holds no handle
    to, and ``stop`` is a separate process from the daemon. There, liveness and shutdown ride on the
    PID file the daemon self-manages (written on start, removed on a clean exit) plus the stop-file
    the watch loop polls — never ``os.kill``.
    """
    return os.name != "nt"


def _unavailable_killpg(pgid: int, sig: int) -> None:  # pragma: no cover - Windows-only guard
    raise OSError("os.killpg is unavailable on this platform")


# Defaults resolved at import time: ``os.getpgid``/``os.killpg`` do not exist on Windows, so a bare
# ``os.killpg`` default would raise at import there. The full (group-kill) path is gated on
# ``_can_signal()`` (POSIX), so these fallbacks are never actually invoked on Windows.
_DEFAULT_GETPGID: GetpgidFn = getattr(os, "getpgid", lambda pid: pid)
_DEFAULT_KILLPG: KillpgFn = getattr(os, "killpg", _unavailable_killpg)


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


def stop_file_path(artifacts_root: str | os.PathLike[str]) -> Path:
    """The canonical stop-sentinel location under an artifacts root (``<root>/orchestrator.stop``).

    The cross-platform graceful-stop channel: ``stop`` writes this file and the watch loop polls it
    between ticks, so a daemon shuts down cleanly even where ``SIGTERM`` is undeliverable (Windows).
    """
    return Path(artifacts_root) / STOP_FILENAME


def stop_file_requested(path: Path) -> bool:
    """True iff the stop-sentinel file exists (the watch loop's cross-platform stop probe)."""
    return path.exists()


def _write_stop_file(path: Path) -> None:
    """Create the stop-sentinel (content is irrelevant; presence is the signal)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stop\n", encoding="utf-8")


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


def _is_no_such_process(exc: OSError) -> bool:
    """True when an ``os.kill`` error means the target PID does not exist.

    POSIX raises ``ProcessLookupError`` (ESRCH). Windows has no ESRCH: ``os.kill`` on a missing PID
    raises a bare ``OSError`` whose ``winerror`` is 87 (``ERROR_INVALID_PARAMETER``) — the
    ``OpenProcess`` failure for an unknown PID. Both mean the process is gone.
    """
    return isinstance(exc, ProcessLookupError) or getattr(exc, "winerror", None) == 87


def is_running(
    pid: int,
    *,
    expected_start: str | None = None,
    kill_fn: KillFn = os.kill,
    start_time_fn: StartTimeFn = _read_proc_start_time,
) -> bool:
    """True iff a process with ``pid`` exists, is signalable by us, and is not a recycled PID.

    A "no such process" error → not running (POSIX ``ProcessLookupError``; Windows ``OSError``
    winerror 87 — see :func:`_is_no_such_process`). ``PermissionError`` → running but owned by
    another user (treated as alive). A live start-time that differs from ``expected_start`` means
    the PID was recycled → ``False``. No start-time available → the bare PID probe.
    """
    try:
        kill_fn(pid, 0)
    except PermissionError:
        return True
    except OSError as exc:
        if _is_no_such_process(exc):
            return False
        raise
    if expected_start is None:
        return True
    actual_start = start_time_fn(pid)
    if actual_start is None:
        return True  # cannot determine the live process's start-time → trust the bare probe
    return actual_start == expected_start


def running_daemon_pid(
    path: Path,
    *,
    kill_fn: KillFn = os.kill,
    start_time_fn: StartTimeFn = _read_proc_start_time,
    can_signal: bool | None = None,
) -> int | None:
    """The PID of the running daemon recorded in ``path``, or ``None``.

    Where the platform can signal an unrelated PID (POSIX; see :func:`_can_signal`), this combines
    :func:`read_pid_record` with :func:`is_running`'s start-time recycling guard: a recorded PID
    that is dead — or alive but recycled — reads as ``None``. Where it cannot (Windows), liveness
    cannot be probed, so PID-file *presence* is the signal: the daemon writes the file on start and
    removes it on a clean exit, so a present file means "a watcher is running". A stale file left by
    a crash reads as running until cleared (by ``stop`` or by hand) — the documented Windows
    trade-off.
    """
    if can_signal is None:
        can_signal = _can_signal()
    record = read_pid_record(path)
    if record is None:
        return None
    if not can_signal:
        return record.pid  # Windows: cannot probe; presence of the self-managed PID file = running
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
    signaled: bool  # was a graceful stop requested (POSIX signal and/or the stop-file)?
    killed: bool  # did we escalate to a hard kill after the timeout (POSIX only)?
    already_dead: bool  # PID file present but the process was not running (stale; POSIX probe)
    timed_out: bool = False  # shutdown was not confirmed within ``timeout``
    group_killed: bool = False  # ``level="full"``: the daemon's process group was SIGKILLed (POSIX)
    degraded_to_soft: bool = False  # ``level="full"`` on Windows fell back to the soft stop


def stop_process(
    path: Path,
    *,
    timeout: float = 30.0,
    poll: float = 0.2,
    term_sig: int = signal.SIGTERM,
    kill_sig: int = getattr(signal, "SIGKILL", signal.SIGTERM),
    stop_file: Path | None = None,
    level: str = "soft",
    kill_fn: KillFn = os.kill,
    sleep_fn: SleepFn = time.sleep,
    now_fn: NowFn = time.monotonic,
    start_time_fn: StartTimeFn = _read_proc_start_time,
    getpgid_fn: GetpgidFn = _DEFAULT_GETPGID,
    killpg_fn: KillpgFn = _DEFAULT_KILLPG,
    can_signal: bool | None = None,
) -> StopOutcome:
    """Ask the daemon recorded in ``path`` to stop, confirming shutdown within ``timeout``.

    Idempotent: an absent PID file is a no-op. ``level`` is the stop ladder's hardness:

    * ``"soft"`` (default) — graceful, between-ticks stop. **POSIX**: write ``stop_file``, probe
      liveness, send ``term_sig`` (SIGTERM) for an immediate wakeup, poll, and escalate to
      ``kill_sig`` (SIGKILL) only if the daemon outlives the timeout. **Windows**: ``os.kill`` can't
      reach an unrelated process, so write ``stop_file`` and wait for the daemon to remove its own
      PID file; if it does not vanish within the timeout, clear it and report ``timed_out``.
    * ``"full"`` — hard stop. **POSIX only**: SIGKILL the daemon's whole process group at once
      (daemon + active agent + any checks child — they share a group because the agent launches
      with ``start_new_session=True``), so nothing is orphaned; recovery is the next ``resume()``.
      On **Windows** there is no cross-process group kill, so it **degrades to the soft path** and
      sets ``degraded_to_soft`` (the CLI surfaces "use Task Manager / taskkill").

    A recorded start-time guards every probe so a recycled PID is never signaled. The PID file and
    stop-file are removed in every terminal branch. Pure under test via the injectable seams.
    """
    if can_signal is None:
        can_signal = _can_signal()
    record = read_pid_record(path)
    if record is None:
        if stop_file is not None:
            stop_file.unlink(missing_ok=True)  # reap a stray sentinel; nothing is recorded
        return StopOutcome(found=False, pid=None, signaled=False, killed=False, already_dead=False)
    if level == "full" and can_signal:
        return _stop_via_group_kill(
            path,
            record,
            kill_sig=kill_sig,
            stop_file=stop_file,
            kill_fn=kill_fn,
            start_time_fn=start_time_fn,
            getpgid_fn=getpgid_fn,
            killpg_fn=killpg_fn,
        )
    if can_signal:
        return _stop_via_signal(
            path,
            record,
            timeout=timeout,
            poll=poll,
            term_sig=term_sig,
            kill_sig=kill_sig,
            stop_file=stop_file,
            kill_fn=kill_fn,
            sleep_fn=sleep_fn,
            now_fn=now_fn,
            start_time_fn=start_time_fn,
        )
    # Windows: no cross-process signal. A "full" request degrades to the soft PID-file wait.
    outcome = _stop_via_pid_file(
        path,
        record,
        timeout=timeout,
        poll=poll,
        stop_file=stop_file,
        sleep_fn=sleep_fn,
        now_fn=now_fn,
    )
    return replace(outcome, degraded_to_soft=True) if level == "full" else outcome


def _stop_via_group_kill(
    path: Path,
    record: ProcessIdentity,
    *,
    kill_sig: int,
    stop_file: Path | None,
    kill_fn: KillFn,
    start_time_fn: StartTimeFn,
    getpgid_fn: GetpgidFn,
    killpg_fn: KillpgFn,
) -> StopOutcome:
    """POSIX hard stop: SIGKILL the daemon's whole process group at once (no graceful wait).

    Guards recycling with the recorded start-time before killing, so a recycled PID's unrelated
    group is never destroyed. A "no such process" race (daemon exited first) reads as already-dead.
    """
    pid = record.pid
    if not is_running(
        pid, expected_start=record.start_time, kill_fn=kill_fn, start_time_fn=start_time_fn
    ):
        path.unlink(missing_ok=True)  # reap the stale (or recycled) file
        if stop_file is not None:
            stop_file.unlink(missing_ok=True)
        return StopOutcome(found=True, pid=pid, signaled=False, killed=False, already_dead=True)
    try:
        killpg_fn(getpgid_fn(pid), kill_sig)
    except OSError as exc:  # raced to exit between probe and kill (ESRCH from getpgid/killpg)
        if not _is_no_such_process(exc):
            raise
        path.unlink(missing_ok=True)
        if stop_file is not None:
            stop_file.unlink(missing_ok=True)
        return StopOutcome(found=True, pid=pid, signaled=False, killed=False, already_dead=True)
    path.unlink(missing_ok=True)
    if stop_file is not None:
        stop_file.unlink(missing_ok=True)
    return StopOutcome(
        found=True,
        pid=pid,
        signaled=True,
        killed=True,
        already_dead=False,
        group_killed=True,
    )


def _stop_via_signal(
    path: Path,
    record: ProcessIdentity,
    *,
    timeout: float,
    poll: float,
    term_sig: int,
    kill_sig: int,
    stop_file: Path | None,
    kill_fn: KillFn,
    sleep_fn: SleepFn,
    now_fn: NowFn,
    start_time_fn: StartTimeFn,
) -> StopOutcome:
    """POSIX stop: probe liveness, SIGTERM (+ stop-file), poll, escalate to SIGKILL on timeout."""
    pid = record.pid
    expected = record.start_time
    if not is_running(pid, expected_start=expected, kill_fn=kill_fn, start_time_fn=start_time_fn):
        path.unlink(missing_ok=True)  # reap the stale (or recycled) file
        if stop_file is not None:
            stop_file.unlink(missing_ok=True)
        return StopOutcome(found=True, pid=pid, signaled=False, killed=False, already_dead=True)

    if stop_file is not None:
        _write_stop_file(stop_file)  # cross-platform fallback; harmless alongside the signal
    try:
        kill_fn(pid, term_sig)
    except OSError as exc:  # raced to exit between the probe and the signal
        if not _is_no_such_process(exc):
            raise
        path.unlink(missing_ok=True)
        if stop_file is not None:
            stop_file.unlink(missing_ok=True)
        return StopOutcome(found=True, pid=pid, signaled=False, killed=False, already_dead=True)

    killed = False
    timed_out = False
    deadline = now_fn() + timeout
    while is_running(pid, expected_start=expected, kill_fn=kill_fn, start_time_fn=start_time_fn):
        if now_fn() >= deadline:
            timed_out = True
            try:
                kill_fn(pid, kill_sig)
                killed = True
            except OSError as exc:  # exited just as we escalated
                if not _is_no_such_process(exc):
                    raise
                killed = False
            break
        sleep_fn(poll)

    path.unlink(missing_ok=True)
    if stop_file is not None:
        stop_file.unlink(missing_ok=True)
    return StopOutcome(
        found=True, pid=pid, signaled=True, killed=killed, already_dead=False, timed_out=timed_out
    )


def _stop_via_pid_file(
    path: Path,
    record: ProcessIdentity,
    *,
    timeout: float,
    poll: float,
    stop_file: Path | None,
    sleep_fn: SleepFn,
    now_fn: NowFn,
) -> StopOutcome:
    """Windows stop: request via the stop-file, then wait for the daemon to remove its own PID file.

    No ``os.kill`` — it cannot reach an unrelated process. The daemon removes its own PID file on
    a clean exit, so the file's disappearance confirms shutdown. If it persists past the timeout
    (wedged, or a stale file from a crash), clear it and report ``timed_out``.
    """
    pid = record.pid
    if stop_file is not None:
        _write_stop_file(stop_file)
    deadline = now_fn() + timeout
    while path.exists():
        if now_fn() >= deadline:
            path.unlink(missing_ok=True)
            if stop_file is not None:
                stop_file.unlink(missing_ok=True)
            return StopOutcome(
                found=True, pid=pid, signaled=True, killed=False, already_dead=False, timed_out=True
            )
        sleep_fn(poll)

    # The PID file is gone: the daemon noticed the stop-file, exited, and reaped it.
    if stop_file is not None:
        stop_file.unlink(missing_ok=True)
    return StopOutcome(found=True, pid=pid, signaled=True, killed=False, already_dead=False)


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
