"""Tests for the environment allowlist."""

from __future__ import annotations

import pytest

from wastech_orchestrator.security.env import (
    build_child_env,
    default_allowed_environment,
    os_essential_env,
)


def test_only_allowlisted_keys_survive() -> None:
    parent = {"PATH": "/usr/bin", "HOME": "/home/u", "SECRET_TOKEN": "shh", "AWS_KEY": "x"}
    child = build_child_env(("PATH", "HOME"), parent)
    assert child == {"PATH": "/usr/bin", "HOME": "/home/u"}


def test_secrets_in_parent_never_forwarded() -> None:
    parent = {"PATH": "/usr/bin", "OPENAI_API_KEY": "sk-secret", "GITHUB_TOKEN": "ghp_x"}
    child = build_child_env(("PATH", "HOME", "CODEX_HOME"), parent)
    assert "OPENAI_API_KEY" not in child
    assert "GITHUB_TOKEN" not in child


def test_missing_keys_are_skipped_not_blanked() -> None:
    parent = {"PATH": "/usr/bin"}
    child = build_child_env(("PATH", "HOME", "CODEX_HOME"), parent)
    assert child == {"PATH": "/usr/bin"}
    assert "HOME" not in child


def test_empty_allowlist_yields_empty_env() -> None:
    assert build_child_env((), {"PATH": "/usr/bin"}) == {}


def test_allowlist_order_is_preserved() -> None:
    parent = {"HOME": "/home/u", "PATH": "/usr/bin", "CODEX_HOME": "/c"}
    child = build_child_env(("PATH", "CODEX_HOME", "HOME"), parent)
    assert list(child) == ["PATH", "CODEX_HOME", "HOME"]


def test_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WASTECH_ENV_SENTINEL", "present")
    monkeypatch.delenv("WASTECH_ENV_ABSENT", raising=False)
    child = build_child_env(("WASTECH_ENV_SENTINEL", "WASTECH_ENV_ABSENT"))
    assert child == {"WASTECH_ENV_SENTINEL": "present"}


def test_input_parent_mapping_not_mutated() -> None:
    parent = {"PATH": "/usr/bin", "SECRET": "x"}
    build_child_env(("PATH",), parent)
    assert parent == {"PATH": "/usr/bin", "SECRET": "x"}


def test_windows_essentials_include_systemroot() -> None:
    # SystemRoot is load-bearing: the Node-based claude.exe crashes at startup without it.
    win = os_essential_env("Windows")
    assert "SystemRoot" in win
    assert "PATHEXT" in win
    # The Windows set must not leak into the POSIX one, and vice versa.
    assert "SystemRoot" not in os_essential_env("Linux")


def test_posix_essentials_for_linux_and_darwin() -> None:
    # WSL reports as "Linux", so the POSIX set covers it too.
    posix = os_essential_env("Linux")
    assert "LD_LIBRARY_PATH" in posix
    assert os_essential_env("Darwin") == posix
    assert "APPDATA" not in posix


def test_default_allowed_environment_is_base_plus_os_essentials() -> None:
    base = ("PATH", "HOME", "USER", "USERPROFILE", "CODEX_HOME", "CLAUDE_CONFIG_DIR")
    win = default_allowed_environment("Windows")
    assert win[: len(base)] == base  # base first, OS essentials appended
    assert win == base + os_essential_env("Windows")
    assert "SystemRoot" in win
    assert "SystemRoot" not in default_allowed_environment("Linux")


def test_default_allowed_environment_detects_current_os() -> None:
    # No argument => current OS; must at least carry the cross-platform base.
    current = default_allowed_environment()
    assert {"PATH", "HOME", "CODEX_HOME"} <= set(current)
