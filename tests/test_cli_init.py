"""`init` + templates DoD (spec §20): idempotency, no-overwrite, --dry-run, --git-mode footprint."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from wastech_orchestrator.cli import _templates_root, main
from wastech_orchestrator.config.loader import load_config
from wastech_orchestrator.config.schema import FootprintLocation, FootprintTracking
from wastech_orchestrator.config.validation import validate_config


def test_templates_are_discoverable() -> None:
    # Packaged via importlib.resources, so `init` works from an installed wheel too.
    assert _templates_root().joinpath("config.example.yaml").is_file()


def test_init_creates_layout_and_config(tmp_path: Path) -> None:
    assert main(["init", str(tmp_path), "--quiet"]) == 0
    assert (tmp_path / "config.yaml").is_file()
    for rel in ("tasks/pending", "logs", "workspace", "templates/prompts"):
        assert (tmp_path / rel).is_dir()


def test_second_run_is_all_skipped_and_exit_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init", str(tmp_path), "--quiet"]) == 0
    capsys.readouterr()
    assert main(["init", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "0 created" in out


def test_config_yaml_never_overwritten_even_with_force(tmp_path: Path) -> None:
    assert main(["init", str(tmp_path), "--quiet"]) == 0
    config = tmp_path / "config.yaml"
    config.write_text("# operator edits\n", encoding="utf-8")
    assert main(["init", str(tmp_path), "--force", "--quiet"]) == 0
    assert config.read_text(encoding="utf-8") == "# operator edits\n"


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    assert main(["init", str(tmp_path), "--dry-run", "--quiet"]) == 0
    assert list(tmp_path.iterdir()) == []


_RUNTIME_IGNORES = (
    "state.db",
    "state.db-wal",
    "state.db-shm",
    "config.yaml.bak-*",
    "orchestrator.pid",
    "worc/",
    "checks/",
)


def test_init_copies_worc_docs(tmp_path: Path) -> None:
    assert main(["init", str(tmp_path), "--quiet"]) == 0
    # The agent task-authoring guide lands beside config.yaml, not under templates/.
    assert (tmp_path / "worc" / "README.md").is_file()
    assert (tmp_path / "worc" / "examples" / "task-minimal.md").is_file()


def test_init_worc_docs_skipped_on_second_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init", str(tmp_path), "--quiet"]) == 0
    capsys.readouterr()
    assert main(["init", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "skip worc/README.md" in out


def test_init_force_recopies_worc_docs(tmp_path: Path) -> None:
    assert main(["init", str(tmp_path), "--quiet"]) == 0
    readme = tmp_path / "worc" / "README.md"
    readme.write_text("# edited\n", encoding="utf-8")
    assert main(["init", str(tmp_path), "--force", "--quiet"]) == 0
    assert readme.read_text(encoding="utf-8") != "# edited\n"  # re-copied from the package


def test_gitignore_tracked_writes_runtime_block(tmp_path: Path) -> None:
    assert main(["init", str(tmp_path), "--gitignore-tracked", "--quiet"]) == 0
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    for pattern in _RUNTIME_IGNORES:
        assert pattern in text


def test_gitignore_tracked_is_idempotent(tmp_path: Path) -> None:
    assert main(["init", str(tmp_path), "--gitignore-tracked", "--quiet"]) == 0
    first = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert main(["init", str(tmp_path), "--gitignore-tracked", "--quiet"]) == 0
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == first


def test_external_mode_skips_runtime_excludes(tmp_path: Path) -> None:
    # External footprint keeps runtime files out of the repo, so there is nothing to exclude.
    argv = ["init", str(tmp_path), "--git-mode", "external", "--gitignore-tracked", "--quiet"]
    assert main(argv) == 0
    gitignore = tmp_path / ".gitignore"
    assert not gitignore.exists() or "runtime files" not in gitignore.read_text(encoding="utf-8")


def test_default_mode_writes_git_info_exclude_in_a_git_repo(
    tmp_path: Path, git_run: Callable[[Sequence[str], Path], str]
) -> None:
    git_run(["init", "-b", "main", "."], tmp_path)
    assert main(["init", str(tmp_path), "--quiet"]) == 0
    exclude = (tmp_path / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "orchestrator.pid" in exclude
    assert "state.db-wal" in exclude
    assert not (tmp_path / ".gitignore").exists()  # default mode leaves tracked .gitignore alone


@pytest.mark.parametrize(
    ("git_mode", "location", "tracking"),
    [
        ("external", "location: external", "tracking: none"),
        ("in_repo_exclude", "location: in_repo", "tracking: exclude_local"),
        ("in_repo_commit", "location: in_repo", "tracking: commit"),
    ],
)
def test_git_mode_seeds_footprint_defaults(
    tmp_path: Path, git_mode: str, location: str, tracking: str
) -> None:
    assert main(["init", str(tmp_path), "--git-mode", git_mode, "--quiet"]) == 0
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert location in text
    assert tracking in text


@pytest.mark.parametrize(
    ("git_mode", "expected_location", "expected_tracking"),
    [
        ("external", FootprintLocation.EXTERNAL, FootprintTracking.NONE),
        ("in_repo_exclude", FootprintLocation.IN_REPO, FootprintTracking.EXCLUDE_LOCAL),
        ("in_repo_commit", FootprintLocation.IN_REPO, FootprintTracking.COMMIT),
    ],
)
def test_generated_config_loads_and_validates_clean(
    tmp_path: Path,
    git_mode: str,
    expected_location: FootprintLocation,
    expected_tracking: FootprintTracking,
) -> None:
    assert main(["init", str(tmp_path), "--git-mode", git_mode, "--quiet"]) == 0
    result = load_config(tmp_path / "config.yaml")
    assert result.config.git.footprint.location is expected_location
    assert result.config.git.footprint.tracking is expected_tracking
    assert validate_config(result.config) == []
