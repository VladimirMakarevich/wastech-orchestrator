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
import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProcessResult:
    """Raw outcome of a single subprocess launch, before any provider-specific normalization."""

    exit_code: int | None  # None when the process timed out or never launched
    timed_out: bool
    launch_error: str | None  # set (secret-free) when the binary could not be launched at all
    duration_seconds: float
    stdout_path: str
    stderr_text: str  # captured stderr, NOT yet redacted — the caller redacts before writing it


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
        ``(pid, pgid)`` is recorded on spawn and cleared on reap so a hard stop can find it.
    :returns: a :class:`ProcessResult`. A failed launch (missing/!executable binary) is reported via
        ``launch_error`` rather than raised; a timeout via ``timed_out``.

    The child launches with ``start_new_session`` on POSIX so it **leads its own process group** —
    a modern agent CLI's descendants are reaped as one subtree (via the recorded handle) on every
    stop route, rather than relying on shared group membership a descendant can break away from. On
    a timeout the subtree is killed here (classification stays ``timed_out``); on a propagating
    interrupt (``KeyboardInterrupt``) the subtree is killed before re-raising, so a foreground
    ``worc run`` never orphans the agent even though it has no daemon ``finally`` backstop.
    """
    start = monotonic()
    timed_out = False
    launch_error: str | None = None
    exit_code: int | None = None
    stderr_text = ""

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
                    # POSIX: lead a new session/group so the agent's whole subtree is killable via
                    # the recorded (pid == pgid) handle on every stop route. A documented no-op on
                    # Windows, where the hard stop is a `taskkill /F /T` tree-kill by parent→child.
                    start_new_session=os.name != "nt",
                )
            except OSError as exc:
                # The binary could not be launched (missing / not executable / bad cwd). argv[0]
                # comes from config (no secret); safe to name. FileNotFoundError / PermissionError /
                # NotADirectoryError are all OSError, so one clause covers them.
                command = argv[0] if argv else "<empty argv>"
                launch_error = f"could not launch {command!r}: {_reason(exc)}"
            else:
                # The child leads its own group, so its pgid equals its pid; record the pid directly
                # rather than calling os.getpgid (which would race an instant-exit child to ESRCH).
                if recorder is not None:
                    recorder.on_spawn(proc.pid, proc.pid)
                try:
                    _, stderr_out = proc.communicate(input=input_arg, timeout=timeout_seconds)
                    exit_code = proc.returncode
                    stderr_text = stderr_out or ""
                except subprocess.TimeoutExpired:
                    kill_agent_subtree(proc.pid, proc.pid)
                    _, drained = proc.communicate()  # reap the zombie; collect any tail stderr
                    timed_out = True
                    stderr_text = drained or ""
                except BaseException:
                    # KeyboardInterrupt (and any other propagating exception): reap the subtree now
                    # so the agent can't outlive the daemon/foreground process, then re-raise. This
                    # bypasses the Router's `except ProviderError`, so it never triggers a fallback.
                    kill_agent_subtree(proc.pid, proc.pid)
                    with contextlib.suppress(Exception):
                        proc.communicate()
                    raise
                finally:
                    if recorder is not None:
                        recorder.on_reap()

    duration_seconds = monotonic() - start
    return ProcessResult(
        exit_code=exit_code,
        timed_out=timed_out,
        launch_error=launch_error,
        duration_seconds=duration_seconds,
        stdout_path=os.fspath(stdout_path),
        stderr_text=stderr_text,
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
    is suppressed so the stop stays idempotent (the CLI already points to Task Manager as backstop).
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
        os.killpg(pgid, signal.SIGKILL)
    for child_pid in descendants:
        with contextlib.suppress(ProcessLookupError):
            os.kill(child_pid, signal.SIGKILL)


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
