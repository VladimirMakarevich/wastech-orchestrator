"""The on-demand `validate-flow` CLI command.

Drives ``cmd_validate_flow`` against real operator flow files under ``<repo>/.worc/flows/``. The
focus is the command's orchestration: operator-only scope (packaged built-ins excluded),
config-aware OK/FAIL per flow, the non-fatal prompt-variable WARN, and the ``0``/``1``/``2`` codes.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from wastech_orchestrator import cli
from wastech_orchestrator.observability import logging as obslog

# A minimal valid flow: one agent node → one publish node (mirrors the registry unit tests).
_FLOW_YAML = """\
flow:
  name: NAME
  task_type: NAME
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: work
      kind: agent
      role_file: roles/work.md
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: work, to: out }
"""


@pytest.fixture(autouse=True)
def _reset_package_logger() -> Iterator[None]:
    pkg = logging.getLogger(obslog.LOGGER_NAME)
    saved = pkg.handlers[:]
    pkg.handlers.clear()
    obslog._configured = False
    yield
    pkg.handlers.clear()
    pkg.handlers.extend(saved)
    obslog._configured = False


def _flows_dir(clone: Path) -> Path:
    d = clone / ".worc" / "flows"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_flow(clone: Path, name: str) -> None:
    (_flows_dir(clone) / f"{name}.yaml").write_text(_FLOW_YAML.replace("NAME", name))


def _args(name: str | None = None, *, all_flows: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        config="config.yaml", log_level="info", name=name, all_flows=all_flows
    )


def _patch_config(monkeypatch: pytest.MonkeyPatch, config: object) -> None:
    monkeypatch.setattr(cli, "_load_config", lambda _path: config)


def test_validate_flow_ok(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_flow(git_repo.clone, "custom")
    _patch_config(monkeypatch, make_git_config(git_repo.clone))
    rc = cli.cmd_validate_flow(_args("custom"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "flow custom: OK" in out


def test_validate_flow_accepts_yaml_suffix(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # `NAME.yaml` resolves the same as the bare stem `NAME` within .worc/flows/.
    _write_flow(git_repo.clone, "custom")
    _patch_config(monkeypatch, make_git_config(git_repo.clone))
    rc = cli.cmd_validate_flow(_args("custom.yaml"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "flow custom: OK" in out


def test_validate_flow_fail_on_broken_flow(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    (_flows_dir(git_repo.clone) / "bad.yaml").write_text("flow:\n  name: bad\n")  # malformed
    _patch_config(monkeypatch, make_git_config(git_repo.clone))
    rc = cli.cmd_validate_flow(_args("bad"))
    out = capsys.readouterr().out
    assert rc == 1
    assert "flow bad: FAIL" in out


def test_validate_flow_name_not_found(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    _flows_dir(git_repo.clone)  # empty flows dir
    _patch_config(monkeypatch, make_git_config(git_repo.clone))
    rc = cli.cmd_validate_flow(_args("nope"))
    out = capsys.readouterr().out
    assert rc == 2
    assert "not found in .worc/flows/" in out


def test_validate_flow_excludes_packaged_builtins(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # A packaged built-in the operator has NOT copied into .worc/flows/ is out of scope — it is
    # reported as not found rather than validated (the packaged copy) against this repo's config.
    _flows_dir(git_repo.clone)
    _patch_config(monkeypatch, make_git_config(git_repo.clone))
    rc = cli.cmd_validate_flow(_args("implementation"))
    out = capsys.readouterr().out
    assert rc == 2
    assert "not found in .worc/flows/" in out


def test_validate_flow_all_reports_each(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_flow(git_repo.clone, "good")
    (_flows_dir(git_repo.clone) / "bad.yaml").write_text("flow:\n  name: bad\n")  # malformed
    _patch_config(monkeypatch, make_git_config(git_repo.clone))
    rc = cli.cmd_validate_flow(_args(all_flows=True))
    out = capsys.readouterr().out
    assert rc == 1  # any invalid → 1
    assert "flow good: OK" in out
    assert "flow bad: FAIL" in out


def test_validate_flow_all_empty_is_vacuous_pass(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_config(monkeypatch, make_git_config(git_repo.clone))  # no flows dir at all
    rc = cli.cmd_validate_flow(_args(all_flows=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "no operator flows" in out


def test_validate_flow_requires_name_or_all(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_config(monkeypatch, make_git_config(git_repo.clone))
    rc = cli.cmd_validate_flow(_args())
    out = capsys.readouterr().out
    assert rc == 2
    assert "specify a flow NAME or --all" in out


def test_validate_flow_rejects_name_and_all_together(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_flow(git_repo.clone, "custom")
    _patch_config(monkeypatch, make_git_config(git_repo.clone))
    rc = cli.cmd_validate_flow(_args("custom", all_flows=True))
    out = capsys.readouterr().out
    assert rc == 2
    assert "not both" in out


def test_validate_flow_warns_on_unknown_prompt_var(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # A valid flow whose role prompt references an unknown {token} is OK (verbatim render is the
    # safe fallback) but surfaces a non-fatal WARN — the lint folded into validate-flow.
    _write_flow(git_repo.clone, "custom")
    roles = _flows_dir(git_repo.clone) / "roles"
    roles.mkdir()
    (roles / "work.md").write_text("Do the work using {bogus_token}.\n")
    _patch_config(monkeypatch, make_git_config(git_repo.clone))
    rc = cli.cmd_validate_flow(_args("custom"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "flow custom: OK" in out
    assert "flow custom: WARN" in out
    assert "bogus_token" in out


def test_validate_flow_config_load_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # No resolvable config → exit 2 (mirrors every other config-loading command).
    monkeypatch.setattr(cli, "resolve_config_path", lambda _args: None)
    rc = cli.cmd_validate_flow(
        argparse.Namespace(config=None, log_level="info", name="x", all_flows=False)
    )
    assert rc == 2
    assert "no orchestrator config found" in capsys.readouterr().out
