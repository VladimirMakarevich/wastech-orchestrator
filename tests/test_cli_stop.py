"""The stop ladder (Phase 3 of the operator console): the idle/busy gate (``_resolve_stop_level``),
the ``YES`` confirm, and ``cmd_stop`` wiring. Pure — ``has_active_task`` / ``stop_process`` /
``sys.stdin`` are injected, so nothing touches a real daemon or terminal."""

from __future__ import annotations

import io
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from wastech_orchestrator import cli, process_control
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.state_store import StateStore, TaskRow

_ConfigFactory = Callable[..., OrchestratorConfig]


def _seed_running(config: OrchestratorConfig, task_id: str, node: str | None) -> None:
    """Seed a real state.db with a single RUNNING task parked at ``node`` (what `stop` leaves)."""
    db = cli.worc_home_for(config) / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = StateStore.open(db)
    store.insert_task(TaskRow(task_id=task_id, title="t", status=Status.RUNNING))
    if node is not None:
        store.save_flow_checkpoint(
            task_id, current_node=node, counters_json="{}", flow_fingerprint="fp", fix_iterations=0
        )
    store.close()


def _resolve(
    config: OrchestratorConfig,
    monkeypatch: pytest.MonkeyPatch,
    *,
    active: bool,
    force: bool = False,
    force_full: bool = False,
    interactive: bool = True,
    yes: bool = True,
) -> cli._StopDecision:
    monkeypatch.setattr(cli, "has_active_task", lambda _c: active)
    monkeypatch.setattr(cli, "_confirm_yes", lambda _p: yes)
    return cli._resolve_stop_level(
        config, force=force, force_full=force_full, interactive=interactive
    )


def _pin_state(monkeypatch: pytest.MonkeyPatch, state: str | None) -> None:
    """Pin the /proc state probe: the real one reads a live PID, which no test may depend on."""
    monkeypatch.setattr(process_control, "read_process_state", lambda _pid: state)


# --- the gate matrix --------------------------------------------------------------------


def test_idle_stops_soft_no_prompt_without_force_full(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path
) -> None:
    config = make_git_config(tmp_path / "clone")
    for force in [False, True]:
        decision = _resolve(config, monkeypatch, active=False, force=force)
        assert decision.proceed is True
        assert decision.level == "soft"  # nothing in flight: an ordinary stop, and no prompt


def test_idle_force_full_is_still_full(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path
) -> None:
    # --force-full outranks the activity probe. An idle daemon is exactly the wedged/suspended case
    # that needs the hard rung, so "no active task" must not silently downgrade it to soft.
    decision = _resolve(
        make_git_config(tmp_path / "clone"), monkeypatch, active=False, force_full=True
    )
    assert decision.proceed is True
    assert decision.level == "full"


def test_busy_no_flag_non_interactive_refuses_nonzero(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path
) -> None:
    decision = _resolve(
        make_git_config(tmp_path / "clone"), monkeypatch, active=True, interactive=False
    )
    assert decision.proceed is False
    assert decision.exit_code == 1
    assert "--force" in (decision.message or "") and "--force-full" in (decision.message or "")


def test_busy_interactive_yes_is_soft(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path
) -> None:
    decision = _resolve(
        make_git_config(tmp_path / "clone"), monkeypatch, active=True, interactive=True, yes=True
    )
    assert decision.proceed is True
    assert decision.level == "soft"  # typed YES never escalates to a hard kill


def test_busy_interactive_prompt_names_force_full_as_interrupt_now(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path
) -> None:
    # The busy prompt must point the operator at --force-full to interrupt the running agent NOW
    # (soft finishes the current flow node) — the discoverability gap this closes.
    captured: list[str] = []
    monkeypatch.setattr(cli, "has_active_task", lambda _c: True)
    monkeypatch.setattr(cli, "_confirm_yes", lambda prompt: captured.append(prompt) or False)
    cli._resolve_stop_level(
        make_git_config(tmp_path / "clone"), force=False, force_full=False, interactive=True
    )
    assert captured and "--force-full" in captured[0]


def test_busy_interactive_declined_aborts_zero(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path
) -> None:
    decision = _resolve(
        make_git_config(tmp_path / "clone"), monkeypatch, active=True, interactive=True, yes=False
    )
    assert decision.proceed is False
    assert decision.exit_code == 0
    assert decision.message == "stop: aborted"


def test_busy_force_is_soft_force_full_is_full(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path
) -> None:
    config = make_git_config(tmp_path / "clone")
    assert _resolve(config, monkeypatch, active=True, force=True).level == "soft"
    full = _resolve(config, monkeypatch, active=True, force_full=True)
    assert full.proceed is True and full.level == "full"


# --- the YES confirm --------------------------------------------------------------------


def test_confirm_yes_requires_exact_uppercase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _p: "YES")
    assert cli._confirm_yes("? ") is True
    monkeypatch.setattr("builtins.input", lambda _p: "yes")
    assert cli._confirm_yes("? ") is False
    monkeypatch.setattr("builtins.input", lambda _p: "")
    assert cli._confirm_yes("? ") is False


def test_confirm_yes_eof_is_no(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_eof(_p: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    assert cli._confirm_yes("? ") is False


# --- argparse + cmd_stop wiring ---------------------------------------------------------


def test_force_and_force_full_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["stop", "--force", "--force-full"])


def _patch_stop(
    monkeypatch: pytest.MonkeyPatch, config: OrchestratorConfig, *, active: bool, tty: bool
) -> dict[str, object]:
    monkeypatch.setattr(cli, "load_config_for", lambda _a: config)
    monkeypatch.setattr(cli, "has_active_task", lambda _c: active)
    monkeypatch.setattr(sys, "stdin", io.StringIO() if not tty else _TTY())
    captured: dict[str, object] = {}

    def fake_stop(_path: object, **kwargs: object) -> process_control.StopOutcome:
        captured["called"] = True
        captured.update(kwargs)
        return process_control.StopOutcome(
            found=True, pid=1, signaled=True, killed=False, already_dead=False
        )

    monkeypatch.setattr(cli.process_control, "stop_process", fake_stop)
    return captured


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_cmd_stop_idle_uses_soft_level(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path
) -> None:
    config = make_git_config(tmp_path / "clone")
    captured = _patch_stop(monkeypatch, config, active=False, tty=False)
    rc = cli.cmd_stop(cli.build_parser().parse_args(["stop"]))
    assert rc == 0
    assert captured.get("level") == "soft"


def test_cmd_stop_busy_force_full_uses_full_level(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path
) -> None:
    config = make_git_config(tmp_path / "clone")
    captured = _patch_stop(monkeypatch, config, active=True, tty=False)
    rc = cli.cmd_stop(cli.build_parser().parse_args(["stop", "--force-full"]))
    assert rc == 0
    assert captured.get("level") == "full"


def test_cmd_stop_busy_no_flag_non_tty_refuses(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path
) -> None:
    config = make_git_config(tmp_path / "clone")
    captured = _patch_stop(monkeypatch, config, active=True, tty=False)
    rc = cli.cmd_stop(cli.build_parser().parse_args(["stop"]))
    assert rc == 1
    assert "called" not in captured  # never reached stop_process


def test_cmd_stop_non_interactive_refuses_busy_without_prompting_even_on_a_tty(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path
) -> None:
    # Even with a TTY (the console's stdin), --non-interactive forces the refuse-with-flags path
    # instead of _confirm_yes()/input() — the console passes it so a busy `down` never blocks on
    # input() inside the prompt_toolkit REPL.
    config = make_git_config(tmp_path / "clone")
    captured = _patch_stop(monkeypatch, config, active=True, tty=True)
    monkeypatch.setattr(
        cli, "_confirm_yes", lambda _p: (_ for _ in ()).throw(AssertionError("must not prompt"))
    )
    rc = cli.cmd_stop(cli.build_parser().parse_args(["stop", "--non-interactive"]))
    assert rc == 1
    assert "called" not in captured  # refused; never reached stop_process


def test_cmd_stop_reports_group_kill(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path, capsys
) -> None:
    config = make_git_config(tmp_path / "clone")
    monkeypatch.setattr(cli, "load_config_for", lambda _a: config)
    monkeypatch.setattr(cli, "has_active_task", lambda _c: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(
        cli.process_control,
        "stop_process",
        lambda _p, **_k: process_control.StopOutcome(
            found=True, pid=7, signaled=True, killed=True, already_dead=False, group_killed=True
        ),
    )
    cli.cmd_stop(cli.build_parser().parse_args(["stop", "--force-full"]))
    assert "hard-stopped" in capsys.readouterr().out


def test_cmd_stop_reports_tree_kill(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path, capsys
) -> None:
    config = make_git_config(tmp_path / "clone")
    monkeypatch.setattr(cli, "load_config_for", lambda _a: config)
    monkeypatch.setattr(cli, "has_active_task", lambda _c: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(
        cli.process_control,
        "stop_process",
        lambda _p, **_k: process_control.StopOutcome(
            found=True,
            pid=7,
            signaled=True,
            killed=True,
            already_dead=False,
            timed_out=True,
            tree_killed=True,
        ),
    )
    cli.cmd_stop(cli.build_parser().parse_args(["stop", "--force-full"]))
    out = capsys.readouterr().out
    assert "hard-stopped after the graceful timeout" in out and "process tree" in out


def test_cmd_stop_soft_timeout_reports_pending_graceful_stop(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path, capsys
) -> None:
    # POSIX soft-stop timeout: nothing was killed, the request stays pending, and the CLI still
    # returns success while pointing the operator at --force-full.
    config = make_git_config(tmp_path / "clone")
    monkeypatch.setattr(cli, "load_config_for", lambda _a: config)
    monkeypatch.setattr(cli, "has_active_task", lambda _c: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    _pin_state(monkeypatch, None)  # the message must not depend on whatever PID 1234 really is
    monkeypatch.setattr(
        cli.process_control,
        "stop_process",
        lambda _p, **_k: process_control.StopOutcome(
            found=True, pid=1234, signaled=True, killed=False, already_dead=False, timed_out=True
        ),
    )
    rc = cli.cmd_stop(cli.build_parser().parse_args(["stop", "--force"]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "pending" in out
    assert "--force-full" in out
    assert "SIGKILL" not in out


def test_timed_out_stop_message_windows_keeps_pid_for_force_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The graceful stop stays pending; tell the operator the target remains intact.
    _pin_state(monkeypatch, None)  # Windows/macOS: no state source, so no claim is made
    msg = cli._timed_out_stop_message(1234, 30.0, is_windows=True)
    assert "did not confirm shutdown in 30s" in msg
    assert "kept its PID file" in msg
    assert "--force-full" in msg


def test_timed_out_stop_message_posix_is_pending_and_points_at_force_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The normal POSIX soft-timeout path: a pending graceful stop, never a SIGKILL. It names
    # --force-full as the immediate interrupt but never mentions the Windows-only taskkill.
    _pin_state(monkeypatch, None)
    msg = cli._timed_out_stop_message(1234, 30.0, is_windows=False)
    assert "did not confirm shutdown in 30s" in msg
    assert "pending" in msg
    assert "--force-full" in msg
    assert "taskkill" not in msg
    assert "suspended" not in msg  # no state source → no claim about why it did not confirm


def test_timed_out_stop_message_names_a_suspended_watcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Linux only: a watcher still in state T after the soft path's own SIGCONT is not busy, and
    # "retry with --force-full" alone would hide the cheaper, cleaner resolution.
    _pin_state(monkeypatch, "T")
    msg = cli._timed_out_stop_message(1234, 30.0, is_windows=False)
    assert "suspended (state T)" in msg
    assert "kill -CONT 1234" in msg
    assert "--force-full" in msg  # the hard rung is still offered, not replaced


def test_timed_out_stop_message_running_state_makes_no_suspension_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_state(monkeypatch, "R")
    msg = cli._timed_out_stop_message(1234, 30.0, is_windows=False)
    assert "suspended" not in msg
    assert "--force-full" in msg


def test_cmd_stop_reports_preserved_handles_when_pid_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    make_git_config: _ConfigFactory,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_git_config(tmp_path)
    root = cli.worc_home_for(config)
    stop_file = process_control.stop_file_path(root)
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.write_text("stop\n", encoding="utf-8")
    monkeypatch.setattr(cli, "load_config_for", lambda args: config)
    monkeypatch.setattr(
        process_control,
        "stop_process",
        lambda path, **kwargs: process_control.StopOutcome(
            found=False, pid=None, signaled=False, killed=False, already_dead=False
        ),
    )

    assert cli.cmd_stop(cli.build_parser().parse_args(["stop", "--force"])) == 0
    assert "preserved pending stop/child handles" in capsys.readouterr().out
    assert stop_file.exists()


@pytest.mark.parametrize("flag", ["--force", "--force-full"])
def test_cmd_stop_wires_hard_kill_seam(
    flag: str, monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path
) -> None:
    """The seam supports both timeout escalation and the explicit Windows hard rung."""
    config = make_git_config(tmp_path / "clone")
    captured = _patch_stop(monkeypatch, config, active=True, tty=False)
    cli.cmd_stop(cli.build_parser().parse_args(["stop", flag]))
    assert captured.get("hard_kill_fn") is cli.agent_process.hard_kill_tree


def test_cmd_stop_reports_windows_degrade(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path, capsys
) -> None:
    config = make_git_config(tmp_path / "clone")
    monkeypatch.setattr(cli, "load_config_for", lambda _a: config)
    monkeypatch.setattr(cli, "has_active_task", lambda _c: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    degraded = process_control.StopOutcome(
        found=True, pid=7, signaled=True, killed=False, already_dead=False, degraded_to_soft=True
    )
    monkeypatch.setattr(cli.process_control, "stop_process", lambda _p, **_k: degraded)
    cli.cmd_stop(cli.build_parser().parse_args(["stop", "--force-full"]))
    assert "unavailable on Windows" in capsys.readouterr().out


# --- the parked-slot note (signpost the recovery at stop time) --------------------------


def test_cmd_stop_notes_parked_slot_after_stopping_daemon(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path, capsys
) -> None:
    # After the daemon stops, a still-RUNNING task holds the slot (parked at its checkpoint). The
    # note must name it and the recovery levers, turning the dead-end into a signposted choice.
    config = make_git_config(tmp_path / "clone")
    _seed_running(config, "task-parked", node="planning")
    _patch_stop(monkeypatch, config, active=True, tty=False)
    rc = cli.cmd_stop(cli.build_parser().parse_args(["stop", "--force"]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "task-parked is still running (parked at node planning)" in out
    assert "rerun task-parked --continue" in out
    assert "finalize task-parked --as failed" in out


def test_cmd_stop_no_note_when_no_active_task(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path, capsys
) -> None:
    # No DB / no RUNNING task → nothing holds the slot → no note (the common idle stop).
    config = make_git_config(tmp_path / "clone")
    _patch_stop(monkeypatch, config, active=False, tty=False)
    rc = cli.cmd_stop(cli.build_parser().parse_args(["stop"]))
    assert rc == 0
    assert "holding the processing slot" not in capsys.readouterr().out
