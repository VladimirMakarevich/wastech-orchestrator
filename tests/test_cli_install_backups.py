"""`install --reconfigure` bounds its own snapshots: keep-last-N for config / flows / tools.

These backups are written by the orchestrator on every refresh and were never reclaimed —
`flows.bak-*` and `tools.bak-*` are whole-directory copies, so the series is what costs disk. What
the orchestrator did not write, it does not delete: the prefix keeps `state.db*.bak*` out of scope,
and matching the timestamp shape keeps a hand-named backup out too.
"""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator import cli


def _stamped(worc_home: Path, prefix: str, count: int, *, directory: bool) -> list[Path]:
    """Create ``count`` chronologically-named ``<prefix>.bak-<UTC>`` snapshots, oldest first."""
    made: list[Path] = []
    for i in range(count):
        path = worc_home / f"{prefix}.bak-2026070{i}T000000Z"
        if directory:
            path.mkdir(parents=True)
            (path / "payload.yaml").write_text("x\n", encoding="utf-8")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n", encoding="utf-8")
        made.append(path)
    return made


def test_prune_keeps_the_newest_n_files(tmp_path: Path) -> None:
    made = _stamped(tmp_path, "config.yaml", 6, directory=False)
    cli._prune_install_backups(tmp_path, "config.yaml")
    survivors = {p.name for p in tmp_path.glob("config.yaml.bak-*")}
    assert survivors == {p.name for p in made[-cli._INSTALL_BACKUP_KEEP :]}


def test_prune_keeps_the_newest_n_directories(tmp_path: Path) -> None:
    made = _stamped(tmp_path, "flows", 5, directory=True)
    cli._prune_install_backups(tmp_path, "flows")
    survivors = {p.name for p in tmp_path.glob("flows.bak-*")}
    assert survivors == {p.name for p in made[-cli._INSTALL_BACKUP_KEEP :]}


def test_prune_under_the_bound_removes_nothing(tmp_path: Path) -> None:
    _stamped(tmp_path, "tools", cli._INSTALL_BACKUP_KEEP, directory=True)
    cli._prune_install_backups(tmp_path, "tools")
    assert len(list(tmp_path.glob("tools.bak-*"))) == cli._INSTALL_BACKUP_KEEP


def test_prune_never_touches_operator_made_backups(tmp_path: Path) -> None:
    # The operator makes these by hand before campaign runs; the orchestrator has no business
    # deleting them. Two guarantees: the prefix keeps `state.db*` out of scope entirely, and
    # matching the UTC *stamp shape* keeps a hand-named backup of our own files out too.
    operator_made = [
        tmp_path / "state.db.bak-manual",
        tmp_path / "state.db.bak-2026-07-01",
        tmp_path / "state.db-wal.bak",
        tmp_path / "config.yaml.bak-before-upgrade",
        tmp_path / "config.yaml.bak-2026-07-01",  # not the %Y%m%dT%H%M%SZ shape we write
    ]
    for path in operator_made:
        path.write_text("db\n", encoding="utf-8")
    _stamped(tmp_path, "config.yaml", 6, directory=False)
    cli._prune_install_backups(tmp_path, "config.yaml")
    for path in operator_made:
        assert path.is_file(), path.name


def test_prune_leaves_the_live_directories_alone(tmp_path: Path) -> None:
    # `flows.bak-*` must not be read as "anything starting with flows": the live dir is the install.
    (tmp_path / "flows").mkdir()
    (tmp_path / "flows" / "implementation.yaml").write_text("x\n", encoding="utf-8")
    _stamped(tmp_path, "flows", 5, directory=True)
    cli._prune_install_backups(tmp_path, "flows")
    assert (tmp_path / "flows" / "implementation.yaml").is_file()


def test_backing_up_flows_prunes_the_series(tmp_path: Path) -> None:
    worc_home = tmp_path / ".worc"
    flows = worc_home / "flows"
    flows.mkdir(parents=True)
    (flows / "implementation.yaml").write_text("x\n", encoding="utf-8")
    _stamped(worc_home, "flows", cli._INSTALL_BACKUP_KEEP + 2, directory=True)
    backup = cli._backup_flows_dir(worc_home)
    assert backup is not None and backup.is_dir()
    # The fresh snapshot counts toward the bound, so the total never exceeds it.
    assert len(list(worc_home.glob("flows.bak-*"))) == cli._INSTALL_BACKUP_KEEP


def test_backing_up_tools_prunes_the_series(tmp_path: Path) -> None:
    worc_home = tmp_path / ".worc"
    tools = worc_home / "tools"
    tools.mkdir(parents=True)
    (tools / "check_chapter").write_text("#!/bin/sh\n", encoding="utf-8")
    _stamped(worc_home, "tools", cli._INSTALL_BACKUP_KEEP + 1, directory=True)
    assert cli._backup_tools_dir(worc_home) is not None
    assert len(list(worc_home.glob("tools.bak-*"))) == cli._INSTALL_BACKUP_KEEP


def test_backing_up_config_prunes_the_series(tmp_path: Path) -> None:
    worc_home = tmp_path / ".worc"
    worc_home.mkdir()
    config = worc_home / "config.yaml"
    config.write_text("schema_version: 32\n", encoding="utf-8")
    _stamped(worc_home, "config.yaml", cli._INSTALL_BACKUP_KEEP + 3, directory=False)
    assert cli._install_backup_config(config).is_file()
    assert len(list(worc_home.glob("config.yaml.bak-*"))) == cli._INSTALL_BACKUP_KEEP
    assert config.is_file()  # the live config is never a prune target
