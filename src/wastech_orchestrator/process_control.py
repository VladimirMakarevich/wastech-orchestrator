"""PID-file and graceful-shutdown plumbing for the ``watch`` daemon (backlog: stop/restart).

Pure and print-free by design: the CLI owns all operator output and exit codes; this module only
reads/writes the PID file, probes liveness, signals a process, and bridges ``SIGTERM`` to a
``threading.Event`` the watch loop and FlowEngine cancellation predicate observe. Every OS seam
(:func:`os.kill`,
:func:`signal.signal`, sleeping, the clock) is injectable so the whole module is unit-testable
without real processes or signals.

The handler **sets an event rather than raising**, so a ``SIGTERM`` that arrives mid-node lets that
node finish; the engine parks before the next node and the loop exits cleanly.
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
HardKillFn = Callable[[int], None]  # tree-kill by pid (Windows ``taskkill /F /T``); injected seam
# The handler/return type accepted by signal.signal (mirrors typeshed's _HANDLER).
SignalHandler = Callable[[int, FrameType | None], object] | int | signal.Handlers | None
SignalFn = Callable[[int, SignalHandler], SignalHandler]

PID_FILENAME = "orchestrator.pid"
STOP_FILENAME = "orchestrator.stop"
CHILDREN_FILENAME = "orchestrator.children"

# Kill the whole subtree of an agent recorded as (pid, pgid): killpg + a descendant sweep (POSIX) or
# ``taskkill /F /T`` (Windows). Injected as a seam so this module never shells out, exactly as
# ``hard_kill_fn`` is — the walk/taskkill live in ``providers.process``.
SubtreeKillFn = Callable[[int, int], None]  # (pid, pgid) -> None


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
# ``os.getpgrp``/``os.setpgid`` are POSIX-only (absent on Windows) — ``None`` there disables the
# daemon group-leader step. Read as module singletons so they are not function-call defaults (B008).
_DEFAULT_GETPGRP: Callable[[], int] | None = getattr(os, "getpgrp", None)
_DEFAULT_SETPGID: Callable[[int, int], None] | None = getattr(os, "setpgid", None)


@dataclass(frozen=True)
class ProcessIdentity:
    """A recorded daemon identity: its PID plus an opaque start-time token.

    ``start_time`` is a platform-specific, monotonically-unique-per-PID token used only for equality
    (Linux: ``/proc/<pid>/stat`` field 22; macOS/BSD: ``ps -o lstart=``). ``None`` when it could not
    be read — liveness then falls back to a bare PID probe (no recycling protection).
    """

    pid: int
    start_time: str | None


@dataclass(frozen=True)
class ChildHandle:
    """A recorded active-agent handle: its ``(pid, pgid)`` plus the recycling-guard start-time.

    Written by the daemon the instant it launches an agent (see the recorder wired in ``cmd_watch``)
    and cleared on the agent's return, so a hard stop — from the daemon's own shutdown or from a
    ``worc stop --force-full`` in another shell — can reap the agent's whole subtree by group + a
    descendant sweep. Carries only integers and the opaque ``start_time`` token: no argv/env/prompt,
    the same no-secrets discipline as the PID file. ``pgid`` equals ``pid`` because the agent leads
    its own session/group (``start_new_session``); it is stored explicitly so the killer never has
    to call ``os.getpgid`` on a possibly-already-exited pid.
    """

    pid: int
    pgid: int
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

    The cross-platform graceful-stop channel: ``stop`` writes this file; the watch loop polls it
    while idle and FlowEngine checks the same predicate between nodes, including on Windows where
    ``SIGTERM`` is undeliverable cross-process.
    """
    return Path(artifacts_root) / STOP_FILENAME


def stop_file_requested(path: Path) -> bool:
    """True iff the stop-sentinel file exists (the watch loop's cross-platform stop probe)."""
    return path.exists()


def _write_stop_file(path: Path) -> None:
    """Create the stop-sentinel (content is irrelevant; presence is the signal)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stop\n", encoding="utf-8")


def ensure_own_process_group(
    *,
    getpid_fn: Callable[[], int] = os.getpid,
    getpgrp_fn: Callable[[], int] | None = _DEFAULT_GETPGRP,
    setpgid_fn: Callable[[int, int], None] | None = _DEFAULT_SETPGID,
) -> bool:
    """Best-effort: make the current (daemon) process a process-group **leader** if it is not one.

    So ``stop --force-full`` can ``killpg`` the daemon's own group (daemon + agents that inherit it)
    without reaching an unrelated group. The two primary launch paths already lead their group — a
    foreground ``worc watch`` via shell job control, and a console-spawned daemon via
    ``start_new_session`` — so this only matters for a script/systemd launch that inherits the
    parent's group. Guarded: it never runs on Windows (no POSIX process groups), never re-parents a
    process that is already a leader (so foreground ``Ctrl-C`` is untouched), and swallows ``EPERM``
    (a session leader cannot ``setpgid``). It does **not** ``setsid`` — the controlling terminal is
    kept, so a foreground daemon still receives terminal signals. Returns whether we are a leader.
    """
    if getpgrp_fn is None or setpgid_fn is None:  # Windows: no POSIX process groups
        return False
    if getpgrp_fn() == getpid_fn():
        return True  # already a leader (foreground job control, or spawned with start_new_session)
    try:
        setpgid_fn(0, 0)  # become leader of a new group; keeps the controlling terminal (no setsid)
    except OSError:
        return False  # e.g. EPERM (already a session leader) — leave the group as-is
    return True


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
    tmp.replace(path)


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


def children_file_path(artifacts_root: str | os.PathLike[str]) -> Path:
    """The canonical active-agent-handle location under an artifacts root
    (``<root>/orchestrator.children``)."""
    return Path(artifacts_root) / CHILDREN_FILENAME


def write_children_file(
    path: Path,
    *,
    pid: int,
    pgid: int,
    start_time_fn: StartTimeFn = _read_proc_start_time,
) -> None:
    """Atomically record the active agent's ``(pid, pgid)`` + its start-time token to ``path``.

    Small JSON (``{"pid", "pgid", "start_time"}``), written temp-file + :func:`os.replace` so a
    concurrent ``stop`` never observes a half-write — symmetric to :func:`write_pid_file`. The
    start-time is the recycling guard (Linux ``/proc`` only; ``None`` elsewhere).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = {"pid": pid, "pgid": pgid, "start_time": start_time_fn(pid)}
    tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_children_record(path: Path) -> ChildHandle | None:
    """Return the :class:`ChildHandle` recorded in ``path``, or ``None`` if absent/malformed.

    Tolerant like :func:`read_pid_record`: a missing/corrupt file (or one written before ``pgid``
    was recorded) is "no active agent", which keeps a hard stop idempotent.
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
    pgid = data.get("pgid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
        return None
    start = data.get("start_time")
    return ChildHandle(pid=pid, pgid=pgid, start_time=start if isinstance(start, str) else None)


def clear_children_file(path: Path) -> None:
    """Remove the active-agent-handle file (idempotent); called when an agent returns/is reaped."""
    path.unlink(missing_ok=True)


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
    killed: bool  # did we hard-kill the daemon, either explicitly or after the grace timeout?
    already_dead: bool  # PID file present but the process was not running (stale; POSIX probe)
    timed_out: bool = False  # shutdown was not confirmed within ``timeout``
    group_killed: bool = False  # ``level="full"``: the daemon's process group was SIGKILLed (POSIX)
    tree_killed: bool = False  # ``level="full"`` on Windows: daemon process tree was taskkilled
    degraded_to_soft: bool = False  # ``level="full"`` on Windows with no hard_kill_fn: fell to soft


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
    hard_kill_fn: HardKillFn | None = None,
    subtree_kill_fn: SubtreeKillFn | None = None,
    children_file: Path | None = None,
    can_signal: bool | None = None,
) -> StopOutcome:
    """Ask the daemon recorded in ``path`` to stop, confirming shutdown within ``timeout``.

    Idempotent: an absent PID file is a no-op. ``level`` is the stop ladder's hardness:

    * ``"soft"`` (default) — cooperative node-boundary stop that **never hard-kills on either
      platform**. **POSIX**: write ``stop_file``, probe liveness, send ``term_sig`` (SIGTERM) for an
      immediate wakeup, and poll. **Windows**: ``os.kill`` can't reach an unrelated process, so
      write ``stop_file`` and wait for the daemon to remove its own PID file. On either platform, if
      the daemon outlives the timeout it stays a **pending graceful stop**: retain every handle and
      report ``timed_out`` so the daemon still exits at its next node boundary, a duplicate watcher
      is blocked, and a later ``--force-full`` can still target it.
    * ``"full"`` — hard stop. **POSIX**: SIGKILL the daemon's own process group (daemon + any checks
      child), **and** reap the recorded active agent's whole subtree via ``subtree_kill_fn`` (the
      agent leads its own group, so the daemon-group kill no longer reaches it — the recorded
      ``children_file`` handle is now the route). Nothing is orphaned; recovery is the next
      ``resume()``. **Windows**: there is no cross-process group kill, so if ``hard_kill_fn`` is
      supplied it tree-kills the daemon (``taskkill /F /T``, reaching the agent as a descendant) and
      the recorded agent for good measure, and sets ``tree_killed``; with no ``hard_kill_fn`` it
      **degrades to the soft path** and sets ``degraded_to_soft``.

    A recorded start-time guards every POSIX probe so a recycled PID is never signaled. Confirmed
    terminal branches remove the PID, stop, and children files; an unconfirmed soft timeout (either
    platform) deliberately retains them. Pure under test via the injectable seams.
    """
    if can_signal is None:
        can_signal = _can_signal()
    record = read_pid_record(path)
    if record is None:
        if can_signal:
            # POSIX can prove that no recorded daemon exists. Windows cannot: an older soft-stop
            # may have dropped the PID while its live watcher still owes the sentinel a shutdown.
            if stop_file is not None:
                stop_file.unlink(missing_ok=True)
            if children_file is not None:
                clear_children_file(children_file)
        return StopOutcome(found=False, pid=None, signaled=False, killed=False, already_dead=False)
    if level == "full" and can_signal:
        return _stop_via_group_kill(
            path,
            record,
            kill_sig=kill_sig,
            stop_file=stop_file,
            children_file=children_file,
            kill_fn=kill_fn,
            start_time_fn=start_time_fn,
            getpgid_fn=getpgid_fn,
            killpg_fn=killpg_fn,
            subtree_kill_fn=subtree_kill_fn,
        )
    if can_signal:
        return _stop_via_signal(
            path,
            record,
            timeout=timeout,
            poll=poll,
            term_sig=term_sig,
            stop_file=stop_file,
            kill_fn=kill_fn,
            sleep_fn=sleep_fn,
            now_fn=now_fn,
            start_time_fn=start_time_fn,
        )
    # Windows: no cross-process signal. Only a "full" request hard-kills, via the injected tree-kill
    # seam if one is supplied; a soft request (or a "full" with no seam) does the cooperative
    # PID-file wait, which stays pending on timeout.
    if level == "full" and hard_kill_fn is not None:
        return _stop_via_tree_kill(
            path,
            record,
            stop_file=stop_file,
            children_file=children_file,
            hard_kill_fn=hard_kill_fn,
        )
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
    children_file: Path | None,
    kill_fn: KillFn,
    start_time_fn: StartTimeFn,
    getpgid_fn: GetpgidFn,
    killpg_fn: KillpgFn,
    subtree_kill_fn: SubtreeKillFn | None,
) -> StopOutcome:
    """POSIX hard stop: SIGKILL the daemon's group **and** reap the recorded agent's subtree.

    Ordering matters against a fallback-respawn race: (1) write the stop sentinel first, so if the
    daemon is momentarily still inside its Router loop it reads "cancelled" and refuses to launch a
    fallback agent; (2) SIGKILL the **daemon's own** group first (daemon + any checks child), so a
    surviving daemon cannot spawn anything; (3) reap the **agent's** own subtree via the injected
    ``subtree_kill_fn`` — the agent leads its own group now, so the daemon-group kill does not reach
    it. The agent reap is best-effort even when the daemon is already dead (a crashed daemon may
    have orphaned the agent — the exact incident this fixes). The recorded start-time guards
    recycling so
    an unrelated group is never destroyed; a "no such process" race reads as already-dead.
    """
    pid = record.pid
    if stop_file is not None:
        _write_stop_file(stop_file)  # cancellation marker first (see docstring / the Router seam)
    handle = read_children_record(children_file) if children_file is not None else None

    already_dead = False
    group_killed = False
    if not is_running(
        pid, expected_start=record.start_time, kill_fn=kill_fn, start_time_fn=start_time_fn
    ):
        already_dead = True  # stale (or recycled) PID file
    else:
        try:
            killpg_fn(getpgid_fn(pid), kill_sig)
            group_killed = True
        except OSError as exc:  # raced to exit between probe and kill (ESRCH from getpgid/killpg)
            if not _is_no_such_process(exc):
                raise
            already_dead = True

    # Reap the agent's own subtree (its group + a descendant sweep for anything that broke away),
    # best-effort even when the daemon is already dead — a crashed daemon may have orphaned it.
    if handle is not None and subtree_kill_fn is not None:
        subtree_kill_fn(handle.pid, handle.pgid)
    path.unlink(missing_ok=True)
    if stop_file is not None:
        stop_file.unlink(missing_ok=True)
    if children_file is not None:
        clear_children_file(children_file)
    return StopOutcome(
        found=True,
        pid=pid,
        signaled=not already_dead,
        killed=group_killed,
        already_dead=already_dead,
        group_killed=group_killed,
    )


def _stop_via_tree_kill(
    path: Path,
    record: ProcessIdentity,
    *,
    stop_file: Path | None,
    children_file: Path | None,
    hard_kill_fn: HardKillFn,
) -> StopOutcome:
    """Windows hard stop: tree-kill the daemon (and its agent/checks descendants) via the seam.

    No liveness probe — ``os.kill`` cannot reach an unrelated process on Windows and there is no
    start-time recycling guard off Linux, so we call the injected killer on the recorded PID (it
    swallows a "no such process" exit). ``taskkill /F /T`` on the daemon already reaches the agent
    as a descendant (no ``setsid`` on Windows), but we also tree-kill the recorded agent for
    robustness against a process that re-parented away, then reap the PID/stop/children files.
    Recovery is the next ``resume()``.
    """
    pid = record.pid
    hard_kill_fn(pid)
    handle = read_children_record(children_file) if children_file is not None else None
    if handle is not None:
        hard_kill_fn(handle.pid)
    path.unlink(missing_ok=True)
    if stop_file is not None:
        stop_file.unlink(missing_ok=True)
    if children_file is not None:
        clear_children_file(children_file)
    return StopOutcome(
        found=True,
        pid=pid,
        signaled=True,
        killed=True,
        already_dead=False,
        tree_killed=True,
    )


def _stop_via_signal(
    path: Path,
    record: ProcessIdentity,
    *,
    timeout: float,
    poll: float,
    term_sig: int,
    stop_file: Path | None,
    kill_fn: KillFn,
    sleep_fn: SleepFn,
    now_fn: NowFn,
    start_time_fn: StartTimeFn,
) -> StopOutcome:
    """POSIX soft stop: probe liveness, SIGTERM (+ stop-file), poll for a cooperative exit.

    On timeout this stays a **pending graceful stop**: it never kills and keeps every handle
    (PID file, stop sentinel) intact, so the daemon still sees the request and exits at its next
    node boundary. ``--force-full`` owns all hard-stop semantics.
    """
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

    timed_out = False
    deadline = now_fn() + timeout
    while is_running(pid, expected_start=expected, kill_fn=kill_fn, start_time_fn=start_time_fn):
        if now_fn() >= deadline:
            timed_out = True
            break
        sleep_fn(poll)

    if timed_out:
        # Pending graceful stop: keep the PID file and stop sentinel (and thus the recorded child
        # handle) intact so the daemon still sees the request and exits at its next node boundary.
        # --force-full remains the only immediate interrupt.
        return StopOutcome(
            found=True, pid=pid, signaled=True, killed=False, already_dead=False, timed_out=True
        )

    # Cooperative exit confirmed: reap the PID file and stop sentinel.
    path.unlink(missing_ok=True)
    if stop_file is not None:
        stop_file.unlink(missing_ok=True)
    return StopOutcome(found=True, pid=pid, signaled=True, killed=False, already_dead=False)


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
    """Windows soft stop: request via the stop-file, then wait for the daemon to remove its own PID.

    No ``os.kill`` — it cannot reach an unrelated process. The daemon removes its own PID file on
    a clean exit, so the file's disappearance confirms shutdown. If it persists past the timeout the
    graceful stop stays **pending**: never hard-kill, keep the PID, stop sentinel, and active-child
    handle intact so the daemon still exits at its next node boundary; this honestly blocks a
    duplicate watcher and leaves a later ``--force-full`` (the only hard rung) able to tree-kill the
    original process.
    """
    pid = record.pid
    if stop_file is not None:
        _write_stop_file(stop_file)
    deadline = now_fn() + timeout
    while path.exists():
        if now_fn() >= deadline:
            return StopOutcome(
                found=True, pid=pid, signaled=True, killed=False, already_dead=False, timed_out=True
            )
        sleep_fn(poll)

    # The PID file is gone: the daemon noticed the stop-file, exited, and reaped it.
    if stop_file is not None:
        stop_file.unlink(missing_ok=True)
    return StopOutcome(found=True, pid=pid, signaled=True, killed=False, already_dead=False)


class StopController:
    """Bridge ``SIGTERM`` to the event used by the watch loop and FlowEngine cancellation seam.

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
