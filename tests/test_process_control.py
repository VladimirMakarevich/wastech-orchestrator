"""Unit tests for the PID-file + graceful-shutdown plumbing (backlog: stop/restart).

Pure: every OS seam (``os.kill``, ``signal.signal``, sleeping, the clock) is injected, so nothing
here touches a real process or signal.
"""

from __future__ import annotations

import signal
from pathlib import Path

from wastech_orchestrator import process_control as pc

# Hermetic, OS-independent signal sentinels. ``SIGKILL`` is absent on Windows, so the hard-kill
# sentinel falls back to its POSIX number (9) — an opaque int distinct from SIGTERM, which is all
# the state machine needs (the real ``stop_process`` falls back the same way).
TERM = signal.SIGTERM
KILL = getattr(signal, "SIGKILL", 9)


def _start(_pid: int) -> str | None:
    """A deterministic, hermetic start-time reader (no real /proc or ps subprocess)."""
    return "start-token"


class FakeProcess:
    """A signalable process stand-in for the injected ``kill_fn``.

    ``kill_fn(pid, 0)`` probes liveness (raises ``ProcessLookupError`` once dead); a signal is
    recorded. ``dies_after`` makes it go dead after that many liveness probes (modelling a process
    that exits in response to a graceful stop); the ``kill_sig`` hard-kill always kills it.
    """

    def __init__(
        self, *, alive: bool = True, dies_after: int | None = None, kill_sig: int = KILL
    ) -> None:
        self.alive = alive
        self.signals: list[int] = []
        self._probes = 0
        self._dies_after = dies_after
        self._kill_sig = kill_sig

    def __call__(self, pid: int, sig: int) -> None:
        if sig == 0:  # liveness probe
            self._probes += 1
            if self._dies_after is not None and self._probes > self._dies_after:
                self.alive = False
            if not self.alive:
                raise ProcessLookupError
            return
        self.signals.append(sig)
        if sig == self._kill_sig:
            self.alive = False


# --- PID file -------------------------------------------------------------------------------------


def test_write_then_read_round_trips_and_creates_parent(tmp_path: Path) -> None:
    path = pc.pid_file_path(tmp_path / "does-not-exist-yet")
    pc.write_pid_file(path, pid=4321, start_time_fn=_start)
    assert path.name == "orchestrator.pid"
    assert pc.read_pid(path) == 4321
    record = pc.read_pid_record(path)
    assert record == pc.ProcessIdentity(pid=4321, start_time="start-token")


def test_write_pid_file_defaults_to_current_process(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.pid"
    pc.write_pid_file(path, start_time_fn=_start)
    assert pc.read_pid(path) == __import__("os").getpid()


def test_read_pid_tolerates_absent_empty_garbage_and_bare_integer(tmp_path: Path) -> None:
    assert pc.read_pid(tmp_path / "missing.pid") is None
    empty = tmp_path / "empty.pid"
    empty.write_text("", encoding="utf-8")
    assert pc.read_pid(empty) is None
    garbage = tmp_path / "garbage.pid"
    garbage.write_text("not-a-pid\n", encoding="utf-8")
    assert pc.read_pid(garbage) is None
    # A pre-JSON bare-integer file is treated as malformed (no migration), not as a live PID.
    legacy = tmp_path / "legacy.pid"
    legacy.write_text("4242\n", encoding="utf-8")
    assert pc.read_pid(legacy) is None


# --- is_running -----------------------------------------------------------------------------------


def test_is_running_true_when_signalable() -> None:
    assert pc.is_running(123, kill_fn=FakeProcess(alive=True)) is True


def test_is_running_false_when_process_lookup_error() -> None:
    assert pc.is_running(123, kill_fn=FakeProcess(alive=False)) is False


def test_is_running_true_on_permission_error() -> None:
    def denied(pid: int, sig: int) -> None:
        raise PermissionError

    assert pc.is_running(123, kill_fn=denied) is True


def _win_missing_pid_error() -> OSError:
    """An ``os.kill`` 'no such PID' error as Windows raises it (bare OSError, winerror 87)."""
    return OSError(22, "The parameter is incorrect", None, 87)


def test_is_no_such_process_classifies_missing_pid() -> None:
    assert pc._is_no_such_process(ProcessLookupError()) is True  # POSIX ESRCH
    assert pc._is_no_such_process(OSError("some other failure")) is False
    win = _win_missing_pid_error()
    if getattr(win, "winerror", None) == 87:  # the 4-arg winerror sticks only on Windows
        assert pc._is_no_such_process(win) is True


def test_is_running_false_on_windows_missing_pid() -> None:
    # Windows reports a dead/stale PID as a bare OSError(winerror=87), not ProcessLookupError; the
    # probe must read that as "not running" (else 'watch'/'stop' crash on a stale PID file).
    win = _win_missing_pid_error()
    if getattr(win, "winerror", None) != 87:
        return  # not on Windows — the constructed winerror does not apply

    def missing(pid: int, sig: int) -> None:
        raise _win_missing_pid_error()

    assert pc.is_running(123, kill_fn=missing) is False


def test_is_running_detects_recycled_pid_by_start_time() -> None:
    # Same PID, live, but a different start-time → the PID was recycled by an unrelated process.
    alive = FakeProcess(alive=True)
    assert (
        pc.is_running(
            123, expected_start="orig", kill_fn=alive, start_time_fn=lambda _pid: "different"
        )
        is False
    )
    # Matching start-time → it is still our process.
    assert (
        pc.is_running(123, expected_start="orig", kill_fn=alive, start_time_fn=lambda _pid: "orig")
        is True
    )
    # Unknown live start-time → degrade to the bare PID probe (alive).
    assert (
        pc.is_running(123, expected_start="orig", kill_fn=alive, start_time_fn=lambda _pid: None)
        is True
    )


def test_running_daemon_pid_none_when_recycled(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.pid"
    pc.write_pid_file(path, pid=4242, start_time_fn=_start)
    # Live PID but a different start-time → recycled → not our daemon (POSIX recycling guard).
    assert (
        pc.running_daemon_pid(
            path,
            kill_fn=FakeProcess(alive=True),
            start_time_fn=lambda _pid: "other",
            can_signal=True,
        )
        is None
    )
    # Live PID + matching start-time → our daemon.
    assert (
        pc.running_daemon_pid(
            path, kill_fn=FakeProcess(alive=True), start_time_fn=_start, can_signal=True
        )
        == 4242
    )


def test_running_daemon_pid_uses_file_presence_when_cannot_signal(tmp_path: Path) -> None:
    # Windows: liveness cannot be probed, so a present PID file reads as "running" (no recycling
    # guard); an absent file reads as "not running".
    path = tmp_path / "orchestrator.pid"
    assert pc.running_daemon_pid(path, can_signal=False) is None
    pc.write_pid_file(path, pid=4242, start_time_fn=_start)
    assert pc.running_daemon_pid(path, can_signal=False) == 4242


# --- stop_process ---------------------------------------------------------------------------------


def test_stop_process_absent_pid_is_idempotent_noop(tmp_path: Path) -> None:
    fake = FakeProcess()
    outcome = pc.stop_process(tmp_path / "orchestrator.pid", kill_fn=fake)
    assert outcome == pc.StopOutcome(
        found=False, pid=None, signaled=False, killed=False, already_dead=False
    )
    assert fake.signals == []


def test_stop_process_stale_file_is_reaped(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.pid"
    pc.write_pid_file(path, pid=4242, start_time_fn=_start)
    outcome = pc.stop_process(
        path, kill_fn=FakeProcess(alive=False), start_time_fn=_start, can_signal=True
    )
    assert outcome.already_dead is True
    assert outcome.signaled is False
    assert not path.exists()


def test_stop_process_recycled_pid_is_not_signaled(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.pid"
    pc.write_pid_file(path, pid=4242, start_time_fn=_start)
    fake = FakeProcess(alive=True)  # the PID is live, but it is now an unrelated process
    outcome = pc.stop_process(
        path, kill_fn=fake, start_time_fn=lambda _pid: "recycled", can_signal=True
    )
    assert outcome.already_dead is True
    assert fake.signals == []  # never signaled the innocent recycled process
    assert not path.exists()


def test_stop_process_graceful_sends_sigterm_only(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.pid"
    pc.write_pid_file(path, pid=4242, start_time_fn=_start)
    fake = FakeProcess(dies_after=2)  # alive through the soft signal, gone after one poll
    sleeps: list[float] = []
    outcome = pc.stop_process(
        path,
        timeout=30.0,
        poll=0.2,
        term_sig=TERM,
        kill_sig=KILL,
        kill_fn=fake,
        sleep_fn=sleeps.append,
        now_fn=lambda: 0.0,  # never reaches the deadline
        start_time_fn=_start,
        can_signal=True,
    )
    assert outcome.signaled is True
    assert outcome.killed is False
    assert fake.signals == [TERM]
    assert sleeps == [0.2]  # exercised the poll-then-recheck path exactly once
    assert not path.exists()


def test_stop_process_escalates_to_hard_kill_after_timeout(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.pid"
    pc.write_pid_file(path, pid=4242, start_time_fn=_start)
    fake = FakeProcess(alive=True)  # never dies on its own
    sleeps: list[float] = []
    outcome = pc.stop_process(
        path,
        timeout=0.0,  # deadline == now: escalate on the first check
        term_sig=TERM,
        kill_sig=KILL,
        kill_fn=fake,
        sleep_fn=sleeps.append,
        now_fn=lambda: 0.0,
        start_time_fn=_start,
        can_signal=True,
    )
    assert outcome.signaled is True
    assert outcome.killed is True
    assert outcome.timed_out is True
    assert fake.signals == [TERM, KILL]
    assert sleeps == []
    assert not path.exists()


# --- stop-file IPC (cross-platform graceful stop) -------------------------------------------------


def test_stop_file_path_and_requested(tmp_path: Path) -> None:
    sf = pc.stop_file_path(tmp_path)
    assert sf.name == "orchestrator.stop"
    assert pc.stop_file_requested(sf) is False
    sf.write_text("stop\n", encoding="utf-8")
    assert pc.stop_file_requested(sf) is True


def test_stop_process_writes_then_reaps_stop_file(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.pid"
    pc.write_pid_file(path, pid=4242, start_time_fn=_start)
    stop_file = pc.stop_file_path(tmp_path)
    fake = FakeProcess(dies_after=2)
    present_while_waiting: list[bool] = []
    outcome = pc.stop_process(
        path,
        term_sig=TERM,
        kill_sig=KILL,
        stop_file=stop_file,
        kill_fn=fake,
        sleep_fn=lambda _s: present_while_waiting.append(stop_file.exists()),
        now_fn=lambda: 0.0,
        start_time_fn=_start,
        can_signal=True,
    )
    assert present_while_waiting == [True]  # sentinel present while we wait for the graceful exit
    assert not stop_file.exists()  # reaped in the terminal branch
    assert outcome.signaled is True
    assert outcome.killed is False


# --- file-based stop (Windows: os.kill cannot reach an unrelated process) -------------------------


def test_stop_via_pid_file_graceful_waits_for_pid_file_removal(tmp_path: Path) -> None:
    # Windows path (can_signal=False): no os.kill. The daemon removes its own PID file on a clean
    # exit; stop confirms shutdown by that disappearance. Simulate it via the sleep seam.
    path = tmp_path / "orchestrator.pid"
    pc.write_pid_file(path, pid=4242, start_time_fn=_start)
    stop_file = pc.stop_file_path(tmp_path)

    polls: list[bool] = []

    def sleep_fn(_s: float) -> None:
        polls.append(stop_file.exists())  # stop-file present while we wait
        path.unlink(missing_ok=True)  # the daemon noticed it and reaped its PID file

    outcome = pc.stop_process(
        path,
        timeout=30.0,
        poll=0.2,
        stop_file=stop_file,
        sleep_fn=sleep_fn,
        now_fn=lambda: 0.0,
        can_signal=False,
    )
    assert polls == [True]
    assert outcome.signaled is True  # requested via the stop-file
    assert outcome.killed is False  # never hard-killed
    assert outcome.timed_out is False
    assert not path.exists()
    assert not stop_file.exists()  # both reaped


def test_stop_via_pid_file_times_out_when_pid_file_persists(tmp_path: Path) -> None:
    # A wedged daemon (or a stale PID file from a crash) never removes the PID file → timeout. We
    # cannot force-kill an unrelated process on Windows, so clear the PID file (unblocks a fresh
    # 'watch') but KEEP the stop-file so a merely-busy daemon still stops on its next tick.
    path = tmp_path / "orchestrator.pid"
    pc.write_pid_file(path, pid=4242, start_time_fn=_start)
    stop_file = pc.stop_file_path(tmp_path)
    outcome = pc.stop_process(
        path,
        timeout=0.0,  # deadline == now: time out on the first check
        stop_file=stop_file,
        sleep_fn=lambda _s: None,
        now_fn=lambda: 0.0,
        can_signal=False,
    )
    assert outcome.timed_out is True
    assert outcome.killed is False  # no hard kill on Windows
    assert not path.exists()  # cleared so a fresh 'watch' can start
    assert stop_file.exists()  # left in place so a busy-but-alive daemon stops itself next tick


# --- StopController -------------------------------------------------------------------------------


def test_stop_controller_installs_handler_sets_event_and_restores() -> None:
    table: dict[int, object] = {}

    def fake_signal(sig: int, handler: object) -> object:
        prev = table.get(sig, signal.SIG_DFL)
        table[sig] = handler
        return prev

    controller = pc.StopController(signal_fn=fake_signal)
    assert not controller.event.is_set()
    with controller:
        installed = table[signal.SIGTERM]
        assert callable(installed)
        installed(signal.SIGTERM, None)  # simulate the OS delivering SIGTERM
        assert controller.event.is_set()
    # the previous disposition is restored on exit
    assert table[signal.SIGTERM] is signal.SIG_DFL


# --- stop_process level="full" (hard process-group kill; the stop ladder's top rung) --------------


def test_stop_full_kills_the_process_group_on_posix(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.pid"
    pc.write_pid_file(path, pid=4242, start_time_fn=_start)
    killpg_calls: list[tuple[int, int]] = []
    outcome = pc.stop_process(
        path,
        level="full",
        can_signal=True,
        kill_fn=FakeProcess(alive=True),  # the daemon is live
        start_time_fn=_start,
        getpgid_fn=lambda _pid: 9000,
        killpg_fn=lambda pgid, sig: killpg_calls.append((pgid, sig)),
        kill_sig=KILL,
        now_fn=lambda: 0.0,
    )
    assert killpg_calls == [(9000, KILL)]  # one group SIGKILL
    assert outcome.group_killed is True
    assert outcome.killed is True
    assert outcome.already_dead is False
    assert not path.exists()


def test_stop_full_already_dead_when_pid_not_running(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.pid"
    pc.write_pid_file(path, pid=4242, start_time_fn=_start)
    touched: list[object] = []
    outcome = pc.stop_process(
        path,
        level="full",
        can_signal=True,
        kill_fn=FakeProcess(alive=False),  # already gone
        start_time_fn=_start,
        getpgid_fn=lambda pid: touched.append(pid) or 1,
        killpg_fn=lambda *a: touched.append(a),
    )
    assert outcome.already_dead is True
    assert outcome.group_killed is False
    assert touched == []  # never resolved/killed a dead pid's group
    assert not path.exists()


def test_stop_full_already_dead_when_getpgid_races(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.pid"
    pc.write_pid_file(path, pid=4242, start_time_fn=_start)
    killpg_calls: list[object] = []

    def getpgid_raises(_pid: int) -> int:
        raise ProcessLookupError

    outcome = pc.stop_process(
        path,
        level="full",
        can_signal=True,
        kill_fn=FakeProcess(alive=True),
        start_time_fn=_start,
        getpgid_fn=getpgid_raises,  # daemon exited between the liveness probe and the kill
        killpg_fn=lambda *a: killpg_calls.append(a),
    )
    assert outcome.already_dead is True
    assert killpg_calls == []
    assert not path.exists()


def test_stop_full_does_not_kill_a_recycled_pid_group(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.pid"
    pc.write_pid_file(path, pid=4242, start_time_fn=_start)
    touched: list[object] = []
    outcome = pc.stop_process(
        path,
        level="full",
        can_signal=True,
        kill_fn=FakeProcess(alive=True),  # the PID is live, but recycled (start-time differs)
        start_time_fn=lambda _pid: "recycled",
        getpgid_fn=lambda pid: touched.append(pid) or 1,
        killpg_fn=lambda *a: touched.append(a),
    )
    assert outcome.already_dead is True
    assert touched == []


def test_stop_full_degrades_to_soft_on_windows(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.pid"
    pc.write_pid_file(path, pid=4242, start_time_fn=_start)
    group_seams: list[object] = []

    def daemon_self_reaps(_seconds: float) -> None:
        path.unlink()  # the daemon noticed the stop-file and removed its own PID file

    outcome = pc.stop_process(
        path,
        level="full",
        can_signal=False,  # Windows: no cross-process group kill
        getpgid_fn=lambda pid: group_seams.append(pid) or 1,
        killpg_fn=lambda *a: group_seams.append(a),
        sleep_fn=daemon_self_reaps,
        now_fn=lambda: 0.0,
    )
    assert outcome.degraded_to_soft is True
    assert outcome.group_killed is False
    assert group_seams == []  # never group-killed on Windows
    assert not path.exists()


def test_stop_full_tree_kills_on_windows_with_hard_kill_fn(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.pid"
    stop_path = tmp_path / "orchestrator.stop"
    pc.write_pid_file(path, pid=4242, start_time_fn=_start)
    killed: list[int] = []
    outcome = pc.stop_process(
        path,
        level="full",
        can_signal=False,  # Windows: no cross-process group kill …
        hard_kill_fn=lambda pid: killed.append(pid),  # … but a taskkill tree-kill seam is supplied
        stop_file=stop_path,
    )
    assert killed == [4242]  # tree-killed via the seam, not degraded to soft
    assert outcome.tree_killed is True
    assert outcome.killed is True
    assert outcome.degraded_to_soft is False
    assert not path.exists()  # PID + stop files reaped
    assert not stop_path.exists()


# --- children handle file + agent-subtree reap (reliable-stop) ------------------------------------


def test_children_file_round_trips(tmp_path: Path) -> None:
    path = pc.children_file_path(tmp_path / "does-not-exist-yet")
    pc.write_children_file(path, pid=321, pgid=321, start_time_fn=_start)
    expected = pc.ChildHandle(pid=321, pgid=321, start_time="start-token")
    assert pc.read_children_record(path) == expected


def test_children_file_tolerates_missing_and_malformed(tmp_path: Path) -> None:
    path = pc.children_file_path(tmp_path)
    assert pc.read_children_record(path) is None  # absent
    path.write_text("{ not json", encoding="utf-8")
    assert pc.read_children_record(path) is None  # corrupt
    path.write_text('{"pid": 5}', encoding="utf-8")
    assert pc.read_children_record(path) is None  # missing pgid (pre-reliable-stop shape)


def test_clear_children_file_is_idempotent(tmp_path: Path) -> None:
    path = pc.children_file_path(tmp_path)
    pc.write_children_file(path, pid=1, pgid=1, start_time_fn=_start)
    pc.clear_children_file(path)
    assert not path.exists()
    pc.clear_children_file(path)  # a second clear is a no-op, not an error


def test_stop_full_reaps_daemon_group_and_agent_subtree(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.pid"
    stop_path = tmp_path / "orchestrator.stop"
    children_path = tmp_path / "orchestrator.children"
    pc.write_pid_file(path, pid=4242, start_time_fn=_start)
    pc.write_children_file(children_path, pid=777, pgid=777, start_time_fn=_start)
    killpg_calls: list[tuple[int, int]] = []
    subtree_calls: list[tuple[int, int]] = []
    sentinel_at_kill: list[bool] = []

    def record_subtree(pid: int, pgid: int) -> None:
        sentinel_at_kill.append(stop_path.exists())  # the marker must exist before the reap
        subtree_calls.append((pid, pgid))

    outcome = pc.stop_process(
        path,
        level="full",
        can_signal=True,
        kill_fn=FakeProcess(alive=True),
        start_time_fn=_start,
        getpgid_fn=lambda _pid: 9000,
        killpg_fn=lambda pgid, sig: killpg_calls.append((pgid, sig)),
        subtree_kill_fn=record_subtree,
        children_file=children_path,
        stop_file=stop_path,
        kill_sig=KILL,
    )
    assert killpg_calls == [(9000, KILL)]  # daemon group SIGKILLed
    assert subtree_calls == [(777, 777)]  # recorded agent's own subtree reaped separately
    assert sentinel_at_kill == [True]  # cancellation marker written first
    assert outcome.group_killed is True
    assert not path.exists()
    assert not stop_path.exists()
    assert not children_path.exists()


def test_stop_full_with_no_recorded_agent_kills_only_the_daemon_group(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.pid"
    children_path = tmp_path / "orchestrator.children"  # no handle written
    pc.write_pid_file(path, pid=4242, start_time_fn=_start)
    killpg_calls: list[tuple[int, int]] = []
    subtree_calls: list[object] = []
    outcome = pc.stop_process(
        path,
        level="full",
        can_signal=True,
        kill_fn=FakeProcess(alive=True),
        start_time_fn=_start,
        getpgid_fn=lambda _pid: 9000,
        killpg_fn=lambda pgid, sig: killpg_calls.append((pgid, sig)),
        subtree_kill_fn=lambda *a: subtree_calls.append(a),
        children_file=children_path,
        kill_sig=KILL,
    )
    assert killpg_calls == [(9000, KILL)]
    assert subtree_calls == []  # nothing recorded → no agent kill
    assert outcome.group_killed is True


def test_stop_full_reaps_orphaned_agent_even_when_daemon_already_dead(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.pid"
    children_path = tmp_path / "orchestrator.children"
    pc.write_pid_file(path, pid=4242, start_time_fn=_start)
    pc.write_children_file(children_path, pid=777, pgid=777, start_time_fn=_start)
    subtree_calls: list[tuple[int, int]] = []
    outcome = pc.stop_process(
        path,
        level="full",
        can_signal=True,
        kill_fn=FakeProcess(alive=False),  # a crashed daemon that never ran its finally
        start_time_fn=_start,
        killpg_fn=lambda *a: None,
        subtree_kill_fn=lambda pid, pgid: subtree_calls.append((pid, pgid)),
        children_file=children_path,
    )
    assert outcome.already_dead is True
    assert subtree_calls == [(777, 777)]  # the orphaned agent is still reaped
    assert not children_path.exists()


def test_stop_full_windows_tree_kills_daemon_and_recorded_agent(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.pid"
    children_path = tmp_path / "orchestrator.children"
    pc.write_pid_file(path, pid=4242, start_time_fn=_start)
    pc.write_children_file(children_path, pid=777, pgid=777, start_time_fn=_start)
    killed: list[int] = []
    outcome = pc.stop_process(
        path,
        level="full",
        can_signal=False,  # Windows: no cross-process group kill
        hard_kill_fn=lambda pid: killed.append(pid),
        children_file=children_path,
    )
    assert killed == [4242, 777]  # daemon tree AND the recorded agent tree
    assert outcome.tree_killed is True
    assert not children_path.exists()


def test_ensure_own_process_group_skips_when_already_leader() -> None:
    calls: list[object] = []
    # getpgrp == getpid → already a group leader (foreground job control / spawned with setsid).
    assert pc.ensure_own_process_group(
        getpid_fn=lambda: 777,
        getpgrp_fn=lambda: 777,
        setpgid_fn=lambda *a: calls.append(a),
    )
    assert calls == []  # never re-parents an existing leader (so foreground Ctrl-C is untouched)


def test_ensure_own_process_group_leads_when_not_leader() -> None:
    calls: list[tuple[int, int]] = []
    assert pc.ensure_own_process_group(
        getpid_fn=lambda: 777,
        getpgrp_fn=lambda: 100,  # inherited the parent's group (script/systemd launch)
        setpgid_fn=lambda pid, pgid: calls.append((pid, pgid)),
    )
    assert calls == [(0, 0)]  # become leader of a new group (no setsid)


def test_ensure_own_process_group_unavailable_on_windows() -> None:
    # Windows: os.getpgrp / os.setpgid do not exist → the helper reports "not a leader", no-op.
    assert pc.ensure_own_process_group(getpgrp_fn=None, setpgid_fn=None) is False


def test_ensure_own_process_group_swallows_eperm() -> None:
    def raise_eperm(_pid: int, _pgid: int) -> None:
        raise PermissionError("already a session leader")

    assert (
        pc.ensure_own_process_group(
            getpid_fn=lambda: 777, getpgrp_fn=lambda: 100, setpgid_fn=raise_eperm
        )
        is False
    )


def test_stop_soft_never_touches_the_group_seams(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.pid"
    pc.write_pid_file(path, pid=4242, start_time_fn=_start)
    touched: list[object] = []
    outcome = pc.stop_process(
        path,
        level="soft",
        can_signal=True,
        kill_fn=FakeProcess(dies_after=1),  # alive for the SIGTERM, gone on the first poll
        start_time_fn=_start,
        getpgid_fn=lambda pid: touched.append(pid) or 1,
        killpg_fn=lambda *a: touched.append(a),
        now_fn=lambda: 0.0,
    )
    assert touched == []
    assert outcome.group_killed is False
    assert outcome.signaled is True  # soft path sent SIGTERM
    assert outcome.killed is False
