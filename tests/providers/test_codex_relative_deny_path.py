"""A relative deny path must never reach Codex, and the canary must say so when one does.

``enrich-07b``: the operator ran with ``--config .worc/config.yaml``; the env-file was derived as
the relative ``.worc/.env`` and written into the generated permission profile as is. Codex 0.163.0
refuses that — "filesystem path `.worc/.env` must be absolute, use `~/...`, or start with `:`" —
so every ``coverage_critic`` attempt on Codex died in one second and fell back to Claude, losing
the cross-check by a second model. The pre-launch canary saw the same refusal on every probe and
read it as an undemonstrable sandbox, which the advanced mode waves through; the preflight smoke
never saw it at all, because it built a profile without the env-file.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path

import pytest

from wastech_orchestrator.cli import resolve_env_file_path
from wastech_orchestrator.composition import build_internal_deny_policy
from wastech_orchestrator.providers.base import ErrorClass
from wastech_orchestrator.providers.codex_canary import (
    CAPABILITY_POLICY_FAILED,
    run_codex_canary,
    run_codex_capability_smoke,
)
from wastech_orchestrator.providers.codex_profile import build_codex_permission_profile
from wastech_orchestrator.runtime_layout import InternalDenyPolicy, RuntimeLayout

_CODEX_REFUSAL = (
    "Error: filesystem path `.worc/.env` must be absolute, use `~/...`, or start with `:`"
)


def test_a_relative_config_path_yields_an_absolute_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    args = argparse.Namespace(config=".worc/config.yaml", env_file=None)
    path, required = resolve_env_file_path(args)
    assert path is not None and path.is_absolute()
    assert path == (tmp_path / ".worc" / ".env").absolute()
    assert required is False


def test_the_deny_policy_is_absolute_whatever_env_file_it_is_handed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    policy = build_internal_deny_policy(
        RuntimeLayout.default(tmp_path), env_file=Path(".worc/.env")
    )
    assert all(path.is_absolute() for path in policy.denied_paths)


def test_the_generated_codex_profile_has_only_absolute_filesystem_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The profile from the run, rebuilt: every key Codex sees is absolute (or a `:`-built-in).
    monkeypatch.chdir(tmp_path)
    profile = build_codex_permission_profile(
        permission_profile="read-only",
        working_directory=str(tmp_path),
        deny_policy=build_internal_deny_policy(
            RuntimeLayout.default(tmp_path), env_file=Path(".worc/.env")
        ),
        write_guard=None,
        denied_read_paths=(".env", "secrets/**"),
        network_access=True,
        strict_isolation=False,
    )
    keys = [key for key in profile["filesystem"] if key != "glob_scan_max_depth"]
    assert all(key.startswith(":") or Path(key).is_absolute() for key in keys), keys


def _refusing_runner(argv: list[str], cwd: str, env: Mapping[str, str]) -> tuple[int, str]:
    return 1, _CODEX_REFUSAL + "\n"


def test_the_canary_reports_a_refused_profile_as_a_configuration_error() -> None:
    # Was CAPABILITY_UNAVAILABLE ("a required read was blocked on this host") — a host verdict the
    # advanced mode lets through. It is our profile, and `codex exec` refuses the same one.
    outcome = run_codex_canary(
        command="codex",
        profile_arg="permissions.worc={ }",
        working_directory="/clone",
        private_probe="/clone/.worc/logs/req.json",
        exchange_probe="/clone/.worc-io/t/task.md",
        env={},
        system="Darwin",
        runner=_refusing_runner,
    )
    assert outcome.ok is False
    assert outcome.error_class is ErrorClass.CONFIGURATION_ERROR
    assert "must be absolute" in outcome.message


def test_the_preflight_smoke_builds_the_attempt_s_deny_entries(tmp_path: Path) -> None:
    # The smoke used to prove a profile without the env-file or the operator's blacklist, so it
    # printed "isolation probe OK" for a profile no real attempt ever launched with.
    env_file = tmp_path / "operator" / ".worc" / ".env"
    runs_home = tmp_path / "operator" / ".worc" / "runs"
    seen: list[str] = []

    def _record(argv: list[str], cwd: str, env: Mapping[str, str]) -> tuple[int, str]:
        seen.extend(token for token in argv if token.startswith("permissions.worc="))
        return _refusing_runner(argv, cwd, env)

    report = run_codex_capability_smoke(
        command="codex",
        home_dir=tmp_path,
        env={},
        permission_profile="read-only",
        system="Linux",
        runner=_record,
        inventory_probe=lambda command, env: (True, "No MCP servers configured yet."),
        attempt_deny=InternalDenyPolicy(
            control_home=tmp_path / "operator" / ".worc",
            private_home=tmp_path / "operator" / ".worc",
            env_file=env_file,
            runs_home=runs_home,
        ),
        denied_read_paths=(".env",),
    )
    assert seen, "the smoke never ran a probe"
    assert all(str(env_file) in profile for profile in seen)
    assert all(str(runs_home) in profile for profile in seen)
    # And a profile Codex refuses fails preflight outright instead of reading as a host gap.
    assert report.status == CAPABILITY_POLICY_FAILED
