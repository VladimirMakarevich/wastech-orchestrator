"""Safe process runner (.agents/rules/coding-style.md).

The single chokepoint for launching any external CLI. Every provider subprocess goes through
``run_process``. The runner is deliberately provider-agnostic: it knows nothing about Codex/Claude
syntax or :class:`~wastech_orchestrator.providers.base.ErrorClass`. It only launches an argv list
safely and reports a raw result for an adapter to normalize.

Invariants enforced here:

* launch via an **argv list** — never a string, never ``shell=True``, never user strings
  interpolated into the command;
* a **mandatory** timeout;
* the child receives exactly the ``env`` mapping passed in (the allowlisted env, see
  :mod:`wastech_orchestrator.security.env`) — never the parent's full environment;
* the prompt is fed on **stdin** (``stdin_text``), keeping argv free of task content; with no
  ``stdin_text`` the child's stdin is ``DEVNULL`` (parent stdin is never inherited);
* stdout is streamed to ``stdout_path``; stderr is captured (it is small and secret-prone, so the
  adapter redacts it before it ever touches an artifact).
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


def _unavailable_killpg(pgid: int, sig: int) -> None:  # pragma: no cover - Windows-only guard
    raise OSError("os.killpg is unavailable on this platform")


# ``os.killpg``/``signal.SIGKILL`` are POSIX-only; resolve via getattr so a Windows ``mypy``/import
# does not choke on the missing attribute (the reap path that uses them is guarded by
# ``os.name != "nt"``). Mirrors ``process_control._DEFAULT_KILLPG``.
_KILLPG: Callable[[int, int], None] = getattr(os, "killpg", _unavailable_killpg)
_SIGKILL: int = getattr(signal, "SIGKILL", signal.SIGTERM)

# Bounded quiescence-proof budget (WRI-012): after terminating the containment, prove that no
# member remains within this wall-clock, re-killing and re-probing this often. Kept as module
# constants (not config) — a safety barrier is not an operator-tunable knob, and tests drive their
# own timings through the containment seams.
_QUIESCENCE_TIMEOUT_SECONDS = 5.0
_QUIESCENCE_POLL_SECONDS = 0.1
# How often the POSIX tracker snapshots the descendant set while the root runs. A detached child is
# still parent-linked until its parent exits, so a snapshot taken *during* the run captures it
# before it can reparent to init and become unattributable by a parent-PID walk after root exit.
_TRACKER_POLL_SECONDS = 2.0


@dataclass(frozen=True)
class QuiescenceResult:
    """Outcome of the WRI-012 process-tree quiescence barrier for one attempt.

    ``proven`` is ``True`` only when the containment was terminated and demonstrated **empty**
    within the bounded budget — no group member and no tracked/adopted descendant remains. When it
    is ``False`` an unknown process may still be running (and writing the repo/exchange), so the
    provider result must not be trusted. ``detail`` is a **secret-free** diagnostic (platform, a
    member count, and pid integers only — never argv/env/prompt), and ``survivors`` records the pids
    still alive for the audit trail. Guards against PID reuse only within the short proof window are
    best-effort (see :class:`PosixProcessContainment`); a longer-lived guard is deferred hardening.
    """

    proven: bool
    detail: str
    survivors: tuple[int, ...] = ()


@dataclass(frozen=True)
class ProcessResult:
    """Raw outcome of a single subprocess launch, before any provider-specific normalization."""

    exit_code: int | None  # None when the process timed out or never launched
    timed_out: bool
    launch_error: str | None  # set (secret-free) when the binary could not be launched at all
    duration_seconds: float
    stdout_path: str
    stderr_text: str  # captured stderr, NOT yet redacted — the caller redacts before writing it
    # WRI-012: the containment quiescence proof for this attempt. ``None`` when no process launched
    # (nothing to contain). ``proven=False`` is a fail-closed security condition — the adapter turns
    # it into a non-fallback ``CONTAINMENT_UNVERIFIED`` before any output is parsed or trusted.
    quiescence: QuiescenceResult | None = None


@dataclass(frozen=True)
class AgentHandleRecorder:
    """Callbacks that let :func:`run_process` record the agent it just launched, so a hard stop can
    reap the whole subtree even while the daemon is blocked waiting on it.

    ``on_spawn(pid, pgid)`` fires the instant the child launches; ``on_reap()`` fires when it is
    reaped (normal exit, post-timeout kill, or a propagating interrupt). The daemon (``cmd_watch``)
    wires these to the ``process_control`` children-file writers; every other caller passes ``None``
    (no recording), so one-shot CLI runs and the preflight/probe launches are unchanged.
    """

    on_spawn: Callable[[int, int], None]
    on_reap: Callable[[], None]


class ProcessContainment(Protocol):
    """A platform process-containment object whose lifetime the orchestrator owns (WRI-012).

    Every provider attempt runs inside one. It hides the platform primitive (a POSIX process group
    plus descendant tracking, or a Windows Job Object) so the process runner — and, above it, the
    provider adapters and Core — stay free of platform-specific process syntax. The contract:

    * :meth:`popen_kwargs` — launch kwargs merged into the child's :class:`subprocess.Popen` so it
      is placed in the containment at creation time;
    * :meth:`adopt` — called immediately after the child launches, to record it and begin any
      during-run tracking;
    * :meth:`terminate` — best-effort, non-proving kill of the whole containment (used to unblock a
      drain after a timeout/interrupt); idempotent;
    * :meth:`terminate_and_prove` — terminate, then **prove empty** within a bounded budget; run on
      every exit path before the result is trusted.
    """

    def popen_kwargs(self) -> dict[str, Any]:
        """Extra :class:`subprocess.Popen` kwargs that place the child into this containment."""
        ...

    def adopt(self, proc: subprocess.Popen[Any]) -> None:
        """Record the just-launched child and start any during-run tracking."""
        ...

    def terminate(self) -> None:
        """Best-effort, non-proving kill of the whole containment (idempotent)."""
        ...

    def terminate_and_prove(self) -> QuiescenceResult:
        """Terminate the containment and prove it empty within the bounded budget."""
        ...


class PosixProcessContainment:
    """POSIX containment: a process group (``start_new_session``) plus during-run descendant
    tracking, with a bounded emptiness proof (WRI-012).

    The child leads its own session/group, so its whole in-group subtree is reaped by one
    ``killpg``. A descendant that breaks away into a **new** session (``setsid``) leaves that group,
    and once its parent exits it reparents to init — after which a parent-PID walk from the root can
    no longer see it. To keep tracking valid across that reparenting, a background thread snapshots
    the descendant set every :data:`_TRACKER_POLL_SECONDS` **while the root is alive** (when a
    ``setsid`` child is still parent-linked) and accumulates every pid it ever sees; termination
    then SIGKILLs the group **and** every tracked pid individually, and the proof re-probes until
    the group is empty and no tracked pid remains (or the budget expires → not proven).

    PID-reuse guard: the proof only trusts a tracked pid within the short (~5s) quiescence window,
    where a SIGKILLed pid being recycled by an unrelated process is negligible; a start-time-based
    guard is deferred hardening. The residual — a ``setsid`` child that spawns *and* reparents
    entirely within one poll gap — is closable only by a kernel container (cgroup v2 / PID
    namespace), also deferred.
    """

    def __init__(
        self,
        *,
        killpg_fn: Callable[[int, int], None] = _KILLPG,
        kill_fn: Callable[[int, int], None] = os.kill,
        snapshot_fn: Callable[[int], list[int]] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        quiescence_timeout: float = _QUIESCENCE_TIMEOUT_SECONDS,
        poll: float = _QUIESCENCE_POLL_SECONDS,
        tracker_poll: float = _TRACKER_POLL_SECONDS,
    ) -> None:
        self._killpg = killpg_fn
        self._kill = kill_fn
        self._snapshot = snapshot_fn if snapshot_fn is not None else _posix_descendants
        self._sleep = sleep_fn
        self._monotonic = monotonic
        self._quiescence_timeout = quiescence_timeout
        self._poll = poll
        self._tracker_poll = tracker_poll
        self._pid: int | None = None
        self._pgid: int | None = None
        self._tracked: set[int] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def popen_kwargs(self) -> dict[str, Any]:
        # Lead a new session/group so the whole subtree is reachable by one killpg.
        return {"start_new_session": True}

    def adopt(self, proc: subprocess.Popen[Any]) -> None:
        # The child leads its own group, so pgid == pid; record it directly rather than calling
        # os.getpgid (which would race an instant-exit child to ESRCH).
        self._pid = proc.pid
        self._pgid = proc.pid
        self._thread = threading.Thread(
            target=self._track, name="worc-containment-tracker", daemon=True
        )
        self._thread.start()

    def _track(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                if self._pid is not None:
                    self._tracked.update(self._snapshot(self._pid))
            self._stop.wait(self._tracker_poll)

    def _stop_tracker(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._quiescence_timeout)
            self._thread = None

    def terminate(self) -> None:
        if self._pid is None:
            return
        self._stop_tracker()
        with contextlib.suppress(Exception):  # one last snapshot before the group dies
            self._tracked.update(self._snapshot(self._pid))
        self._kill_all()

    def _kill_all(self) -> None:
        if self._pgid is not None:
            with contextlib.suppress(ProcessLookupError, OSError):
                self._killpg(self._pgid, _SIGKILL)
        for pid in sorted(self._tracked):
            with contextlib.suppress(ProcessLookupError, OSError):
                self._kill(pid, _SIGKILL)

    def terminate_and_prove(self) -> QuiescenceResult:
        if self._pid is None:
            return QuiescenceResult(proven=True, detail="no process launched")
        self.terminate()  # idempotent kill (also stops the tracker)
        deadline = self._monotonic() + self._quiescence_timeout
        while True:
            survivors = self._alive_tracked()
            group_busy = self._group_nonempty()
            if not survivors and not group_busy:
                return QuiescenceResult(
                    proven=True, detail=f"posix: process group {self._pgid} empty, no survivors"
                )
            if self._monotonic() >= deadline:
                marker = "group non-empty; " if group_busy else ""
                ordered = tuple(sorted(survivors))
                pgid, secs = self._pgid, self._quiescence_timeout
                detail = (
                    f"posix: {marker}{len(ordered)} process(es) still alive after "
                    f"{secs:.0f}s; pgid={pgid}; survivors={list(ordered)}"
                )
                return QuiescenceResult(proven=False, detail=detail, survivors=ordered)
            self._kill_all()  # re-kill; a zombie awaiting its reaper clears within the budget
            self._sleep(self._poll)

    def _group_nonempty(self) -> bool:
        """True iff the process group still has a signalable member (``killpg(pgid, 0)``)."""
        if self._pgid is None:
            return False
        try:
            self._killpg(self._pgid, 0)  # signal 0 = probe, kill nothing
        except ProcessLookupError:
            return False  # ESRCH → the group is empty
        except OSError:
            return True  # e.g. EPERM (a member owned by another user) → conservatively non-empty
        return True

    def _alive_tracked(self) -> set[int]:
        """The tracked/adopted descendant pids (plus any fresh ones) that are still alive."""
        candidates = set(self._tracked)
        if self._pid is not None:
            with contextlib.suppress(Exception):
                candidates.update(self._snapshot(self._pid))
        alive: set[int] = set()
        for pid in candidates:
            try:
                self._kill(pid, 0)  # liveness probe
            except ProcessLookupError:
                continue  # ESRCH → gone (or reaped zombie)
            except OSError:
                pass  # EPERM etc → treat as alive (conservative)
            alive.add(pid)
        return alive


# --- Windows Job Object containment (WRI-012) -----------------------------------------------------
# ctypes struct/constant definitions for the Job Object primitive. Defined unconditionally (they are
# pure type declarations that import cleanly on every platform); the actual ``kernel32`` calls in
# ``_RealWin32`` run only under ``os.name == "nt"``.

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JobObjectExtendedLimitInformation = 9
_JobObjectBasicProcessIdList = 3
_MAX_JOB_PIDS = 256  # count buffer; NumberOfAssignedProcesses reports the true total even if larger


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    )


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = (
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    )


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


class _JOBOBJECT_BASIC_PROCESS_ID_LIST(ctypes.Structure):
    _fields_ = (
        ("NumberOfAssignedProcesses", ctypes.c_uint32),
        ("NumberOfProcessIdsInList", ctypes.c_uint32),
        ("ProcessIdList", ctypes.c_size_t * _MAX_JOB_PIDS),
    )


class Win32JobApi(Protocol):
    """The narrow Win32 Job Object surface the Windows containment needs (an injectable seam).

    Real calls live in :class:`_RealWin32` (``kernel32`` via ctypes, Windows only); tests inject a
    fake so the call sequence is verifiable off-Windows, exactly as the ``taskkill`` seam is tested.
    """

    def create_job(self) -> int: ...
    def set_kill_on_close(self, job: int) -> None: ...
    def assign(self, job: int, process_handle: int) -> None: ...
    def terminate(self, job: int) -> None: ...
    def process_count(self, job: int) -> int: ...
    def close(self, job: int) -> None: ...


class _RealWin32:  # pragma: no cover - exercised only on native Windows (WRI-006 gate)
    """The real ``kernel32`` Job Object calls. Instantiated only when ``os.name == "nt"``."""

    def __init__(self) -> None:
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

    def _check(self, ok: object, call: str) -> None:
        if not ok:
            errno = ctypes.get_last_error()  # type: ignore[attr-defined]  # Windows-only in typeshed
            raise OSError(errno, f"{call} failed")

    def create_job(self) -> int:
        handle = self._k32.CreateJobObjectW(None, None)
        self._check(handle, "CreateJobObjectW")
        return int(handle)

    def set_kill_on_close(self, job: int) -> None:
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = self._k32.SetInformationJobObject(
            job, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
        )
        self._check(ok, "SetInformationJobObject")

    def assign(self, job: int, process_handle: int) -> None:
        ok = self._k32.AssignProcessToJobObject(job, process_handle)
        self._check(ok, "AssignProcessToJobObject")

    def terminate(self, job: int) -> None:
        self._k32.TerminateJobObject(job, 1)

    def process_count(self, job: int) -> int:
        info = _JOBOBJECT_BASIC_PROCESS_ID_LIST()
        # Even when the buffer cannot hold every pid, the call fills NumberOfAssignedProcesses with
        # the true total, which is all the emptiness proof needs.
        self._k32.QueryInformationJobObject(
            job, _JobObjectBasicProcessIdList, ctypes.byref(info), ctypes.sizeof(info), None
        )
        return int(info.NumberOfAssignedProcesses)

    def close(self, job: int) -> None:
        self._k32.CloseHandle(job)


class WindowsJobObjectContainment:
    """Windows containment via a kill-on-close Job Object (WRI-012).

    A Job Object owns its members across reparenting, unlike ``taskkill /T`` after the root exits,
    so it is the only primitive that can *prove* the tree gone. The child is assigned to the job
    immediately after launch; ``KILL_ON_JOB_CLOSE`` means an orchestrator crash (which closes the
    job handle) also kills the whole tree — the deliberate crash-semantics change WRI-012 documents.
    Emptiness is proven by ``QueryInformationJobObject`` reporting zero assigned processes after
    ``TerminateJobObject``. If the job cannot be created or the process cannot be assigned,
    containment was never established → the proof fails closed (``proven=False``).

    There is a negligible race between launch and assignment (a grandchild spawned in that window
    could escape); it is accepted for a Milestone-0 barrier and documented, the STARTUPINFOEX
    job-at-creation path being deferred hardening.
    """

    def __init__(
        self,
        *,
        win32: Win32JobApi,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        quiescence_timeout: float = _QUIESCENCE_TIMEOUT_SECONDS,
        poll: float = _QUIESCENCE_POLL_SECONDS,
    ) -> None:
        self._win32 = win32
        self._sleep = sleep_fn
        self._monotonic = monotonic
        self._quiescence_timeout = quiescence_timeout
        self._poll = poll
        self._job: int | None = None
        self._assigned = False
        self._adopt_error: str | None = None

    def popen_kwargs(self) -> dict[str, Any]:
        # A documented no-op on Windows (``start_new_session`` is ignored there); kept for kwarg
        # stability with the POSIX path. The Job Object, not a process group, does the containing.
        return {"start_new_session": False}

    def adopt(self, proc: subprocess.Popen[Any]) -> None:
        try:
            self._job = self._win32.create_job()
            self._win32.set_kill_on_close(self._job)
            handle = int(proc._handle)  # type: ignore[attr-defined]  # Windows Popen exposes _handle
            self._win32.assign(self._job, handle)
            self._assigned = True
        except (OSError, AttributeError, ValueError, TypeError) as exc:
            # Containment could not be established — recorded so the proof fails closed with a
            # secret-free reason. type(exc).__name__ avoids echoing any handle value.
            self._adopt_error = type(exc).__name__

    def terminate(self) -> None:
        if self._job is not None:
            with contextlib.suppress(OSError):
                self._win32.terminate(self._job)

    def terminate_and_prove(self) -> QuiescenceResult:
        if self._job is None or not self._assigned:
            self._safe_close()
            reason = self._adopt_error or "not assigned"
            return QuiescenceResult(
                proven=False, detail=f"windows: job object containment unavailable ({reason})"
            )
        self.terminate()
        deadline = self._monotonic() + self._quiescence_timeout
        try:
            while True:
                count = self._win32.process_count(self._job)
                if count == 0:
                    self._safe_close()
                    return QuiescenceResult(proven=True, detail="windows: job object empty")
                if self._monotonic() >= deadline:
                    self._safe_close()  # kill-on-close is the final backstop
                    return QuiescenceResult(
                        proven=False,
                        detail=(
                            f"windows: {count} process(es) still in job after "
                            f"{self._quiescence_timeout:.0f}s"
                        ),
                    )
                self._win32.terminate(self._job)
                self._sleep(self._poll)
        except OSError as exc:
            self._safe_close()
            return QuiescenceResult(
                proven=False, detail=f"windows: job query failed ({type(exc).__name__})"
            )

    def _safe_close(self) -> None:
        if self._job is not None:
            with contextlib.suppress(OSError):
                self._win32.close(self._job)
            self._job = None


def _default_make_containment() -> ProcessContainment:
    """Pick the platform containment: a Windows Job Object, else the POSIX process group."""
    if os.name == "nt":  # pragma: no cover - Windows-only branch (WRI-006 gate)
        return WindowsJobObjectContainment(win32=_RealWin32())
    return PosixProcessContainment()


def run_process(
    argv: Sequence[str],
    *,
    cwd: str | Path,
    env: Mapping[str, str],
    timeout_seconds: int,
    stdout_path: str | Path,
    stdin_text: str | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    recorder: AgentHandleRecorder | None = None,
    make_containment: Callable[[], ProcessContainment] = _default_make_containment,
) -> ProcessResult:
    """Launch ``argv`` safely and return a raw :class:`ProcessResult`.

    :param argv: the command and its arguments as a list (never a shell string).
    :param cwd: working directory for the child (the clone).
    :param env: the **entire** child environment (already allowlisted); not merged with the parent.
    :param timeout_seconds: mandatory wall-clock timeout; on expiry the child's whole subtree is
        killed and ``timed_out`` is set with ``exit_code=None``.
    :param stdout_path: file the child's stdout is streamed to (created/overwritten).
    :param stdin_text: text fed to the child's stdin; ``None`` means ``DEVNULL`` (no parent stdin).
    :param monotonic: monotonic clock seam for the measured duration (injected in tests).
    :param recorder: optional :class:`AgentHandleRecorder`; when set, the launched child's
        ``(pid, pgid)`` is recorded on spawn and cleared on reap so a hard stop can find it. The
        handle is cleared **only after** quiescence is proven — an unproven subtree keeps it so a
        later stop/recovery can still reap the survivor.
    :param make_containment: factory for the platform :class:`ProcessContainment` (injected in
        tests); defaults to a Windows Job Object or the POSIX process-group containment.
    :returns: a :class:`ProcessResult` carrying the :class:`QuiescenceResult`. A failed launch
        (missing/!executable binary) is reported via ``launch_error`` rather than raised; a timeout
        via ``timed_out``.

    **WRI-012 quiescence barrier.** Every attempt runs inside a containment object whose lifetime
    this call owns. On **every** exit path — clean exit, non-zero exit, timeout, interrupt, or any
    exception — the containment is terminated and **proven empty** within a bounded budget before
    the result is returned, so a background/detached descendant that outlived the root process
    cannot keep writing after this returns. A failure to prove quiescence is surfaced (via
    ``ProcessResult.quiescence.proven == False``) as a fail-closed security condition, not a silent
    success. On POSIX the containment is the child's own session/group plus during-run descendant
    tracking; on Windows it is a kill-on-close Job Object. On a timeout the subtree is killed here
    so the drain can return (classification stays ``timed_out``); on a propagating interrupt it is
    killed before re-raising, so a foreground ``worc run`` never orphans the agent.
    """
    start = monotonic()
    timed_out = False
    launch_error: str | None = None
    exit_code: int | None = None
    stderr_text = ""
    quiescence: QuiescenceResult | None = None

    # ``input`` and ``stdin`` are mutually exclusive: feed the prompt via PIPE + communicate(input),
    # or send EOF immediately via DEVNULL so a prompt-less child can never hang on inherited stdin.
    if stdin_text is None:
        stdin_arg: Any = subprocess.DEVNULL
        input_arg: str | None = None
    else:
        stdin_arg = subprocess.PIPE
        input_arg = stdin_text

    try:
        stdout_file = Path(stdout_path).open("wb")  # noqa: SIM115 — closed in the inner `with`
    except OSError as exc:
        # The stdout sink itself could not be opened (unwritable dir, bad path). Degrade rather than
        # raise, and name the *path* — not argv[0], which launched fine and is not the culprit.
        launch_error = f"could not open stdout path {os.fspath(stdout_path)!r}: {_reason(exc)}"
    else:
        with stdout_file:
            containment = make_containment()
            try:
                proc = subprocess.Popen(
                    list(argv),
                    cwd=os.fspath(cwd),
                    env=dict(env),
                    stdin=stdin_arg,
                    stdout=stdout_file,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    shell=False,
                    # Place the child into the platform containment at creation (POSIX: lead a new
                    # session/group; Windows: a documented no-op — the Job Object does the owning).
                    **containment.popen_kwargs(),
                )
            except OSError as exc:
                # The binary could not be launched (missing / not executable / bad cwd). argv[0]
                # comes from config (no secret); safe to name. FileNotFoundError / PermissionError /
                # NotADirectoryError are all OSError, so one clause covers them. Nothing launched →
                # nothing to contain, so quiescence stays None (trivially satisfied).
                command = argv[0] if argv else "<empty argv>"
                launch_error = f"could not launch {command!r}: {_reason(exc)}"
            else:
                containment.adopt(proc)
                # The child leads its own group, so its pgid equals its pid; record the pid directly
                # rather than calling os.getpgid (which would race an instant-exit child to ESRCH).
                if recorder is not None:
                    recorder.on_spawn(proc.pid, proc.pid)
                try:
                    _, stderr_out = proc.communicate(input=input_arg, timeout=timeout_seconds)
                    exit_code = proc.returncode
                    stderr_text = stderr_out or ""
                except subprocess.TimeoutExpired:
                    containment.terminate()  # kill so the drain returns; proof runs in `finally`
                    _, drained = proc.communicate()  # reap the zombie; collect any tail stderr
                    timed_out = True
                    stderr_text = drained or ""
                except BaseException:
                    # KeyboardInterrupt (and any other propagating exception): kill the subtree now
                    # so the agent can't outlive the daemon/foreground process, then re-raise. This
                    # bypasses the Router's `except ProviderError`, so it never triggers a fallback.
                    containment.terminate()
                    with contextlib.suppress(Exception):
                        proc.communicate()
                    raise
                finally:
                    # WRI-012 barrier — on EVERY exit path: terminate the containment and prove it
                    # empty within the bounded budget before this call returns. Clear the external
                    # hard-stop handle ONLY once quiescence is proven; an unproven subtree keeps the
                    # handle so a later stop/recovery can still reap the survivor.
                    quiescence = containment.terminate_and_prove()
                    if recorder is not None and quiescence.proven:
                        recorder.on_reap()

    duration_seconds = monotonic() - start
    return ProcessResult(
        exit_code=exit_code,
        timed_out=timed_out,
        launch_error=launch_error,
        duration_seconds=duration_seconds,
        stdout_path=os.fspath(stdout_path),
        stderr_text=stderr_text,
        quiescence=quiescence,
    )


def spawn_detached(
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    capture_path: str | Path | None = None,
) -> subprocess.Popen[bytes]:
    """Launch a long-running child (the ``watch`` daemon the console supervises); return its handle.

    The detached counterpart to :func:`run_process`: it neither waits nor captures — the caller
    supervises/stops the child itself. Same safety invariants: an **argv list**, ``shell=False``,
    and the child's stdin is ``DEVNULL`` (the parent's stdin is never inherited). ``env``/``cwd``
    default to the parent's (the daemon is the orchestrator itself and needs its normal
    environment), unlike the allowlisted-env agent runner.

    ``capture_path`` names a *startup log*: the child's stdout/stderr are redirected there (created
    /truncated) so a startup crash — an argparse error, an import failure, a preflight abort, all of
    which land on raw stderr **before** the daemon configures its ``--log-file`` — is recoverable
    instead of vanishing into ``DEVNULL``. The console reads this file's tail when its liveness
    probe fails, to surface the real error rather than a false "started". With ``capture_path=None``
    (every other caller / test) stdout/stderr stay ``DEVNULL`` — the daemon is then observed through
    its own rotating ``--log-file``, not this stream. The startup log holds only the daemon's own
    output (no secrets beyond what the daemon already logs, which passes the same redaction filter).

    On POSIX the daemon launches with ``start_new_session=True`` so it **leads its own process
    group**: a ``stop --force-full`` can ``killpg`` that group (daemon + any checks child) without
    touching the console that spawned it. The agent runs in its **own** session/group (see
    :func:`run_process`) and is reaped separately via its recorded handle, so it is not orphaned by
    the daemon-group kill. On Windows the hard stop is a ``taskkill /T`` tree-kill (by parent→child,
    no group flag needed), so no ``creationflags`` are set here.
    """
    sink: Any = subprocess.DEVNULL
    if capture_path is not None:
        Path(capture_path).parent.mkdir(parents=True, exist_ok=True)
        # Truncate per spawn so the file holds only the current daemon's stream (never grows across
        # restarts). Handed to the child; the parent keeps no reference — the child owns/closes it.
        sink = Path(capture_path).open("wb")  # noqa: SIM115 — owned by the spawned child, not this frame
    try:
        return subprocess.Popen(
            list(argv),
            cwd=os.fspath(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.STDOUT if capture_path is not None else subprocess.DEVNULL,
            shell=False,
            # POSIX only: lead a new session/group so the daemon's agents are killable as one group.
            # A documented no-op on Windows (``start_new_session`` is ignored there).
            start_new_session=os.name != "nt",
        )
    finally:
        # Popen dup'd the fd into the child; close our copy so only the child holds it (a leaked
        # write handle here would keep the file open for this process's lifetime).
        if capture_path is not None:
            sink.close()


def hard_kill_tree(pid: int) -> None:
    """Windows hard stop: terminate the process tree rooted at ``pid`` via ``taskkill /F /T``.

    Wired into :func:`wastech_orchestrator.process_control.stop_process` as the ``hard_kill_fn``
    seam so ``process_control`` stays subprocess-free (its no-shell-out invariant): the daemon's
    agents are its child processes, so ``/T`` (whole tree) reaches them while the daemon's parent
    (the console) is untouched. An argv list, ``shell=False``; ``check=False`` ignores a non-zero
    exit (dead / recycled PID — no start-time guard on Windows), and an unlaunchable ``taskkill``
    is suppressed so the stop stays idempotent. The same seam serves both explicit
    ``--force-full`` and automatic escalation after a Windows soft-stop grace timeout.
    """
    with contextlib.suppress(OSError):
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            shell=False,
            capture_output=True,
            check=False,
        )


def kill_agent_subtree(pid: int, pgid: int) -> None:
    """Reap an agent and its **whole** descendant subtree; idempotent and shell-free.

    Called by every stop route (the ``run_process`` timeout / interrupt, the ``cmd_watch`` daemon
    ``finally``, and ``stop --force-full`` via the ``process_control`` seam). POSIX: SIGKILL the
    agent's own process group (the fast path — it leads its session, so this reaches in-group
    descendants at once), **plus** SIGKILL every descendant pid individually as a backstop for a
    process that broke away into its own session/group (the field failure this fixes). The
    descendant set is snapshotted **before** the group kill so a child that re-parents to init once
    its parent dies is still targeted. Windows: ``taskkill /F /T`` walks the tree by parent→child.
    """
    if os.name == "nt":
        hard_kill_tree(pid)
        return
    descendants = _posix_descendants(pid)  # snapshot before killing anything
    with contextlib.suppress(ProcessLookupError):
        _KILLPG(pgid, _SIGKILL)
    for child_pid in descendants:
        with contextlib.suppress(ProcessLookupError):
            os.kill(child_pid, _SIGKILL)


def _posix_descendants(root: int) -> list[int]:
    """The full descendant pid set of ``root`` (all generations), from one ``ps`` snapshot.

    Uses ``ps -axo pid=,ppid=`` (the trailing ``=`` suppresses the column headers) — one argv-list
    subprocess, ``shell=False``, no pid/path ever interpolated into a shell string. Builds the
    ppid→children map and DFS-walks it from ``root``. Defensive: an unreadable/absent ``ps`` or a
    malformed row yields no descendants rather than raising (the group kill remains the fast path).
    """
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            shell=False,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    children: dict[int, list[int]] = {}
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            cpid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(cpid)
    seen: list[int] = []
    stack = list(children.get(root, []))
    while stack:
        cpid = stack.pop()
        if cpid == root or cpid in seen:
            continue
        seen.append(cpid)
        stack.extend(children.get(cpid, []))
    return seen


def _reason(exc: OSError) -> str:
    """A short, secret-free reason from an OS error (its ``strerror``, else the exception type)."""
    return exc.strerror or type(exc).__name__


def _coerce_stderr(raw: str | bytes | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw
