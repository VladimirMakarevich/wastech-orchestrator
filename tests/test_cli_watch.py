"""`watch` reads its pending queue from the configured artifact root (backlog: interactive
installer) — under the in-repo footprint that is the bound repo itself, not the cwd."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from wastech_orchestrator import cli, preflight, process_control
from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.install.config_writer import InstallSpec, build_and_validate
from wastech_orchestrator.providers.base import ProviderId


def test_pending_dir_is_under_the_bound_repo(tmp_path: Path) -> None:
    repo = tmp_path / "my-repo"
    spec = InstallSpec(
        repo_url="git@github.com:me/my-repo.git",
        repo_local_path=repo,
        base_branch="main",
        providers=(ProviderId.CODEX,),
        create_pull_request=False,
        auto_mode=False,
    )
    config = loads_config(build_and_validate(spec)).config
    # in_repo footprint: artifacts (and the pending queue) live in the bound repo, not the cwd.
    assert cli.pending_dir(config) == repo / "tasks" / "pending"


# --- stop / restart daemon control (backlog: stop/restart) ----------------------------------------


class _FakeOrch:
    """Minimal orchestrator stub for ``watch_loop`` tests (every tick is a no-op)."""

    def __init__(self, on_refresh: Callable[[], None] | None = None) -> None:
        self.refresh_calls = 0
        self._on_refresh = on_refresh

    def refresh_repo(self) -> None:
        self.refresh_calls += 1
        if self._on_refresh is not None:
            self._on_refresh()

    def resume(self) -> None:
        return None


class _FakeController:
    """Stand-in for ``StopController`` that never touches the real signal table."""

    def __init__(self, **_kwargs: object) -> None:
        self.event = threading.Event()

    def __enter__(self) -> _FakeController:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


@pytest.fixture
def in_repo_config(
    tmp_path: Path, make_git_config: Callable[..., OrchestratorConfig]
) -> OrchestratorConfig:
    """An in-repo config whose artifact root (PID-file home) is an isolated clone dir, PR off."""
    clone = tmp_path / "clone"
    clone.mkdir()
    return make_git_config(clone, create_pr=False)


def test_watch_loop_stops_before_first_tick_when_event_preset(
    in_repo_config: OrchestratorConfig, tmp_path: Path
) -> None:
    orch = _FakeOrch()
    event = threading.Event()
    event.set()
    results = cli.watch_loop(
        orch, in_repo_config, tmp_path / "pending", poll_interval=5, stop_event=event
    )
    assert results == []
    assert orch.refresh_calls == 0


def test_watch_loop_honors_event_set_during_tick(
    in_repo_config: OrchestratorConfig, tmp_path: Path
) -> None:
    event = threading.Event()
    orch = _FakeOrch(on_refresh=event.set)  # SIGTERM arrives mid-tick
    cli.watch_loop(orch, in_repo_config, tmp_path / "pending", poll_interval=100, stop_event=event)
    assert orch.refresh_calls == 1  # post-tick wait returns at once; no second tick


def test_watch_loop_without_event_uses_sleep_fn(
    in_repo_config: OrchestratorConfig, tmp_path: Path
) -> None:
    orch = _FakeOrch()
    sleeps: list[float] = []
    cli.watch_loop(
        orch,
        in_repo_config,
        tmp_path / "pending",
        poll_interval=3,
        max_iterations=2,
        sleep_fn=sleeps.append,
    )
    assert orch.refresh_calls == 2
    assert sleeps == [3]  # slept once between ticks, not after the last


def test_watch_loop_stops_before_first_tick_when_stop_file_present(
    in_repo_config: OrchestratorConfig, tmp_path: Path
) -> None:
    stop_file = tmp_path / "orchestrator.stop"
    stop_file.write_text("stop\n", encoding="utf-8")
    orch = _FakeOrch()
    results = cli.watch_loop(
        orch, in_repo_config, tmp_path / "pending", poll_interval=5, stop_file=stop_file
    )
    assert results == []
    assert orch.refresh_calls == 0  # cross-platform stop honored before any work


def test_watch_loop_honors_stop_file_created_during_tick(
    in_repo_config: OrchestratorConfig, tmp_path: Path
) -> None:
    stop_file = tmp_path / "orchestrator.stop"
    orch = _FakeOrch(on_refresh=lambda: stop_file.write_text("stop\n", encoding="utf-8"))
    sleeps: list[float] = []
    cli.watch_loop(
        orch,
        in_repo_config,
        tmp_path / "pending",
        poll_interval=100,
        sleep_fn=sleeps.append,
        stop_file=stop_file,
    )
    assert orch.refresh_calls == 1  # noticed the sentinel between ticks; no second tick
    assert sleeps == [100]


def test_watch_loop_event_present_honors_stop_file(
    in_repo_config: OrchestratorConfig, tmp_path: Path
) -> None:
    # The real daemon always passes a stop_event (SIGTERM channel). On Windows that event never
    # fires cross-process, so a stop request rides the stop-file alone. Regression guard: the
    # between-tick wait must re-check the stop-file, not block the whole poll_interval on the event.
    # Pre-fix this called stop_event.wait(100) on an unset event and slept ~100s (would hang here).
    stop_file = tmp_path / "orchestrator.stop"
    event = threading.Event()  # never set — mimics Windows (no cross-process SIGTERM)
    orch = _FakeOrch(on_refresh=lambda: stop_file.write_text("stop\n", encoding="utf-8"))
    cli.watch_loop(
        orch,
        in_repo_config,
        tmp_path / "pending",
        poll_interval=100,
        stop_event=event,
        stop_file=stop_file,
    )
    assert orch.refresh_calls == 1  # stop-file noticed in the interruptible wait; no second tick


# --- idle-gap memory cleanup hook (04.3 / AC-C2) --------------------------------------------------


def test_idle_cleanup_runs_when_no_task_active(
    in_repo_config: OrchestratorConfig, tmp_path: Path
) -> None:
    # No state.db under the clone → idle → the hook fires once per tick in the idle gap.
    calls: list[int] = []
    cli.watch_loop(
        _FakeOrch(),
        in_repo_config,
        tmp_path / "pending",
        poll_interval=0,  # single pass
        cleanup_hook=lambda: calls.append(1),
    )
    assert calls == [1]


def test_idle_cleanup_skipped_while_task_active(
    in_repo_config: OrchestratorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC-C2: a busy slot (RUNNING soft-pause) must never trigger cleanup.
    monkeypatch.setattr(cli, "has_active_task", lambda _config: True)
    calls: list[int] = []
    cli.watch_loop(
        _FakeOrch(),
        in_repo_config,
        tmp_path / "pending",
        poll_interval=0,
        cleanup_hook=lambda: calls.append(1),
    )
    assert calls == []


def test_build_cleanup_hook_none_when_disabled(
    in_repo_config: OrchestratorConfig,
) -> None:
    # Memory disabled (Q10) → no cleanup is ever scheduled.
    assert cli._build_cleanup_hook(in_repo_config) is None


def test_stop_no_pid_file_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    in_repo_config: OrchestratorConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "load_config_for", lambda args: in_repo_config)
    assert cli.main(["stop"]) == 0
    assert "no running watcher" in capsys.readouterr().out


def test_stop_clears_stale_pid_file(
    monkeypatch: pytest.MonkeyPatch,
    in_repo_config: OrchestratorConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "load_config_for", lambda args: in_repo_config)
    monkeypatch.setattr(process_control, "_can_signal", lambda: True)  # exercise the POSIX path
    pid_path = process_control.pid_file_path(cli.worc_home_for(in_repo_config))
    process_control.write_pid_file(pid_path, pid=999111)
    monkeypatch.setattr(process_control, "is_running", lambda pid, **kw: False)
    assert cli.main(["stop"]) == 0
    assert "cleared stale" in capsys.readouterr().out
    assert not pid_path.exists()


def test_watch_refuses_to_start_when_already_running(
    monkeypatch: pytest.MonkeyPatch,
    in_repo_config: OrchestratorConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "load_config_for", lambda args: in_repo_config)
    pid_path = process_control.pid_file_path(cli.worc_home_for(in_repo_config))
    process_control.write_pid_file(pid_path, pid=4242)
    monkeypatch.setattr(process_control, "is_running", lambda pid, **kw: True)
    assert cli.main(["watch", "--poll-seconds", "5"]) == 1
    assert "already running" in capsys.readouterr().out


def test_watch_writes_then_removes_pid_file(
    monkeypatch: pytest.MonkeyPatch, in_repo_config: OrchestratorConfig
) -> None:
    monkeypatch.setattr(cli, "load_config_for", lambda args: in_repo_config)
    monkeypatch.setattr(cli, "build_orchestrator", lambda *a, **k: object())
    monkeypatch.setattr(process_control, "StopController", _FakeController)
    pid_path = process_control.pid_file_path(cli.worc_home_for(in_repo_config))
    seen: dict[str, bool] = {}

    def fake_loop(orch: object, config: object, folder: object, **_kw: object) -> list[object]:
        seen["during"] = pid_path.exists()
        return []

    monkeypatch.setattr(cli, "watch_loop", fake_loop)
    assert cli.main(["watch", "--poll-seconds", "5"]) == 0
    assert seen["during"] is True
    assert not pid_path.exists()


def test_watch_clears_stale_stop_file_on_start_and_reaps_on_exit(
    monkeypatch: pytest.MonkeyPatch, in_repo_config: OrchestratorConfig
) -> None:
    monkeypatch.setattr(cli, "load_config_for", lambda args: in_repo_config)
    monkeypatch.setattr(cli, "build_orchestrator", lambda *a, **k: object())
    monkeypatch.setattr(process_control, "StopController", _FakeController)
    stop_path = process_control.stop_file_path(cli.worc_home_for(in_repo_config))
    stop_path.parent.mkdir(parents=True, exist_ok=True)
    stop_path.write_text("stop\n", encoding="utf-8")  # a stale sentinel from a prior run
    seen: dict[str, object] = {}

    def fake_loop(orch: object, config: object, folder: object, **kw: object) -> list[object]:
        seen["stop_file_kw"] = kw.get("stop_file")
        seen["cleared_during"] = not stop_path.exists()
        return []

    monkeypatch.setattr(cli, "watch_loop", fake_loop)
    assert cli.main(["watch", "--poll-seconds", "5"]) == 0
    assert seen["cleared_during"] is True  # stale sentinel cleared before the loop ran
    assert seen["stop_file_kw"] == stop_path  # the loop received the sentinel path to poll
    assert not stop_path.exists()  # reaped on exit


def test_restart_stops_previous_then_delegates_to_watch(
    monkeypatch: pytest.MonkeyPatch,
    in_repo_config: OrchestratorConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "load_config_for", lambda args: in_repo_config)
    outcome = process_control.StopOutcome(
        found=True, pid=4242, signaled=True, killed=False, already_dead=False
    )
    monkeypatch.setattr(process_control, "stop_process", lambda path, **kw: outcome)
    captured: dict[str, object] = {}

    def fake_watch(args: object) -> int:
        captured["poll"] = args.poll_seconds  # type: ignore[attr-defined]
        return 0

    monkeypatch.setattr(cli, "cmd_watch", fake_watch)
    assert cli.main(["restart", "--poll-seconds", "7"]) == 0
    assert "stopped previous watcher 4242" in capsys.readouterr().out
    assert captured["poll"] == 7


def test_restart_does_not_start_replacement_after_unconfirmed_stop(
    monkeypatch: pytest.MonkeyPatch,
    in_repo_config: OrchestratorConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "load_config_for", lambda args: in_repo_config)
    outcome = process_control.StopOutcome(
        found=True,
        pid=4242,
        signaled=True,
        killed=False,
        already_dead=False,
        timed_out=True,
    )
    monkeypatch.setattr(process_control, "stop_process", lambda path, **kw: outcome)
    called: list[object] = []
    monkeypatch.setattr(cli, "cmd_watch", called.append)

    assert cli.main(["restart", "--poll-seconds", "7"]) == 1
    assert called == []
    assert "did not start a replacement" in capsys.readouterr().out


def test_restart_does_not_start_replacement_when_pid_is_missing_but_stop_handle_remains(
    monkeypatch: pytest.MonkeyPatch,
    in_repo_config: OrchestratorConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "load_config_for", lambda args: in_repo_config)
    stop_file = process_control.stop_file_path(cli.worc_home_for(in_repo_config))
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.write_text("stop\n", encoding="utf-8")
    outcome = process_control.StopOutcome(
        found=False, pid=None, signaled=False, killed=False, already_dead=False
    )
    monkeypatch.setattr(process_control, "stop_process", lambda path, **kw: outcome)
    called: list[object] = []
    monkeypatch.setattr(cli, "cmd_watch", called.append)

    assert cli.main(["restart", "--force", "--poll-seconds", "7"]) == 1
    assert called == []
    assert stop_file.exists()
    assert "no replacement was started" in capsys.readouterr().out


def test_watch_fails_fast_when_gh_missing_and_pr_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    make_git_config: Callable[..., OrchestratorConfig],
    capsys: pytest.CaptureFixture[str],
) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    config = make_git_config(clone, create_pr=True)
    monkeypatch.setattr(cli, "load_config_for", lambda args: config)
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert cli.main(["watch", "--poll-seconds", "5"]) == 2
    assert "gh" in capsys.readouterr().out


def test_run_fails_fast_when_gh_missing_and_pr_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    make_git_config: Callable[..., OrchestratorConfig],
    capsys: pytest.CaptureFixture[str],
) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    config = make_git_config(clone, create_pr=True)
    monkeypatch.setattr(cli, "load_config_for", lambda args: config)
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert cli.main(["run", "task.md"]) == 2
    assert "gh" in capsys.readouterr().out


def test_watch_skips_gh_check_when_pr_disabled(
    monkeypatch: pytest.MonkeyPatch, in_repo_config: OrchestratorConfig
) -> None:
    monkeypatch.setattr(cli, "load_config_for", lambda args: in_repo_config)  # create_pr=False
    monkeypatch.setattr(cli, "build_orchestrator", lambda *a, **k: object())
    monkeypatch.setattr(cli, "watch_loop", lambda *a, **k: [])
    calls: list[int] = []
    monkeypatch.setattr(preflight, "require_gh", lambda: calls.append(1))
    assert cli.main(["watch", "--poll-seconds", "0"]) == 0
    assert calls == []
