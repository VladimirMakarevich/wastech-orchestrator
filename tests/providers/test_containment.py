"""Unit tests for the process-containment objects.

Fully seam-injected — no real process is launched or signalled. The POSIX containment's kill/probe/
snapshot/clock are fakes modelling a small process world; the Windows Job Object's ``kernel32``
surface is a fake recording the call sequence (the real calls are exercised only under the native
native-Windows gate). These tests own the fail-closed / unprovable behaviour deterministically; the
"real subtree is terminated" acceptance criteria live in ``test_process_quiescence_posix.py``.
"""

from __future__ import annotations

import wastech_orchestrator.providers.process as process_mod
from wastech_orchestrator.providers.process import (
    PosixProcessContainment,
    QuiescenceResult,
    WindowsJobObjectContainment,
)

_SIGKILL = process_mod._SIGKILL


# --- POSIX containment ----------------------------------------------------------------------------


class _World:
    """A tiny fake process world for the POSIX seams.

    ``group_alive`` drives ``killpg(pgid, 0)``; ``alive`` is the set of individually-probe-able pids
    (``kill(pid, 0)``). A recorded SIGKILL removes a pid / empties the group unless it is pinned
    ``unkillable`` (modelling a stuck writer). ``snapshot`` returns the fresh descendant set.
    """

    def __init__(
        self,
        *,
        group_alive: bool = False,
        alive: set[int] | None = None,
        snapshot: list[int] | None = None,
        unkillable: set[int] | None = None,
    ) -> None:
        self.group_alive = group_alive
        self.alive = set(alive or ())
        self._snapshot = list(snapshot or ())
        self.unkillable = set(unkillable or ())
        self.killpg_calls: list[tuple[int, int]] = []
        self.kill_calls: list[tuple[int, int]] = []

    def killpg(self, pgid: int, sig: int) -> None:
        self.killpg_calls.append((pgid, sig))
        if sig == 0:  # liveness probe
            if not self.group_alive:
                raise ProcessLookupError
            return
        if pgid not in self.unkillable:  # SIGKILL clears the group unless pinned
            self.group_alive = False

    def kill(self, pid: int, sig: int) -> None:
        self.kill_calls.append((pid, sig))
        if sig == 0:  # liveness probe
            if pid not in self.alive:
                raise ProcessLookupError
            return
        if pid not in self.unkillable:  # SIGKILL reaps it
            self.alive.discard(pid)

    def snapshot(self, _root: int) -> list[int]:
        return list(self._snapshot)


def _posix(world: _World, *, monotonic, quiescence_timeout=1.0, **kw) -> PosixProcessContainment:
    return PosixProcessContainment(
        killpg_fn=world.killpg,
        kill_fn=world.kill,
        snapshot_fn=world.snapshot,
        sleep_fn=lambda _s: None,
        monotonic=monotonic,
        quiescence_timeout=quiescence_timeout,
        poll=0.01,
        tracker_poll=100.0,  # large: the background thread never fires a second snapshot in a test
        **kw,
    )


def test_posix_no_process_launched_is_trivially_proven() -> None:
    world = _World()
    c = _posix(world, monotonic=lambda: 0.0)
    result = c.terminate_and_prove()  # never adopted
    assert result == QuiescenceResult(proven=True, detail="no process launched")
    assert world.killpg_calls == []  # nothing was signalled


def test_posix_empty_group_and_no_survivors_is_proven() -> None:
    world = _World(group_alive=False, alive=set(), snapshot=[])
    c = _posix(world, monotonic=lambda: 0.0)
    c._pid = 100
    c._pgid = 100
    result = c.terminate_and_prove()
    assert result.proven is True
    assert (100, _SIGKILL) in world.killpg_calls  # the group was SIGKILLed on the way to proving


def test_posix_tracked_setsid_escapee_is_killed_by_pid() -> None:
    # A tracked descendant that broke out of the group (setsid) is SIGKILLed individually, then the
    # proof confirms it gone — the group is empty but the escapee was still reaped.
    world = _World(group_alive=False, alive={200}, snapshot=[])
    c = _posix(world, monotonic=lambda: 0.0)
    c._pid = 100
    c._pgid = 100
    c._tracked = {200}
    result = c.terminate_and_prove()
    assert result.proven is True
    assert (200, _SIGKILL) in world.kill_calls  # the escapee was killed by pid, not by group


def test_posix_unkillable_survivor_fails_closed_with_pids() -> None:
    # A tracked pid that resists SIGKILL keeps the proof from succeeding: after the bounded budget
    # it returns proven=False and records the survivor pid (secret-free) for the audit.
    world = _World(group_alive=False, alive={200}, snapshot=[], unkillable={200})
    ticks = iter([0.0, 0.5, 5.0])  # deadline=1.0; second check (5.0) is past it
    c = _posix(world, monotonic=lambda: next(ticks), quiescence_timeout=1.0)
    c._pid = 100
    c._pgid = 100
    c._tracked = {200}
    result = c.terminate_and_prove()
    assert result.proven is False
    assert result.survivors == (200,)
    assert "200" in result.detail and "posix" in result.detail


def test_posix_nonempty_group_fails_closed() -> None:
    # A process group that never empties (a stuck in-group member) is a fail-closed condition even
    # with no individually-tracked survivor.
    world = _World(group_alive=True, alive=set(), snapshot=[], unkillable={100})
    ticks = iter([0.0, 0.5, 5.0])
    c = _posix(world, monotonic=lambda: next(ticks), quiescence_timeout=1.0)
    c._pid = 100
    c._pgid = 100
    result = c.terminate_and_prove()
    assert result.proven is False
    assert "group non-empty" in result.detail


def test_posix_zombie_clears_within_budget_then_proven() -> None:
    # A survivor still visible on the first probe (a SIGKILLed process awaiting its reaper) clears
    # on a later poll → proven within the budget, not a false fail-closed.
    world = _World(group_alive=False, alive={200}, snapshot=[])

    calls = {"n": 0}
    real_kill = world.kill

    def kill(pid: int, sig: int) -> None:
        # First liveness probe sees it; a later probe (after the reaper runs) reports it gone.
        if sig == 0 and pid == 200:
            calls["n"] += 1
            if calls["n"] >= 2:
                world.alive.discard(200)
        real_kill(pid, sig)

    world.kill = kill  # type: ignore[method-assign]
    ticks = iter([0.0, 0.2, 0.4, 0.6])  # never reaches the 1.0 deadline
    c = _posix(world, monotonic=lambda: next(ticks), quiescence_timeout=1.0)
    c._pid = 100
    c._pgid = 100
    c._tracked = {200}
    result = c.terminate_and_prove()
    assert result.proven is True


def test_posix_terminate_captures_a_final_snapshot_before_group_dies() -> None:
    # terminate() takes one last descendant snapshot so a child spawned just before shutdown is
    # still tracked and killed, even if the background tracker never observed it.
    world = _World(group_alive=False, alive={500}, snapshot=[500])
    c = _posix(world, monotonic=lambda: 0.0)
    c._pid = 100
    c._pgid = 100
    c.terminate()
    assert (500, _SIGKILL) in world.kill_calls  # the just-appeared descendant was reaped


# --- Windows Job Object containment ---------------------------------------------------------------


class _FakeWin32:
    """Records the Job Object call sequence; ``counts`` feeds successive ``process_count`` reads."""

    def __init__(self, *, counts: list[int], create_error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self._counts = iter(counts)
        self._create_error = create_error

    def create_job(self) -> int:
        self.calls.append("create")
        if self._create_error is not None:
            raise self._create_error
        return 42

    def set_kill_on_close(self, job: int) -> None:
        self.calls.append("set_kill_on_close")

    def assign(self, job: int, process_handle: int) -> None:
        self.calls.append(f"assign:{process_handle}")

    def terminate(self, job: int) -> None:
        self.calls.append("terminate")

    def process_count(self, job: int) -> int:
        self.calls.append("count")
        return next(self._counts)

    def close(self, job: int) -> None:
        self.calls.append("close")


class _FakeProc:
    def __init__(self, handle: int | None = 999) -> None:
        self.pid = 7
        if handle is not None:
            self._handle = handle


def _win(win32: _FakeWin32, *, monotonic, quiescence_timeout=1.0) -> WindowsJobObjectContainment:
    return WindowsJobObjectContainment(
        win32=win32,
        sleep_fn=lambda _s: None,
        monotonic=monotonic,
        quiescence_timeout=quiescence_timeout,
        poll=0.01,
    )


def test_windows_popen_kwargs_is_a_documented_noop() -> None:
    c = _win(_FakeWin32(counts=[0]), monotonic=lambda: 0.0)
    assert c.popen_kwargs() == {"start_new_session": False}


def test_windows_adopt_creates_kill_on_close_job_and_assigns() -> None:
    win = _FakeWin32(counts=[0])
    c = _win(win, monotonic=lambda: 0.0)
    c.adopt(_FakeProc(handle=999))
    assert win.calls == ["create", "set_kill_on_close", "assign:999"]


def test_windows_empty_job_is_proven_and_closed() -> None:
    win = _FakeWin32(counts=[0])
    c = _win(win, monotonic=lambda: 0.0)
    c.adopt(_FakeProc())
    result = c.terminate_and_prove()
    assert result.proven is True
    assert win.calls == ["create", "set_kill_on_close", "assign:999", "terminate", "count", "close"]


def test_windows_nonempty_job_fails_closed_after_budget() -> None:
    win = _FakeWin32(counts=[2, 2, 2, 2])  # never drains
    ticks = iter([0.0, 0.5, 5.0])  # deadline=1.0; second check past it
    c = _win(win, monotonic=lambda: next(ticks), quiescence_timeout=1.0)
    c.adopt(_FakeProc())
    result = c.terminate_and_prove()
    assert result.proven is False
    assert "2 process" in result.detail
    assert win.calls[-1] == "close"  # the handle is still closed (kill-on-close backstop)


def test_windows_adopt_failure_fails_closed_without_a_process() -> None:
    win = _FakeWin32(counts=[0], create_error=OSError("access denied"))
    c = _win(win, monotonic=lambda: 0.0)
    c.adopt(_FakeProc())
    result = c.terminate_and_prove()
    assert result.proven is False
    assert "containment unavailable" in result.detail
    assert win.calls == ["create"]  # never got to set/assign/terminate


def test_windows_missing_process_handle_fails_closed() -> None:
    win = _FakeWin32(counts=[0])
    c = _win(win, monotonic=lambda: 0.0)
    c.adopt(_FakeProc(handle=None))  # no _handle attribute → cannot assign
    result = c.terminate_and_prove()
    assert result.proven is False
    assert "containment unavailable" in result.detail
