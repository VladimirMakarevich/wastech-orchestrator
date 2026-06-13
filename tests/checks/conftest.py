"""Fixtures for the check discovery/resolution suite (backlog: automatic check discovery)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

import pytest

from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.providers.process import ProcessResult


@pytest.fixture
def make_repo(tmp_path: Path) -> Callable[..., Path]:
    """Build a fixture repository with the given marker files and optional venv tool scripts."""

    def build(
        files: dict[str, str] | None = None,
        *,
        venv: str | None = None,
        venv_tools: Iterable[str] = (),
        windows_venv: bool = False,
    ) -> Path:
        root = tmp_path / "repo"
        root.mkdir(exist_ok=True)
        for rel, content in (files or {}).items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        if venv is not None:
            bin_name, py_name, suffix = (
                ("Scripts", "python.exe", ".exe") if windows_venv else ("bin", "python", "")
            )
            bin_dir = root / venv / bin_name
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / py_name).write_text("#!/bin/sh\n", encoding="utf-8")
            for tool in venv_tools:
                (bin_dir / f"{tool}{suffix}").write_text("#!/bin/sh\n", encoding="utf-8")
        return root

    return build


@pytest.fixture
def make_checks_config() -> Callable[..., OrchestratorConfig]:
    """Build a minimal config with a given discovery mode and commands."""

    def build(
        *,
        local_path: str = "/tmp/repo",
        mode: str = "deterministic",
        commands: Sequence[str] | None = None,
        denied_commands: Sequence[str] = (),
    ) -> OrchestratorConfig:
        command_lines = "\n".join(f"    - {c!r}" for c in (commands or []))
        denied_lines = "\n".join(f"    - {d!r}" for d in denied_commands)
        text = f"""
repo:
  local_path: {local_path!r}
agents:
  allowed: [claude]
  providers:
    claude:
      command: "claude"
security:
  allowed_environment: [PATH, HOME]
  denied_commands:
{denied_lines if denied_commands else "    []"}
checks:
  discovery:
    mode: {mode}
  commands:
{command_lines if commands else "    []"}
"""
        return loads_config(text).config

    return build


def make_process_result(
    *, exit_code: int | None = 0, launch_error: str | None = None, timed_out: bool = False
) -> ProcessResult:
    return ProcessResult(
        exit_code=exit_code,
        timed_out=timed_out,
        launch_error=launch_error,
        duration_seconds=0.0,
        stdout_path="",
        stderr_text="",
    )
