"""End-to-end `install-templates` command.

Delivers the packaged ``templates/`` tree (beside ``config.yaml``) add-missing-only: writes absent
files, **skips** existing ones to preserve operator edits, is idempotent (all present → no-op),
overwrites under ``--force``, previews with ``--dry-run``, never removes operator-added files
(unlike ``upgrade-docs``), never touches ``config.yaml``, and fails closed (exit 2) when no config
can be resolved. The delivered tree is **prompts-only** (schema v6).
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from wastech_orchestrator import cli
from wastech_orchestrator.cli import _iter_template_files, _templates_root

# A stable file the packaged tree always carries (used as the "one missing file" probe).
PROBE = Path("prompts/review.md")


def _packaged() -> dict[Path, bytes]:
    """The packaged templates/ tree this command installs (config.example.yaml is out of scope)."""
    with resources.as_file(_templates_root()) as troot:
        return {
            rel: (Path(troot) / rel).read_bytes()
            for rel in _iter_template_files(Path(troot))
            if rel.name != "config.example.yaml"
        }


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("schema_version: 4\n", encoding="utf-8")
    return cfg


def _seed_templates(tmp_path: Path, files: dict[Path, bytes]) -> Path:
    templates = tmp_path / "templates"
    for rel, content in files.items():
        dest = templates / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    return templates


def test_fresh_install_writes_whole_tree(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)  # no templates/ beside it yet
    packaged = _packaged()

    assert cli.main(["--config", str(cfg), "install-templates"]) == 0
    for rel, content in packaged.items():
        assert (tmp_path / "templates" / rel).read_bytes() == content
    assert not (tmp_path / "templates" / "config.example.yaml").exists()


def test_single_missing_file_added_siblings_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _write_config(tmp_path)
    seeded = {rel: c for rel, c in _packaged().items() if rel != PROBE}
    _seed_templates(tmp_path, seeded)

    assert cli.main(["--config", str(cfg), "install-templates"]) == 0
    out = capsys.readouterr().out
    assert f"+ add {Path('templates') / PROBE}" in out
    assert f"skip {Path('templates') / 'prompts' / 'implementation.md'}" in out
    assert (tmp_path / "templates" / PROBE).read_bytes() == _packaged()[PROBE]


def test_delivered_tree_is_prompts_only(tmp_path: Path) -> None:
    # Schema v6: only prompts/ ships; skills/, AGENTS.md, CLAUDE.md, task.md are no longer shipped.
    cfg = _write_config(tmp_path)
    assert cli.main(["--config", str(cfg), "install-templates"]) == 0
    delivered = {
        p.relative_to(tmp_path / "templates").as_posix()
        for p in (tmp_path / "templates").rglob("*")
        if p.is_file()
    }
    assert delivered == {
        "prompts/refinement.md",
        "prompts/planning.md",
        "prompts/implementation.md",
        "prompts/review.md",
        "prompts/fixing.md",
        "prompts/summary.md",
    }


def test_already_complete_is_noop(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _write_config(tmp_path)
    templates = _seed_templates(tmp_path, _packaged())
    before = {p: p.read_bytes() for p in templates.rglob("*") if p.is_file()}

    assert cli.main(["--config", str(cfg), "install-templates"]) == 0
    assert "already complete" in capsys.readouterr().out
    after = {p: p.read_bytes() for p in templates.rglob("*") if p.is_file()}
    assert before == after  # nothing rewritten


def test_operator_edited_file_preserved_by_default(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    edited = dict(_packaged())
    edited[PROBE] = b"# operator edit\n"
    templates = _seed_templates(tmp_path, edited)

    assert cli.main(["--config", str(cfg), "install-templates"]) == 0
    assert (templates / PROBE).read_bytes() == b"# operator edit\n"  # not clobbered


def test_force_overwrites_existing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _write_config(tmp_path)
    edited = dict(_packaged())
    edited[PROBE] = b"# operator edit\n"
    templates = _seed_templates(tmp_path, edited)

    assert cli.main(["--config", str(cfg), "install-templates", "--force"]) == 0
    assert "~ overwrite" in capsys.readouterr().out
    assert (templates / PROBE).read_bytes() == _packaged()[PROBE]  # restored to packaged


def test_dry_run_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _write_config(tmp_path)
    seeded = {rel: c for rel, c in _packaged().items() if rel != PROBE}
    _seed_templates(tmp_path, seeded)

    assert cli.main(["--config", str(cfg), "install-templates", "--dry-run"]) == 0
    assert "dry-run" in capsys.readouterr().out
    assert not (tmp_path / "templates" / PROBE).exists()  # nothing written


def test_force_dry_run_previews_overwrite_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _write_config(tmp_path)
    edited = dict(_packaged())
    edited[PROBE] = b"# operator edit\n"
    templates = _seed_templates(tmp_path, edited)

    assert cli.main(["--config", str(cfg), "install-templates", "--force", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "~ overwrite" in out
    assert (templates / PROBE).read_bytes() == b"# operator edit\n"  # untouched


def test_config_example_yaml_is_never_written(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    assert cli.main(["--config", str(cfg), "install-templates"]) == 0
    assert not (tmp_path / "templates" / "config.example.yaml").exists()


def test_orphan_operator_file_is_not_removed(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    files = dict(_packaged())
    files[Path("prompts/custom.md")] = b"# my own template\n"
    templates = _seed_templates(tmp_path, files)

    assert cli.main(["--config", str(cfg), "install-templates"]) == 0
    assert (templates / "prompts/custom.md").exists()  # never removed (unlike upgrade-docs)


def test_does_not_touch_config_or_overrides(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("schema_version: 4\nprompts:\n  overrides: {}\n", encoding="utf-8")
    before = cfg.read_bytes()

    assert cli.main(["--config", str(cfg), "install-templates"]) == 0
    assert cfg.read_bytes() == before  # config.yaml / prompts.overrides untouched


def test_resolves_via_worc_config_walk_up(monkeypatch: pytest.MonkeyPatch, git_repo: Any) -> None:
    worc = git_repo.clone / ".worc"
    worc.mkdir()
    _write_config(worc)  # <repo>/.worc/config.yaml, discovered by walking up from the cwd
    nested = git_repo.clone / "src"
    nested.mkdir()
    monkeypatch.chdir(nested)

    assert cli.main(["install-templates"]) == 0
    assert (worc / "templates" / PROBE).is_file()  # lands beside the discovered config


def test_missing_config_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # no config.yaml here
    monkeypatch.setattr("wastech_orchestrator.install.detect.git_info", lambda _p: None)
    assert cli.main(["install-templates"]) == 2
    assert "no config.yaml found" in capsys.readouterr().out


def test_install_and_install_templates_produce_same_tree(
    git_repo: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shared-helper parity guard: `install` and `install-templates` deliver the same tree."""
    monkeypatch.setattr(
        "shutil.which", lambda n: f"/usr/bin/{n}" if n in {"git", "codex"} else None
    )
    argv = [
        "install",
        str(git_repo.clone),
        "--provider",
        "codex",
        "--skip-preflight",
        "--non-interactive",
    ]
    assert cli.main(argv) == 0

    b = git_repo.clone.parent / "via_install_templates"
    b.mkdir()
    cfg = _write_config(b)
    assert cli.main(["--config", str(cfg), "install-templates"]) == 0

    def _tree(base: Path) -> dict[Path, bytes]:
        return {p.relative_to(base): p.read_bytes() for p in base.rglob("*") if p.is_file()}

    assert _tree(git_repo.clone / ".worc" / "templates") == _tree(b / "templates")
