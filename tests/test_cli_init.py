"""`init` + templates DoD (spec §20): idempotency, no-overwrite, --dry-run, --git-mode footprint."""

from __future__ import annotations

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
