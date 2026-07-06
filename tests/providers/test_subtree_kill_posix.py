"""POSIX-gated real-topology tests for the reliable-stop subtree kill (no orphaned agents).

The acceptance gate for the ADR: spawn a real agent that leads its OWN process group and itself
spawns a grandchild in a SEPARATE group (the "broke away" case the field failure hit), then confirm
every stop route reaps the grandchild too — not just the direct child. These launch real processes,
so they are skipped on Windows (no ``setsid``/``killpg``; that path is the injected-seam tests +
the tracked Windows-validation-pending follow-up).
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from wastech_orchestrator.providers.process import (
    _posix_descendants,
    kill_agent_subtree,
    run_process,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX process-group topology")

# An agent that leads its own group (launched with start_new_session) and spawns a grandchild in a
# SEPARATE new session/group. The grandchild pid is written to argv[1] so the test can watch it.
_AGENT_CODE = (
    "import subprocess, sys, time\n"
    "gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'],"
    " start_new_session=True)\n"
    "open(sys.argv[1], 'w').write(str(gc.pid))\n"
    "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
    "time.sleep(300)\n"
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while _alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _read_grandchild_pid(gc_file: Path, timeout: float = 5.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if gc_file.exists() and gc_file.read_text().strip():
            return int(gc_file.read_text().strip())
        time.sleep(0.02)
    raise AssertionError("agent never recorded its grandchild pid")


def _best_effort_cleanup(*pids: int) -> None:
    for pid in pids:
        if pid:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


def test_kill_agent_subtree_reaps_a_grandchild_in_its_own_group(tmp_path: Path) -> None:
    """The direct primitive (also what the daemon ``finally`` and ``--force-full`` call) reaps a
    grandchild that broke away into its own group, not just the in-group agent."""
    gc_file = tmp_path / "gc.pid"
    agent = subprocess.Popen(
        [sys.executable, "-c", _AGENT_CODE, str(gc_file)],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,  # the agent leads its own group (as run_process launches it)
    )
    grandchild = 0
    try:
        grandchild = _read_grandchild_pid(gc_file)
        assert _alive(agent.pid) and _alive(grandchild)

        kill_agent_subtree(agent.pid, agent.pid)  # pgid == pid (agent is its own group leader)

        assert _wait_dead(grandchild), "the broke-away grandchild was orphaned"
        agent.wait(timeout=5)  # reap our direct child (a killed child is a zombie until waited)
        assert not _alive(grandchild)
    finally:
        _best_effort_cleanup(agent.pid, grandchild)


def test_run_process_timeout_reaps_the_whole_subtree(tmp_path: Path) -> None:
    """The ``run_process`` timeout route reaps the grandchild's separate group too (real kill)."""
    gc_file = tmp_path / "gc.pid"
    result = run_process(
        [sys.executable, "-c", _AGENT_CODE, str(gc_file)],
        cwd=tmp_path,
        env={},
        timeout_seconds=1,
        stdout_path=tmp_path / "o.log",
    )
    assert result.timed_out is True
    grandchild = int(gc_file.read_text().strip())
    try:
        assert _wait_dead(grandchild), "timeout left the grandchild orphaned"
    finally:
        _best_effort_cleanup(grandchild)


def test_posix_descendants_finds_all_generations(tmp_path: Path) -> None:
    """The descendant walk returns every generation below the agent (child + grandchild)."""
    gc_file = tmp_path / "gc.pid"
    agent = subprocess.Popen(
        [sys.executable, "-c", _AGENT_CODE, str(gc_file)],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    grandchild = 0
    try:
        grandchild = _read_grandchild_pid(gc_file)
        descendants = _posix_descendants(agent.pid)
        assert grandchild in descendants  # the broke-away grandchild is discovered by the sweep
    finally:
        _best_effort_cleanup(agent.pid, grandchild)
        agent.wait(timeout=5)
