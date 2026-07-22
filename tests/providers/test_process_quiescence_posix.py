"""POSIX integration fixtures for the WRI-012 quiescence barrier (real processes).

These spawn genuine multi-generation / detached / reparented child processes and prove that a
background writer cannot modify the filesystem after ``run_process`` returns — the acceptance
criteria that only a real process tree can exercise. POSIX-only (the Windows Job Object path is
covered by the seam tests in ``test_containment.py`` and lands under the WRI-006 native gate).

The seam-injected fail-closed / unprovable behaviour lives in ``test_containment.py``; here every
survivor is one we own, so it is always genuinely reaped (a real process cannot resist SIGKILL).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

import wastech_orchestrator.providers.process as process_mod
from wastech_orchestrator.providers.process import PosixProcessContainment, run_process

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="POSIX process-group + descendant-tracking containment fixtures"
)

# A child sleeps this long before writing its marker; the test then waits past it. If the barrier
# reaped the child the marker never appears; if it leaked, the marker shows up after the sleep.
_CHILD_SLEEP = 1.5
_WAIT_PAST = 2.5


def _writer(marker: Path) -> str:
    """Python source for a child that waits, then writes ``marker`` (proof it outlived the root)."""
    return (
        f"import time, pathlib; time.sleep({_CHILD_SLEEP}); "
        f"pathlib.Path({str(marker)!r}).write_text('leaked')"
    )


def _spawn(child_src: str, *, setsid: bool = False) -> str:
    """Python source spawning ``child_src`` with detached stdio (so it never holds the root's pipe).

    ``setsid=True`` puts the child in its OWN session/group (the escape a process group cannot reap
    on its own — only the during-run descendant tracker catches it).
    """
    session = ", start_new_session=True" if setsid else ""
    return (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', {child_src!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL"
        f"{session}); "
    )


def _run(root_src: str, tmp_path: Path, make_containment=None) -> process_mod.ProcessResult:
    kwargs = {"make_containment": make_containment} if make_containment is not None else {}
    return run_process(
        [sys.executable, "-c", root_src],
        cwd=tmp_path,
        env={},
        timeout_seconds=30,
        stdout_path=tmp_path / "stdout.log",
        **kwargs,  # type: ignore[arg-type]
    )


def test_clean_exit_with_no_children_is_proven_quiescent(tmp_path: Path) -> None:
    result = _run("import sys; sys.exit(0)", tmp_path)
    assert result.exit_code == 0
    assert result.quiescence is not None
    assert result.quiescence.proven is True


def test_background_in_group_writer_is_reaped_after_exit(tmp_path: Path) -> None:
    """A background child (same process group, detached stdio) that outlives the exit-0 root is
    SIGKILLed by the group reap before ``run_process`` returns — it can never write the marker."""
    marker = tmp_path / "bg.txt"
    root = _spawn(_writer(marker)) + "sys.exit(0)"
    result = _run(root, tmp_path)
    assert result.exit_code == 0
    assert result.quiescence is not None and result.quiescence.proven is True
    time.sleep(_WAIT_PAST)
    assert not marker.exists()  # the background writer was reaped, not left to write


def test_nested_reparented_in_group_writer_is_reaped(tmp_path: Path) -> None:
    """root → mid → grandchild, all in one group; mid exits (grandchild reparents to init but stays
    in the group), so the group reap still reaches the grandchild writer."""
    marker = tmp_path / "nested.txt"
    mid = _spawn(_writer(marker)) + "sys.exit(0)"
    root = _spawn(mid) + "sys.exit(0)"
    result = _run(root, tmp_path)
    assert result.quiescence is not None and result.quiescence.proven is True
    time.sleep(_WAIT_PAST)
    assert not marker.exists()


def test_setsid_detached_writer_is_tracked_and_reaped(tmp_path: Path) -> None:
    """A child that breaks into its OWN session (``setsid``) leaves the group, so ``killpg`` alone
    cannot reap it. The during-run descendant tracker records it while it is still parent-linked, so
    termination kills it by pid and the barrier proves quiescence — the marker never appears."""
    marker = tmp_path / "setsid.txt"
    # The root stays alive briefly so the tracker snapshots the setsid child before the root exits
    # and the child reparents to init (after which a parent-PID walk could no longer attribute it).
    root = _spawn(_writer(marker), setsid=True) + "import time; time.sleep(0.4); sys.exit(0)"
    result = _run(
        root, tmp_path, make_containment=lambda: PosixProcessContainment(tracker_poll=0.05)
    )
    assert result.exit_code == 0
    assert result.quiescence is not None and result.quiescence.proven is True
    time.sleep(_WAIT_PAST)
    assert not marker.exists()  # the escaped setsid writer was tracked and reaped


def test_recorder_handle_retained_when_quiescence_unproven(tmp_path: Path) -> None:
    """When the barrier cannot prove quiescence, the external hard-stop handle is NOT cleared, so a
    later stop/recovery can still reap the survivor. Uses an injected always-unproven containment so
    the assertion does not depend on a genuinely unkillable process."""
    events: list[str] = []
    recorder = process_mod.AgentHandleRecorder(
        on_spawn=lambda pid, pgid: events.append("spawn"),
        on_reap=lambda: events.append("reap"),
    )

    class _Unproven:
        def popen_kwargs(self) -> dict[str, object]:
            return {"start_new_session": True}

        def adopt(self, proc: object) -> None:
            pass

        def terminate(self) -> None:
            pass

        def terminate_and_prove(self) -> process_mod.QuiescenceResult:
            return process_mod.QuiescenceResult(proven=False, detail="posix: unkillable (pid 999)")

    result = run_process(
        [sys.executable, "-c", "import sys; sys.exit(0)"],
        cwd=tmp_path,
        env={},
        timeout_seconds=30,
        stdout_path=tmp_path / "o.log",
        recorder=recorder,
        make_containment=_Unproven,
    )
    assert result.quiescence is not None and result.quiescence.proven is False
    assert events == ["spawn"]  # recorded on spawn, but the handle is NOT cleared (no "reap")
