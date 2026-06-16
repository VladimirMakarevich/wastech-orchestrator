"""End-to-end `upgrade-docs` command.

Refreshes the installed ``.worc/guide/`` task-authoring docs to the packaged version: overwrites
stale files, adds missing ones, removes orphans, is idempotent (already-current → no-op), previews
with ``--dry-run``, and fails closed (exit 2) when no config can be resolved.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest

from wastech_orchestrator import cli
from wastech_orchestrator.cli import _iter_template_files, _worc_root


def _packaged() -> dict[Path, bytes]:
    with resources.as_file(_worc_root()) as wroot:
        return {rel: (Path(wroot) / rel).read_bytes() for rel in _iter_template_files(Path(wroot))}


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("schema_version: 4\n", encoding="utf-8")
    return cfg


def _seed_worc(tmp_path: Path, files: dict[Path, bytes]) -> Path:
    guide = tmp_path / "guide"  # the installed docs land in <config-dir>/guide/
    for rel, content in files.items():
        dest = guide / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    return guide


def test_stale_docs_are_updated(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    packaged = _packaged()
    stale = dict(packaged)
    stale[Path("README.md")] = b"# stale\n"
    worc = _seed_worc(tmp_path, stale)

    rc = cli.main(["--config", str(cfg), "upgrade-docs"])
    assert rc == 0
    assert (worc / "README.md").read_bytes() == packaged[Path("README.md")]


def test_already_current_is_noop(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _write_config(tmp_path)
    worc = _seed_worc(tmp_path, _packaged())
    before = {p: p.read_bytes() for p in worc.rglob("*") if p.is_file()}

    rc = cli.main(["--config", str(cfg), "upgrade-docs"])
    assert rc == 0
    assert "already up to date" in capsys.readouterr().out
    after = {p: p.read_bytes() for p in worc.rglob("*") if p.is_file()}
    assert before == after  # nothing rewritten


def test_dry_run_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _write_config(tmp_path)
    stale = dict(_packaged())
    stale[Path("README.md")] = b"# stale\n"
    worc = _seed_worc(tmp_path, stale)

    rc = cli.main(["--config", str(cfg), "upgrade-docs", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert (worc / "README.md").read_bytes() == b"# stale\n"  # untouched


def test_orphan_file_is_removed(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    files = dict(_packaged())
    files[Path("old-removed-doc.md")] = b"# no longer shipped\n"
    worc = _seed_worc(tmp_path, files)

    assert cli.main(["--config", str(cfg), "upgrade-docs"]) == 0
    assert not (worc / "old-removed-doc.md").exists()


def test_missing_guide_dir_is_populated(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)  # no guide/ beside it yet
    assert cli.main(["--config", str(cfg), "upgrade-docs"]) == 0
    assert (tmp_path / "guide" / "README.md").is_file()


def test_missing_config_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # no config.yaml here
    monkeypatch.setattr("wastech_orchestrator.install.detect.git_info", lambda _p: None)
    rc = cli.main(["upgrade-docs"])
    assert rc == 2
    assert "no config.yaml found" in capsys.readouterr().out
