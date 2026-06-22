"""Wizard DoD: resolving an InstallSpec from flags/detection/answers, and every hard stop
(not a repo, no origin, no provider, dirty+declined, aborted confirm)."""

from __future__ import annotations

from pathlib import Path

import pytest

from wastech_orchestrator.install import detect, wizard
from wastech_orchestrator.install.detect import GitInfo
from wastech_orchestrator.install.wizard import InstallError, run_wizard
from wastech_orchestrator.providers.base import ProviderId


class _ScriptedPrompter:
    """A deterministic Prompter: pops queued answers, falling back to each prompt's default."""

    def __init__(
        self,
        *,
        answers: list[str] | None = None,
        confirms: list[bool] | None = None,
        lists: list[list[str]] | None = None,
    ) -> None:
        self.answers = list(answers or [])
        self.confirms = list(confirms or [])
        self.lists = list(lists or [])

    def info(self, message: str) -> None:  # noqa: D102
        pass

    def ask(self, prompt: str, *, default: str) -> str:  # noqa: D102
        return self.answers.pop(0) if self.answers else default

    def confirm(self, prompt: str, *, default: bool) -> bool:  # noqa: D102
        return self.confirms.pop(0) if self.confirms else default

    def ask_list(self, prompt: str) -> list[str]:  # noqa: D102
        return self.lists.pop(0) if self.lists else []


def _patch_detect(
    monkeypatch: pytest.MonkeyPatch,
    *,
    root: Path,
    origin: str | None = "git@github.com:o/r.git",
    clean: bool = True,
    providers: tuple[str, ...] = ("codex", "claude"),
    gh: bool = True,
    checks: tuple[str, ...] = (),
) -> None:
    info = GitInfo(
        root=root, origin_url=origin, current_branch="main", default_branch="main", is_clean=clean
    )
    monkeypatch.setattr(detect, "git_info", lambda _cwd: info)
    monkeypatch.setattr(detect, "find_executable", lambda name: "/git" if name == "git" else None)
    detected = {
        ProviderId.CODEX: "/codex" if "codex" in providers else None,
        ProviderId.CLAUDE: "/claude" if "claude" in providers else None,
    }
    monkeypatch.setattr(detect, "detect_providers", lambda: detected)
    monkeypatch.setattr(detect, "has_gh", lambda: gh)
    monkeypatch.setattr(wizard, "propose_default_commands", lambda _root: list(checks))


def _run(monkeypatch: pytest.MonkeyPatch, root: Path, **kwargs: object) -> wizard.WizardOutcome:
    defaults: dict[str, object] = {
        "repo_path": root,
        "provider": "auto",
        "checks": None,
        "create_pr": None,
        "auto_mode": None,
        "non_interactive": True,
        "prompter": _ScriptedPrompter(),
    }
    defaults.update(kwargs)
    return run_wizard(**defaults)  # type: ignore[arg-type]


def test_auto_selects_all_present_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _patch_detect(monkeypatch, root=root, providers=("codex", "claude"))
    outcome = _run(monkeypatch, root)
    assert set(outcome.spec.providers) == {ProviderId.CODEX, ProviderId.CLAUDE}
    assert outcome.spec.repo_local_path == root
    assert outcome.spec.base_branch == "main"
    assert outcome.missing_providers == ()


def test_auto_with_one_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    _patch_detect(monkeypatch, root=root, providers=("codex",))
    assert _run(monkeypatch, root).spec.providers == (ProviderId.CODEX,)


def test_auto_with_no_provider_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    _patch_detect(monkeypatch, root=root, providers=())
    with pytest.raises(InstallError, match="no agent CLI"):
        _run(monkeypatch, root)


def test_explicit_provider_missing_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _patch_detect(monkeypatch, root=root, providers=())  # neither on PATH
    outcome = _run(monkeypatch, root, provider="codex")
    assert outcome.spec.providers == (ProviderId.CODEX,)
    assert outcome.missing_providers == (ProviderId.CODEX,)


def test_not_a_repo_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(detect, "find_executable", lambda name: "/git")
    monkeypatch.setattr(detect, "git_info", lambda _cwd: None)
    with pytest.raises(InstallError, match="not inside a Git repository"):
        _run(monkeypatch, tmp_path)


def test_missing_git_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(detect, "find_executable", lambda name: None)
    with pytest.raises(InstallError, match="git was not found"):
        _run(monkeypatch, tmp_path)


def test_missing_origin_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    _patch_detect(monkeypatch, root=root, origin=None)
    with pytest.raises(InstallError, match="no 'origin' remote"):
        _run(monkeypatch, root)


def test_dirty_repo_warns_but_proceeds_when_non_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _patch_detect(monkeypatch, root=root, clean=False)
    assert _run(monkeypatch, root).spec.repo_local_path == root  # no raise


def test_dirty_repo_declined_aborts_in_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _patch_detect(monkeypatch, root=root, clean=False)
    prompter = _ScriptedPrompter(confirms=[False])  # decline the cleanliness prompt
    with pytest.raises(InstallError, match="not clean"):
        _run(
            monkeypatch,
            root,
            non_interactive=False,
            checks=["x"],
            create_pr=False,
            auto_mode=False,
            prompter=prompter,
        )


def test_aborted_final_confirm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    _patch_detect(monkeypatch, root=root, providers=("codex",))
    prompter = _ScriptedPrompter(confirms=[False])  # the only confirm is the final "write?"
    with pytest.raises(InstallError, match="aborted"):
        _run(
            monkeypatch,
            root,
            non_interactive=False,
            checks=["x"],
            create_pr=False,
            auto_mode=False,
            prompter=prompter,
        )


def test_explicit_checks_override_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _patch_detect(monkeypatch, root=root, checks=("pytest",))
    outcome = _run(monkeypatch, root, checks=["cargo test", "cargo clippy"])
    assert outcome.spec.checks == ("cargo test", "cargo clippy")


def test_detected_checks_used_in_non_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _patch_detect(monkeypatch, root=root, checks=("pytest",))
    assert _run(monkeypatch, root).spec.checks == ("pytest",)


def test_discovery_mode_configured_when_checks_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _patch_detect(monkeypatch, root=root, checks=("pytest",))
    assert _run(monkeypatch, root).spec.discovery_mode == "configured"


def test_discovery_mode_auto_when_no_checks_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _patch_detect(monkeypatch, root=root, checks=())  # nothing detected
    assert _run(monkeypatch, root).spec.discovery_mode == "auto"


def test_interactive_checks_entered_one_by_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _patch_detect(monkeypatch, root=root, providers=("codex",), checks=())  # nothing detected
    prompter = _ScriptedPrompter(lists=[["pytest", "ruff check ."]], confirms=[True])
    outcome = _run(
        monkeypatch,
        root,
        non_interactive=False,
        create_pr=False,
        auto_mode=False,
        prompter=prompter,
    )
    assert outcome.spec.checks == ("pytest", "ruff check .")


def test_create_pr_defaults_to_gh_presence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    _patch_detect(monkeypatch, root=root, providers=("codex",), gh=False)
    assert _run(monkeypatch, root).spec.create_pull_request is False

    _patch_detect(monkeypatch, root=root, providers=("codex",), gh=True)
    assert _run(monkeypatch, root).spec.create_pull_request is True


def test_flags_override_create_pr_and_auto_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _patch_detect(monkeypatch, root=root, providers=("codex",), gh=False)
    outcome = _run(monkeypatch, root, create_pr=True, auto_mode=True)
    assert outcome.spec.create_pull_request is True
    assert outcome.spec.auto_mode is True
