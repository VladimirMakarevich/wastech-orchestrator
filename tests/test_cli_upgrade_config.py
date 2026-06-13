"""End-to-end `upgrade-config` command.

Drives ``main(["--config", ..., "upgrade-config"])`` against a config file written to a temp dir:
adds keys the current format introduced, preserves operator values, stamps ``schema_version``, backs
up the original, and is idempotent / fail-closed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from wastech_orchestrator import cli
from wastech_orchestrator.config.loader import load_config
from wastech_orchestrator.config.schema import CONFIG_SCHEMA_VERSION
from wastech_orchestrator.config.upgrade import packaged_template_mapping
from wastech_orchestrator.config.validation import validate_config


def _old_config_text(**agent_overrides: object) -> str:
    """The packaged template as it would look one version back: no skip-stage keys, version 3."""
    m = packaged_template_mapping()
    m["schema_version"] = 3
    m["agents"].pop("skip_stages", None)
    m["agents"].pop("allow_review_skip", None)
    m["agents"].update(agent_overrides)
    return yaml.safe_dump(m, sort_keys=False)


def _write(tmp_path: Path, text: str) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(text, encoding="utf-8")
    return cfg


def test_upgrade_adds_new_keys_and_bumps_version(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _old_config_text())
    rc = cli.main(["--config", str(cfg), "upgrade-config"])
    assert rc == 0

    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["schema_version"] == CONFIG_SCHEMA_VERSION
    assert "skip_stages" in data["agents"]
    assert "allow_review_skip" in data["agents"]
    # A timestamped backup of the original was written, and the result loads + validates clean.
    assert len(list(tmp_path.glob("config.yaml.bak-*"))) == 1
    validate_config(load_config(cfg).config)


def test_upgrade_preserves_operator_values(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _old_config_text(max_fix_cycles=5))
    assert cli.main(["--config", str(cfg), "upgrade-config"]) == 0
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["agents"]["max_fix_cycles"] == 5  # operator's tuned value survives


def test_dry_run_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    original = _old_config_text()
    cfg = _write(tmp_path, original)
    rc = cli.main(["--config", str(cfg), "upgrade-config", "--dry-run"])
    assert rc == 0
    assert cfg.read_text(encoding="utf-8") == original  # untouched
    assert list(tmp_path.glob("config.yaml.bak-*")) == []  # no backup
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "skip_stages" in out


def test_already_current_is_noop(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # A config already at the current shape is left byte-for-byte untouched (comments survive).
    current = yaml.safe_dump(packaged_template_mapping(), sort_keys=False)
    cfg = _write(tmp_path, current)
    rc = cli.main(["--config", str(cfg), "upgrade-config"])
    assert rc == 0
    assert cfg.read_text(encoding="utf-8") == current
    assert list(tmp_path.glob("config.yaml.bak-*")) == []
    assert "already up to date" in capsys.readouterr().out


def test_missing_config_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # no config.yaml here
    monkeypatch.setattr("wastech_orchestrator.install.detect.git_info", lambda _p: None)
    rc = cli.main(["upgrade-config"])
    assert rc == 2
    assert "no config.yaml found" in capsys.readouterr().out


def test_newer_schema_version_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    m = packaged_template_mapping()
    m["schema_version"] = CONFIG_SCHEMA_VERSION + 1
    cfg = _write(tmp_path, yaml.safe_dump(m, sort_keys=False))
    rc = cli.main(["--config", str(cfg), "upgrade-config"])
    assert rc == 2
    assert "newer than this orchestrator" in capsys.readouterr().out
