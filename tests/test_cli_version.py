"""`--version` flag and the fail-loud CLI boundary for a backward-incompatible config (exit 2)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from wastech_orchestrator import __version__, cli
from wastech_orchestrator.observability import logging as obslog


@pytest.fixture(autouse=True)
def _reset_logger() -> Iterator[None]:
    pkg = logging.getLogger(obslog.LOGGER_NAME)
    saved = pkg.handlers[:]
    pkg.handlers.clear()
    obslog._configured = False
    yield
    pkg.handlers.clear()
    pkg.handlers.extend(saved)
    obslog._configured = False


def test_version_flag_prints_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "wastech-orchestrator" in out
    assert __version__ in out


def test_newer_config_schema_fails_loud_with_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("schema_version: 999\nrepo:\n  url: x\n", encoding="utf-8")
    rc = cli.main(["--config", str(config), "preflight"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "error:" in out
    assert "newer than this orchestrator supports" in out  # clean message, not a traceback
