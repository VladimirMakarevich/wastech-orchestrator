"""End-to-end `install` command DoD (backlog: interactive installer).

Drives ``main(["install", ...])`` against a real git clone (``git_repo`` fixture), with provider/gh
discovery faked via ``shutil.which`` and the registry redirected to a temp dir. Covers interactive
and non-interactive runs, routing modes, idempotency/reconfigure/backup, foreign-workspace refusal,
dry-run, target-repo immutability, gh-gated PR default, and the post-write preflight wiring.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from wastech_orchestrator import cli
from wastech_orchestrator.config.loader import load_config
from wastech_orchestrator.install import registry
from wastech_orchestrator.observability import logging as obslog
from wastech_orchestrator.providers.base import ProviderId

GitRunner = Callable[[list[str], Path], str]


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Redirect the registry to a temp dir and reset the package logger around each test."""
    monkeypatch.setenv(registry.HOME_ENV, str(tmp_path / "registry-home"))
    pkg = logging.getLogger(obslog.LOGGER_NAME)
    saved = pkg.handlers[:]
    pkg.handlers.clear()
    obslog._configured = False
    yield
    pkg.handlers.clear()
    pkg.handlers.extend(saved)
    obslog._configured = False


def _present(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """Fake ``shutil.which`` so only ``git`` plus ``names`` resolve on PATH."""
    available = {"git", *names}
    monkeypatch.setattr("shutil.which", lambda n: f"/usr/bin/{n}" if n in available else None)


def _ni(clone: Path, *extra: str) -> list[str]:
    """argv for a non-interactive install of ``clone`` with the given extra flags."""
    return ["install", str(clone), "--non-interactive", *extra]


def _config_for(clone: Path) -> Path | None:
    bound = registry.lookup(clone)
    return Path(bound) if bound is not None else None


def _loaded(clone: Path) -> Any:
    config_path = _config_for(clone)
    assert config_path is not None
    return load_config(config_path).config


def test_non_interactive_codex_only(git_repo: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from wastech_orchestrator.config.schema import FootprintLocation, FootprintTracking

    _present(monkeypatch, "codex")
    rc = cli.main(_ni(git_repo.clone, "--provider", "codex", "--skip-preflight"))
    assert rc == 0
    config_path = _config_for(git_repo.clone)
    assert config_path is not None and config_path.is_file()
    workspace = config_path.parent
    assert workspace.name == f"{git_repo.clone.name}-orchestrator"
    # in_repo footprint: the task lifecycle + artifact dirs live in the repo; the quarantine for
    # rejected tasks stays in the control workspace, out of the repo (§21).
    assert (git_repo.clone / "tasks" / "pending").is_dir()
    assert (git_repo.clone / "logs").is_dir()
    assert (workspace / "tasks" / "rejected").is_dir()
    cfg = load_config(config_path).config
    assert cfg.agents.allowed == (ProviderId.CODEX,)
    assert cfg.git.footprint.location is FootprintLocation.IN_REPO
    assert cfg.git.footprint.tracking is FootprintTracking.COMMIT
    assert cfg.repo.local_path == str(git_repo.clone.resolve())


def test_non_interactive_both_routing(git_repo: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _present(monkeypatch, "codex", "claude")
    assert cli.main(_ni(git_repo.clone, "--provider", "both", "--skip-preflight")) == 0
    assert set(_loaded(git_repo.clone).agents.allowed) == {ProviderId.CODEX, ProviderId.CLAUDE}


def test_auto_selects_available_provider(git_repo: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _present(monkeypatch, "claude")  # only claude on PATH
    assert cli.main(_ni(git_repo.clone, "--skip-preflight")) == 0
    assert _loaded(git_repo.clone).agents.allowed == (ProviderId.CLAUDE,)


def test_auto_with_no_provider_errors_and_writes_nothing(
    git_repo: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _present(monkeypatch)  # only git
    assert cli.main(_ni(git_repo.clone, "--skip-preflight")) == 1
    assert "no agent CLI" in capsys.readouterr().out
    assert _config_for(git_repo.clone) is None


def test_missing_gh_defaults_create_pr_false(
    git_repo: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _present(monkeypatch, "codex")  # no gh
    cli.main(_ni(git_repo.clone, "--provider", "codex", "--skip-preflight"))
    assert _loaded(git_repo.clone).git.create_pull_request is False


def test_create_pr_flag_overrides_missing_gh(
    git_repo: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _present(monkeypatch, "codex")
    cli.main(_ni(git_repo.clone, "--provider", "codex", "--create-pr", "--skip-preflight"))
    assert _loaded(git_repo.clone).git.create_pull_request is True


def test_dry_run_writes_nothing(
    git_repo: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _present(monkeypatch, "codex")
    assert cli.main(_ni(git_repo.clone, "--provider", "codex", "--dry-run")) == 0
    assert "dry-run" in capsys.readouterr().out
    assert _config_for(git_repo.clone) is None
    workspace = git_repo.clone.parent / f"{git_repo.clone.name}-orchestrator"
    assert not (workspace / "config.yaml").exists()


def test_idempotent_second_run(
    git_repo: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _present(monkeypatch, "codex")
    argv = _ni(git_repo.clone, "--provider", "codex", "--skip-preflight")
    assert cli.main(argv) == 0
    config_path = _config_for(git_repo.clone)
    assert config_path is not None
    first = config_path.read_text(encoding="utf-8")
    capsys.readouterr()

    assert cli.main(argv) == 0
    assert "already configured" in capsys.readouterr().out
    assert config_path.read_text(encoding="utf-8") == first  # unchanged


def test_reconfigure_backs_up_and_replaces(git_repo: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _present(monkeypatch, "codex", "claude")
    assert cli.main(_ni(git_repo.clone, "--provider", "codex", "--skip-preflight")) == 0
    config_path = _config_for(git_repo.clone)
    assert config_path is not None

    redo = _ni(git_repo.clone, "--provider", "both", "--reconfigure", "--skip-preflight")
    assert cli.main(redo) == 0
    backups = list(config_path.parent.glob("config.yaml.bak-*"))
    assert len(backups) == 1
    assert set(load_config(config_path).config.agents.allowed) == {
        ProviderId.CODEX,
        ProviderId.CLAUDE,
    }  # regenerated


def test_foreign_workspace_is_not_overwritten(
    git_repo: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _present(monkeypatch, "codex")
    workspace = git_repo.clone.parent / f"{git_repo.clone.name}-orchestrator"
    workspace.mkdir()
    (workspace / "config.yaml").write_text("# someone else's config\n", encoding="utf-8")

    assert cli.main(_ni(git_repo.clone, "--provider", "codex", "--skip-preflight")) == 1
    assert "not bound" in capsys.readouterr().out
    assert (workspace / "config.yaml").read_text(encoding="utf-8") == "# someone else's config\n"
    assert _config_for(git_repo.clone) is None


def test_target_repo_is_left_unchanged(
    git_repo: Any, monkeypatch: pytest.MonkeyPatch, git_run: GitRunner
) -> None:
    _present(monkeypatch, "codex")
    head_before = git_run(["rev-parse", "HEAD"], git_repo.clone)
    cli.main(_ni(git_repo.clone, "--provider", "codex", "--skip-preflight"))
    assert git_run(["status", "--porcelain"], git_repo.clone) == ""  # nothing staged or created
    assert git_run(["rev-parse", "HEAD"], git_repo.clone) == head_before


def test_explicit_provider_missing_writes_config_but_preflight_fails(
    git_repo: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _present(monkeypatch)  # codex not on PATH
    failing = (False, ["codex: FAIL — not found", "preflight: NOT ready"])
    monkeypatch.setattr(cli, "run_preflight", lambda _c: failing)
    assert cli.main(_ni(git_repo.clone, "--provider", "codex")) == 1
    out = capsys.readouterr().out
    assert "not on PATH" in out
    assert "preflight is NOT ready" in out
    assert _config_for(git_repo.clone) is not None  # config kept despite the failed preflight


def test_successful_preflight_exits_zero(
    git_repo: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _present(monkeypatch, "codex")
    monkeypatch.setattr(cli, "run_preflight", lambda _c: (True, ["codex: OK", "preflight: ready"]))
    assert cli.main(_ni(git_repo.clone, "--provider", "codex")) == 0
    assert "preflight: ready" in capsys.readouterr().out


def test_interactive_run_takes_defaults(git_repo: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _present(monkeypatch, "codex", "claude", "gh")
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")  # accept every default
    assert cli.main(["install", str(git_repo.clone), "--skip-preflight"]) == 0
    cfg = _loaded(git_repo.clone)
    assert set(cfg.agents.allowed) == {ProviderId.CODEX, ProviderId.CLAUDE}
    assert cfg.git.create_pull_request is True  # gh present + default yes
