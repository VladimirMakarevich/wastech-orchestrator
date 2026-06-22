"""Detection DoD: git root/origin/branch/cleanliness, CLI discovery on PATH, and ecosystem-based
check auto-detection (backlog: interactive installer)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from wastech_orchestrator import preflight
from wastech_orchestrator.checks.detect import propose_default_commands
from wastech_orchestrator.install import detect
from wastech_orchestrator.providers.base import ProviderId

GitRunner = Callable[[list[str], Path], Any]


def test_git_info_reads_root_origin_and_branch(git_repo: Any) -> None:
    info = detect.git_info(git_repo.clone)
    assert info is not None
    assert info.root == git_repo.clone.resolve()
    assert info.origin_url == str(git_repo.remote)
    assert info.current_branch == "main"
    assert info.default_branch == "main"
    assert info.is_clean is True


def test_git_info_detects_dirty_tree(git_repo: Any) -> None:
    (git_repo.clone / "README.md").write_text("# changed\n", encoding="utf-8")
    info = detect.git_info(git_repo.clone)
    assert info is not None
    assert info.is_clean is False


def test_git_info_from_nested_subdir_resolves_root(git_repo: Any) -> None:
    nested = git_repo.clone / "src" / "deep"
    nested.mkdir(parents=True)
    info = detect.git_info(nested)
    assert info is not None
    assert info.root == git_repo.clone.resolve()


def test_git_info_without_origin(tmp_path: Path, git_run: GitRunner) -> None:
    repo = tmp_path / "norigin"
    repo.mkdir()
    git_run(["init", "-b", "main", "."], repo)
    git_run(["config", "user.email", "t@example.com"], repo)
    git_run(["config", "user.name", "T"], repo)
    git_run(["config", "commit.gpgsign", "false"], repo)
    git_run(["commit", "--allow-empty", "-m", "init"], repo)
    info = detect.git_info(repo)
    assert info is not None
    assert info.origin_url is None


def test_git_info_outside_repo_is_none(tmp_path: Path) -> None:
    assert detect.git_info(tmp_path) is None


def test_detect_providers_and_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    present = {"codex": "/usr/bin/codex", "gh": "/usr/bin/gh"}
    monkeypatch.setattr("shutil.which", lambda name: present.get(name))
    providers = detect.detect_providers()
    assert providers[ProviderId.CODEX] == "/usr/bin/codex"
    assert providers[ProviderId.CLAUDE] is None
    assert detect.has_gh() is True


def test_has_gh_false_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert detect.has_gh() is False


def test_require_gh_is_noop_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh" if name == "gh" else None)
    preflight.require_gh()  # must not raise


def test_require_gh_raises_with_actionable_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(preflight.GhNotAvailableError) as exc:
        preflight.require_gh()
    message = str(exc.value)
    assert "gh" in message
    assert "cli.github.com" in message


# -- gh auth advisory (non-blocking) ------------------------------------------


def _fake_process_result(**overrides: Any) -> Any:
    from wastech_orchestrator.providers.process import ProcessResult

    base: dict[str, Any] = {
        "exit_code": 0,
        "timed_out": False,
        "launch_error": None,
        "duration_seconds": 0.0,
        "stdout_path": "/dev/null",
        "stderr_text": "",
    }
    base.update(overrides)
    return ProcessResult(**base)


@pytest.mark.parametrize(
    ("result_kwargs", "expected"),
    [
        ({"exit_code": 0}, True),  # authenticated
        ({"exit_code": 1}, False),  # logged out
        ({"launch_error": "could not launch 'gh'"}, None),  # unknown (launch failure)
        ({"timed_out": True, "exit_code": None}, None),  # unknown (timeout)
    ],
)
def test_gh_auth_ok_maps_exit_to_tristate(
    monkeypatch: pytest.MonkeyPatch, result_kwargs: dict[str, Any], expected: bool | None
) -> None:
    monkeypatch.setattr(
        detect, "run_process", lambda *a, **k: _fake_process_result(**result_kwargs)
    )
    assert detect.gh_auth_ok() is expected


def test_warn_if_gh_logged_out_emits_once_when_logged_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight, "has_gh", lambda: True)
    monkeypatch.setattr(preflight, "gh_auth_ok", lambda: False)
    emitted: list[str] = []
    preflight.warn_if_gh_logged_out(emitted.append)
    assert len(emitted) == 1
    # Generic text, no raw `gh auth status` output (no account login / token scopes leaked).
    assert "gh auth login" in emitted[0]


@pytest.mark.parametrize("auth", [True, None])
def test_warn_if_gh_logged_out_silent_when_authenticated_or_unknown(
    monkeypatch: pytest.MonkeyPatch, auth: bool | None
) -> None:
    # True (authenticated) and None (probe unknown — transient / env-token auth) both stay silent.
    monkeypatch.setattr(preflight, "has_gh", lambda: True)
    monkeypatch.setattr(preflight, "gh_auth_ok", lambda: auth)
    emitted: list[str] = []
    preflight.warn_if_gh_logged_out(emitted.append)
    assert emitted == []


def test_warn_if_gh_logged_out_silent_when_gh_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # gh missing is require_gh's job; the advisory must not probe auth or warn.
    monkeypatch.setattr(preflight, "has_gh", lambda: False)
    monkeypatch.setattr(
        preflight, "gh_auth_ok", lambda: pytest.fail("gh_auth_ok must not run when gh is absent")
    )
    emitted: list[str] = []
    preflight.warn_if_gh_logged_out(emitted.append)
    assert emitted == []


def test_warn_if_gh_logged_out_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even if the probe somehow raised, the advisory must never propagate / block the run. Here we
    # assert the happy path returns None and does not raise with the default (logger) emit.
    monkeypatch.setattr(preflight, "has_gh", lambda: True)
    monkeypatch.setattr(preflight, "gh_auth_ok", lambda: False)
    assert preflight.warn_if_gh_logged_out() is None  # default emit = logger; no exception


@pytest.mark.parametrize(
    ("marker", "content", "expected"),
    [
        ("pyproject.toml", "[project]\n", ["pytest"]),
        ("Cargo.toml", "[package]\n", ["cargo test"]),
        ("go.mod", "module x\n", ["go test ./..."]),
    ],
)
def test_propose_defaults_by_marker(
    tmp_path: Path, marker: str, content: str, expected: list[str]
) -> None:
    (tmp_path / marker).write_text(content, encoding="utf-8")
    assert propose_default_commands(tmp_path) == expected


def test_propose_defaults_node_uses_scripts(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "jest", "lint": "eslint ."}}', encoding="utf-8"
    )
    # Names are emitted in sorted logical order (lint before tests); npm is the default runner.
    assert propose_default_commands(tmp_path) == ["npm run lint", "npm test"]


def test_propose_defaults_node_is_lockfile_aware(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "vitest", "lint": "eslint ."}}', encoding="utf-8"
    )
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n", encoding="utf-8")
    # The lockfile makes the detector prefer pnpm over the first-match npm of the old installer.
    assert propose_default_commands(tmp_path) == ["pnpm run lint", "pnpm test"]


def test_propose_defaults_python_is_lockfile_aware(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n[dependency-groups]\ndev = ["pytest"]\n', encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    assert propose_default_commands(tmp_path) == ["uv run pytest"]


def test_propose_defaults_node_without_scripts(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name": "x"}', encoding="utf-8")
    assert propose_default_commands(tmp_path) == []


def test_propose_defaults_empty_when_no_markers(tmp_path: Path) -> None:
    assert propose_default_commands(tmp_path) == []
