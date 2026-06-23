"""Unit tests for the canonical check model and shared predicates."""

from __future__ import annotations

import pytest

from wastech_orchestrator.checks.model import (
    CheckCommandError,
    argv_matches_denied,
    is_safe_relpath,
    normalize_check_command,
    normalize_command_sets,
    shell_metachars,
)
from wastech_orchestrator.config.schema import CheckCommandSpec, CommandSet


def test_normalize_legacy_string_splits_and_names_from_argv0() -> None:
    check = normalize_check_command("pytest -q")
    assert check.argv == ("pytest", "-q")
    assert check.name == "pytest"


def test_normalize_string_name_from_path_basename() -> None:
    check = normalize_check_command(".venv/bin/python -m pytest")
    assert check.argv == (".venv/bin/python", "-m", "pytest")
    assert check.name == "python"


def test_normalize_windows_path_basename() -> None:
    # Windows interpreter paths arrive as structured argv (a legacy string would be shlex-mangled).
    check = normalize_check_command({"argv": [r".venv\Scripts\python.exe", "-m", "pytest"]})
    assert check.name == "python.exe"


def test_normalize_structured_mapping() -> None:
    check = normalize_check_command({"name": "tests", "argv": [".venv/bin/python", "-m", "pytest"]})
    assert check.name == "tests"
    assert check.argv == (".venv/bin/python", "-m", "pytest")


def test_normalize_command_spec_object() -> None:
    spec = CheckCommandSpec(argv=("ruff", "check", "."), name="lint")
    check = normalize_check_command(spec)
    assert check.name == "lint"
    assert check.argv == ("ruff", "check", ".")


def test_normalize_spec_without_name_derives_it() -> None:
    spec = CheckCommandSpec(argv=("cargo", "test"))
    assert normalize_check_command(spec).name == "cargo"


def test_normalize_empty_string_raises() -> None:
    with pytest.raises(CheckCommandError):
        normalize_check_command("   ")


def test_normalize_mapping_without_argv_raises() -> None:
    with pytest.raises(CheckCommandError):
        normalize_check_command({"name": "x"})


def test_normalize_command_spec_carries_cwd() -> None:
    spec = CheckCommandSpec(argv=("dotnet", "test"), name="bt", cwd="backend/src")
    assert normalize_check_command(spec).cwd == "backend/src"


def test_normalize_command_sets_carries_paths_timeout_skip_and_cwd() -> None:
    sets = normalize_command_sets(
        {
            "backend": CommandSet(
                commands=(CheckCommandSpec(argv=("dotnet", "test"), cwd="backend/src"),),
                paths=("backend/**",),
                timeout_seconds=2400,
                skip_if_unavailable=True,
            )
        }
    )
    assert len(sets) == 1
    s = sets[0]
    assert s.name == "backend"
    assert s.paths == ("backend/**",)
    assert s.timeout_seconds == 2400
    assert s.skip_if_unavailable is True
    assert s.checks[0].cwd == "backend/src"


def test_is_safe_relpath_rejects_traversal_and_absolute() -> None:
    assert is_safe_relpath("backend/src")
    assert is_safe_relpath("mobile")
    assert not is_safe_relpath("../escape")
    assert not is_safe_relpath("/etc")
    assert not is_safe_relpath("~/x")
    assert not is_safe_relpath("")


def test_shell_metachars_detects_injection_shapes() -> None:
    assert shell_metachars(["pytest"]) is None
    assert shell_metachars(["go", "test", "./..."]) is None  # dots/slashes are fine
    assert shell_metachars(["echo", "$(whoami)"]) == "$(whoami)"
    assert shell_metachars(["sh", "-c", "a; b"]) == "a; b"


def test_argv_matches_denied_prefix() -> None:
    denied = ("git commit", "git push")
    assert argv_matches_denied(["git", "commit", "-m", "x"], denied) == "git commit"
    assert argv_matches_denied(["git", "status"], denied) is None
    assert argv_matches_denied(["gitfoo"], denied) is None  # not a prefix on a word boundary
