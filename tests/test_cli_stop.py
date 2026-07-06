"""The stop ladder (Phase 3 of the operator console): the idle/busy gate (``_resolve_stop_level``),
the ``YES`` confirm, and ``cmd_stop`` wiring. Pure — ``has_active_task`` / ``stop_process`` /
``sys.stdin`` are injected, so nothing touches a real daemon or terminal."""

from __future__ import annotations

import io
import sys
from collections.abc import Callable

import pytest

from wastech_orchestrator import cli, process_control
from wastech_orchestrator.config.schema import OrchestratorConfig

_ConfigFactory = Callable[..., OrchestratorConfig]


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


# --- the gate matrix --------------------------------------------------------------------


def test_idle_stops_soft_no_prompt_for_any_form(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path
) -> None:
    config = make_git_config(tmp_path / "clone")
    for force, force_full in [(False, False), (True, False), (False, True)]:
        decision = _resolve(config, monkeypatch, active=False, force=force, force_full=force_full)
        assert decision.proceed is True
        assert decision.level == "soft"  # idle: ordinary stop even with --force-full


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
    # (soft only finishes the current step) — the discoverability gap the ADR closes.
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
            found=True, pid=7, signaled=True, killed=True, already_dead=False, tree_killed=True
        ),
    )
    cli.cmd_stop(cli.build_parser().parse_args(["stop", "--force-full"]))
    out = capsys.readouterr().out
    assert "hard-stopped" in out and "process tree" in out


def test_cmd_stop_wires_hard_kill_seam(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path
) -> None:
    """cmd_stop passes the taskkill seam so the Windows hard rung is real, not a soft-degrade."""
    config = make_git_config(tmp_path / "clone")
    captured = _patch_stop(monkeypatch, config, active=True, tty=False)
    cli.cmd_stop(cli.build_parser().parse_args(["stop", "--force-full"]))
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
