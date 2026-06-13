"""Unit tests for the canonical check model and shared predicates (automatic check discovery)."""

from __future__ import annotations

import pytest

from wastech_orchestrator.checks.model import (
    CheckCommandError,
    argv_matches_denied,
    normalize_check_command,
    normalize_commands,
    shell_metachars,
)
from wastech_orchestrator.config.schema import CheckCommandSpec


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


def test_normalize_commands_skips_blank_legacy_strings() -> None:
    checks = normalize_commands(["pytest", "  ", {"argv": ["ruff", "check", "."]}])
    assert [c.name for c in checks] == ["pytest", "ruff"]


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
