"""Unit tests for the PID-file + graceful-shutdown plumbing (backlog: stop/restart).

Pure: every OS seam (``os.kill``, ``signal.signal``, sleeping, the clock) is injected, so nothing
here touches a real process or signal.
"""

from __future__ import annotations

import signal
from pathlib import Path

from wastech_orchestrator import process_control as pc


def _start(_pid: int) -> str | None:
    """A deterministic, hermetic start-time reader (no real /proc or ps subprocess)."""
    return "start-token"


class FakeProcess:
    """A signalable process stand-in for the injected ``kill_fn``.

    ``kill_fn(pid, 0)`` probes liveness (raises ``ProcessLookupError`` once dead); a signal is
    recorded. ``dies_after`` makes it go dead after that many liveness probes (modelling a process
    that exits in response to SIGTERM); ``SIGKILL`` always kills it.
    """

    def __init__(self, *, alive: bool = True, dies_after: int | None = None) -> None:
        self.alive = alive
        self.signals: list[int] = []
        self._probes = 0
        self._dies_after = dies_after

    def __call__(self, pid: int, sig: int) -> None:
        if sig == 0:  # liveness probe
            self._probes += 1
            if self._dies_after is not None and self._probes > self._dies_after:
                self.alive = False
            if not self.alive:
                raise ProcessLookupError
            return
        self.signals.append(sig)
        if sig == signal.SIGKILL:
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
    # Live PID but a different start-time → recycled → not our daemon.
    assert (
        pc.running_daemon_pid(
            path, kill_fn=FakeProcess(alive=True), start_time_fn=lambda _pid: "other"
        )
        is None
    )
    # Live PID + matching start-time → our daemon.
    assert (
        pc.running_daemon_pid(path, kill_fn=FakeProcess(alive=True), start_time_fn=_start) == 4242
    )


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
    outcome = pc.stop_process(path, kill_fn=FakeProcess(alive=False), start_time_fn=_start)
    assert outcome.already_dead is True
    assert outcome.signaled is False
    assert not path.exists()


def test_stop_process_recycled_pid_is_not_signaled(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.pid"
    pc.write_pid_file(path, pid=4242, start_time_fn=_start)
    fake = FakeProcess(alive=True)  # the PID is live, but it is now an unrelated process
    outcome = pc.stop_process(path, kill_fn=fake, start_time_fn=lambda _pid: "recycled")
    assert outcome.already_dead is True
    assert fake.signals == []  # never signaled the innocent recycled process
    assert not path.exists()


def test_stop_process_graceful_sends_sigterm_only(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.pid"
    pc.write_pid_file(path, pid=4242, start_time_fn=_start)
    fake = FakeProcess(dies_after=2)  # alive through the SIGTERM, gone after one poll
    sleeps: list[float] = []
    outcome = pc.stop_process(
        path,
        timeout=30.0,
        poll=0.2,
        kill_fn=fake,
        sleep_fn=sleeps.append,
        now_fn=lambda: 0.0,  # never reaches the deadline
        start_time_fn=_start,
    )
    assert outcome.signaled is True
    assert outcome.killed is False
    assert fake.signals == [signal.SIGTERM]
    assert sleeps == [0.2]  # exercised the poll-then-recheck path exactly once
    assert not path.exists()


def test_stop_process_escalates_to_sigkill_after_timeout(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.pid"
    pc.write_pid_file(path, pid=4242, start_time_fn=_start)
    fake = FakeProcess(alive=True)  # never dies on its own
    sleeps: list[float] = []
    outcome = pc.stop_process(
        path,
        timeout=0.0,  # deadline == now: escalate on the first check
        kill_fn=fake,
        sleep_fn=sleeps.append,
        now_fn=lambda: 0.0,
        start_time_fn=_start,
    )
    assert outcome.signaled is True
    assert outcome.killed is True
    assert fake.signals == [signal.SIGTERM, signal.SIGKILL]
    assert sleeps == []
    assert not path.exists()


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
