"""``worc run`` is a live executor, and every liveness probe must see it as one.

`run` is a documented first-class entry point, but it recorded nothing about itself — so for its
whole duration its task read as ``parked (no daemon)`` ("parked at its checkpoint, awaiting resume
— not executing"), and every guard that asks "is anything running here?" answered no. The two
consequences are one mislabel and one real hazard: the label is the exact prompt that sends an
operator to ``rerun --continue``, and that command's only concurrency guard was the watch-daemon
PID — so it would have driven a second engine over the same branch in the same clone.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from wastech_orchestrator import cli, process_control
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.core.orchestrator import PipelineResult
from wastech_orchestrator.state_store import StateStore, Status, TaskRow

# `run` probes every allowed provider's credentials before starting; these tests are about the
# liveness marker, not credentials, so the gate is disarmed module-wide (its own tests assert it).
pytestmark = [pytest.mark.slow, pytest.mark.usefixtures("no_provider_auth_gate")]

_TASK_BODY = (
    '---\nid: task-1\ntitle: "T"\n---\n\n## Description\n\nDo it.\n\n'
    "## Acceptance criteria\n\n- ok\n"
)


@pytest.fixture
def in_repo_config(
    tmp_path: Path, make_git_config: Callable[..., OrchestratorConfig]
) -> OrchestratorConfig:
    """An in-repo config whose artifact root is an isolated clone dir, pull requests off."""
    clone = tmp_path / "clone"
    clone.mkdir()
    return make_git_config(clone, create_pr=False)


def _task_file(tmp_path: Path) -> Path:
    path = tmp_path / "task-1.md"
    path.write_text(_TASK_BODY, encoding="utf-8")
    return path


def test_run_records_its_own_liveness_and_reaps_it(
    monkeypatch: pytest.MonkeyPatch, in_repo_config: OrchestratorConfig, tmp_path: Path
) -> None:
    marker = process_control.runner_file_path(cli.worc_home_for(in_repo_config))
    seen: dict[str, object] = {}

    class _Orch:
        def run_task(self, task_file: str) -> PipelineResult:
            seen["during"] = marker.exists()
            seen["probe"] = cli._executor_alive(in_repo_config)
            return PipelineResult(task_id="task-1", final_status=Status.DONE)

    monkeypatch.setattr(cli, "load_config_for", lambda args: in_repo_config)
    monkeypatch.setattr(cli, "build_orchestrator", lambda *a, **k: _Orch())

    assert cli.main(["run", str(_task_file(tmp_path))]) == 0

    assert seen["during"] is True  # recorded for the whole run…
    assert seen["probe"] is True  # …and the shared liveness probe answers with it
    assert not marker.exists()  # reaped on exit, like the daemon's own PID file


def test_a_failing_run_still_reaps_its_marker(
    monkeypatch: pytest.MonkeyPatch, in_repo_config: OrchestratorConfig, tmp_path: Path
) -> None:
    # A marker left behind by a crash would make every guard below refuse forever.
    marker = process_control.runner_file_path(cli.worc_home_for(in_repo_config))

    class _Boom:
        def run_task(self, task_file: str) -> PipelineResult:
            raise RuntimeError("provider exploded")

    monkeypatch.setattr(cli, "load_config_for", lambda args: in_repo_config)
    monkeypatch.setattr(cli, "build_orchestrator", lambda *a, **k: _Boom())

    with pytest.raises(RuntimeError, match="provider exploded"):
        cli.main(["run", str(_task_file(tmp_path))])

    assert not marker.exists()


def _mark_running(config: OrchestratorConfig, pid: int = 4242) -> None:
    """Record a live ``run`` executor for this worc home (a fresh, real-looking PID record)."""
    process_control.write_pid_file(
        process_control.runner_file_path(cli.worc_home_for(config)), pid=pid
    )


def test_a_running_task_under_run_does_not_read_as_parked(
    monkeypatch: pytest.MonkeyPatch, in_repo_config: OrchestratorConfig
) -> None:
    _mark_running(in_repo_config)
    monkeypatch.setattr(process_control, "is_running", lambda pid, **kw: True)
    row = TaskRow(task_id="t1", title="T", status=Status.RUNNING)

    assert cli._executor_alive(in_repo_config) is True
    assert cli._display_status(row, executor_alive=True) == "running"
    assert cli._display_status(row, executor_alive=False) == "parked (no daemon)"


def test_stop_does_not_offer_to_continue_a_task_that_is_executing(
    monkeypatch: pytest.MonkeyPatch, in_repo_config: OrchestratorConfig
) -> None:
    # The note recommends `rerun --continue`, which is precisely the command that must not be run
    # against a live executor. It says who owns the slot instead — a note is more use here than
    # silence, since `stop` itself reports "no running watcher" and explains nothing.
    _mark_running(in_repo_config)
    monkeypatch.setattr(process_control, "is_running", lambda pid, **kw: True)

    note = cli._parked_slot_note(in_repo_config)

    assert note is not None
    assert "rerun" not in note
    assert "4242" in note


def test_rerun_refuses_while_a_run_owns_the_clone(
    monkeypatch: pytest.MonkeyPatch,
    in_repo_config: OrchestratorConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _mark_running(in_repo_config)
    monkeypatch.setattr(cli, "load_config_for", lambda args: in_repo_config)
    monkeypatch.setattr(process_control, "is_running", lambda pid, **kw: True)
    started: list[int] = []
    monkeypatch.setattr(cli, "build_orchestrator", lambda *a, **k: started.append(1))

    code = cli.main(["rerun", "task-1", "--continue"])

    assert code == 1
    assert started == []  # refused before any engine was built
    assert "run" in capsys.readouterr().out


def test_watch_refuses_to_start_while_a_run_owns_the_clone(
    monkeypatch: pytest.MonkeyPatch,
    in_repo_config: OrchestratorConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _mark_running(in_repo_config)
    monkeypatch.setattr(cli, "load_config_for", lambda args: in_repo_config)
    monkeypatch.setattr(process_control, "is_running", lambda pid, **kw: True)

    assert cli.main(["watch", "--poll-seconds", "5"]) == 1
    assert "run" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["finalize", "task-1", "--as", "done", "-y"], ["prs", "--sync"]])
def test_the_other_shared_clone_commands_refuse_too(
    monkeypatch: pytest.MonkeyPatch,
    in_repo_config: OrchestratorConfig,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    # Every command that refuses under a live daemon does so because it drives git in the shared
    # clone; a `run` executor holds that clone just as firmly.
    StateStore.open(Path(cli.worc_home_for(in_repo_config)) / "state.db").close()
    _mark_running(in_repo_config)
    monkeypatch.setattr(cli, "load_config_for", lambda args: in_repo_config)
    monkeypatch.setattr(process_control, "is_running", lambda pid, **kw: True)
    started: list[int] = []
    monkeypatch.setattr(cli, "build_orchestrator", lambda *a, **k: started.append(1))

    code = cli.main([*argv, *(["-y"] if argv[0] == "prs" else [])])

    assert code == 1
    assert started == []
    assert "run" in capsys.readouterr().out
